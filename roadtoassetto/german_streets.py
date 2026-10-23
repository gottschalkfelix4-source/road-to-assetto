"""Inferred German village street edges, separate from the driving surface.

The road's surveyed/interpreted alignment and collision meshes are inputs and
are never changed here. Kerb reveals, stone sizes and drain positions are
plausible construction detail, not a survey of Langstrasse's street furniture.
All additions are outside the exported asphalt and have no collision flag.
"""
from __future__ import annotations

from pathlib import Path
import json

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
from shapely.geometry import LineString, Point, Polygon
from shapely.prepared import prep


def street_edge_textures(textures):
    """Original, unlit small-stone textures, with explicit metre-scale repeats."""
    textures = Path(textures)
    textures.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(35129)
    size = 512
    yy, xx = np.indices((size, size))
    for filename, repeat, length, width, base, tint in [
        ('de_paving.png', 1., .20, .10, 145., [2., 1., -1.]),
        ('de_paving_red.png', 1., .20, .10, 111., [17., -3., -9.]),
        ('de_gutter.png', .64, .16, .16, 130., [2., 1., -2.]),
    ]:
        x, y = xx * repeat / size, yy * repeat / size
        row = np.floor(y / width).astype(int)
        shifted = x + (row % 2) * length * .5
        col = np.floor(shifted / length).astype(int)
        stone = np.sin(col * 72.31 + row * 135.7 + 8.2) * 43758.5453
        stone = stone - np.floor(stone)
        joint = (np.mod(shifted, length) < .003) | (np.mod(y, width) < .003)
        grain = gaussian_filter(rng.normal(size=(size, size)), .45, mode='wrap')
        value = base + (stone - .5) * 12 + grain * 4
        value[joint] = 83 + grain[joint] * 2
        pixels = np.clip(value[..., None] + np.array(tint), 0, 255).astype('uint8')
        Image.fromarray(pixels).save(textures / filename)
    grain = rng.normal(size=(size, size))
    pores = gaussian_filter(grain, .7, mode='wrap')
    value = 165 + 2 * grain + 3 * pores
    value[pores < -.9] -= 12
    Image.fromarray(np.clip(value[..., None] + np.array([2, 1, -2]), 0, 255).astype('uint8')).save(textures / 'de_kerb.png')
    metal = np.clip(61 + rng.normal(0, 2, (128, 128, 1)) + np.array([1, 1, 0]), 0, 255).astype('uint8')
    Image.fromarray(metal).save(textures / 'de_cast_iron.png')
    Image.new('RGB', (32, 32), (29, 31, 29)).save(textures / 'de_drain_shadow.png')
    value = 69 + gaussian_filter(rng.normal(size=(size, size)), .5, mode='wrap') * 5
    Image.fromarray(np.clip(value[..., None] + np.array([0., .5, 1.]), 0, 255).astype('uint8')).save(textures / 'de_sidewalk_asphalt.png')


