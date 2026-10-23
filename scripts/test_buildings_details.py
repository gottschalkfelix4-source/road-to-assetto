"""Focused behavioral checks for source-preserving facade detail generation."""
from pathlib import Path
import json
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import LineString

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.buildings import (build_lod2,clip_height_band,eave_prism,exportable_mesh,
                                     facade_uv,floor_layout,triangulate_face)


def area(vertices,faces):
    t=vertices[np.asarray(faces)]
    return float(np.linalg.norm(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0]),axis=1).sum()/2)


def section_photo_fixture(data):
    """Two actual source buildings, only one within the chosen route corridor."""
    textures=data/'textures';textures.mkdir()
    source=data/'materials/PaintedPlaster006';source.mkdir(parents=True)
    Image.new('RGB',(16,16),(210,202,190)).save(source/'PaintedPlaster006_2K-JPG_Color.jpg')
    Image.new('RGB',(16,16),(128,128,255)).save(source/'PaintedPlaster006_2K-JPG_NormalDX.jpg')
    Image.new('RGB',(30,30),(165,34,68)).save(data/'photo.png')
    transform=Transformer.from_crs(25832,4326,always_xy=True)
    def encoded(points):
        lon,lat=transform.transform(points[:,0],points[:,1])
        return ' '.join(str(x) for x in np.c_[lat,lon,points[:,2]].ravel())
    members=[]
    for name,offset in (('near',0),('far',400)):
        origin=np.array([462000.+offset,5556400.,230.])
        front=np.array([[0,0,0],[8,0,0],[8,0,6],[4,0,9],[0,0,6],[0,0,0]])+origin
        roof=np.array([[0,0,6],[4,0,9],[4,8,9],[0,8,6],[0,0,6]])+origin
        polygons=''.join(f'<gml:Polygon gml:id="{name}-{kind}"><gml:exterior><gml:LinearRing><gml:posList>{encoded(points)}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>' for kind,points in [('front',front),('roof',roof)])
        members.append(f'<wfs:member><bu:Building gml:id="{name}">'+polygons+'</bu:Building></wfs:member>')
    (data/'buildings_lod2.gml').write_text('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:bu="http://inspire.ec.europa.eu/schemas/bu-core3d/4.0">'+''.join(members)+'</wfs:FeatureCollection>')
    override={'building_id':'far','surface_id':'far-front','texture':'photo.png','license':'CC0','source':'test fixture'}
    def build(value,allow=False):
        (data/'facade_overrides.json').write_text(json.dumps({'facades':[value]}))
        scene={'materials':[]}
        def material(name,texture,**kwargs):
            scene['materials'].append(dict(name=name,texture=texture,**kwargs))
            return len(scene['materials'])-1
        line=LineString([(461990,5556395),(462020,5556395)])
        return build_lod2(data,textures,scene,material,lambda *args:None,lambda xy,z:np.c_[xy,z],
                          line,line.buffer(1),None,(461950,5556350,462500,5556450),
                          allow_outside_photo_overrides=allow)
    return override,build


