"""HTTP map contracts with isolated state and an injected, offline geo service.

The fake supplies provider results, while the real server, request validation,
queue, SQLite persistence and public-response filtering are exercised. No live
geocoder, map service, renderer, user settings or game process is contacted.
"""
from concurrent.futures import ThreadPoolExecutor
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
import uuid

import requests

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.geodata import GeoError, GeoService
from webapp.server import create_server


class OfflineGeoService:
    """Keep route identities separate, including across concurrent requests."""
    PUBLIC = ('id', 'name', 'length_m', 'coordinates', 'bounds', 'source', 'warnings')

    def __init__(self):
        self.lock = threading.Lock()
        self.routes = {}
        self.searches = []
        self.previews = []
        self.failure = None
        self.results = [{'label': 'Teststraße, Hessen', 'lat': 50.158, 'lon': 8.463}]

    def search(self, query):
        with self.lock:
            self.searches.append(query)
        if self.failure:
            raise self.failure
        return deepcopy(self.results)

    def preview(self, start, end):
        if self.failure:
            raise self.failure
        with self.lock:
            self.previews.append((deepcopy(start), deepcopy(end)))
            identifier = uuid.uuid4().hex
            value = {'id': identifier, 'name': 'Teststraße', 'length_m': 320.,
                     'coordinates': [[start['lat'], start['lon']], [end['lat'], end['lon']]],
                     'bounds': [8.46, 50.15, 8.48, 50.17],
                     'source': 'Offline HTTP test fixture', 'warnings': [],
                     'ways': [{'id': 1, 'nodes': [1, 2]}],
                     'bounds_utm': [461000, 5556000, 461500, 5556500],
                     'context_key': 'PRIVATE-CACHE-IDENTIFIER',
                     'private_path': '/never-expose-geo-cache'}
            self.routes[identifier] = value
            return {key: deepcopy(value[key]) for key in self.PUBLIC}

    def get_route(self, identifier):
        if not isinstance(identifier, str):
            raise GeoError('Ungültige Streckenkennung.', code='invalid_route_id')
        with self.lock:
            if identifier not in self.routes:
                raise GeoError('Die Route wurde nicht gefunden.', code='route_not_found', status=404)
            return deepcopy(self.routes[identifier])


