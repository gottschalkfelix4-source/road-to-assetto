"""Observed white road markings, clipped onto exact exported road triangles.

There is no country-wide or route-wide line generator here. Each feature must
have a reviewable world-coordinate observation tied to a specific open image.
Missing or changed source data produces no guessed markings.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
from PIL import Image
from shapely.geometry import LineString, Polygon

from roadtoassetto.road_wear import ExportedRoad
from roadtoassetto.scenery import Batches


def load_observations(data_dir, observation_file=None):
    """Read reviewed EPSG:25832 features only when their source hash matches."""
    data_dir = Path(data_dir)
    path = Path(observation_file) if observation_file else data_dir / 'marking_observations.json'
    if not path.exists():
        return None, 'no_observations'
    document = json.loads(path.read_text(encoding='utf-8'))
    if document.get('schema_version') != 1 or document.get('crs') != 'EPSG:25832':
        raise ValueError('Road marking observations require schema 1 in EPSG:25832')
    source = document.get('source', {})
    filename, digest = source.get('file', ''), source.get('sha256', '')
    if (not filename or Path(filename).name != filename
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('Road marking source needs a local filename and SHA256')
    source_path = data_dir / filename
    if not source_path.exists():
        return None, 'source_missing'
    with source_path.open('rb') as handle:
        actual = hashlib.file_digest(handle, 'sha256').hexdigest()
    if actual != digest:
        return None, 'source_changed_review_required'
    if not isinstance(document.get('observations'), list):
        raise ValueError('Road marking observations must be a list')
    return document, 'verified_source'


def observation_polygon(item):
    """Validate explicitly reviewed geometry; unsupported guesses stay absent."""
    if not item.get('enabled') or item.get('status') != 'observed':
        return None
    if item.get('confidence') not in ('medium', 'high'):
        return None
    if not re.fullmatch(r'[a-z0-9_]{1,80}', item.get('id', '')):
        raise ValueError('Invalid road marking observation ID')
    uncertainty = float(item.get('position_uncertainty_m', float('nan')))
    if not np.isfinite(uncertainty) or not 0 < uncertainty <= 2.:
        raise ValueError('Road marking needs a finite position uncertainty')
    surfaces = item.get('surfaces', ['main'])
    if not isinstance(surfaces, list) or not surfaces or not set(surfaces) <= {'main', 'junction'}:
        raise ValueError('Invalid road marking surface scope')
    geometry = item.get('geometry', {})
    points = np.asarray(geometry.get('coordinates'), dtype=float)
    if (points.ndim != 2 or points.shape[1] != 2 or len(points) < 2
            or len(points) > 2000 or not np.isfinite(points).all()):
        raise ValueError('Invalid road marking coordinates')
    if geometry.get('type') == 'LineString':
        width = float(item.get('paint_width_m', 0.))
        if not np.isfinite(width) or not .06 <= width <= .6:
            raise ValueError('Invalid observed paint stroke width')
        line = LineString(points)
        if line.length <= .05 or line.length > 40.:
            raise ValueError('Observed paint line must be local, not a generated route-wide marking')
        polygon = line.buffer(width / 2., cap_style=2, join_style=2)
    elif geometry.get('type') == 'Polygon' and len(points) >= 3:
        polygon = Polygon(points)
    else:
        raise ValueError('Unsupported road marking geometry')
    if not polygon.is_valid or polygon.is_empty or not 0 < polygon.area <= 40.:
        raise ValueError('Invalid observed marking footprint')
    return polygon


def marking_texture(textures):
    """Create original fine paint grain; no photographic asset is edited."""
    textures = Path(textures)
    textures.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(882741)
    size = 512
    grain = rng.normal(0., 3., (size, size))
    pores = rng.random((size, size)) < .025
    value = 218. + grain - pores * rng.uniform(9., 24., (size, size))
    pixels = np.clip(value[..., None] + np.array([2., 1., -2.]), 0, 255).astype('uint8')
    Image.fromarray(pixels).save(textures / 'observed_road_paint.png')


def _surface(scene, ac, scope):
    selected = []
    for mesh in scene.get('meshes', []):
        name = mesh.get('name', '')
        if (('main' in scope and name == '1ROAD_MAIN')
                or ('junction' in scope and name.startswith('JUNCTION_ASPHALT_'))):
            # ExportedRoad already implements exact surface clipping, crown
            # heights and the writer's sliver rejection. Rename a shallow
            # view, without modifying either original geometry or material.
            selected.append(dict(mesh, name='1ROAD_MAIN'))
    return ExportedRoad({'meshes': selected}, ac) if selected else None


def build_road_markings(data_dir, textures, scene, material, add_mesh, ac,
                        *, observation_file=None):
    """Append observed, non-colliding paint; crop builds use world coordinates.

    Call after main road and optional junction mouths exist. Missing observations
    are supported and produce no synthetic defaults. Paint lies 1.8 mm above
    the chosen asphalt mesh, using that exact mesh's triangles and heights.
    """
    document, status = load_observations(data_dir, observation_file)
    stats = {'road_markings': 0, 'road_marking_area_m2': 0.,
             'road_marking_status': status, 'road_marking_max_lift_mm': 1.8,
             'road_marking_collision': False, 'road_marking_records': [],
             'road_marking_skipped': [],
             'road_marking_method': 'Individually reviewed DOP20 white paint footprints, exact asphalt-triangle clipping; no generated centerline or zebra crossing'}
    if document is None:
        return stats
    stats['road_marking_source'] = document['source']
    stats['road_marking_reviewed_on'] = document.get('reviewed_on')
    stats['road_marking_not_reconstructed'] = document.get('not_reconstructed', [])
    accepted, identifiers = [], set()
    for item in document['observations']:
        polygon = observation_polygon(item)
        if polygon is None:
            stats['road_marking_skipped'].append({'id': item.get('id'), 'reason': 'not an enabled medium/high-confidence observation'})
            continue
        if item['id'] in identifiers:
            raise ValueError('Duplicate road marking observation ID')
        identifiers.add(item['id'])
        accepted.append((item, polygon))
    if not accepted:
        return stats
    marking_texture(textures)
    paint = material('observed_white_road_paint', 'observed_road_paint.png',
                     ambient=.43, diffuse=.65, specular=.07)
    scene['materials'][paint].update(roughness=.86, specular_exp=14.)
    stats['road_marking_paint_material'] = 'Original seeded fine paint grain, warm white, no edited source images'
    surfaces, batches = {}, Batches(add_mesh)
    for item, polygon in accepted:
        scope = tuple(sorted(set(item.get('surfaces', ['main']))))
        if scope not in surfaces:
            surfaces[scope] = _surface(scene, ac, scope)
        target = surfaces[scope]
        if target is None:
            stats['road_marking_skipped'].append({'id': item['id'], 'reason': 'selected surface is absent in this route'})
            continue
        # Small local UVs survive float32; 1 m repeats keep grain at its
        # declared physical scale instead of shifting million-metre UTM UVs.
        origin = np.asarray(polygon.centroid.coords)[0]
        area = target.append(polygon, 'OBSERVED_MARKING_' + item['id'], paint, batches,
                             lambda points, o=origin: points - o, .0018)
        if area <= 1e-7:
            stats['road_marking_skipped'].append({'id': item['id'], 'reason': 'outside exported asphalt surfaces for this route'})
            continue
        stats['road_markings'] += 1
        stats['road_marking_area_m2'] += area
        stats['road_marking_records'].append({
            'id': item['id'], 'label': item.get('label', item['id']),
            'confidence': item['confidence'], 'position_uncertainty_m': item['position_uncertainty_m'],
            'area_m2': round(area, 4), 'observed_area_m2': round(polygon.area, 4),
            'coverage_fraction': round(min(1., area / polygon.area), 4),
            'surface_scope': list(scope), 'evidence': item.get('evidence'),
            'geometry': item['geometry'], 'paint_width_m': item.get('paint_width_m')})
    batches.finish()
    stats['road_marking_area_m2'] = round(stats['road_marking_area_m2'], 4)
    return stats
