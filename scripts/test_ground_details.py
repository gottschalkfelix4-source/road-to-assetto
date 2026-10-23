"""Terrain conservation and clearance checks, using small synthetic fixtures."""
from pathlib import Path
import sys
import unittest
import numpy as np
from shapely.geometry import box, Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.ground_details import (RoadGroundProfile,StreetGroundProfile,terrain_samples,
    build_terrain_mesh,apron_panels,PavementContactIndex,connection_surfaces,_emit_connected_ground,
    protected_road_surfaces,foundation_ground_surfaces)
from roadtoassetto.terrain_sampling import ExportedTerrainSampler


class GroundDetailTests(unittest.TestCase):
    def section(self,start=5,end=25,inside=3.5,outside=4.6,height=100.12):
        return {'xy':[[start,inside],[end,inside],[end,outside],[start,outside]],
                'top':[height,height,height,height],'faces':[[0,1,2],[0,2,3]],
                'outer_edge_indices':[3,2],'inner_edge_indices':[0,1],
                'side':1,'style':'red_concrete'}

    def test_local_street_profile_preserves_road_far_dgm_and_exact_paving_clearance(self):
        base=RoadGroundProfile([[0,0],[35,0]],[100,100],3,crossfall=.025)
        paving=self.section(height=100.1)
        profile=StreetGroundProfile(base,[paving],box(0,-3,35,3))
        p=np.array([[10,0],[10,3.2],[10,4],[10,4.6],[10,5],[10,5.4],[10,8],[0,8]])
        raw=np.array([101,100.5,101.4,99.6,100.8,100.7,102.5,104.])
        before=base(p,raw)
        after=profile(p,raw)
        np.testing.assert_array_equal(after[[0,1,5,6,7]],before[[0,1,5,6,7]])
        np.testing.assert_allclose(after[[2,3]]-.08,100.1-.035,atol=1e-10)
        self.assertGreater(after[4],min(before[4],100.145))
        self.assertLess(after[4],max(before[4],100.145))
        self.assertGreater(len(profile.constraint_points()),60)

    def test_exported_terrain_cannot_wave_through_accepted_paving(self):
        base=RoadGroundProfile([[4,10],[30,10]],[100,100.35],3)
        paving={'xy':[[5,13.5],[29,13.5],[29,15.2],[5,15.2]],
                'top':[100.15,100.47,100.4955,100.1755],'faces':[[0,1,2],[0,2,3]],
                'outer_edge_indices':[3,2],'inner_edge_indices':[0,1]}
        profile=StreetGroundProfile(base,[paving],box(4,7,30,13))
        scene={'meshes':[]};ac=lambda p,z:np.c_[p[:,0],z,-p[:,1]]
        def add(name,vertices,faces,material,uv,collision):
            scene['meshes'].append(dict(name=name,vertices=np.asarray(vertices).round(5).tolist(),faces=np.asarray(faces).tolist()))
        stats=build_terrain_mesh([0,0,35,25],4,profile,
            lambda p:100.3+.35*np.sin(p[:,0]*.8)+.18*np.cos(p[:,1]*2),ac,add,0,[0,0,35,25],scene=scene)
        sampler=ExportedTerrainSampler.from_scene(scene,[0,0],0)
        xx,yy=np.meshgrid(np.linspace(5.05,28.95,85),np.linspace(13.55,15.15,19))
        p=np.c_[xx.ravel(),yy.ravel()]
        top=100.15+(p[:,0]-5)*(.32/24)+(p[:,1]-13.5)*.015
        clearance=top-sampler(p)
        self.assertGreater(clearance.min(),.02)
        self.assertLess(clearance.max(),.06)
        self.assertGreater(stats['terrain_contact_support_points'],0)

    def test_house_connection_reaches_actual_sidewalk_and_uses_its_surface(self):
        paving=self.section(outside=4.6,height=100.02)
        index=PavementContactIndex([paving])
        edge=np.array([[9,5.2,100.04],[13,5.2,100.04]])
        saved=edge.copy()
        cells=connection_surfaces(edge,[0,-1],index,lambda p:np.full(len(p),100),box(0,-3,35,3),Polygon())
        self.assertEqual(len(cells),4)
        np.testing.assert_array_equal(edge,saved)
        for cell in cells:
            p=np.asarray(cell['xy']);z=np.asarray(cell['top'])
            self.assertEqual(cell['style'],'red_concrete')
            np.testing.assert_allclose(p[2:,1],4.6)
            np.testing.assert_allclose(z[2:],100.02)
            self.assertAlmostEqual(p[0,1],5.2-.018)
            self.assertEqual(cell['step_count'],0)
        self.assertEqual(connection_surfaces(edge,[0,-1],PavementContactIndex([]),lambda p:np.full(len(p),100),Polygon(),Polygon()),[])

    def test_rotated_utm_constraints_survive_scene_and_float32_export(self):
        # A non-planar paving quad, not aligned with the DGM lattice, next to
        # steep source terrain. Include every actual terrain/top intersection
        # vertex, so a hidden triangle cutting through a corner cannot pass.
        from scripts.verify_street_contacts import contact_vertices
        origin=np.array([462046.011314652,5556359.471014719])
        angle=.317;rotation=np.array([[np.cos(angle),-np.sin(angle)],[np.sin(angle),np.cos(angle)]])
        world=lambda p:np.asarray(p)@rotation.T+origin
        local=lambda p:(np.asarray(p)-origin)@rotation
        road=RoadGroundProfile(world([[4,10],[30,10]]),[100,100.35],3)
        paving={'xy':world([[5,13.46],[29,13.46],[29,15.2],[5,15.2]]).tolist(),
                'top':[100.15,100.47,100.60,100.18],'faces':[[0,1,2],[0,2,3]],
                'outer_edge_indices':[3,2],'inner_edge_indices':[0,1]}
        polygon=Polygon(world([[4,7],[30,7],[30,13],[4,13]]))
        profile=StreetGroundProfile(road,[paving],polygon)
        extent=world([[0,0],[35,0],[35,25],[0,25]])
        bounds=np.r_[extent.min(axis=0)-2,extent.max(axis=0)+2]
        scene={'meshes':[]}
        ac=lambda p,z:np.c_[p[:,0]-origin[0],z-100,origin[1]-p[:,1]]
        def add(name,v,f,m,u,c):
            scene['meshes'].append(dict(name=name,vertices=np.asarray(v).round(5).tolist(),faces=np.asarray(f).tolist()))
        build_terrain_mesh(bounds,4,profile,
            lambda p:100.3+1.2*np.sin(local(p)[:,0]*.8)+.7*np.cos(local(p)[:,1]*2),
            ac,add,0,bounds,scene=scene)
        sampler=ExportedTerrainSampler.from_scene(scene,origin,100)
        q=np.asarray(paving['xy']);top=np.asarray(paving['top'])
        top_triangles=ac(q,top).round(5).astype(np.float32).astype(float)[np.asarray(paving['faces'])]
        intersections,coverage=contact_vertices(top_triangles,sampler)
        self.assertLessEqual(coverage['uncovered_area_m2'],1e-5)
        p=np.c_[intersections[:,0]+origin[0],origin[1]-intersections[:,2]]
        clearance=intersections[:,1]+100-sampler(p)
        self.assertGreater(clearance.min(),.025)
        self.assertLess(clearance.max(),.05)
        for mesh in scene['meshes']:
            tri=np.asarray(mesh['vertices'],dtype=np.float32).astype(float)[np.asarray(mesh['faces'])]
            self.assertTrue(np.all(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])[:,1]>1e-6))

    def test_protected_driving_planes_override_source_dgm_without_mutating_meshes(self):
        origin=np.array([462046.011314652,5556359.471014719]);h0=100.
        scene={'meshes':[{'name':'1ROAD_MAIN','vertices':[[0,0,0],[20,.5,0],[20,.425,-3],[0,-.075,-3]],
                          'faces':[[0,1,2],[0,2,3]]},
                         {'name':'1SHOULDER_LEFT','vertices':[[0,-.09,-3],[20,.41,-3],[20,.41,-3.4],[0,-.09,-3.4]],
                          'faces':[[0,1,2],[0,2,3]]}]}
        original=repr(scene)
        protected=protected_road_surfaces(scene,origin,h0)
        road=RoadGroundProfile(origin+[[0,0],[20,0]],[100,100.5],3,crossfall=.025)
        profile=StreetGroundProfile(road,[],Polygon(origin+[[0,-3],[20,-3],[20,3],[0,3]]),protected_surfaces=protected)
        query=origin+[[10,1.5],[10,3.2]]
        np.testing.assert_allclose(profile(query,[110,110])-.08,[100.1325,100.08],atol=1e-8)
        self.assertEqual(repr(scene),original)

    def test_road_tie_in_uses_fixed_outer_grade_without_rounded_berm(self):
        source=lambda p:100+.8*(np.asarray(p)[:,1]-3)
        profile=RoadGroundProfile([[0,0],[20,0]],[100,100],3,source_sampler=source)
        p=np.c_[np.full(21,10.),np.linspace(3.45,5.45,21)]
        values=profile(p,source(p))
        np.testing.assert_allclose(values,np.linspace(100,101.96,21),atol=1e-10)
        self.assertTrue(np.all(np.diff(values)>=0))
        far=np.array([[10,6.5],[10,9.]])
        np.testing.assert_array_equal(profile(far,source(far)),source(far))

    def test_sidewalk_outer_transition_is_monotone_and_conserves_outer_dgm(self):
        source=lambda p:100+.8*(np.asarray(p)[:,1]-4.6)+.15*np.sin(9*(np.asarray(p)[:,1]-4.6))
        base=RoadGroundProfile([[0,0],[35,0]],[100,100],3,blend=.2,source_sampler=source)
        profile=StreetGroundProfile(base,[self.section(height=100.1)],box(0,-3,35,3),source_sampler=source)
        p=np.c_[np.full(17,10.),np.linspace(4.6,5.4,17)]
        result=profile(p,source(p))
        np.testing.assert_allclose(result,np.linspace(100.145,source(p[-1:])[0],17),atol=1e-9)
        self.assertTrue(np.all(np.diff(result)>0))
        far=np.array([[10,6.],[10,7.]])
        np.testing.assert_array_equal(profile(far,source(far)),base(far,source(far)))

    def test_foundation_strips_are_local_bounded_and_avoid_public_paving(self):
        edge=np.array([[9,5.2,100],[13,5.2,100]])
        empty=Polygon()
        cells=foundation_ground_surfaces(edge,[0,-1],lambda p:np.full(len(p),100.2),empty,empty)
        self.assertEqual(len(cells),4)
        for cell in cells:
            p=np.array(cell['xy']);top=np.array(cell['top'])
            self.assertAlmostEqual(p[0,1],5.2-.018)
            self.assertAlmostEqual(p[2,1],5.2-.45)
            np.testing.assert_allclose(top[:2],99.955)
            self.assertLessEqual(cell['maximum_cut_m'],.45)
            self.assertLessEqual(cell['maximum_fill_m'],.30)
        for ground in (100.6,99.5,107):
            self.assertEqual(foundation_ground_surfaces(edge,[0,-1],lambda p:np.full(len(p),ground),empty,empty),[])
        obstacle=box(8,4.6,14,5.1)
        self.assertEqual(foundation_ground_surfaces(edge,[0,-1],lambda p:np.full(len(p),100.2),obstacle,empty),[])
        self.assertEqual(foundation_ground_surfaces(edge,[0,-1],lambda p:np.full(len(p),100.2),empty,obstacle),[])

    def test_foundation_mesh_uses_existing_gravel_and_shared_ground_plan(self):
        cells=foundation_ground_surfaces(np.array([[9,5.2,100],[13,5.2,100]]),[0,-1],
            lambda p:np.full(len(p),100.2),Polygon(),Polygon())
        scene={'materials':[],'meshes':[]}
        def material(name,texture,**kw):
            scene['materials'].append(dict(name=name,texture=texture,**kw));return len(scene['materials'])-1
        def add(name,v,f,mat,uv,collision):
            scene['meshes'].append(dict(name=name,vertices=np.array(v),faces=np.array(f),material=mat,collision=collision))
        result=_emit_connected_ground(scene,material,add,lambda p,z:np.c_[p[:,0],z,-p[:,1]],
            lambda p:np.full(len(p),99.92),{'contact_surfaces':[],'foundation_surfaces':cells})
        self.assertEqual(result['house_ground_apron_panels'],0)
        self.assertEqual(result['house_foundation_ground_panels'],4)
        self.assertNotIn('foundation_surfaces',result)
        tops=[m for m in scene['meshes'] if m['name'].startswith('HOUSE_FOUNDATION_GRAVEL')]
        self.assertTrue(tops)
        self.assertEqual(scene['materials'][tops[0]['material']]['texture'],'shoulder_gravel.jpg')
        self.assertTrue(all(not m['collision'] for m in scene['meshes']))

    def test_rotated_foundation_support_contains_unsnapped_visible_corners(self):
        # This real UTM orientation previously left the outer gravel corner
        # 0.86 mm beyond its 2-mm terrain contour. A steep outside triangle
        # then crossed the visible top despite having no missing ground.
        from shapely import set_precision
        edge=np.array([[462030.95103049686,5556357.743642245,236.125],
                       [462029.93571207614,5556358.112250103,236.125]])
        tangent=(edge[1,:2]-edge[0,:2])/np.linalg.norm(edge[1,:2]-edge[0,:2])
        outward=np.array([tangent[1],-tangent[0]])
        cells=foundation_ground_surfaces(edge,outward,lambda p:np.full(len(p),236.3),Polygon(),Polygon())
        self.assertEqual(len(cells),1)
        cell=cells[0];visible=np.asarray(cell['xy']);grade=np.asarray(cell['terrain_xy'])
        np.testing.assert_allclose(visible[:2],edge[:,:2]+outward*.018,atol=1e-9)
        np.testing.assert_allclose(visible[2:],(edge[:,:2]+outward*.45)[::-1],atol=1e-9)
        # Include the actual JSON/KN5 horizontal precision, not just source
        # vertices, and verify full polygon coverage after contour snapping.
        origin=np.array([462000.,5556300.])
        exported=(visible-origin).round(5).astype(np.float32).astype(float)+origin
        support=set_precision(Polygon(grade),.002)
        self.assertTrue(support.covers(Polygon(exported)))
        self.assertGreater(support.boundary.distance(Polygon(exported)),.002)
        # The hidden extension remains exactly on the existing plane.
        gradient=np.linalg.lstsq(visible-visible[0],np.asarray(cell['top'])-cell['top'][0],rcond=None)[0]
        expected=cell['top'][0]+(grade-visible[0])@gradient
        np.testing.assert_allclose(cell['terrain_top'],expected,atol=1e-9)

    def test_cropped_step_support_keeps_low_plane_beyond_unsnapped_edge(self):
        from shapely import set_precision
        from roadtoassetto.hillside import _horizontal_ground_support
        # First tread of the actual 100-400 m / 5.5 m cropped-route failure.
        # The shared terrain rise previously entered this original top edge.
        xy=np.array([[462007.22950164415,5556369.543879934],
                     [462006.55552491255,5556370.475682147],
                     [462006.7986037509,5556370.651502164],
                     [462007.4725804825,5556369.719699951]])
        height=235.63775357633756
        section={'xy':xy.tolist(),'top':[height]*4,'faces':[[0,1,2],[0,2,3]],
                 'kind':'stair','inner_edge_indices':[0,1],'outer_edge_indices':[3,2]}
        saved=repr(section);support=_horizontal_ground_support(section)
        self.assertEqual(repr(section),saved)
        self.assertEqual(support['top'],section['top'])
        self.assertEqual(support['faces'],section['faces'])
        origin=np.array([462073.32702708204,5556353.478475282])
        exported=(xy-origin).round(5).astype(np.float32).astype(float)+origin
        self.assertTrue(set_precision(Polygon(support['xy']),.002).covers(Polygon(exported)))
        # The overlap belongs to the lower surface even when the neighbouring
        # landing is more than 16 cm higher. Do not average the two planes.
        direction=xy[3]-xy[0]
        upper=dict(section,xy=(xy-direction).tolist(),top=[height+.164400368455]*4)
        road=RoadGroundProfile(origin+[[0,-20],[40,-20]],[235,235],2)
        profile=StreetGroundProfile(road,[],Polygon(origin+[[0,-22],[40,-22],[40,-18],[0,-18]]),
                                   hillside_surfaces=[upper,support])
        q=(xy[:2].mean(axis=0)-direction*.006)[None,:]
        actual=profile(q,np.array([236.]))-.08
        np.testing.assert_allclose(actual,height-.035,atol=1e-9)

    def test_foundation_support_reaches_actual_wall_and_keeps_distant_ground(self):
        source=lambda p:np.full(len(p),100.2)
        cells=foundation_ground_surfaces(np.array([[9,5.2,100],[13,5.2,100]]),[0,-1],
            source,Polygon(),Polygon())
        road=RoadGroundProfile([[0,0],[30,0]],[100,100],3,source_sampler=source)
        profile=StreetGroundProfile(road,[],box(0,-3,30,3),foundation_surfaces=cells,source_sampler=source)
        at_wall=np.array([[10.4,5.2],[11.4,5.2]])
        np.testing.assert_allclose(profile(at_wall,source(at_wall))-.08,99.92027,atol=1e-8)
        far=np.array([[10.4,7.],[11.4,8.]])
        np.testing.assert_array_equal(profile(far,source(far)),source(far))

    def test_small_step_connection_is_bounded_and_large_or_blocked_gaps_rejected(self):
        index=PavementContactIndex([self.section(outside=4.6,height=100)])
        edge=np.array([[9,6,100.30],[12,6,100.30]])
        ground=lambda p:np.full(len(p),100.1)
        cells=connection_surfaces(edge,[0,-1],index,ground,box(0,-3,35,3),Polygon())
        self.assertEqual(len(cells),9)
        self.assertTrue(all(c['step_count']==2 for c in cells))
        levels=[c['top'][0] for c in cells[:3]]
        self.assertTrue(np.all(abs(np.diff(levels))<=.17))
        self.assertAlmostEqual(levels[-1],100)
        for obstacle in (box(9,4.5,12,5.1),box(8,4.8,14,6.1)):
            self.assertEqual(connection_surfaces(edge,[0,-1],index,ground,obstacle,Polygon()),[])
            self.assertEqual(connection_surfaces(edge,[0,-1],index,ground,Polygon(),obstacle),[])
        high=edge.copy();high[:,2]+=1
        self.assertEqual(connection_surfaces(high,[0,-1],index,ground,Polygon(),Polygon()),[])

    def test_joined_step_terrain_stays_below_lower_shared_landing(self):
        base=RoadGroundProfile([[0,0],[35,0]],[100,100],3)
        paving=self.section(height=100)
        index=PavementContactIndex([paving])
        cells=connection_surfaces(np.array([[9,6,100.3],[12,6,100.3]]),[0,-1],index,
                                 lambda p:np.full(len(p),100.1),Polygon(),Polygon())
        profile=StreetGroundProfile(base,[paving],box(0,-3,35,3),contact_surfaces=cells)
        first,second=cells[:2]
        boundary=np.asarray(first['xy'])[2:]
        actual=profile(boundary,np.full(len(boundary),101))-.08
        self.assertTrue(np.all(actual<=np.asarray(second['top'])[:2].max()-.0349))

    def test_contact_render_uses_metric_matching_material_and_embedded_solid(self):
        index=PavementContactIndex([self.section(outside=4.6,height=100.02)])
        cells=connection_surfaces(np.array([[9,5.2,100.04],[13,5.2,100.04]]),[0,-1],index,
                                 lambda p:np.full(len(p),100),Polygon(),Polygon())
        scene={'materials':[],'meshes':[]}
        def material(name,texture,**kwargs):
            scene['materials'].append(dict(name=name,texture=texture,**kwargs));return len(scene['materials'])-1
        def add(name,v,f,mat,uv,collision):
            scene['meshes'].append(dict(name=name,vertices=np.asarray(v),faces=np.asarray(f),material=mat,uv=np.asarray(uv),collision=collision))
        ac=lambda p,z:np.c_[p[:,0],z,-p[:,1]]
        result=_emit_connected_ground(scene,material,add,ac,lambda p:np.full(len(p),99.99),
            {'contact_surfaces':cells,'house_ground_apron_buildings':1})
        self.assertEqual(result['house_ground_apron_panels'],4)
        self.assertEqual(result['house_connection_mortar_joints'],4)
        for mesh in scene['meshes']:
            self.assertFalse(mesh['collision'])
            self.assertTrue(np.isfinite(mesh['vertices']).all())
            if mesh['name'].startswith('HOUSE_CONTACT_PAVING'):
                self.assertEqual(scene['materials'][mesh['material']]['texture'],'de_paving_red.png')
                f=mesh['faces'];v=mesh['vertices'];uv=mesh['uv']
                np.testing.assert_allclose(np.linalg.norm(v[f[:,0]][:,[0,2]]-v[f[:,1]][:,[0,2]],axis=1),
                                           np.linalg.norm(uv[f[:,0]]-uv[f[:,1]],axis=1))
            if mesh['name'].startswith('HOUSE_CONTACT_EDGE'):
                self.assertLess(mesh['vertices'][:,1].min(),99.99)

    def test_synthetic_crown_continues_to_edge_and_preserves_far_ground(self):
        profile=RoadGroundProfile([[0,0],[20,0]],[100,100],3,crossfall=.025)
        p=np.array([[10,0],[10,1.5],[10,-1.5],[10,3],[10,-3],[10,3.4],[10,8]])
        values=profile(p,np.full(len(p),102.))
        np.testing.assert_allclose(values,[100,99.9625,99.9625,99.925,99.925,99.925,102.])
        with self.assertRaises(ValueError):
            RoadGroundProfile([[0,0],[20,0]],[100,100],3,crossfall=.1)

    def test_segment_height_and_variable_width_are_interpolated(self):
        profile = RoadGroundProfile([[0, 0], [10, 0]], [100, 101], [2, 4])
        distance, height, width = profile.sample([[2.5, 1], [7.5, -2]])
        np.testing.assert_allclose(distance, [1, 2])
        np.testing.assert_allclose(height, [100.25, 100.75])
        np.testing.assert_allclose(width, [2.5, 3.5])
        np.testing.assert_allclose(profile([[2.5, 1], [7.5, -2]], [110, 115]), height)

    def test_far_terrain_is_exactly_preserved_and_transition_is_bounded(self):
        profile = RoadGroundProfile([[0, 0], [20, 0]], [100, 100], 3)
        p = np.c_[np.full(41, 10.), np.linspace(3.45, 5.45, 41)]
        h = profile(p, np.full(41, 102.))
        self.assertAlmostEqual(h[0], 100)
        self.assertAlmostEqual(h[-1], 102)
        self.assertTrue(np.all(np.diff(h) >= 0))
        np.testing.assert_array_equal(profile([[2, 12], [5, -7]], [103.1, 99.8]), [103.1, 99.8])
        # Unlike the previous 6.5-m swath, points 2.5 m beyond the road retain DGM.
        np.testing.assert_array_equal(profile([[10, 5.5]], [103.7]), [103.7])

    def test_sharp_bend_uses_nearest_segment_and_finite_heights(self):
        profile = RoadGroundProfile([[0, 0], [10, 0], [10, 10]], [0, 1, 2], 2)
        d, h, width = profile.sample([[9, 5], [6, -1], [20, 10]])
        np.testing.assert_allclose(d, [1, 1, 10])
        np.testing.assert_allclose(h, [1.5, .6, 2])
        with self.assertRaises(ValueError):
            RoadGroundProfile([[0, 0], [0, 0]], [0, 1], 3)
        with self.assertRaises(ValueError):
            profile([[1, 1]], [np.nan])

    def test_adaptive_mesh_faces_have_area_and_source_resolution(self):
        profile = RoadGroundProfile([[5, 15], [30, 15], [50, 25]], [100, 101, 102], [2, 3, 2])
        points, faces = terrain_samples([0, 0, 65, 45], 4, profile, corridor=8)
        tri = points[faces]
        first, second = tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0]
        area = first[:, 0]*second[:, 1]-first[:, 1]*second[:, 0]
        self.assertTrue(np.all(area > 1e-5))
        self.assertTrue(np.all(points >= 2))
        self.assertTrue(np.all(points <= [63, 43]))
        self.assertGreater(len(points), 400)
        self.assertEqual(len(points), len(np.unique(points, axis=0)))

    def test_export_splits_mesh_with_upward_faces_and_valid_uv(self):
        profile = RoadGroundProfile([[5, 15], [50, 15]], [100, 102], 3)
        captured = []
        ac = lambda xy, z: np.c_[xy[:, 0], z, -xy[:, 1]]
        def add(name, vertices, faces, material, uv, collision):
            captured.append(name)
            self.assertLessEqual(len(vertices), 65535)
            self.assertEqual(material, 7)
            self.assertTrue(collision)
            tri = vertices[np.asarray(faces)]
            self.assertTrue(np.all(np.cross(tri[:, 1]-tri[:, 0], tri[:, 2]-tri[:, 0])[:, 1] > 1e-6))
            self.assertTrue(np.isfinite(uv).all())
            self.assertTrue(np.all((uv >= 0) & (uv <= 1)))
        stats = build_terrain_mesh([0, 0, 65, 45], 4, profile, lambda p: 100+p[:, 0]*.04,
                                   ac, add, 7, [0, 0, 65, 45], corridor=8)
        self.assertTrue(captured)
        self.assertEqual(stats['terrain_near_spacing_m'], 1)

    def test_aprons_embed_bases_and_reject_road_neighbours_and_height_gaps(self):
        edge = np.array([[0, 5, 100], [5, 5, 100]], float)
        empty = Polygon()
        panels = apron_panels(edge, [0, -1], lambda p: np.full(len(p), 99.7), box(-10, -4, 10, 3), empty)
        self.assertEqual(len(panels), 4)
        np.testing.assert_array_equal(edge, [[0, 5, 100], [5, 5, 100]])
        for p, bottom, top in panels:
            self.assertTrue(np.all(bottom < 99.7))
            self.assertTrue(np.all(top > 100))
            self.assertFalse(box(-10, -4, 10, 3).intersects(Polygon(p)))
        for ground in (100.2, 98.8):
            self.assertEqual(apron_panels(edge, [0, -1], lambda p: np.full(len(p), ground), empty, empty), [])
        self.assertEqual(apron_panels(edge, [0, -1], lambda p: np.full(len(p), 99.7), box(-1, 4, 6, 6), empty), [])
        self.assertEqual(apron_panels(edge, [0, -1], lambda p: np.full(len(p), 99.7), empty, box(-1, 4, 6, 6)), [])

    def test_split_terrain_preserves_identical_shared_vertex_normals(self):
        profile = RoadGroundProfile([[5, 40], [170, 40]], [100, 103], 3)
        scene = {'meshes': []}
        ac = lambda xy, z: np.c_[xy[:, 0], z, -xy[:, 1]]
        def add(name, vertices, faces, material, uv, collision):
            scene['meshes'].append({'name': name, 'vertices': vertices})
        build_terrain_mesh([0, 0, 180, 100], 4, profile,
                           lambda p: 100+np.sin(p[:, 1]*.12)+p[:, 0]*.03,
                           ac, add, 0, [0, 0, 180, 100], corridor=100, scene=scene)
        self.assertGreater(len(scene['meshes']), 1)
        shared = {}; comparisons = 0
        for mesh in scene['meshes']:
            normal = np.asarray(mesh['normals'])
            np.testing.assert_allclose(np.linalg.norm(normal, axis=1), 1, atol=2e-7)
            for p, n in zip(mesh['vertices'], normal):
                key = tuple(p)
                if key in shared:
                    np.testing.assert_array_equal(n, shared[key]); comparisons += 1
                shared[key] = n
        self.assertGreater(comparisons, 0)


if __name__ == '__main__':
    unittest.main()
