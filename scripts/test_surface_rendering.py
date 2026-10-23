"""Check actual exported alpha-test and metric layered-terrain contracts."""
from pathlib import Path
import sys,tempfile,unittest
import numpy as np
from PIL import Image
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.real_surfaces import surface_materials,terrain_albedo,terrain_material_fields
from roadtoassetto.kn5 import write
from roadtoassetto.inspect_kn5 import inspect

class SurfaceTests(unittest.TestCase):
    def test_blue_shadow_correction_requires_infrared_vegetation(self):
        rgb=np.array([[[48,72,100],[48,72,100],[100,130,70]]],dtype='uint8')
        source=rgb.transpose(2,0,1).copy()
        derived,fraction=terrain_albedo(source,np.array([[180,35,180]],dtype='uint8'))
        self.assertGreater(derived[0,0,1],derived[0,0,2])
        np.testing.assert_array_equal(derived[0,1:],rgb[0,1:])
        np.testing.assert_array_equal(source,rgb.transpose(2,0,1))
        np.testing.assert_array_equal(terrain_albedo(source)[0],rgb)
        self.assertAlmostEqual(fraction,1/3)

    def test_world_detail_mask_and_native_asphalt_scale(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results') as temp:
            folder=Path(temp)
            mats=[{'name':name,'texture':'unused','ambient':.3,'diffuse':.5,'specular':0} for name in ('road','shoulder','terrain')]
            scene={'materials':mats,'meshes':[],'markers':[],'metadata':{'id':'test'}}
            rgb=np.full((3,16,20),100,dtype='uint8');rgb[1,:,:10]=150
            stats=surface_materials(ROOT/'data/altenhain',folder,scene,0,1,2,rgb)
            self.assertEqual(stats['asphalt_repeat_m'],[1.25,1.25])
            mask=np.asarray(Image.open(folder/'ground_detail_mask.png'))
            self.assertTrue(np.all(mask.astype(int).sum(axis=2)==255))
            self.assertTrue(np.all(mask[:,:,3]==0))
            self.assertGreater(mask[:,:5,0].mean(),mask[:,-5:,0].mean())
            write(folder/'test.kn5',scene,folder)
            mat=inspect(folder/'test.kn5')['materials'][2]
            self.assertEqual(mat['shader'],'ksMultilayer')
            self.assertEqual(set(s[0] for s in mat['samplers']),{'txDiffuse','txMask','txDetailR','txDetailG','txDetailB','txDetailA'})
            self.assertEqual(mat['properties']['magicMult'][0],1.)
            self.assertAlmostEqual(mat['properties']['multR'][0],1/1.4,places=6)
            self.assertAlmostEqual(mat['properties']['multG'][0],1/1.5,places=6)
            self.assertAlmostEqual(mat['properties']['multB'][0],1/1.25,places=6)
            self.assertEqual(mat['properties']['multA'][1:3],(1.,1.))
            self.assertEqual(mats[2]['texture'],'ground_macro_reflectance.png')
            self.assertEqual(mats[2]['detail_color_space'],'sRGB')
            self.assertFalse(stats['terrain_material_fields']['photographic_brightness_projected'])

    def test_rgb_vegetation_mask_uses_infrared_in_blue_shadow(self):
        source=np.empty((3,48,80),dtype='uint8')
        source[:,:,:40]=np.array([48,72,100])[:,None,None]
        source[:,:,40:]=np.array([48,72,100])[:,None,None]
        nir=np.full((48,80),35,dtype='uint8');nir[:,:40]=180
        original=source.copy()
        mask,macro,stats=terrain_material_fields(source,nir,ortho_bounds=[0,0,16,9.6])
        self.assertGreater(mask[:,:30,0].mean(),240)
        self.assertLess(mask[:,50:,0].mean(),15)
        self.assertTrue(np.all(mask.astype(int).sum(axis=2)==255))
        self.assertTrue(np.all(mask[:,:,3]==0))
        self.assertEqual(macro.shape,(48,80,3))
        self.assertEqual(stats['field_resolution_m'],[.2,.2])
        np.testing.assert_array_equal(source,original)

    def test_macro_albedo_does_not_bake_photographic_shadow_brightness(self):
        source=np.full((3,80,100),0,dtype='uint8')
        source[:]=np.array([60,90,50])[:,None,None]
        nir=np.full((80,100),120,dtype='uint8')
        lit_mask,lit_macro,_=terrain_material_fields(source,nir)
        shadow_mask,shadow_macro,_=terrain_material_fields(source//2,nir//2)
        np.testing.assert_array_equal(lit_mask,shadow_mask)
        np.testing.assert_array_equal(lit_macro,shadow_macro)
        self.assertGreater(shadow_macro.mean(),230)

    def test_rgb_fallback_is_explicit_and_fields_are_deterministic(self):
        rng=np.random.default_rng(103)
        source=rng.integers(30,180,(3,96,128),dtype='uint8')
        first=terrain_material_fields(source,max_size=64)
        second=terrain_material_fields(source,max_size=64)
        np.testing.assert_array_equal(first[0],second[0])
        np.testing.assert_array_equal(first[1],second[1])
        self.assertIn('fallback',first[2]['classification'])
        self.assertEqual(first[2]['field_size_px'],[64,48])
        self.assertEqual(first[2]['field_resolution_m'],[.4,.4])
        self.assertTrue(np.all(first[0].astype(int).sum(axis=2)==255))

    def test_original_ground_scans_are_copied_without_pixel_rewrites(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results') as temp:
            folder=Path(temp)
            scene={'materials':[{'name':str(i),'texture':'unused','ambient':.3,'diffuse':.5,'specular':0} for i in range(3)]}
            surface_materials(ROOT/'data/altenhain',folder,scene,0,1,2,np.full((3,16,20),90,dtype='uint8'))
            cache=ROOT/'data/altenhain/materials/surface_photos'
            for asset,label in [('Grass004','grass'),('Gravel022','gravel')]:
                for kind,suffix in [('Color','color')]:
                    self.assertEqual(next((cache/asset).glob('*'+kind+'*')).read_bytes(),
                                     (folder/f'ground_{label}_{suffix}.jpg').read_bytes())

    def test_material_fields_reject_incompatible_source_or_bounds(self):
        source=np.full((3,16,20),90,dtype='uint8')
        for bad in (np.zeros((4,16,20)),np.full((3,16,20),np.nan)):
            with self.assertRaises(ValueError):terrain_material_fields(bad)
        with self.assertRaises(ValueError):terrain_material_fields(source,np.zeros((15,20)))
        with self.assertRaises(ValueError):terrain_material_fields(source,ortho_bounds=[0,0,0,10])

    def test_alpha_cards_export_as_cutout_with_shadow_and_valid_normals(self):
        with tempfile.TemporaryDirectory(dir=ROOT/'test-results') as temp:
            folder=Path(temp);Image.new('RGBA',(8,8),(90,110,50,0)).save(folder/'leaf.png')
            scene={'materials':[{'name':'leaf','texture':'leaf.png','ambient':.3,'diffuse':.3,'specular':0.,'alpha_test':True,'alpha_ref':.3}],
                   'metadata':{'id':'leaf_test'},'markers':[],
                   'meshes':[{'name':'leaves','material':0,'vertices':[[0,0,0],[1,0,0],[0,1,0],[0,0,0],[1,0,0],[0,1,0]],
                              'uv':[[0,0],[1,0],[0,1],[0,0],[1,0],[0,1]],'faces':[[0,1,2],[3,5,4]]}]}
            write(folder/'leaf.kn5',scene,folder)
            output=inspect(folder/'leaf.kn5',geometry=True)
            material=output['materials'][0];mesh=output['meshes'][0]
            self.assertEqual((material['shader'],material['alpha'],material['blend']),('ksPerPixelAT',1,0))
            self.assertEqual(mesh['cast_shadows'],1)
            np.testing.assert_allclose(np.linalg.norm(mesh['normals'],axis=1),1)
            self.assertLess(np.dot(mesh['normals'][0],mesh['normals'][3]),-.99)

if __name__=='__main__':unittest.main(verbosity=2)
