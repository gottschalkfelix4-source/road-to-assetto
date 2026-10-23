"""Cached, user-triggered long-route planning through an OSRM service.

One full-resolution routing request replaces downloading a huge rectangular
OSM extract. Detailed OSM and elevation data still need to be checked and
acquired along the resulting corridor before generating any road meshes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from urllib.parse import urlsplit
import uuid

from pyproj import Transformer
import requests


OSRM = 'https://router.project-osrm.org'
POLICY_URL = 'https://routing.openstreetmap.de/about.html'
ATTRIBUTION = 'Routing: OSRM / FOSSGIS; Kartendaten: © OpenStreetMap-Mitwirkende (ODbL)'
USER_AGENT = 'RoadToAssetto/0.13 (personal track generator; https://github.com/gottschalkfelix4-source/road-to-assetto)'
CACHE_SECONDS = 7 * 86400
MAX_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_GEOMETRY_POINTS = 250_000
MAX_SNAP_M = 60
TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
_lock = threading.Lock()
_last_request = 0.0


class RoutingError(ValueError):
    def __init__(self, message, code='routing_error', status=400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def _point(value):
    try:
        lat, lon = (value['lat'], value['lon']) if isinstance(value, dict) else value
        if not (_number(lat) and _number(lon) and -90 < lat < 90 and -180 <= lon <= 180):
            raise ValueError()
        return float(lat), float(lon)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise RoutingError('Start und Ziel benötigen gültige Koordinaten.', 'invalid_coordinates') from error


def _invalid():
    return RoutingError('Der Routingdienst lieferte keine vollständige gültige Straßenroute.', 'invalid_routing_response', 502)


def parse_route(payload, start, end, *, exclude=()):
    """Validate a provider response and retain unsimplified A-to-B geometry."""
    if not isinstance(payload, dict):
        raise _invalid()
    code = payload.get('code')
    if code == 'NoRoute':
        raise RoutingError('Zwischen diesen Punkten wurde keine befahrbare Route mit den gewählten Routingoptionen gefunden.', 'route_disconnected')
    if code == 'NoSegment':
        raise RoutingError('Ein gewählter Punkt liegt zu weit von einer befahrbaren Straße entfernt.', 'snap_too_far')
    if code in ('InvalidValue', 'InvalidOptions'):
        raise RoutingError('Der konfigurierte Routingdienst unterstützt die eingestellten Routenausschlüsse nicht. Bitte die serverseitige Routing-Konfiguration prüfen.', 'routing_configuration', 503)
    if code != 'Ok':
        raise _invalid()
    routes, waypoints = payload.get('routes'), payload.get('waypoints')
    if not isinstance(routes, list) or not routes or not isinstance(routes[0], dict):
        raise _invalid()
    if not isinstance(waypoints, list) or len(waypoints) != 2:
        raise _invalid()
    route = routes[0]
    geometry = route.get('geometry')
    if not isinstance(geometry, dict) or geometry.get('type') != 'LineString':
        raise _invalid()
    raw = geometry.get('coordinates')
    if not isinstance(raw, list) or not 2 <= len(raw) <= MAX_GEOMETRY_POINTS:
        raise _invalid()
    coordinates, projected = [], []
    for position in raw:
        if not isinstance(position, list) or len(position) != 2:
            raise _invalid()
        try:
            lat, lon = _point((position[1], position[0]))
        except RoutingError as error:
            raise _invalid() from error
        point = TO_UTM.transform(lon, lat)
        if not all(math.isfinite(value) for value in point):
            raise _invalid()
        if projected and math.dist(projected[-1], point) < .001:
            continue
        coordinates.append([lat, lon])
        projected.append(point)
    if len(coordinates) < 2:
        raise _invalid()
    distances = []
    for requested, waypoint, endpoint in zip((start, end), waypoints, (projected[0], projected[-1])):
        if not isinstance(waypoint, dict) or not _number(waypoint.get('distance')):
            raise _invalid()
        if not 0 <= waypoint['distance'] <= MAX_SNAP_M:
            raise RoutingError(f'Ein Punkt liegt mehr als {MAX_SNAP_M} m neben der Straße. Bitte näher an die Fahrbahn setzen.', 'snap_too_far')
        location = waypoint.get('location')
        if not isinstance(location, list) or len(location) != 2:
            raise _invalid()
        try:
            lat, lon = _point((location[1], location[0]))
        except RoutingError as error:
            raise _invalid() from error
        # A dishonest/stale response cannot place the geometry elsewhere while
        # merely reporting a small waypoint snapping distance.
        snapped = TO_UTM.transform(lon, lat)
        requested_xy = TO_UTM.transform(requested[1], requested[0])
        if math.dist(snapped, endpoint) > 2 or math.dist(requested_xy, endpoint) > MAX_SNAP_M + 2:
            raise _invalid()
        distances.append(round(math.dist(requested_xy, endpoint), 2))
    length = sum(math.dist(a, b) for a, b in zip(projected, projected[1:]))
    if length < 120:
        raise RoutingError('Die Strecke muss mindestens 120 m lang sein.', 'route_too_short')
    if not _number(route.get('distance')) or route['distance'] <= 0:
        raise _invalid()
    # OSRM uses geodesic distances; projected UTM length differs slightly, but
    # simplified/truncated geometry or enormous leaps should never pass.
    if abs(length - route['distance']) > max(25, route['distance'] * .03):
        raise _invalid()
    legs = route.get('legs')
    if not isinstance(legs, list) or len(legs) != 1 or not isinstance(legs[0], dict):
        raise _invalid()
    node_ids = legs[0].get('annotation', {}).get('nodes', []) if isinstance(legs[0].get('annotation'), dict) else []
    if (not isinstance(node_ids, list) or not 2 <= len(node_ids) <= MAX_GEOMETRY_POINTS + 2
            or any(not _number(node) or node != int(node) or not 0 < node <= 9_007_199_254_740_991 for node in node_ids)):
        raise _invalid()
    # The public OSRM serializer also emits large IDs in exponent notation.
    # Exact integral doubles remain safe; fractional or lossy IDs do not.
    node_ids = [int(node) for node in node_ids]
    names, classes = [], set()
    steps = legs[0].get('steps')
    if not isinstance(steps, list):
        raise _invalid()
    for step in steps:
        if not isinstance(step, dict):
            raise _invalid()
        name = step.get('name')
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(''.join(c for c in name if ord(c) >= 32).strip())
        intersections = step.get('intersections', [])
        if not isinstance(intersections, list):
            raise _invalid()
        for intersection in intersections:
            if not isinstance(intersection, dict):
                raise _invalid()
            values = intersection.get('classes', [])
            if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
                raise _invalid()
            classes.update(values)
        if step.get('mode') == 'ferry':
            classes.add('ferry')
    if classes.intersection(exclude):
        raise RoutingError('Der Routingdienst konnte die konfigurierten Straßenausschlüsse nicht einhalten. Bitte einen anderen Start oder ein anderes Ziel wählen.', 'routing_exclusion_failed', 422)
    if 'ferry' in classes:
        raise RoutingError('Diese Route enthält eine Fähre. Eine Fährverbindung kann nicht als Fahrbahn exportiert werden.', 'unsupported_ferry', 422)
    name = ' / '.join(names)[:60] or 'Hessen – ausgewählte Route'
    nodes = [-number - 1 for number in range(len(coordinates))]
    # Negative identifiers are deliberately synthetic. Annotation node IDs do
    # not correspond one-to-one to snapped endpoint geometry; never invent that
    # relationship or pretend the routing response includes OSM way tags.
    way = {'type': 'way', 'id': -1, 'nodes': nodes,
           'geometry': [{'lat': lat, 'lon': lon} for lat, lon in coordinates],
           'tags': {'name': name, 'source': 'OSRM full route geometry'},
           'route_start_node': nodes[0]}
    return {'name': name, 'length_m': length, 'coordinates': coordinates, 'ways': [way],
            'bounds_utm': [min(p[0] for p in projected), min(p[1] for p in projected),
                           max(p[0] for p in projected), max(p[1] for p in projected)],
            'source_way_ids': [], 'snap_distances_m': distances,
            'routing': {'provider': 'OSRM', 'attribution': ATTRIBUTION,
                        'policy_url': POLICY_URL, 'exclude': list(exclude), 'node_ids': node_ids,
                        'provider_distance_m': route['distance'], 'classes': sorted(classes),
                        'data_version': str(payload.get('data_version', ''))[:100]},
            'warnings': ['OSRM berechnet eine Autoroute auf OpenStreetMap-Daten. Ohne Betreiberkonfiguration können auch Autobahnen enthalten sein.',
                         'Brücken, Tunnel und Fahrbahneigenschaften werden vor dem Export abschnittsweise an den OSM-Daten geprüft.']}


class RoutingService:
    def __init__(self, data_root):
        self.cache = Path(data_root) / 'geo-cache' / 'routing'
        self.cache.mkdir(parents=True, exist_ok=True)
        self.url = os.environ.get('ROADTOASSETTO_ROUTER_URL', OSRM).rstrip('/')
        self.profile = os.environ.get('ROADTOASSETTO_ROUTER_PROFILE', 'driving')
        excluded = os.environ.get('ROADTOASSETTO_ROUTER_EXCLUDE', '').strip()
        self.exclude = sorted(set(excluded.split(','))) if excluded else []
        parsed = urlsplit(self.url)
        if (parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not re.fullmatch(r'[a-zA-Z0-9_-]{1,40}', self.profile)
                or len(self.exclude) > 8 or any(not re.fullmatch(r'[a-z_]{1,30}', value) for value in self.exclude)):
            raise RoutingError('Ungültige serverseitige Routing-Konfiguration.', 'routing_configuration', 503)

    def _request(self, url, params):
        started = time.monotonic()
        try:
            with requests.Session() as session:
                session.headers.update({'User-Agent': USER_AGENT, 'Accept': 'application/json'})
                with session.get(url, params=params, stream=True, allow_redirects=False, timeout=(15, 45)) as response:
                    if response.status_code in (429, 502, 503, 504):
                        raise RoutingError('Der öffentliche Routingdienst ist gerade ausgelastet. Bitte später erneut versuchen.', 'routing_busy', 503)
                    if response.status_code not in (200, 400):
                        raise RoutingError('Der Routingdienst konnte keine Route liefern.', 'routing_provider_error', 502)
                    try:
                        size = int(response.headers.get('Content-Length', '0'))
                    except (ValueError, TypeError) as error:
                        raise _invalid() from error
                    if not 0 <= size <= MAX_RESPONSE_BYTES:
                        raise RoutingError('Die Routing-Antwort überschreitet das sichere Downloadbudget.', 'routing_response_too_large', 502)
                    chunks, size = [], 0
                    for chunk in response.iter_content(128 * 1024):
                        size += len(chunk)
                        if size > MAX_RESPONSE_BYTES or time.monotonic() - started > 75:
                            raise RoutingError('Der begrenzte Routing-Download wurde abgebrochen. Bitte später erneut versuchen.', 'routing_response_limit', 502)
                        chunks.append(chunk)
                    return b''.join(chunks)
        except requests.RequestException as error:
            raise RoutingError('Der Routingdienst ist zurzeit nicht erreichbar. Gespeicherte Routen bleiben verfügbar.', 'routing_unavailable', 503) from error

    def route(self, start, end):
        global _last_request
        start, end = _point(start), _point(end)
        # Seven decimal digits prevent giant user-supplied numbers in URLs and
        # are substantially finer than the source geometry precision.
        points = ';'.join(f'{point[1]:.7f},{point[0]:.7f}' for point in (start, end))
        params = {'overview': 'full', 'geometries': 'geojson', 'annotations': 'nodes',
                  'steps': 'true', 'alternatives': 'false',
                  'generate_hints': 'false', 'radiuses': f'{MAX_SNAP_M};{MAX_SNAP_M}'}
        if self.exclude:
            params['exclude'] = ','.join(self.exclude)
        key = hashlib.sha256(json.dumps([1, self.url, self.profile, points, params], sort_keys=True).encode()).hexdigest()
        path = self.cache / (key + '.json')
        with _lock:
            content = None
            if path.is_file() and path.stat().st_size <= MAX_RESPONSE_BYTES and time.time() - path.stat().st_mtime < CACHE_SECONDS:
                cached = path.read_bytes()
                try:
                    result = parse_route(json.loads(cached), start, end, exclude=self.exclude)
                    content = cached
                except (ValueError, UnicodeError):
                    pass
            if content is None:
                delay = 1.05 - (time.monotonic() - _last_request)
                if delay > 0:
                    time.sleep(delay)
                _last_request = time.monotonic()
                content = self._request(f'{self.url}/route/v1/{self.profile}/{points}', params)
                try:
                    result = parse_route(json.loads(content), start, end, exclude=self.exclude)
                except (json.JSONDecodeError, UnicodeError) as error:
                    raise _invalid() from error
                temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
                temporary.write_bytes(content)
                temporary.replace(path)
            result['routing']['cache_key'] = key
            result['routing']['response_sha256'] = hashlib.sha256(content).hexdigest()
            return result
