"""Metric architectural appearance with source-wall and roof constraints."""
from pathlib import Path
import sys
import unittest

import numpy as np
from shapely.geometry import Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.buildings import (architectural_palette,downpipe_layout,pipe_mesh,
                                     plaster_uv,wall_eave_height)
from roadtoassetto.kn5 import normals


class BuildingAppearanceTests(unittest.TestCase):
    def setUp(self):
        self.wall=np.array([[0,0,100],[8,0,100],[8,0,106],[0,0,106],[0,0,100]],float)
        self.normal=np.array([0.,-1.,0.])

    def test_paint_and_architecture_are_stable_but_independent(self):
        choices=[architectural_palette(f'house-{i}') for i in range(160)]
        self.assertEqual(architectural_palette('real-id'),architectural_palette('real-id'))
        self.assertEqual({p['paint'] for p in choices},set(range(8)))
        self.assertEqual({p['style'] for p in choices},set(range(8)))
        for style in range(8):
            self.assertGreater(len({p['paint'] for p in choices if p['style']==style}),4)
        self.assertTrue(all(.25<=p['plinth_height']<=.65 for p in choices))

    def test_plaster_has_fixed_metric_scale_and_does_not_follow_storey_bands(self):
        samples=np.array([[0,0,100],[3,0,100],[0,0,101.5]])
        uv=plaster_uv(samples,self.normal,100,0,(.3,.2))
        np.testing.assert_allclose(uv,[[.3,1.2],[1.3,1.2],[.3,.2]])
        np.testing.assert_allclose(plaster_uv(samples,self.normal,100,0),
                                   plaster_uv(np.vstack([samples,[0,0,109]]),self.normal,100,0)[:3])

    def test_downpipe_stays_in_actual_wall_and_follows_valid_ground(self):
        original=self.wall.copy()
        drain=downpipe_layout([self.wall],self.normal,100,106,0,
                              lambda p:np.full(len(p),100.18))
        self.assertIsNotNone(drain)
        self.assertAlmostEqual(drain['bottom'],100.30)
        self.assertAlmostEqual(drain['top'],105.82)
        self.assertTrue(np.all(drain['path'][:,1]<0))
        self.assertLessEqual(float(np.abs(drain['path'][:,1]).max()),.18)
        shape=Polygon(self.wall[:,[0,2]])
        self.assertTrue(all(shape.covers(Polygon([(x-.05,z-.01),(x+.05,z-.01),
                                                (x+.05,z+.01),(x-.05,z+.01)]))
                            for x,_,z in drain['path']))
        np.testing.assert_array_equal(self.wall,original)
        for height in (99.2,100.8,float('nan')):
            self.assertIsNone(downpipe_layout([self.wall],self.normal,100,106,0,
                                              lambda p:np.full(len(p),height)))

    def test_downpipe_does_not_cover_wall_holes(self):
        holes=[]
        for x in (.22,7.78):
            holes.append(np.array([[x-.15,0,101],[x-.15,0,104],
                                    [x+.15,0,104],[x+.15,0,101],[x-.15,0,101]]))
        self.assertIsNone(downpipe_layout([self.wall,*holes],self.normal,100,106))

    def test_pipe_ends_at_surveyed_eave_not_a_gable_or_neighbour_roof(self):
        roof=np.array([[0,0,106],[8,0,106],[8,4,109],[0,4,109]])
        self.assertEqual(wall_eave_height(self.wall,self.normal,[(roof,None,[0,-.6,.8])]),106)
        self.assertIsNone(wall_eave_height(self.wall,self.normal,[(roof+[0,3,0],None,[0,-.6,.8])]))
        self.assertIsNone(wall_eave_height(self.wall,self.normal,[(roof,None,[0,0,1])]))
        gable=np.array([[0,0,100],[0,8,100],[0,8,106],[0,4,109],[0,0,106],[0,0,100]])
        self.assertIsNone(wall_eave_height(gable,np.array([-1.,0.,0.]),[(roof,None,[0,-.6,.8])]))

    def test_cylindrical_pipe_faces_are_outward_and_nondegenerate(self):
        path=np.array([[0,0,100],[0,0,105.5],[0,-.07,105.7]])
        points,faces,uv=pipe_mesh(path)
        t=points[np.asarray(faces)]
        cross=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])
        self.assertTrue(np.all(np.linalg.norm(cross,axis=1)>1e-7))
        np.testing.assert_allclose(np.linalg.norm(normals(points,faces),axis=1),1,atol=1e-6)
        # The long vertical shaft's side normals point away from its axis.
        centers=t[:20].mean(axis=1)
        self.assertTrue(np.all(np.sum(cross[:20,:2]*centers[:,:2],axis=1)>0))
        self.assertTrue(np.isfinite(uv).all())
        for bad in ([[0,0,0]],[[0,0,0],[0,0,0]]):
            with self.assertRaises(ValueError):
                pipe_mesh(bad)


if __name__=='__main__':
    unittest.main(verbosity=2)
