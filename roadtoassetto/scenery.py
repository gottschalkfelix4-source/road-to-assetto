"""Deterministic, openly sourced roadside detail for the prototype.

Tree placement is a conservative interpretation of OSM and aerial colour, not
a tree survey. All OSM highways and official building footprints are excluded.
The driving surface is never changed by these decorative, non-colliding meshes.
"""
from __future__ import annotations

import math
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
import rasterio
from scipy.ndimage import gaussian_filter, uniform_filter, binary_erosion, maximum_filter
from scipy.spatial import cKDTree
from shapely.geometry import LineString, MultiPoint, Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep


def select_route(points, start=None, end=None):
    """Crop a contiguous polyline using metres along its original geometry.

    Exact endpoints are interpolated before normal resampling and smoothing.
    An unspecified/full range returns the input unchanged, preserving v0.2
    road physics and spawn geometry. Short, reversed and nonfinite ranges fail.
    """
    points = np.asarray(points, dtype=float)
    lengths = np.linalg.norm(np.diff(points, axis=0), axis=1)
    if len(points) < 2 or not np.isfinite(points).all() or np.any(lengths < .01):
        raise ValueError('Route contains invalid or very short segments')
    distance = np.r_[0., np.cumsum(lengths)]
    total = float(distance[-1])
    start = 0. if start is None else float(start)
    end = total if end is None else float(end)
    if not np.isfinite([start, end]).all():
        raise ValueError('Route start and end must be finite metres')
    if start < 0 or end > total + .001 or end <= start:
        raise ValueError(f'Route must lie between 0 and {total:.2f} metres, with start before end')
    end = min(end, total)
    # Decimal route stations can subtract to 119.99999999999999 metres.
    # Ignore numerical roundoff only; genuinely shorter sections still fail.
    if end - start < 120. - 1e-8:
        raise ValueError('Selected route must be at least 120 metres long')
    if start == 0. and end == total:
        return points, {'route_start_m': 0., 'route_end_m': total, 'source_length_m': total}
    inner = points[(distance > start) & (distance < end)]
    endpoints = np.c_[np.interp([start, end], distance, points[:, 0]),
                      np.interp([start, end], distance, points[:, 1])]
    result = np.vstack([endpoints[0], inner, endpoints[1]])
    # A boundary less than a centimetre from a source vertex does not warrant
    # a tiny segment; preserve the exact requested boundary and omit that node.
    if len(result) > 2 and np.linalg.norm(result[1] - result[0]) < .01:
        result = np.delete(result, 1, axis=0)
    if len(result) > 2 and np.linalg.norm(result[-1] - result[-2]) < .01:
        result = np.delete(result, -2, axis=0)
    return result, {'route_start_m': start, 'route_end_m': end, 'source_length_m': total}


