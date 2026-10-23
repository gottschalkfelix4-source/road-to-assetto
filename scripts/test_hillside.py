"""Independent construction contracts for inferred hillside access geometry.

These are model limits for scenery, not a building-code or vehicle-access
certification. Fixtures exercise connected surfaces and hard exclusions.
"""
from pathlib import Path
import copy
import json
import sys
import unittest
from unittest.mock import patch

import numpy as np
from shapely.geometry import Polygon, LineString, Point, box
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.hillside import plan_frontage, plan_hillside
from scripts.verify_street_contacts import hillside_contact_checks


class HillsidePlanningTests(unittest.TestCase):
    def frontage(self, distance=6., level=101.1, *, obstacle=None, neighbour=None,
                 offset=(0., 0.), ground=100.):
        origin = np.array(offset)
        edge = np.array([[0., distance, level], [7., distance, level]])
        edge[:, :2] += origin
        public = box(origin[0]-50, origin[1]-50, origin[0]+50, origin[1])
        reserved = public if obstacle is None else public.union(obstacle)
        sampler = lambda p: np.full(len(p), ground)
        return plan_frontage(edge, [0., -1.], [], sampler, reserved,
                             Polygon() if neighbour is None else neighbour,
                             assembly_id='fixed-source-frontage', public_boundary=public)

    def assert_valid_plan(self, plan):
        self.assertIsNotNone(plan)
        json.dumps(plan, allow_nan=False)
        ids = [s['id'] for s in plan['surfaces']]
        self.assertEqual(len(ids), len(set(ids)))
        self.assertLessEqual(plan['maximum_cut_m'], 2.5)
        self.assertLessEqual(plan['maximum_fill_m'], 1.8)
        for surface in plan['surfaces'] + plan['terrain_surfaces']:
            xy = np.asarray(surface['xy'])
            top = np.asarray(surface['top'])
            faces = np.asarray(surface['faces'])
            self.assertEqual(xy.shape, (len(top), 2))
            self.assertTrue(np.isfinite(xy).all() and np.isfinite(top).all())
            polygon = Polygon(xy)
            self.assertTrue(polygon.is_valid)
            self.assertGreater(polygon.area, 1e-5)
            triangles = [Polygon(xy[face]) for face in faces]
            self.assertTrue(all(t.area > 1e-6 for t in triangles))
            self.assertLess(polygon.symmetric_difference(unary_union(triangles)).area, 1e-7)

    def test_stairs_join_terrace_and_both_landings_without_isolated_treads(self):
        plan = self.frontage()
        self.assert_valid_plan(plan)
        self.assertEqual(len(plan['stairs']), 1)
        stair = plan['stairs'][0]
        surfaces = {s['id']: s for s in plan['surfaces']}
        chain = [surfaces[stair['upper_landing_id']],
                 *[surfaces[key] for key in stair['tread_surface_ids']],
                 surfaces[stair['lower_landing_id']]]
        self.assertGreaterEqual(len(stair['tread_surface_ids']), 2)
        self.assertLessEqual(len(stair['tread_surface_ids']), 12)
        self.assertGreaterEqual(stair['riser_m'], .12)
        self.assertLessEqual(stair['riser_m'], .175 + 1e-8)
        self.assertGreaterEqual(stair['tread_m'], .30)
        self.assertGreaterEqual(stair['width_m'], 1.0)
        for first, second in zip(chain[:-1], chain[1:]):
            a, b = Polygon(first['xy']), Polygon(second['xy'])
            self.assertLess(a.distance(b), 1e-8)
            self.assertGreater(a.boundary.intersection(b.boundary).length, 1.0)
            self.assertLess(a.intersection(b).area, 1e-8)
            delta = abs(float(np.mean(first['top'])-np.mean(second['top'])))
            self.assertLessEqual(delta, .175 + 1e-8)
        bottom = np.asarray(chain[-1]['xy'])
        self.assertGreaterEqual(float(np.linalg.norm(bottom[3]-bottom[0])), .85 - 1e-7)
        self.assertEqual(len(stair['handrails']), 2)

    def test_driveway_is_broad_gentle_and_has_matching_terrace_endpoint(self):
        plan = self.frontage(distance=8., level=101.)
        self.assert_valid_plan(plan)
        driveways = [s for s in plan['surfaces'] if s['kind'] == 'driveway']
        self.assertEqual(len(driveways), 1)
        self.assertEqual(plan['stairs'], [])
        driveway = driveways[0]
        xy, z = np.array(driveway['xy']), np.array(driveway['top'])
        self.assertGreaterEqual(np.linalg.norm(xy[1]-xy[0]), 2.6 - 1e-8)
        actual_grade = np.abs(z[[0, 1]]-z[[3, 2]]) / np.linalg.norm(xy[[0, 1]]-xy[[3, 2]], axis=1)
        self.assertLessEqual(float(actual_grade.max()), .14 + 1e-8)
        self.assertAlmostEqual(driveway['maximum_gradient'], float(actual_grade.max()))
        terrace = plan['surfaces'][0]
        self.assertLess(Polygon(terrace['xy']).distance(Polygon(xy)), 1e-8)
        np.testing.assert_allclose(z[:2], np.asarray(terrace['top'])[2], atol=1e-8)

    def test_steep_direct_vehicle_path_uses_only_bounded_pedestrian_access(self):
        plan = self.frontage(distance=4., level=101.)
        self.assert_valid_plan(plan)
        self.assertTrue(all(s['kind'] != 'driveway' for s in plan['surfaces']))
        # A compact terrace can now make room for a connected staircase;
        # the original contract still excludes a steep car ramp.
        for stair in plan['stairs']:
            self.assertLessEqual(stair['riser_m'], .175)
            self.assertGreaterEqual(stair['tread_m'], .30)
            self.assertLessEqual(len(stair['tread_surface_ids']), 12)

    def test_narrow_gentle_pedestrian_access_is_separate_from_driveway(self):
        plan=plan_frontage(np.array([[0.,8.,100.7],[2.3,8.,100.7]]),[0.,-1.],[],
                           lambda p:np.full(len(p),100.),box(-50,-50,50,0),Polygon())
        self.assert_valid_plan(plan)
        self.assertEqual(plan['stairs'],[])
        footpaths=[s for s in plan['surfaces'] if s['kind']=='footpath']
        self.assertEqual(len(footpaths),1)
        self.assertFalse(any(s['kind']=='driveway' for s in plan['surfaces']))
        p=footpaths[0];xy=np.array(p['xy']);z=np.array(p['top'])
        across=xy[1]-xy[0];across/=np.linalg.norm(across)
        for face in p['faces']:
            g=np.linalg.solve(np.c_[xy[face],np.ones(3)],z[face])[:2]
            self.assertLessEqual(np.linalg.norm(g),.12+1e-8)
            self.assertLessEqual(abs(float(g@across)),.08+1e-8)

    def test_treads_stay_level_while_landings_absorb_source_crossfall(self):
        plan=plan_frontage(np.array([[0.,6.,101.1],[7.,6.,101.35]]),[0.,-1.],[],
                           lambda p:100.+.06*(np.array(p)[:,0]-3.5),
                           box(-50,-50,50,0),Polygon())
        self.assert_valid_plan(plan)
        self.assertEqual(len(plan['stairs']),1)
        for s in plan['surfaces']:
            if s['kind']=='stair':
                self.assertLessEqual(float(np.ptp(s['top'])),1e-8)
            if s['kind']=='landing':
                xy=np.array(s['xy']);z=np.array(s['top'])
                for face in s['faces']:
                    g=np.linalg.solve(np.c_[xy[face],np.ones(3)],z[face])[:2]
                    self.assertLessEqual(np.linalg.norm(g),.14+1e-8)

    def test_last_handrail_post_remains_on_tapered_sloping_end_landing(self):
        street={'xy':[[-20.,-1.],[20.,1.],[20.,1.5],[-20.,-.5]],
                'top':[99.8,100.2,100.2,99.8],'faces':[[0,1,2],[0,2,3]],
                'outer_edge_indices':[3,2],'inner_edge_indices':[0,1]}
        reserved=Polygon(street['xy']).buffer(.035).union(box(-50,-50,50,-.5))
        plan=plan_frontage(np.array([[0.,6.,101.1],[7.,6.,101.35]]),[0.,-1.],[street],
                           lambda p:100.+.06*(np.array(p)[:,0]-3.5),reserved,Polygon())
        self.assert_valid_plan(plan)
        self.assertEqual(len(plan['stairs']),1)
        sections={s['id']:s for s in plan['surfaces']}
        for stair in plan['stairs']:
            landing=sections[stair['lower_landing_id']]
            xy=np.array(landing['xy']);top=np.array(landing['top'])
            for rail in stair['handrails']:
                self.assertTrue(np.isfinite(rail['base']).all())
                p=np.asarray(rail['xy'][-1])
                self.assertTrue(Polygon(xy).buffer(1e-7).covers(Point(p)))
                expected=[]
                for face in landing['faces']:
                    if Polygon(xy[face]).buffer(1e-7).covers(Point(p)):
                        plane=np.linalg.solve(np.c_[xy[face],np.ones(3)],top[face])
                        expected.append(float(np.r_[p,1.]@plane))
                self.assertTrue(expected)
                self.assertLess(min(abs(height-rail['base'][-1]) for height in expected),1e-7)

    def test_retaining_wall_bodies_do_not_block_the_constructed_access(self):
        plan=self.frontage()
        self.assert_valid_plan(plan)
        walking=unary_union([Polygon(s['xy']) for s in plan['surfaces']]).buffer(-.001)
        self.assertTrue(plan['retaining_edges'])
        for wall in plan['retaining_edges']:
            p=np.array(wall['xy']);q=p+np.array(wall['outward'])*wall['width_m']
            body=Polygon([p[0],p[1],q[1],q[0]])
            self.assertLess(body.intersection(walking).area,1e-8,wall['id'])

    def test_wall_ground_strip_cannot_pull_down_a_neighbouring_landing(self):
        plan=self.frontage()
        strips=[s for s in plan['terrain_surfaces'] if s['kind']=='retaining_support']
        self.assertTrue(strips)
        for strip in strips:
            xy=np.array(strip['xy']);inner=LineString(xy[:2]);body=Polygon(xy)
            owners=[s for s in plan['surfaces'] if
                    Polygon(s['xy']).boundary.intersection(inner).length>inner.length-1e-7]
            self.assertEqual(len(owners),1,strip['id'])
            # A previous corner case left this buried wall strip1mm from
            # a higher landing. The terrain snap/tolerance then inherited
            # its lower grade beneath that landing. The emitting surface
            # may share the inner edge; every other surface needs6mm.
            for surface in plan['surfaces']:
                if surface is owners[0]:
                    continue
                self.assertGreaterEqual(body.distance(Polygon(surface['xy'])),.006-1e-7,strip['id'])

    def test_a_gentle_longitudinal_ramp_cannot_hide_extreme_crossfall(self):
        # Both long ramp edges stay below14%, while a naive two-endpoint
        # check misses the12% sideways tilt imposed by the old terrain.
        plan = plan_frontage(np.array([[0., 8., 100.7], [7., 8., 100.7]]), [0., -1.], [],
                             lambda p: 100.+.12*(np.array(p)[:, 0]-3.5),
                             box(-50, -50, 50, 0), Polygon())
        self.assert_valid_plan(plan)
        for surface in plan['surfaces']:
            if surface['kind'] != 'driveway':
                continue
            xy, z = np.array(surface['xy']), np.array(surface['top'])
            across = xy[1]-xy[0]
            across /= np.linalg.norm(across)
            along = xy[3]-xy[0]
            along /= np.linalg.norm(along)
            for face in surface['faces']:
                gradient = np.linalg.solve(np.c_[xy[face], np.ones(3)], z[face])[:2]
                self.assertLessEqual(abs(float(gradient @ along)), .14 + 1e-8)
                self.assertLessEqual(abs(float(gradient @ across)), .05 + 1e-8)

    def test_access_end_reaches_actual_pavement_rear_edge_at_its_height(self):
        street = {'xy': [[-20., 0.], [20., 0.], [20., .5], [-20., .5]],
                  'top': [100.065]*4, 'faces': [[0, 1, 2], [0, 2, 3]],
                  'outer_edge_indices': [3, 2], 'inner_edge_indices': [0, 1]}
        plan = plan_frontage(np.array([[0., 6., 101.1], [7., 6., 101.1]]), [0., -1.], [street],
                             lambda p: np.full(len(p), 100.), box(-50, -50, 50, .535), Polygon())
        self.assert_valid_plan(plan)
        self.assertEqual(len(plan['stairs']), 1)
        surfaces = {s['id']: s for s in plan['surfaces']}
        landing = surfaces[plan['stairs'][0]['lower_landing_id']]
        np.testing.assert_allclose(np.array(landing['xy'])[[2, 3], 1], .5, atol=.005)
        np.testing.assert_allclose(np.array(landing['top'])[[2, 3]], 100.065, atol=.002)
        self.assertLess(Polygon(landing['xy']).intersection(Polygon(street['xy'])).area, 1e-8)

    def test_downhill_house_has_connected_uphill_steps_with_bounded_risers(self):
        plan = self.frontage(distance=8., level=99.)
        self.assert_valid_plan(plan)
        self.assertEqual(len(plan['stairs']), 1)
        ids = plan['stairs'][0]['tread_surface_ids']
        surfaces = {s['id']: s for s in plan['surfaces']}
        levels = np.array([np.mean(surfaces[key]['top']) for key in ids])
        self.assertTrue(np.all(np.diff(levels) > 0))
        self.assertLessEqual(np.diff(levels).max(), .175 + 1e-8)

    def test_road_junction_and_neighbour_are_hard_footprint_exclusions(self):
        obstacle = box(5.3, 1.8, 9., 4.2)
        neighbour = box(-1., 1.3, 1.3, 4.6)
        plan = self.frontage(distance=8., level=101.1, obstacle=obstacle, neighbour=neighbour)
        self.assert_valid_plan(plan)
        public = box(-50, -50, 50, 0)
        for surface in plan['surfaces'] + plan['terrain_surfaces']:
            p = Polygon(surface['xy'])
            for forbidden in (public, obstacle, neighbour):
                self.assertLess(p.intersection(forbidden).area, 1e-8)
        for wall in plan['retaining_edges']:
            edge = np.array(wall['xy'])
            outer = edge + np.array(wall['outward']) * wall['width_m']
            p = Polygon([edge[0], edge[1], outer[1], outer[0]])
            for forbidden in (public, obstacle, neighbour):
                self.assertLess(p.intersection(forbidden).area, 1e-8)

    def test_insufficient_space_extreme_height_and_flat_sites_are_skipped(self):
        self.assertIsNone(self.frontage(distance=.8))
        self.assertIsNone(self.frontage(level=103.))
        self.assertIsNone(self.frontage(level=96.))
        self.assertIsNone(self.frontage(level=100.))

    def test_world_coordinate_translation_preserves_construction_and_source_inputs(self):
        first = self.frontage()
        offset = np.array([462046.011314652, 5556359.471014719])
        shifted = self.frontage(offset=offset)
        self.assert_valid_plan(shifted)
        self.assertEqual([s['kind'] for s in first['surfaces']], [s['kind'] for s in shifted['surfaces']])
        for a, b in zip(first['surfaces'], shifted['surfaces']):
            np.testing.assert_allclose(np.array(b['xy'])-offset, a['xy'], atol=2e-8)
            np.testing.assert_allclose(b['top'], a['top'], atol=2e-8)
        edge = np.array([[0., 6., 101.1], [7., 6., 101.1]])
        original = edge.copy()
        plan_frontage(edge, np.array([0., -1.]), [], lambda p: np.full(len(p), 100.),
                      box(-50, -50, 50, 0), Polygon())
        np.testing.assert_array_equal(edge, original)

    @staticmethod
    def building(x=0., y=6., level=101.1):
        footprint = box(x, y, x+7., y+5.)
        def wall(z):
            return np.array([[x, y, z], [x+7., y, z], [x+7., y, z+3.],
                             [x, y, z+3.], [x, y, z]])
        return footprint, [wall(level+6.), wall(level)]

    def whole_plan(self, buildings, *, line=None, bounds=None):
        if line is None:
            line = LineString([[-30., -1.], [70., -1.]])
        if bounds is None:
            bounds = [-40, -20, 80, 30]
        with patch('roadtoassetto.ground_details._ground_buildings', return_value=buildings):
            return plan_hillside(ROOT/'data/altenhain', line, box(-30, -3, 70, 0), bounds,
                                 [], lambda p: np.full(len(p), 100.))

    def test_lowest_source_frontage_excludes_duplicate_upper_storey_seams(self):
        plan = self.whole_plan([self.building()])
        self.assertEqual(plan['stats']['hillside_terraces'], 1)
        self.assertEqual(len(plan['stats']['hillside_construction_records']), 1)
        np.testing.assert_allclose(np.array(plan['stats']['hillside_construction_records'][0]['source_wall'])[:, 2], 101.1)
        self.assertTrue(all(max(s['top']) < 102 for s in plan['surfaces']))

    def test_repeatable_json_and_lowest_source_order(self):
        building = self.building()
        first = self.whole_plan([building])
        second = self.whole_plan([copy.deepcopy(building)])
        self.assertEqual(json.dumps(first, sort_keys=True), json.dumps(second, sort_keys=True))
        reversed_rings = (building[0], list(reversed(building[1])))
        third = self.whole_plan([reversed_rings])
        self.assertEqual(first['surfaces'], third['surfaces'])

    def test_crop_keeps_unaffected_interior_construction_in_world_coordinates(self):
        main = self.building(x=10.)
        first = self.whole_plan([self.building(x=-20.), main])
        cropped = self.whole_plan([main], line=LineString([[0., -1.], [40., -1.]]), bounds=[0, -15, 40, 25])
        full_surfaces = [s for s in first['surfaces'] if np.array(s['xy'])[:, 0].mean() > 5.]
        self.assertTrue(full_surfaces and cropped['surfaces'])
        self.assertEqual(len(full_surfaces), len(cropped['surfaces']))
        for a, b in zip(full_surfaces, cropped['surfaces']):
            self.assertEqual(a['id'], b['id'])
            self.assertEqual(a['kind'], b['kind'])
            np.testing.assert_allclose(a['xy'], b['xy'], atol=1e-8)
            np.testing.assert_allclose(a['top'], b['top'], atol=1e-8)


