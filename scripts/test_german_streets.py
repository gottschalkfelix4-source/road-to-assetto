"""Geometry, source-side interpretation and clearance of village edges."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

import numpy as np
from pyproj import Transformer
from shapely.geometry import Polygon, Point, box
import shapely

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.german_streets import (build_street_edges, kerb_segment,
                                         sidewalk_permissions, _paving_style, plan_street_edges,
                                         _paved_prism)
from roadtoassetto.scenery import Batches
from roadtoassetto.kn5 import normals as export_normals


class StreetEdgeTests(unittest.TestCase):
    def fixture(self, folder, other_roads=None, edge_offset=-.075, data_dir=None,
                ground_sampler=None, preplanned=False):
        station = np.arange(0., 141., 2.)
        xy = np.c_[station, np.zeros(len(station))]
        half = 3 + .25 * np.sin(station / 55)
        road = 100 + station * .025
        left = np.tile([0., 1.], (len(station), 1))
        road_polygon = Polygon(np.vstack([xy + left * half[:, None], (xy - left * half[:, None])[::-1]]))
        buildings = box(-3, 8, 145, 12).union(box(-3, -12, 145, -8))
        context = {'buildings': buildings, 'other_roads': other_roads if other_roads is not None else Polygon()}
        meshes = []; scene = {'materials': []}
        def material(name, texture, **options):
            scene['materials'].append({'name': name, 'texture': texture, **options})
            return len(scene['materials'])-1
        ac = lambda p, h: np.c_[np.asarray(p)[:, 0], np.asarray(h)-100, -np.asarray(p)[:, 1]]
        dem = lambda p: 100 + np.asarray(p)[:, 0] * .025 - .03
        flatten = lambda p, h: h
        batch = Batches(lambda *args: meshes.append(args))
        original = xy.copy(), road.copy(), half.copy()
        plan = plan_street_edges(context=context, xy=xy, road_y=road, left=left,
            distance=station, half_width=half, road_polygon=road_polygon,
            dem=dem, flatten=flatten, edge_y=road+edge_offset, data_dir=data_dir) if preplanned else None
        result = build_street_edges(context=context, xy=xy, road_y=road, left=left,
                                   distance=station, half_width=half, road_polygon=road_polygon,
                                   dem=dem, flatten=flatten, ac=ac, batches=batch,
                                   material=material, scene=scene, textures=folder,
                                   edge_y=road + edge_offset, data_dir=data_dir,
                                   plan=plan, ground_sampler=ground_sampler)
        batch.finish()
        np.testing.assert_array_equal(xy, original[0])
        np.testing.assert_array_equal(road, original[1])
        np.testing.assert_array_equal(half, original[2])
        return result, meshes, road_polygon, context

    def test_outside_physical_road_and_open_side_street_with_valid_export_geometry(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'test-results', prefix='de-street-test-') as folder:
            other = box(61, -30, 68, 30)
            result, meshes, road, context = self.fixture(folder, other_roads=other)
            self.assertGreater(result['counts']['pavement_sections'], 50)
            self.assertGreater(result['counts']['kerb_sections'], 100)
            self.assertGreater(result['counts']['gullies'], 2)
            self.assertEqual(result['street_edge_provenance']['collision'], False)
            for name, vertices, faces, mat, uv, collision in meshes:
                v = np.asarray(vertices).round(5).astype('float32')
                self.assertFalse(collision)
                self.assertLessEqual(len(v), 63000)
                self.assertTrue(np.isfinite(uv).all())
                n = export_normals(v, faces)
                self.assertTrue(np.isfinite(n).all(), name)
                triangle = v[np.asarray(faces)]
                self.assertTrue(np.all(np.linalg.norm(np.cross(triangle[:, 1]-triangle[:, 0], triangle[:, 2]-triangle[:, 0]), axis=1) > 1e-8), name)
                world = np.c_[v[:, 0], -v[:, 2]]
                for forbidden in (road, other, context['buildings']):
                    self.assertFalse(shapely.contains_xy(forbidden, world[:, 0], world[:, 1]).any(), name)
            for run in result['street_edge_runs']:
                self.assertFalse(run['start_m'] < 65 < run['end_m'])

    def test_crown_edge_height_drives_all_tops_not_wavy_ground(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'test-results', prefix='de-street-test-') as folder:
            result, meshes, _, _ = self.fixture(folder, edge_offset=-.075)
            for name, vertices, faces, mat, uv, collision in meshes:
                v = np.asarray(vertices)
                relative = v[:, 1] - v[:, 0] * .025
                if name.startswith('DE_STONE_DRAINAGE'):
                    np.testing.assert_allclose(relative, -.075 + .003, atol=1e-6)
                elif name.startswith('DE_GULLY_GRATES'):
                    self.assertGreaterEqual(relative.min(), -.075 + .004 - 1e-6)
                    self.assertLessEqual(relative.max(), -.075 + .006 + 1e-6)
                elif name.startswith('DE_KERB_UNITS'):
                    self.assertLessEqual(relative.max(), -.075 + .12 + .0001)
                    self.assertGreater(relative.max(), .044)
            self.assertEqual(result['street_edge_provenance']['pavement_crossfall'], .015)

    def test_kerbs_are_closed_outward_bevelled_and_segmented(self):
        ac = lambda p, h: np.c_[p[:, 0], h, -p[:, 1]]
        for side in (-1, 1):
            v, f, uv = kerb_segment(np.array([[0., 0.], [1., 0.]]), np.array([[0., 1.], [0., 1.]]),
                                    [3., 3.], [100., 100.03], side, [.012, .12], ac)
            triangles = v[np.asarray(f)]
            cross = np.cross(triangles[:, 1]-triangles[:, 0], triangles[:, 2]-triangles[:, 0])
            self.assertTrue(np.all(np.sum(cross * (triangles.mean(axis=1) - v.mean(axis=0)), axis=1) > 0))
            self.assertGreater(len(np.unique(np.round(v[:, 2], 4))), 3)
            self.assertAlmostEqual(np.ptp(v[:, 0]), 1.)
            self.assertEqual(len(uv), len(v))

    def test_way_relative_sidewalk_tags_follow_reversed_route(self):
        inverse = Transformer.from_crs(25832, 4326, always_xy=True)
        p = [[462000., 5556300.], [462140., 5556300.]]
        geometry = [dict(zip(('lon', 'lat'), inverse.transform(*x))) for x in p]
        with tempfile.TemporaryDirectory(dir=ROOT / 'test-results', prefix='de-street-test-') as folder:
            path = Path(folder) / 'road_ways.json'
            for tag in ('left', 'right', 'no', 'both', 'separate'):
                path.write_text(json.dumps([{'id': 1, 'geometry': geometry, 'tags': {'sidewalk': tag}}]))
                for reverse in (False, True):
                    permissions, sources = sidewalk_permissions(folder, [[462050., 5556300.]], [[-1. if reverse else 1., 0.]])
                    self.assertEqual(sources[0]['way_id'], 1)
                    for side in (-1, 1):
                        physical = 'left' if side * (-1 if reverse else 1) > 0 else 'right'
                        self.assertEqual(bool(permissions[side][0]), tag == 'both' or tag == physical)

    def test_no_sidewalk_tag_suppresses_paving_without_removing_gutter(self):
        inverse = Transformer.from_crs(25832, 4326, always_xy=True)
        geometry = [dict(zip(('lon', 'lat'), inverse.transform(x, 0.))) for x in (0., 140.)]
        with tempfile.TemporaryDirectory(dir=ROOT / 'test-results', prefix='de-street-test-') as folder:
            (Path(folder) / 'road_ways.json').write_text(json.dumps([{'id': 7, 'geometry': geometry, 'tags': {'sidewalk': 'no'}}]))
            result, meshes, _, _ = self.fixture(folder, data_dir=folder)
            self.assertEqual(result['counts']['pavement_sections'], 0)
            self.assertGreater(result['counts']['drainage_sections'], 100)
            self.assertTrue(all(not m[0].startswith('DE_PAVING') for m in meshes))

    def test_historical_material_reference_is_local_and_on_correct_side(self):
        reference = np.array([462014.977459, 5556364.651311])
        road = reference + [0., -7.]
        self.assertEqual(_paving_style(road + [0, 4], road)[0], 'red_concrete')
        self.assertEqual(_paving_style(road - [0, 4], road)[0], 'neutral_concrete')
        self.assertEqual(_paving_style(road + [90, 4], road + [90, 0])[0], 'neutral_concrete')

    def test_deterministic_meshes_and_texture_bytes(self):
        with tempfile.TemporaryDirectory(dir=ROOT / 'test-results', prefix='de-street-test-') as first, tempfile.TemporaryDirectory(dir=ROOT / 'test-results', prefix='de-street-test-') as second:
            a, meshes_a, _, _ = self.fixture(first)
            b, meshes_b, _, _ = self.fixture(second)
            self.assertEqual(a, b)
            self.assertEqual(meshes_a, meshes_b)
            for path in Path(first).glob('*.png'):
                self.assertEqual(path.read_bytes(), (Path(second) / path.name).read_bytes())

    def test_variable_frontage_width_reaches_actual_building_edge_and_plan_is_serializable(self):
        station = np.arange(0., 141., 2.)
        xy = np.c_[station, np.zeros(len(station))]
        left = np.tile([0.,1.],(len(station),1))
        # The mapped frontage slowly moves away from the road. A single
        # minimum width would leave an increasing artificial strip beside it.
        building = Polygon([[-2.,5.3],[142.,6.1],[142.,10.],[-2.,10.]])
        ctx = {'buildings':building,'other_roads':Polygon()}
        plan = plan_street_edges(context=ctx,xy=xy,road_y=100+station*.02,left=left,
            distance=station,half_width=3.,road_polygon=box(0,-3,140,3),
            dem=lambda p:100+np.asarray(p)[:,0]*.02,flatten=lambda p,h:h)
        self.assertEqual(json.loads(json.dumps(plan)),plan)
        widths=[]
        for section in plan['paving_sections']:
            if section['side']!=1 or not 22<section['start_m']<118:continue
            p=np.asarray(section['xy']); widths.extend(section['width_m'])
            self.assertTrue(section['frontage'][0])
            for v in p[[3,2]]:
                self.assertLess(Point(v).distance(building),.04)
                self.assertGreater(Point(v).distance(building),.02)
            self.assertEqual(section['faces'],[[0,1,2],[0,2,3]])
        self.assertGreater(np.ptp(widths),.4)

    def test_shared_plan_vertices_have_identical_heights_and_nonpointed_ends(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='de-street-test-') as folder:
            result,_,_,_=self.fixture(folder,preplanned=True)
        seen={}; matches=0
        for section in result['street_edge_surfaces']:
            self.assertGreaterEqual(min(section['width_m']),.32)
            self.assertLessEqual(abs(section['width_m'][1]-section['width_m'][0]),.350001)
            for p,h in zip(section['xy'],section['top']):
                key=tuple(np.round(p,7))
                if key in seen:
                    self.assertAlmostEqual(h,seen[key],places=9);matches+=1
                seen[key]=h
        self.assertGreater(matches,100)
        self.assertEqual(result['street_edge_provenance']['edge_end_lowering_m'],3.)

    def test_paving_undersides_use_exported_sampler_even_when_analytic_ground_differs(self):
        def exported_ground(p):
            return 99.1+np.asarray(p)[:,0]*.025
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='de-street-test-') as folder:
            result,meshes,_,_=self.fixture(folder,ground_sampler=exported_ground,preplanned=True)
        self.assertEqual(result['street_edge_provenance']['underside_height_source'],'exact exported terrain triangles')
        for name,vertices,faces,mat,uv,collision in meshes:
            if not name.startswith(('DE_PAVING','DE_PAVEMENT_EDGING')):continue
            v=np.asarray(vertices)
            for i in range(0,len(v),24):
                bottom=v[i:i+4]
                world=np.c_[bottom[:,0],-bottom[:,2]]
                self.assertTrue(np.all(bottom[:,1]+100<exported_ground(world)-.14))

    def test_two_phase_and_inline_build_have_identical_top_geometry(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='de-street-test-') as one, \
             tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='de-street-test-') as two:
            a,ma,_,_=self.fixture(one,preplanned=True)
            b,mb,_,_=self.fixture(two,preplanned=False)
        self.assertEqual(a,b)
        self.assertEqual(ma,mb)

    def test_exported_top_uses_plan_diagonal_for_warped_quads_on_both_sides(self):
        ac=lambda p,z:np.c_[p[:,0],z,-p[:,1]]
        for side in (-1,1):
            p=np.array([[0.,3.],[1.,3.],[1.,4.2],[0.,4.]])*[1,side]
            top=np.array([100.,100.03,100.06,100.015])
            v,f=_paved_prism(p,top-.3,top,ac)
            np.testing.assert_allclose(v[-4:],ac(p,top))
            # Both declared top triangles share original corners 0 and 2,
            # independent of whether winding needed reversing.
            self.assertEqual(set(f[-1])&set(f[-2]),{20,22})
            for face in f[-2:]:
                q=v[list(face)]
                self.assertGreater(np.cross(q[1]-q[0],q[2]-q[0])[1],0)


if __name__ == '__main__':
    unittest.main()

