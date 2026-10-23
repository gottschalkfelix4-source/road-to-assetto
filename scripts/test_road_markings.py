"""Evidence gating and exported-surface checks for observed road markings."""
from copy import deepcopy
from pathlib import Path
import hashlib
import json
import sys
import tempfile
import unittest

import numpy as np
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.road_markings import build_road_markings, observation_polygon
from roadtoassetto.terrain_sampling import ExportedTerrainSampler
from roadtoassetto.kn5 import normals
from scripts.test_road_wear import fixture


class RoadMarkingTests(unittest.TestCase):
    def setUp(self):
        (ROOT / 'test-results').mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='road-markings-', dir=ROOT / 'test-results')
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name)
        self.textures = self.path / 'textures'
        self.data = fixture(24.)
        self.scene, self.material, self.add, self.ac, self.xy, _, _, _, _, self.origin = self.data
        source = b'controlled source fixture; not real aerial pixels'
        (self.path / 'ortho.fixture').write_bytes(source)
        self.digest = hashlib.sha256(source).hexdigest()
        self.document = {'schema_version': 1, 'crs': 'EPSG:25832',
                         'source': {'file': 'ortho.fixture', 'sha256': self.digest,
                                    'provider': 'test source', 'pixel_size_m': .2},
                         'reviewed_on': '2026-10-22', 'observations': []}

    def line(self, identity='observed_bar', coordinates=None, **changes):
        points = self.origin + np.array([[10., -8.], [10., 8.]]) if coordinates is None else coordinates
        result = {'id': identity, 'enabled': True, 'status': 'observed', 'confidence': 'high',
                  'position_uncertainty_m': .35, 'paint_width_m': .25,
                  'surfaces': ['main'], 'geometry': {'type': 'LineString', 'coordinates': np.asarray(points).tolist()}}
        result.update(changes)
        return result

    def write(self):
        (self.path / 'marking_observations.json').write_text(json.dumps(self.document), encoding='utf-8')

    def build(self):
        return build_road_markings(self.path, self.textures, self.scene, self.material, self.add, self.ac)

    def test_missing_evidence_never_creates_a_default_centerline(self):
        before = deepcopy(self.scene)
        result = self.build()
        self.assertEqual(result['road_markings'], 0)
        self.assertEqual(result['road_marking_status'], 'no_observations')
        self.assertEqual(self.scene, before)
        self.assertFalse(self.textures.exists())

    def test_observed_paint_follows_exact_exported_crown_and_clips(self):
        self.document['observations'] = [self.line()]
        self.write()
        before = deepcopy(self.scene['meshes'][0])
        result = self.build()
        self.assertEqual(result['road_markings'], 1)
        self.assertLess(result['road_marking_records'][0]['coverage_fraction'], .5)
        self.assertEqual(self.scene['meshes'][0], before)
        triangles = np.asarray(before['vertices'])[np.asarray(before['faces'])]
        sampler = ExportedTerrainSampler(triangles, self.origin, 200.)
        for mesh in self.scene['meshes'][1:]:
            self.assertFalse(mesh['collision'])
            v, f = np.asarray(mesh['vertices']), np.asarray(mesh['faces'])
            samples = np.vstack([v, v[f].mean(axis=1)])
            world = np.c_[samples[:, 0] + self.origin[0], self.origin[1] - samples[:, 2]]
            np.testing.assert_allclose(samples[:, 1] + 200. - sampler(world), .0018, atol=2e-9)
            self.assertLess(np.abs(mesh['uv']).max(), 10.)
            for positions in (v.round(5), v.round(5).astype('float32').astype(float)):
                self.assertTrue(np.isfinite(normals(positions, f)).all())

    def test_stale_source_is_disabled_until_review(self):
        self.document['observations'] = [self.line()]
        self.write()
        (self.path / 'ortho.fixture').write_bytes(b'different imagery')
        result = self.build()
        self.assertEqual(result['road_markings'], 0)
        self.assertEqual(result['road_marking_status'], 'source_changed_review_required')
        self.assertEqual(len(self.scene['meshes']), 1)

    def test_historical_disabled_and_ambiguous_observations_are_not_invented(self):
        self.document['observations'] = [self.line('old', status='historical_only'),
                                         self.line('off', enabled=False),
                                         self.line('unclear', confidence='low')]
        self.write()
        result = self.build()
        self.assertEqual(result['road_markings'], 0)
        self.assertEqual(len(result['road_marking_skipped']), 3)
        self.assertFalse(self.textures.exists())

    def test_crop_keeps_world_coordinates_and_omits_outside_feature(self):
        self.document['observations'] = [self.line('outside', self.origin + [[60, -2], [60, 2]])]
        self.write()
        result = self.build()
        self.assertEqual(result['road_markings'], 0)
        self.assertIn('outside', result['road_marking_skipped'][0]['reason'])
        self.assertEqual(len(self.scene['meshes']), 1)

    def test_explicit_junction_scope_uses_only_existing_junction_triangles(self):
        mouth = deepcopy(self.scene['meshes'][0])
        mouth['name'] = 'JUNCTION_ASPHALT_fixture'
        mouth['vertices'] = (np.asarray(mouth['vertices']) + [0., .5, -15.]).tolist()
        mouth['collision'] = False
        self.scene['meshes'].append(mouth)
        self.document['observations'] = [self.line('mouth', self.origin + [[10, 12], [10, 18]], surfaces=['junction'])]
        self.write()
        result = self.build()
        self.assertEqual(result['road_markings'], 1)
        triangles = np.asarray(mouth['vertices'])[np.asarray(mouth['faces'])]
        sampler = ExportedTerrainSampler(triangles, self.origin, 200.)
        for mesh in self.scene['meshes'][2:]:
            v = np.asarray(mesh['vertices'])
            world = np.c_[v[:, 0] + self.origin[0], self.origin[1] - v[:, 2]]
            np.testing.assert_allclose(v[:, 1] + 200. - sampler(world), .0018, atol=2e-9)
        self.assertEqual(self.scene['meshes'][1], mouth)

    def test_source_assets_unchanged_and_scene_survives_json_roundtrip(self):
        self.document['observations'] = [self.line()]
        self.write()
        before = (self.path / 'ortho.fixture').read_bytes()
        self.build()
        self.assertEqual((self.path / 'ortho.fixture').read_bytes(), before)
        self.assertEqual(list(self.textures.iterdir()), [self.textures / 'observed_road_paint.png'])
        copy = json.loads(json.dumps(self.scene))
        for mesh in copy['meshes']:
            v = np.asarray(mesh['vertices']).round(5)
            f = np.asarray(mesh['faces'])
            normals(v, f)
            normals(v.astype('float32').astype(float), f)

    def test_invalid_crs_and_unbounded_guesses_are_rejected(self):
        self.document['crs'] = 'EPSG:4326'
        self.write()
        with self.assertRaisesRegex(ValueError, 'EPSG:25832'):
            self.build()
        with self.assertRaisesRegex(ValueError, 'local'):
            observation_polygon(self.line(coordinates=self.origin + [[0, 0], [100, 0]]))
        with self.assertRaisesRegex(ValueError, 'width'):
            observation_polygon(self.line(paint_width_m=1.))
        with self.assertRaisesRegex(ValueError, 'surface scope'):
            observation_polygon(self.line(surfaces=['houses']))


if __name__ == '__main__':
    unittest.main()
