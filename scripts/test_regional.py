"""Complete bounded OSM extracts through the real libosmium reader."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET

from webapp import regional
from webapp.geodata import GeoError, _json_write, TO_UTM

XML = b'''<osm version="0.6">
<node id="1" lat="50" lon="8.00" user="not-exported"/>
<node id="2" lat="50" lon="8.04"/>
<node id="3" lat="50.001" lon="8.011"><tag k="natural" v="tree"/></node>
<node id="4" lat="50" lon="8.021"><tag k="barrier" v="gate"/><tag k="access" v="private"/></node>
<node id="5" lat="51" lon="9"/>
<node id="6" lat="51" lon="9.01"/>
<way id="10"><nd ref="1"/><nd ref="2"/><tag k="highway" v="secondary"/><tag k="bridge" v="yes"/></way>
<way id="11"><nd ref="5"/><nd ref="6"/><tag k="highway" v="residential"/></way>
</osm>'''


class RegionalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'fixture.osm'
        self.source.write_bytes(XML)
        self.windows = [[8.01, 49.999, 8.012, 50.002], [8.02, 49.999, 8.022, 50.002]]

    def test_crossing_way_keeps_all_references_and_structure_tags(self):
        extracts = regional.extract_windows(self.source, self.windows, lambda *a: None)
        for content in extracts:
            doc = ET.fromstring(content)
            self.assertEqual([w.get('id') for w in doc.findall('way')], ['10'])
            nodes = {n.get('id') for n in doc.findall('node')}
            self.assertTrue({'1', '2'} <= nodes)
            self.assertEqual(doc.find('way/tag[@k="bridge"]').get('v'), 'yes')
            self.assertNotIn(b'not-exported', content)
        self.assertIn(b'k="natural" v="tree"', extracts[0])
        self.assertNotIn(b'k="natural" v="tree"', extracts[1])
        self.assertIn(b'k="access" v="private"', extracts[1])

    def test_empty_coverage_fails_instead_of_fabricating_roads(self):
        with self.assertRaises(GeoError) as error:
            regional.extract_windows(self.source, [[7., 49., 7.1, 49.1]], lambda *a: None)
        self.assertEqual(error.exception.code, 'regional_coverage')

    def test_partition_cache_hash_verified_and_corruption_rebuilt(self):
        sections = []
        for bounds in self.windows:
            a = TO_UTM.transform(*bounds[:2]); b = TO_UTM.transform(*bounds[2:])
            sections.append({'bounds': [*a, *b]})
        meta = {'sha256': hashlib.sha256(XML).hexdigest(), 'provider': regional.SOURCE}
        with patch.dict('os.environ', {'ROADTOASSETTO_OSM_MODE': 'regional'}), \
                patch.object(regional, 'regional_file', return_value=(self.source, meta)), \
                patch.object(regional, 'extract_windows', wraps=regional.extract_windows) as extract:
            initial = regional.prepare_regional(self.root, sections, lambda *a: None)
            again = regional.prepare_regional(self.root, sections, lambda *a: None)
            self.assertEqual(initial, again); self.assertEqual(extract.call_count, 1)
            (self.root / 'osm' / (initial[0][0] + '.osm')).write_bytes(b'corrupt')
            self.assertEqual(initial, regional.prepare_regional(self.root, sections, lambda *a: None))
            self.assertEqual(extract.call_count, 2)

    def test_short_route_uses_no_regional_download(self):
        with patch.dict('os.environ', {'ROADTOASSETTO_OSM_MODE': 'auto'}), patch.object(regional, 'regional_file') as download:
            self.assertIsNone(regional.prepare_regional(self.root, [{}], lambda *a: None))
            download.assert_not_called()

    def test_verified_regional_cache_avoids_network(self):
        folder = self.root / 'regional'; folder.mkdir()
        path = folder / 'hessen-latest.osm.pbf'; path.write_bytes(XML)
        meta = {'sha256': hashlib.sha256(XML).hexdigest(), 'provider': regional.SOURCE}
        _json_write(path.with_suffix('.json'), meta)
        with patch.object(regional.requests, 'get') as get:
            self.assertEqual(regional.regional_file(self.root, lambda *a: None), (path, meta))
            get.assert_not_called()


if __name__ == '__main__':
    unittest.main()
