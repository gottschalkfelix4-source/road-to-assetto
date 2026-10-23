"""Local AI integration contracts; no live providers or user settings are used.

The HTTP tests bind an ephemeral loopback port with the builder disabled. Image,
settings, queue and progress fixtures live exclusively in temporary directories.
"""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from roadtoassetto.ai_materials import apply_material_pack
from roadtoassetto.buildings import glass_textures
from webapp.server import create_server
from webapp.worker import write_progress


class AIHTTPIntegrationTests(unittest.TestCase):
    SECRET = 'integration-test-only-NOT-A-REAL-API-KEY'

    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        temporary = self.resources.enter_context(tempfile.TemporaryDirectory(prefix='rtac-ai-http-'))
        self.root = Path(temporary)
        upstream = self.resources.enter_context(patch(
            'webapp.ai._request', side_effect=AssertionError('No upstream API is allowed in integration tests')))
        self.resources.callback(upstream.assert_not_called)
        self.server = create_server(port=0, data_root=self.root, start_worker=False)
        from scripts.test_auth import authenticated_headers
        self.auth_headers=authenticated_headers(self.server)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(.05,), daemon=True)
        self.thread.start()
        self.resources.callback(self.close_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)
        self.opener = build_opener(ProxyHandler({}))

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.manager.close()
        self.assertFalse(self.thread.is_alive(), 'The temporary HTTP server must stop')

    def call(self, path, payload=None):
        request = Request(self.base + path,
                          data=None if payload is None else json.dumps(payload).encode('utf-8'),
                          headers={'Content-Type': 'application/json',**self.auth_headers})
        try:
            response = self.opener.open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
            # Check every response, including error messages and job metadata.
            self.assertNotIn(self.SECRET.encode(), raw)
            return response.status, json.loads(raw)

    def assert_public_settings(self, response, configured):
        self.assertEqual(set(response), {'provider', 'base_url', 'image_model', 'api_key_configured'})
        self.assertIs(response['api_key_configured'], configured)

    def configure(self, **overrides):
        payload = {'provider': 'compatible', 'base_url': 'http://127.0.0.1:9/v1',
                   'image_model': 'local-test-image-model', 'api_key': self.SECRET}
        payload.update(overrides)
        status, response = self.call('/api/ai/settings', payload)
        self.assertEqual(status, 200, response)
        return response

    def test_settings_never_return_key_and_blank_update_preserves_it(self):
        status, response = self.call('/api/ai/settings')
        self.assertEqual(status, 200)
        self.assert_public_settings(response, False)
        self.assert_public_settings(self.configure(), True)
        for payload in (None, {'api_key': '', 'image_model': 'another-local-model'}, None):
            status, response = self.call('/api/ai/settings', payload)
            self.assertEqual(status, 200, response)
            self.assert_public_settings(response, True)
        persisted = json.loads((self.root / 'ai-settings.json').read_text(encoding='utf-8'))
        self.assertEqual(persisted['api_key'], self.SECRET)
        status, response = self.call('/api/ai/settings', {'clear_api_key': True})
        self.assertEqual(status, 200)
        self.assert_public_settings(response, False)
        self.assert_public_settings(self.call('/api/ai/settings')[1], False)
        self.assertEqual(json.loads((self.root / 'ai-settings.json').read_text())['api_key'], '')

    def test_switching_provider_or_destination_discards_previous_key(self):
        scenarios = [
            ({'provider': 'openai', 'base_url': 'https://api.openai.com/v1'},
             {'provider': 'compatible', 'base_url': 'https://api.openai.com/v1'}),
            ({}, {'base_url': 'http://127.0.0.1:10/v1'}),
        ]
        for original, changed in scenarios:
            with self.subTest(change=changed):
                self.assert_public_settings(self.configure(**original), True)
                status, response = self.call('/api/ai/settings', changed)
                self.assertEqual(status, 200, response)
                self.assert_public_settings(response, False)
                self.assert_public_settings(self.call('/api/ai/settings')[1], False)
                saved = json.loads((self.root / 'ai-settings.json').read_text(encoding='utf-8'))
                self.assertEqual(saved['api_key'], '')

    def test_settings_errors_do_not_echo_key_or_corrupt_saved_settings(self):
        self.configure()
        original = (self.root / 'ai-settings.json').read_bytes()
        status, response = self.call('/api/ai/settings', {'image_model': self.SECRET})
        self.assertEqual(status, 400)
        self.assertIn('error', response)
        self.assertEqual((self.root / 'ai-settings.json').read_bytes(), original)
        (self.root / 'ai-settings.json').write_text('{"api_key":"' + self.SECRET, encoding='utf-8')
        status, response = self.call('/api/ai/settings')
        self.assertEqual(status, 500)
        self.assertEqual(response['code'], 'settings_unreadable')

    def test_job_rejects_non_boolean_ai_values_without_enqueueing(self):
        self.configure()
        for value in (None, 0, 1, 0.0, 1.0, '', 'false', 'true', [], {}, [True], {'enabled': True}):
            with self.subTest(ai_materials=value):
                status, response = self.call('/api/jobs', {'template_id': 'altenhain', 'ai_materials': value})
                self.assertEqual(status, 400, response)
                self.assertIn('error', response)
                self.assertNotIn('id', response)
        self.assertEqual(self.call('/api/jobs'), (200, {'jobs': []}))

    def test_ai_without_model_is_rejected_but_optional_ai_jobs_are_allowed(self):
        status, response = self.call('/api/jobs', {'template_id': 'altenhain', 'ai_materials': True})
        self.assertEqual(status, 400, response)
        self.assertIn('error', response)
        self.assertEqual(self.call('/api/jobs')[1]['jobs'], [])
        for optional in ({}, {'ai_materials': False}):
            with self.subTest(settings=optional):
                status, response = self.call('/api/jobs', {'template_id': 'altenhain', **optional})
                self.assertEqual(status, 202, response)
                self.assertEqual(response['status'], 'queued')
                self.assertIs(response['settings']['ai_materials'], False)
        self.assertFalse((self.root / 'ai-settings.json').exists())

    def test_configured_ai_job_is_queued_without_calling_provider(self):
        self.configure()
        status, response = self.call('/api/jobs', {'template_id': 'altenhain', 'ai_materials': True})
        self.assertEqual(status, 202, response)
        self.assertEqual(response['status'], 'queued')
        self.assertIs(response['settings']['ai_materials'], True)
        status, stored = self.call('/api/jobs/' + response['id'])
        self.assertEqual(status, 200)
        self.assertEqual(stored['settings'], response['settings'])


