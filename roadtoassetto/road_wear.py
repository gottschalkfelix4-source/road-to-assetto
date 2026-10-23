"""Restrained, inferred German village-road wear on the exported road surface.

Placement is deliberately illustrative, not a survey of utility covers or road
damage. The ambientCG asphalt photograph stays unchanged. All overlays are
clipped to the actual road triangles, raised at most 1.8 mm, and non-colliding.
"""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
from shapely import STRtree, constrained_delaunay_triangles
from shapely.geometry import LineString, Polygon

from roadtoassetto.scenery import Batches


def wear_textures(textures):
    """Original mathematical iron casting / sealant patterns, no source editing."""
    textures = Path(textures)
    textures.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(716204)
    size = 512
    y, x = np.mgrid[:size, :size]
    xx = (x + .5) / size - .5
    yy = (y + .5) / size - .5
    radius = np.hypot(xx, yy)
    grain = gaussian_filter(rng.normal(size=(size, size)), .6)
    grain /= max(float(grain.std()), .01)
    # Small raised diagonal ribs typical of a nonslip iron casting, not a
    # claimed reproduction of any particular DIN-certified product.
    period = .067
    rib_a = np.minimum(np.mod(xx + yy, period), period - np.mod(xx + yy, period)) < .0045
    rib_b = np.minimum(np.mod(xx - yy, period), period - np.mod(xx - yy, period)) < .0045
    ribs = (rib_a | rib_b) & (radius < .425)
    rim = (radius > .442) & (radius < .476)
    ring_groove = ((radius > .427) & (radius < .438)) | ((radius > .480) & (radius < .490))
    value = 63. + 3.8 * grain + ribs * 17. + rim * 9. - ring_groove * 20.
    # Two recessed lifting pockets, deliberately without municipal emblems,
    # labels, or invented certification markings.
    pockets = (np.abs(xx) < .048) & (np.abs(np.abs(yy) - .343) < .016)
    value[pockets] -= 32.
    rusty = gaussian_filter(rng.random((size, size)), 18)
    rusty = np.clip((rusty - .497) * 260, 0, 1)
    iron = value[:, :, None] + rusty[:, :, None] * np.array([6., 2., -1.])
    Image.fromarray(np.clip(iron, 0, 255).astype('uint8')).save(textures / 'road_cast_iron.png')
    relief = ribs.astype(float) * .65 + rim * .25 - pockets * .55
    relief = gaussian_filter(relief, .8)
    gx = (np.roll(relief, -1, 1) - np.roll(relief, 1, 1)) * .6
    gy = (np.roll(relief, -1, 0) - np.roll(relief, 1, 0)) * .6
    normal = np.stack([-gx, gy, np.ones_like(gx)], axis=-1)
    normal /= np.linalg.norm(normal, axis=-1)[:, :, None]
    Image.fromarray(np.clip((normal + 1) * 127.5, 0, 255).astype('uint8')).save(textures / 'road_cast_iron_normal_dx.png')
    for filename, color, noise in [('road_bitumen.png', (41, 41, 40), 1.2),
                                   ('road_iron_rim.png', (68, 67, 64), 3.0)]:
        pixels = np.array(color) + rng.normal(0, noise, (128, 128, 1))
        Image.fromarray(np.clip(pixels, 0, 255).astype('uint8')).save(textures / filename)


