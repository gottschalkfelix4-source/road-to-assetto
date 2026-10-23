"""Regression tests for path-specific structure preflight on long routes."""
import unittest
from xml.etree import ElementTree as ET

import numpy as np

from roadtoassetto.prepare import ordered_road
from webapp.geodata import GeoError, TO_UTM, TO_WGS
from webapp.route_sections import section_route


BASE = np.array([462000., 5556400.])


def osm(nodes, ways):
    document = ET.Element('osm', version='0.6')
    for node, xy in nodes.items():
        lon, lat = TO_WGS.transform(*(BASE + xy))
        ET.SubElement(document, 'node', id=str(node), lat=str(lat), lon=str(lon))
    for identifier, sequence, tags in ways:
        way = ET.SubElement(document, 'way', id=str(identifier))
        for node in sequence:
            ET.SubElement(way, 'nd', ref=str(node))
        for key, value in {'highway': 'residential', **tags}.items():
            ET.SubElement(way, 'tag', k=key, v=value)
    return ET.tostring(document)


class SectionMatchingTests(unittest.TestCase):
    def setUp(self):
        self.xy = BASE + np.c_[np.arange(401), np.zeros(401)]
        self.route = {'id': '1' * 32, 'name': 'Teststraße', 'length_m': 400,
                      'routing': {'node_ids': [1, 2, 3]}, 'warnings': [], 'source': 'Test'}
        self.section = {'index': 0, 'halo_start': 0, 'halo_end': 400,
                        'start_m': 0, 'end_m': 400, 'bounds': [461840, 5556240, 462560, 5556560]}
        self.nodes = {1: [0, 0], 2: [200, 0], 3: [400, 0]}

    def test_real_way_ids_tags_and_continuous_synthetic_nodes(self):
        source = osm(self.nodes, [(10, [1, 2], {'name': 'Erste Straße'}), (11, [2, 3], {'name': 'Zweite Straße', 'lanes': '2'})])
        result = section_route(self.route, self.xy, self.section, source)
        self.assertEqual(result['source_way_ids'], [10, 11])
        self.assertEqual([way['id'] for way in result['ways']], [10, 11])
        self.assertEqual(result['ways'][1]['tags']['lanes'], '2')
        self.assertEqual(result['ways'][0]['nodes'][-1], result['ways'][1]['nodes'][0])
        self.assertEqual(result['ways'][0]['route_start_node'], -1)
        lonlat = ordered_road(result['ways'])
        projected = np.array(TO_UTM.transform(lonlat[:, 0], lonlat[:, 1])).T
        np.testing.assert_allclose(projected, self.xy, atol=1e-4)
        self.assertEqual(result['route_match']['node_evidence_edges'], 400)
        self.assertEqual(result['length_m'], 400)
        self.assertNotIn('ways', self.route)

    def test_perpendicular_bridge_above_route_does_not_reject_ground_road(self):
        nodes = {**self.nodes, 4: [180, -100], 5: [180, 100]}
        source = osm(nodes, [(10, [1, 2, 3], {'name': 'Bodenstraße'}),
                             (20, [4, 5], {'name': 'Querbrücke', 'bridge': 'yes', 'layer': '1'})])
        result = section_route(self.route, self.xy, self.section, source)
        self.assertEqual(result['source_way_ids'], [10])

    def test_parallel_bridge_near_route_does_not_override_node_pair_evidence(self):
        nodes = {**self.nodes, 4: [0, .1], 5: [400, .1]}
        # The route is deliberately 15 cm closer to the parallel bridge, but
        # actual adjacent OSM node IDs establish the selected ground road.
        xy = self.xy + [0, .2]
        source = osm(nodes, [(10, [1, 2, 3], {'name': 'Bodenstraße'}),
                             (20, [4, 5], {'bridge': 'yes', 'name': 'Parallelbrücke'})])
        result = section_route(self.route, xy, self.section, source)
        self.assertEqual(result['source_way_ids'], [10])

    def test_selected_bridge_is_rejected_with_global_kilometre_and_name(self):
        source = osm(self.nodes, [(10, [1, 2], {'name': 'Straße'}),
                                  (20, [2, 3], {'name': 'Talbrücke', 'bridge': 'viaduct'})])
        with self.assertRaises(GeoError) as raised:
            section_route(self.route, self.xy, self.section, source)
        self.assertEqual(raised.exception.code, 'unsupported_bridge')
        self.assertIn('km 0.20', str(raised.exception))
        self.assertIn('Talbrücke', str(raised.exception))

    def test_tunnel_subtypes_layers_and_access_are_not_silently_cleared(self):
        for tags, expected in [({'tunnel': 'building_passage'}, 'unsupported_tunnel'),
                               ({'layer': '-1'}, 'unsupported_structure'),
                               ({'covered': 'yes'}, 'unsupported_structure'),
                               ({'motor_vehicle': 'private'}, 'unsupported_access')]:
            source = osm(self.nodes, [(10, [1, 2, 3], tags)])
            with self.subTest(tags=tags), self.assertRaises(GeoError) as raised:
                section_route(self.route, self.xy, self.section, source)
            self.assertEqual(raised.exception.code, expected)

    def test_stale_route_nodes_can_match_unambiguous_current_road_geometry(self):
        source = osm(self.nodes, [(10, [1, 2, 3], {'name': 'Straße'})])
        route = {**self.route, 'routing': {'node_ids': [101, 102, 103]}}
        result = section_route(route, self.xy, self.section, source)
        self.assertEqual(result['source_way_ids'], [10])
        self.assertEqual(result['route_match']['node_evidence_edges'], 0)

    def test_equal_parallel_roads_without_node_evidence_are_ambiguous(self):
        nodes = {1: [0, -1], 2: [400, -1], 3: [0, 1], 4: [400, 1]}
        source = osm(nodes, [(10, [1, 2], {}), (20, [3, 4], {})])
        with self.assertRaises(GeoError) as raised:
            section_route({**self.route, 'routing': {}}, self.xy, self.section, source)
        self.assertEqual(raised.exception.code, 'route_osm_ambiguous')

    def test_incomplete_or_shifted_road_is_not_invented(self):
        source = osm({1: [0, 10], 2: [400, 10]}, [(10, [1, 2], {})])
        with self.assertRaises(GeoError) as raised:
            section_route(self.route, self.xy, self.section, source)
        self.assertEqual(raised.exception.code, 'route_osm_mismatch')
        missing = osm({1: [0, 0]}, [(10, [1, 2], {})])
        with self.assertRaises(GeoError) as raised:
            section_route(self.route, self.xy, self.section, missing)
        self.assertEqual(raised.exception.code, 'osm_invalid')

    def test_halo_slice_keeps_shared_global_node_sequence(self):
        source = osm(self.nodes, [(10, [1, 2, 3], {})])
        section = {**self.section, 'index': 1, 'halo_start': 180, 'halo_end': 350}
        result = section_route(self.route, self.xy, section, source)
        self.assertEqual(result['ways'][0]['nodes'][0], -181)
        self.assertEqual(result['ways'][0]['nodes'][-1], -351)
        self.assertEqual(result['global_start_m'], 180)
        self.assertEqual(result['global_end_m'], 350)
        self.assertEqual(result['length_m'], 170)


if __name__ == '__main__':
    unittest.main()