def sidewalk_permissions(data_dir, positions, tangents):
    """Apply original OSM way-relative left/right tags in actual travel direction.

    The nearest original Langstrasse segment, rather than the generated
    direction, decides which physical side is tagged. Missing tags retain
    conservative inference; `no` and `separate` explicitly suppress sidewalk
    generation because a separately mapped path must not become a new one.
    """
    from pyproj import Transformer
    positions, tangents = np.asarray(positions), np.asarray(tangents)
    result = {-1: np.ones(len(positions), bool), 1: np.ones(len(positions), bool)}
    sources = [None] * len(positions)
    path = Path(data_dir) / 'road_ways.json' if data_dir else None
    if path is None or not path.exists():
        return result, sources
    projector = Transformer.from_crs(4326, 25832, always_xy=True)
    segments = []
    for way in json.loads(path.read_text(encoding='utf-8')):
        points = way.get('geometry', [])
        if len(points) < 2:
            continue
        points = np.c_[projector.transform([p['lon'] for p in points], [p['lat'] for p in points])]
        # np.c_ of a transform tuple is (N,2), preserving OSM way order.
        for a, b in zip(points[:-1], points[1:]):
            if np.linalg.norm(b-a) > .01:
                segments.append((a, b, way.get('tags', {}), way.get('id')))
    if not segments:
        return result, sources
    a = np.array([s[0] for s in segments]); delta = np.array([s[1]-s[0] for s in segments])
    t = np.clip(np.sum((positions[:, None, :] - a) * delta, axis=2) / np.sum(delta * delta, axis=1), 0, 1)
    projected = a + t[..., None] * delta
    nearest = np.argmin(np.linalg.norm(projected - positions[:, None, :], axis=2), axis=1)
    for i, seg in enumerate(nearest):
        tags, way_id = segments[seg][2:]
        general = tags.get('sidewalk', '')
        direction = 1 if np.dot(tangents[i], delta[seg]) >= 0 else -1
        for side in (-1, 1):
            label = 'left' if side * direction > 0 else 'right'
            exact = tags.get('sidewalk:' + label)
            if exact in ('yes', 'no', 'separate'):
                enabled = exact == 'yes'
            elif general in ('no', 'separate'):
                enabled = False
            elif general in ('left', 'right'):
                enabled = general == label
            else:
                enabled = True
            result[side][i] = enabled
        sources[i] = {'way_id': way_id, 'sidewalk': general or 'unspecified'}
    return result, sources


def _paving_style(point, road_center):
    """Local 2012 CC0 photos guide only nearby material interpretation.

    Coordinates are OSM building centres (EPSG:25832) independently matched
    to Langstrasse 39 and 13. This does not assert current material coverage.
    """
    references = [([462014.977459, 5556364.651311], 'red_concrete', 'Langstrasse 39, CC0 Karsten Ratzke, 2012-01-15'),
                  ([462224.367278, 5556289.423234], 'asphalt', 'Langstrasse 13, CC0 Karsten Ratzke, 2012-01-15')]
    for center, style, source in references:
        if (np.linalg.norm(np.asarray(point) - center) < 22.
                and np.dot(np.asarray(center) - road_center, np.asarray(point) - road_center) > 0):
            return style, source
    return 'neutral_concrete', 'generic inferred small-format paving'


def _orient(vertices, quads):
    """Independent face vertices keep the cast concrete's bevels crisp."""
    center = vertices.mean(axis=0)
    output, faces, source = [], [], []
    for indices in quads:
        indices = list(indices)
        p = vertices[indices]
        if np.dot(np.cross(p[1] - p[0], p[2] - p[0]), p.mean(axis=0) - center) < 0:
            indices.reverse()
            p = vertices[indices]
        offset = len(output)
        output.extend(p)
        source.extend(indices)
        faces.extend((offset, offset + j, offset + j + 1) for j in range(1, len(p) - 1))
    return np.asarray(output), faces, np.asarray(source)


def kerb_segment(centers, normals, half, road_height, side, height, ac,
                 gutter_end=.352, width=.15):
    """One cast concrete kerb: 25 mm bevel, vertical reveal, buried base."""
    centers, normals, half, road_height, height = map(np.asarray, (centers, normals, half, road_height, height))
    xy, z, local = [], [], []
    for i in range(2):
        bevel = min(.025, float(height[i]) * .45)
        profile = [(0, -.24), (0, height[i] - bevel),
                   (bevel, height[i]), (width, height[i]), (width, -.24)]
        for offset, high in profile:
            xy.append(centers[i] + normals[i] * side * (half[i] + gutter_end + offset))
            z.append(road_height[i] + high)
            local.append([i * np.linalg.norm(centers[1] - centers[0]), offset, high])
    vertices = ac(np.asarray(xy), np.asarray(z))
    quads = [tuple(range(5)), tuple(range(5, 10))]
    quads += [(j, (j + 1) % 5, (j + 1) % 5 + 5, j + 5) for j in range(5)]
    vertices, faces, source = _orient(vertices, quads)
    # One metre along, half a metre around the cross-section. No enormous
    # world-coordinate UVs, no one-texture-per-slice distortion.
    local = np.asarray(local)[source]
    uv = np.c_[local[:, 0], (local[:, 1] + local[:, 2]) / .5]
    return vertices, faces, uv


