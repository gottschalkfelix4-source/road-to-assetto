"""Actual render geometry and KN5 serialization of hillside construction."""
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.hillside_render import (build_hillside,handrail_geometry,
    hillside_textures,surface_prism_parts,wall_cap_segments,wall_outline)
from roadtoassetto.kn5 import write
from roadtoassetto.inspect_kn5 import inspect


def ac(xy,z):
    xy=np.asarray(xy);z=np.asarray(z)
    return np.c_[xy[:,0]-462000,z-230,5556400-xy[:,1]]


def wall_fixture():
    return dict(id='wall',xy=[[462000,5556400],[462003,5556400]],
                bottom=[229.8,229.9],top=[231.,231.15],width_m=.24,outward=[0,-1],assembly_id='one')


class HillsideRenderTests(unittest.TestCase):
    def test_warped_surface_keeps_exact_diagonal_and_closed_outward_body(self):
        xy=np.array([[462000,5556400],[462003,5556400],[462003,5556402],[462000,5556402]])
        top=np.array([230.,230.1,230.3,230.05]);faces=[[0,1,2],[0,2,3]]
        upper,body=surface_prism_parts(xy,top-.2,top,faces,ac)
        v,f,uv=upper
        np.testing.assert_allclose(v,ac(xy,top))
        self.assertEqual({tuple(sorted(i)) for i in f},{(0,1,2),(0,2,3)})
        t=v[f];self.assertTrue(np.all(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])[:,1]>0))
        v,f,uv=body;t=v[f]
        center=np.vstack([ac(xy,top),ac(xy,top-.2)]).mean(axis=0)
        self.assertTrue(np.all(np.sum(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])*(t.mean(axis=1)-center),axis=1)>0))
        self.assertTrue(np.isfinite(uv).all())

    def test_wall_is_outside_declared_inner_edge_and_coping_has_real_joints(self):
        wall=wall_fixture();xy,bottom,top,out=wall_outline(wall)
        np.testing.assert_allclose(xy[:2],wall['xy']);self.assertTrue(np.all(xy[:,1]<=5556400))
        np.testing.assert_allclose(xy[2:,1],5556399.76)
        caps=wall_cap_segments(wall)
        self.assertEqual(len(caps),3)
        for p,lo,hi in caps:
            self.assertLessEqual(float(np.ptp(p[:,0])),1.)
            self.assertAlmostEqual(float(p[:,1].max()),5556400.02)
            self.assertAlmostEqual(float(p[:,1].min()),5556399.74)
            self.assertTrue(np.all(hi-lo>=.0539));self.assertTrue(np.all(hi-lo<=.0601))
        self.assertAlmostEqual(caps[1][0][:,0].min()-caps[0][0][:,0].max(),.005,places=6)

    def test_wall_rejects_wrong_outward_and_negative_heights(self):
        for update in ({'outward':[1,0]},{'width_m':-.2},{'top':[229,229]}):
            with self.subTest(update=update),self.assertRaises(ValueError):wall_outline(dict(wall_fixture(),**update))

    def test_rail_uses_supplied_step_support_heights(self):
        xy=np.array([[462000+i*.28,5556400] for i in range(6)])
        base=230+np.arange(6)*.17
        parts=handrail_geometry({'xy':xy,'base':base},ac)
        self.assertGreater(len(parts),len(xy)-1)
        # Tube segments precede vertical posts. Every post foot belongs to an
        # actual supplied tread; no interpolated midpoint between two levels.
        for v,f,uv in parts[len(xy)-1:]:
            bottom=float(v[:,1].min()+230+.035)
            self.assertLess(float(np.min(np.abs(base-bottom))),1e-7)
            self.assertAlmostEqual(float(v[:,1].max()-v[:,1].min()),.985,places=6)

    def test_masonry_is_deterministic_metric_material_with_subtle_normals(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='hillside-material-') as tmp:
            path=Path(tmp);hillside_textures(path)
            first=(path/'hillside_masonry.png').read_bytes();hillside_textures(path)
            self.assertEqual(first,(path/'hillside_masonry.png').read_bytes())
            color=np.asarray(Image.open(path/'hillside_masonry.png'));normal=np.asarray(Image.open(path/'hillside_masonry_normal_dx.png'))
            self.assertEqual(color.shape,(512,512,3));self.assertGreater(float(color.std()),10)
            self.assertGreater(float(normal[:,:,0].std()),2);self.assertGreater(float(normal[:,:,2].mean()),230)

    def test_handrail_rejects_missing_or_nonfinite_podium_support(self):
        xy=[[462000,5556400],[462000.35,5556400],[462000.7,5556400]]
        for levels in ([230.3,230.15,float('nan')],[230.3,230.15,float('inf')],[230.3,230.15]):
            with self.subTest(levels=levels),self.assertRaisesRegex(ValueError,'Invalid hillside handrail'):
                handrail_geometry({'xy':xy,'base':levels},ac)

    def test_complete_plan_exports_real_tops_risers_walls_caps_and_rails(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='hillside-render-') as tmp:
            path=Path(tmp);textures=path/'textures';textures.mkdir()
            for filename in ('de_paving.png','de_sidewalk_asphalt.png','shoulder_gravel.jpg'):
                Image.new('RGB',(16,16),(140,135,120)).save(textures/filename)
            scene={'materials':[],'meshes':[],'markers':[],'metadata':{'id':'hillside-test'}}
            def material(name,texture,**kwargs):
                scene['materials'].append(dict(name=name,texture=texture,**kwargs));return len(scene['materials'])-1
            def add(name,v,f,mid,uv,collision=False):
                scene['meshes'].append(dict(name=name,vertices=v,faces=f,material=mid,uv=uv,collision=collision))
            sections=[]
            for i,kind in enumerate(('terrace','landing','stair','driveway','bank_ledge','footpath')):
                xy=np.array([[0,0],[1,0],[1,.4],[0,.4]])+[462000+i*1.2,5556400]
                sections.append(dict(id=kind,kind=kind,xy=xy.tolist(),top=[230.2]*4,
                                     faces=[[0,1,2],[0,2,3]],style='pavers',assembly_id='one'))
            plan={'surfaces':sections,'retaining_edges':[wall_fixture()],
                  'stairs':[{'handrails':[{'xy':[[462000,5556400],[462001,5556400]],'base':[230.2,230.2]}]}]}
            stats=build_hillside(plan,textures,scene,material,add,ac,lambda xy:np.full(len(xy),230.165))
            self.assertEqual(stats['hillside_rendered_surface_counts'],dict.fromkeys(('terrace','landing','stair','driveway','bank_ledge','footpath'),1))
            self.assertEqual(stats['hillside_rendered_walls'],1);self.assertEqual(stats['hillside_wall_coping_stones'],3)
            self.assertEqual(stats['hillside_rendered_handrails'],1);self.assertGreater(stats['hillside_flush_edging_parts'],0)
            self.assertFalse(any(m['collision'] for m in scene['meshes']))
            target=path/'hillside.kn5';write(target,scene,textures);model=inspect(target,geometry=True)
            prefixes=('HILLSIDE_TERRACE_','HILLSIDE_LANDING_','HILLSIDE_STAIR_TREAD_','HILLSIDE_DRIVEWAY_',
                      'HILLSIDE_BANK_LEDGE_','HILLSIDE_FOOTPATH_')
            for mesh in model['meshes']:
                v=mesh['positions'].astype(float);faces=mesh['indices'].reshape(-1,3);t=v[faces]
                cross=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])
                self.assertTrue(np.all(np.linalg.norm(cross,axis=1)>1e-7),mesh['name'])
                self.assertLess(mesh['vertices'],65536)
                if mesh['name'].startswith(prefixes):
                    self.assertTrue(np.all(cross[:,1]>0));np.testing.assert_allclose(v[:,1],.2,atol=1e-6)
            self.assertIn('hillside_masonry_normal_dx.png',model['textures'])
            bank=next(m for m in model['meshes'] if m['name'].startswith('HILLSIDE_BANK_LEDGE_'))
            self.assertIn('shoulder_gravel.jpg',[s[2] for s in model['materials'][bank['material']]['samplers']])
            np.testing.assert_allclose(np.ptp(bank['uv'],axis=0),[1/1.5,.4/1.5],atol=1e-6)

    def test_empty_plan_is_valid_and_does_not_generate_assets(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results',prefix='hillside-empty-') as tmp:
            def forbidden(*args,**kwargs):raise AssertionError('Unexpected callback for an empty plan')
            stats=build_hillside({},Path(tmp),{'materials':[]},forbidden,forbidden,ac,forbidden)
            self.assertEqual(stats['hillside_rendered_surfaces'],0)
            self.assertEqual(stats['hillside_rendered_surface_counts'],dict.fromkeys(('terrace','landing','stair','driveway','bank_ledge','footpath'),0))
            self.assertEqual(list(Path(tmp).iterdir()),[])


if __name__=='__main__':unittest.main(verbosity=2)