class ExportedRoad:
    """Clip UTM polygons into the exact exported road triangles before lifting."""

    def __init__(self, scene, ac):
        origin = np.asarray(ac(np.zeros((1, 2)), np.zeros(1)), dtype=float)[0]
        self.ac = ac
        self.origin = origin
        pieces = []
        for mesh in scene.get('meshes', []):
            if mesh.get('name') != '1ROAD_MAIN':
                continue
            v = np.asarray(mesh['vertices'], dtype=np.float32).astype(float)
            faces = np.asarray(mesh['faces'], dtype=int)
            if v.ndim != 2 or v.shape[1] != 3 or faces.ndim != 2 or faces.shape[1] != 3:
                raise ValueError('Invalid exported road mesh')
            if not np.isfinite(v).all() or not len(faces) or np.any(faces < 0) or np.any(faces >= len(v)):
                raise ValueError('Invalid exported road vertices or indices')
            absolute = np.c_[v[:, 0] - origin[0], origin[2] - v[:, 2], v[:, 1] - origin[1]]
            pieces.append(absolute[faces])
        if not pieces:
            raise ValueError('Road wear requires an exported 1ROAD_MAIN mesh')
        self.triangles = np.concatenate(pieces)
        self.polygons = [Polygon(t[:, :2]) for t in self.triangles]
        if any(p.area <= 1e-10 for p in self.polygons):
            raise ValueError('Road wear requires nondegenerate road triangles')
        self.index = STRtree(self.polygons)

    def append(self, polygon, name, mat, batches, uv, offset):
        if not 0 < offset <= .0018:
            raise ValueError('Road decoration must remain between 0 and 1.8 mm above asphalt')
        if polygon.is_empty or not polygon.is_valid:
            return 0.
        area = 0.
        for index in sorted(self.index.query(polygon).tolist()):
            clipped = polygon.intersection(self.polygons[index])
            if clipped.is_empty or clipped.area < 1e-9:
                continue
            triangle = self.triangles[index]
            basis = (triangle[1:, :2] - triangle[0, :2]).T
            vertices = []
            faces = []
            # Constrained triangulation preserves narrow concave paint bends
            # and ring holes. Unconstrained triangulate()+covers discarded
            # valid area at zigzag corners. This only tessellates each exact
            # original road-face intersection, never bridges its diagonals.
            for part in constrained_delaunay_triangles(clipped).geoms:
                if part.area < 1e-9:
                    continue
                xy = np.asarray(part.exterior.coords)[:3, :2]
                weights = np.linalg.solve(basis, (xy - triangle[0, :2]).T).T
                heights = triangle[0, 2] + weights @ (triangle[1:, 2] - triangle[0, 2])
                points = self.ac(xy, heights + offset)
                # Very thin intersection slivers can collapse after prepare's
                # decimal rounding and KN5 float32 storage. Omit those slivers
                # before they become degenerate export faces.
                rounded = points.round(5)
                stored = rounded.astype(np.float32).astype(float)
                # The writer validates scene JSON's rounded float64 positions
                # before packing them as float32. Either representation may
                # collapse first, so both must satisfy its unchanged 1e-8
                # cross-product threshold (not the old, weaker 1e-9 filter).
                if any(np.linalg.norm(np.cross(v[1] - v[0], v[2] - v[0])) <= 1e-8
                       for v in (rounded, stored)):
                    continue
                if np.cross(points[1] - points[0], points[2] - points[0])[1] < 0:
                    points = points[[0, 2, 1]]
                    xy = xy[[0, 2, 1]]
                n = len(vertices)
                vertices.extend(points)
                faces.append((n, n + 1, n + 2))
                area += part.area
            if faces:
                vertices = np.asarray(vertices)
                absolute_xy = np.c_[vertices[:, 0] - self.origin[0], self.origin[2] - vertices[:, 2]]
                batches.append(name, mat, vertices, faces, uv(absolute_xy))
        return area


class RouteFrame:
    """Metric station/lateral coordinates respecting the variable-width route."""

    def __init__(self, xy, left, distance, halfwidth):
        self.xy = np.asarray(xy, dtype=float)
        self.left = np.asarray(left, dtype=float)
        self.distance = np.asarray(distance, dtype=float)
        self.halfwidth = np.broadcast_to(np.asarray(halfwidth, dtype=float), self.distance.shape).copy()
        n = len(self.distance)
        if (n < 2 or self.xy.shape != (n, 2) or self.left.shape != (n, 2)
                or not all(np.isfinite(a).all() for a in (self.xy, self.left, self.distance, self.halfwidth))
                or np.any(np.diff(self.distance) <= 0) or np.any(self.halfwidth <= .5)):
            raise ValueError('Invalid route frame for road wear')

    def points(self, station, lateral):
        s, l = np.broadcast_arrays(station, lateral)
        s, l = s.ravel(), l.ravel()
        if np.any(s < self.distance[0]) or np.any(s > self.distance[-1]):
            raise ValueError('Decoration station lies outside the selected road')
        center = np.c_[np.interp(s, self.distance, self.xy[:, 0]), np.interp(s, self.distance, self.xy[:, 1])]
        left = np.c_[np.interp(s, self.distance, self.left[:, 0]), np.interp(s, self.distance, self.left[:, 1])]
        # Linear row interpolation matches the road's offset strip, rather than
        # normalising interpolated vectors and moving toward its outside edge.
        return center + left * l[:, None]

    def width(self, station):
        return np.interp(station, self.distance, self.halfwidth)