class MaterialPackIntegrationTests(unittest.TestCase):
    def test_pack_changes_only_visual_materials_and_preserves_registered_photo(self):
        with tempfile.TemporaryDirectory(prefix='rtac-ai-materials-') as temporary:
            root = Path(temporary)
            textures, pack = root / 'textures', root / 'pack'
            textures.mkdir()
            pack.mkdir()
            for index, kind in enumerate(('plaster', 'roof', 'asphalt')):
                image = Image.new('RGB', (64, 64), (135 + index * 20, 120, 105))
                ImageDraw.Draw(image).rectangle((4, 7, 30, 53), fill=(80, 75, 65))
                image.save(pack / ('ai_' + kind + '.png'))
            provenance = {'type': 'ai_generated_generic_materials', 'synthetic': True,
                          'real_building_photographs': False, 'geographic_accuracy': False,
                          'provider': 'local-test-fixture', 'image_model': 'fixture-model',
                          'rights_note': 'Synthetic fixture, not a site photograph or a CC0 claim.'}
            (pack / 'ai_materials.json').write_text(json.dumps(provenance), encoding='utf-8')
            names = ['asphalt', 'photographic_roof_tiles', 'village_plain_0',
                     'village_close_ground_0', 'village_foundation_0',
                     'photo_real_registered_wall', 'window_glass_0']
            materials = []
            for index, name in enumerate(names):
                filename = name + '.png'
                Image.new('RGB', (48, 32), (90 + index * 15, 110, 100)).save(textures / filename)
                materials.append({'name': name, 'texture': filename, 'ambient': .4, 'diffuse': .6})
            scene = {'materials': materials, 'meshes': [
                {'name': '1ROAD', 'material': 0, 'vertices': [[0, 0, 0], [6, 0, 0], [0, 0, 10]],
                 'faces': [[0, 1, 2]], 'uvs': [[0, 0], [1, 0], [0, 1]], 'collision': True},
                {'name': 'LOD2_PHOTO_WALL', 'material': 5,
                 'vertices': [[12, 0, 0], [12, 6, 0], [12, 0, 8]], 'faces': [[0, 1, 2]]}],
                'nodes': [{'name': 'AC_START_0', 'position': [3, .1, 5]}],
                'metadata': {'route_length_m': 10, 'source': 'unchanged real survey'}}
            before = deepcopy(scene)
            originals = {path.name: path.read_bytes() for path in textures.iterdir()}
            result = apply_material_pack(scene, textures, pack)

            self.assertEqual({key: value for key, value in scene.items() if key != 'materials'},
                             {key: value for key, value in before.items() if key != 'materials'})
            self.assertEqual(len(scene['materials']), len(before['materials']))
            self.assertEqual(result['applied_materials'], names[:5])
            self.assertEqual(scene['materials'][5:], before['materials'][5:])
            for name, raw in originals.items():
                self.assertEqual((textures / name).read_bytes(), raw, name + ' original was overwritten')
            for original, applied in zip(before['materials'][:5], scene['materials'][:5]):
                with self.subTest(material=original['name']):
                    self.assertNotEqual(applied['texture'], original['texture'])
                    for key in original.keys() - {'texture', 'normal', 'normal_strength'}:
                        self.assertEqual(applied[key], original[key])
                    for key in ('texture', 'normal'):
                        with Image.open(textures / applied[key]) as image:
                            image.load()
                            self.assertEqual(image.mode, 'RGB')
                            self.assertEqual(image.width, 1024)
                            self.assertIn(image.height, (512, 1024))
            self.assertIs(result['enabled'], True)
            self.assertEqual(result['provenance'], provenance)
            exported = json.loads((textures / 'ai_materials.json').read_text(encoding='utf-8'))
            self.assertEqual(exported, provenance)
            self.assertIs(exported['synthetic'], True)
            self.assertIs(exported['real_building_photographs'], False)
            self.assertIs(exported['geographic_accuracy'], False)