class BuildingDetailTests(unittest.TestCase):
    def test_same_floor_height_on_rectangular_and_gabled_walls(self):
        normal=np.array([0.,-1.,0.])
        rectangle=np.array([[0,0,100],[6,0,100],[6,0,106],[0,0,106]])
        gable=np.vstack([rectangle,[3,0,109]])
        np.testing.assert_allclose(facade_uv(rectangle,normal,100,3),facade_uv(gable,normal,100,3)[:4])
        np.testing.assert_allclose(facade_uv(rectangle,normal,100,3)[:,1],[1,1,-1,-1])

    def test_height_bands_preserve_gable_area_holes_and_outward_winding(self):
        outer=[[0,0,0],[8,0,0],[8,0,6],[4,0,9],[0,0,6],[0,0,0]]
        hole=[[3,0,1],[3,0,2],[5,0,2],[5,0,1],[3,0,1]]
        vertices,faces,normal=triangulate_face([outer,hole])
        total=0
        for lo,hi in ((-np.inf,3),(3,6),(6,np.inf)):
            points,triangles=clip_height_band(vertices,faces,lo,hi)
            total+=area(points,triangles)
            self.assertTrue(np.all(points[:,2]>=lo-1e-8))
            self.assertTrue(np.all(points[:,2]<=hi+1e-8))
            p=points[np.asarray(triangles)]
            self.assertTrue(np.all(np.cross(p[:,1]-p[:,0],p[:,2]-p[:,0])@normal>0))
            # No triangle centroid may fill the source wall's missing doorway.
            centers=p.mean(axis=1)
            self.assertFalse(np.any((centers[:,0]>3)&(centers[:,0]<5)&(centers[:,2]>1)&(centers[:,2]<2)))
        self.assertAlmostEqual(total,area(vertices,faces),places=6)

    def test_floor_layout_uses_eaves_instead_of_roof_ridge(self):
        roof=np.array([[0,0,106],[3,0,109],[3,6,109],[0,6,106]])
        height,cap=floor_layout(100,[(roof,None,None)],[])
        self.assertEqual(height,3)
        self.assertEqual(cap,106)

    def test_fascia_faces_are_outward_and_bounded_on_source_edge(self):
        a=np.array([1,2,8.]);b=np.array([5,5,8.])
        v,f,uv=eave_prism(a,b)
        self.assertTrue(np.isfinite(uv).all())
        self.assertAlmostEqual(float(v[:,2].max()),8.)
        self.assertAlmostEqual(float(v[:,2].min()),7.88)
        t=v[np.asarray(f)];center=v.mean(axis=0)
        normals=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])
        self.assertTrue(np.all(np.sum(normals*(t.mean(axis=1)-center),axis=1)>0))

    def test_export_rounding_removes_slivers_and_unreferenced_vertices(self):
        points=np.array([[237.786791,20.6214,-23.94068],
                         [237.786791,14.23608,-23.94068],
                         [237.786793,20.6214,-23.94068],
                         [238.78679,20.6214,-23.94068],
                         [238.78679,14.23608,-23.94068],
                         [239.78679,20.6214,-23.94068]])
        uv=np.array([[0,0],[0,1],[.001,0],[0,0],[0,1],[1,0]])
        v,f,result_uv=exportable_mesh(points,[(0,1,2),(3,4,5)],uv)
        self.assertEqual(f,[[0,1,2]])
        self.assertEqual(len(v),3)
        np.testing.assert_allclose(result_uv,uv[3:])
        stored=np.asarray(np.round(v,5),dtype=np.float32)
        cross=np.cross(stored[1]-stored[0],stored[2]-stored[0])
        self.assertGreater(np.linalg.norm(cross),1e-7)

    def test_section_can_ignore_valid_outside_photo_and_default_remains_strict(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='section-photo-') as temp:
            override,build=section_photo_fixture(Path(temp))
            with self.assertRaisesRegex(ValueError,'unknown or excluded wall'):
                build(override)
            stats=build(override,allow=True)
            self.assertEqual(stats['buildings'],1)
            self.assertEqual(stats['photo_facades'],0)
            self.assertEqual(stats['photo_facades_outside_segment'],1)
            self.assertEqual(stats['photo_sources'],[])

    def test_section_still_rejects_unknown_ids_and_nonwall_photo_targets(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='section-photo-') as temp:
            override,build=section_photo_fixture(Path(temp))
            for change in ({'building_id':'missing'},{'surface_id':'missing'},{'surface_id':'far-roof'}):
                with self.subTest(change=change),self.assertRaises(ValueError):
                    build(dict(override,**change),allow=True)

    def test_section_still_validates_outside_photo_file_and_provenance(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='section-photo-') as temp:
            data=Path(temp);override,build=section_photo_fixture(data)
            (data/'broken.jpg').write_bytes(b'not an image')
            for change in ({'texture':'missing.jpg'},{'texture':'broken.jpg'},{'license':''},{'source':'  '}):
                with self.subTest(change=change),self.assertRaises(ValueError):
                    build(dict(override,**change),allow=True)

    def test_build_keeps_photo_mapping_and_roof_coordinates(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='building-detail-') as temp:
            data=Path(temp);textures=data/'textures';textures.mkdir()
            source=data/'materials/PaintedPlaster006';source.mkdir(parents=True)
            Image.new('RGB',(16,16),(210,202,190)).save(source/'PaintedPlaster006_2K-JPG_Color.jpg')
            Image.new('RGB',(16,16),(150,140,252)).save(source/'PaintedPlaster006_2K-JPG_NormalDX.jpg')
            Image.new('RGB',(30,30),(165,34,68)).save(data/'photo.png')
            origin=np.array([462000.,5556400.,230.])
            front=np.array([[0,0,0],[8,0,0],[8,0,6],[4,0,9],[0,0,6],[0,0,0]])+origin
            roof=np.array([[0,0,6],[4,0,9],[4,8,9],[0,8,6],[0,0,6]])+origin
            transform=Transformer.from_crs(25832,4326,always_xy=True)
            def encoded(points):
                lon,lat=transform.transform(points[:,0],points[:,1])
                return ' '.join(str(x) for x in np.c_[lat,lon,points[:,2]].ravel())
            polygons=''.join(f'<gml:Polygon gml:id="{name}"><gml:exterior><gml:LinearRing><gml:posList>{encoded(points)}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>' for name,points in [('front',front),('roof',roof)])
            (data/'buildings_lod2.gml').write_text('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:bu="http://inspire.ec.europa.eu/schemas/bu-core3d/4.0"><wfs:member><bu:Building gml:id="house">'+polygons+'</bu:Building></wfs:member></wfs:FeatureCollection>')
            (data/'facade_overrides.json').write_text(json.dumps({'facades':[{'building_id':'house','surface_id':'front','texture':'photo.png','license':'CC0','source':'test fixture','flip_u':True}]}))
            scene={'materials':[],'meshes':[]}
            def material(name,texture,**kwargs):
                scene['materials'].append(dict(name=name,texture=texture,**kwargs))
                return len(scene['materials'])-1
            def add(name,points,faces,mid,uv):
                scene['meshes'].append(dict(name=name,points=np.asarray(points),faces=faces,material=mid,uv=np.asarray(uv)))
            line=LineString([(461990,5556395),(462020,5556395)])
            stats=build_lod2(data,textures,scene,material,add,lambda xy,z:np.c_[xy,z],line,line.buffer(1),lambda xy:np.zeros(len(xy)),(461950,5556350,462050,5556450))
            self.assertEqual(stats['photo_facades'],1)
            self.assertEqual(stats['procedural_entrances'],0)
            self.assertEqual(stats['photo_sources'][0]['source'],'test fixture')
            photo=next(m for m in scene['meshes'] if m['name'].startswith('LOD2_PHOTO'))
            self.assertTrue(np.all(photo['uv']>=-1e-8) and np.all(photo['uv']<=1+1e-8))
            np.testing.assert_allclose(photo['uv'][:,0],1-(photo['points'][:,0]-462000)/8,atol=1e-5)
            roofs=next(m for m in scene['meshes'] if m['name'].startswith('LOD2_ROOFS'))
            np.testing.assert_allclose(roofs['uv'][:,0],(roofs['points'][:,0]-461950)/100)
            np.testing.assert_allclose(roofs['uv'][:,1],1-(roofs['points'][:,1]-5556350)/100)
            self.assertFalse(any(m['name'].startswith('LOD2_WALLS') for m in scene['meshes']))


if __name__=='__main__':
    unittest.main(verbosity=2)
