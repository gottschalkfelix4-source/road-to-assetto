"""Long-build orchestration with synthetic geography and real KN5/ZIP export.

No remote service, Blender, user map or game is used. Only expensive acquisition
and scene preparation are replaced; section ownership, shared road profiles,
seam validation, binary serialization and packaging run their production paths.
"""
from contextlib import ExitStack
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import ProxyHandler, build_opener
import zipfile

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp import longbuild
from webapp.geodata import GeoError, TO_UTM, TO_WGS
from webapp.jobs import JobManager
from webapp.server import create_server


def synthetic_route(length=54_500.):
    x = np.linspace(450_000., 450_000. + length, int(length / 100) + 1)
    y = np.full_like(x, 5_556_000.)
    lon, lat = TO_WGS.transform(x, y)
    px, py = TO_UTM.transform(lon, lat)
    actual = float(np.linalg.norm(np.diff(np.c_[px, py], axis=0), axis=1).sum())
    return {'id': '1' * 32, 'name': 'Synthetic continuous road', 'length_m': actual,
            'coordinates': np.c_[lat, lon].tolist(), 'source': 'Offline synthetic test',
            'bounds': [float(min(lon)), float(min(lat)), float(max(lon)), float(max(lat))],
            'warnings': [], 'routing': {'provider': 'Offline test', 'node_ids': list(range(len(x)))}}


def selection(route, start=0., end=None):
    return {'template_id': 'hessen', 'route_id': route['id'], 'name': 'Long route test',
            'start_m': start, 'end_m': route['length_m'] if end is None else end,
            'road_width_m': 6., 'scenery_quality': 'standard',
            'vegetation_density': 0., 'ai_materials': False}


class SyntheticHeights:
    def __init__(self, path):
        if not Path(path).is_file():
            raise AssertionError('Orchestrator must acquire the DEM before sampling it')

    def __call__(self, xy):
        return 200. + (np.asarray(xy)[:, 0] - 450_000.) * .001


class SyntheticGeo:
    def __init__(self, root, route):
        self.cache = root / 'geo-cache'
        self.route = route
        self.osm_calls = []
        self.rasters = []
        self.prepared = []
        self.fail_osm_at = None

    def get_route(self, identifier):
        if identifier != self.route['id']:
            raise GeoError('Unknown test route')
        return deepcopy(self.route)

    def _osm(self, bounds):
        self.osm_calls.append(bounds)
        if len(self.osm_calls) == self.fail_osm_at:
            raise GeoError('Synthetic unavailable road section', 'unsupported_structure')
        return 'synthetic-context', b'<osm version="0.6"/>'

    def _raster(self, path, bounds, kind):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'Synthetic DEM sampled through SyntheticHeights')
        self.rasters.append((kind, bounds))

    def prepare_route(self, identifier, destination, progress=None, *, section_route, section_index):
        self.prepared.append((identifier, section_index))
        destination.mkdir(parents=True, exist_ok=True)
        (destination / 'textures').mkdir()
        Image.new('RGB', (2, 2), (90, 90, 90)).save(destination / 'textures/road.png')
        Image.new('RGB', (2, 2), (100, 110, 95)).save(destination / 'textures/ortho.jpg')
        (destination / 'area.osm').write_bytes(b'<osm version="0.6"/>')
        for name, value in (
            ('road_ways.json', section_route['ways']),
            ('bounds.json', section_route['bounds_utm']),
            ('retrieval.json', {'recorded_at_utc': '2026-10-23T00:00:00Z', 'route_id': identifier,
              'sources': {}, 'osm': {'provider': 'Synthetic test', 'source_way_ids': [42]},
              'warnings': ['Synthetic test source, not real geography.']})):
            (destination / name).write_text(json.dumps(value), encoding='utf-8')
        if progress:
            progress('Synthetic data ready', 100)
        return {'warnings': ['Synthetic test source, not real geography.']}