class ProgressWriteIntegrationTests(unittest.TestCase):
    def test_two_transient_replace_failures_eventually_publish_complete_json(self):
        with tempfile.TemporaryDirectory(prefix='rtac-progress-retry-') as temporary:
            root = Path(temporary)
            destination = root / 'progress.json'
            previous = {'stage': 'Vorheriger Schritt', 'progress': 12}
            destination.write_text(json.dumps(previous), encoding='utf-8')
            expected = {'stage': 'Gebäude und Oberfläche', 'progress': 38}
            replace = Path.replace
            attempts = []

            def interrupted(source, target):
                self.assertEqual(source, root / 'progress.tmp')
                self.assertEqual(target, destination)
                self.assertEqual(json.loads(source.read_text(encoding='utf-8')), expected)
                attempts.append(source)
                if len(attempts) <= 2:
                    self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), previous)
                    raise PermissionError('Simulated reader still holds the progress file')
                return replace(source, target)

            with patch.object(Path, 'replace', interrupted), patch('webapp.worker.time.sleep') as sleep:
                write_progress(root, expected['stage'], expected['progress'])
            self.assertEqual(len(attempts), 3)
            self.assertEqual(sleep.call_count, 2)
            self.assertEqual(json.loads(destination.read_text(encoding='utf-8')), expected)
            self.assertFalse((root / 'progress.tmp').exists())

    def test_persistent_replace_failure_is_reported_and_previous_json_stays_valid(self):
        with tempfile.TemporaryDirectory(prefix='rtac-progress-failure-') as temporary:
            root = Path(temporary)
            previous = {'stage': 'Schon veröffentlicht', 'progress': 14}
            (root / 'progress.json').write_text(json.dumps(previous), encoding='utf-8')
            with patch.object(Path, 'replace', side_effect=PermissionError('Persistent sharing violation')) as replace:
                with patch('webapp.worker.time.sleep'):
                    with self.assertRaisesRegex(PermissionError, 'Persistent sharing violation'):
                        write_progress(root, 'Neuer Schritt', 26)
            self.assertGreater(replace.call_count, 1)
            self.assertLessEqual(replace.call_count, 10, 'Retries must be bounded')
            self.assertEqual(json.loads((root / 'progress.json').read_text(encoding='utf-8')), previous)
            self.assertEqual(json.loads((root / 'progress.tmp').read_text(encoding='utf-8')),
                             {'stage': 'Neuer Schritt', 'progress': 26})


