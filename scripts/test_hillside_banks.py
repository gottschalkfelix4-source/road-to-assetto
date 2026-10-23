"""Independent geometric bounds for stepped, reserved hillside banks."""
from pathlib import Path
import sys,unittest
import numpy as np
from shapely.geometry import Polygon,LineString,box
from shapely.ops import unary_union
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from roadtoassetto.hillside_banks import plan_hillside_banks

class BankTests(unittest.TestCase):
    def setUp(self):
        self.line=LineString([(462000,5556400),(462060,5556400)])
        self.road=box(462000,5556397,462060,5556403)
        self.near_house=box(462000,5556410,462060,5556414)
    def ground(self,p):
        p=np.asarray(p);return 230+.02*(p[:,0]-462000)+np.maximum(0,p[:,1]-5556403.5)*1.5
    def plan(self,**kwargs):
        houses=[self.near_house,*kwargs.pop('buildings',[])]
        return plan_hillside_banks(self.line,self.road,[],self.ground,buildings=houses,**kwargs)
    def test_steep_bank_has_connected_three_tier_walls_and_real_terrain(self):
        plan=self.plan();self.assertGreater(plan['stats']['hillside_bank_runs'],0)
        self.assertGreater(len(plan['surfaces']),20)
        self.assertTrue(all(r['tiers']==3 for r in plan['stats']['hillside_bank_records']))
        for wall in plan['retaining_edges']:
            self.assertLessEqual(max(np.array(wall['top'])-np.array(wall['bottom'])),2.46+1e-6)
            self.assertEqual(wall['width_m'],.28)
        self.assertEqual(len(plan['terrain_surfaces']),len(plan['surfaces'])*2)
        for surface in plan['surfaces']:
            xy=np.array(surface['xy']);self.assertEqual(surface['kind'],'bank_ledge')
            np.testing.assert_allclose(np.linalg.norm(xy[3]-xy[0]),.5,atol=1e-7)
            self.assertFalse(Polygon(xy).intersects(self.road.buffer(.502)))
        # The outermost ledges tie back to source DGM, rather than lifting
        # an entire distant hillside or covering it with a decorative slab.
        for surface in plan['surfaces']:
            if surface['id'].endswith('_ledge_2'):
                xy=np.array(surface['xy']);top=np.array(surface['top'])
                np.testing.assert_allclose(top[[2,3]]-.035,self.ground(xy[[2,3]]),atol=1e-8)
    def test_buildings_existing_access_and_side_roads_remain_reserved(self):
        house=box(462020,5556403.6,462025,5556410)
        junction=box(462035,5556401,462040,5556410)
        occupied=dict(xy=list(box(462045,5556403.6,462050,5556408).exterior.coords)[:-1])
        result=self.plan(buildings=[house],other_roads=junction,occupied_surfaces=[occupied])
        forbidden=unary_union([house,junction,Polygon(occupied['xy']),self.road.buffer(.502)])
        self.assertTrue(result['surfaces'])
        self.assertTrue(all(not Polygon(s['xy']).intersects(forbidden) for s in result['terrain_surfaces']))
    def test_gentle_or_downhill_land_is_not_walled(self):
        for slope in (.1,-1.5):
            def ground(p):
                p=np.asarray(p);return 230+np.maximum(0,p[:,1]-5556403.5)*slope
            result=plan_hillside_banks(self.line,self.road,[],ground,buildings=[self.near_house])
            self.assertEqual(result['surfaces'],[])
    def test_stable_geometry_ids_and_explicit_inference(self):
        first=self.plan();second=self.plan()
        self.assertEqual(first,second)
        self.assertEqual(len({s['id'] for s in first['surfaces']}),len(first['surfaces']))
        self.assertIn('not surveyed',first['stats']['hillside_bank_provenance'])
        self.assertTrue(all(r['maximum_cut_m']<=2.5 and r['maximum_fill_m']<=1.8 for r in first['stats']['hillside_bank_records']))
    def test_curved_runs_share_exact_bench_corners_and_wall_outward(self):
        from roadtoassetto.hillside_render import wall_outline
        x=np.linspace(462000,462060,31);bend=np.sin((x-462000)/30)*.8
        center=np.c_[x,5556400+bend]
        polygon=Polygon(np.vstack([center+[0,3],(center-[0,3])[::-1]]))
        plan=plan_hillside_banks(LineString(center),polygon,[],self.ground,buildings=[self.near_house])
        for wall in plan['retaining_edges']:wall_outline(wall)
        grouped={}
        for s in plan['surfaces']:grouped.setdefault(s['assembly_id'],[]).append(s)
        count=0;groups=list(grouped.values())
        for left,right in zip(groups[:-1],groups[1:]):
            if len(left)!=len(right):continue
            a=np.array(left[0]['xy']);b=np.array(right[0]['xy'])
            if np.linalg.norm(a[1]-b[0])<1e-6:
                for ls,rs in zip(left,right):
                    np.testing.assert_allclose(np.array(ls['xy'])[[1,2]],np.array(rs['xy'])[[0,3]],atol=1e-7)
                    np.testing.assert_allclose(np.array(ls['top'])[[1,2]],np.array(rs['top'])[[0,3]],atol=1e-7)
                count+=1
        self.assertGreater(count,5)
    def test_millimetre_bevels_do_not_break_long_house_side_run(self):
        # 1-mm outward bevels between every 1-m edge reproduced the real
        # public-edge union bug: short edges must not fragment a 20-m bank.
        upper=[]
        for x in np.arange(462000,462061):
            upper.extend([[x,5556403],[x+.001,5556403.001]])
        polygon=Polygon([*[list(p) for p in upper],[462061,5556397],[462000,5556397]])
        plan=plan_hillside_banks(self.line,polygon,[],self.ground,buildings=[self.near_house])
        self.assertTrue(plan['surfaces']);self.assertLessEqual(plan['stats']['hillside_bank_runs'],2)
        stations=[r['station_m'] for r in plan['stats']['hillside_bank_records']]
        self.assertGreater(max(stations)-min(stations),30)
    def test_open_or_opposite_side_hill_is_not_walled(self):
        opposite=box(462000,5556394,462060,5556396)
        for houses in ([],[opposite]):
            plan=plan_hillside_banks(self.line,self.road,[],self.ground,buildings=houses)
            self.assertEqual(plan['surfaces'],[])
    def test_coping_ends_buried_and_benches_return_to_original_grade(self):
        plan=self.plan();walls=plan['retaining_edges'];surfaces=plan['surfaces']
        for wall,ledge,endpoint in ((walls[0],surfaces[0],0),(walls[-1],surfaces[-1],1)):
            p=np.array(wall['xy'])[endpoint]
            source=float(self.ground([p])[0])
            self.assertLess(wall['top'][endpoint]+.06,source)
            xy=np.array(ledge['xy']);top=np.array(ledge['top'])
            ids=[0,3] if endpoint==0 else [1,2]
            np.testing.assert_allclose(top[ids]-.035,self.ground(xy[ids]),atol=1e-7)

if __name__=='__main__':unittest.main(verbosity=2)
