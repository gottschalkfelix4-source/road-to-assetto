"""Source-bound basement shells and terrain contact, independent small fixtures."""
from pathlib import Path
import sys
import unittest
import numpy as np
from shapely.geometry import Polygon,box

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from roadtoassetto.building_bases import source_footprint,plan_building_base,_terrain_stations,_wall_box
from roadtoassetto.terrain_sampling import ExportedTerrainSampler


def house(x=0.,y=0.,bottom=3.,top=7.):
    low=np.array([[x,y,bottom],[x+4,y,bottom],[x+4,y+3,bottom],[x,y+3,bottom]])
    high=low.copy();high[:,2]=top
    faces=[('ground',[np.vstack([low[::-1],low[-1]])]),('roof',[np.vstack([high,high[0]])])]
    for i in range(4):
        j=(i+1)%4;p=np.array([low[i],low[j],high[j],high[i],low[i]])
        faces.append((f'wall_{i}',[p]))
    return faces


class BuildingBaseTests(unittest.TestCase):
    def test_three_metre_open_house_has_complete_exterior_and_source_soffit(self):
        faces=house();saved=repr(faces)
        plan=plan_building_base('test',faces,lambda p:np.zeros(len(p)))
        self.assertEqual(repr(faces),saved)
        self.assertEqual(len(plan['soffits']),1)
        self.assertEqual({r['source_surface_id'] for r in plan['panels']},{f'wall_{i}' for i in range(4)})
        self.assertEqual({r['kind'] for r in plan['panels']},{'wall','plinth'})
        self.assertAlmostEqual(plan['stats']['building_base_maximum_gap_m'],3.)
        for r in plan['panels']:
            v=np.array(r['vertices_utm_xyz']);f=np.array(r['faces'])
            self.assertTrue(np.isfinite(v).all())
            self.assertTrue(np.all(np.linalg.norm(np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]]),axis=1)>1e-7))
            self.assertTrue(box(0,0,4,3).buffer(1e-10).covers(Polygon(np.array(r['footprint_utm'][0]['outer']))))
            if r['kind']=='plinth':self.assertAlmostEqual(v[:,2].min(),-.15)

    def test_upper_storey_seam_is_never_a_second_basement(self):
        faces=house(bottom=0,top=3)
        faces=[f for f in faces if f[0] not in ('roof',)]
        upper=house(bottom=3,top=7)
        faces.extend((name+'_upper',rings) for name,rings in upper if name!='ground')
        plan=plan_building_base('stack',faces,lambda p:np.full(len(p),-1.))
        self.assertTrue(plan['panels'])
        self.assertFalse(any('_upper' in p['source_surface_id'] for p in plan['panels']))
        self.assertTrue(all(np.max(np.array(p['source_edge_xyz'])[:,2])==0 for p in plan['panels']))

    def test_partial_burial_closes_only_exposed_intervals(self):
        plan=plan_building_base('slope',house(bottom=1),lambda p:p[:,0]-.5)
        self.assertTrue(plan['panels'])
        for p in plan['panels']:
            edge=np.array(p['source_edge_xyz'])
            self.assertTrue(np.all(edge[:,0]<=1.5+1e-8))
            if p['kind']=='plinth':
                z=np.array(p['ground_contact_z']);v=np.array(p['vertices_utm_xyz'])
                np.testing.assert_allclose(v[[0,1],2],z-.15,atol=1e-9)

    def test_ground_outline_keeps_concavity_and_courtyard(self):
        outer=np.array([[0,0,3],[0,5,3],[5,5,3],[5,0,3],[0,0,3]])
        inner=np.array([[1,1,3],[4,1,3],[4,4,3],[1,4,3],[1,1,3]])
        footprint=source_footprint([('ground',[outer,inner])])
        self.assertAlmostEqual(footprint.area,16)
        self.assertEqual(len(footprint.interiors),1)
        outline=np.array([[0,0,3],[0,4,3],[2,4,3],[2,2,3],[4,2,3],[4,0,3],[0,0,3]])
        self.assertAlmostEqual(source_footprint([('ground',[outline])]).area,12)

    def test_short_inner_rings_skip_invalid_face_without_losing_valid_neighbours(self):
        valid=house();expected=source_footprint(valid)
        for label,rings in (valid[0],valid[2]):
            for length in (0,1,2):
                broken=('broken_'+label,[rings[0],np.asarray(rings[0])[:length]])
                self.assertTrue(source_footprint([broken]).is_empty)
                self.assertTrue(source_footprint([broken,*valid]).equals_exact(expected,0))
                plan=plan_building_base('guard',[broken,*valid],lambda p:np.zeros(len(p)))
                self.assertTrue(plan['records'])
                self.assertFalse(any(r['source_surface_id']==broken[0] for r in plan['records']))

    def test_reservations_exclude_actual_body_volume_and_soffit(self):
        reserve=box(-1,-1,2,1)
        neighbour=box(3.9,2,5,4)
        plan=plan_building_base('reserved',house(),lambda p:np.zeros(len(p)),
                                road_reservation=reserve,neighbour_footprints=[neighbour])
        for p in [*plan['panels'],*plan['soffits']]:
            shape=Polygon(p['footprint_utm'][0]['outer'])
            self.assertLessEqual(shape.intersection(reserve).area,1e-6)
            self.assertLessEqual(shape.intersection(neighbour).area,1e-6)
        self.assertGreater(plan['stats']['building_base_rejected_intervals'],0)

    def test_terrain_stations_include_triangle_breakpoints(self):
        # A ridge at x=.731 is deliberately off the regular .6-m samples.
        v=np.array([[0,0,0],[.731,1,0],[2,0,0],[0,0,-2],[.731,1,-2],[2,0,-2]])
        f=np.array([[0,1,4],[0,4,3],[1,2,5],[1,5,4]])
        sampler=ExportedTerrainSampler(v[f],[0,0],0)
        stations=_terrain_stations(np.array([0.,1.,3.]),np.array([2.,1.,3.]),sampler)
        self.assertTrue(np.any(np.isclose(stations,.731/2,atol=1e-7)))
        self.assertGreater(len(stations),5)

    def test_ids_are_deterministic_with_source_face_order(self):
        faces=house(462000,5556300)
        ground=lambda p:np.zeros(len(p))
        a=plan_building_base('stable',faces,ground)
        b=plan_building_base('stable',list(reversed(faces)),ground)
        self.assertEqual([p['id'] for p in a['records']],[p['id'] for p in b['records']])

    def test_large_untrusted_gap_is_bounded_but_source_lid_retained(self):
        plan=plan_building_base('high',house(bottom=8,top=12),lambda p:np.zeros(len(p)))
        self.assertFalse(plan['panels']);self.assertEqual(len(plan['soffits']),1)
        self.assertGreater(plan['stats']['building_base_rejected_intervals'],0)

    def test_source_wall_uv_is_continuous_across_terrain_segments_and_bands(self):
        plan=plan_building_base('uv',house(462000,5556300),lambda p:(p[:,0]-462000)*.3)
        seen={};shared=0
        for panel in plan['panels']:
            vertices=np.asarray(panel['vertices_utm_xyz']);uv=np.asarray(panel['uv'])
            for point,coordinate in zip(vertices,uv):
                key=(panel['source_surface_id'],*point.round(8))
                if key in seen:
                    np.testing.assert_allclose(coordinate,seen[key],atol=1e-9)
                    shared+=1
                seen[key]=coordinate
        self.assertGreater(shared,40)

    def test_acute_inside_corner_clips_back_shell_but_keeps_front_and_height_planes(self):
        edge=np.array([[462000.,5556300.,3.],[462002.,5556300.,3.]])
        # The rear wall corner, not the original frontage, crosses this acute
        # building boundary. A chamfer adds an extra polygon vertex as well.
        limit=Polygon([[462000.,5556300.],[462002.,5556300.],
                       [462002.,5556302.],[462000.7,5556302.],
                       [462000.04,5556300.05]])
        lower=np.array([0.,.4]);upper=np.array([3.,3.])
        normal=np.array([0.,-1.,0.])
        original,_,_,body=_wall_box(edge,lower,upper,normal)
        self.assertGreater(body.difference(limit).area,1e-4)
        v,f,uv,body=_wall_box(edge,lower,upper,normal,footprint_limit=limit)
        self.assertLess(body.difference(limit).area,1e-10)
        for p in original[:4]:
            self.assertLess(np.linalg.norm(v-p,axis=1).min(),1e-8)
        self.assertTrue(np.isfinite(uv).all())
        self.assertTrue(np.all(v[:,2]>=.2*(v[:,0]-462000)-1e-8))
        self.assertTrue(np.all(v[:,2]<=3+1e-8))
        # A closed outward shell has positive signed volume equal to the
        # polygon integral of the unchanged linear height difference.
        local=v-np.array([462000.,5556300.,0.]);tri=local[f]
        volume=np.einsum('ij,ij->i',tri[:,0],np.cross(tri[:,1],tri[:,2])).sum()/6
        expected=body.area*(3-.2*(body.centroid.x-462000))
        self.assertAlmostEqual(volume,expected,places=7)

    def test_original_front_outside_conflicting_ground_polygon_is_not_moved(self):
        edge=np.array([[0.,-.005,3.],[2.,-.005,3.]])
        args=(edge,np.array([0.,.4]),np.array([3.,3.]),np.array([0.,-1.,0.]))
        original=_wall_box(*args)
        checked=_wall_box(*args,footprint_limit=box(0,0,2,2))
        for before,after in zip(original[:3],checked[:3]):
            np.testing.assert_array_equal(before,after)
        self.assertTrue(original[3].equals_exact(checked[3],0))


if __name__=='__main__':unittest.main()
