"""Match a planned road corridor to local OSM before loading heavy geodata.

Only features actually followed by the route can reject it. A bridge crossing
above the route, or a parallel carriageway nearby, is not the selected road.
"""
from __future__ import annotations

import math

import numpy as np
from shapely import STRtree
from shapely.geometry import LineString, Point

from webapp.geodata import GeoError, TO_UTM, TO_WGS, _wgs_bounds, _xml


MATCH_DISTANCE_M = 4.0
MIN_DIRECTION_COSINE = .65


def _message(tags, chainage, reason):
    name = str(tags.get('name') or tags.get('ref') or 'Straße')
    name = ''.join(character for character in name if ord(character) >= 32)[:70]
    return f'Bei km {chainage / 1000:.2f} ({name}): {reason}'


def _check_tags(tags, chainage):
    inactive = ('no', '0', 'false', '')
    # Preserve bridge/tunnel/covered/layer tags for the global 3-D profile.
    # A layer alone specifies ordering, not a height or construction type.
    if any(tags.get(key) in ('no', 'private', 'agricultural', 'forestry') for key in ('access', 'vehicle', 'motor_vehicle', 'motorcar')):
        raise GeoError(_message(tags, chainage, 'Die OSM-Daten kennzeichnen diese Straße als für Autos gesperrt oder privat.'), 'unsupported_access', 422)
    if tags.get('highway') not in ('motorway', 'motorway_link', 'trunk', 'trunk_link', 'primary', 'primary_link',
                                   'secondary', 'secondary_link', 'tertiary', 'tertiary_link', 'unclassified',
                                   'residential', 'living_street', 'service', 'road'):
        raise GeoError(_message(tags, chainage, 'Der Verlauf stimmt nicht mit einer befahrbaren Straße in den aktuellen OSM-Daten überein.'), 'route_osm_mismatch', 422)


def _roads(content, annotated_edges):
    document = _xml(content)
    if document.tag != 'osm' or document.find('remark') is not None:
        raise GeoError('Der OSM-Ausschnitt für die Straßenprüfung ist unvollständig.', 'osm_incomplete', 502)
    nodes, records, shapes = {}, [], []
    try:
        for element in document.findall('node'):
            lon, lat = float(element.attrib['lon']), float(element.attrib['lat'])
            if not math.isfinite(lon + lat) or not -180 <= lon <= 180 or not -90 < lat < 90:
                raise ValueError()
            nodes[int(element.attrib['id'])] = np.array(TO_UTM.transform(lon, lat))
        for element in document.findall('way'):
            tags = {tag.attrib['k']: tag.attrib['v'] for tag in element.findall('tag')}
            if 'highway' not in tags or tags.get('area') == 'yes':
                continue
            identifier = int(element.attrib['id'])
            sequence = [int(node.attrib['ref']) for node in element.findall('nd')]
            for a, b in zip(sequence, sequence[1:]):
                if a not in nodes or b not in nodes:
                    raise ValueError()
                vector = nodes[b] - nodes[a]
                length = float(np.linalg.norm(vector))
                if length < .01:
                    continue
                shape = LineString([nodes[a], nodes[b]])
                records.append({'way_id': identifier, 'tags': tags, 'a': nodes[a], 'b': nodes[b],
                                'unit': vector / length,
                                'annotated': (min(a, b), max(a, b)) in annotated_edges})
                shapes.append(shape)
    except (KeyError, ValueError, TypeError, OverflowError) as error:
        raise GeoError('Ungültige oder unvollständige OSM-Straßendaten im gewählten Abschnitt.', 'osm_invalid', 502) from error
    if not records:
        raise GeoError('Im gewählten Abschnitt fehlen die OSM-Straßen für eine sichere Prüfung.', 'osm_incomplete', 502)
    return records, shapes, STRtree(shapes)