def _surface(points, heights, ac, uv):
    vertices = ac(np.asarray(points), np.asarray(heights))
    faces = [(0, 1, 2), (0, 2, 3)]
    if np.cross(vertices[1] - vertices[0], vertices[2] - vertices[0])[1] < 0:
        faces = [(a, c, b) for a, b, c in faces]
    return vertices, faces, np.asarray(uv)


def _paved_prism(points, bottom, top, ac):
    """Closed slab whose top retains the plan's 0--2 diagonal on both sides.

    The generic prism reverses whole face vertex order when flipping winding,
    which also changes its diagonal on a warped quad. Terrain contact needs
    the exact same triangles as the declared paving surface on either side.
    """
    from roadtoassetto.scenery import prism
    v, f = prism(points, bottom, top, ac)
    body = np.vstack([v[:4], v[8:]])
    faces = [tuple(j if j < 4 else j-4 for j in face) for n, face in enumerate(f) if n not in (2,3)]
    surface, top_faces, _ = _surface(points, top, ac, np.zeros((4,2)))
    return np.vstack([body, surface]), faces + [tuple(j+len(body) for j in face) for face in top_faces]


def _runs(values):
    start = None
    for i, enabled in enumerate(list(values) + [False]):
        if enabled and start is None:
            start = i
        if not enabled and start is not None:
            yield start, i
            start = None


def _street_frame(xy, road_y, left, distance, half_width, edge_y=None):
    xy, road_y, left, distance = map(lambda a: np.asarray(a, dtype=float), (xy, road_y, left, distance))
    half = np.broadcast_to(np.asarray(half_width, dtype=float), (len(xy),))
    if (xy.shape != (len(distance), 2) or left.shape != xy.shape or road_y.shape != distance.shape
            or len(xy) < 2 or not np.isfinite(np.r_[xy.ravel(), road_y, left.ravel(), distance, half]).all()
            or np.any(np.diff(distance) <= 0) or np.any(half <= 0)):
        raise ValueError('Invalid German street-edge route or road width profile')
    edge_y = road_y if edge_y is None else np.asarray(edge_y, dtype=float)
    if edge_y.shape != road_y.shape or not np.isfinite(edge_y).all():
        raise ValueError('Invalid road edge height profile')
    stations = np.r_[np.arange(float(distance[0]), float(distance[-1]), 1.), float(distance[-1])]
    centers = np.c_[np.interp(stations, distance, xy[:, 0]), np.interp(stations, distance, xy[:, 1])]
    directions = np.c_[np.interp(stations, distance, left[:, 0]), np.interp(stations, distance, left[:, 1])]
    directions /= np.maximum(np.linalg.norm(directions, axis=1)[:, None], 1e-12)
    return {'stations': stations.tolist(), 'centers': centers.tolist(), 'normals': directions.tolist(),
            'half_widths': np.interp(stations, distance, half).tolist(),
            'edge_heights': np.interp(stations, distance, edge_y).tolist()}


