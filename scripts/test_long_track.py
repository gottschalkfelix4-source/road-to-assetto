"""Real KN5 serialization of a >50 km route plus adversarial section seams."""
from copy import deepcopy
import configparser
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

import numpy as np
from PIL import Image
from shapely.geometry import LineString, box
from shapely.ops import unary_union

from roadtoassetto.long_track import (build_profile, plan_sections, plan_ownership,
    configure_section, finalize_section, SceneClipper, TerrainSeams,
    MultiModelExporter, aggregate_metadata, namespace_textures)
from roadtoassetto.package import write_track_map, package, archive
from roadtoassetto.inspect_kn5 import inspect


def road_scene(profile, section):
    a,b=section['halo_start'],section['halo_end']+1
    xy=profile['xy'][a:b];left=profile['left'][a:b];height=profile['road_y'][a:b]
    origin=profile['origin_utm32'];h0=profile['height_origin_m'];distance=profile['distance'][a:b]
    meshes=[]
    for name,offsets in [('1ROAD_MAIN',np.linspace(-3,3,7)),('1SHOULDER_LEFT',np.array([3,3.2,3.4])),
                         ('1SHOULDER_RIGHT',np.array([-3.4,-3.2,-3]))]:
        count=len(offsets)
        planar=(xy[:,None,:]+left[:,None,:]*offsets[None,:,None]).reshape(-1,2)
        yy=(height[:,None]-profile['crossfall']*np.minimum(np.abs(offsets),3)).ravel()
        if name!='1ROAD_MAIN':yy-=.015
        vertices=np.c_[planar[:,0]-origin[0],yy-h0,-(planar[:,1]-origin[1])]
        faces=[]
        for i in range(len(xy)-1):
            for j in range(count-1):
                v=i*count+j;w=v+count
                faces.extend([(v,w,v+1),(v+1,w,w+1)])
        meshes.append({'name':name,'vertices':vertices.round(5).tolist(),'faces':faces,
                       'uv':np.c_[np.tile(offsets,len(xy))/4,np.repeat(distance,count)/4].tolist(),
                       'material':0,'collision':True})
    center=np.c_[xy[:,0]-origin[0],height-h0,-(xy[:,1]-origin[1])]
    markers=[]
    if section['first']:
        matrix=np.eye(4);matrix[3,:3]=center[18]+[0,.22,0]
        for name in ('AC_PIT_0','AC_START_0','AC_HOTLAP_START_0'):
            markers.append({'name':name,'matrix':matrix.tolist()})
    return {'meshes':meshes,'materials':[{'name':'road','texture':'road.png','ambient':.3,'diffuse':.5,'specular':.02}],
            'markers':markers,'textures_dir':'textures',
            'metadata':{'id':'test','centerline_ac':center.tolist(),'road_width_profile_m':[6.]*len(xy),
                        'origin_utm32':origin.tolist(),'height_origin_m':h0,'buildings':0}}


