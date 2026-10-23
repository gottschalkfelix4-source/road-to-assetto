"""Behavioural checks for interpreted road references and cropped exports."""
import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from pyproj import Transformer
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.prepare import ordered_road
from roadtoassetto.road_alignment import load_alignment, widths_at
from roadtoassetto.scenery import select_route


class AlignmentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = ROOT / 'data/altenhain'
        ll = ordered_road(json.loads((cls.data / 'road_ways.json').read_text(encoding='utf-8')))
        transform = Transformer.from_crs(4326, 25832, always_xy=True)
        cls.osm = np.array(transform.transform(ll[:, 0], ll[:, 1])).T
        cls.xy, cls.meta = load_alignment(cls.data, cls.osm)

    def test_missing_reference_preserves_osm_and_constant_width(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'test-results') as temp:
            xy, meta = load_alignment(Path(temp), self.osm)
        np.testing.assert_array_equal(xy, self.osm)
        np.testing.assert_array_equal(widths_at(xy, meta, 5.5), np.full(len(xy), 5.5))
        self.assertFalse(meta['applied'])

    def test_reference_preserves_endpoints_and_has_honest_provenance(self):
        np.testing.assert_array_equal(self.xy[[0, -1]], self.osm[[0, -1]])
        self.assertTrue(self.meta['applied'])
        self.assertIn('not positional accuracy', self.meta['accuracy'])
        self.assertGreater(self.meta['observation_counts']['fallback'], 0)
        self.assertLess(self.meta['dop_interpreted_source_length_fraction'], 1)

    def test_wrong_route_does_not_silently_apply_local_correction(self):
        with self.assertRaisesRegex(ValueError, 'endpoints'):
            load_alignment(self.data, self.osm + [1, 0])

    def test_corrected_junction_follows_reviewed_path(self):
        line = LineString(self.xy)
        doc = json.loads((self.data / 'road_alignment.json').read_text(encoding='utf-8'))
        tr = doc['pixel_to_utm']
        for x, y in doc['junction']['reviewed_pixel_points']:
            self.assertLess(line.distance(Point(tr[0] * x + tr[2], tr[4] * y + tr[5])), .15)
        self.assertLess(line.length, LineString(self.osm).length)

    def test_obscured_sections_retain_osm(self):
        original = LineString(self.osm)
        source_s = np.linspace(0, original.length, len(self.xy))
        mask = (source_s >= 645) & (source_s <= original.length)
        self.assertLess(max(original.distance(Point(p)) for p in self.xy[mask]), 1e-6)

    def test_widths_retain_world_location_when_cropped(self):
        cropped, _ = select_route(self.xy, 180, 330)
        expected = widths_at(self.xy, self.meta)
        actual = widths_at(cropped, self.meta)
        tree = __import__('scipy').spatial.cKDTree(self.xy)
        distance, indices = tree.query(cropped[1:-1])
        self.assertLess(float(distance.max()), 1e-8)
        np.testing.assert_allclose(actual[1:-1], expected[indices], atol=1e-8)
        np.testing.assert_allclose(widths_at(cropped, self.meta, 5.5), actual * 5.5 / 6)

    def test_full_and_short_corridors_have_no_folded_road_or_shoulders(self):
        for start, end in [(None, None), (180, 330), (290, 450), (500, 650)]:
            selected, _ = select_route(self.xy, start, end)
            station = np.r_[0, np.cumsum(np.linalg.norm(np.diff(selected, axis=0), axis=1))]
            samples = np.linspace(0, station[-1], int(np.ceil(station[-1])) + 1)
            xy = np.c_[np.interp(samples, station, selected[:, 0]), np.interp(samples, station, selected[:, 1])]
            xy = gaussian_filter1d(xy, .8, axis=0, mode='nearest')
            xy[[0, -1]] = selected[[0, -1]]
            direction = np.gradient(xy, axis=0)
            direction /= np.linalg.norm(direction, axis=1)[:, None]
            normal = np.c_[-direction[:, 1], direction[:, 0]]
            for nominal in (4., 5.5, 6., 7.):
                half = widths_at(xy, self.meta, nominal) / 2 + .4
                a, b = xy + normal * half[:, None], xy - normal * half[:, None]
                self.assertTrue(Polygon(np.vstack([a, b[::-1]])).is_valid, (start, nominal))
                # Longitudinal mesh rows must always advance along the local
                # road direction; a valid perimeter alone could hide folds.
                tangent = xy[1:] - xy[:-1]
                self.assertTrue(np.all(np.sum(np.diff(a, axis=0) * tangent, axis=1) > 0))
                self.assertTrue(np.all(np.sum(np.diff(b, axis=0) * tangent, axis=1) > 0))

    def test_nonfinite_or_invalid_width_rejected(self):
        for value in (float('nan'), 0, -6, 100):
            with self.assertRaises(ValueError):
                widths_at(self.xy, self.meta, value)


if __name__ == '__main__':
    unittest.main()