class HillsideExportAuditTests(unittest.TestCase):
    def exported_ramp(self):
        origin=np.array([462046.011314652,5556359.471014719])
        xy=origin+np.array([[0.,0.],[2.6,0.],[2.6,6.],[0.,6.]])
        top=np.array([100.4,100.4,100.,100.])
        positions=np.c_[xy[:,0]-origin[0],top-100.,origin[1]-xy[:,1]].astype(np.float32)
        faces=np.array([[0,1,2],[0,2,3]],dtype=np.int32)
        def mesh(name,p):
            return dict(name=name,positions=p,indices=faces.ravel().copy(),visible=True,renderable=True)
        model={'meshes':[mesh('1GRASS_TERRAIN_0',positions-np.array([0.,.035,0.],dtype=np.float32)),
                         mesh('HILLSIDE_DRIVEWAY_0',positions)]}
        metadata={'origin_utm32':origin.tolist(),'height_origin_m':100.,
                  'hillside_rendered_surface_counts':{'terrace':0,'landing':0,'stair':0,'driveway':1,'bank_ledge':0,'footpath':0},
                  'hillside_limits':{'maximum_stair_riser_m':.175,'minimum_stair_tread_m':.30,
                                     'maximum_driveway_gradient':.14,'maximum_driveway_crossfall':.05,
                                     'maximum_footpath_gradient':.12,'maximum_footpath_crossfall':.08},
                  'hillside_driveway_records':[{'id':'source-ramp','xy':xy.tolist(),'top':top.tolist(),
                                               'faces':faces.tolist()}]}
        return model,metadata

    def test_actual_float32_ramp_and_plan_pass_all_geometry_checks(self):
        model,meta=self.exported_ramp()
        checks,groups=hillside_contact_checks(model,meta)
        self.assertTrue(all(checks.values()),checks)
        self.assertLessEqual(groups['hillside_driveway']['maximum_clearance_m'],.036)
        self.assertLessEqual(groups['hillside_driveway']['record_checks'][0]['maximum_plan_height_error_m'],.0001)

    def test_corrupted_exported_plane_fails_without_trusting_declared_gradients(self):
        model,meta=self.exported_ramp()
        model['meshes'][1]['positions'][[1,2],1]+=.4
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_driveway_actual_planes_bounded'])
        self.assertFalse(checks['hillside_driveway_actual_record_geometry'])

    def test_plan_comparison_uses_export_rounding_but_detects_real_boundary_changes(self):
        model,meta=self.exported_ramp()
        record=meta['hillside_driveway_records'][0]
        origin=np.asarray(meta['origin_utm32'])
        angle=.317
        rotation=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
        xy=(np.asarray(record['xy'])-origin)@rotation.T+[12.1234567,-3.89123456]
        record['xy']=(xy+origin).tolist()
        local=np.c_[xy[:,0],np.asarray(record['top'])-100.,-xy[:,1]]
        model['meshes'][1]['positions']=local.round(5).astype(np.float32)
        model['meshes'][0]['positions']=model['meshes'][1]['positions']-np.array([0.,.035,0.],dtype=np.float32)
        checks,groups=hillside_contact_checks(model,meta)
        self.assertTrue(all(checks.values()),checks)
        result=groups['hillside_driveway']['record_checks'][0]
        self.assertEqual(result['maximum_plan_height_error_m'],0.)
        self.assertEqual(result['uncovered_plan_area_m2'],0.)
        model['meshes'][1]['positions'][0,0]+=.005
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_driveway_actual_record_geometry'])

    def test_terrain_protrusion_and_reversed_face_are_detected(self):
        model,meta=self.exported_ramp()
        model['meshes'][0]['positions'][:,1]+=.10
        model['meshes'][1]['indices'][:3]=[2,1,0]
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_driveway_all_faces_upward'])
        self.assertFalse(checks['hillside_driveway_terrain_not_above_top'])

    def test_empty_crop_categories_are_valid_but_missing_declared_geometry_is_not(self):
        model,meta=self.exported_ramp()
        model['meshes']=model['meshes'][:1]
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_driveway_count_matches_mesh_presence'])
        meta['hillside_rendered_surface_counts']['driveway']=0
        meta['hillside_driveway_records']=[]
        checks,_=hillside_contact_checks(model,meta)
        self.assertTrue(all(checks.values()),checks)

    def test_footpath_crossfall_uses_pedestrian_limits_and_keeps_vehicle_limits(self):
        model,meta=self.exported_ramp()
        record=meta['hillside_driveway_records'][0]
        z=np.asarray(record['top']);z[[1,2]]+=2.6*.07
        record['top']=z.tolist()
        model['meshes'][1]['positions'][:,1]=(z-100.).round(5).astype(np.float32)
        model['meshes'][0]['positions']=model['meshes'][1]['positions']-np.array([0.,.035,0.],dtype=np.float32)
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_driveway_actual_record_geometry'])
        model['meshes'][1]['name']='HILLSIDE_FOOTPATH_0'
        meta['hillside_rendered_surface_counts'].update(driveway=0,footpath=1)
        meta['hillside_footpath_records']=meta.pop('hillside_driveway_records')
        checks,_=hillside_contact_checks(model,meta)
        self.assertTrue(all(checks.values()),checks)

    def test_bank_ledge_uses_normal_support_limit(self):
        model,meta=self.exported_ramp()
        model['meshes'][1]['name']='HILLSIDE_BANK_LEDGE_0'
        meta['hillside_rendered_surface_counts'].update(driveway=0,bank_ledge=1)
        meta['hillside_driveway_records']=[]
        checks,_=hillside_contact_checks(model,meta)
        self.assertTrue(all(checks.values()),checks)
        model['meshes'][0]['positions'][:,1]-=.15
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_bank_ledge_ground_supported'])

    def test_sloped_tread_is_rejected_even_when_ground_contact_is_correct(self):
        model,meta=self.exported_ramp()
        model['meshes'][1]['name']='HILLSIDE_STAIR_TREAD_0'
        meta['hillside_rendered_surface_counts'].update(driveway=0,stair=1)
        meta['hillside_driveway_records']=[]
        checks,_=hillside_contact_checks(model,meta)
        self.assertFalse(checks['hillside_stair_actual_planes_bounded'])
        self.assertTrue(checks['hillside_stair_ground_supported'])

    def test_narrow_internal_terrain_riser_is_detected_between_clear_tread_corners(self):
        # A crop exposed a 4.886-mm terrain sliver near a shared landing edge.
        # Its tread corners were clear: the audit must also test interior
        # intersections of the actual stored terrain and tread triangles.
        model,meta=self.exported_ramp()
        top=np.array([[0.,0.,0.],[1.,0.,0.],[1.,0.,-1.],[0.,0.,-1.]],dtype=np.float32)
        terrain=np.vstack([top-np.array([0.,.035,0.],dtype=np.float32),
                           np.array([[.0005,.00488631685,-.6]],dtype=np.float32)])
        model['meshes'][0]['positions']=terrain
        model['meshes'][0]['indices']=np.array([[0,1,4],[1,2,4],[2,3,4],[3,0,4]],dtype=np.int32).ravel()
        model['meshes'][1]['positions']=top
        model['meshes'][1]['name']='HILLSIDE_STAIR_TREAD_0'
        meta['hillside_rendered_surface_counts'].update(driveway=0,stair=1)
        meta['hillside_driveway_records']=[]
        checks,groups=hillside_contact_checks(model,meta)
        self.assertTrue(checks['hillside_stair_all_faces_upward'])
        self.assertTrue(checks['hillside_stair_actual_planes_bounded'])
        self.assertTrue(checks['hillside_stair_terrain_covers_footprint'])
        self.assertTrue(checks['hillside_stair_ground_supported'])
        self.assertFalse(checks['hillside_stair_terrain_not_above_top'])
        self.assertAlmostEqual(groups['hillside_stair']['minimum_clearance_m'],-.004886,places=6)


if __name__ == '__main__':
    unittest.main()
