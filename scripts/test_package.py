"""Track exports carry their own geographic attribution without local state."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from roadtoassetto.package import package, archive


ROOT = Path(__file__).resolve().parents[1]


class PackageProvenanceTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / 'test-results'
        parent.mkdir(exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(prefix='package-proof-', dir=parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.data = self.root / 'data/fixture'
        self.data.mkdir(parents=True)
        self.config = {'id': 'rtac_fixture', 'name': 'Neue Teststraße', 'city': 'Hessen',
                       'country': 'Germany', 'road_width_m': 5.5, 'pitboxes': 4,
                       'version': '0.12.0', 'data_dir': 'data/fixture', 'geotags': ['50.15', '8.46']}
        self.scene = {'metadata': {'length_m': 200., 'elevation_min_m': 100.,
                      'elevation_max_m': 104., 'centerline_ac': [[0, 100, 0], [200, 104, 0]],
                      'road_crossfall': {'slope': .025}, 'hillside_provenance': 'inferred'}}

    def new_route(self):
        self.config['source_route'] = {'id': 'a' * 32, 'name': 'Neue Teststraße',
            'length_m': 200, 'coordinates': [[50.15, 8.46], [50.15, 8.463]],
            'bounds': [8.45, 50.14, 8.47, 50.16], 'source': 'OpenStreetMap / HVBG',
            'warnings': [], 'context_key': 'PRIVATE-CACHE-CANARY'}
        self.scene['metadata']['source_route'] = deepcopy(self.config['source_route'])
        (self.data / 'area.osm').write_bytes(b'<osm version="0.6"><way id="42"/></osm>')
        (self.data / 'road_ways.json').write_text(json.dumps([{'id': 42, 'nodes': [1, 2]}]))
        (self.data / 'bounds.json').write_text('[461000,5556000,461500,5556500]')
        retrieval = {'recorded_at_utc': '2026-10-23T12:34:56+00:00', 'route_id': 'a' * 32,
            'warnings': ['DOM1 unavailable'],
            'sources': {'dgm1': {'provider': 'HVBG Hessen', 'license': 'dl-de/zero-2-0',
                                'sha256': 'f' * 64, 'private_path': 'PRIVATE-CACHE-CANARY'},
                        'internal_cache': {'private_path': 'PRIVATE-CACHE-CANARY'}},
            'osm': {'provider': 'OpenStreetMap contributors', 'license': 'ODbL-1.0',
                    'url': 'https://www.openstreetmap.org/copyright', 'source_way_ids': [42],
                    'internal_key': 'PRIVATE-CACHE-CANARY'},
            'private_path': 'PRIVATE-CACHE-CANARY'}
        (self.data / 'retrieval.json').write_text(json.dumps(retrieval), encoding='utf-8')
        (self.data / 'ai-settings.json').write_text('PRIVATE-SETTINGS-CANARY')
        (self.data / 'build.log').write_text('PRIVATE-LOG-CANARY')

    def test_new_export_has_actual_identity_date_raw_osm_and_safe_provenance(self):
        self.new_route()
        original = deepcopy(self.scene)
        track = package(self.root, self.config, self.scene)
        credits = (track / 'CREDITS.txt').read_text(encoding='utf-8')
        self.assertIn('Road to Assetto - Neue Teststraße prototype', credits)
        self.assertIn('2026-10-23T12:34:56+00:00', credits)
        self.assertNotIn('2026-10-21', credits)
        self.assertNotIn('Altenhain', credits)
        self.assertNotIn('research/', credits)
        self.assertNotIn('historical local photographs', credits)
        self.assertEqual(self.scene, original, 'Export attribution must not rewrite source geometry metadata')
        manifest = json.loads((track / 'prototype_manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(manifest['source_route']['source_way_ids'], [42])
        self.assertEqual(manifest['source_route']['retrieved_at_utc'], '2026-10-23T12:34:56+00:00')
        self.assertEqual(set(manifest['source_route']['sources']), {'dgm1'})
        with zipfile.ZipFile(archive(self.root, self.config, track)) as zipped:
            prefix = 'content/tracks/rtac_fixture/'
            self.assertEqual(zipped.read(prefix + 'provenance/area.osm'), (self.data / 'area.osm').read_bytes())
            self.assertEqual(zipped.read(prefix + 'provenance/road_ways.json'), (self.data / 'road_ways.json').read_bytes())
            for name in zipped.namelist():
                self.assertNotIn('ai-settings', name)
                self.assertNotIn('build.log', name)
                self.assertNotIn(b'PRIVATE-', zipped.read(name))
            for name, record in manifest['provenance_files'].items():
                content = zipped.read(prefix + name)
                self.assertEqual(record['bytes'], len(content))
                self.assertEqual(record['sha256'], hashlib.sha256(content).hexdigest())

    def test_missing_new_route_source_fails_instead_of_claiming_it_is_included(self):
        self.new_route()
        (self.data / 'area.osm').unlink()
        with self.assertRaisesRegex(ValueError, 'area.osm'):
            package(self.root, self.config, self.scene)

    def test_prepared_track_keeps_historical_reference_without_requiring_live_sources(self):
        self.config['name'] = 'Altenhain - Langstraße'
        track = package(self.root, self.config, self.scene)
        credits = (track / 'CREDITS.txt').read_text(encoding='utf-8')
        self.assertIn('Altenhain - Langstraße', credits)
        self.assertIn('2026-10-21', credits)
        self.assertIn('historical local photographs', credits)
        self.assertFalse((track / 'provenance').exists())
        manifest = json.loads((track / 'prototype_manifest.json').read_text(encoding='utf-8'))
        self.assertNotIn('source_route', manifest)


if __name__ == '__main__':
    unittest.main()