def plan_street_edges(*, context, xy, road_y, left, distance, half_width,
                      road_polygon, dem, flatten, aerial=None, data_dir=None,
                      edge_y=None, quality='high'):
    """Plan exact paved top triangles before the surrounding terrain is built.

    ``paving_sections`` is a JSON-serializable shared interface: absolute UTM
    ``xy[4,2]``, absolute ``top[4]``, fixed ``faces`` and inner/outer edge
    indices. Adjacent cells share their station vertices and top heights.
    Ground builders must use these surfaces, not independently estimate another
    road-side profile. The returned plan contains no Shapely objects or meshes.
    """
    frame = _street_frame(xy, road_y, left, distance, half_width, edge_y)
    stations, centers, normals, widths, heights = (np.asarray(frame[k]) for k in
        ('stations', 'centers', 'normals', 'half_widths', 'edge_heights'))
    midpoint = (centers[:-1] + centers[1:]) * .5
    permissions, tag_sources = sidewalk_permissions(data_dir, midpoint, np.diff(centers, axis=0))
    buildings = context['buildings']
    close_buildings = buildings.buffer(.025)
    obstacle = prep(context['other_roads'].union(buildings.buffer(.012)))
    urban = prep(buildings.buffer(22.))
    edge_runs, pavement_runs, sections = [], [], []
    gutter_start, kerb_end = .012, .502

    def band(i, side, low, high):
        low = np.broadcast_to(np.asarray(low, dtype=float), (2,))
        high = np.broadcast_to(np.asarray(high, dtype=float), (2,))
        return np.array([centers[i] + normals[i] * side * (widths[i] + low[0]),
                         centers[i+1] + normals[i+1] * side * (widths[i+1] + low[1]),
                         centers[i+1] + normals[i+1] * side * (widths[i+1] + high[1]),
                         centers[i] + normals[i] * side * (widths[i] + high[0])])

    def clear(p):
        poly = Polygon(p)
        return (poly.is_valid and poly.area > .002 and not obstacle.intersects(poly)
                and not road_polygon.intersects(poly))

    for side in (-1, 1):
        # Query the actual nearest building face along each outward cross
        # section. A nearby wall gets a continuous frontage, while open land
        # retains a modest inferred path rather than paving entire gardens.
        desired = np.full(len(stations), 1.15)
        frontage = np.zeros(len(stations), bool)
        for j in range(len(stations)):
            start = centers[j] + normals[j] * side * (widths[j] + kerb_end)
            ray = LineString([start, start + normals[j] * side * 2.81])
            hit = ray.intersection(close_buildings)
            if not hit.is_empty:
                desired[j] = min(2.8, max(0., Point(start).distance(hit) - .008))
                frontage[j] = True

        allowed, available = [], []
        limits = desired.copy()
        for i in range(len(stations)-1):
            p = band(i, side, gutter_start, kerb_end)
            okay = (stations[i] >= 14 and stations[i+1] <= stations[-1]-14
                    and urban.covers(Point(p.mean(axis=0))) and clear(p)
                    and np.dot(normals[i], normals[i+1]) > .985)
            allowed.append(okay)
            available.append(False)
            if not okay or not permissions[side][i]:
                continue
            candidate = desired[[i, i+1]].copy()
            # Shared widths are constrained by both neighbouring cells. A
            # corner cannot be crossed simply because both endpoint rays clear.
            for _ in range(14):
                if candidate.min() < .32:
                    break
                p = band(i, side, kerb_end, kerb_end+candidate)
                clear_geometry = clear(p)
                ground = flatten(p, dem(p))-.08
                road = heights[[i, i+1, i+1, i]]
                # Local grading can flatten modest DGM waves. Real banks and
                # deep drops still require a separately designed retaining wall.
                grade_okay = np.max(ground-road) <= .55 and np.min(ground-road) >= -.65
                colour_okay = True
                if aerial is not None:
                    rgb, green, _ = aerial.at(np.vstack([p, p.mean(axis=0)]))
                    if frontage[i] or frontage[i+1]:
                        colour_okay = np.mean(green) < .65
                    else:
                        colour_okay = np.max(green) < .45 and np.mean(rgb) >= 49
                if clear_geometry and grade_okay and colour_okay:
                    available[-1] = True
                    limits[[i, i+1]] = np.minimum(limits[[i, i+1]], candidate)
                    break
                candidate *= .86

        for begin, end in _runs(allowed):
            if stations[end]-stations[begin] < 7.:
                continue
            edge_runs.append({'side': side, 'start_m': float(stations[begin]), 'end_m': float(stations[end]),
                              'first_index': begin, 'last_index': end})
            for a, b in _runs(available[begin:end]):
                a, b = a+begin, b+begin
                if stations[b]-stations[a] < 7.:
                    continue
                local_width = limits[a:b+1].copy()
                # Cap width changes to 35 cm/metre. This never expands a cell
                # past its obstacle clearance, unlike unconstrained smoothing.
                for j in range(1, len(local_width)):
                    local_width[j] = min(local_width[j], local_width[j-1]+.35*(stations[a+j]-stations[a+j-1]))
                for j in range(len(local_width)-2, -1, -1):
                    local_width[j] = min(local_width[j], local_width[j+1]+.35*(stations[a+j+1]-stations[a+j]))
                # Broad rounded-in-plan transitions have a nonzero 35 cm end;
                # never collapse into the old pointed slab. At building fronts
                # keep the full face-following width instead of reopening a gap.
                edge_distance = np.minimum(stations[a:b+1]-stations[a], stations[b]-stations[a:b+1])
                blend = np.clip(edge_distance/3., 0., 1.)
                blend = blend*blend*(3.-2.*blend)
                return_width = np.minimum(local_width, .35+(local_width-.35)*blend)
                local_width = np.where(frontage[a:b+1], local_width, return_width)
                # A transition into a building frontage must obey the same
                # bound after applying the open-land end easing.
                for j in range(1, len(local_width)):
                    local_width[j] = min(local_width[j], local_width[j-1]+.35*(stations[a+j]-stations[a+j-1]))
                for j in range(len(local_width)-2, -1, -1):
                    local_width[j] = min(local_width[j], local_width[j+1]+.35*(stations[a+j+1]-stations[a+j]))
                materials, sources, emitted = set(), set(), []
                for i in range(a, b):
                    pair_width = local_width[i-a:i-a+2]
                    p = band(i, side, kerb_end, kerb_end+pair_width)
                    if not clear(p):
                        continue
                    s = stations[[i, i+1, i+1, i]]
                    taper = np.clip(np.minimum(s-stations[begin], stations[end]-s)/3., 0., 1.)
                    taper = taper*taper*(3.-2.*taper)
                    crossfall = np.array([0., 0., pair_width[1]*.015, pair_width[0]*.015])
                    top = heights[[i, i+1, i+1, i]]+.012+.098*taper+crossfall*taper
                    style, source = _paving_style(p.mean(axis=0), (centers[i]+centers[i+1])*.5)
                    section = {'xy': p.tolist(), 'top': top.tolist(), 'faces': [[0,1,2],[0,2,3]],
                        'outer_edge_indices': [3,2], 'inner_edge_indices': [0,1],
                        'side': side, 'start_m': float(stations[i]), 'end_m': float(stations[i+1]),
                        'width_m': pair_width.tolist(), 'style': style, 'source': source,
                        'frontage': [bool(frontage[i]), bool(frontage[i+1])],
                        'frontage_target_clearance_m': .033,
                        'frontage_gap_m': [float(Point(v).distance(buildings)) for v in p[[3,2]]],
                        'frame_index': i}
                    sections.append(section); emitted.append(section)
                    materials.add(style); sources.add(source)
                if emitted:
                    all_widths = np.asarray([q['width_m'] for q in emitted])
                    pavement_runs.append({'side': side, 'start_m': emitted[0]['start_m'],
                        'end_m': emitted[-1]['end_m'], 'width_m': float(all_widths.mean()),
                        'width_range_m': [float(all_widths.min()), float(all_widths.max())],
                        'materials': sorted(materials), 'material_sources': sorted(sources)})
    return {'version': 2, 'frame': frame, 'paving_sections': sections,
            'pavement_runs': pavement_runs, 'street_edge_runs': edge_runs,
            'osm_sidewalk_tags_applied': sorted({str(t['way_id'])+':'+t['sidewalk'] for t in tag_sources if t}),
            'pavement_width_method': 'Per-station building-face ray cast up to 2.8m; bounded longitudinal transitions and conservative open-land fallback',
            'surface_interface': 'Absolute UTM xy and top elevations with exact two-triangle topology; outer edge indices [3,2]',
            'edge_end_lowering_m': 3., 'pavement_max_width_m': 2.8,
            'frontage_accuracy': 'Building hull edges constrain inferred paving; no cadastral property boundaries are claimed'}