class WindowAtlasIntegrationTests(unittest.TestCase):
    def test_uv_rectangles_sample_their_own_atlas_cell_with_safe_insets(self):
        with tempfile.TemporaryDirectory(prefix='rtac-window-atlas-') as temporary:
            root = Path(temporary)
            textures = root / 'textures'
            textures.mkdir()
            atlas = Image.new('RGB', (1024, 512))
            draw = ImageDraw.Draw(atlas)
            colours = [(30 + i * 25, 220 - i * 20, 60 + i * 12) for i in range(8)]
            for index, colour in enumerate(colours):
                column, row = index % 4, index // 4
                draw.rectangle((column * 256, row * 256, (column + 1) * 256 - 1,
                                (row + 1) * 256 - 1), fill=colour)
            source = root / 'source-atlas.png'
            atlas.save(source)
            scene = {'materials': [{'name': 'existing material', 'texture': 'existing.png'}]}

            def material(name, texture, **properties):
                scene['materials'].append({'name': name, 'texture': texture, **properties})
                return len(scene['materials']) - 1

            indices = glass_textures(textures, material, scene, source)
            self.assertEqual(indices, list(range(1, 9)))
            self.assertEqual((textures / 'window_atlas.png').read_bytes(), source.read_bytes())
            rectangles = []
            for variant, index in enumerate(indices):
                with self.subTest(variant=variant):
                    current = scene['materials'][index]
                    self.assertEqual(current['name'], 'window_glass_' + str(variant))
                    self.assertEqual(current['texture'], 'window_atlas.png')
                    u0, v0, u1, v1 = current['uv_rect']
                    column, row = variant % 4, variant // 4
                    self.assertTrue(column / 4 < u0 < u1 < (column + 1) / 4)
                    self.assertTrue(row / 2 < v0 < v1 < (row + 1) / 2)
                    self.assertGreater(u1 - u0, .2, 'The window must retain most of its cell')
                    self.assertGreater(v1 - v0, .4)
                    for u in (u0, (u0 + u1) / 2, u1):
                        for v in (v0, (v0 + v1) / 2, v1):
                            self.assertEqual(atlas.getpixel((int(u * atlas.width), int(v * atlas.height))),
                                             colours[variant], 'UV crosses into another window variant')
                    rectangles.append((u0, v0, u1, v1))
            for i, a in enumerate(rectangles):
                for b in rectangles[i + 1:]:
                    self.assertTrue(a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


if __name__ == '__main__':
    unittest.main(verbosity=2)