class MapHTTPTests(unittest.TestCase):
    def new_geo(self, folder):
        return OfflineGeoService()

    def setUp(self):
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        parent = ROOT / 'test-results'
        parent.mkdir(exist_ok=True)
        self.folder = Path(self.resources.enter_context(
            tempfile.TemporaryDirectory(prefix='map-http-', dir=parent)))
        self.geo = self.new_geo(self.folder)
        self.server = create_server(port=0, data_root=self.folder, start_worker=False,
                                    geo_service=self.geo)
        self.thread = threading.Thread(target=self.server.serve_forever, args=(.03,), daemon=True)
        self.thread.start()
        self.resources.callback(self.close_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_port)

    def close_server(self):
        self.server.shutdown()
        self.thread.join(timeout=5)
        self.server.server_close()
        self.server.manager.close()
        self.assertFalse(self.thread.is_alive())

    def call(self, path, value=None, *, headers=None, raw=None):
        fields = {'Content-Type': 'application/json'}
        fields.update(headers or {})
        body = raw if raw is not None else (None if value is None else json.dumps(value).encode())
        request = Request(self.base + path, data=body, headers=fields)
        try:
            response = build_opener(ProxyHandler({})).open(request, timeout=10)
        except HTTPError as error:
            response = error
        with response:
            data = response.read()
            self.assertNotIn(b'PRIVATE-CACHE-IDENTIFIER', data)
            self.assertNotIn(b'/never-expose-geo-cache', data)
            return response.status, response.headers, json.loads(data)

    def preview(self, *, latitude=50.158):
        request = {'start': {'lat': latitude, 'lon': 8.463},
                   'end': {'lat': latitude, 'lon': 8.4675}}
        status, _, value = self.call('/api/routes/preview', request)
        self.assertEqual(status, 200, value)
        self.assertEqual(set(value), {'route'})
        return value['route']

    def test_explicit_search_is_forwarded_once_without_rewriting_results(self):
        query = 'Teststraße 5, Hessen'
        status, headers, value = self.call('/api/search', {'query': query})
        self.assertEqual(status, 200, value)
        self.assertEqual(value, {'results': self.geo.results})
        self.assertEqual(self.geo.searches, [query])
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_route_fetch_exposes_only_public_fields_and_keeps_original_preview(self):
        route = self.preview()
        self.assertEqual(set(route), set(OfflineGeoService.PUBLIC))
        status, _, fetched = self.call('/api/routes/' + route['id'])
        self.assertEqual(status, 200, fetched)
        self.assertEqual(fetched, {'route': route})
        self.assertEqual(len(self.geo.previews), 1)
        self.assertIs(self.server.manager.geo, self.geo)

    def test_hessen_job_keeps_saved_route_when_another_preview_is_created(self):
        first = self.preview()
        request = {'template_id': 'hessen', 'route_id': first['id'], 'name': 'Freunde A',
                   'road_width_m': 7.5}
        status, _, job = self.call('/api/jobs', request)
        self.assertEqual(status, 202, job)
        self.assertEqual(job['settings']['route_id'], first['id'])
        self.assertEqual(job['settings']['start_m'], 0)
        self.assertEqual(job['settings']['end_m'], first['length_m'])
        self.assertEqual(job['settings']['road_width_m'], 7.5)
        second = self.preview(latitude=50.159)
        self.assertNotEqual(first['id'], second['id'])
        status, _, saved = self.call('/api/jobs/' + job['id'])
        self.assertEqual(status, 200)
        self.assertEqual(saved['settings'], job['settings'])
        self.assertNotIn('coordinates', saved['settings'])
        self.assertNotIn('ways', saved['settings'])

    def test_unknown_or_path_shaped_route_cannot_queue_or_read_private_files(self):
        for identifier in ('a' * 32, '../jobs.sqlite3', '/data/routes/route.json', 'x', ''):
            with self.subTest(identifier=identifier):
                status, _, value = self.call('/api/jobs', {
                    'template_id': 'hessen', 'route_id': identifier})
                self.assertIn(status, (400, 404), value)
        for path in ('/api/routes/' + 'a' * 32, '/api/routes/../jobs.sqlite3',
                     '/api/routes/%2e%2e%2fjobs.sqlite3', '/geo-cache/inputs',
                     '/routes/' + 'a' * 32 + '.json'):
            with self.subTest(path=path):
                self.assertEqual(self.call(path)[0], 404)
        self.assertEqual(self.call('/api/jobs')[2], {'jobs': []})

    def test_hessen_job_rejects_client_geometry_and_invalid_output_parameters(self):
        route = self.preview()
        base = {'template_id': 'hessen', 'route_id': route['id']}
        invalid = [
            {'coordinates': [[50.15, 8.46], [50.16, 8.47]]},
            {'data_dir': '../../outside'}, {'route_id': []},
            {'road_width_m': True}, {'road_width_m': 8.1}, {'road_width_m': 3.9},
            {'end_m': float('nan')}, {'start_m': float('inf')},
            {'vegetation_density': float('nan')}, {'scenery_quality': []},
        ]
        for change in invalid:
            with self.subTest(change=change):
                status, _, value = self.call('/api/jobs', {**base, **change})
                self.assertEqual(status, 400, value)
        self.assertEqual(self.call('/api/jobs')[2], {'jobs': []})

    def test_cross_site_and_rebinding_requests_never_reach_geo_providers(self):
        preview = {'start': {'lat': 50.158, 'lon': 8.463},
                   'end': {'lat': 50.158, 'lon': 8.4675}}
        for path, body in (('/api/search', {'query': 'Teststraße'}),
                           ('/api/routes/preview', preview)):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, body,
                    headers={'Origin': 'https://unrelated.example'})[0], 403)
                self.assertEqual(self.call(path, body,
                    headers={'Host': 'unrelated.example'})[0], 403)
        self.assertEqual(self.geo.searches, [])
        self.assertEqual(self.geo.previews, [])

    def test_payload_types_and_limits_are_rejected_before_provider_calls(self):
        for path in ('/api/search', '/api/routes/preview'):
            with self.subTest(path=path):
                self.assertEqual(self.call(path, raw=b'{}',
                    headers={'Content-Type': 'text/plain'})[0], 415)
                self.assertEqual(self.call(path, raw=b'x' * 16385)[0], 413)
                self.assertEqual(self.call(path, raw=b'{broken JSON')[0], 400)
                self.assertEqual(self.call(path, raw=b'[]')[0], 400)
                self.assertEqual(self.call(path, raw=b'null')[0], 400)
        self.assertEqual(self.geo.searches, [])
        self.assertEqual(self.geo.previews, [])

    def test_provider_error_has_stable_status_code_and_does_not_queue(self):
        self.geo.failure = GeoError('Der Kartendienst ist vorübergehend nicht erreichbar.',
                                    code='provider_unavailable', status=503)
        for path, payload in (('/api/search', {'query': 'Teststraße'}),
                              ('/api/routes/preview', {'start': {'lat': 50.158, 'lon': 8.463},
                                                       'end': {'lat': 50.158, 'lon': 8.4675}})):
            with self.subTest(path=path):
                status, _, value = self.call(path, payload)
                self.assertEqual(status, 503, value)
                self.assertEqual(value, {'error': self.geo.failure.message,
                                         'code': 'provider_unavailable'})
        self.assertEqual(self.call('/api/jobs')[2], {'jobs': []})

    def test_concurrent_friends_keep_their_own_preview_and_queued_route(self):
        def friend(latitude):
            route = self.preview(latitude=latitude)
            status, _, job = self.call('/api/jobs', {
                'template_id': 'hessen', 'route_id': route['id'], 'name': str(latitude)})
            self.assertEqual(status, 202, job)
            return route, job
        with ThreadPoolExecutor(max_workers=2) as pool:
            pairs = list(pool.map(friend, [50.158, 50.159]))
        self.assertEqual(len({route['id'] for route, _ in pairs}), 2)
        self.assertEqual(len({job['id'] for _, job in pairs}), 2)
        for route, job in pairs:
            self.assertEqual(job['settings']['route_id'], route['id'])
            self.assertEqual(self.call('/api/routes/' + route['id'])[2], {'route': route})


