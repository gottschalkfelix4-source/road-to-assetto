"""Fault injection tests for the independent exported-basement audit."""
import copy
import unittest

import numpy as np
from shapely.geometry import box, mapping

from scripts.verify_building_bases import (audit_building_bases, face_coverage,
    first_ray_hit, horizontal_intrusion, terrain_section, triangle_signature,
    wall_ground_closure,stored_mesh_hash)
from roadtoassetto.terrain_sampling import ExportedTerrainSampler


def model_fixture():
    # A manually specified closed box; no planner/renderer imports.
    p=np.array([[0,-.15,0],[2,-.15,0],[2,-.15,-.12],[0,-.15,-.12],
                [0,1.5,0],[2,1.5,0],[2,1.5,-.12],[0,1.5,-.12]],dtype=np.float32)
    f=np.array([[0,1,5],[0,5,4],[1,2,6],[1,6,5],[2,3,7],[2,7,6],
                [3,0,4],[3,4,7],[0,3,2],[0,2,1],[4,5,6],[4,6,7]],dtype=np.uint16)
    g=np.array([[-3,0,3],[4,0,3],[4,0,-3],[-3,0,-3]],dtype=np.float32)
    gf=np.array([[0,1,2],[0,2,3]],dtype=np.uint16)
    def mesh(name,p,f):
        return dict(name=name,positions=p,indices=f.ravel(),visible=True,renderable=True)
    model={'meshes':[mesh('1GRASS_TERRAIN_P0',g,gf),mesh('BASEMENT_PLINTHS_M2_P0',p,f)]}
    world=np.c_[p[:,0],-p[:,2],p[:,1]]
    record={'id':'closed-base','building_id':'house-a','source_surface_id':'wall-a','kind':'plinth',
            'mesh_prefix':'BASEMENT_PLINTHS','vertices_utm_xyz':world.tolist(),'faces':f.tolist(),
            'footprint_utm':[{'outer':[[0,0],[2,0],[2,.12],[0,.12],[0,0]],'holes':[]}],
            'source_edge_xyz':[[0,0,1.5],[2,0,1.5]],'outward_xyz':[0,-1,0],
            'ground_contact_xy':[[0,0],[2,0]],'ground_contact_z':[0,0],'embed_depth_m':.15}
    metadata={'origin_utm32':[0,0],'height_origin_m':0,'building_base_records':[record],
              'building_base_stats':{'buildings':1,'panels':1,'soffits':0},
              'building_base_source_footprints':[{'building_id':'house-a','geometry':mapping(box(0,0,2,2))}]}
    return model,metadata