def _stations(rng, start, end, minimum, extra):
    station = start + rng.uniform(0, extra)
    while station < end:
        yield float(station)
        station += minimum + rng.exponential(extra)


def build_road_wear(textures, scene, material, add_mesh, ac, xy, road_y, left,
                    distance, halfwidth, *, asphalt_material=None, seed=680107):
    """Add subtle road repairs, resealed joints and circular utility covers.

    Call after 1ROAD_MAIN exists. The existing road mesh, normal map, alignment,
    width and collision remain untouched. ``road_y`` is retained in the public
    API for consistent integration; all geometry heights come from the export.
    """
    frame = RouteFrame(xy, left, distance, halfwidth)
    if np.asarray(road_y).shape != frame.distance.shape or not np.isfinite(road_y).all():
        raise ValueError('Invalid road elevations')
    surface = ExportedRoad(scene, ac)
    wear_textures(textures)
    if asphalt_material is None:
        asphalt_material = next((i for i, m in enumerate(scene['materials']) if m['name'] == 'asphalt'), None)
    if asphalt_material is None:
        raise ValueError('Road wear requires an existing asphalt material')
    original = dict(scene['materials'][asphalt_material])
    original_tint = np.asarray(original.get('albedo_multiplier', [1., 1., 1.]), dtype=float)
    patch_materials = []
    for i, factor in enumerate((.86, .92, 1.035)):
        index = material(f'asphalt_repair_{i}', original['texture'],
                         ambient=original.get('ambient', .4) * factor,
                         diffuse=original.get('diffuse', .48) * factor,
                         specular=.035)
        scene['materials'][index].update({k: original[k] for k in ('normal', 'normal_strength') if k in original})
        scene['materials'][index].update(roughness=.90, albedo_multiplier=(original_tint * factor).tolist())
        patch_materials.append(index)
    bitumen = material('road_resealed_joints', 'road_bitumen.png', ambient=.36, diffuse=.44, specular=.08)
    scene['materials'][bitumen].update(roughness=.78)
    iron = material('road_cast_iron_cover', 'road_cast_iron.png', ambient=.38, diffuse=.43, specular=.16)
    scene['materials'][iron].update(normal='road_cast_iron_normal_dx.png', normal_strength=.34, roughness=.72)
    rim = material('road_cast_iron_frame', 'road_iron_rim.png', ambient=.38, diffuse=.43, specular=.17)
    scene['materials'][rim].update(roughness=.67)
    rng = np.random.default_rng(seed)
    batches = Batches(add_mesh)
    start, end = float(frame.distance[0] + 34), float(frame.distance[-1] - 25)
    stats = {'asphalt_repair_sections': 0, 'road_resealed_cracks': 0, 'road_manhole_covers': 0,
             'road_wear_area_m2': 0., 'road_wear_max_lift_mm': 1.8,
             'road_wear_provenance': 'Illustrative deterministic placement; not mapped utility assets or surveyed road damage',
             'road_wear_materials': 'Unmodified ambientCG CC0 asphalt scan with restrained shader tint; original mathematical iron and bitumen patterns',
             'road_wear_conformity': 'Each overlay is clipped to exact exported 1ROAD_MAIN triangles; decorative and non-colliding',
             'road_wear_positions': []}
    metric_uv = lambda p: p / 1.25
    occupied = []
    for station in _stations(rng, start, end, 32, 24):
        # Covers are usually offset from the centreline. Their arrangement here
        # conveys construction scale; placement is explicitly not surveyed.
        lateral = float(rng.uniform(-.75, .75))
        center = frame.points([station], [lateral])[0]
        angle = np.linspace(0, 2 * math.pi, 65)[:-1]
        circle = lambda radius: Polygon(center + np.c_[np.cos(angle), np.sin(angle)] * radius)
        # DN625 denotes a nominal clear opening, not the whole visible frame.
        # This illustrative casting has a 0.68 m lid and 0.78 m outer frame.
        opening, frame_outer = circle(.34), circle(.39)
        rim_ring = frame_outer.difference(circle(.350))
        seat = frame_outer.buffer(.014, quad_segs=16).difference(opening)
        cover_uv = lambda p, c=center.copy(): (p - c) / .68 + .5
        area = surface.append(seat, 'ROAD_MANHOLE_JOINT', bitumen, batches, metric_uv, .0006)
        area += surface.append(rim_ring, 'ROAD_MANHOLE_FRAME', rim, batches, metric_uv, .0018)
        area += surface.append(opening, 'ROAD_MANHOLE_COVER', iron, batches, cover_uv, .0012)
        if area:
            stats['road_manhole_covers'] += 1
            stats['road_wear_area_m2'] += area
            occupied.append((station, 1.1))
            stats['road_wear_positions'].append({'kind': 'manhole', 'station_m': round(station, 3), 'lateral_m': round(lateral, 3),
                                                'nominal_clear_opening_m': .625, 'visible_lid_diameter_m': .68, 'outer_frame_diameter_m': .78})
    for station in _stations(rng, start, end, 19, 31):
        if any(abs(station - other) < radius + 3 for other, radius in occupied):
            continue
        length = float(rng.uniform(.85, 3.3))
        width = float(rng.uniform(.48, 1.3))
        extent = float(np.min(frame.width([station - length / 2, station + length / 2])) - width / 2 - .55)
        if extent <= .05:
            continue
        lateral = float(rng.uniform(-extent, extent))
        shear = float(rng.uniform(-.14, .14))
        count = max(6, math.ceil(length / .24))
        local_s = np.linspace(-length / 2, length / 2, count)
        sides = []
        for sign in (-1, 1):
            offsets = lateral + sign * width / 2 + local_s * shear + rng.uniform(-.015, .015, count)
            sides.append(frame.points(station + local_s, offsets))
        polygon = Polygon(np.vstack([sides[0], sides[1][::-1]]))
        # Short excavated repair outlines get narrow sealed seams. Repairs
        # deliberately differ in length, width, tone, location and UV phase.
        edge = polygon.buffer(.010, join_style=2).difference(polygon.buffer(-.009, join_style=2))
        center = frame.points([station], [lateral])[0]
        theta = float(rng.uniform(-.22, .22))
        rot = np.array([[math.cos(theta), -math.sin(theta)], [math.sin(theta), math.cos(theta)]])
        phase = rng.uniform(0, 5, 2)
        uv = lambda p, c=center.copy(), r=rot.copy(), ph=phase.copy(): (p-c) @ r / 1.25 + ph
        area = surface.append(polygon, 'ROAD_ASPHALT_PATCH', int(rng.choice(patch_materials)), batches, uv, .0007)
        area += surface.append(edge, 'ROAD_REPAIR_JOINT', bitumen, batches, metric_uv, .0013)
        if area:
            stats['asphalt_repair_sections'] += 1
            stats['road_wear_area_m2'] += area
            occupied.append((station, length / 2 + 1.2))
            stats['road_wear_positions'].append({'kind': 'repair', 'station_m': round(station, 3), 'lateral_m': round(lateral, 3), 'length_m': round(length, 3), 'width_m': round(width, 3)})
    for station in _stations(rng, start, end, 13, 23):
        if any(abs(station - other) < radius for other, radius in occupied):
            continue
        length = float(rng.uniform(.6, 2.15))
        lateral = float(rng.uniform(-1, 1) * max(.1, frame.width(station) - .8))
        local_s = np.linspace(station - length / 2, station + length / 2, 7)
        bends = rng.uniform(-.065, .065, 7) + np.linspace(0, rng.uniform(-.2, .2), 7)
        path = LineString(frame.points(local_s, lateral + bends))
        seam = path.buffer(float(rng.uniform(.006, .012)), quad_segs=3, cap_style=2, join_style=2)
        area = surface.append(seam, 'ROAD_RESEALED_CRACK', bitumen, batches, metric_uv, .0009)
        if area:
            stats['road_resealed_cracks'] += 1
            stats['road_wear_area_m2'] += area
            stats['road_wear_positions'].append({'kind': 'sealed_crack', 'station_m': round(station, 3), 'lateral_m': round(lateral, 3)})
    batches.finish()
    stats['road_wear_area_m2'] = round(stats['road_wear_area_m2'], 3)
    stats['road_wear_positions'].sort(key=lambda item: item['station_m'])
    return stats
