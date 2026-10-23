"""Behavioural checks for cropping, foliage geometry and source exclusions."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
from collections import Counter
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image
from pyproj import Transformer
import rasterio
from rasterio.transform import from_origin
from shapely.geometry import LineString, Point, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.scenery import (Batches, build_scenery, context_geometries, ellipsoid,
                                  select_route, grow_plant, photographic_vegetation_textures,
                                  Aerial, canopy_candidates)
from roadtoassetto.kn5 import normals


class SceneryTests(unittest.TestCase):
    def test_full_route_is_exact_and_subroute_has_interpolated_endpoints(self):
        points = np.array([[0., 0.], [100., 0.], [100., 100.], [200., 100.]])
        full, meta = select_route(points)
        np.testing.assert_array_equal(full, points)
        cropped, meta = select_route(points, 35., 230.)
        np.testing.assert_array_equal(cropped, [[35., 0.], [100., 0.], [100., 100.], [130., 100.]])
        self.assertEqual(meta['source_length_m'], 300.)
        self.assertEqual(meta['route_end_m'] - meta['route_start_m'], 195.)

    def test_invalid_and_too_short_routes_are_rejected(self):
        from unittest.mock import patch
        from webapp.catalog import validate_request
        points = [[0, 0], [350, 0]]
        for start, end in [(-1, 200), (0, 351), (200, 150), (0, 119.99), (0, 119.99999), (float('nan'), 300), (0, float('inf'))]:
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                select_route(points, start, end)
        start, end = 72.60873975476291, 192.6087397547629
        self.assertLess(end-start, 120.)  # Exact reported floating-point case.
        selected, metadata = select_route(points, start, end)
        np.testing.assert_allclose(selected, [[start, 0], [end, 0]], atol=1e-12, rtol=0)
        self.assertEqual(metadata['route_start_m'], start)
        self.assertEqual(metadata['route_end_m'], end)
        with patch('webapp.catalog.template', return_value={'length_m': 350.}):
            result = validate_request({'template_id':'altenhain','start_m':start,'end_m':end})
            self.assertEqual((result['start_m'], result['end_m']), (start, end))
            with self.assertRaises(ValueError):
                validate_request({'template_id':'altenhain','start_m':0.,'end_m':119.99999})

    def test_canopies_are_closed_have_outward_faces_and_valid_normals(self):
        for segments, rings in [(8, 4), (11, 6)]:
            v, f = ellipsoid([12, 6, 30], [2.5, 4., 2.1], np.random.default_rng(8), segments, rings)
            n = normals(v, f)
            self.assertTrue(np.isfinite(n).all())
            edges = Counter(tuple(sorted((a, b))) for a, b, c in f for a, b in [(a, b), (b, c), (c, a)])
            self.assertEqual(set(edges.values()), {2})
            triangles = v[np.asarray(f)]
            cross = np.cross(triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0])
            self.assertTrue(np.all(np.sum(cross * (triangles.mean(axis=1) - [12, 6, 30]), axis=1) > 0))

    def test_batches_never_overflow_kn5_indices(self):
        output = []
        batch = Batches(lambda *args: output.append(args))
        vertices, faces = ellipsoid([0, 2, 0], [1, 2, 1], np.random.default_rng(1), 11, 6)
        for _ in range(1200):
            batch.append('CANOPY', 0, vertices, faces)
        batch.finish()
        self.assertGreater(len(output), 1)
        self.assertEqual(sum(len(m[1]) for m in output), len(vertices) * 1200)
        for name, v, f, material, uv, collision in output:
            self.assertLessEqual(len(v), 63000)
            self.assertLess(np.asarray(f).max(), len(v))

    def test_leaf_cards_are_volumetric_with_valid_export_normals_and_checked_envelope(self):
        for shrub, radius, height in [(False, 2.4, 7.2), (False, 7.48, 22.),
                                      (False, 8.2, 30.), (True, 1.1, 1.3)]:
            meshes = []
            batch = Batches(lambda *args: meshes.append(args))
            grow_plant(np.zeros(3), radius, height, np.random.default_rng(71), batch, 0, [1,2,3,4], 'high', shrub)
            batch.finish()
            leaves = [m for m in meshes if 'LEAVES' in m[0]]
            self.assertEqual(len(leaves), 4)
            self.assertGreater(sum(len(m[1]) for m in leaves), 200)
            for name, vertices, faces, mid, uv, collision in meshes:
                # Exactly the precision at which the generator and KN5 operate.
                vertices = np.asarray(vertices).round(5).astype('float32')
                n = normals(vertices, faces)
                self.assertTrue(np.isfinite(n).all(), name)
                self.assertTrue(np.all(np.linalg.norm(n, axis=1) > .99), name)
                triangle = vertices[np.asarray(faces)]
                self.assertTrue(np.all(np.linalg.norm(np.cross(triangle[:,1]-triangle[:,0], triangle[:,2]-triangle[:,0]),axis=1) > 1e-7), name)
                self.assertLessEqual(float(np.linalg.norm(vertices[:,[0,2]], axis=1).max()), radius*1.24+.15)
                self.assertFalse(collision)
            if not shrub:
                trunk = [m for m in meshes if 'TRUNKS' in m[0]][0]
                self.assertLess(np.asarray(trunk[1])[:,1].min(), -.1)
                foliage=np.vstack([np.asarray(m[1]) for m in leaves])
                self.assertLess(foliage[:,1].min(),height*.36)
                self.assertLessEqual(foliage[:,1].max(),height+.01)
                self.assertGreater(np.ptp(foliage[:,0]),radius*1.45)
                self.assertGreater(np.ptp(foliage[:,2]),radius*1.45)
                self.assertGreater(np.ptp(foliage[:,1]),height*.57)
                # The individual photographed branch never grows with a tall
                # tree into metre-wide individual leaves. Each card carries
                # twenty-six leaves and remains <=1.05m wide.
                for mesh in leaves:
                    cards=np.asarray(mesh[1]).reshape(-1,10,3)
                    self.assertTrue(np.all(np.linalg.norm(cards[:,1]-cards[:,0],axis=1)<=1.05001))

    def test_photographic_foliage_has_alpha_holes_and_clean_leaf_edges(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results', prefix='leaf-test-') as folder:
            output = Path(folder)
            photographic_vegetation_textures(output, ROOT/'data/altenhain/materials/vegetation_photos')
            for i in range(4):
                pixels = np.asarray(Image.open(output/f'foliage_{i}.png'))
                self.assertEqual(pixels.shape, (512, 512, 4))
                coverage = (pixels[:,:,3] > 127)
                self.assertGreater(float(coverage.mean()), .15)
                self.assertLess(float(coverage.mean()), .55)
                self.assertLess(float((pixels[:,:,:3][coverage].min(axis=1) > 180).mean()), .001)
            self.assertGreater((output/'bark_photo.jpg').stat().st_size, 100000)

    def test_optional_dom_uses_canopy_height_excludes_roofs_and_keeps_fallback(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='dom-test-') as folder:
            data=Path(folder)
            fixtures(data)
            x,y=np.meshgrid(np.arange(461900,462200)+.5,5556500-np.arange(200)-.5)
            surface=np.full(x.shape,220.)
            # The largest peak is a roof: it must never become a tree. Another
            # canopy lies beyond the explicit 80 m DOM vegetation range.
            for px,py,h in [(461995,5556430,12),(462130,5556427,15),(461930,5556375,8),
                            (462045,5556430,19),(462130,5556490,23)]:
                surface+=h*np.exp(-((x-px)**2+(y-py)**2)/12.)
            with rasterio.open(data/'dom1.tif','w',driver='GTiff',width=300,height=200,count=1,
                               dtype='float32',crs='EPSG:25832',transform=from_origin(461900,5556500,1,1)) as out:
                out.write(surface.astype('float32'),1)
            meta,meshes=generate(data,1.)
            self.assertEqual(meta['canopy_height_model']['status'],'available')
            self.assertGreater(meta['scenery_counts']['dom_trees'],0)
            measured=[p for p in meta['scenery_placement'] if p['height_source']=='DOM1-DGM1 local maximum']
            self.assertEqual(len(measured),meta['scenery_counts']['dom_trees'])
            self.assertTrue(all(5<p['height_m']<16 for p in measured))
            self.assertTrue(all(abs(p['xy'][1]-5556400)<80 for p in measured))
            context=context_geometries(data,{1})
            import shapely
            for name,vertices,faces,mid,uv,collision in meshes:
                if name.startswith(('TREE_','SHRUB_')):
                    v=np.asarray(vertices);world=np.c_[v[:,0]+461900.,5556300.-v[:,2]]
                    self.assertFalse(shapely.contains_xy(context['roads'],world[:,0],world[:,1]).any(),name)
                    self.assertFalse(shapely.contains_xy(context['buildings'],world[:,0],world[:,1]).any(),name)
            # Invalid or missing optional coverage does not disable the builder.
            (data/'dom1.tif').write_bytes(b'not a TIFF')
            fallback,_=generate(data,0.)
            self.assertEqual(fallback['canopy_height_model']['status'],'unusable')
            (data/'dom1.tif').unlink()
            absent,_=generate(data,0.)
            self.assertEqual(absent['canopy_height_model']['status'],'absent')

    def test_aerial_vegetation_is_repeatable_and_clear_of_all_roads_and_buildings(self):
        parent = ROOT / 'test-results'
        parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent, prefix='scenery-test-') as folder:
            data = Path(folder)
            self.assertTrue(data.resolve().is_relative_to(parent.resolve()))
            fixtures(data)
            context = context_geometries(data, {1})
            self.assertEqual(context['osm_highways'], 3)
            self.assertTrue(context['roads'].covers(Point(462080, 5556440)))
            self.assertTrue(context['buildings'].covers(Point(462045, 5556430)))
            first, meshes = generate(data, .8)
            second, _ = generate(data, .8)
            self.assertEqual(first['scenery_placement'], second['scenery_placement'])
            self.assertGreater(len(first['scenery_placement']), 10)
            for plant in first['scenery_placement']:
                footprint = Point(plant['xy']).buffer(plant['radius_m'] * 1.2)
                self.assertFalse(context['roads'].intersects(footprint))
                self.assertFalse(context['buildings'].intersects(footprint))
            self.assertGreater(first['scenery_counts']['pavement_sections'], 0)
            self.assertGreater(first['scenery_counts']['pavement_runs'], 0)
            for run in first['pavement_runs']:
                self.assertGreaterEqual(run['end_m'] - run['start_m'], 7.)
            for name, vertices, faces, mid, uv, collision in meshes:
                self.assertFalse(collision)
                self.assertTrue(np.isfinite(normals(vertices, faces)).all(), name)
                if name.startswith('PAVEMENT_DETAIL'):
                    # Each prism contributes 24 vertices. Check the complete
                    # footprint, including side-street intersections.
                    for start in range(0, len(vertices), 24):
                        v = np.asarray(vertices[start:start + 24])
                        world = np.c_[v[:, 0] + 461900., 5556300. - v[:, 2]]
                        footprint = __import__('shapely').MultiPoint(world).convex_hull
                        self.assertFalse(context['other_roads'].intersects(footprint))
                        self.assertFalse(context['buildings'].intersects(footprint))
                        self.assertLessEqual(v[:,1].min(), -.30)
                        self.assertLessEqual(v[:,1].max(), .10)
                if name.startswith(('TREE_', 'SHRUB_')):
                    # Check generated geometry, not just metadata/trunk centres.
                    v = np.asarray(vertices)
                    world = np.c_[v[:,0]+461900., 5556300.-v[:,2]]
                    import shapely
                    self.assertFalse(shapely.contains_xy(context['roads'], world[:,0], world[:,1]).any(), name)
                    self.assertFalse(shapely.contains_xy(context['buildings'], world[:,0], world[:,1]).any(), name)
            disabled, _ = generate(data, 0.)
            self.assertEqual(disabled['scenery_placement'], [])


def fixtures(data):
    inverse = Transformer.from_crs(25832, 4326, always_xy=True)
    osm = ET.Element('osm')
    coordinates = [(461910, 5556400), (462190, 5556400), (462080, 5556310), (462080, 5556490),
                   (461920, 5556460), (462180, 5556460), (461960, 5556440)]
    for i, (x, y) in enumerate(coordinates, 1):
        lon, lat = inverse.transform(x, y)
        node = ET.SubElement(osm, 'node', id=str(i), lon=str(lon), lat=str(lat))
        if i == 7:
            ET.SubElement(node, 'tag', k='natural', v='tree')
    for i, refs, kind in [(1, [1, 2], 'residential'), (2, [3, 4], 'service'), (3, [5, 6], 'footway')]:
        way = ET.SubElement(osm, 'way', id=str(i))
        for ref in refs:
            ET.SubElement(way, 'nd', ref=str(ref))
        ET.SubElement(way, 'tag', k='highway', v=kind)
    ET.ElementTree(osm).write(data / 'area.osm', encoding='utf-8')
    (data / 'road_ways.json').write_text(json.dumps([{'id': 1}]), encoding='utf-8')
    gml = '{http://www.opengis.net/gml/3.2}'
    bu = '{http://inspire.ec.europa.eu/schemas/bu-core3d/4.0}'
    doc = ET.Element('root')
    building = ET.SubElement(doc, bu + 'Building')
    points = []
    for x, y in [(462035, 5556420), (462055, 5556420), (462055, 5556440), (462035, 5556440), (462035, 5556420)]:
        lon, lat = inverse.transform(x, y)
        points.extend([lat, lon, 220.])
    ET.SubElement(building, gml + 'posList').text = ' '.join(map(str, points))
    ET.ElementTree(doc).write(data / 'buildings_lod2.gml', encoding='utf-8')
    rng = np.random.default_rng(71)
    noise = rng.uniform(-31, 31, (200, 300, 1))
    rgb = np.clip(np.array([40., 87., 37.]) + noise, 0, 255).astype('uint8')
    # Neutral paved margins beside the main road must still stop at service
    # road crossings; the rest remains aerial green for vegetation placement.
    rgb[91:109] = np.clip(142. + noise[91:109] / 4, 0, 255)
    with rasterio.open(data / 'ortho.tif', 'w', driver='GTiff', width=300, height=200, count=3,
                       dtype='uint8', crs='EPSG:25832', transform=from_origin(461900, 5556500, 1, 1)) as out:
        out.write(rgb.transpose(2, 0, 1))


def generate(data, density):
    scene = {'materials': [], 'meshes': []}
    def material(name, texture, **kwargs):
        scene['materials'].append({'name': name, 'texture': texture, **kwargs})
        return len(scene['materials']) - 1
    xy = np.c_[np.arange(461920., 462181.), np.full(261, 5556400.)]
    road_y = np.full(len(xy), 220.)
    tangent = np.tile([1., 0.], (len(xy), 1))
    left = np.tile([0., 1.], (len(xy), 1))
    def ac(p, h):
        p = np.asarray(p)
        return np.c_[p[:, 0] - 461900, np.asarray(h) - 220., -(p[:, 1] - 5556300)]
    meta = build_scenery(data, data, {'road_width_m': 6., 'scenery_quality': 'standard', 'vegetation_density': density},
                         scene, material, lambda *args: scene['meshes'].append(args), ac, xy, road_y,
                         tangent, left, np.arange(261.), LineString(xy).buffer(3),
                         lambda points: np.full(len(points), 220.), lambda points, height: height)
    return meta, scene['meshes']


if __name__ == '__main__':
    unittest.main(verbosity=2)
