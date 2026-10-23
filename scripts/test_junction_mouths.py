"""Source topology, clipping and exact terrain tests for visual side roads."""
from pathlib import Path
import json
import hashlib
import tempfile
import sys
import unittest
from xml.etree import ElementTree as ET

import numpy as np
from pyproj import Transformer
from shapely.geometry import LineString, Polygon, box
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.junction_mouths import connected_mouths, build_junction_mouths, reviewed_junction_reservation
from roadtoassetto.terrain_sampling import ExportedTerrainSampler
from roadtoassetto.ground_details import RoadGroundProfile


class JunctionTests(unittest.TestCase):
    def setUp(self):
        temporary_root = ROOT / 'test-results'
        temporary_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='junction-fixture-', dir=temporary_root)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.origin = np.array([462000., 5556300.])
        self.xy = self.origin + [[-20., 0.], [0., 0.], [20., 0.]]
        self.road = LineString(self.xy).buffer(3., cap_style=2, join_style=2)
        self.profile = RoadGroundProfile(self.xy, [100., 100., 100.], 3., crossfall=.025)
        self.doc = ET.Element('osm')
        self.reverse = Transformer.from_crs(25832, 4326, always_xy=True)
        for identity, xy in {1: (-20, 0), 2: (0, 0), 3: (20, 0),
                             4: (0, 20), 5: (8, 4), 6: (8, 20),
                             7: (0, -20), 8: (-6, 14), 9: (6, 14)}.items():
            self.node(identity, xy)
        self.way(10, [1, 2, 3], highway='residential', surface='asphalt')
        (self.path / 'road_ways.json').write_text(json.dumps([{'id': 10, 'nodes': [1, 2, 3]}]))

    def node(self, identity, point):
        lon, lat = self.reverse.transform(*(self.origin + point))
        ET.SubElement(self.doc, 'node', id=str(identity), lon=str(lon), lat=str(lat))

    def way(self, identity, nodes, **tags):
        way = ET.SubElement(self.doc, 'way', id=str(identity))
        for node in nodes:
            ET.SubElement(way, 'nd', ref=str(node))
        for key, value in tags.items():
            ET.SubElement(way, 'tag', k=key, v=value)

    def write(self):
        ET.ElementTree(self.doc).write(self.path / 'area.osm', encoding='utf-8')

    def terrain(self):
        # Bounded wavy terrain under the side road; exact original diagonals.
        points = np.array([[x, y] for y in range(-15, 16) for x in range(-26, 27)], float)
        world = points + self.origin
        raw = 100. + np.sin(points[:, 0] * .65) * .07 + np.cos(points[:, 1] * .8) * .09
        height = self.profile(world, raw) - .08
        ac = lambda p, z: np.c_[p[:, 0] - self.origin[0], z - 100., self.origin[1] - p[:, 1]]
        vertices = ac(world, height)
        faces = []
        for y in range(30):
            for x in range(52):
                a = y * 53 + x
                faces.extend([(a, a + 1, a + 54), (a, a + 54, a + 53)])
        sampler = ExportedTerrainSampler(vertices[np.array(faces)], self.origin, 100.)
        return sampler, ac

    def test_only_explicitly_connected_sources_are_used(self):
        self.way(20, [2, 4], highway='residential', surface='asphalt')
        self.way(21, [5, 6], highway='residential', surface='asphalt')
        self.way(22, [8, 9], highway='residential', surface='asphalt')
        self.write()
        records, _ = connected_mouths(self.path, self.xy, 3., self.road)
        self.assertEqual({r['way_id'] for r in records}, {'20'})
        self.assertTrue(all(r['width_estimated'] for r in records))
        self.assertEqual(records[0]['width_m'], 5.)

    def test_restricted_nonasphalt_and_footways_are_excluded(self):
        self.way(20, [2, 4], highway='steps')
        self.way(21, [2, 7], highway='service', access='private', surface='asphalt')
        self.way(22, [2, 4], highway='residential', surface='paving_stones')
        self.way(23, [2, 7], highway='service', motor_vehicle='no', surface='asphalt')
        self.way(24, [2, 7], highway='service', service='driveway')
        self.write()
        records, skipped = connected_mouths(self.path, self.xy, 3., self.road)
        self.assertEqual(records, [])
        self.assertEqual(len(skipped), 5)

    def test_main_shoulders_building_and_maximum_extent_are_reserved(self):
        self.way(20, [2, 4], highway='residential', surface='asphalt', width='6 m')
        for identity, point in [(30, (-1, 7)), (31, (1, 7)), (32, (1, 9)), (33, (-1, 9))]:
            self.node(identity, point)
        self.way(30, [30, 31, 32, 33, 30], building='yes')
        self.write()
        records, _ = connected_mouths(self.path, self.xy, 3., self.road)
        whole = unary_union([r['polygon'] for r in records])
        self.assertGreater(whole.area, 25.)
        self.assertLess(whole.intersection(self.road.buffer(.4)).area, 1e-8)
        house = box(*(self.origin + [-1, 7]), *(self.origin + [1, 9]))
        self.assertLess(whole.intersection(house.buffer(.099)).area, 1e-8)
        self.assertLess(whole.difference(self.road.buffer(8.0001)).area, 1e-8)
        self.assertFalse(records[0]['width_estimated'])

    def test_crop_does_not_project_distant_connections_onto_route_end(self):
        self.way(20, [2, 4], highway='residential', surface='asphalt')
        self.write()
        xy = self.origin + [[10., 0.], [20., 0.]]
        road = LineString(xy).buffer(3., cap_style=2)
        records, _ = connected_mouths(self.path, xy, 3., road)
        self.assertEqual(records, [])

    def test_reverse_route_preserves_same_source_footprints(self):
        self.way(20, [2, 4], highway='service', surface='asphalt')
        self.write()
        first, _ = connected_mouths(self.path, self.xy, 3., self.road)
        second, _ = connected_mouths(self.path, self.xy[::-1], 3., self.road)
        self.assertAlmostEqual(first[0]['polygon'].symmetric_difference(second[0]['polygon']).area, 0.)
        self.assertEqual(first[0]['width_m'], 3.5)

    def test_dop_shift_reconnects_only_the_shared_source_node(self):
        shared = next(n for n in self.doc.findall('node') if n.get('id') == '2')
        lon, lat = self.reverse.transform(*(self.origin + [0., 6.5]))
        shared.set('lon', str(lon)); shared.set('lat', str(lat))
        self.way(20, [2, 4], highway='residential', surface='asphalt')
        self.write()
        records, _ = connected_mouths(self.path, self.xy, 3., self.road)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]['alignment_connector_m'], 6.5)
        self.assertAlmostEqual(records[0]['polygon'].distance(self.road.buffer(.4005)), 0., places=5)

    def test_actual_source_node_at_route_end_is_retained(self):
        self.node(40, (-20, -20))
        self.way(20, [1, 40], highway='residential', surface='asphalt')
        self.write()
        records, _ = connected_mouths(self.path, self.xy, 3., self.road)
        self.assertEqual(len(records), 1)
        self.assertAlmostEqual(records[0]['source_station_m'], 0.)

    def reviewed_outline(self, way_id='20', connection_node='2'):
        source = b'reviewed aerial test fixture'
        (self.path / 'ortho.fixture').write_bytes(source)
        outline = self.origin + [[-6, 2], [5, 2], [4, 7], [1, 12], [-2, 12], [-5, 6], [-6, 2]]
        document = {'schema_version': 1, 'crs': 'EPSG:25832',
                    'source': {'file': 'ortho.fixture', 'sha256': hashlib.sha256(source).hexdigest()},
                    'observations': [{'id': 'reviewed_test', 'enabled': True, 'status': 'observed',
                                      'confidence': 'medium', 'position_uncertainty_m': 1.,
                                      'way_id': way_id, 'connection_node': connection_node,
                                      'geometry': {'type': 'Polygon', 'coordinates': outline.tolist()}}]}
        (self.path / 'junction_observations.json').write_text(json.dumps(document), encoding='utf-8')
        return Polygon(outline)

    def test_reviewed_outline_and_preterrain_reservation_share_verified_topology(self):
        self.way(20, [2, 4], highway='residential', surface='asphalt')
        self.write(); outline = self.reviewed_outline()
        records, _ = connected_mouths(self.path, self.xy, 3., self.road)
        self.assertEqual(len(records), 1)
        self.assertTrue(records[0]['outline_reviewed'])
        self.assertIsNone(records[0]['width_m'])
        self.assertLess(records[0]['polygon'].difference(outline).area, 1e-8)
        # Expanded reviewed portion is not constrained by the old 5 m width.
        self.assertLess(records[0]['polygon'].bounds[0], self.origin[0]-4.)
        reservation = reviewed_junction_reservation(self.path, self.xy, self.road)
        self.assertLess(records[0]['polygon'].difference(reservation).area, 1e-8)
        # Reserving the shoulder too prevents a new gutter/kerb across access.
        self.assertGreater(reservation.intersection(self.road.buffer(.35)).area, .1)
        self.assertLess(reservation.intersection(self.road).area, 1e-8)
        (self.path / 'ortho.fixture').write_bytes(b'changed source')
        self.assertTrue(reviewed_junction_reservation(self.path, self.xy, self.road).is_empty)
        fallback, _ = connected_mouths(self.path, self.xy, 3., self.road)
        self.assertFalse(fallback[0].get('outline_reviewed', False))
        self.assertEqual(fallback[0]['width_m'], 5.)

    def test_reviewed_polygon_does_not_bypass_connectivity_restrictions_or_crop(self):
        self.way(20, [5, 6], highway='residential', surface='asphalt')
        self.write(); self.reviewed_outline()
        self.assertTrue(reviewed_junction_reservation(self.path, self.xy, self.road).is_empty)
        self.way(21, [2, 4], highway='service', access='private', surface='asphalt')
        self.write(); self.reviewed_outline(way_id='21')
        self.assertTrue(reviewed_junction_reservation(self.path, self.xy, self.road).is_empty)
        self.way(22, [2, 4], highway='residential', surface='asphalt')
        self.write(); self.reviewed_outline(way_id='22')
        xy = self.origin + [[10., 0.], [20., 0.]]
        road = LineString(xy).buffer(3., cap_style=2)
        self.assertTrue(reviewed_junction_reservation(self.path, xy, road).is_empty)

    def test_export_tracks_exact_terrain_with_short_smooth_edge_join(self):
        self.way(20, [2, 4], highway='residential', surface='asphalt')
        self.write()
        sampler, ac = self.terrain()
        meshes = []
        def add(name, vertices, faces, material, uv, collision):
            meshes.append((name, vertices, faces, material, uv, collision))
        scene = {'meshes': [{'name': 'existing_road', 'vertices': [[1, 2, 3]]}], 'materials': []}
        before = json.dumps(scene)
        result = build_junction_mouths(self.path, scene, None, add, ac, self.xy,
                                       np.full(3, 100.), 3., self.road, sampler, self.profile,
                                       asphalt_material=4)
        self.assertEqual(json.dumps(scene), before)
        self.assertEqual(result['junction_mouths'], 1)
        self.assertFalse(result['junction_mouth_collision'])
        self.assertTrue(meshes)
        for name, vertices, faces, material, uv, collision in meshes:
            self.assertFalse(collision)
            self.assertEqual(material, 4)
            self.assertTrue(np.isfinite(uv).all())
            triangles = vertices[faces]
            self.assertTrue(np.all(np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])[:, 1] > 0))
            p = np.c_[vertices[:, 0] + self.origin[0], self.origin[1] - vertices[:, 2]]
            height = vertices[:, 1] + 100.
            ground = sampler(p)
            self.assertTrue(np.all(height >= ground + .008 - 1e-8))
            away = np.abs(p[:, 1] - self.origin[1]) >= 4.40001
            np.testing.assert_allclose(height[away], ground[away] + .008, atol=1e-8)
            near = np.abs(p[:, 1] - self.origin[1]) < 3.401
            self.assertTrue(near.any())
            np.testing.assert_allclose(height[near], 100. - .025 * 3. - .015, atol=1e-5)
            # A face farther out must also agree at its centroid, proving
            # source terrain diagonals were clipped instead of bridged.
            centers = triangles.mean(axis=1)
            q = np.c_[centers[:, 0] + self.origin[0], self.origin[1] - centers[:, 2]]
            far = np.min(np.abs(p[faces, 1] - self.origin[1]), axis=1) >= 4.40001
            np.testing.assert_allclose(centers[far, 1] + 100., sampler(q[far]) + .008, atol=1e-8)


if __name__ == '__main__':
    unittest.main()
