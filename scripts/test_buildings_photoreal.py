"""Containment, export precision and material variety of close building detail."""
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image
from shapely.geometry import Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.buildings import facade_box,window_layout,glass_textures,exportable_mesh,foundation_strips
from roadtoassetto.kn5 import normals


class CloseBuildingTests(unittest.TestCase):
    def test_foundations_close_only_lower_source_edge_without_changing_source(self):
        wall=np.array([[0,0,100],[5,0,100],[5,0,106],[0,0,106],[0,0,100]],float)
        original=wall.copy();normal=np.array([0.,-1.,0.])
        panels=foundation_strips([wall],normal,lambda points:np.full(len(points),99.6))
        self.assertEqual(len(panels),5)
        np.testing.assert_array_equal(wall,original)
        for v,f,uv,gap in panels:
            self.assertAlmostEqual(gap,.4)
            self.assertAlmostEqual(float(v[:,2].min()),99.5)
            self.assertAlmostEqual(float(v[:,2].max()),100.012)
            self.assertLessEqual(float(v[:,0].max()-v[:,0].min()),1.+1e-9)
            t=v[np.asarray(f)]
            self.assertTrue(np.all(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])@normal>0))

    def test_foundations_do_not_fill_holes_buried_walls_or_large_datums(self):
        wall=np.array([[0,0,100],[5,0,100],[5,0,106],[0,0,106],[0,0,100]],float)
        hole=np.array([[1,0,100.5],[1,0,101],[2,0,101],[2,0,100.5],[1,0,100.5]],float)
        normal=np.array([0.,-1.,0.])
        for terrain in (100.,100.4,97.8):
            with self.subTest(terrain=terrain):
                panels=foundation_strips([wall,hole],normal,lambda p:np.full(len(p),terrain))
                self.assertEqual(panels,[])
        with self.assertRaisesRegex(ValueError,'Invalid terrain'):
            foundation_strips([wall],normal,lambda p:np.full(len(p),np.nan))

    def test_foundations_follow_sloped_ground_in_bounded_short_panels(self):
        wall=np.array([[0,0,100],[5,0,100.5],[5,0,106],[0,0,106],[0,0,100]],float)
        normal=np.array([0.,-1.,0.])
        panels=foundation_strips([wall],normal,lambda p:99.5+p[:,0]*.1)
        self.assertEqual(len(panels),5)
        for v,faces,uv,gap in panels:
            self.assertAlmostEqual(gap,.5)
            low=v[[1,2]]
            np.testing.assert_allclose(low[:,2],99.4+low[:,0]*.1)

    def test_foundations_never_cover_a_lower_source_wall_at_a_surface_seam(self):
        upper=np.array([[0,0,100.7],[5,0,100.7],[5,0,106],[0,0,106],[0,0,100.7]],float)
        lower=np.array([[0,0,100],[5,0,100],[5,0,100.7],[0,0,100.7],[0,0,100]],float)
        normal=np.array([0.,-1.,0.]);horizontal=np.array([1.,0.,0.])
        neighbor=(lower[0],normal,horizontal,Polygon(lower[:,[0,2]]))
        panels=foundation_strips([upper],normal,lambda p:np.full(len(p),99.6),wall_neighbors=[neighbor])
        self.assertEqual(panels,[])

    def test_window_bays_stay_inside_sloped_wall_and_exclude_holes(self):
        outer=np.array([[0,0,100],[10,0,100],[10,0,106],[5,0,109],[0,0,106],[0,0,100]],float)
        hole=np.array([[4,0,100.2],[4,0,103],[6,0,103],[6,0,100.2],[4,0,100.2]],float)
        wall=Polygon(outer[:,[0,2]],[hole[:,[0,2]]])
        u,layouts=window_layout([outer,hole],np.array([0.,-1.,0.]),100,3.,106,0)
        self.assertGreater(len(layouts),2)
        for center,bottom,top,width,level in layouts:
            rectangle=Polygon([(center-width/2-.1,bottom-.1),(center+width/2+.1,bottom-.1),
                               (center+width/2+.1,top+.1),(center-width/2-.1,top+.1)])
            self.assertTrue(wall.covers(rectangle))
            self.assertLess(top,106)

    def test_trim_boxes_have_hard_outward_normals_after_actual_export(self):
        for angle in (0.,.37,1.2,2.9):
            normal=np.array([np.cos(angle),np.sin(angle),0.])
            u=np.array([-normal[1],normal[0],0.])
            points,faces,uv=facade_box(np.array([237.,14.,0.]),u,normal,(-.025,.025,.024,.095,8.2,9.63))
            points,faces,uv=exportable_mesh(points,faces,uv)
            points=points.astype(np.float32)
            n=normals(points,faces)
            np.testing.assert_allclose(np.linalg.norm(n,axis=1),1,atol=1e-6)
            t=points[np.asarray(faces)];cross=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])
            self.assertTrue(np.all(np.sum(cross*(t.mean(axis=1)-points.mean(axis=0)),axis=1)>0))
            self.assertEqual(len(points),24)

    def test_glazing_has_varied_curtains_and_reserved_glass_material(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='glass-test-') as tmp:
            folder=Path(tmp);scene={'materials':[]}
            def material(name,texture,**kwargs):
                scene['materials'].append(dict(name=name,texture=texture,**kwargs))
                return len(scene['materials'])-1
            mids=glass_textures(folder,material,scene)
            self.assertEqual(len(mids),8)
            hashes=set()
            means=[]
            for mid in mids:
                mat=scene['materials'][mid]
                pixels=np.asarray(Image.open(folder/mat['texture']))
                means.append(float(pixels.mean()))
                self.assertGreater(float(pixels.std()),8)
                self.assertLess(float(pixels.mean()),150)
                hashes.add((folder/mat['texture']).read_bytes())
                self.assertGreater(mat['specular'],.2)
                self.assertLess(mat['roughness'],.3)
            self.assertEqual(len(hashes),8)
            self.assertGreater(max(means)-min(means),15)


if __name__=='__main__':
    unittest.main(verbosity=2)
