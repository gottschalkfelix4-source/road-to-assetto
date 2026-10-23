"""Bounded routing, provider validation and cache regression tests (offline)."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from webapp.geodata import (BU, GML, WFS, GeoError, GeoService, TO_WGS,
                           _bounds, _point, _wgs_bounds, plan_route)


BASE = (462000, 5556400)


def position(x, y=0):
    lon, lat = TO_WGS.transform(BASE[0] + x, BASE[1] + y)
    return [lat, lon]


def osm(ways=None, points=None):
    points = points or {1: (0, 0), 2: (100, 0), 3: (200, 0), 4: (300, 0), 5: (400, 0)}
    ways = ways or [(10, [1, 2, 3, 4, 5], {'highway': 'residential', 'name': 'Teststraße'})]
    doc = ET.Element('osm', version='0.6')
    for identifier, point in points.items():
        lat, lon = position(*point)
        ET.SubElement(doc, 'node', id=str(identifier), lat=str(lat), lon=str(lon))
    for identifier, nodes, tags in ways:
        way = ET.SubElement(doc, 'way', id=str(identifier))
        for node in nodes:
            ET.SubElement(way, 'nd', ref=str(node))
        for key, value in tags.items():
            ET.SubElement(way, 'tag', k=key, v=value)
    return ET.tostring(doc)


class RouteTests(unittest.TestCase):
    bounds = [BASE[0] - 250, BASE[1] - 250, BASE[0] + 700, BASE[1] + 250]

    def test_snaps_to_segment_and_keeps_selected_direction(self):
        result = plan_route(osm(), position(350, 8), position(25, -10), self.bounds)
        self.assertAlmostEqual(result['length_m'], 325, places=2)
        self.assertEqual(result['ways'][0]['route_start_node'], -1)
        self.assertEqual(result['ways'][0]['nodes'], [-1, 4, 3, 2, -2])
        self.assertEqual(result['source_way_ids'], [10])

    def test_same_edge_snaps_remain_connected(self):
        data = osm([(10, [1, 5], {'highway': 'residential'})])
        result = plan_route(data, position(35), position(350), self.bounds)
        self.assertAlmostEqual(result['length_m'], 315, places=2)

    def test_private_and_non_motor_paths_rejected(self):
        for extra in ({'access': 'private'}, {'highway': 'footway'}):
            with self.subTest(extra=extra):
                data = osm([(10, [1, 2, 3, 4, 5], {'highway': 'residential', **extra})])
                with self.assertRaises(GeoError):
                    plan_route(data, position(0), position(400), self.bounds)

    def test_structure_tags_survive_local_planning(self):
        for extra in ({'bridge':'viaduct'},{'tunnel':'building_passage'},{'layer':'1'}):
            result=plan_route(osm([(10,[1,2,3,4,5],{'highway':'residential',**extra})]),position(0),position(400),self.bounds)
            self.assertTrue(all(result['ways'][0]['tags'][k]==v for k,v in extra.items()))

    def test_oneway_direction_and_disconnected_components(self):
        data = osm([(10, [1, 2, 3, 4, 5], {'highway': 'residential', 'oneway': 'yes'})])
        self.assertAlmostEqual(plan_route(data, position(0), position(400), self.bounds)['length_m'], 400)
        with self.assertRaisesRegex(GeoError, 'zusammenhängende'):
            plan_route(data, position(400), position(0), self.bounds)
        disconnected = osm([(10, [1, 2], {'highway': 'residential'}), (11, [4, 5], {'highway': 'residential'})])
        with self.assertRaisesRegex(GeoError, 'zusammenhängende'):
            plan_route(disconnected, position(0), position(400), self.bounds)

    def test_uses_connected_branch_instead_of_straight_line(self):
        points = {1: (0, 0), 2: (100, 0), 3: (100, 100), 4: (300, 100), 5: (400, 100)}
        data = osm([(10, [1, 2], {'highway': 'residential'}), (11, [2, 3, 4, 5], {'highway': 'residential'})], points)
        result = plan_route(data, position(0), position(400, 100), self.bounds)
        self.assertAlmostEqual(result['length_m'], 500, places=2)
        self.assertEqual(result['source_way_ids'], [10, 11])

    def test_snap_and_area_limits(self):
        with self.assertRaisesRegex(GeoError, '60 m'):
            plan_route(osm(), position(0, 70), position(400), self.bounds)
        with self.assertRaisesRegex(GeoError, 'zu groß'):
            _bounds([(0, 0), (2000, 2000)], 160)

    def test_invalid_coordinates(self):
        for value in ([True, 8.4], [float('nan'), 8.4], [50, float('inf')], [48, 8.4], {'lat': 50}, 'https://x'):
            with self.subTest(value=value), self.assertRaises(GeoError):
                _point(value)


class ProviderTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.service = GeoService(self.root)

    def test_search_filters_state_and_caches(self):
        results = [{'osm_type': 'way', 'osm_id': 10, 'lat': '50.16', 'lon': '8.47', 'display_name': 'Teststraße, Hessen',
                    'address': {'country_code': 'de', 'state': 'Hessen'}, 'boundingbox': ['50.15', '50.17', '8.46', '8.48']},
                   {'lat': 50.16, 'lon': 8.47, 'address': {'country_code': 'de', 'state': 'Bayern'}}]
        with patch.object(self.service, '_request_bytes', return_value=json.dumps(results).encode()) as request:
            first = self.service.search('Teststraße')
            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]['bbox'], [8.46, 50.15, 8.48, 50.17])
            self.assertEqual(first, self.service.search('Teststraße'))
            self.assertEqual(request.call_count, 1)

    def test_route_id_blocks_traversal(self):
        for value in ('../secret', 'a' * 31, 'a' * 33, 'A' * 32, None):
            with self.subTest(value=value), self.assertRaises(GeoError):
                self.service.get_route(value)

    def test_preview_persists_server_owned_source(self):
        with patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_osm', return_value=('1' * 64, osm())):
            route = self.service.preview(position(20), position(380))
        self.assertEqual(len(route['id']), 32)
        self.assertNotIn('ways', route)
        saved = self.service.get_route(route['id'])
        self.assertEqual(saved['ways'][0]['id'], 10)
        self.assertAlmostEqual(saved['length_m'], 360)
        with patch.object(self.service, '_request_bytes', side_effect=AssertionError('no repeat network query')):
            self.assertEqual(route, self.service.preview(position(20), position(380)))

    def test_provider_change_invalidates_preview_cache_but_preserves_saved_route(self):
        with patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_osm', return_value=('1' * 64, osm())):
            original = self.service.preview(position(20), position(380))
        original_record = self.service.get_route(original['id'])
        changes = {'ROADTOASSETTO_ROUTER_URL': 'https://routing.example.test',
                   'ROADTOASSETTO_ROUTER_PROFILE': 'alternate_profile',
                   'ROADTOASSETTO_ROUTER_EXCLUDE': 'motorway'}
        for name, value in changes.items():
            with self.subTest(setting=name), patch.dict(os.environ, {name: value}):
                changed = GeoService(self.root)
                with patch.object(changed, '_confirm_hessen'), patch.object(changed, '_osm', return_value=('1' * 64, osm())) as fetch:
                    fresh = changed.preview(position(20), position(380))
                self.assertNotEqual(fresh['id'], original['id'])
                fetch.assert_called_once()
                self.assertEqual(changed.get_route(original['id']), original_record)
                with patch.object(changed, '_request_bytes', side_effect=AssertionError('unchanged provider must reuse cache')):
                    self.assertEqual(changed.preview(position(20), position(380)), fresh)

    def test_long_preview_stores_provider_fingerprint_for_reuse(self):
        from copy import deepcopy
        points = [position(number * 100) for number in range(51)]
        planned = {'name': 'Synthetic long route', 'length_m': 5000., 'coordinates': points,
                   'bounds_utm': [BASE[0], BASE[1], BASE[0] + 5000, BASE[1]],
                   'routing': {'provider': 'OSRM'}, 'warnings': []}
        with patch.object(self.service, '_confirm_hessen'), patch('webapp.routing.RoutingService.route', side_effect=lambda *args: deepcopy(planned)) as route:
            original = self.service.preview(points[0], points[-1])
            self.assertEqual(self.service.preview(points[0], points[-1]), original)
            route.assert_called_once()
        record = self.service.get_route(original['id'])
        self.assertEqual(record['schema_version'], 2)
        self.assertEqual(record['provider_fingerprint'], self.service.provider_fingerprint)
        with patch.dict(os.environ, {'ROADTOASSETTO_ROUTER_EXCLUDE': 'motorway'}):
            changed = GeoService(self.root)
            with patch.object(changed, '_confirm_hessen'), patch('webapp.routing.RoutingService.route', side_effect=lambda *args: deepcopy(planned)) as route:
                fresh = changed.preview(points[0], points[-1])
                route.assert_called_once()
            self.assertNotEqual(fresh['id'], original['id'])
            self.assertEqual(changed.get_route(original['id']), record)

    def test_nearby_endpoints_fall_back_when_local_detour_exceeds_area(self):
        points = {1: (0, 0), 2: (0, 2000), 3: (1500, 2000), 4: (1500, 0), 5: (400, 0)}
        content = osm([(10, list(points), {'highway': 'residential'})], points)
        fallback = {'id': 'f' * 32, 'length_m': 6600., 'sections_count': 7}
        bounds = [BASE[0] - 250, BASE[1] - 250, BASE[0] + 1750, BASE[1] + 2250]
        cached = ('1' * 64, content, _wgs_bounds(bounds), bounds)
        with patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_cached_osm', return_value=cached):
            with patch.object(self.service, '_preview_routed', return_value=fallback) as routed:
                result = self.service.preview(position(0), position(400))
        self.assertEqual(result, fallback)
        routed.assert_called_once_with(tuple(position(0)), tuple(position(400)))

    def test_nearby_endpoints_fall_back_when_local_extract_is_disconnected(self):
        content = osm([(10, [1, 2], {'highway': 'residential'}), (11, [4, 5], {'highway': 'residential'})])
        fallback = {'id': 'f' * 32, 'length_m': 1800., 'sections_count': 2}
        with patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_osm', return_value=('1' * 64, content)):
            with patch.object(self.service, '_preview_routed', return_value=fallback) as routed:
                self.assertEqual(self.service.preview(position(0), position(400)), fallback)
                routed.assert_called_once()

    def test_no_eligible_private_roads_do_not_trigger_routing_fallback(self):
        for tags in ({'access': 'private'}, {'highway': 'footway'}):
            content = osm([(10, [1, 2, 3, 4, 5], {'highway': 'residential', **tags})])
            with self.subTest(tags=tags), patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_osm', return_value=('1' * 64, content)):
                with patch.object(self.service, '_preview_routed') as routed:
                    with self.assertRaises(GeoError) as caught:
                        self.service.preview(position(0), position(400))
                    self.assertEqual(caught.exception.code, 'no_roads')
                    routed.assert_not_called()

    def test_local_route_longer_than_one_km_exposes_section_plan(self):
        points = {1: (0, 0), 2: (0, 200), 3: (200, 200), 4: (200, -200), 5: (400, -200), 6: (400, 0)}
        content = osm([(10, list(points), {'highway': 'residential'})], points)
        with patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_osm', return_value=('1' * 64, content)):
            with patch.object(self.service, '_preview_routed') as routed:
                result = self.service.preview(position(0), position(400))
                routed.assert_not_called()
        self.assertGreater(result['length_m'], 1100)
        self.assertEqual(result['sections_count'], 2)
        self.assertGreater(result['estimates']['source_bytes'], 0)
        self.assertGreater(result['estimates']['workspace_bytes'], result['estimates']['source_bytes'])
        self.assertEqual(self.service.get_route(result['id'])['schema_version'], 1)

    def test_contained_osm_extent_reuses_verified_larger_cache(self):
        bounds = [8.4, 50.1, 8.5, 50.2]
        with patch.object(self.service, '_request_bytes', return_value=osm()) as request:
            original_key, original = self.service._osm(bounds)
            reused_key, reused = self.service._osm([8.42, 50.12, 8.48, 50.18])
        self.assertEqual(request.call_count, 1)
        self.assertEqual((original_key, original), (reused_key, reused))

    def test_nearby_endpoint_uses_actual_160m_corridor_from_cached_250m_area(self):
        points = {number + 1: (number * 100, 0) for number in range(8)}
        content = osm([(10, list(points), {'highway': 'residential'})], points)
        with patch.object(self.service, '_confirm_hessen'), patch.object(self.service, '_request_bytes', return_value=content) as request:
            first = self.service.preview(position(20), position(380))
            nearby = self.service.preview(position(20), position(440))
        # New 250 m search padding would extend 60 m beyond the old cache;
        # its connected route and the complete 160 m scenery extent still fit.
        self.assertEqual(request.call_count, 1)
        self.assertNotEqual(first['id'], nearby['id'])
        self.assertAlmostEqual(nearby['length_m'], 420, places=3)
        self.assertEqual(self.service.get_route(first['id'])['context_key'], self.service.get_route(nearby['id'])['context_key'])

    def raster(self, *, crs='EPSG:25832', nodata=None, value=230):
        with MemoryFile() as memory:
            with memory.open(driver='GTiff', width=20, height=20, count=1, dtype='float32', crs=crs,
                             transform=from_origin(BASE[0], BASE[1] + 20, 1, 1), nodata=nodata) as dataset:
                dataset.write(np.full((1, 20, 20), value, dtype='float32'))
            return memory.read()

    def test_raster_crs_nodata_and_native_resolution(self):
        bounds = [BASE[0], BASE[1], BASE[0] + 20, BASE[1] + 20]
        with patch.object(self.service, '_request_bytes', return_value=self.raster()):
            record = self.service._raster(self.root / 'valid.tif', bounds, 'dgm')
        self.assertEqual(record['resolution_m'], [1, 1])
        for name, content in [('wrong_crs', self.raster(crs='EPSG:3857')), ('nodata', self.raster(nodata=-9999, value=-9999)), ('missing', b'<exception/>')]:
            with self.subTest(name=name), patch.object(self.service, '_request_bytes', return_value=content), self.assertRaises(GeoError):
                self.service._raster(self.root / (name + '.tif'), bounds, 'dgm')
            self.assertFalse((self.root / (name + '.download.tif')).exists())

    def test_contained_raster_reused_at_native_resolution_without_network(self):
        source=self.service.cache/'inputs'/'existing'/'dgm1.tif'
        source.parent.mkdir(parents=True);source.write_bytes(self.raster())
        bounds=[BASE[0]+2,BASE[1]+3,BASE[0]+17,BASE[1]+18]
        with patch.object(self.service,'_request_bytes',side_effect=AssertionError('No download')):
            record=self.service._raster(self.root/'cropped.tif',bounds,'dgm')
        self.assertEqual(record['bounds'],bounds)
        self.assertEqual(record['width'],15)
        self.assertEqual(record['height'],15)
        self.assertIn('cropped_from_cached_source_sha256',record)

    @staticmethod
    def buildings(start, count, reported='unknown'):
        root = ET.Element(WFS + 'FeatureCollection', numberMatched=reported, numberReturned='0')
        for number in range(start, start + count):
            member = ET.SubElement(root, WFS + 'member')
            ET.SubElement(member, BU + 'Building', {GML + 'id': 'building_' + str(number)})
        return ET.tostring(root)

    def test_lod2_pagination_and_lying_returned_count(self):
        responses = [self.buildings(0, 250), self.buildings(250, 2)]
        with patch.object(self.service, '_request_bytes', side_effect=responses) as request:
            result = self.service._lod2(self.root / 'buildings.gml', RouteTests.bounds)
        self.assertEqual(result['building_count'], 252)
        self.assertEqual(request.call_args_list[1].kwargs['params']['STARTINDEX'], 250)

    def test_lod2_repeated_pages_truncation_and_limit_fail(self):
        cases = [[self.buildings(0, 250), self.buildings(0, 250)],
                 [self.buildings(0, 2, reported='100')], [self.buildings(0, 2, reported='5001')]]
        for responses in cases:
            with self.subTest(pages=len(responses)), patch.object(self.service, '_request_bytes', side_effect=responses), self.assertRaises(GeoError):
                self.service._lod2(self.root / 'rejected.gml', RouteTests.bounds)
            self.assertFalse((self.root / 'rejected.gml').exists())

    def test_dense_town_window_preserves_more_than_one_thousand_buildings(self):
        responses=[self.buildings(i,250) for i in range(0,1000,250)]+[self.buildings(1000,3)]
        with patch.object(self.service,'_request_bytes',side_effect=responses):
            result=self.service._lod2(self.root/'dense.gml',RouteTests.bounds)
        self.assertEqual(result['building_count'],1003)
        self.assertEqual(len(ET.parse(self.root/'dense.gml').findall(WFS+'member')),1003)

    def test_empty_lod2_extract_remains_empty_in_building_engine(self):
        from PIL import Image
        from shapely.geometry import LineString
        from roadtoassetto.buildings import build_lod2
        with patch.object(self.service, '_request_bytes', return_value=self.buildings(0, 0)):
            record = self.service._lod2(self.root / 'buildings_lod2.gml', RouteTests.bounds)
        self.assertEqual(record['building_count'], 0)
        textures = self.root / 'textures'
        textures.mkdir()
        material_dir = self.root / 'materials/PaintedPlaster006'
        material_dir.mkdir(parents=True)
        Image.new('RGB', (16, 16), (210, 202, 190)).save(material_dir / 'PaintedPlaster006_2K-JPG_Color.jpg')
        Image.new('RGB', (16, 16), (128, 128, 255)).save(material_dir / 'PaintedPlaster006_2K-JPG_NormalDX.jpg')
        scene = {'materials': []}
        def material(name, texture, **kwargs):
            scene['materials'].append(dict(name=name, texture=texture, **kwargs))
            return len(scene['materials']) - 1
        line = LineString([BASE, (BASE[0] + 200, BASE[1])])
        result = build_lod2(self.root, textures, scene, material, lambda *args: None,
                            lambda xy, z: np.c_[xy, z], line, line.buffer(3), None, RouteTests.bounds)
        self.assertEqual(result['buildings'], 0)
        self.assertEqual(result['source_buildings_in_extract'], 0)


if __name__ == '__main__':
    unittest.main()