def surface_textures(textures: Path, quality='high'):
    """Create metric, seamless asphalt and small paving/vegetation materials."""
    rng = np.random.default_rng(82174)
    size = 1024 if quality == 'high' else 512
    grains = rng.normal(0, 1, (size, size))
    # Each asphalt repeat is 3 x 4 metres in prepare.strip's existing UV frame.
    micro = gaussian_filter(grains, .55, mode='wrap')
    micro /= max(float(micro.std()), .001)
    aggregate = gaussian_filter(rng.normal(0, 1, (size, size)), 1.8, mode='wrap')
    aggregate /= max(float(aggregate.std()), .001)
    broad = gaussian_filter(rng.normal(0, 1, (size, size)), size / 26, mode='wrap')
    broad /= max(float(broad.std()), .001)
    value = 66 + 5.5 * micro + 4.2 * aggregate + 2.4 * broad
    flecks = (micro > 1.45) & (aggregate > -.2)
    value[flecks] += 10
    rgb = np.clip(value[:, :, None] + np.array([-1., 0., 1.5]), 0, 255).astype('uint8')
    Image.fromarray(rgb).save(textures / 'asphalt.png')
    # A subtle DX normal carries aggregate highlights at driving distance.
    relief = gaussian_filter(grains, .9, mode='wrap')
    dx = (np.roll(relief, -1, axis=1) - np.roll(relief, 1, axis=1)) * .4
    dy = (np.roll(relief, -1, axis=0) - np.roll(relief, 1, axis=0)) * .4
    n = np.stack([-dx, dy, np.ones_like(dx)], axis=-1)
    n /= np.linalg.norm(n, axis=-1)[:, :, None]
    Image.fromarray(np.clip((n + 1) * 127.5, 0, 255).astype('uint8')).save(textures / 'asphalt_normal_dx.png')
    for filename, color in [('paving.png', (156, 150, 138)), ('kerb.png', (169, 168, 157)),
                            ('bark.png', (83, 73, 57)), ('repair.png', (54, 56, 57))]:
        pixels = np.clip(np.array(color)[None, None, :] + rng.normal(0, 3, (256, 256, 1)), 0, 255).astype('uint8')
        image = Image.fromarray(pixels)
        draw = ImageDraw.Draw(image)
        if filename == 'paving.png':
            # A 2 x 2 metre tile; rectangular stone joints, no road markings.
            for y in range(0, 257, 32):
                draw.line((0, y, 255, y), fill=(110, 108, 100), width=2)
                shift = 32 if (y // 32) % 2 else 0
                for x in range(-64 + shift, 257, 64):
                    draw.line((x, y, x, y + 32), fill=(110, 108, 100), width=2)
        elif filename == 'kerb.png':
            draw.line((0, 0, 0, 255), fill=(127, 126, 117), width=3)
        elif filename == 'bark.png':
            for x in range(0, 256, 13):
                draw.line((x, 0, x + int(rng.integers(-9, 10)), 255), fill=(62, 56, 44), width=2)
        image.save(textures / filename)
    photographic_vegetation_textures(textures)


def photographic_vegetation_textures(textures, assets=None):
    """Build open, airy branch cards from CC0 photographic leaf pixels.

    The source model's UV atlas has unrelated UV padding around its leaves.
    Explicit masks isolate four photographed leaves, so that atlas padding can
    never turn into opaque triangular fans when sampled by a whole quad.
    """
    assets = Path(assets) if assets else textures.parent / 'materials' / 'vegetation_photos'
    diffuse = Image.open(assets / 'tree_small_02_leaves_diff_1k.png').convert('RGB')
    opacity = Image.open(assets / 'tree_small_02_leaves_alpha_1k.png').convert('L')
    outlines = [
        [(701,144),(695,109),(703,74),(722,48),(746,46),(765,53),(771,76),(766,111),(747,142),(719,164)],
        [(735,286),(731,257),(743,220),(772,194),(811,177),(831,180),(845,203),(846,232),(832,261),(804,280),(765,295)],
        [(734,432),(733,408),(750,385),(783,366),(815,358),(838,368),(858,383),(858,405),(837,426),(800,442),(758,447)],
        [(592,482),(600,459),(628,445),(663,441),(687,448),(709,467),(721,489),(711,510),(688,524),(657,528),(626,518),(606,502)],
    ]
    leaves = []
    for outline in outlines:
        mask = Image.new('L', diffuse.size)
        ImageDraw.Draw(mask).polygon(outline, fill=255)
        pixels = np.asarray(diffuse)
        # Exclude bright UV padding which belongs to neighbouring atlas islands.
        valid = (np.asarray(mask) > 127) & (np.asarray(opacity) > 127)
        valid &= ~((pixels.max(axis=2) > 180) & (pixels.min(axis=2) > 120))
        valid = binary_erosion(valid, iterations=1)
        mask = Image.fromarray(valid.astype('uint8') * 255)
        image = diffuse.convert('RGBA')
        image.putalpha(mask)
        image = image.crop(mask.getbbox())
        leaves.append(image)
    rng = np.random.default_rng(72918)
    for variant in range(4):
        canvas = Image.new('RGBA', (512, 512), (48, 73, 30, 0))
        draw = ImageDraw.Draw(canvas)
        bend = int(rng.integers(-22, 23))
        draw.line([(256, 505), (250 + bend, 270), (270, 15)], fill=(91, 95, 49, 255), width=4)
        for row, y in enumerate(range(72, 448, 29)):
            for side in (-1, 1):
                leaf = leaves[(row + variant + (side == 1)) % len(leaves)]
                width = int(rng.integers(68, 88))
                leaf = leaf.resize((width, int(width * leaf.height / leaf.width)), Image.Resampling.LANCZOS)
                leaf = leaf.rotate(side * (28 + rng.uniform(-12, 12)), Image.Resampling.BICUBIC, expand=True)
                px = int(251 + bend * .5 + side * rng.uniform(43, 62))
                py = y + int(rng.integers(-10, 11))
                draw.line([(253 + bend * .5, y + 20), (px, py)], fill=(81, 89, 43, 255), width=3)
                canvas.alpha_composite(leaf, (px - leaf.width // 2, py - leaf.height // 2))
        pixels = np.array(canvas)
        # Restrained natural variation; no neon green or baked hard lighting.
        tint = [(.97, 1.00, .95), (1.03, 1.01, .94), (.92, .96, .91), (1.02, .98, .96)][variant]
        pixels[:, :, :3] = np.clip(pixels[:, :, :3].astype(float) * tint, 0, 255).astype('uint8')
        Image.fromarray(pixels).save(textures / f'foliage_{variant}.png')
    shutil.copyfile(assets / 'tree_bark_03_diff_1k.jpg', textures / 'bark_photo.jpg')
    shutil.copyfile(assets / 'tree_bark_03_nor_dx_1k.jpg', textures / 'bark_normal_dx.jpg')


def _number(value, fallback):
    try:
        result = float(str(value).split(';')[0].replace(' m', ''))
        return result if np.isfinite(result) and result > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def context_geometries(data_dir, main_way_ids):
    """Read road reservations, hedges, trees and conservative building hulls.

    LoD2 convex hulls deliberately also include buildings skipped by the visual
    exporter. This prevents a missing/invalid wall from becoming a tree plot.
    """
    project = Transformer.from_crs(4326, 25832, always_xy=True)
    doc = ET.parse(data_dir / 'area.osm').getroot()
    nodes = {}
    trees = []
    for node in doc.findall('node'):
        xy = project.transform(float(node.get('lon')), float(node.get('lat')))
        nodes[node.get('id')] = xy
        tags = {t.get('k'): t.get('v') for t in node.findall('tag')}
        if tags.get('natural') == 'tree':
            trees.append((xy, min(18., max(3.5, _number(tags.get('height'), 7.)))))
    roads = []
    other_roads = []
    hedges = []
    woods = []
    buildings = []
    for way in doc.findall('way'):
        tags = {t.get('k'): t.get('v') for t in way.findall('tag')}
        positions = [nodes[n.get('ref')] for n in way.findall('nd') if n.get('ref') in nodes]
        if len(positions) < 2:
            continue
        line = LineString(positions)
        if tags.get('highway'):
            # Preserve footpaths, steps, tracks and private driveways too.
            kind = tags['highway']
            fallback = {'footway': 2., 'path': 2., 'steps': 2., 'cycleway': 2.5,
                        'track': 3., 'service': 3.5, 'pedestrian': 4.}.get(kind, 6.)
            width = min(18., max(1., _number(tags.get('width'), fallback)))
            reserved = line.buffer(width / 2 + .8)
            roads.append(reserved)
            if int(way.get('id')) not in main_way_ids:
                other_roads.append(reserved)
        if tags.get('barrier') == 'hedge':
            hedges.append((line, min(3., max(.7, _number(tags.get('height'), 1.5)))))
        if len(positions) >= 4 and positions[0] == positions[-1]:
            polygon = Polygon(positions)
            if not polygon.is_valid:
                continue
            if tags.get('building'):
                buildings.append(polygon)
            if tags.get('natural') in ('wood', 'scrub') or tags.get('landuse') in ('forest', 'orchard'):
                woods.append(polygon)
    gml = '{http://www.opengis.net/gml/3.2}'
    bu = '{http://inspire.ec.europa.eu/schemas/bu-core3d/4.0}'
    for building in ET.parse(data_dir / 'buildings_lod2.gml').getroot().iter(bu + 'Building'):
        clouds = []
        for positions in building.iter(gml + 'posList'):
            if not positions.text:
                continue
            values = np.fromstring(positions.text, sep=' ')
            if len(values) % 3:
                continue
            triples = values.reshape(-1, 3)
            x, y = project.transform(triples[:, 1], triples[:, 0])
            clouds.append(np.c_[x, y])
        if clouds:
            polygon = MultiPoint(np.vstack(clouds)).convex_hull
            if isinstance(polygon, Polygon) and polygon.area > 1:
                buildings.append(polygon)
    return {'roads': unary_union(roads), 'other_roads': unary_union(other_roads),
            'buildings': unary_union(buildings), 'woods': unary_union(woods),
            'hedges': hedges, 'trees': trees, 'osm_highways': len(roads),
            'building_footprints': len(buildings)}


class Aerial:
    def __init__(self, path):
        with rasterio.open(path) as dataset:
            self.rgb = dataset.read([1, 2, 3]).transpose(1, 2, 0).astype('float32')
            self.transform = ~dataset.transform
            self.bounds = list(dataset.bounds)
            # The cached Hessen DOP20 RGBI coverage includes near infrared.
            # It separates canopy/grass from blue-grey shadows better than a
            # green-only threshold, but never supplies height by itself.
            nir = dataset.read(4).astype('float32') if dataset.count == 4 else None
        r, g, b = self.rgb.transpose(2, 0, 1)
        green = (g > r * 1.07) & (g > b * 1.08) & (g - r > 6) & (g > 32)
        self.green = uniform_filter(green.astype('float32'), size=7)
        self.ndvi = ((nir-r)/(nir+r+1.)) if nir is not None else None
        gray = self.rgb.mean(axis=2)
        # Contrast gives a modest canopy cue; height/species remain estimates.
        self.contrast = np.sqrt(np.maximum(uniform_filter(gray * gray, 7) - uniform_filter(gray, 7) ** 2, 0))

    def at(self, points):
        rows, columns = self._indices(points)
        return self.rgb[rows, columns], self.green[rows, columns], self.contrast[rows, columns]

    def _indices(self, points):
        points = np.atleast_2d(points)
        columns = (points[:, 0] * self.transform.a + points[:, 1] * self.transform.b + self.transform.c).astype(int)
        rows = (points[:, 0] * self.transform.d + points[:, 1] * self.transform.e + self.transform.f).astype(int)
        columns = np.clip(columns, 0, self.rgb.shape[1] - 1)
        rows = np.clip(rows, 0, self.rgb.shape[0] - 1)
        return rows, columns

    def vegetation_at(self, points):
        rows, columns = self._indices(points)
        confidence = self.green[rows, columns] > .22
        if self.ndvi is not None:
            confidence |= self.ndvi[rows, columns] > .19
        return confidence


def canopy_candidates(path, field, dem, aerial, line):
    """Optional DSM-DTM canopy peaks; source failures retain the RGB fallback.

    The surface model is sampled in its actual CRS and subtracted from the DGM,
    never from the road-graded mesh. Only vegetated local maxima within 80 m of
    the selected route survive. Crown clearance is checked by the caller after
    choosing a radius; roofs are not interpreted as vegetation there.
    """
    path = Path(path)
    info = {'status': 'absent', 'source': 'HVBG Hessen DOM1 minus DGM1',
            'height_method': 'smoothed local maxima of surface minus terrain, not surveyed individual trees'}
    if not path.is_file():
        return [], info
    try:
        with rasterio.open(path) as dataset:
            if dataset.crs is None or dataset.crs.to_epsg() != 25832:
                raise ValueError('DOM1 requires EPSG:25832')
            if dataset.transform.b or dataset.transform.d or not .5 <= dataset.res[0] <= 2 or not .5 <= dataset.res[1] <= 2:
                raise ValueError('DOM1 requires a north-up 0.5-2 m grid')
            region = field.intersection(line.buffer(80.)).intersection(box(*dataset.bounds).buffer(-2.))
            if region.is_empty:
                raise ValueError('DOM1 does not cover the selected route')
            minx,miny,maxx,maxy = region.bounds
            first_row,first_col = dataset.index(minx,maxy)
            last_row,last_col = dataset.index(maxx,miny)
            first_row,max_row=max(0,first_row),min(dataset.height,last_row+1)
            first_col,max_col=max(0,first_col),min(dataset.width,last_col+1)
            window = rasterio.windows.Window(first_col,first_row,max_col-first_col,max_row-first_row)
            surface = dataset.read(1,window=window,masked=True)
            transform = dataset.window_transform(window)
            cols,rows = np.meshgrid(np.arange(surface.shape[1])+.5,np.arange(surface.shape[0])+.5)
            positions = np.c_[transform.c+cols.ravel()*transform.a, transform.f+rows.ravel()*transform.e]
            import shapely
            valid = ~np.ma.getmaskarray(surface).ravel()
            valid &= np.isfinite(np.asarray(surface).ravel())
            valid &= shapely.contains_xy(region,positions[:,0],positions[:,1])
            height = np.zeros(surface.size,dtype=float)
            height[valid] = np.asarray(surface).ravel()[valid] - dem(positions[valid])
            valid &= (height >= 0.) & (height <= 38.)
            # Smooth metre-scale noise while retaining genuine crown tops.
            weights = gaussian_filter(valid.reshape(surface.shape).astype(float), .7)
            smoothed = gaussian_filter((height*valid).reshape(surface.shape), .7) / np.maximum(weights,.001)
            maxima = maximum_filter(smoothed,size=max(5,int(round(7/dataset.res[0]))),mode='nearest')
            peaks = (smoothed >= maxima-1e-5) & (smoothed >= 3.5) & (smoothed <= 30.) & valid.reshape(surface.shape)
            indices = np.flatnonzero(peaks)
            candidates = positions[indices]
            is_vegetation = aerial.vegetation_at(candidates)
            heights = smoothed.ravel()[indices]
            candidates,heights = candidates[is_vegetation],heights[is_vegetation]
            distances = cKDTree(np.asarray(line.coords)).query(candidates)[0] if len(candidates) else np.array([])
            order = np.lexsort((-heights,distances))
            info.update(status='available',resolution_m=list(dataset.res),candidate_peaks=len(order),
                        nir_assisted=aerial.ndvi is not None,height_reference='DHHN2016_NH for DOM1 and DGM1')
            return [(candidates[i],float(heights[i])) for i in order],info
    except (ValueError,rasterio.errors.RasterioError) as error:
        info.update(status='unusable',reason=str(error))
        return [],info


class Batches:
    """Keep each KN5 mesh comfortably below the 16-bit vertex-index limit."""
    def __init__(self, add_mesh):
        self.add_mesh = add_mesh
        self.groups = {}

    def append(self, name, material, vertices, faces, uv=None):
        vertices = np.asarray(vertices)
        key = (name, material)
        group = self.groups.setdefault(key, {'v': [], 'f': [], 'uv': [], 'part': 0})
        if len(group['v']) + len(vertices) > 63000:
            self._flush(key, group)
            group = {'v': [], 'f': [], 'uv': [], 'part': group['part'] + 1}
            self.groups[key] = group
        offset = len(group['v'])
        group['v'].extend(vertices.tolist())
        group['f'].extend((a + offset, b + offset, c + offset) for a, b, c in faces)
        group['uv'].extend(np.asarray(uv if uv is not None else vertices[:, [0, 2]] / 2).tolist())

    def _flush(self, key, group):
        if group['f']:
            self.add_mesh(f'{key[0]}_{key[1]}_{group["part"]}', group['v'], group['f'], key[1], group['uv'], False)

    def finish(self):
        for key, group in self.groups.items():
            self._flush(key, group)


def ellipsoid(center, radii, rng, segments=10, rings=5, irregularity=.1):
    """Closed low-polygon canopy with single poles and no zero-area triangles."""
    center = np.asarray(center, dtype=float)
    radii = np.asarray(radii, dtype=float)
    vertices = [center + [0., radii[1], 0.]]
    for ring in range(1, rings):
        theta = math.pi * ring / rings
        for sector in range(segments):
            phi = 2 * math.pi * sector / segments
            k = 1 + rng.uniform(-irregularity, irregularity)
            direction = np.array([math.sin(theta) * math.cos(phi), math.cos(theta), math.sin(theta) * math.sin(phi)])
            vertices.append(center + direction * radii * k)
    bottom = len(vertices)
    vertices.append(center - [0., radii[1], 0.])
    faces = []
    for sector in range(segments):
        nxt = (sector + 1) % segments
        faces.append((0, 1 + sector, 1 + nxt))
        for ring in range(rings - 2):
            a = 1 + ring * segments + sector
            b = 1 + ring * segments + nxt
            faces.extend([(a, a + segments, b), (b, a + segments, b + segments)])
        a = 1 + (rings - 2) * segments + sector
        b = 1 + (rings - 2) * segments + nxt
        faces.append((a, bottom, b))
    vertices = np.asarray(vertices)
    faces = np.asarray(faces)
    # The AC coordinate convention is right-handed; make every surface outward.
    triangles = vertices[faces]
    normal = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
    outward = triangles.mean(axis=1) - center
    reverse = np.sum(normal * outward, axis=1) < 0
    faces[reverse] = faces[reverse][:, [0, 2, 1]]
    return vertices, faces.tolist()


def tapered_branch(start, end, base_radius, tip_radius, segments=7):
    """Closed, metric-UV trunk/branch with valid caps and tapered silhouette."""
    start, end = np.asarray(start, dtype=float), np.asarray(end, dtype=float)
    axis = end - start
    length = np.linalg.norm(axis)
    axis /= length
    helper = np.array([1., 0., 0.]) if abs(axis[0]) < .8 else np.array([0., 0., 1.])
    u = np.cross(axis, helper)
    u /= np.linalg.norm(u)
    w = np.cross(axis, u)
    vertices, uv, faces = [], [], []
    for i in range(segments + 1):
        angle = 2 * np.pi * i / segments
        direction = math.cos(angle) * u + math.sin(angle) * w
        vertices.extend([start + direction * base_radius, end + direction * tip_radius])
        uv.extend([[i / segments, 0.], [i / segments, length / 1.8]])
        if i < segments:
            a = i * 2
            faces.extend([(a, a + 2, a + 1), (a + 1, a + 2, a + 3)])
    for center, radius, direction in [(start, base_radius, -axis), (end, tip_radius, axis)]:
        offset = len(vertices)
        vertices.append(center)
        uv.append([.5, .5])
        for i in range(segments):
            angle = 2 * np.pi * i / segments
            vertices.append(center + radius * (math.cos(angle) * u + math.sin(angle) * w))
            uv.append([.5 + .5 * math.cos(angle), .5 + .5 * math.sin(angle)])
        for i in range(segments):
            a, b, c = offset, offset + 1 + i, offset + 1 + (i + 1) % segments
            if np.dot(np.cross(vertices[b] - vertices[a], vertices[c] - vertices[a]), direction) < 0:
                b, c = c, b
            faces.append((a, b, c))
    return np.asarray(vertices), faces, np.asarray(uv)


def leaf_card(center, horizontal, vertical, width, height, mirror=False):
    """A gently bent foliage card, independently shaded on both sides.

    Sharing front/back vertices would sum opposing normals to zero. Each side
    therefore has its own vertices. All UVs sample only one prepared branch.
    """
    center = np.asarray(center)
    horizontal = np.asarray(horizontal) * width / 2
    vertical = np.asarray(vertical) * height / 2
    normal = np.cross(horizontal, vertical)
    normal /= np.linalg.norm(normal)
    p = np.array([center - horizontal - vertical, center + horizontal - vertical,
                  center + horizontal + vertical, center - horizontal + vertical,
                  center + normal * width * .065])
    uv = np.array([[0., 1.], [1., 1.], [1., 0.], [0., 0.], [.5, .5]])
    if mirror:
        uv[:, 0] = 1 - uv[:, 0]
    faces = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    faces += [(a + 5, c + 5, b + 5) for a, b, c in faces]
    return np.vstack([p, p]), faces, np.vstack([uv, uv])


def grow_plant(world, radius, height, rng, batches, bark, leaves, quality, shrub=False):
    """A branch skeleton and volumetric cutout foliage, never a solid crown."""
    world = np.asarray(world)
    center = world + [0., height * (.47 if shrub else .61), 0.]
    leaf_bottom = height * (.10 if shrub else .23)
    leaf_vertical = height * (.41 if shrub else .32)
    turn = rng.uniform(0., 2 * math.pi)
    bend = np.array([math.cos(turn), 0., math.sin(turn)]) * radius * .19
    if not shrub:
        # Fork well below the crown rather than producing an unbroken pole
        # almost to the tip. The photographed bark remains in metre-scale UVs.
        trunk = [world + [0., -.16, 0.], world + bend * .3 + [0., height * .17, 0.],
                 world + bend + [0., height * .36, 0.], world + bend * 1.3 + [0., height * .67, 0.]]
        radii = [max(.10, height * .017), height * .012, height * .007, .016]
        for i in range(3):
            v, f, uv = tapered_branch(trunk[i], trunk[i + 1], radii[i], radii[i + 1])
            batches.append('TREE_TRUNKS', bark, v, f, uv)
        for i in range(14 if quality == 'high' else 10):
            angle = turn + i * 2.39996 + rng.uniform(-.25, .25)
            h = height * rng.uniform(.20, .51)
            start = world + bend * (h / height) + [0., h, 0.]
            reach = radius * rng.uniform(.56,.79)
            end = world + np.array([math.cos(angle) * reach,
                                     max(h+height*.08,height*rng.uniform(.40,.79)),
                                     math.sin(angle) * reach])
            v, f, uv = tapered_branch(start, end, height * .0055, .014, 6)
            batches.append('TREE_BRANCHES', bark, v, f, uv)
            if i % 2 == 0:
                fork_start = start*.36 + end*.64
                fork_end = end + np.array([math.cos(angle+.8)*radius*.15,height*.10,
                                          math.sin(angle+.8)*radius*.15])
                v,f,uv=tapered_branch(fork_start,fork_end,height*.0025,.008,5)
                batches.append('TREE_BRANCHES',bark,v,f,uv)
    if shrub:
        count = 38 if quality=='high' else 26
    else:
        # More crown surface gets more small branch cards. Enlarging each card
        # with tree height would turn individual photographed leaves into giant
        # paddles on a tall DOM tree, even with a plausible trunk height.
        count = int(min(1900,300+radius*radius*40)) if quality=='high' else int(min(1150,210+radius*radius*26))
    locations=[];horizontals=[];verticals=[];widths=[];heights=[]
    for i in range(count):
        # A deformed ellipsoidal volume gives depth; its boundary remains inside
        # the checked crown envelope even after accounting for card corners.
        angle = turn + i * 2.39996 + rng.uniform(-.3, .3)
        y = rng.uniform(-.96, .96)
        lobe = 1 + .10*math.sin(3*angle+turn) + .06*math.sin(5*angle-y*2)
        radial = rng.uniform(.54,.84) if rng.random() < .68 else rng.uniform(.12,.60)
        spread = math.sqrt(max(.05, 1 - y * y)) * radial * lobe
        local = np.array([math.cos(angle) * radius * spread,
                          y * leaf_vertical,
                          math.sin(angle) * radius * spread])
        location = center + local
        card_width = min(max(radius*rng.uniform(.25,.39),.24),.72 if shrub else 1.05)
        card_height = min(card_width * rng.uniform(1.05, 1.30), height * .34)
        normal_angle = angle + rng.uniform(-.9, .9)
        # Cards face broadly out from the crown. Radial card *planes* make all
        # near-facing branches edge-on and leave a conspicuous hollow centre.
        horizontal = np.array([-math.sin(normal_angle), 0., math.cos(normal_angle)])
        tilt = rng.uniform(-1.22, 1.22)
        vertical = np.array([math.cos(normal_angle) * math.sin(tilt), math.cos(tilt),
                             math.sin(normal_angle) * math.sin(tilt)])
        # Branchlets lean in different directions inside the crown. Keeping
        # every photographic twig upright produces the look of hanging reeds.
        roll = rng.uniform(-1.35,1.35)
        horizontal,vertical=(horizontal*math.cos(roll)+vertical*math.sin(roll),
                             -horizontal*math.sin(roll)+vertical*math.cos(roll))
        locations.append(location);horizontals.append(horizontal);verticals.append(vertical)
        widths.append(card_width);heights.append(card_height)
    # Keep every random draw, card and material in its original order. Only the
    # arithmetic moves into arrays; no LOD or foliage-density reduction.
    locations=np.asarray(locations);horizontal=np.asarray(horizontals);vertical=np.asarray(verticals)
    widths=np.asarray(widths);heights=np.asarray(heights)
    card_normal=np.cross(horizontal,vertical)
    vertical_half=(widths*.5*np.abs(horizontal[:,1])+heights*.5*np.abs(vertical[:,1])
                   +widths*.065*np.abs(card_normal[:,1]))
    fit=np.minimum(1.,(height-leaf_bottom)*.48/np.maximum(vertical_half,.001))
    widths*=fit;heights*=fit;vertical_half*=fit
    locations[:,1]=np.clip(locations[:,1],world[1]+leaf_bottom+vertical_half,world[1]+height-vertical_half)
    horizontal=horizontal*widths[:,None]/2;vertical=vertical*heights[:,None]/2
    normals=np.cross(horizontal,vertical)
    # Explicit three-term sum matches the scalar dot product used by leaf_card.
    normals/=np.sqrt(normals[:,0]**2+normals[:,1]**2+normals[:,2]**2)[:,None]
    cards=np.stack((locations-horizontal-vertical,locations+horizontal-vertical,
                    locations+horizontal+vertical,locations-horizontal+vertical,
                    locations+normals*widths[:,None]*.065),axis=1)
    cards=np.concatenate((cards,cards),axis=1)
    _,faces,uv=leaf_card([0.,0.,0.],[1.,0.,0.],[0.,1.,0.],1.,1.)
    mirrored=uv.copy();mirrored[:,0]=1-mirrored[:,0]
    for i,vertices in enumerate(cards):
        batches.append('SHRUB_LEAVES' if shrub else 'TREE_LEAVES',leaves[i%len(leaves)],
                       vertices,faces,mirrored if i%2==0 else uv)


def prism(points, bottom, top, ac):
    """Closed 4-sided prism; independent face vertices give crisp kerb edges."""
    low = ac(points, bottom)
    high = ac(points, top)
    points = np.vstack([low, high])
    center = points.mean(axis=0)
    faces = [(0, 1, 2, 3), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    vertices = []
    triangles = []
    for quad in faces:
        p = points[list(quad)]
        if np.dot(np.cross(p[1] - p[0], p[2] - p[0]), p.mean(axis=0) - center) < 0:
            p = p[::-1]
        offset = len(vertices)
        vertices.extend(p)
        triangles.extend([(offset, offset + 1, offset + 2), (offset, offset + 2, offset + 3)])
    return np.asarray(vertices), triangles


def build_scenery(data_dir, textures, config, scene, material, add_mesh, ac,
                  xy, road_y, tangent, left, distance, road_polygon, dem, flatten,road_widths=None,edge_y=None,
                  street_plan=None, street_context=None, street_aerial=None, terrain_height_sampler=None,
                  construction_reservation=None):
    quality = config.get('scenery_quality', 'high')
    if quality not in ('standard', 'high'):
        raise ValueError('scenery_quality must be standard or high')
    density = float(config.get('vegetation_density', .65))
    if not np.isfinite(density) or not 0 <= density <= 1:
        raise ValueError('vegetation_density must be between 0 and 1')
    main_ids = {w['id'] for w in __import__('json').loads((data_dir / 'road_ways.json').read_text(encoding='utf-8'))}
    context = street_context if street_context is not None else context_geometries(data_dir, main_ids)
    aerial = street_aerial if street_aerial is not None else Aerial(data_dir / 'ortho.tif')
    line = LineString(xy)
    paving_footprints = [Polygon(section['xy']) for section in (street_plan or {}).get('paving_sections', [])]
    reserved = unary_union([context['roads'], context['buildings'].buffer(.75), road_polygon.buffer(.7),
                            *paving_footprints])
    if construction_reservation is not None and not construction_reservation.is_empty:
        reserved=reserved.union(construction_reservation.buffer(.15))
    reserved_prepared = prep(reserved)
    bounds = np.array(aerial.bounds)
    available_bounds = box(*(bounds + [8, 8, -8, -8]))
    field = line.buffer(115 if quality == 'high' else 90).intersection(available_bounds)
    rng = np.random.default_rng(19417)
    batches = Batches(add_mesh)
    leaves = [material(f'foliage_{i}', f'foliage_{i}.png', ambient=.33, diffuse=.58, specular=.025) for i in range(4)]
    for index in leaves:
        scene['materials'][index].update(alpha_test=True, alpha_ref=.45, roughness=.88, specular_exp=8.)
    bark = material('tree_bark', 'bark_photo.jpg', ambient=.27, diffuse=.5, specular=.02)
    scene['materials'][bark].update(normal='bark_normal_dx.jpg', normal_strength=.32, roughness=.95, specular_exp=8.)
    placements = []
    accepted_centers = []
    accepted_radii = []
    counts = {'osm_trees': 0, 'dom_trees': 0, 'aerial_trees': 0, 'aerial_shrubs': 0, 'hedge_sections': 0,
              'pavement_sections': 0, 'kerb_sections': 0, 'asphalt_repair_sections': 0}
    dom_candidates,dom_info = canopy_candidates(data_dir/'dom1.tif',field,dem,aerial,line)

    def vegetation_candidate(point, radius, height, source, shrub=False):
        p = Point(point)
        # Clearance includes the entire horizontal crown, not only its trunk.
        envelope = p.buffer(radius * 1.24 + .15)
        if not field.covers(envelope) or reserved_prepared.intersects(envelope):
            return False
        if accepted_centers:
            separation = np.linalg.norm(np.asarray(accepted_centers) - point, axis=1)
            if np.any(separation < (np.asarray(accepted_radii) + radius) * .84):
                return False
        samples = np.asarray(point) + np.array([[0, 0], [radius, 0], [-radius, 0], [0, radius], [0, -radius]])
        h = flatten(samples, dem(samples)) - .08
        # Tall measured surface peaks may stand on real wooded slopes. Their
        # trunks still start at the sampled ground and the whole crown must
        # clear buildings/roads; low inferred shrubs keep the stricter slope.
        dom_height = source.startswith('HVBG DOM1')
        slope_limit = max(2., min(height*.35, radius*1.5)) if dom_height else max(2.,radius*.75)
        if h.max() - h.min() > slope_limit:
            return False
        ground = float(h[0])
        world = ac(np.array([point]), [ground])[0]
        color = int(rng.integers(0, 4))
        plant_quality = quality if line.distance(p) <= 45 else 'standard'
        grow_plant(world, radius, height, rng, batches, bark, leaves[color:] + leaves[:color], plant_quality, shrub)
        accepted_centers.append(np.asarray(point))
        accepted_radii.append(radius)
        placements.append({'xy': np.asarray(point).round(3).tolist(), 'radius_m': round(radius, 3),
                           'height_m': round(height, 3), 'source': source, 'kind': 'shrub' if shrub else 'tree',
                           'height_source': 'DOM1-DGM1 local maximum' if dom_height else 'OSM tag or visual estimate'})
        return True

    if density > 0:
        for point, height in context['trees']:
            radius = min(3.7, height * .29)
            if vegetation_candidate(np.array(point), radius, height, 'OSM natural=tree'):
                counts['osm_trees'] += 1
        maximum_dom = int((560 if quality == 'high' else 340)*density)
        for point,height in dom_candidates:
            if counts['dom_trees'] >= maximum_dom:
                break
            if rng.random() > density:
                continue
            # Radius/species are still inferred; the recorded height is the
            # actual smoothed DOM-DGM peak, not a random garden-tree height.
            # A broadleaf crown has appreciable width compared with total tree
            # height. Reserve that whole footprint; never squeeze a tall tree
            # into a road-edge gap by shrinking its crown to a pole silhouette.
            radius = float(np.clip(height*.34,1.25,8.2))
            if vegetation_candidate(point,radius,height,'HVBG DOM1-DGM1 canopy peak with DOP20 vegetation evidence'):
                counts['dom_trees'] += 1
        minx, miny, maxx, maxy = field.bounds
        step = 4.3 if quality == 'high' else 5.2
        xx, yy = np.meshgrid(np.arange(minx, maxx, step), np.arange(miny, maxy, step))
        candidates = np.c_[xx.ravel(), yy.ravel()]
        candidates += rng.uniform(-step * .38, step * .38, candidates.shape)
        rgb, green, contrast = aerial.at(candidates)
        brightness = rgb.mean(axis=1)
        mask = (green > .67) & (brightness < 135) & (brightness > 25) & (rng.random(len(candidates)) < density)
        # Process nearest the street first; cap far scenery to bound export cost.
        nearest = cKDTree(xy).query(candidates)[0]
        order = np.argsort(nearest + rng.uniform(0, 10, len(candidates)))
        maximum = int((1350 if quality == 'high' else 750) * density)
        for i in order:
            if not mask[i] or counts['aerial_trees'] + counts['aerial_shrubs'] >= maximum:
                continue
            in_wood = context['woods'].covers(Point(candidates[i]))
            shrub = contrast[i] < 14 and not in_wood
            radius = float(rng.uniform(.75, 1.45) if shrub else rng.uniform(1.4, 2.8))
            height = float(rng.uniform(.7, 1.6) if shrub else rng.uniform(4.8, 8.5))
            if vegetation_candidate(candidates[i], radius, height, 'aerial green colour and local contrast', shrub):
                counts['aerial_shrubs' if shrub else 'aerial_trees'] += 1
        for hedge, height in context['hedges']:
            for s in np.arange(.7, hedge.length, 1.3):
                point = np.array(hedge.interpolate(s).coords[0])
                if vegetation_candidate(point, .65, height, 'OSM barrier=hedge', True):
                    counts['hedge_sections'] += 1

    from roadtoassetto.german_streets import build_street_edges
    half = np.full(len(xy),float(config['road_width_m'])/2) if road_widths is None else np.asarray(road_widths,dtype=float)/2
    if half.shape!=(len(xy),) or not np.isfinite(half).all() or np.any(half<=0):
        raise ValueError('Invalid scenery road width profile')
    edge_details=build_street_edges(context=context,xy=xy,road_y=road_y,left=left,distance=distance,
        half_width=half,road_polygon=road_polygon,dem=dem,flatten=flatten,ac=ac,batches=batches,
        material=material,scene=scene,textures=textures,aerial=aerial,quality=quality,
        data_dir=data_dir,edge_y=edge_y,plan=street_plan,ground_sampler=terrain_height_sampler)
    counts.update(edge_details['counts'])
    pavement_runs=edge_details['pavement_runs']
    batches.finish()
    counts['pavement_runs'] = len(pavement_runs)
    return {'scenery_quality': quality, 'vegetation_density': density, 'scenery_counts': counts,
            'scenery_placement': placements,
            'street_edges': {k:v for k,v in edge_details.items() if k not in ('counts','pavement_runs')},
            'pavement_runs': pavement_runs,
            'canopy_height_model': dom_info,
            'scenery_provenance': {'vegetation': 'OSM trees/hedges; optional HVBG DOM1-DGM1 canopy peaks with DOP20 RGBI vegetation evidence; DOP20 colour/contrast fallback',
                                   'clearance': f'All {context["osm_highways"]} OSM highway ways and official LoD2 + OSM building footprints',
                                   'paving': 'OSM-sided pavements with inferred widths/materials; German-style kerbs and drainage following the road edge elevation',
                                   'textures': 'Poly Haven CC0 tree_small_02 photographed leaves and tree_bark_03 bark; original procedural asphalt and paving',
                                   'vegetation_assets': ['https://polyhaven.com/a/tree_small_02', 'https://polyhaven.com/a/tree_bark_03'],
                                   'vegetation_license': 'https://polyhaven.com/license'},
            'scenery_limitations': ['DOM1-DGM1 supplies approximate canopy heights where available; individual tree positions, crown radius, species, RGB fallback and sidewalk dimensions remain estimates',
                                    'Decorative vegetation, kerbs and paving do not add physical collision',
                                    'Drains, kerb heights and pavement construction details are inferred, not surveyed']}
