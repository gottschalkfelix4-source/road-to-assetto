"""Auditable, manually reviewed DOP20 road alignment; no AI geometry.

The reference is an interpretation of visible asphalt edges, not a survey.
OSM remains the source at obscured locations and exact route endpoints.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.interpolate import PchipInterpolator
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import LineString, Point


def _stations(points):
    return np.r_[0., np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]


def _validate_points(points):
    points = np.asarray(points, dtype=float)
    if points.ndim != 2 or points.shape[1] != 2 or len(points) < 2 or not np.isfinite(points).all():
        raise ValueError('Road alignment requires finite UTM coordinate pairs')
    if np.any(np.linalg.norm(np.diff(points, axis=0), axis=1) < .001):
        raise ValueError('Road alignment contains duplicate points')
    if not LineString(points).is_simple:
        raise ValueError('Road alignment crosses itself')
    return points


def load_alignment(data_dir: Path, osm_xy):
    """Return a corrected full polyline and JSON-serializable provenance.

    Call BEFORE route selection. If road_alignment.json is absent, OSM is
    returned unchanged. Invalid/mismatching references fail explicitly. The
    default 6-m UI setting means the observed profile at its natural scale.
    """
    osm_xy = _validate_points(osm_xy)
    source_s = _stations(osm_xy)
    reference = Path(data_dir) / 'road_alignment.json'
    if not reference.exists():
        return osm_xy.copy(), {'enabled': False, 'applied': False, 'source': 'OpenStreetMap',
                               'width_source': 'constant user estimate'}
    doc = json.loads(reference.read_text(encoding='utf-8'))
    if doc.get('schema_version') != 1 or doc.get('crs') != 'EPSG:25832':
        raise ValueError('Unsupported road alignment reference')
    expected = np.asarray(doc['source_endpoints_utm'], dtype=float)
    if expected.shape != (2, 2) or not np.allclose(osm_xy[[0, -1]], expected, atol=.002, rtol=0):
        raise ValueError('Road alignment reference does not match route endpoints')
    if abs(source_s[-1] - float(doc['source_length_m'])) > .01:
        raise ValueError('Road alignment reference does not match route length')
    ways = Path(data_dir) / 'road_ways.json'
    if ways.exists() and hashlib.sha256(ways.read_bytes()).hexdigest() != doc['source_osm_sha256']:
        raise ValueError('Road alignment requires re-review after OSM data changes')
    observations = doc['cross_sections']
    station = np.asarray([r['station_m'] for r in observations], dtype=float)
    offsets = np.asarray([r['center_offset_left_m'] for r in observations], dtype=float)
    widths = np.asarray([r['width_m'] for r in observations], dtype=float)
    if (not np.isfinite(np.r_[station, offsets, widths]).all() or
            np.any(np.diff(station) <= 0) or station[0] != 0 or
            abs(station[-1] - source_s[-1]) > .001 or
            np.any(widths < 3.5) or np.any(widths > 9) or np.any(np.abs(offsets) > 3)):
        raise ValueError('Invalid road edge observations')
    sample_s = np.linspace(0, source_s[-1], int(np.ceil(source_s[-1])) + 1)
    base = np.c_[np.interp(sample_s, source_s, osm_xy[:, 0]),
                 np.interp(sample_s, source_s, osm_xy[:, 1])]
    tangent = np.gradient(gaussian_filter1d(base, 2., axis=0, mode='nearest'), axis=0)
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    left = np.c_[-tangent[:, 1], tangent[:, 0]]
    corrected = base + left * PchipInterpolator(station, offsets)(sample_s)[:, None]
    # Junctions have no uniquely observable road centre. These reviewed points
    # follow the unobstructed paved continuation, instead of OSM's branch node.
    junction = doc['junction']
    tr = np.asarray(doc['pixel_to_utm'], dtype=float)
    pixel = np.asarray(junction['reviewed_pixel_points'], dtype=float)
    controls = np.c_[tr[0] * pixel[:, 0] + tr[2], tr[4] * pixel[:, 1] + tr[5]]
    old_line = LineString(osm_xy)
    control_s = np.asarray([old_line.project(Point(p)) for p in controls])
    a, b = map(float, junction['blend_source_station_m'])
    if not (0 < a < control_s[0] < control_s[-1] < b < source_s[-1]) or np.any(np.diff(control_s) <= 0):
        raise ValueError('Invalid junction reference order')
    boundary = np.c_[np.interp([a, b], sample_s, corrected[:, 0]),
                     np.interp([a, b], sample_s, corrected[:, 1])]
    knot_s = np.r_[a, control_s, b]
    knot_xy = np.vstack([boundary[0], controls, boundary[1]])
    mask = (sample_s >= a) & (sample_s <= b)
    corrected[mask] = PchipInterpolator(knot_s, knot_xy, axis=0)(sample_s[mask])
    # Small sub-metre filter removes interpolation kinks, not real bends.
    corrected = gaussian_filter1d(corrected, .65, axis=0, mode='nearest')
    fallback_mask = np.zeros(len(sample_s), dtype=bool)
    for section in doc['fallback_sections']:
        fallback_mask |= (sample_s >= section['start_m']) & (sample_s <= section['end_m'])
    corrected[fallback_mask] = base[fallback_mask]
    corrected[[0, -1]] = osm_xy[[0, -1]]
    _validate_points(corrected)
    if np.max(np.linalg.norm(corrected - base, axis=1)) > 10:
        raise ValueError('Road reference correction exceeds reviewed bounds')
    line = LineString(corrected)
    profile_points = np.c_[np.interp(station, sample_s, corrected[:, 0]),
                           np.interp(station, sample_s, corrected[:, 1])]
    profile_s = np.asarray([line.project(Point(p)) for p in profile_points])
    profile_s[0] = 0.
    profile_s[-1] = line.length
    metadata = {
        'enabled': True, 'applied': True, 'source': doc['source'], 'reference_file': reference.name,
        'reference_sha256': hashlib.sha256(reference.read_bytes()).hexdigest(),
        'method': doc['method'], 'accuracy': doc['accuracy'],
        'source_length_m': float(source_s[-1]), 'corrected_length_m': float(line.length),
        'maximum_station_displacement_m': float(np.max(np.linalg.norm(corrected - base, axis=1))),
        'maximum_lateral_deviation_from_osm_m': float(max(old_line.distance(Point(p)) for p in corrected)),
        'width_range_m': [float(widths.min()), float(widths.max())],
        'width_scale_reference_m': 6., 'cross_section_count': len(observations),
        'confidence': 'mixed: manual image interpretation; not ground surveyed',
        'observation_counts': {key: sum(r['confidence'] == key for r in observations)
                               for key in ('medium', 'low', 'inferred', 'fallback')},
        'dop_interpreted_source_length_fraction': float(1 - sum(r['end_m'] - r['start_m']
                               for r in doc['fallback_sections']) / source_s[-1]),
        'recommended_resample_smoothing_sigma_m': .8,
        'maximum_resample_deviation_m': 1.5,
        'junction': junction,
        'reference_xy_utm': corrected.round(5).tolist(),
        'width_profile': [{'distance_m': float(s), 'width_m': float(w)} for s, w in zip(profile_s, widths)],
        'fallback_sections': doc['fallback_sections'],
    }
    return corrected, metadata


def widths_at(sampled_xy, metadata, nominal_width_m=6.):
    """Sample the reference width along full OR cropped route coordinates.

    A nominal UI width of 6 uses the observed profile unchanged; other values
    scale that profile proportionally. The caller must still check offset
    polygon validity, especially when allowing unusually wide custom roads.
    """
    xy = np.asarray(sampled_xy, dtype=float)
    nominal = float(nominal_width_m)
    if not np.isfinite(nominal) or not 3.5 <= nominal <= 9:
        raise ValueError('Nominal road width must be between 3.5 and 9 metres')
    if xy.ndim != 2 or xy.shape[1] != 2 or not np.isfinite(xy).all():
        raise ValueError('Width sampling requires finite UTM coordinate pairs')
    if not metadata.get('enabled'):
        return np.full(len(xy), nominal)
    line = LineString(metadata['reference_xy_utm'])
    profile = metadata['width_profile']
    station = np.asarray([p['distance_m'] for p in profile])
    widths = np.asarray([p['width_m'] for p in profile])
    sampled_s = np.asarray([line.project(Point(p)) for p in xy])
    result = PchipInterpolator(station, widths)(sampled_s) * nominal / 6.
    if not np.isfinite(result).all() or np.any(result <= 0):
        raise ValueError('Invalid sampled road widths')
    return result
