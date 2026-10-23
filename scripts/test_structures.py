"""Vertical geometry and physical clearance, independent of provider network."""
import unittest
import numpy as np
from roadtoassetto.structures import (resolve_heights,validate_crossings,StructureGround,
    structure_kind,build_structures,clear_structure_corridors)
from roadtoassetto.ground_details import RoadGroundProfile
from roadtoassetto.kn5 import normals


class StructureTests(unittest.TestCase):
    def test_isolated_dem_crest_is_smoothed_locally_before_export(self):
        xy=np.c_[np.arange(301.),np.zeros(301)]
        ground=100+np.maximum(0,2.-np.abs(np.arange(301)-150)*.35)
        height,_=resolve_heights(xy,ground,[])
        self.assertLess(np.max(abs(np.diff(height,n=2))),.035)
        self.assertAlmostEqual(height[30],100.)
        self.assertGreater(height[150],100.5)

    def test_bridge_does_not_follow_valley_and_ground_stays_below(self):
        xy=np.c_[np.arange(301.),np.zeros(301)]
        ground=100-15*np.sin(np.linspace(0,np.pi,301))**4
        span={'start':70,'end':230,'kind':'bridge','code':1,'tags':{'bridge':'yes'}}
        height,records=resolve_heights(xy,ground,[span],np.full(301,99.))
        self.assertGreater(height[150]-ground[150],10)
        self.assertFalse(records[0]['surveyed'])
        codes=np.zeros(301);codes[70:230]=1
        profile=StructureGround(RoadGroundProfile(xy,height,3),xy,codes)
        self.assertAlmostEqual(profile(xy[[150]],ground[[150]])[0],ground[150])
    def test_tunnel_keeps_hill_and_floor_uses_portals(self):
        xy=np.c_[np.arange(301.),np.zeros(301)]
        ground=100+25*np.sin(np.linspace(0,np.pi,301))**4
        height,_=resolve_heights(xy,ground,[{'start':50,'end':250,'kind':'tunnel','code':2,'tags':{}}])
        self.assertLess(height[150],105)
        self.assertLess(np.max(np.abs(np.diff(height))),.25)
    def test_layer_is_not_an_elevation(self):
        self.assertEqual(structure_kind({'layer':'1'}),'ground')
        self.assertEqual(structure_kind({'covered':'yes'}),'tunnel')
    def test_overpass_crossing_requires_clearance(self):
        xy=np.array([[-10.,0],[10,0],[20,20],[0,10],[0,-10]])
        y=np.array([0,0,8,8,8]);codes=np.array([0,0,0,1,0])
        self.assertGreater(validate_crossings(xy,y,codes)[0]['vertical_separation_m'],5)
        with self.assertRaises(ValueError):validate_crossings(xy,y*.4,codes)
    def test_collision_solids_leave_tunnel_envelope_open(self):
        xy=np.c_[np.arange(41.),np.zeros(41)];y=np.full(41,100.)
        codes=np.zeros(41,dtype=int);codes[10:30]=2
        meshes=[]
        def ac(p,h):return np.c_[p[:,0],np.broadcast_to(h,len(p))-100,-p[:,1]]
        def add(name,v,f,mat,uv=None,collision=True):meshes.append({'name':name,'vertices':np.asarray(v),'faces':np.asarray(f),'collision':collision})
        stats=build_structures(xy,y,np.tile([0,1.],(41,1)),np.full(41,3.),np.arange(41),codes,ac,add,lambda *a,**k:0,lambda p:np.full(len(p),100.))
        self.assertEqual(stats['tunnel_portals'],2)
        self.assertTrue(meshes)
        for m in meshes:
            self.assertTrue(m['collision']);self.assertTrue(np.isfinite(normals(m['vertices'],m['faces'])).all())
            p=m['vertices']
            if 'WALL' in m['name']:self.assertGreaterEqual(np.min(np.abs(p[:,2])),3.5)
            if 'ROOF' in m['name']:self.assertGreaterEqual(p[:,1].min(),5.5)
            if 'HEADER' in m['name']:self.assertGreaterEqual(p[:,1].min(),5.5)
            if 'PORTAL_WING' in m['name'] or 'PORTAL_PIER' in m['name']:self.assertGreaterEqual(np.min(np.abs(p[:,2])),3.48-1e-9)
    def test_structure_start_requires_ground_approach(self):
        xy=np.c_[np.arange(301.),np.zeros(301)]
        with self.assertRaises(ValueError):resolve_heights(xy,np.zeros(301),[{'start':2,'end':50,'kind':'bridge','code':1,'tags':{}}])


if __name__=='__main__':unittest.main()
