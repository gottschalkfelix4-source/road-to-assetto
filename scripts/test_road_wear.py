"""Decorative road details must respect exported physics, crown and source art."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image
from shapely.geometry import Polygon, LineString
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.road_wear import build_road_wear, ExportedRoad, RouteFrame
from roadtoassetto.scenery import Batches
from roadtoassetto.terrain_sampling import ExportedTerrainSampler
from roadtoassetto.kn5 import normals


def fixture(length=240., narrow=False):
    origin = np.array([475000., 5556000.])
    distance = np.linspace(0, length, int(length / 2) + 1)
    xy = origin + np.c_[distance, np.sin(distance / 37) * 2]
    tangent = np.gradient(xy, axis=0)
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    left = np.c_[-tangent[:, 1], tangent[:, 0]]
    half = (1.2 if narrow else 2.5) + .12 * np.sin(distance / 31)
    height = 200 + distance * .063 + np.sin(distance / 20) * .03
    offsets = half[:, None] * np.linspace(-1, 1, 7)[None, :]
    points = (xy[:, None, :] + left[:, None, :] * offsets[:, :, None]).reshape(-1, 2)
    h = (height[:, None] - abs(offsets) * .025).ravel()
    ac = lambda p, z: np.c_[p[:, 0] - origin[0], z - 200, origin[1] - p[:, 1]]
    v = ac(points, h)
    faces = []
    for row in range(len(xy) - 1):
        for col in range(6):
            a = row * 7 + col; b = a + 7
            faces.extend([(a, a+1, b), (a+1, b+1, b)])
    mesh = {'name': '1ROAD_MAIN', 'vertices': v.round(5).tolist(), 'faces': faces,
            'collision': True, 'material': 0, 'uv': (points / 1.25).tolist()}
    scene = {'meshes': [mesh], 'materials': [{'name': 'asphalt', 'texture': 'asphalt_photo.jpg',
             'ambient': .352, 'diffuse': .4224, 'albedo_multiplier': [.88, .88, .88],
             'normal': 'asphalt_photo_normal_dx.jpg', 'normal_strength': .48}]}
    def material(name, texture, ambient=.4, diffuse=.5, specular=.04):
        scene['materials'].append(dict(name=name, texture=texture, ambient=ambient, diffuse=diffuse, specular=specular))
        return len(scene['materials']) - 1
    def add(name, vertices, faces, mat, uv, collision=False):
        scene['meshes'].append(dict(name=name, vertices=np.asarray(vertices).tolist(), faces=np.asarray(faces).tolist(),
                                    material=mat, uv=np.asarray(uv).tolist(), collision=collision))
    return scene, material, add, ac, xy, height, left, distance, half, origin


class RoadWearTests(unittest.TestCase):
    def run_fixture(self, length=240., narrow=False):
        data = fixture(length, narrow)
        scene, material, add, ac, xy, h, left, distance, half, origin = data
        before = deepcopy(scene['meshes'][0])
        with tempfile.TemporaryDirectory(prefix='road-wear-', dir=ROOT / 'test-results') as folder:
            source = Path(folder) / 'asphalt_photo.jpg'
            Image.new('RGB', (8, 8), (81, 84, 85)).save(source)
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            stats = build_road_wear(folder, scene, material, add, ac, xy, h, left, distance, half)
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
            with Image.open(Path(folder) / 'road_cast_iron.png') as iron:
                self.assertEqual(iron.size, (512, 512))
        self.assertEqual(before, scene['meshes'][0])
        return data, stats

    def test_all_details_noncolliding_and_inside_exported_road(self):
        data, stats = self.run_fixture()
        scene, _, _, _, _, _, _, _, _, origin = data
        main = scene['meshes'][0]
        main_vertices = np.asarray(main['vertices'])
        triangles = main_vertices[np.asarray(main['faces'])]
        sampler = ExportedTerrainSampler(triangles, origin, 200)
        footprint = unary_union([Polygon(t[:, [0, 2]]) for t in triangles]).buffer(1e-7)
        for mesh in scene['meshes'][1:]:
            self.assertFalse(mesh['collision'])
            v = np.asarray(mesh['vertices']); f = np.asarray(mesh['faces'])
            self.assertLessEqual(len(v), 63000)
            self.assertTrue(np.isfinite(v).all())
            self.assertTrue(np.isfinite(mesh['uv']).all())
            # Check vertices and face centroids, so a detail cannot bridge the
            # road crown or a segment boundary despite correct vertex heights.
            points = np.vstack([v, v[f].mean(axis=1)])
            utm = np.c_[points[:, 0] + origin[0], origin[1] - points[:, 2]]
            gap = points[:, 1] + 200 - sampler(utm)
            self.assertTrue(np.all(gap > .00059), mesh['name'])
            self.assertTrue(np.all(gap < .00181), mesh['name'])
            self.assertTrue(all(footprint.covers(Polygon(t[:, [0, 2]])) for t in v[f]))
            normals = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
            self.assertTrue(np.all(normals[:, 1] > 1e-10))
        self.assertGreater(stats['road_manhole_covers'], 1)
        self.assertGreater(stats['asphalt_repair_sections'], 0)
        self.assertGreater(stats['road_resealed_cracks'], 0)

    def test_deterministic_nonperiodic_and_restrained_coverage(self):
        first, a = self.run_fixture(500)
        second, b = self.run_fixture(500)
        self.assertEqual(a, b)
        self.assertEqual(first[0], second[0])
        self.assertIn('not mapped', a['road_wear_provenance'])
        self.assertLess(a['road_wear_area_m2'], 500 * 5 * .035)
        positions = [p['station_m'] for p in a['road_wear_positions'] if p['kind'] == 'manhole']
        self.assertGreater(np.std(np.diff(positions)), 4)
        for p in a['road_wear_positions']:
            self.assertGreaterEqual(p['station_m'], 34)
            self.assertLessEqual(p['station_m'], 475)
            if p['kind'] == 'manhole':
                self.assertGreater(p['outer_frame_diameter_m'], p['nominal_clear_opening_m'])

    def test_variable_narrow_street_remains_within_main_mesh(self):
        data, stats = self.run_fixture(160, narrow=True)
        surface = ExportedRoad(data[0], data[3])
        footprint = unary_union(surface.polygons).buffer(1e-7)
        origin = data[-1]
        for mesh in data[0]['meshes'][1:]:
            v = np.asarray(mesh['vertices'])
            utm = np.c_[v[:, 0] + origin[0], origin[1] - v[:, 2]]
            self.assertTrue(all(footprint.covers(Polygon(t)) for t in utm[np.asarray(mesh['faces'])]))
        self.assertGreater(stats['road_manhole_covers'], 0)

    def test_patch_tint_is_relative_to_base_asphalt(self):
        data, _ = self.run_fixture()
        scene = data[0]
        patches = [m for m in scene['materials'] if m['name'].startswith('asphalt_repair_')]
        self.assertEqual(len(patches), 3)
        for mat, factor in zip(patches, (.86, .92, 1.035)):
            np.testing.assert_allclose(mat['albedo_multiplier'], np.array([.88]*3) * factor)
            self.assertEqual(mat['texture'], 'asphalt_photo.jpg')
            self.assertEqual(mat['normal'], 'asphalt_photo_normal_dx.jpg')

    def test_exact_clip_preserves_crown_under_a_large_polygon(self):
        scene, material, add, ac, xy, h, left, distance, half, origin = fixture(10)
        surface = ExportedRoad(scene, ac)
        batch = Batches(add)
        shape = Polygon([xy[1]+[-.7,-2],xy[1]+[4.7,-2],xy[1]+[4.7,2],xy[1]+[-.7,2]])
        area = surface.append(shape, 'TEST', 0, batch, lambda p: p, .0018)
        batch.finish()
        self.assertAlmostEqual(area, shape.area, places=7)
        sampler = ExportedTerrainSampler(np.asarray(scene['meshes'][0]['vertices'])[np.asarray(scene['meshes'][0]['faces'])], origin, 200)
        for mesh in scene['meshes'][1:]:
            v = np.asarray(mesh['vertices']); f = np.asarray(mesh['faces'])
            center = v[f].mean(axis=1)
            expected = sampler(np.c_[center[:,0]+origin[0],origin[1]-center[:,2]])
            np.testing.assert_allclose(center[:,1]+200-expected,.0018,atol=2e-9)

    def test_invalid_route_and_missing_export_fail_without_guessing(self):
        with self.assertRaises(ValueError):
            RouteFrame([[0,0],[1,0]], [[0,1],[0,1]], [0,0], 2)
        with self.assertRaisesRegex(ValueError, '1ROAD_MAIN'):
            ExportedRoad({'meshes':[]}, lambda p,z: np.c_[p[:,0],z,-p[:,1]])
        scene, _, add, ac, *_ = fixture(10)
        surface = ExportedRoad(scene, ac)
        with self.assertRaisesRegex(ValueError, '1.8 mm'):
            surface.append(Polygon([(0,0),(1,0),(0,1)]), 'BAD',0,Batches(add),lambda p:p,.01)

    def test_concave_zigzag_keeps_every_bend_on_original_road_triangles(self):
        scene, material, add, ac, xy, h, left, distance, half, origin = fixture(30)
        surface = ExportedRoad(scene, ac)
        points = origin + [[10., -1.], [11., 1.], [12., -1.], [13., 1.], [14., -1.]]
        shape = LineString(points).buffer(.09, cap_style=2, join_style=2)
        batches = Batches(add)
        area = surface.append(shape, 'ZIGZAG', 0, batches, lambda p: p-origin, .0018)
        batches.finish()
        self.assertAlmostEqual(area, shape.area, places=7)
        sampler = ExportedTerrainSampler(np.asarray(scene['meshes'][0]['vertices'])[np.asarray(scene['meshes'][0]['faces'])], origin, 200.)
        emitted = []
        for mesh in scene['meshes'][1:]:
            v = np.asarray(mesh['vertices']); f = np.asarray(mesh['faces'])
            world = np.c_[v[:, 0]+origin[0], origin[1]-v[:, 2]]
            emitted.extend(Polygon(t) for t in world[f])
            centers = v[f].mean(axis=1)
            q = np.c_[centers[:, 0]+origin[0], origin[1]-centers[:, 2]]
            np.testing.assert_allclose(centers[:, 1]+200.-sampler(q), .0018, atol=2e-9)
        self.assertLess(unary_union(emitted).symmetric_difference(shape).area, 1e-7)

    def test_millimetre_clip_sliver_is_rejected_before_json_and_float32(self):
        # Regression from ROAD_MANHOLE_JOINT_110_0: a real polygon survives
        # clipping, but 5-decimal scene rounding collapses its thin altitude.
        # Float32 happens to enlarge it again, still below the writer limit.
        vertices = [[-143.,25.,-53.],[-142.,25.,-53.],[-142.,25.,-54.],[-143.,25.,-54.]]
        scene = {'meshes':[{'name':'1ROAD_MAIN','vertices':vertices,'faces':[[0,2,1],[0,3,2]]}]}
        ac = lambda p,z: np.c_[p[:,0],z,-p[:,1]]
        surface = ExportedRoad(scene, ac)
        emitted = []
        batch = Batches(lambda *args: emitted.append(args))
        sliver = Polygon([[-142.76123,53.66362],[-142.76138,53.66509],[-142.761238,53.66372]])
        self.assertGreater(sliver.area, 1e-9)
        self.assertEqual(surface.append(sliver, 'THIN',0,batch,lambda p:p,.0006), 0.)
        batch.finish()
        self.assertEqual(emitted, [])
        regular = Polygon([[-142.9,53.1],[-142.5,53.1],[-142.5,53.4],[-142.9,53.4]])
        area = surface.append(regular, 'VALID',0,batch,lambda p:p,.0006)
        batch.finish()
        self.assertAlmostEqual(area, regular.area, places=8)
        self.assertTrue(emitted)

    def test_all_generated_faces_survive_scene_serialization_and_kn5_precision(self):
        data, _ = self.run_fixture(500)
        serialized = deepcopy(data[0])
        for mesh in serialized['meshes']:
            mesh['vertices'] = np.asarray(mesh['vertices']).round(5).tolist()
            mesh['uv'] = np.asarray(mesh['uv']).round(6).tolist()
        restored = json.loads(json.dumps(serialized))
        for mesh in restored['meshes']:
            positions = np.asarray(mesh['vertices'],dtype=float)
            faces = np.asarray(mesh['faces'],dtype=int)
            for representation in (positions, positions.astype(np.float32).astype(float)):
                result = normals(representation,faces)
                self.assertTrue(np.isfinite(result).all(),mesh['name'])
                np.testing.assert_allclose(np.linalg.norm(result,axis=1),1.,atol=1e-10)


if __name__ == '__main__':
    unittest.main()
