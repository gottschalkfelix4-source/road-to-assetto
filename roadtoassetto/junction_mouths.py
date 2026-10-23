"""Short visual side-road mouths grounded in explicit OSM node connectivity.

These are estimated edge reconstructions, not extra drivable roads. Source
asphalt and node topology determine eligibility; cached terrain triangles
determine height. Main-road collision/alignment is never modified.
"""
from __future__ import annotations

import json
import hashlib
import re
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from pyproj import Transformer
from shapely import constrained_delaunay_triangles
from shapely.geometry import LineString, Point, Polygon, MultiPoint
from shapely.ops import unary_union, substring


def _polygons(geometry):
    if isinstance(geometry, Polygon):
        yield geometry
    elif hasattr(geometry, 'geoms'):
        for part in geometry.geoms:
            yield from _polygons(part)


def _width(tags):
    fallback = 3.5 if tags.get('highway') == 'service' else 5.
    match = re.fullmatch(r'\s*(\d+(?:[.,]\d+)?)\s*(?:m)?\s*', tags.get('width', ''))
    value = float(match.group(1).replace(',', '.')) if match else fallback
    if not 2. <= value <= 9.:
        return fallback, True
    return value, not bool(match)


def _reviewed_outlines(data_dir):
    """Optional source-hash-bound outline observations, never broad defaults."""
    path = data_dir / 'junction_observations.json'
    if not path.exists():
        return {}
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema_version') != 1 or document.get('crs') != 'EPSG:25832':
        raise ValueError('Junction observations require schema 1 in EPSG:25832')
    source = document.get('source', {})
    filename, digest = source.get('file', ''), source.get('sha256', '')
    if (not filename or Path(filename).name != filename
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('Junction outline source needs a local file and SHA256')
    source_path = data_dir / filename
    if not source_path.exists():
        return {}
    with source_path.open('rb') as handle:
        if hashlib.file_digest(handle, 'sha256').hexdigest() != digest:
            return {}
    result = {}
    for item in document.get('observations', []):
        if not item.get('enabled') or item.get('status') != 'observed' or item.get('confidence') not in ('medium', 'high'):
            continue
        coordinates = np.asarray(item.get('geometry', {}).get('coordinates'), dtype=float)
        uncertainty = float(item.get('position_uncertainty_m', float('nan')))
        if (item.get('geometry', {}).get('type') != 'Polygon' or coordinates.ndim != 2
                or coordinates.shape[1] != 2 or len(coordinates) < 4
                or not np.isfinite(coordinates).all() or not np.isfinite(uncertainty)
                or not 0 < uncertainty <= 2.):
            raise ValueError('Invalid reviewed junction outline')
        polygon = Polygon(coordinates)
        if not polygon.is_valid or not 1. < polygon.area < 500.:
            raise ValueError('Reviewed junction must be a small valid local outline')
        key = (str(item['way_id']), str(item['connection_node']))
        if key in result:
            raise ValueError('Duplicate reviewed junction connection')
        result[key] = (polygon, item, source)
    return result


def _buildings(doc, nodes, data_dir, transform):
    result = []
    for way in doc.findall('way'):
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        if not tags.get('building') or tags.get('building') == 'no':
            continue
        coordinates = [nodes[n.get('ref')] for n in way.findall('nd') if n.get('ref') in nodes]
        if len(coordinates) >= 4:
            polygon = Polygon(coordinates)
            if polygon.is_valid and polygon.area > .05:
                result.append(polygon)
    path = data_dir / 'buildings_lod2.gml'
    if path.exists():
        gml = '{http://www.opengis.net/gml/3.2}'
        bu = '{http://inspire.ec.europa.eu/schemas/bu-core3d/4.0}'
        for building in ET.parse(path).getroot().iter(bu + 'Building'):
            clouds = []
            for positions in building.iter(gml + 'posList'):
                if not positions.text:
                    continue
                values = np.fromstring(positions.text, sep=' ')
                if len(values) % 3:
                    continue
                values = values.reshape(-1, 3)
                x, y = transform.transform(values[:, 1], values[:, 0])
                clouds.append(np.c_[x, y])
            if clouds:
                polygon = MultiPoint(np.vstack(clouds)).convex_hull
                if isinstance(polygon, Polygon):
                    result.append(polygon)
    return unary_union(result).buffer(.10)


def connected_mouths(data_dir, xy, half, road_polygon):
    """Return clipped footprints and source records; no proximity-only roads."""
    data_dir = Path(data_dir)
    main = json.loads((data_dir / 'road_ways.json').read_text(encoding='utf-8'))
    main_ids = {str(w['id']) for w in main}
    main_nodes = {str(n) for w in main for n in w['nodes']}
    doc = ET.parse(data_dir / 'area.osm').getroot()
    transform = Transformer.from_crs(4326, 25832, always_xy=True)
    nodes = {n.get('id'): transform.transform(float(n.get('lon')), float(n.get('lat')))
             for n in doc.findall('node')}
    buildings = _buildings(doc, nodes, data_dir, transform)
    reviewed = _reviewed_outlines(data_dir)
    route = LineString(xy)
    half = np.broadcast_to(np.asarray(half, dtype=float), (len(xy),))
    station = np.r_[0., np.cumsum(np.linalg.norm(np.diff(xy, axis=0), axis=1))]
    # .4 m physical shoulders are reserved, including corners and end caps.
    reserved = road_polygon.buffer(.4005, join_style=2)
    outer = road_polygon.buffer(8., join_style=2)
    records, skipped, used = [], [], Polygon()
    for way in doc.findall('way'):
        way_id = way.get('id')
        if way_id in main_ids:
            continue
        ids = [n.get('ref') for n in way.findall('nd')]
        shared = [(i, n) for i, n in enumerate(ids) if n in main_nodes]
        if not shared:
            continue
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        reason = None
        if tags.get('highway') not in ('residential', 'tertiary', 'service', 'unclassified'):
            reason = 'not a supported motor road'
        elif any(tags.get(key) in ('no', 'private') for key in ('access', 'vehicle', 'motor_vehicle')):
            reason = 'restricted motor access'
        elif tags.get('service') in ('driveway', 'parking_aisle'):
            reason = 'private access or parking detail not reconstructed'
        elif tags.get('surface') not in (None, 'asphalt'):
            reason = 'source surface is not asphalt'
        elif tags.get('bridge') == 'yes' or tags.get('tunnel') == 'yes' or tags.get('layer', '0') != '0':
            reason = 'grade-separated road requires a separate height model'
        if reason:
            skipped.append({'way_id': way_id, 'reason': reason})
            continue
        width, estimated = _width(tags)
        for index, shared_id in shared:
            if shared_id not in nodes:
                continue
            connection = Point(nodes[shared_id])
            s = route.project(connection)
            # A real shared source node may lie exactly at the full route end;
            # a distant cropped-out node must not project onto that endpoint.
            if ((s <= .5 and connection.distance(Point(xy[0])) > .5)
                    or (s >= route.length - .5 and connection.distance(Point(xy[-1])) > .5)
                    or connection.distance(route) > max(6.75, float(np.interp(s, station, half)) + 1.)):
                continue
            override = reviewed.get((way_id, shared_id))
            if override is not None:
                outline, observation, source = override
                clipped = outline.intersection(outer).difference(reserved).difference(buildings).difference(used)
                for polygon in _polygons(clipped):
                    if polygon.area < .35 or polygon.distance(reserved) > .002:
                        continue
                    records.append({'polygon': polygon, 'way_id': way_id,
                                    'connection_node': shared_id, 'name': tags.get('name', ''),
                                    'width_m': None, 'width_estimated': True,
                                    'width_method': 'Individually reviewed variable outline; no constant width substituted',
                                    'surface_estimated': 'surface' not in tags,
                                    'alignment_connector_m': 0.,
                                    'source_station_m': round(float(s), 3),
                                    'outline_reviewed': True, 'outline_id': observation.get('id'),
                                    'outline_position_uncertainty_m': observation['position_uncertainty_m'],
                                    'outline_evidence': observation.get('evidence'),
                                    'outline_source': source})
                    used = used.union(polygon)
                continue
            for direction in (-1, 1):
                chain = [nodes[shared_id]]
                j = index + direction
                while 0 <= j < len(ids) and ids[j] in nodes:
                    if ids[j] in main_nodes:
                        break
                    chain.append(nodes[ids[j]])
                    if LineString(chain).length > 24.:
                        break
                    j += direction
                if len(chain) < 2 or LineString(chain).length < .5:
                    continue
                connector = connection.distance(route)
                # A DOP-corrected mainline can no longer pass through its old
                # OSM intersection node (6.54 m locally). Keep explicit source
                # connectivity and reconnect only this shared node to its
                # corrected mainline station, with the displacement disclosed.
                if connector > .05:
                    chain.insert(0, np.asarray(route.interpolate(s).coords)[0])
                center = substring(LineString(chain), 0., 24.)
                # No fans into gardens: remain inside the original connected
                # road corridor; widths are conservative if absent from OSM.
                footprint = center.buffer(width / 2., cap_style=2, join_style=2)
                footprint = footprint.intersection(outer).difference(reserved).difference(buildings).difference(used)
                # Source-aligned polygons detached by a building/other cut are
                # discarded rather than becoming isolated asphalt islands.
                for polygon in _polygons(footprint):
                    if polygon.area < .35 or polygon.distance(reserved) > .002:
                        continue
                    records.append({'polygon': polygon, 'way_id': way_id,
                                    'connection_node': shared_id, 'name': tags.get('name', ''),
                                    'width_m': width, 'width_estimated': estimated,
                                    'surface_estimated': 'surface' not in tags,
                                    'alignment_connector_m': round(float(connector), 3),
                                    'source_station_m': round(float(s), 3)})
                    used = used.union(polygon)
    return records, skipped


def reviewed_junction_reservation(data_dir, xy, road_polygon):
    """World-XY reservation for planning sidewalks before scene generation.

    Only hash-verified, source-node-connected outlines within this selected
    route qualify. Reservation starts at the main asphalt edge, including its
    physical shoulder band, so gutters/kerbs cannot run across the mouth.
    Export separately removes those already-present shoulders and buildings.
    Callers may union this result into both road and side-road reservations.
    """
    records, _ = connected_mouths(data_dir, np.asarray(xy, dtype=float), 3., road_polygon)
    reviewed = _reviewed_outlines(Path(data_dir))
    keys = {(record['way_id'], record['connection_node']) for record in records if record.get('outline_reviewed')}
    return unary_union([reviewed[key][0].intersection(road_polygon.buffer(8., join_style=2)).difference(road_polygon)
                        for key in keys])


def _terrain_parts(polygon, sampler):
    """Intersect the footprint with the exact float32-export terrain faces."""
    from shapely.affinity import affine_transform
    if not all(hasattr(sampler, key) for key in ('triangles', 'index', 'origin', 'height_origin')):
        raise ValueError('Junction mouths require an ExportedTerrainSampler')
    local = affine_transform(polygon, [1., 0., 0., -1., -sampler.origin[0], sampler.origin[1]])
    for index in sorted(sampler.index.query(local).tolist()):
        tri = sampler.triangles[index]
        world = np.c_[tri[:, 0] + sampler.origin[0], sampler.origin[1] - tri[:, 2]]
        intersection = polygon.intersection(Polygon(world))
        for part in _polygons(intersection):
            if part.area > 1e-8:
                # Preserve exact original diagonals while adding half-metre
                # boundary samples for the short blended shoulder transition.
                triangles = constrained_delaunay_triangles(part.segmentize(.5))
                for triangle in triangles.geoms:
                    if triangle.area > 1e-8:
                        yield np.asarray(triangle.exterior.coords)[:3, :2]


def build_junction_mouths(data_dir, scene, material, add_mesh, ac, xy, road_y,
                         half, road_polygon, ground_sampler, road_profile,
                         *, asphalt_material):
    """Append non-colliding asphalt mouths; mainline physics stays untouched.

    All coordinates are UTM32 metres / absolute elevations. ``material`` is
    retained for the generator callback contract; the existing scanned asphalt
    material is reused unchanged. Widths and missing surfacing are annotated.
    """
    xy, road_y = np.asarray(xy, float), np.asarray(road_y, float)
    if (xy.ndim != 2 or xy.shape[1] != 2 or len(xy) < 2 or road_y.shape != (len(xy),)
            or not np.isfinite(xy).all() or not np.isfinite(road_y).all()):
        raise ValueError('Invalid junction route')
    records, skipped = connected_mouths(data_dir, xy, half, road_polygon)
    repeat = np.array([1.25, 1.25])
    path = Path(data_dir) / 'materials/surface_photos/asset_sources.json'
    if path.exists():
        sources = json.loads(path.read_text(encoding='utf-8'))
        found = next((r for r in sources if r.get('asset') == 'Asphalt023S'), None)
        if found:
            repeat = np.asarray(found['dimensions_cm'][:2], dtype=float) / 100.
    if repeat.shape != (2,) or not np.isfinite(repeat).all() or np.any(repeat <= 0):
        raise ValueError('Invalid junction asphalt scale')
    metadata = {'junction_mouths': 0, 'junction_mouth_area_m2': 0.,
                'junction_mouth_max_extension_m': 8., 'junction_mouth_collision': False,
                'junction_mouth_source': 'OSM shared-node topology; source-hash-verified individual DOP20 outlines where reviewed, otherwise original side-road centerlines reconnected to adjusted mainline; widths and hidden edges estimated; 8 m visual mouths only, no added drivable route',
                'junction_mouth_height': 'Exact exported terrain triangles plus 8 mm; first metre after physical shoulder blends to road edge minus 15 mm',
                'junction_mouth_asphalt_repeat_m': repeat.tolist(),
                'junction_mouth_records': [], 'junction_mouth_skipped': skipped,
                'junction_mouth_footprints_utm32': []}
    for record in records:
        vertices, faces, uvs = [], [], []
        area = 0.
        for p in _terrain_parts(record['polygon'], ground_sampler):
            terrain = ground_sampler(p) + .008
            lateral, height, width = road_profile.sample(p)
            edge = height - float(getattr(road_profile, 'crossfall', 0.)) * width - .015
            blend = np.clip((lateral - width - .4) / 1., 0., 1.)
            blend = blend * blend * (3. - 2. * blend)
            elevation = np.maximum(terrain, edge * (1. - blend) + terrain * blend)
            points = ac(p, elevation)
            if np.cross(points[1] - points[0], points[2] - points[0])[1] < 0:
                points, p = points[[0, 2, 1]], p[[0, 2, 1]]
            exported = np.round(points, 5).astype(np.float32).astype(float)
            if np.cross(exported[1] - exported[0], exported[2] - exported[0])[1] <= 1e-7:
                continue
            offset = len(vertices)
            vertices.extend(points)
            faces.append((offset, offset + 1, offset + 2))
            uvs.extend((p - xy[0]) / repeat)
            area += Polygon(p).area
        if faces:
            # Short mouths normally have hundreds, not tens of thousands of
            # vertices; split nevertheless to respect KN5's uint16 index bound.
            for first in range(0, len(faces), 18000):
                last = min(first + 18000, len(faces))
                points = np.asarray(vertices[first * 3:last * 3])
                f = np.arange(len(points)).reshape(-1, 3)
                add_mesh(f'JUNCTION_ASPHALT_{record["way_id"]}_{metadata["junction_mouths"]}_P{first // 18000}',
                         points, f, asphalt_material, np.asarray(uvs[first * 3:last * 3]), False)
            metadata['junction_mouths'] += 1
            metadata['junction_mouth_area_m2'] += area
            metadata['junction_mouth_records'].append({k: v for k, v in record.items() if k != 'polygon'} | {'area_m2': round(area, 3)})
            metadata['junction_mouth_footprints_utm32'].append({
                'exterior': np.asarray(record['polygon'].exterior.coords).round(5).tolist(),
                'holes': [np.asarray(ring.coords).round(5).tolist() for ring in record['polygon'].interiors]})
    metadata['junction_mouth_area_m2'] = round(metadata['junction_mouth_area_m2'], 3)
    metadata['junction_mouth_reviewed_outlines'] = sum(bool(item.get('outline_reviewed')) for item in metadata['junction_mouth_records'])
    return metadata
