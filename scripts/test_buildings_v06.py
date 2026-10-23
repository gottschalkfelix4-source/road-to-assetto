"""Source containment and practical ground contact of v0.6 architectural detail."""
from pathlib import Path
import hashlib
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import LineString,Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.buildings import (atlas_uv,build_lod2,door_textures,doorway_components,
                                     entrance_layout,exportable_mesh,facade_box,foundation_strips,window_layout)


class ArchitecturalDetailTests(unittest.TestCase):
    def setUp(self):
        self.wall=np.array([[0,0,100],[12,0,100],[12,0,106],[0,0,106],[0,0,100]],float)
        self.normal=np.array([0.,-1.,0.])

    def test_door_threshold_follows_ground_and_rejects_uncertain_heights(self):
        source=self.wall.copy()
        layout=entrance_layout([self.wall],self.normal,100,2,lambda p:np.full(len(p),100.22))
        self.assertIsNotNone(layout)
        self.assertAlmostEqual(layout['bottom'],100.255)
        self.assertAlmostEqual(layout['ground'],100.22)
        np.testing.assert_array_equal(self.wall,source)
        for height in (99.2,100.9):
            self.assertIsNone(entrance_layout([self.wall],self.normal,100,2,lambda p:np.full(len(p),height)))
        self.assertIsNone(entrance_layout([self.wall],self.normal,100,2,lambda p:np.array([100.,100.4])))
        with self.assertRaisesRegex(ValueError,'Invalid terrain'):
            entrance_layout([self.wall],self.normal,100,2,lambda p:np.full(len(p),np.nan))

    def test_doors_never_cover_source_wall_holes_or_cross_top_boundary(self):
        hole=np.array([[1,0,100.01],[1,0,103],[11,0,103],[11,0,100.01],[1,0,100.01]],float)
        self.assertIsNone(entrance_layout([self.wall,hole],self.normal,100))
        low=self.wall.copy();low[low[:,2]>100,2]=101.8
        self.assertIsNone(entrance_layout([low],self.normal,100))

    def test_foundation_keeps_positive_ground_crossing_subinterval_in_both_directions(self):
        wall=np.array([[0,0,100],[1,0,100],[1,0,106],[0,0,106],[0,0,100]],float)
        source=wall.copy()
        for downhill in (True,False):
            def ground(points):
                x=points[:,0] if downhill else 1-points[:,0]
                return 100.1-.3*x
            panels=foundation_strips([wall],self.normal,ground)
            self.assertEqual(len(panels),1)
            v,faces,uv,gap=panels[0]
            xmin,xmax=float(v[:,0].min()),float(v[:,0].max())
            if downhill:
                self.assertAlmostEqual(xmin,1/3);self.assertAlmostEqual(xmax,1.)
            else:
                self.assertAlmostEqual(xmin,0.);self.assertAlmostEqual(xmax,2/3)
            self.assertAlmostEqual(gap,.2)
            np.testing.assert_allclose(v[[1,2],2],ground(v[[1,2],:2])-.1)
            np.testing.assert_array_equal(wall,source)

    def test_foundation_closes_small_visible_gaps_but_never_extends_a_datum_jump(self):
        wall=np.array([[0,0,100],[1,0,100],[1,0,106],[0,0,106],[0,0,100]],float)
        panels=foundation_strips([wall],self.normal,lambda p:np.full(len(p),99.986))
        self.assertEqual(len(panels),1)
        self.assertAlmostEqual(panels[0][3],.014)
        self.assertEqual(foundation_strips([wall],self.normal,lambda p:100.1-2.3*p[:,0]),[])

    def test_door_solids_have_positive_dimensions_and_outward_exported_faces(self):
        layout=entrance_layout([self.wall],self.normal,100,3,lambda p:np.full(len(p),99.68))
        self.assertIsNotNone(layout)
        parts=doorway_components(layout,3,textured_leaf=True)
        self.assertGreater(len(parts),9)
        for role,bounds in parts:
            x0,x1,d0,d1,z0,z1=bounds
            self.assertGreater(x1,x0);self.assertGreater(d1,d0);self.assertGreater(z1,z0)
            self.assertGreaterEqual(z0,layout['ground']-.035-1e-8)
            self.assertLessEqual(d1,1.1)
            v,f,uv=facade_box(layout['origin'],layout['u'],layout['normal'],bounds)
            v,f,uv=exportable_mesh(v,f,uv)
            t=v[np.asarray(f)].astype(np.float32)
            normals=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])
            self.assertTrue(np.all(np.sum(normals*(t.mean(axis=1)-v.mean(axis=0)),axis=1)>0),role)

    def test_textured_leaf_uses_actual_metal_hardware_without_duplicate_panel_relief(self):
        layout=entrance_layout([self.wall],self.normal,100,1)
        atlas=doorway_components(layout,1,True)
        fallback=doorway_components(layout,1,False)
        self.assertEqual(sum(role=='metal' for role,b in atlas),2)
        self.assertLess(len(atlas),len(fallback))
        left=layout['center']-layout['width']/2
        hardware=[b for role,b in atlas if role=='metal'][0]
        self.assertAlmostEqual((hardware[0]+hardware[1])/2,left+layout['width']*.89)

    def test_atlas_is_copied_unchanged_and_uvs_cannot_sample_neighbouring_door(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='door-atlas-') as temp:
            data=Path(temp);textures=data/'textures';textures.mkdir()
            source=data/'atlas.png';Image.new('RGB',(128,64),(128,78,32)).save(source)
            scene={'materials':[]}
            def material(name,texture,**kwargs):
                scene['materials'].append(dict(name=name,texture=texture,**kwargs))
                return len(scene['materials'])-1
            mids=door_textures(textures,material,scene,source)
            self.assertEqual((textures/'door_atlas.png').read_bytes(),source.read_bytes())
            self.assertEqual(len(mids),8)
            for index,mid in enumerate(mids):
                uv=atlas_uv([(0,1),(1,1),(1,0),(0,0)],scene['materials'][mid]['uv_rect'])
                col,row=index%4,index//4
                self.assertTrue(np.all(uv[:,0]>col/4) and np.all(uv[:,0]<(col+1)/4))
                self.assertTrue(np.all(uv[:,1]>row/2) and np.all(uv[:,1]<(row+1)/2))

    def test_architectural_types_vary_proportions_but_retain_aligned_storeys(self):
        signatures=set();shape=Polygon(self.wall[:,[0,2]])
        for style in range(8):
            u,layouts=window_layout([self.wall],self.normal,100,3,106,style)
            signatures.add(tuple(round(v,3) for item in layouts for v in item[:4]))
            levels={}
            for center,bottom,top,width,level in layouts:
                rectangle=Polygon([(center-width/2-.1,bottom-.1),(center+width/2+.1,bottom-.1),
                                   (center+width/2+.1,top+.1),(center-width/2-.1,top+.1)])
                self.assertTrue(shape.covers(rectangle))
                levels.setdefault(level,[]).append(center)
            np.testing.assert_allclose(levels[0],levels[1])
        self.assertEqual(len(signatures),8)

    def test_complete_build_exports_doors_shutters_without_changing_source_wall(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='architecture-build-') as temp:
            data=Path(temp);textures=data/'textures';textures.mkdir()
            source=data/'materials/PaintedPlaster006';source.mkdir(parents=True)
            Image.new('RGB',(16,16),(210,202,190)).save(source/'PaintedPlaster006_2K-JPG_Color.jpg')
            Image.new('RGB',(16,16),(128,128,255)).save(source/'PaintedPlaster006_2K-JPG_NormalDX.jpg')
            atlas=data/'materials/AIDoors';atlas.mkdir(parents=True)
            Image.new('RGB',(128,64),(91,68,41)).save(atlas/'door_atlas.png')
            identifier=next(f'house{i}' for i in range(100) if int(hashlib.sha256(f'house{i}'.encode()).hexdigest()[:8],16)%8==2)
            origin=np.array([462000.,5556400.,230.])
            front=self.wall-np.array([0,0,100])+origin
            roof=np.array([[0,0,6],[6,0,9],[6,8,9],[0,8,6],[0,0,6]])+origin
            transform=Transformer.from_crs(25832,4326,always_xy=True)
            def encoded(points):
                lon,lat=transform.transform(points[:,0],points[:,1])
                return ' '.join(str(x) for x in np.c_[lat,lon,points[:,2]].ravel())
            polygons=''.join(f'<gml:Polygon gml:id="{name}"><gml:exterior><gml:LinearRing><gml:posList>{encoded(points)}</gml:posList></gml:LinearRing></gml:exterior></gml:Polygon>' for name,points in [('front',front),('roof',roof)])
            (data/'buildings_lod2.gml').write_text('<wfs:FeatureCollection xmlns:wfs="http://www.opengis.net/wfs/2.0" xmlns:gml="http://www.opengis.net/gml/3.2" xmlns:bu="http://inspire.ec.europa.eu/schemas/bu-core3d/4.0"><wfs:member><bu:Building gml:id="'+identifier+'">'+polygons+'</bu:Building></wfs:member></wfs:FeatureCollection>')
            scene={'materials':[],'meshes':[]}
            def material(name,texture,**kwargs):
                scene['materials'].append(dict(name=name,texture=texture,**kwargs))
                return len(scene['materials'])-1
            def add(name,points,faces,mid,uv):
                scene['meshes'].append(dict(name=name,points=np.asarray(points),faces=faces,material=mid,uv=np.asarray(uv)))
            line=LineString([(461990,5556395),(462020,5556395)])
            stats=build_lod2(data,textures,scene,material,add,lambda xy,z:np.c_[xy,z],line,line.buffer(1),None,
                              (461950,5556350,462050,5556450),terrain_height_sampler=lambda p:np.full(len(p),230.1))
            self.assertEqual(stats['procedural_entrances'],1)
            self.assertGreater(stats['three_dimensional_door_components'],8)
            self.assertGreater(stats['three_dimensional_shutters'],0)
            self.assertTrue(stats['synthetic_door_atlas'])
            wall_meshes=[m for m in scene['meshes'] if m['name'].startswith('LOD2_WALLS')]
            area=0.
            for mesh in wall_meshes:
                t=mesh['points'][np.asarray(mesh['faces'])]
                area+=np.linalg.norm(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0]),axis=1).sum()/2
                np.testing.assert_allclose(mesh['points'][:,1],5556400.,atol=1e-5)
            self.assertAlmostEqual(area,72,places=4)
            door=next(m for m in scene['meshes'] if m['name'].startswith('LOD2_ENTRANCES'))
            self.assertAlmostEqual(float(door['points'][:,2].min()),230.135,places=5)


if __name__=='__main__':
    unittest.main(verbosity=2)