def build_street_edges(*, context, xy, road_y, left, distance, half_width,
                       road_polygon, dem, flatten, ac, batches, material,
                       scene, textures, aerial=None, quality='high', data_dir=None,
                       edge_y=None, plan=None, ground_sampler=None):
    """Render the shared street plan after the exact terrain has been exported.

    A precomputed plan fixes paving boundaries and top triangles for terrain,
    architecture and rendering. Bases use the exported terrain sampler; the
    analytic fallback exists only for callers without a terrain scene.
    """
    from roadtoassetto.scenery import prism
    expected_frame = _street_frame(xy, road_y, left, distance, half_width, edge_y)
    if plan is None:
        plan = plan_street_edges(context=context, xy=xy, road_y=road_y, left=left,
            distance=distance, half_width=half_width, road_polygon=road_polygon,
            dem=dem, flatten=flatten, aerial=aerial, data_dir=data_dir, edge_y=edge_y,
            quality=quality)
    if plan.get('version') != 2 or any(not np.array_equal(np.asarray(plan['frame'].get(k)), np.asarray(v))
                                      for k, v in expected_frame.items()):
        raise ValueError('Street plan does not match the supplied road geometry')
    stations, centers, normals, widths, heights = (np.asarray(plan['frame'][k]) for k in
        ('stations', 'centers', 'normals', 'half_widths', 'edge_heights'))
    sample_ground = ground_sampler if ground_sampler is not None else lambda p: flatten(p, dem(p))-.08
    street_edge_textures(textures)
    paving = material('de_small_format_concrete_pavers', 'de_paving.png', ambient=.3, diffuse=.47, specular=.025)
    paving_red = material('de_local_red_concrete_pavers', 'de_paving_red.png', ambient=.3, diffuse=.47, specular=.025)
    paving_asphalt = material('de_asphalt_sidewalk', 'de_sidewalk_asphalt.png', ambient=.3, diffuse=.47, specular=.03)
    gutter = material('de_two_row_stone_gutter', 'de_gutter.png', ambient=.3, diffuse=.46, specular=.03)
    kerb = material('de_chamfered_concrete_kerb', 'de_kerb.png', ambient=.3, diffuse=.5, specular=.025)
    iron = material('de_cast_iron_gully', 'de_cast_iron.png', ambient=.25, diffuse=.4, specular=.12)
    dark = material('de_drain_slots', 'de_drain_shadow.png', ambient=.2, diffuse=.3, specular=.01)
    for index in (paving, paving_red, paving_asphalt, gutter, kerb):
        scene['materials'][index].update(roughness=.92, specular_exp=10.)
    scene['materials'][iron].update(roughness=.78, specular_exp=25.)
    gully_positions, emitted_surfaces = [], []
    counts = {'pavement_sections': 0, 'kerb_sections': 0, 'drainage_sections': 0,
              'gullies': 0, 'pavement_back_edgings': 0}
    gutter_start, gutter_end, kerb_end = .012, .352, .502

    def band(i, side, low, high):
        return np.array([centers[i]+normals[i]*side*(widths[i]+low),
                         centers[i+1]+normals[i+1]*side*(widths[i+1]+low),
                         centers[i+1]+normals[i+1]*side*(widths[i+1]+high),
                         centers[i]+normals[i]*side*(widths[i]+high)])

    for run in plan['street_edge_runs']:
        side, begin, end = run['side'], run['first_index'], run['last_index']
        valid_gullies = []
        for i in range(begin, end):
            span = stations[i+1]-stations[i]
            if span < .05:
                continue
            p = band(i, side, gutter_start, gutter_end)
            level = heights[[i,i+1,i+1,i]]+.003
            uv = [[stations[i]/.64, 0], [stations[i+1]/.64, 0],
                  [stations[i+1]/.64, .34/.64], [stations[i]/.64, .34/.64]]
            v, f, uv = _surface(p, level, ac, uv)
            batches.append('DE_STONE_DRAINAGE', gutter, v, f, uv)
            counts['drainage_sections'] += 1
            taper = np.clip(np.minimum(stations[[i,i+1]]-stations[begin],
                                        stations[end]-stations[[i,i+1]])/3., 0., 1.)
            taper = taper*taper*(3.-2.*taper)
            reveal = .012+.108*taper
            pair = centers[[i,i+1]].copy()
            delta = pair[1]-pair[0]
            pair[0] += delta*(.002/span); pair[1] -= delta*(.002/span)
            v, f, uv = kerb_segment(pair, normals[[i,i+1]], widths[[i,i+1]],
                                   heights[[i,i+1]], side, reveal, ac)
            batches.append('DE_KERB_UNITS', kerb, v, f, uv)
            counts['kerb_sections'] += 1
            if i >= begin+3 and i < end-3:
                valid_gullies.append(i)

        for i in valid_gullies:
            if (i+(9 if side == 1 else 0)) % 28 != 0:
                continue
            middle = (centers[i]+centers[i+1])*.5
            normal = (normals[i]+normals[i+1])*.5
            normal /= np.linalg.norm(normal)
            forward = (centers[i+1]-centers[i])/np.linalg.norm(centers[i+1]-centers[i])
            curbside = middle+normal*side*((widths[i]+widths[i+1])*.5+.182)
            slope = (heights[i+1]-heights[i])/(stations[i+1]-stations[i])

            def grate_rect(s0, s1, t0, t1, high, mid):
                p = np.array([curbside+forward*s0+normal*side*t0,
                              curbside+forward*s1+normal*side*t0,
                              curbside+forward*s1+normal*side*t1,
                              curbside+forward*s0+normal*side*t1])
                z = (heights[i]+heights[i+1])*.5+slope*np.array([s0,s1,s1,s0])+high
                v, f, uv = _surface(p, z, ac, [[0,0],[1,0],[1,1],[0,1]])
                batches.append('DE_GULLY_GRATES', mid, v, f, uv)

            grate_rect(-.25,.25,-.14,.14,.004,dark)
            for a,b in [(-.25,-.225),(.225,.25)]:
                grate_rect(a,b,-.14,.14,.006,iron)
            for a,b in [(-.14,-.115),(.115,.14)]:
                grate_rect(-.225,.225,a,b,.006,iron)
            for s in np.arange(-.18,.19,.06):
                grate_rect(float(s),float(s+.019),-.115,.115,.006,iron)
            counts['gullies'] += 1
            gully_positions.append({'side':side,'station_m':round(float((stations[i]+stations[i+1])*.5),3),
                                     'source':'inferred interval in accepted kerb run'})

    for section in plan['paving_sections']:
        p, top = np.asarray(section['xy']), np.asarray(section['top'])
        ground = np.asarray(sample_ground(p),dtype=float)
        if ground.shape != (4,) or not np.isfinite(ground).all():
            raise ValueError('Paving needs one exact finite terrain height per corner')
        # Full underside is below the exact exported terrain and the planned
        # top, independent of DGM interpolation used during the planning pass.
        bottom = np.minimum(ground-.20,top-.35)
        v,f = _paved_prism(p,bottom,top,ac)
        i,side = section['frame_index'],section['side']
        direction = (centers[i+1]-centers[i])/np.linalg.norm(centers[i+1]-centers[i])
        direction_ac = np.array([direction[0],-direction[1]])
        normal_ac = np.array([normals[i,0],-normals[i,1]])*side
        local = v[:,[0,2]]-ac(centers[[i]],heights[[i]])[0,[0,2]]
        uv = np.c_[local@direction_ac+stations[i],local@normal_ac]
        current_material = {'neutral_concrete':paving,'red_concrete':paving_red,'asphalt':paving_asphalt}[section['style']]
        batches.append('DE_PAVING',current_material,v,f,uv)
        counts['pavement_sections'] += 1
        emitted_surfaces.append({**section,'material':current_material})
        # A small flush edging defines open-land boundaries. It occupies only
        # the outermost 6 cm of the accepted path, never a separate retaining
        # wall or a new strip through the garden. At house contact omit it.
        if not any(section['frontage']) and min(section['width_m']) >= .5:
            inner = np.array([p[3]+(p[0]-p[3])*(.06/section['width_m'][0]),
                              p[2]+(p[1]-p[2])*(.06/section['width_m'][1])])
            q = np.array([inner[0],inner[1],p[2],p[3]])
            border_top = np.array([top[3]-.0009,top[2]-.0009,top[2],top[3]])+.006
            border_ground = np.asarray(sample_ground(q),dtype=float)
            vv,ff = prism(q,np.minimum(border_ground-.15,border_top-.20),border_top,ac)
            local = vv[:,[0,2]]-ac(centers[[i]],heights[[i]])[0,[0,2]]
            buv = np.c_[local@direction_ac+stations[i],local@normal_ac]
            batches.append('DE_PAVEMENT_EDGING',kerb,vv,ff,buv)
            counts['pavement_back_edgings'] += 1

    counts['pavement_runs'] = len(plan['pavement_runs'])
    counts['kerb_runs'] = len(plan['street_edge_runs'])
    return {'counts':counts,'pavement_runs':plan['pavement_runs'],
            'street_edge_runs':plan['street_edge_runs'],'street_edge_surfaces':emitted_surfaces,
            'gully_placements':gully_positions,
            'street_edge_provenance': {
                'source':'Inferred construction detail beside DOP-aligned road; OSM side roads and official building footprints reserved',
                'materials':'Original deterministic unlit textures: 20x10 cm concrete pavers, 16x16 cm gutter stones and fine concrete',
                'kerb_unit_m':1.,'kerb_width_m':.15,'kerb_reveal_m':.12,'kerb_chamfer_m':.025,
                'drainage_width_m':.34,'pavement_crossfall':.015,'edge_end_lowering_m':3.,
                'pavement_width_method':plan['pavement_width_method'],'pavement_max_width_m':2.8,
                'surface_interface':plan['surface_interface'],
                'underside_height_source':'exact exported terrain triangles' if ground_sampler is not None else 'analytic terrain fallback',
                'height_reference':'Shared planned paved top triangles at road grade; terrain and house interfaces reuse the same boundaries',
                'collision':False,'surveyed_street_furniture':False,
                'osm_sidewalk_tags_applied':plan['osm_sidewalk_tags_applied'],
                'local_material_references':['https://commons.wikimedia.org/wiki/File:Altenhain,_Langstra%C3%9Fe_39.JPG',
                    'https://commons.wikimedia.org/wiki/File:Altenhain,_Langstra%C3%9Fe_13,_Vorderbau.JPG'],
                'local_material_reference_date':'2012-01-15; historical material guidance only, not current surface verification',
                'limitations':'Construction detail and paving widths are inferred between known building footprints; no cadastral property boundary or as-built survey'}}