class BuildingBaseAuditTests(unittest.TestCase):
    def test_closed_manual_shell_passes(self):
        checks,_=audit_building_bases(*model_fixture())
        self.assertTrue(all(checks.values()),checks)

    def test_missing_half_front_is_not_hidden_by_intact_corners(self):
        model,meta=model_fixture()
        model['meshes'][1]['indices']=model['meshes'][1]['indices'][3:]
        checks,groups=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_record_surfaces_closed'])
        self.assertFalse(checks['building_base_source_to_terrain_closed'])
        self.assertGreater(groups['records'][0]['coverage']['uncovered_area_m2'],1.)

    def test_reversed_front_cannot_close_visible_shell(self):
        model,meta=model_fixture()
        faces=model['meshes'][1]['indices'].reshape(-1,3).copy()
        faces[:2]=faces[:2,::-1]
        model['meshes'][1]['indices']=faces.ravel()
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_record_surfaces_closed'])
        self.assertFalse(checks['building_base_source_to_terrain_closed'])

    def test_displaced_extra_geometry_is_detected(self):
        model,meta=model_fixture()
        extra=copy.deepcopy(model['meshes'][1]);extra['name']='BASEMENT_PLINTHS_extra'
        extra['positions'][:,0]+=10
        model['meshes'].append(extra)
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_all_exported_faces_accounted_for'])

    def test_empty_crop_is_valid_but_record_without_mesh_fails(self):
        model,meta=model_fixture();model['meshes']=model['meshes'][:1]
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_records_match_mesh_presence'])
        meta['building_base_records']=[]
        meta['building_base_stats']={'buildings':0,'panels':0,'soffits':0}
        checks,_=audit_building_bases(model,meta)
        self.assertTrue(all(checks.values()),checks)

    def test_ground_dip_between_clear_endpoints_exposes_a_hole(self):
        model,meta=model_fixture()
        g=np.array([[-3,0,3],[4,0,3],[4,0,-3],[-3,0,-3],[1,-.6,0]],dtype=np.float32)
        faces=np.array([[0,1,4],[1,2,4],[2,3,4],[3,0,4]])
        model['meshes'][0].update(positions=g,indices=faces.ravel())
        checks,groups=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_source_to_terrain_closed'])
        self.assertGreater(groups['source_seams'][0]['terrain_breakpoints'],2)
        self.assertGreater(groups['source_seams'][0]['unclosed_area_m2'],.2)

    def test_zero_width_vertical_wall_crossing_road_is_detected(self):
        triangle=np.array([[[0,0,0],[2,0,0],[2,2,0]]],dtype=float)
        intrusion=horizontal_intrusion(triangle,box(.5,-.5,1.5,.5))
        self.assertEqual(intrusion['area_m2'],0)
        self.assertAlmostEqual(intrusion['length_m'],1.)

    def test_exported_base_entering_road_fails(self):
        model,meta=model_fixture()
        road=np.array([[.5,0,1],[1.5,0,1],[1.5,0,-1],[.5,0,-1]],dtype=np.float32)
        model['meshes'].append(dict(name='1ROAD_MAIN',positions=road,indices=np.array([0,1,2,0,2,3])))
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_clear_of_actual_road'])
        model['meshes'][-1]['name']='JUNCTION_ASPHALT_side_street'
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_clear_of_actual_road'])

    def test_body_cannot_expand_into_neighbour_by_claiming_a_wider_record(self):
        model,meta=model_fixture()
        meta['building_base_source_footprints']=[
            {'building_id':'house-a','geometry':mapping(box(0,0,1,2))},
            {'building_id':'house-b','geometry':mapping(box(1.2,0,3,2))}]
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_within_source_footprints'])
        self.assertFalse(checks['building_base_clear_of_neighbour_additions'])

    def test_original_house_shoulder_overlap_does_not_allow_new_intrusion(self):
        model,meta=model_fixture()
        shoulder=np.array([[1.9,0,1],[2.4,0,1],[2.4,0,-1],[1.9,0,-1]],dtype=np.float32)
        model['meshes'].append(dict(name='1SHOULDER_LEFT',positions=shoulder,indices=np.array([0,1,2,0,2,3])))
        checks,groups=audit_building_bases(model,meta)
        self.assertTrue(all(checks.values()),checks)
        self.assertGreater(groups['inherited_shoulder_under_original_buildings']['area_m2'],0)
        # Moving the complete, internally consistent new component cannot
        # expand the exception, which comes only from the original contour.
        model['meshes'][1]['positions'][:,0]+=.15
        record=meta['building_base_records'][0]
        for p in record['vertices_utm_xyz']:p[0]+=.15
        for p in record['source_edge_xyz']:p[0]+=.15
        for p in record['footprint_utm'][0]['outer']:p[0]+=.15
        checks,_=audit_building_bases(model,meta)
        self.assertFalse(checks['building_base_clear_of_actual_road'])
        self.assertFalse(checks['building_base_within_source_footprints'])

    def test_matching_record_cannot_hide_insufficient_terrain_embed(self):
        model,meta=model_fixture()
        model['meshes'][1]['positions'][:4,1]=-.01
        record=meta['building_base_records'][0]
        for p in record['vertices_utm_xyz'][:4]:p[2]=-.01
        checks,_=audit_building_bases(model,meta)
        self.assertTrue(checks['building_base_record_surfaces_closed'])
        self.assertFalse(checks['building_base_source_to_terrain_closed'])

    def test_original_geometry_and_uv_are_compared_to_stored_baseline(self):
        model,meta=model_fixture()
        original=copy.deepcopy(model['meshes'][1]);original['name']='LOD2_WALLS_GROUND_M0_P0'
        original['uv']=np.zeros((8,2),dtype=np.float32)
        model['meshes'].append(original)
        meta.update(centerline_ac=[[0,0,0],[2,0,0]],road_width_profile_m=[5,5],length_m=2)
        fields=('centerline_ac','road_width_profile_m','length_m','origin_utm32','height_origin_m')
        baseline={'mesh_stored_geometry_sha256':{original['name']:stored_mesh_hash(original)},
                  'road_reference':{k:meta[k] for k in fields}}
        checks,_=audit_building_bases(model,meta,baseline=baseline)
        self.assertTrue(checks['building_base_original_wall_roof_photo_geometry_preserved'])
        self.assertTrue(checks['building_base_original_road_reference_preserved'])
        original['uv'][0,0]=.01
        checks,_=audit_building_bases(model,meta,baseline=baseline)
        self.assertFalse(checks['building_base_original_wall_roof_photo_geometry_preserved'])

    def test_rays_require_outward_occluder_before_old_backface(self):
        model,meta=model_fixture()
        probe={'id':'old-open-view','camera_ac':[1,.7,4],'target_ac':[1,.7,-1]}
        checks,_=audit_building_bases(model,meta,ray_probes=[probe])
        self.assertTrue(checks['building_base_known_open_view_closed'])
        model['meshes'][1]['positions'][:,2]-=5
        checks,_=audit_building_bases(model,meta,ray_probes=[probe])
        self.assertFalse(checks['building_base_known_open_view_closed'])

    def test_coverage_checks_real_surface_not_just_equal_area(self):
        a=np.array([[[0,0,0],[1,0,0],[0,1,0]]],dtype=float)
        b=a.copy();b[:,:,0]+=2
        result=face_coverage(a,b)
        self.assertAlmostEqual(result['uncovered_area_m2'],.5)
        self.assertAlmostEqual(face_coverage(a,a[:,::-1])['uncovered_area_m2'],.5)

    def test_triangle_signature_ignores_order_but_preserves_orientation(self):
        a=np.array([[[0,0,0],[1,0,0],[0,1,0]],[[0,0,0],[0,1,0],[0,0,1]]],dtype=float)
        self.assertEqual(triangle_signature(a),triangle_signature(np.roll(a[::-1],1,axis=1)))
        self.assertNotEqual(triangle_signature(a),triangle_signature(a[:,::-1]))


if __name__=='__main__':
    unittest.main()