def synthetic_section(route, xy, section, content):
    return {'id': route['id'], 'bounds_utm': section['bounds'], 'source_way_ids': [42],
            'ways': [{'id': 42}], 'warnings': [], 'bounds': route['bounds']}


def synthetic_scene(root, cfg):
    """Small road-only fixture from the real shared profile supplied to prepare."""
    profile = cfg['_road_profile']
    xy, left = np.asarray(profile['xy']), np.asarray(profile['left'])
    origin = np.asarray(profile['origin_utm32'])
    distance = np.asarray(profile['distance'])
    road_y = np.asarray(profile['road_y']) - profile['height_origin_m']
    offsets = np.asarray(profile['road_widths'])[:, None] / 2 * np.linspace(-1, 1, 7)
    horizontal = xy[:, None, :] + left[:, None, :] * offsets[:, :, None]
    height = road_y[:, None] - np.abs(offsets) * cfg['road_crossfall']
    vertices = np.stack([horizontal[:, :, 0] - origin[0], height,
                         -(horizontal[:, :, 1] - origin[1])], axis=-1).reshape(-1, 3)
    faces = []
    for i in range(len(xy) - 1):
        for j in range(6):
            a, b = i * 7 + j, (i + 1) * 7 + j
            faces.extend(((a, b, a + 1), (a + 1, b, b + 1)))
    a, b, c = vertices[np.asarray(faces[0])]
    if np.cross(b - a, c - a)[1] < 0:
        faces = [(a, c, b) for a, b, c in faces]
    uv = np.c_[offsets.ravel() / 2, np.repeat(distance, 7) / 2]
    matrix = np.eye(4)
    matrix[3, :3] = vertices[10 * 7 + 3] + [0, .25, 0]
    return {'materials': [{'name': 'synthetic asphalt', 'texture': 'road.png',
                            'ambient': .4, 'diffuse': .6, 'specular': .1}],
            'meshes': [{'name': '1ROAD_MAIN', 'vertices': vertices.tolist(), 'faces': faces,
                        'uv': uv.tolist(), 'material': 0, 'collision': True}],
            'markers': [{'name': 'AC_PIT_0', 'matrix': matrix.tolist()}],
            'textures_dir': cfg['data_dir'] + '/textures',
            'metadata': {'id': cfg['id'], 'length_m': float(distance[-1] - distance[0]),
                         'buildings': 0, 'elevation_min_m': float(road_y.min()),
                         'elevation_max_m': float(road_y.max()),
                         'origin_utm32': origin.tolist(),
                         'centerline_ac': np.c_[xy[:, 0] - origin[0], road_y,
                                                -(xy[:, 1] - origin[1])].tolist(),
                         'road_width_profile_m': profile['road_widths']}}


def synthetic_preview(section_root, track, progress):
    Image.new('RGB', (16, 9), (80, 85, 70)).save(track / 'ui/preview.png')
    return 'render', ['overview']


class LongBuildTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'test-results'
        parent.mkdir(exist_ok=True)
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.folder = Path(self.resources.enter_context(tempfile.TemporaryDirectory(
            prefix='long-build-test-', dir=parent)))
        self.route = synthetic_route()
        self.geo = SyntheticGeo(self.folder, self.route)
        self.progress = []

    def run_fixture(self, request=None):
        request = request or selection(self.route)
        cfg = {'id': 'test', 'name': 'test', 'city': 'Hessen', 'country': 'Germany',
               'version': '0.13.0', 'pitboxes': 1, 'data_dir': 'data/altenhain',
               'road_crossfall': .025}
        with ExitStack() as stack:
            stack.enter_context(patch.object(longbuild, 'config', return_value=cfg))
            stack.enter_context(patch.object(longbuild, 'Heights', SyntheticHeights))
            stack.enter_context(patch.object(longbuild, 'prepare', side_effect=synthetic_scene))
            stack.enter_context(patch.object(longbuild, 'copy_materials'))
            stack.enter_context(patch.object(longbuild, 'render_section', side_effect=synthetic_preview))
            stack.enter_context(patch('webapp.route_sections.section_route', side_effect=synthetic_section))
            stack.enter_context(patch.object(longbuild.shutil, 'disk_usage', return_value=SimpleNamespace(free=2**50)))
            longbuild.run_long(self.folder, request, self.route, self.geo,
                               lambda stage, percent: self.progress.append((stage, percent)))
        return json.loads((self.folder / 'result.json').read_text(encoding='utf-8'))

    def test_over_fifty_km_builds_one_real_multimodel_archive(self):
        result = self.run_fixture()
        summary = result['summary']
        self.assertGreater(summary['length_m'], 54_000)
        self.assertGreater(summary['sections_count'], 50)
        self.assertEqual(summary['sections_count'], summary['model_count'])
        self.assertEqual(summary['preview_scope'], 'representative_section')
        self.assertEqual(len(self.geo.prepared), summary['sections_count'])
        self.assertEqual([index for _, index in self.geo.prepared], list(range(summary['sections_count'])))
        self.assertEqual(len(set(identifier for identifier, _ in self.geo.prepared)), summary['sections_count'])
        self.assertEqual(self.progress[-1], ('Fertig', 100))
        self.assertTrue(all(0 <= value <= 100 for _, value in self.progress))
        with zipfile.ZipFile(self.folder / result['archive']) as archive:
            self.assertIsNone(archive.testzip())
            names = archive.namelist()
            models = [name for name in names if name.endswith('.kn5')]
            self.assertEqual(len(models), summary['sections_count'])
            self.assertEqual(len({name.split('/')[2] for name in names}), 1)
            ini = next(name for name in names if name.endswith('/models.ini'))
            model_names = [line[5:] for line in archive.read(ini).decode().splitlines() if line.startswith('FILE=')]
            self.assertEqual(set(model_names), {Path(name).name for name in models})
            for name in models:
                self.assertEqual(archive.read(name)[:6], b'sc6969')
            self.assertEqual(sum(name.endswith('/geometry.json') for name in names), summary['sections_count'])
            self.assertTrue(any(name.endswith('/provenance/routing.json') for name in names))
            self.assertFalse(any('geo-cache' in name or 'ai-settings' in name for name in names))
            prefix = ini.removesuffix('models.ini')
            manifest = json.loads(archive.read(prefix + 'prototype_manifest.json'))
            source_files = {name.removeprefix(prefix) for name in names if name.startswith(prefix + 'provenance/')}
            self.assertEqual(set(manifest['provenance_files']), source_files)
            for name, record in manifest['provenance_files'].items():
                content = archive.read(prefix + name)
                self.assertEqual(record['bytes'], len(content))
                self.assertEqual(record['sha256'], hashlib.sha256(content).hexdigest())

    def test_selected_subrange_preserves_requested_extent_and_location(self):
        request = selection(self.route, 12_500., 14_700.)
        xy = longbuild.selected_geometry(self.route, request)
        self.assertAlmostEqual(xy[0, 0], 462_500., places=4)
        self.assertAlmostEqual(xy[-1, 0], 464_700., places=4)
        result = self.run_fixture(request)
        self.assertAlmostEqual(result['summary']['length_m'], 2200., places=3)
        self.assertEqual(result['summary']['sections_count'], 3)
        with zipfile.ZipFile(self.folder / result['archive']) as archive:
            ui = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith('/ui/ui_track.json'))))
            lon, lat = TO_WGS.transform(xy[0, 0], xy[0, 1])
            np.testing.assert_allclose(np.asarray(ui['geotags'], dtype=float), [lat, lon], atol=1e-6)
            generated = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith('/provenance/generated-route.json'))))
            self.assertEqual(generated['selection']['start_m'], 12_500.)
            self.assertEqual(generated['selection']['end_m'], 14_700.)
            self.assertAlmostEqual(generated['selection']['generated_length_m'], 2200., places=3)
            np.testing.assert_allclose(generated['coordinates'][0], [lat, lon], atol=1e-7)
            record = json.loads(archive.read(next(name for name in archive.namelist() if name.endswith('/provenance/route.json'))))
            self.assertEqual(record['sources_scope'], 'first_section')
            self.assertEqual(len(record['section_source_records']), 3)

    def test_storage_failure_precedes_any_provider_access(self):
        with patch.object(longbuild.shutil, 'disk_usage', return_value=SimpleNamespace(free=1)):
            with self.assertRaises(GeoError) as caught:
                longbuild.run_long(self.folder, selection(self.route), self.route, self.geo, lambda *args: None)
        self.assertEqual(caught.exception.code, 'insufficient_storage')
        self.assertFalse(self.geo.osm_calls)
        self.assertFalse(self.geo.rasters)
        self.assertFalse(self.geo.prepared)
        self.assertFalse((self.folder / 'result.json').exists())

    def test_unsupported_later_section_fails_before_detailed_downloads(self):
        self.geo.fail_osm_at = 3
        with self.assertRaises(GeoError) as caught:
            self.run_fixture()
        self.assertEqual(caught.exception.code, 'unsupported_structure')
        self.assertEqual(len(self.geo.osm_calls), 3)
        self.assertFalse(self.geo.prepared)
        self.assertFalse((self.folder / 'result.json').exists())

    def test_job_poll_does_not_repeat_full_route_geometry(self):
        manager = JobManager(self.folder / 'jobs-state', start_worker=False, geo_service=self.geo)
        try:
            job = manager.create(selection(self.route))
            summary = {'length_m': self.route['length_m'], 'sections_count': 55,
                       'source_route': deepcopy(self.route), 'preview_scope': 'representative_section',
                       'models': [{'file': 'test.kn5'}], 'centerline_ac': [[1, 2, 3]] * 55_000}
            manager._update(job['id'], status='succeeded', summary=json.dumps(summary))
            public = manager.get(job['id'])
            self.assertNotIn('coordinates', public['summary']['source_route'])
            self.assertNotIn('routing', public['summary']['source_route'])
            self.assertNotIn('centerline_ac', public['summary'])
            self.assertNotIn('models', public['summary'])
            self.assertEqual(public['summary']['sections_count'], 55)
            self.assertLess(len(json.dumps(public)), 5000)
        finally:
            manager.close()

    def test_queue_prelaunch_failure_does_not_spawn_or_orphan_a_worker(self):
        manager = JobManager(self.folder / 'failed-launch-state', start_worker=False, geo_service=self.geo)
        try:
            job = manager.create(selection(self.route, 0., 200.))
            update = manager._update
            def observe_failure(identifier, **values):
                update(identifier, **values)
                if values.get('status') == 'failed':
                    manager.stop.set()
            with patch.object(self.geo, 'get_route', side_effect=GeoError('Cached route is unavailable')):
                with patch.object(manager, '_update', side_effect=observe_failure):
                    with patch('webapp.jobs.subprocess.Popen') as launch:
                        manager._run()
                        launch.assert_not_called()
            self.assertIsNone(manager.process)
            self.assertEqual(manager.get(job['id'])['status'], 'failed')
        finally:
            manager.close()

    def test_catalog_has_no_fixed_kilometre_cap(self):
        with patch('webapp.server.template', return_value={'id': 'altenhain'}):
            server = create_server(port=0, data_root=self.folder / 'http-state',
                                   start_worker=False, geo_service=self.geo)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
            thread.start()
            try:
                with build_opener(ProxyHandler({})).open(
                    f'http://127.0.0.1:{server.server_port}/api/catalog', timeout=5) as response:
                    data = json.load(response)
                self.assertIsNone(data['discovery']['max_length_m'])
                self.assertEqual(data['discovery']['min_segment_m'], 120)
                self.assertEqual(data['discovery']['section_length_m'], 1000)
                self.assertGreaterEqual(data['discovery']['preview_timeout_s'], 120)
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
                server.manager.close()


if __name__ == '__main__':
    unittest.main()
