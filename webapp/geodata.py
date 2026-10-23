"""Bounded, cached geographic acquisition for explicitly selected Hessian roads.

Only server-configured services receive requests. Browser input consists of a
search string, two coordinates or an opaque saved-route ID, never a URL/query.
Nominatim searches must be explicit submissions, not autocomplete requests.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import heapq
import json
import math
import os
from pathlib import Path
import re
import shutil
import threading
import time
from urllib.parse import urlsplit
import uuid
from xml.etree import ElementTree as ET

import numpy as np
from pyproj import Transformer
import rasterio
import requests

NOMINATIM = 'https://nominatim.openstreetmap.org'
OVERPASS = 'https://overpass-api.de/api/interpreter'
DGM = 'https://inspire-hessen.de/raster/dgm1/ows'
DOP = 'https://inspire-hessen.de/raster/dop20/ows'
DOM = 'https://inspire-hessen.de/raster/dom1/ows'
LOD2 = 'https://inspire-hessen.de/ows/services/org.2.ef07833e-78a6-4c2c-a895-e31de788aac3_wfs'
USER_AGENT = 'RoadToAssetto/0.12 (personal track generator; https://github.com/gottschalkfelix4-source/road-to-assetto)'
WFS = '{http://www.opengis.net/wfs/2.0}'
GML = '{http://www.opengis.net/gml/3.2}'
BU = '{http://inspire.ec.europa.eu/schemas/bu-core3d/4.0}'
MAX_AREA_M2 = 2_000_000
MAX_ROUTE_M = 2000
MIN_ROUTE_M = 120
MAX_SNAP_M = 60
MAX_BUILDINGS = 1000
CACHE_SECONDS = 7 * 86400
TO_UTM = Transformer.from_crs(4326, 25832, always_xy=True)
TO_WGS = Transformer.from_crs(25832, 4326, always_xy=True)
_nom_lock = threading.Lock()
_nom_last = 0.0
_osm_lock = threading.Lock()
_prepare_lock = threading.Lock()


class GeoError(ValueError):
    def __init__(self, message, code='geodata_error', status=400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def _json_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False), encoding='utf-8')
    temporary.replace(path)


def _digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def _xml(content):
    if b'<!DOCTYPE' in content.upper() or b'<!ENTITY' in content.upper():
        raise GeoError('Der Geodatendienst lieferte eine unzulässige XML-Datei.', 'invalid_provider_response', 502)
    try:
        return ET.fromstring(content)
    except ET.ParseError as error:
        raise GeoError('Der Geodatendienst lieferte keine gültigen XML-Daten.', 'invalid_provider_response', 502) from error


def _point(value):
    try:
        lat, lon = (value['lat'], value['lon']) if isinstance(value, dict) else value
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in (lat, lon)):
            raise ValueError()
        lat, lon = float(lat), float(lon)
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise GeoError('Start und Ziel benötigen gültige Breiten- und Längengrade.', 'invalid_coordinates') from error
    if not (math.isfinite(lat) and math.isfinite(lon) and 49.3 <= lat <= 51.7 and 7.7 <= lon <= 10.3):
        raise GeoError('Bitte Start und Ziel innerhalb Hessens auswählen.', 'outside_hessen')
    return lat, lon


def _bounds(xy, padding):
    result = [math.floor(min(p[0] for p in xy)) - padding,
              math.floor(min(p[1] for p in xy)) - padding,
              math.ceil(max(p[0] for p in xy)) + padding,
              math.ceil(max(p[1] for p in xy)) + padding]
    if (result[2] - result[0]) * (result[3] - result[1]) > MAX_AREA_M2:
        raise GeoError('Der Kartenausschnitt ist zu groß. Bitte einen kompakteren Abschnitt auswählen (höchstens 2 km²).', 'area_too_large')
    return result


def _wgs_bounds(bounds):
    points = [TO_WGS.transform(x, y) for x in (bounds[0], bounds[2]) for y in (bounds[1], bounds[3])]
    return [min(p[0] for p in points), min(p[1] for p in points),
            max(p[0] for p in points), max(p[1] for p in points)]


def _contains(outer, inner):
    return outer[0] <= inner[0] and outer[1] <= inner[1] and outer[2] >= inner[2] and outer[3] >= inner[3]


def _hessen(address):
    return isinstance(address, dict) and address.get('country_code') == 'de' and (address.get('ISO3166-2-lvl4') == 'DE-HE' or address.get('state') in ('Hessen', 'Hesse'))


def _public(route):
    return {key: route[key] for key in ('id', 'name', 'length_m', 'coordinates', 'bounds', 'source', 'warnings')}


def _eligible(tags):
    if tags.get('highway') not in ('primary', 'secondary', 'tertiary', 'unclassified', 'residential', 'living_street', 'service'):
        return False
    if tags.get('area') == 'yes' or tags.get('service') in ('driveway', 'parking_aisle', 'emergency_access'):
        return False
    if any(tags.get(key, 'no') not in ('no', '0', 'false', '') for key in ('bridge', 'tunnel')):
        return False
    if tags.get('layer', '0') not in ('0', ''):
        return False
    return not any(tags.get(key) in ('no', 'private', 'agricultural', 'forestry') for key in ('access', 'vehicle', 'motor_vehicle', 'motorcar'))


def plan_route(content, start, end, search_bounds):
    """Snap endpoints onto eligible OSM segments, then use connected Dijkstra."""
    document = _xml(content)
    if document.tag != 'osm' or document.find('remark') is not None:
        raise GeoError('Der OSM-Ausschnitt ist unvollständig. Bitte später erneut versuchen.', 'osm_incomplete', 502)
    nodes, xy, blocked = {}, {}, set()
    for node in document.findall('node'):
        identifier = int(node.attrib['id'])
        lat, lon = float(node.attrib['lat']), float(node.attrib['lon'])
        if not math.isfinite(lat + lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise GeoError('Ungültige OSM-Koordinaten.', 'osm_invalid', 502)
        nodes[identifier] = (lat, lon)
        xy[identifier] = np.array(TO_UTM.transform(lon, lat))
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in node.findall('tag')}
        if tags.get('barrier') in ('bollard', 'block', 'bus_trap', 'cycle_barrier') or tags.get('access') in ('private', 'no'):
            blocked.add(identifier)
    ways, segments = {}, []
    for element in document.findall('way'):
        tags = {tag.attrib['k']: tag.attrib['v'] for tag in element.findall('tag')}
        if not _eligible(tags):
            continue
        way_id = int(element.attrib['id'])
        sequence = [int(node.attrib['ref']) for node in element.findall('nd')]
        ways[way_id] = tags
        for a, b in zip(sequence, sequence[1:]):
            if a not in nodes or b not in nodes or a in blocked or b in blocked or a == b:
                continue
            if any(not (search_bounds[0] <= xy[n][0] <= search_bounds[2] and search_bounds[1] <= xy[n][1] <= search_bounds[3]) for n in (a, b)):
                continue
            if np.linalg.norm(xy[b] - xy[a]) < .05:
                continue
            segments.append((a, b, way_id))
    if not segments:
        raise GeoError('Hier wurden keine geeigneten befahrbaren Straßen gefunden.', 'no_roads')
    snaps = []
    for position in (start, end):
        query = np.array(TO_UTM.transform(position[1], position[0]))
        nearest = None
        for index, (a, b, _) in enumerate(segments):
            vector = xy[b] - xy[a]
            fraction = float(np.clip(np.dot(query - xy[a], vector) / np.dot(vector, vector), 0, 1))
            point = xy[a] + fraction * vector
            distance = float(np.linalg.norm(query - point))
            if nearest is None or distance < nearest[0]:
                nearest = (distance, index, fraction, point)
        if nearest[0] > MAX_SNAP_M:
            raise GeoError(f'Ein gewählter Punkt liegt mehr als {MAX_SNAP_M} m neben einer geeigneten Straße. Bitte näher an die Fahrbahn klicken.', 'snap_too_far')
        snaps.append(nearest)
    splits = {}
    endpoints = []
    for number, (_, index, fraction, point) in enumerate(snaps):
        a, b, _ = segments[index]
        if fraction < 1e-7:
            identifier = a
        elif fraction > 1 - 1e-7:
            identifier = b
        else:
            identifier = -1 - number
            lon, lat = TO_WGS.transform(*point)
            nodes[identifier], xy[identifier] = (lat, lon), point
            splits.setdefault(index, []).append((fraction, identifier))
        endpoints.append(identifier)
    graph = {}
    for index, (a, b, way_id) in enumerate(segments):
        tags = ways[way_id]
        sequence = [(0., a)] + sorted(splits.get(index, [])) + [(1., b)]
        oneway = tags.get('oneway', 'yes' if tags.get('junction') == 'roundabout' else 'no')
        for (_, left), (_, right) in zip(sequence, sequence[1:]):
            weight = float(np.linalg.norm(xy[right] - xy[left]))
            if oneway != '-1':
                graph.setdefault(left, []).append((right, weight, way_id))
            if oneway not in ('yes', '1', 'true'):
                graph.setdefault(right, []).append((left, weight, way_id))
    origin, target = endpoints
    distances, previous, queue = {origin: 0.}, {}, [(0., origin)]
    while queue:
        distance, node = heapq.heappop(queue)
        if distance != distances[node]:
            continue
        if node == target:
            break
        for neighbor, weight, way_id in graph.get(node, []):
            candidate = distance + weight
            if candidate <= MAX_ROUTE_M + .01 and candidate < distances.get(neighbor, math.inf):
                distances[neighbor] = candidate
                previous[neighbor] = (node, way_id)
                heapq.heappush(queue, (candidate, neighbor))
    if target not in distances:
        raise GeoError('Keine zusammenhängende Strecke bis 2 km gefunden. Einbahnstraßen, private Wege, Brücken und Tunnel werden berücksichtigt.', 'route_disconnected')
    if distances[target] < MIN_ROUTE_M:
        raise GeoError(f'Die Strecke muss mindestens {MIN_ROUTE_M} m lang sein.', 'route_too_short')
    path, edge_ways = [target], []
    while path[-1] != origin:
        previous_node, way_id = previous[path[-1]]
        edge_ways.append(way_id)
        path.append(previous_node)
    path.reverse()
    edge_ways.reverse()
    from shapely.geometry import LineString
    if not LineString([xy[node] for node in path]).is_simple:
        raise GeoError('Die gewählte Straßenführung kreuzt sich. Bitte einen einfacheren Abschnitt auswählen.', 'route_self_intersection')
    groups = []
    for index, way_id in enumerate(edge_ways):
        if not groups or groups[-1]['id'] != way_id:
            groups.append({'type': 'way', 'id': way_id, 'nodes': [path[index]], 'geometry': [], 'tags': ways[way_id]})
        groups[-1]['nodes'].append(path[index + 1])
    for group in groups:
        group['geometry'] = [{'lat': nodes[node][0], 'lon': nodes[node][1]} for node in group['nodes']]
    groups[0]['route_start_node'] = origin
    names = list(dict.fromkeys(ways[way_id].get('name', '') for way_id in edge_ways))
    name = ' / '.join(value for value in names if value)[:60] or 'Hessen – ausgewählte Straße'
    return {'name': name, 'length_m': distances[target],
            'coordinates': [[round(nodes[node][0], 8), round(nodes[node][1], 8)] for node in path],
            'ways': groups, 'bounds_utm': _bounds([xy[node] for node in path], 160),
            'source_way_ids': list(dict.fromkeys(edge_ways)),
            'snap_distances_m': [round(snap[0], 1) for snap in snaps]}


class GeoService:
    def __init__(self, data_root):
        self.root = Path(data_root)
        self.routes = self.root / 'routes'
        self.cache = self.root / 'geo-cache'
        self.routes.mkdir(parents=True, exist_ok=True)
        self.cache.mkdir(parents=True, exist_ok=True)
        # Operator configuration only: HTTP callers cannot override providers.
        self.geocoder = os.environ.get('ROADTOASSETTO_GEOCODER_URL', NOMINATIM).rstrip('/')
        self.overpass = os.environ.get('ROADTOASSETTO_OVERPASS_URL', OVERPASS)
        for url in (self.geocoder, self.overpass):
            parsed = urlsplit(url)
            if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise GeoError('Ungültige serverseitige Geodatendienst-Konfiguration.', 'provider_configuration', 503)

    def _request_bytes(self, url, *, params=None, data=None, max_bytes=2_000_000, timeout=75):
        started = time.monotonic()
        try:
            with requests.Session() as session:
                session.headers.update({'User-Agent': USER_AGENT, 'Accept-Language': 'de,en;q=0.7'})
                with session.request('POST' if data is not None else 'GET', url, params=params, data=data,
                                     timeout=(15, min(timeout, 45)), stream=True, allow_redirects=False) as response:
                    if response.status_code in (429, 502, 503, 504):
                        raise GeoError('Der öffentliche Geodatendienst ist gerade ausgelastet. Bitte später erneut versuchen.', 'provider_busy', 503)
                    if response.status_code != 200:
                        raise GeoError('Der Geodatendienst konnte den Ausschnitt nicht liefern.', 'provider_error', 502)
                    try:
                        announced = int(response.headers.get('Content-Length', '0'))
                    except (ValueError, TypeError) as error:
                        raise GeoError('Der Geodatendienst lieferte ungültige Antwort-Metadaten.', 'invalid_provider_response', 502) from error
                    if announced < 0 or announced > max_bytes:
                        raise GeoError('Die Antwort des Geodatendienstes ist zu groß. Bitte einen kleineren Ausschnitt wählen.', 'provider_response_too_large', 502)
                    content, size = [], 0
                    for chunk in response.iter_content(1024 * 1024):
                        size += len(chunk)
                        if size > max_bytes or time.monotonic() - started > timeout:
                            raise GeoError('Der begrenzte Geodaten-Download wurde abgebrochen. Bitte später mit einem kleineren Ausschnitt versuchen.', 'provider_limit', 502)
                        content.append(chunk)
                    return b''.join(content)
        except requests.RequestException as error:
            raise GeoError('Der Geodatendienst ist zurzeit nicht erreichbar. Bereits gespeicherte Daten bleiben verfügbar.', 'provider_unavailable', 503) from error

    def _geocode(self, endpoint, params):
        global _nom_last
        key = hashlib.sha256(json.dumps([self.geocoder, endpoint, params], sort_keys=True).encode()).hexdigest()
        path = self.cache / 'geocoder' / (key + '.json')
        with _nom_lock:
            if path.is_file() and time.time() - path.stat().st_mtime < CACHE_SECONDS:
                return json.loads(path.read_text(encoding='utf-8'))
            delay = 1.05 - (time.monotonic() - _nom_last)
            if delay > 0:
                time.sleep(delay)
            _nom_last = time.monotonic()
            content = self._request_bytes(self.geocoder + '/' + endpoint, params=params, max_bytes=1_000_000, timeout=35)
            try:
                result = json.loads(content)
            except (ValueError, UnicodeError) as error:
                raise GeoError('Die Ortssuche lieferte keine gültige Antwort.', 'invalid_geocoder_response', 502) from error
            _json_write(path, result)
            return result

    def search(self, query):
        if (not isinstance(query, str) or not 3 <= len(query.strip()) <= 160
                or any(ord(character) < 32 for character in query) or '://' in query
                or query.strip().startswith(('/', '\\')) or re.match(r'^[A-Za-z]:[\\/]', query.strip())):
            raise GeoError('Bitte eine Adresse oder einen Ort mit 3 bis 160 Zeichen eingeben.', 'invalid_search')
        result = self._geocode('search', {'q': query.strip(), 'format': 'jsonv2', 'addressdetails': 1,
                                       'countrycodes': 'de', 'viewbox': '7.7,51.7,10.3,49.3', 'bounded': 1, 'limit': 8})
        if not isinstance(result, list):
            raise GeoError('Die Ortssuche lieferte keine gültige Ergebnisliste.', 'invalid_geocoder_response', 502)
        matches = []
        for item in result:
            if not isinstance(item, dict) or not _hessen(item.get('address', {})):
                continue
            try:
                lat, lon = _point({'lat': float(item['lat']), 'lon': float(item['lon'])})
                match = {'id': str(item.get('osm_type', 'place')) + ':' + str(item.get('osm_id', item.get('place_id', ''))),
                         'label': str(item.get('display_name', query))[:250], 'lat': lat, 'lon': lon}
                bounds = [float(value) for value in item.get('boundingbox', [])]
                if len(bounds) == 4 and all(math.isfinite(value) for value in bounds):
                    match['bbox'] = [bounds[2], bounds[0], bounds[3], bounds[1]]
                matches.append(match)
            except (ValueError, TypeError, KeyError):
                continue
        return matches

    def _confirm_hessen(self, point):
        result = self._geocode('reverse', {'lat': round(point[0], 6), 'lon': round(point[1], 6),
                                          'format': 'jsonv2', 'addressdetails': 1, 'zoom': 10})
        if not isinstance(result, dict) or not _hessen(result.get('address', {})):
            raise GeoError('Diese Auswahl liegt außerhalb Hessens oder konnte nicht sicher Hessen zugeordnet werden.', 'outside_hessen')

    def _cached_osm(self, bounds):
        """Find verified coverage without making any provider request."""
        candidates = []
        for metadata in list((self.cache / 'osm').glob('*.json'))[:500]:
            try:
                candidates.append(json.loads(metadata.read_text(encoding='utf-8')))
            except (OSError, ValueError):
                continue
        for saved_path in list(self.routes.glob('*.json'))[:500]:
            try:
                saved = json.loads(saved_path.read_text(encoding='utf-8'))
                candidates.append({'key': saved['context_key'], 'bounds': _wgs_bounds(saved['context_bounds_utm']),
                                   'bounds_utm': saved['context_bounds_utm'], 'sha256': saved['context_sha256']})
            except (OSError, KeyError, ValueError, TypeError):
                continue
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_key, outer = candidate.get('key', ''), candidate.get('bounds', [])
            if (not re.fullmatch(r'[0-9a-f]{64}', str(candidate_key)) or not isinstance(outer, list) or len(outer) != 4
                    or not all(isinstance(value, (int, float)) and math.isfinite(value) for value in outer)
                    or not _contains(outer, bounds)):
                continue
            cached = self.cache / 'osm' / (candidate_key + '.osm')
            if cached.is_file() and time.time() - cached.stat().st_mtime < CACHE_SECONDS:
                content = cached.read_bytes()
                if hashlib.sha256(content).hexdigest() == candidate.get('sha256'):
                    projected = [TO_UTM.transform(lon, lat) for lon in (outer[0], outer[2]) for lat in (outer[1], outer[3])]
                    utm = candidate.get('bounds_utm') or [min(p[0] for p in projected), min(p[1] for p in projected),
                                                        max(p[0] for p in projected), max(p[1] for p in projected)]
                    return candidate_key, content, outer, utm
        return None

    def _osm(self, bounds):
        key = hashlib.sha256(json.dumps([self.overpass, [round(value, 7) for value in bounds]]).encode()).hexdigest()
        path = self.cache / 'osm' / (key + '.osm')
        with _osm_lock:
            if path.is_file() and time.time() - path.stat().st_mtime < CACHE_SECONDS:
                return key, path.read_bytes()
            cached = self._cached_osm(bounds)
            if cached:
                return cached[0], cached[1]
            west, south, east, north = bounds
            bbox = ','.join(f'{value:.7f}' for value in (south, west, north, east))
            query = f'[out:xml][timeout:45][maxsize:33554432];(way({bbox});node["natural"="tree"]({bbox}););(._;>;);out body;'
            content = self._request_bytes(self.overpass, data={'data': query}, max_bytes=32_000_000, timeout=90)
            document = _xml(content)
            if document.tag != 'osm' or document.find('remark') is not None or not document.findall('way'):
                raise GeoError('Der OSM-Ausschnitt ist leer oder unvollständig.', 'osm_incomplete', 502)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix('.tmp')
            temporary.write_bytes(content)
            temporary.replace(path)
            _json_write(path.with_suffix('.json'), {'key': key, 'bounds': bounds,
                        'provider': self.overpass, 'sha256': hashlib.sha256(content).hexdigest()})
            return key, content

    def preview(self, start, end):
        start, end = _point(start), _point(end)
        for saved_path in list(self.routes.glob('*.json'))[:500]:
            if time.time() - saved_path.stat().st_mtime >= CACHE_SECONDS:
                continue
            try:
                saved = json.loads(saved_path.read_text(encoding='utf-8'))
                if saved.get('requested_start') == list(start) and saved.get('requested_end') == list(end):
                    return _public(self.get_route(saved['id']))
            except (OSError, KeyError, ValueError, TypeError):
                continue
        points = [TO_UTM.transform(point[1], point[0]) for point in (start, end)]
        direct = math.dist(*points)
        if direct > MAX_ROUTE_M:
            raise GeoError('Start und Ziel dürfen höchstens 2 km auseinander liegen.', 'route_too_long')
        if direct < 40:
            raise GeoError('Bitte Start und Ziel weiter auseinander setzen.', 'route_too_short')
        minimum_bounds = _bounds(points, 160)
        self._confirm_hessen(start)
        self._confirm_hessen(end)
        route = None
        cached = self._cached_osm(_wgs_bounds(minimum_bounds))
        if cached:
            cached_key, cached_content, cached_wgs_bounds, cached_utm_bounds = cached
            try:
                candidate = plan_route(cached_content, start, end, cached_utm_bounds)
                # A connected path alone is insufficient: its entire 160 m
                # scenery corridor must also lie inside the cached extract.
                if _contains(cached_wgs_bounds, _wgs_bounds(candidate['bounds_utm'])):
                    route = candidate
                    key, content, search_bounds = cached_key, cached_content, cached_utm_bounds
            except GeoError:
                pass
        if route is None:
            search_bounds = _bounds(points, 250)
            key, content = self._osm(_wgs_bounds(search_bounds))
            route = plan_route(content, start, end, search_bounds)
        route.update(id=uuid.uuid4().hex, bounds=_wgs_bounds(route['bounds_utm']),
                     context_key=key, context_bounds_utm=search_bounds, context_sha256=hashlib.sha256(content).hexdigest(),
                     requested_start=list(start), requested_end=list(end), schema_version=1,
                     source='OpenStreetMap contributors (ODbL); HVBG Hessen DGM1/DOP20/LoD2 (dl-de/zero-2-0)',
                     created_at=datetime.now(timezone.utc).isoformat(),
                     warnings=['Straßenverlauf aus OpenStreetMap; keine vermessenen Fahrbahnränder.',
                               'Gebäudeformen und Gelände werden beim Erstellen aus Hessens offenen Geodaten geladen. Fassaden und Grundstücksdetails bleiben angenähert.',
                               'Brücken, Tunnel und private Wege sind ausgeschlossen. Keine Verkehrsregeln oder Fahrberechtigung für die reale Straße ableiten.'])
        _json_write(self.routes / (route['id'] + '.json'), route)
        return _public(route)

    def get_route(self, route_id):
        if not isinstance(route_id, str) or not re.fullmatch(r'[0-9a-f]{32}', route_id):
            raise GeoError('Ungültige Streckenkennung.', 'invalid_route_id')
        path = self.routes / (route_id + '.json')
        if not path.is_file():
            raise GeoError('Die ausgewählte Strecke wurde nicht gefunden. Bitte erneut auf der Karte planen.', 'route_not_found', 404)
        route = json.loads(path.read_text(encoding='utf-8'))
        if route.get('id') != route_id or route.get('schema_version') != 1:
            raise GeoError('Die gespeicherte Strecke ist beschädigt. Bitte erneut planen.', 'invalid_saved_route', 409)
        return route

    def _raster(self, path, bounds, kind):
        url, coverage = {'dgm': (DGM, 'he_dgm1'), 'dop': (DOP, 'he_dop20'), 'dom': (DOM, 'dom1')}[kind]
        axis = ('E', 'N') if kind == 'dom' else ('e', 'n')
        params = [('SERVICE', 'WCS'), ('VERSION', '2.0.1'), ('REQUEST', 'GetCoverage'),
                  ('COVERAGEID', coverage), ('FORMAT', 'image/tiff' if kind == 'dom' else 'GTIFF'),
                  ('SUBSET', f'{axis[0]}({bounds[0]},{bounds[2]})'),
                  ('SUBSET', f'{axis[1]}({bounds[1]},{bounds[3]})')]
        if kind == 'dop':
            params.append(('SCALEFACTOR', '1'))
        if not path.is_file():
            content = self._request_bytes(url, params=params, max_bytes=240_000_000 if kind == 'dop' else 16_000_000, timeout=180)
            if content[:2] not in (b'II', b'MM'):
                raise GeoError('Für diesen Ausschnitt sind keine vollständigen Hessischen Rasterdaten verfügbar.', 'raster_unavailable', 422)
            temporary = path.with_suffix('.download.tif')
            temporary.write_bytes(content)
        else:
            temporary = path
        try:
            with rasterio.open(temporary) as raster:
                resolution = .2 if kind == 'dop' else 1.
                if (raster.crs is None or raster.crs.to_epsg() != 25832 or raster.width <= 0 or raster.height <= 0
                        or raster.width * raster.height > (MAX_AREA_M2 / resolution ** 2) * 1.02
                        or max(abs(value - resolution) for value in raster.res) > .002
                        or max(abs(a - b) for a, b in zip(raster.bounds, bounds)) > 1.01
                        or (raster.count < 3 if kind == 'dop' else raster.count != 1)):
                    raise GeoError('Die Rasterdaten haben eine unerwartete Auflösung, Projektion oder Ausdehnung.', 'raster_invalid', 502)
                for _, window in raster.block_windows(1):
                    pixels = raster.read(window=window, masked=True)
                    if np.ma.getmaskarray(pixels).any() or not np.isfinite(pixels.compressed()).all():
                        raise GeoError('Der Ausschnitt enthält Lücken in Hessens Geodaten. Bitte weiter innerhalb Hessens auswählen.', 'raster_nodata', 422)
                    if kind != 'dop' and (pixels.min() < -100 or pixels.max() > 2000):
                        raise GeoError('Ungültige Geländehöhen im Geodaten-Ausschnitt.', 'raster_invalid', 502)
                record = {'provider': 'HVBG Hessen', 'license': 'dl-de/zero-2-0', 'url': url,
                          'coverage_id': coverage, 'bounds': list(raster.bounds), 'crs': 'EPSG:25832',
                          'resolution_m': list(raster.res), 'width': raster.width, 'height': raster.height,
                          'bytes': temporary.stat().st_size, 'sha256': _digest(temporary),
                          'recorded_at_utc': datetime.now(timezone.utc).isoformat()}
            if temporary != path:
                temporary.replace(path)
            return record
        except rasterio.errors.RasterioError as error:
            raise GeoError('Die Rasterdatei ist beschädigt oder unvollständig.', 'raster_invalid', 502) from error
        finally:
            if temporary != path and temporary.exists():
                temporary.unlink()

    def _lod2(self, path, bounds):
        west, south, east, north = _wgs_bounds(bounds)
        output = ET.Element(WFS + 'FeatureCollection', numberMatched='unknown')
        identifiers, start, pages = set(), 0, 0
        while True:
            count = min(250, MAX_BUILDINGS - start) if start < MAX_BUILDINGS else 1
            params = {'SERVICE': 'WFS', 'VERSION': '2.0.0', 'REQUEST': 'GetFeature', 'TYPENAMES': 'bu-core3d:Building',
                      'SRSNAME': 'urn:ogc:def:crs:EPSG::7423',
                      'BBOX': f'{south},{west},{north},{east},urn:ogc:def:crs:EPSG::4326',
                      'COUNT': count, 'STARTINDEX': start}
            document = _xml(self._request_bytes(LOD2, params=params, max_bytes=32_000_000, timeout=120))
            if document.tag != WFS + 'FeatureCollection':
                raise GeoError('Der Gebäudedienst konnte diesen Ausschnitt nicht liefern.', 'lod2_unavailable', 502)
            members = document.findall(WFS + 'member')
            # This service reports numberReturned=0 even for nonempty pages.
            # Count actual members, verify IDs, and probe after a full last page.
            reported = document.attrib.get('numberMatched', 'unknown')
            if reported.isdigit() and int(reported) > MAX_BUILDINGS:
                raise GeoError('Mehr als 1000 Gebäude im Ausschnitt. Bitte einen kleineren Abschnitt wählen.', 'too_many_buildings')
            if len(members) > count or start + len(members) > MAX_BUILDINGS:
                raise GeoError('Der Gebäudedatenausschnitt ist zu groß oder wurde abgeschnitten.', 'lod2_truncated')
            for member in members:
                building = member.find(BU + 'Building')
                identifier = building.attrib.get(GML + 'id') if building is not None else None
                if not identifier or identifier in identifiers:
                    raise GeoError('Der Gebäudedienst liefert keine zuverlässig vollständigen Seiten.', 'lod2_pagination', 502)
                identifiers.add(identifier)
                output.append(member)
            pages += 1
            start += len(members)
            if len(members) < count:
                if reported.isdigit() and int(reported) > start:
                    raise GeoError('Der Gebäudedienst lieferte weniger Gebäude als angekündigt.', 'lod2_truncated', 502)
                break
        output.set('numberReturned', str(start))
        ET.ElementTree(output).write(path, encoding='utf-8', xml_declaration=True)
        return {'provider': 'HVBG Hessen', 'license': 'dl-de/zero-2-0', 'url': LOD2,
                'building_count': start, 'pages': pages, 'crs': 'EPSG:7423', 'sha256': _digest(path)}

    def prepare_route(self, route_id, destination, progress=None):
        route = self.get_route(route_id)
        destination = Path(destination)
        progress = progress or (lambda stage, percent: None)
        with _prepare_lock:
            cache = self.cache / 'inputs' / route_id
            cache.mkdir(parents=True, exist_ok=True)
            ready = cache / 'ready.json'
            if ready.is_file():
                record = json.loads(ready.read_text(encoding='utf-8'))
                if not all((cache / name).is_file() and _digest(cache / name) == digest for name, digest in record['files'].items()):
                    raise GeoError('Der gespeicherte Geodatencache ist beschädigt. Bitte die Strecke erneut planen.', 'cache_corrupt', 409)
                progress('Gespeicherte Geodaten verwenden', 100)
            else:
                bounds = route['bounds_utm']
                _bounds([(bounds[0], bounds[1]), (bounds[2], bounds[3])], 0)
                progress('OSM-Straßen und Umgebung laden', 10)
                context = self.cache / 'osm' / (route['context_key'] + '.osm')
                existing = route['context_bounds_utm']
                if (not context.is_file() or _digest(context) != route['context_sha256']
                        or bounds[0] < existing[0] or bounds[1] < existing[1] or bounds[2] > existing[2] or bounds[3] > existing[3]):
                    _, content = self._osm(route['bounds'])
                else:
                    content = context.read_bytes()
                (cache / 'area.osm').write_bytes(content)
                _json_write(cache / 'road_ways.json', route['ways'])
                _json_write(cache / 'bounds.json', bounds)
                sources, warnings = {}, list(route['warnings'])
                progress('Amtliches Gelände DGM1 laden', 25)
                sources['dgm1'] = self._raster(cache / 'dgm1.tif', bounds, 'dgm')
                progress('Luftbilder DOP20 mit 20 cm Auflösung laden', 45)
                sources['dop20'] = self._raster(cache / 'ortho.tif', bounds, 'dop')
                progress('Amtliche Gebäude LoD2 laden', 65)
                sources['lod2'] = self._lod2(cache / 'buildings_lod2.gml', bounds)
                if not sources['lod2']['building_count']:
                    warnings.append('Der amtliche LoD2-Ausschnitt enthält keine Gebäude; es werden keine Gebäude erfunden.')
                progress('Optionale Vegetationshöhen DOM1 laden', 85)
                try:
                    sources['dom1'] = self._raster(cache / 'dom1.tif', bounds, 'dom')
                    _json_write(cache / 'dom1_source.json', sources['dom1'])
                except GeoError:
                    warnings.append('DOM1 ist für diesen Lauf nicht verfügbar; Vegetation wird aus Luftbildern angenähert.')
                    for name in ('dom1.tif', 'dom1_source.json'):
                        if (cache / name).is_file():
                            (cache / name).unlink()
                _json_write(cache / 'visual_sources.json', {'buildings': sources['lod2'], 'facades': 'Generic attributed materials; no surveyed local facade photographs'})
                _json_write(cache / 'retrieval.json', {'recorded_at_utc': datetime.now(timezone.utc).isoformat(),
                            'route_id': route_id, 'sources': sources, 'osm': {'provider': 'OpenStreetMap contributors',
                            'license': 'ODbL-1.0', 'url': 'https://www.openstreetmap.org/copyright',
                            'source_way_ids': route['source_way_ids']}, 'warnings': warnings})
                names = ('area.osm', 'road_ways.json', 'bounds.json', 'dgm1.tif', 'ortho.tif', 'buildings_lod2.gml',
                         'dom1.tif', 'dom1_source.json', 'visual_sources.json', 'retrieval.json')
                record = {'route_id': route_id, 'sources': sources, 'warnings': warnings,
                          'files': {name: _digest(cache / name) for name in names if (cache / name).is_file()}}
                _json_write(ready, record)
            destination.mkdir(parents=True, exist_ok=True)
            for name in record['files']:
                shutil.copy2(cache / name, destination / name)
            progress('Geodaten bereit', 100)
            return {'route': _public(route), 'sources': record['sources'], 'warnings': record['warnings']}
