"""Bounded routing, provider validation and cache regression tests (offline)."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

import numpy as np
from rasterio.io import MemoryFile
from rasterio.transform import from_origin

from webapp.geodata import (BU, GML, WFS, GeoError, GeoService, TO_WGS,
                           _bounds, _point, plan_route)


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

    def test_bridges_tunnels_private_and_unpaved_paths_rejected(self):
        for extra in ({'bridge': 'viaduct'}, {'tunnel': 'building_passage'}, {'access': 'private'}, {'highway': 'footway'}, {'layer': '1'}):
            with self.subTest(extra=extra):
                data = osm([(10, [1, 2, 3, 4, 5], {'highway': 'residential', **extra})])
                with self.assertRaises(GeoError):
                    plan_route(data, position(0), position(400), self.bounds)

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
                 [self.buildings(0, 2, reported='100')], [self.buildings(0, 2, reported='1001')]]
        for responses in cases:
            with self.subTest(pages=len(responses)), patch.object(self.service, '_request_bytes', side_effect=responses), self.assertRaises(GeoError):
                self.service._lod2(self.root / 'rejected.gml', RouteTests.bounds)
            self.assertFalse((self.root / 'rejected.gml').exists())

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