class RealGeoHTTPTests(unittest.TestCase):
    """Real parsing, route calculation, saved routes and caches; fake transport."""
    setUp = MapHTTPTests.setUp
    close_server = MapHTTPTests.close_server
    call = MapHTTPTests.call
    preview = MapHTTPTests.preview

    def new_geo(self, folder):
        service = GeoService(folder)
        self.network = self.resources.enter_context(patch.object(
            service, '_request_bytes', side_effect=self.response))
        return service

    def response(self, url, *, params=None, data=None, **limits):
        if url == self.geo.geocoder + '/search':
            return json.dumps([
                {'osm_type': 'way', 'osm_id': 100, 'lat': '50.158', 'lon': '8.463',
                 'display_name': 'Teststraße, Hessen',
                 'address': {'country_code': 'de', 'ISO3166-2-lvl4': 'DE-HE'},
                 'boundingbox': ['50.157', '50.159', '8.462', '8.468']},
                {'lat': '50.158', 'lon': '8.463', 'display_name': 'Anderes Bundesland',
                 'address': {'country_code': 'de', 'state': 'Bayern'}}]).encode()
        if url == self.geo.geocoder + '/reverse':
            return json.dumps({'address': {'country_code': 'de', 'state': 'Hessen'}}).encode()
        if url == self.geo.overpass:
            nodes = ''.join(f'<node id="{index}" lat="50.158" lon="{longitude}"/>'
                            for index, longitude in enumerate([8.463, 8.464, 8.465, 8.466, 8.4675], 1))
            refs = ''.join(f'<nd ref="{index}"/>' for index in range(1, 6))
            return (f'<osm version="0.6">{nodes}<way id="100">{refs}'
                    '<tag k="name" v="Teststraße"/><tag k="highway" v="residential"/>'
                    '<tag k="width" v="5.5"/></way></osm>').encode()
        raise AssertionError('Unexpected provider request in an offline test')

    def test_search_is_hessen_filtered_and_repeated_query_uses_disk_cache(self):
        query = {'query': 'Teststraße, Hessen'}
        status, _, result = self.call('/api/search', query)
        self.assertEqual(status, 200, result)
        self.assertEqual(len(result['results']), 1)
        self.assertEqual(result['results'][0]['label'], 'Teststraße, Hessen')
        self.assertEqual(result['results'][0]['bbox'], [8.462, 50.157, 8.468, 50.159])
        self.assertEqual(self.call('/api/search', query)[2], result)
        self.assertEqual(self.network.call_count, 1)
        restored = GeoService(self.folder)
        with patch.object(restored, '_request_bytes', side_effect=AssertionError('Cache miss after restart')):
            self.assertEqual(restored.search(query['query']), result['results'])

    def test_real_route_survives_service_reload_and_can_queue_without_network(self):
        route = self.preview()
        self.assertGreater(route['length_m'], 300)
        self.assertLess(route['length_m'], 340)
        self.assertEqual(len(route['coordinates']), 5)
        before = self.network.call_count
        restored = GeoService(self.folder)
        with patch.object(restored, '_request_bytes', side_effect=AssertionError('Saved route needs network')):
            saved = restored.get_route(route['id'])
            self.assertEqual(saved['coordinates'], route['coordinates'])
            self.assertEqual(saved['ways'][0]['route_start_node'], 1)
        status, _, job = self.call('/api/jobs', {'template_id': 'hessen', 'route_id': route['id']})
        self.assertEqual(status, 202, job)
        self.assertEqual(job['settings']['route_id'], route['id'])
        self.assertEqual(self.call('/api/routes/' + route['id'])[2], {'route': route})
        self.assertEqual(self.network.call_count, before)

    def test_invalid_coordinates_and_searches_fail_before_any_network(self):
        valid = {'lat': 50.158, 'lon': 8.463}
        invalid = [None, True, {}, [], [50.15], {'lat': True, 'lon': 8.463},
                   {'lat': float('nan'), 'lon': 8.463}, {'lat': 50.158, 'lon': float('inf')},
                   {'lat': 40, 'lon': 8.463}, {'lat': 50.158, 'lon': 20},
                   {'lat': {}, 'lon': 8.463}]
        for point in invalid:
            with self.subTest(point=point):
                status, _, result = self.call('/api/routes/preview', {'start': point, 'end': valid})
                self.assertEqual(status, 400, result)
        for query in (None, True, [], {}, '', 'ab', 'x' * 161, 'x\ny'):
            with self.subTest(query=query):
                self.assertEqual(self.call('/api/search', {'query': query})[0], 400)
        self.network.assert_not_called()

    def test_too_short_or_too_long_endpoints_do_not_start_provider_download(self):
        for endpoint in ({'lat': 50.158, 'lon': 8.46301}, {'lat': 50.158, 'lon': 8.51}):
            with self.subTest(endpoint=endpoint):
                status, _, result = self.call('/api/routes/preview', {
                    'start': {'lat': 50.158, 'lon': 8.463}, 'end': endpoint})
                self.assertEqual(status, 400, result)
        self.network.assert_not_called()

    def test_private_roads_never_become_a_queued_route(self):
        original = self.response
        def blocked(url, **kwargs):
            content = original(url, **kwargs)
            return content.replace(b'</way>', b'<tag k="access" v="private"/></way>') if url == self.geo.overpass else content
        self.network.side_effect = blocked
        status, _, result = self.call('/api/routes/preview', {
            'start': {'lat': 50.158, 'lon': 8.463}, 'end': {'lat': 50.158, 'lon': 8.4675}})
        self.assertEqual(status, 400, result)
        self.assertEqual(result['code'], 'no_roads')
        self.assertEqual(list((self.folder / 'routes').glob('*.json')), [])
        self.assertEqual(self.call('/api/jobs')[2], {'jobs': []})

    def test_malformed_nested_provider_addresses_do_not_disconnect_http(self):
        def malformed(url, **kwargs):
            if url.endswith('/search'):
                return json.dumps([{'lat': '50.158', 'lon': '8.463', 'address': None},
                                   {'lat': '50.158', 'lon': '8.463', 'address': []}]).encode()
            if url.endswith('/reverse'):
                return json.dumps({'address': None}).encode()
            raise AssertionError('Invalid provider response must stop before OSM download')
        self.network.side_effect = malformed
        status, _, result = self.call('/api/search', {'query': 'Malformed fixture'})
        self.assertEqual(status, 200, result)
        self.assertEqual(result, {'results': []})
        status, _, result = self.call('/api/routes/preview', {
            'start': {'lat': 50.158, 'lon': 8.463}, 'end': {'lat': 50.158, 'lon': 8.4675}})
        self.assertEqual(status, 400, result)
        self.assertEqual(result['code'], 'outside_hessen')

    def test_transport_failure_redacts_raw_provider_diagnostics(self):
        diagnostic = 'PRIVATE-TRANSPORT-DETAIL /local/cache/private upstream-token=test-fixture-only'
        self.network.side_effect = lambda url, **kwargs: GeoService._request_bytes(self.geo, url, **kwargs)
        with patch('webapp.geodata.requests.Session.request',
                   side_effect=requests.ConnectionError(diagnostic)):
            status, _, result = self.call('/api/search', {'query': 'Provider failure fixture'})
        self.assertEqual(status, 503, result)
        self.assertEqual(result['code'], 'provider_unavailable')
        self.assertNotIn('PRIVATE-TRANSPORT-DETAIL', json.dumps(result))
        self.assertNotIn('upstream-token', json.dumps(result))
        self.assertEqual(self.call('/api/jobs')[2], {'jobs': []})


if __name__ == '__main__':
    unittest.main()