class LongTrackTests(unittest.TestCase):
    def test_float32_terrain_rounding_is_bounded_and_never_repairs_roads(self):
        from roadtoassetto.long_track import stabilize_terrain_winding
        # Positive source triangle becomes clockwise at a shared 20 km origin.
        v=np.array([[-18280.19823,0,-18513.11735],[-18280.20023,0,-18513.12135],[-18280.34423,0,-18513.26735]])
        if np.cross(v[1]-v[0],v[2]-v[0])[1]<0:v=v[[0,2,1]]
        scene={'meshes':[{'name':'1GRASS_TEST','vertices':v.tolist(),'faces':[[0,1,2]],'uv':[[0,0]]*3}]}
        q=v.astype(np.float32);self.assertLessEqual(np.cross(q[1]-q[0],q[2]-q[0])[1],0)
        stats=stabilize_terrain_winding(scene)
        self.assertLess(stats['source_area_m2'],.01)
        self.assertGreater(stats['reoriented_triangles']+stats['collapsed_triangles'],0)
        invalid={'meshes':[{'name':'1GRASS_BAD','vertices':[[0,0,0],[1,0,0],[0,0,1]],'faces':[[0,1,2]]}]}
        with self.assertRaises(ValueError):stabilize_terrain_winding(invalid)
        invalid['meshes'][0]['name']='1ROAD_MAIN'
        self.assertEqual(stabilize_terrain_winding(invalid)['reoriented_triangles'],0)

    def test_angular_osm_junction_gets_drivable_arc_within_source_tolerance(self):
        from roadtoassetto.long_track import resample_route
        from webapp.geodata import TO_WGS
        source=np.array([[450000.,5556000.],[450100.,5556000.],[450120.,5556100.]])
        lon,lat=TO_WGS.transform(source[:,0],source[:,1])
        xy=resample_route(np.c_[lat,lon],minimum_radius_m=3.6)
        original=LineString(source)
        from shapely.geometry import Point
        self.assertLessEqual(max(original.distance(Point(p)) for p in xy),1.5)
        profile=build_profile(xy,np.full(len(xy),200.),6.)
        section=plan_sections(xy)[0]
        scene=road_scene(profile,section)
        for mesh in scene['meshes']:
            v=np.array(mesh['vertices']);f=np.array(mesh['faces'])
            normals=np.cross(v[f[:,1]]-v[f[:,0]],v[f[:,2]]-v[f[:,0]])
            self.assertTrue(np.all(normals[:,1]>0),mesh['name'])

    def test_55km_continuous_serialized_route_is_one_multimodel_track(self):
        x=np.arange(55001,dtype=float)
        xy=np.c_[400000+x,5550000+18*np.sin(x/2300)]
        profile=build_profile(xy,180+12*np.sin(x/3000),6)
        sections=plan_sections(xy)
        self.assertGreater(len(sections),50)
        self.assertGreater(profile['distance'][-1],55000)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);textures=root/'textures';textures.mkdir()
            Image.new('RGB',(4,4),(90,90,90)).save(textures/'road.png')
            track=root/'build/long_test'
            exporter=MultiModelExporter(track,'long_test',profile,sections)
            previous=None;all_faces=0
            for section in sections:
                cfg=configure_section({},profile,section)
                self.assertEqual(cfg['_road_profile']['origin_utm32'],profile['origin_utm32'].tolist())
                scene=finalize_section(road_scene(profile,section),profile,section)
                model=exporter.write_section(scene,section,textures)
                saved=inspect(track/model['file'],geometry=True)
                road=next(m for m in saved['meshes'] if m['name'].startswith('1ROAD_MAIN'))
                rows=road['positions'].reshape(-1,7,3)
                if previous is not None:np.testing.assert_array_equal(previous,rows[0])
                previous=rows[-1].copy();all_faces+=road['triangles']
                self.assertLess(max(m['vertices'] for m in saved['meshes']),65536)
                self.assertEqual(bool(saved['markers']),section['first'])
            models=exporter.finish()
            self.assertEqual(all_faces,(len(xy)-1)*12)
            manifest=configparser.ConfigParser();manifest.read(track/'models.ini')
            self.assertEqual(len(manifest.sections()),len(sections))
            self.assertEqual(sum(m['end_m']-m['start_m'] for m in models),profile['distance'][-1])
            metadata=aggregate_metadata({'buildings':0},profile,models)
            self.assertEqual(metadata['sections_count'],len(sections))
            self.assertEqual(metadata['preview_scope'],'representative_section')
            config={'id':'long_test','name':'55 km synthetic export test','city':'Test fixture',
                    'country':'Germany','road_width_m':6,'pitboxes':1,'version':'test'}
            metadata['id']='long_test'
            package(root,config,{'metadata':metadata})
            Image.new('RGB',(4,4)).save(track/'ui/preview.png')
            output=archive(root,config,track)
            with zipfile.ZipFile(output) as zipped:
                self.assertIsNone(zipped.testzip())
                names=zipped.namelist()
                self.assertEqual(sum(name.endswith('.kn5') for name in names),len(sections))
                self.assertTrue(all(name.startswith('content/tracks/long_test/') for name in names))
                self.assertIn('content/tracks/long_test/models.ini',names)

    def test_voronoi_ownership_covers_hairpin_without_double_ground(self):
        # The two parallel arms are closer than the scenery corridor width.
        theta=np.linspace(-np.pi/2,np.pi/2,121)
        xy=np.vstack([np.c_[np.arange(0,601),np.zeros(601)],
                      np.c_[600+50*np.cos(theta[1:]),50+50*np.sin(theta[1:])],
                      np.c_[np.arange(599,-1,-1),np.full(600,100)]])
        profile=build_profile(xy,np.zeros(len(xy)),6)
        sections=plan_sections(xy,section_m=400)
        ownership=plan_ownership(profile,sections)
        self.assertAlmostEqual(unary_union(ownership).area,LineString(xy).buffer(120,cap_style='flat').area,places=4)
        for i,a in enumerate(ownership):
            for b in ownership[i+1:]:self.assertLess(a.intersection(b).area,1e-5)

    def test_clips_vertical_wall_and_preserves_interpolated_uv(self):
        mesh={'name':'wall','vertices':[[-.5,0,-.5],[1.5,0,-.5],[1.5,2,-.5],[-.5,2,-.5]],
              'faces':[[0,1,2],[0,2,3]],'uv':[[0,0],[1,0],[1,1],[0,1]],'material':0,'collision':False}
        output=SceneClipper(box(0,0,1,1),{'origin_utm32':[0,0]}).clip(mesh)
        vertices=np.vstack([m['vertices'] for m in output]);uv=np.vstack([m['uv'] for m in output])
        self.assertGreater(len(vertices),0)
        self.assertGreaterEqual(vertices[:,0].min(),-1e-6);self.assertLessEqual(vertices[:,0].max(),1+1e-6)
        np.testing.assert_allclose(uv[:,0],(vertices[:,0]+.5)/2,atol=1e-6)
        area=0
        for part in output:
            tri=np.asarray(part['vertices'])[np.asarray(part['faces'])]
            area+=np.linalg.norm(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0]),axis=1).sum()/2
        self.assertAlmostEqual(area,2.,places=6)

    def test_terrain_weld_inserts_missing_neighbor_knot(self):
        def mesh(vertices,faces):
            return {'name':'1GRASS_TERRAIN_P0','vertices':vertices,'faces':faces,
                    'uv':np.asarray(vertices)[:,[0,2]].tolist(),'material':0,'collision':True}
        first=mesh([[0,0,0],[1,0,0],[1,2,1],[1,0,2],[0,0,2]],[[0,2,1],[0,4,2],[4,3,2]])
        second=mesh([[1,99,0],[2,0,0],[2,0,2],[1,99,2]],[[0,2,1],[0,3,2]])
        weld=TerrainSeams();weld.weld({'meshes':[first]},box(0,0,1,2).boundary)
        stats=weld.weld({'meshes':[second]},box(1,0,2,2).boundary)
        vertices=np.asarray(second['vertices']);border=vertices[np.abs(vertices[:,0]-1)<1e-8]
        self.assertGreater(stats['terrain_border_knots_inserted'],0)
        self.assertTrue(any(np.allclose(p,[1,2,1]) for p in border))
        for x,y,z in border:self.assertAlmostEqual(y,2*(1-abs(z-1)),places=6)

    def test_clipped_double_sided_foliage_keeps_opposite_vertex_normals(self):
        from roadtoassetto.kn5 import normals
        vertices=[[-.5,0,-.5],[1.5,0,-.5],[1.5,2,-.5],[-.5,2,-.5]]
        mesh={'name':'TREE_LEAVES','vertices':vertices+vertices,
              'faces':[[0,1,2],[0,2,3],[4,6,5],[4,7,6]],
              'uv':[[0,0],[1,0],[1,1],[0,1]]*2,'material':0,'collision':False}
        result=SceneClipper(box(0,0,1,1),{'origin_utm32':[0,0]}).clip(mesh)
        for part in result:
            computed=normals(part['vertices'],part['faces'])
            self.assertTrue(np.isfinite(computed).all())
            self.assertTrue(np.any(computed[:,2]>.9));self.assertTrue(np.any(computed[:,2]<-.9))

    def test_terrain_weld_matches_rounded_endpoint_without_micron_sliver(self):
        from roadtoassetto.kn5 import normals
        def mesh(vertices,faces):
            return {'name':'1GRASS_TERRAIN','vertices':vertices,'faces':faces,
                    'uv':np.asarray(vertices)[:,[0,2]].tolist(),'material':0,'collision':True}
        first=mesh([[0,0,0],[1,0,0],[1,0,.000001],[1,0,2],[0,0,2]],[[0,2,1],[0,4,2],[4,3,2]])
        second=mesh([[1,0,0],[2,0,0],[2,0,2],[1,0,2]],[[0,2,1],[0,3,2]])
        weld=TerrainSeams();weld.weld({'meshes':[first]},box(0,0,1,2).boundary)
        result=weld.weld({'meshes':[second]},box(1,0,2,2).boundary)
        self.assertEqual(result['terrain_border_knots_inserted'],0)
        normals(second['vertices'],second['faces'])
        json.dumps(second,allow_nan=False)

    def test_inserted_rounded_neighbor_knot_cannot_leave_clockwise_zero_area_face(self):
        from roadtoassetto.long_track import stabilize_terrain_winding
        a=[-16939.6377172,138.8114529,-18276.6334877]
        b=[-16939.6790966,138.81145,-18276.7880022]
        opposite=[-16939.74023,138.81148,-18277.01935]
        knot=[-16939.6789988,138.81145,-18276.7876318]
        mesh={'name':'1GRASS_TERRAIN','vertices':[a,opposite,b],'faces':[[0,1,2]],
              'uv':[[0,0],[0,1],[1,0]],'material':0,'collision':True}
        seam=TerrainSeams();seam.segments=[[a,knot],[knot,b]]
        seam.normal_segments=[[[0,1,0],[0,1,0]]]*2
        scene={'meshes':[mesh]}
        stats=seam.weld(scene,LineString([np.array(a)[[0,2]],np.array(b)[[0,2]]]))
        self.assertEqual(stats['terrain_border_rounding_remnants'],1)
        self.assertLess(stats['terrain_border_rounding_area_m2'],1e-7)
        stabilize_terrain_winding(scene)
        for m in scene['meshes']:
            v=np.asarray(m['vertices'],dtype=np.float32);t=v[np.asarray(m['faces'])]
            self.assertTrue(np.all(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])[:,1]>0))

    def test_only_new_microscopic_terrain_clipping_remnants_are_removed_and_counted(self):
        mesh={'name':'1GRASS_TERRAIN','vertices':[[0,0,0],[1,0,0],[1,0,1]],
              'faces':[[0,2,1]],'uv':[[0,0],[1,0],[1,1]],'material':0,'collision':True}
        clipper=SceneClipper(box(.99995,-.00005,1,0),{'origin_utm32':[0,0]})
        self.assertEqual(clipper.clip(mesh),[])
        self.assertGreater(clipper.cleanup['triangles'],0)
        self.assertLess(clipper.cleanup['projected_area_m2'],1e-8)
        # A fully owned source triangle is not silently altered by cleanup.
        tiny={**mesh,'vertices':[[1,0,0],[1,0,.00001],[1.00001,0,0]]}
        whole=SceneClipper(box(0,-1,2,1),{'origin_utm32':[0,0]})
        self.assertEqual(whole.clip(tiny),[tiny]);self.assertEqual(whole.cleanup['triangles'],0)

    def test_model_export_rejects_seam_gap(self):
        x=np.arange(301,dtype=float);profile=build_profile(np.c_[x,np.zeros(len(x))],x*.01,6)
        sections=plan_sections(profile['xy'],section_m=150)
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);Image.new('RGB',(2,2)).save(root/'road.png')
            exporter=MultiModelExporter(root/'out','gap',profile,sections)
            exporter.write_section(finalize_section(road_scene(profile,sections[0]),profile,sections[0]),sections[0],root)
            scene=finalize_section(road_scene(profile,sections[1]),profile,sections[1])
            scene['meshes'][0]['vertices'][0][1]+=.1
            with self.assertRaisesRegex(ValueError,'seam'):exporter.write_section(scene,sections[1],root)

    def test_clipped_scenery_rounding_fragments_are_counted_without_removing_source_faces(self):
        from roadtoassetto.long_track import stabilize_clipped_scenery
        from roadtoassetto.kn5 import normals
        vertices=[[-13429.8047495,208.81019,-18280.7093521],
                  [-13429.8101972,208.8927031,-18280.6958479],
                  [-13429.8101973,208.8927029,-18280.6958479],
                  [-13429,209,-18280]]
        mesh={'name':'TREE_LEAVES_C2_S0005','vertices':vertices,
              'faces':[[0,1,2],[0,1,3]],'uv':[[0,0],[0,1],[0,1],[1,1]],'material':0}
        scene={'meshes':[deepcopy(mesh)]}
        stats=stabilize_clipped_scenery(scene)
        self.assertEqual(stats['triangles'],1);self.assertLess(stats['source_area_m2'],5e-9)
        self.assertEqual(len(scene['meshes'][0]['vertices']),3)
        normals(scene['meshes'][0]['vertices'],scene['meshes'][0]['faces'])
        for name in ('TREE_LEAVES','1ROAD_MAIN_C0_S0005','1SHOULDER_C0_S0005','1GRASS_C0_S0005'):
            source={**deepcopy(mesh),'name':name};scene={'meshes':[source]}
            self.assertEqual(stabilize_clipped_scenery(scene)['triangles'],0)
            self.assertEqual(scene['meshes'][0],source)

    def test_scenery_rounding_refuses_excessive_area_loss(self):
        from roadtoassetto.long_track import stabilize_clipped_scenery
        mesh={'name':'WALL_C0_S0005','vertices':[[20000,0,0],[20000,100,0],[20000.0008,100,0]],
              'faces':[[0,1,2]],'uv':[[0,0],[0,1],[1,1]],'material':0}
        with self.assertRaisesRegex(ValueError,'Excessive scenery'):
            stabilize_clipped_scenery({'meshes':[mesh]})

    def test_bounded_map_keeps_world_to_pixel_transform(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'ui').mkdir();(root/'data').mkdir()
            center=[[-27500,10,-10000],[27500,20,10000]]
            write_track_map(root,center)
            with Image.open(root/'map.png') as image:size=image.size
            self.assertLessEqual(max(size),4096)
            cfg=configparser.ConfigParser();cfg.read(root/'data/map.ini');p=cfg['PARAMETERS']
            self.assertEqual(size,(int(p['WIDTH']),int(p['HEIGHT'])))
            pixel=(center[0][0]+float(p['X_OFFSET']))/float(p['SCALE_FACTOR'])
            self.assertAlmostEqual(pixel,20,places=5)
            pixel_end=(center[-1][0]+float(p['X_OFFSET']))/float(p['SCALE_FACTOR'])
            self.assertAlmostEqual(pixel_end,size[0]-20,places=5)

    def test_texture_names_reuse_common_scans_but_distinguish_aerial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);a=root/'a';b=root/'b';a.mkdir();b.mkdir()
            Image.new('RGB',(4,4),'red').save(a/'ortho.png');Image.new('RGB',(4,4),'blue').save(b/'ortho.png')
            sample={'meshes':[{'material':0}],'materials':[{'name':'ground','texture':'ortho.png'}]}
            first=deepcopy(sample);second=deepcopy(sample)
            namespace_textures(first,a);namespace_textures(second,b)
            self.assertNotEqual(first['materials'][0]['texture'],second['materials'][0]['texture'])


if __name__=='__main__':unittest.main()
