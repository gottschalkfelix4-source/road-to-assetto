"""Offline regression tests for long-route OSRM acquisition and validation."""
from copy import deepcopy
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from pyproj import Transformer

from webapp.routing import RoutingError, RoutingService, TO_UTM, parse_route


TO_WGS = Transformer.from_crs(25832, 4326, always_xy=True)


def position(x, y=0):
    lon, lat = TO_WGS.transform(462000 + x, 5556400 + y)
    return [lat, lon]


def fixture(length=55_000):
    coordinates = [position(x, 150 * math.sin(x / 5000)) for x in range(0, length + 1, 100)]
    projected = [TO_UTM.transform(lon, lat) for lat, lon in coordinates]
    distance = sum(math.dist(a, b) for a, b in zip(projected, projected[1:]))
    return {'code': 'Ok', 'data_version': '2026-10-23T00:00:00Z',
            'waypoints': [{'location': list(reversed(point)), 'distance': 0} for point in (coordinates[0], coordinates[-1])],
            'routes': [{'distance': distance,
                        'geometry': {'type': 'LineString', 'coordinates': [list(reversed(point)) for point in coordinates]},
                        'legs': [{'annotation': {'nodes': list(range(1, len(coordinates) + 1))},
                                  'steps': [{'name': 'Landstraße', 'mode': 'driving',
                                             'intersections': [{'classes': []}]}]}]}]}


class LongRouteTests(unittest.TestCase):
    def setUp(self):
        self.payload = fixture()
        self.start, self.end = [list(reversed(waypoint['location'])) for waypoint in self.payload['waypoints']]

    def test_more_than_50km_retains_full_geometry_and_projected_length(self):
        result = parse_route(self.payload, self.start, self.end)
        self.assertGreater(result['length_m'], 55_000)
        self.assertEqual(len(result['coordinates']), 551)
        self.assertEqual(result['coordinates'][0], self.start)
        self.assertEqual(result['coordinates'][-1], self.end)
        self.assertAlmostEqual(result['length_m'], self.payload['routes'][0]['distance'], places=7)
        self.assertEqual(result['ways'][0]['route_start_node'], -1)
        self.assertEqual(result['ways'][0]['nodes'][-1], -551)
        self.assertNotIn('highway', result['ways'][0]['tags'])
        self.assertEqual(result['source_way_ids'], [])
        self.assertEqual(result['routing']['node_ids'][0], 1)

    def test_preserves_reverse_direction(self):
        payload = deepcopy(self.payload)
        payload['waypoints'].reverse()
        payload['routes'][0]['geometry']['coordinates'].reverse()
        payload['routes'][0]['legs'][0]['annotation']['nodes'].reverse()
        result = parse_route(payload, self.end, self.start)
        self.assertEqual(result['coordinates'][0], self.end)
        self.assertEqual(result['coordinates'][-1], self.start)

    def test_does_not_accept_fabricated_snap_or_unrelated_geometry(self):
        payload = deepcopy(self.payload)
        payload['waypoints'][0]['distance'] = 100
        with self.assertRaisesRegex(RoutingError, '60 m'):
            parse_route(payload, self.start, self.end)
        payload['waypoints'][0]['distance'] = 0
        with self.assertRaises(RoutingError):
            parse_route(payload, position(-500), self.end)
        payload['waypoints'][0]['location'][0] += .1
        with self.assertRaises(RoutingError):
            parse_route(payload, self.start, self.end)

    def test_rejects_simplified_or_truncated_geometry(self):
        payload = deepcopy(self.payload)
        payload['routes'][0]['distance'] *= 2
        with self.assertRaises(RoutingError):
            parse_route(payload, self.start, self.end)

    def test_rejects_bad_geometry_numbers_shapes_and_annotations(self):
        for value in (float('nan'), float('inf'), True, '8.4', None):
            payload = deepcopy(self.payload)
            payload['routes'][0]['geometry']['coordinates'][4][0] = value
            with self.subTest(value=value), self.assertRaises(RoutingError):
                parse_route(payload, self.start, self.end)
        for node_ids in (None, [], [1, True], [1, 'x'], [0, 1], [1, 2.5], [1, float('inf')], [1, 2**54]):
            payload = deepcopy(self.payload)
            payload['routes'][0]['legs'][0]['annotation']['nodes'] = node_ids
            with self.subTest(nodes=node_ids), self.assertRaises(RoutingError):
                parse_route(payload, self.start, self.end)

    def test_integral_exponent_serialized_osm_ids_are_preserved(self):
        payload = deepcopy(self.payload)
        payload['routes'][0]['legs'][0]['annotation']['nodes'] = [12491945710.0, 12491945711.0]
        result = parse_route(payload, self.start, self.end)
        self.assertEqual(result['routing']['node_ids'], [12491945710, 12491945711])
        self.assertIsInstance(result['routing']['node_ids'][0], int)

    def test_ferry_and_failed_motorway_exclusion_are_explicit(self):
        for kind, code in [('ferry', 'unsupported_ferry'), ('motorway', 'routing_exclusion_failed')]:
            payload = deepcopy(self.payload)
            payload['routes'][0]['legs'][0]['steps'][0]['intersections'][0]['classes'] = [kind]
            with self.subTest(kind=kind), self.assertRaises(RoutingError) as raised:
                parse_route(payload, self.start, self.end, exclude=['motorway'])
            self.assertEqual(raised.exception.code, code)

    def test_default_driving_returns_classes_without_false_exclusion_claim(self):
        payload = deepcopy(self.payload)
        payload['routes'][0]['legs'][0]['steps'][0]['intersections'][0]['classes'] = ['motorway', 'tunnel']
        result = parse_route(payload, self.start, self.end)
        self.assertEqual(result['routing']['classes'], ['motorway', 'tunnel'])
        self.assertEqual(result['routing']['exclude'], [])

    def test_errors_are_provider_safe(self):
        for code, expected in [('NoRoute', 'route_disconnected'), ('NoSegment', 'snap_too_far'), ('InvalidValue', 'routing_configuration')]:
            with self.subTest(code=code), self.assertRaises(RoutingError) as raised:
                parse_route({'code': code, 'message': 'private /local/path secret'}, self.start, self.end)
            self.assertEqual(raised.exception.code, expected)
            self.assertNotIn('secret', str(raised.exception))


class AcquisitionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {'ROADTOASSETTO_ROUTER_URL': 'https://router.example', 'ROADTOASSETTO_ROUTER_PROFILE': 'driving', 'ROADTOASSETTO_ROUTER_EXCLUDE': ''})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.service = RoutingService(self.root)
        self.payload = fixture()
        self.start, self.end = [list(reversed(waypoint['location'])) for waypoint in self.payload['waypoints']]

    def test_full_cached_route_avoids_second_provider_request(self):
        with patch.object(self.service, '_request', return_value=json.dumps(self.payload).encode()) as request:
            first = self.service.route(self.start, self.end)
            second = self.service.route(self.start, self.end)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(first, second)
        url, params = request.call_args.args
        self.assertTrue(url.startswith('https://router.example/route/v1/driving/'))
        self.assertNotIn('exclude', params)
        self.assertEqual(params['overview'], 'full')
        self.assertEqual(params['annotations'], 'nodes')
        self.assertEqual(len(first['routing']['response_sha256']), 64)
        self.assertTrue((self.service.cache / (first['routing']['cache_key'] + '.json')).is_file())

    def test_corrupt_cache_is_refetched_and_provider_error_is_not_cached(self):
        with patch.object(self.service, '_request', return_value=json.dumps(self.payload).encode()) as request:
            result = self.service.route(self.start, self.end)
            path = self.service.cache / (result['routing']['cache_key'] + '.json')
            path.write_text('{not JSON')
            self.service.route(self.start, self.end)
        self.assertEqual(request.call_count, 2)
        path.unlink()
        with patch.object(self.service, '_request', return_value=b'{"code":"NoRoute"}'):
            with self.assertRaises(RoutingError):
                self.service.route(self.start, self.end)
        self.assertFalse(path.exists())

    def test_invalid_inputs_do_not_call_provider(self):
        for invalid in ([True, 8.4], [float('nan'), 8.4], {'lat': 50}, 'http://evil', [90, 8.4]):
            with self.subTest(invalid=invalid), patch.object(self.service, '_request') as request:
                with self.assertRaises(RoutingError):
                    self.service.route(invalid, self.end)
                request.assert_not_called()

    def test_provider_endpoint_only_from_valid_operator_configuration(self):
        for url in ('file:///etc/passwd', 'https://name:secret@example.com', 'https://example.com?q=x', 'https://example.com#frag'):
            with self.subTest(url=url), patch.dict(os.environ, {'ROADTOASSETTO_ROUTER_URL': url}):
                with self.assertRaises(RoutingError):
                    RoutingService(self.root)
        with patch.dict(os.environ, {'ROADTOASSETTO_ROUTER_PROFILE': '../driving?x=1'}):
            with self.assertRaises(RoutingError):
                RoutingService(self.root)

    def test_operator_exclusion_is_sent_without_silent_fallback(self):
        with patch.dict(os.environ, {'ROADTOASSETTO_ROUTER_EXCLUDE': 'motorway'}):
            service = RoutingService(self.root)
        with patch.object(service, '_request', return_value=b'{"code":"InvalidValue"}') as request:
            with self.assertRaises(RoutingError) as raised:
                service.route(self.start, self.end)
        self.assertEqual(raised.exception.code, 'routing_configuration')
        request.assert_called_once()
        self.assertEqual(request.call_args.args[1]['exclude'], 'motorway')

    def test_provider_requests_are_serialized_and_rate_limited(self):
        with patch.object(self.service, '_request', return_value=json.dumps(self.payload).encode()), \
                patch('webapp.routing._last_request', 100.0), \
                patch('webapp.routing.time.monotonic', return_value=100.1), \
                patch('webapp.routing.time.sleep') as sleep:
            self.service.route(self.start, self.end)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], .95)


if __name__ == '__main__':
    unittest.main()