def section_route(fullroute, xy, section, osmcontent):
    """Return an immutable-input-compatible route for one canonical window.

    Real OSM way IDs and tags identify source roads. Synthetic negative node
    IDs describe the separately documented, resampled canonical road geometry;
    they are never passed off as surveyed OSM node positions.
    """
    xy = np.asarray(xy, dtype=float)
    a, b = int(section['halo_start']), int(section['halo_end'])
    if xy.ndim != 2 or xy.shape[1] != 2 or not np.isfinite(xy).all() or not 0 <= a < b < len(xy):
        raise GeoError('Ungültiger Abschnitt der geplanten Straßenroute.', 'invalid_route_section', 500)
    vectors = np.diff(xy, axis=0)
    lengths = np.linalg.norm(vectors, axis=1)
    if np.any(lengths < .01):
        raise GeoError('Die Straßenroute enthält doppelte Stützpunkte.', 'invalid_route_section', 422)
    distances = np.r_[0., np.cumsum(lengths)]
    annotation = fullroute.get('routing', {}).get('node_ids', [])
    annotated_edges = {(min(left, right), max(left, right)) for left, right in zip(annotation, annotation[1:]) if left != right}
    records, shapes, tree = _roads(osmcontent, annotated_edges)
    selected, errors, evidence = [], [], []
    for index in range(a, b):
        midpoint = (xy[index] + xy[index + 1]) * .5
        unit = vectors[index] / lengths[index]
        point = Point(midpoint)
        candidates = []
        for item in tree.query(point.buffer(MATCH_DISTANCE_M)):
            record = records[int(item)]
            direction = abs(float(np.dot(unit, record['unit'])))
            distance = float(shapes[int(item)].distance(point))
            if direction < MIN_DIRECTION_COSINE or distance > MATCH_DISTANCE_M:
                continue
            # Exact adjacent routing node IDs resolve nearby parallel roads.
            # Distance/orientation provide the explicit fallback for stale IDs.
            score = distance + 3. * (1 - direction)
            candidates.append((not record['annotated'], score, record['way_id'], int(item), distance))
        if not candidates:
            raise GeoError(_message({}, distances[index], 'Die Route weicht von den aktuellen OSM-Straßen ab. Bitte die Route neu planen oder den Verlauf prüfen.'), 'route_osm_mismatch', 422)
        candidates.sort()
        chosen = candidates[0]
        if chosen[0]:
            competing = [candidate for candidate in candidates[1:] if candidate[2] != chosen[2]]
            # At a normal junction, adjacent source ways share an endpoint.
            # Equidistant parallel ways without routing-node evidence remain
            # ambiguous; never quietly select the wrong height or access tags.
            if competing and competing[0][1] - chosen[1] < .35:
                first, second = records[chosen[3]], records[competing[0][3]]
                connected = min(float(np.linalg.norm(left - right)) for left in (first['a'], first['b'])
                                for right in (second['a'], second['b'])) < .05
                parallel=abs(float(np.dot(first['unit'],second['unit'])))>.95
                if not connected and parallel:
                    raise GeoError(_message({}, distances[index], 'Die Route liegt zwischen mehreren Straßen. Die genaue Fahrbahn ist anhand der OSM-Daten nicht eindeutig.'), 'route_osm_ambiguous', 422)
        record = records[chosen[3]]
        _check_tags(record['tags'], distances[index])
        selected.append(record)
        errors.append(chosen[4])
        evidence.append(not chosen[0])
    lon, lat = TO_WGS.transform(xy[a:b + 1, 0], xy[a:b + 1, 1])
    coordinates = [[float(latitude), float(longitude)] for latitude, longitude in zip(lat, lon)]
    groups = []
    for index, record in enumerate(selected):
        if not groups or groups[-1]['id'] != record['way_id']:
            groups.append({'type': 'way', 'id': record['way_id'], 'tags': dict(record['tags']),
                           'nodes': [-a - index - 1], 'geometry': [{'lat': coordinates[index][0], 'lon': coordinates[index][1]}],
                           'geometry_source': 'Canonical route profile, matched to OSM source way'})
        groups[-1]['nodes'].append(-a - index - 2)
        groups[-1]['geometry'].append({'lat': coordinates[index + 1][0], 'lon': coordinates[index + 1][1]})
    groups[0]['route_start_node'] = -a - 1
    bounds = list(section['bounds'])
    return {**fullroute, 'coordinates': coordinates, 'length_m': float(distances[b] - distances[a]),
            'ways': groups, 'source_way_ids': list(dict.fromkeys(group['id'] for group in groups)),
            'bounds_utm': bounds, 'bounds': _wgs_bounds(bounds),
            'section_index': section['index'], 'global_start_m': float(distances[a]), 'global_end_m': float(distances[b]),
            'route_match': {'method': 'OSRM node-pair evidence and oriented local OSM segment proximity',
                            'checked_edges': len(selected), 'node_evidence_edges': sum(evidence),
                            'maximum_offset_m': max(errors), 'geometry': 'Canonical resampled road profile'}}
