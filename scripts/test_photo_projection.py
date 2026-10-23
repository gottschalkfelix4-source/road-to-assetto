"""Camera calibration rejection and continuity across adjacent source walls."""
from pathlib import Path
import copy
import sys
import unittest
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.photo_projection import fit_homography,project_points,projected_photo_uv


class PhotoProjectionTests(unittest.TestCase):
    def spec(self):
        points=np.array([[0,0],[8,0],[8,6],[0,6],[4,10.]])
        matrix=np.array([[80,5,120],[4,-65,850],[.006,.002,1.]])
        return {'projection':{'type':'planar-homography-v1','origin_utm32_height':[462000,5556400,225],
                   'axis_u':[1,0,0],'axis_v':[0,0,1],'image_size':[1200,1000],
                   'source_points_m':points.tolist(),'image_points_px':project_points(points,matrix).tolist()}}

    def test_known_projective_camera_recovers_control_points(self):
        spec=self.spec()['projection']
        h,errors=fit_homography(spec['source_points_m'],spec['image_points_px'])
        self.assertLess(float(errors.max()),1e-9)

    def test_adjacent_wall_halves_have_identical_uvs_at_shared_seam(self):
        override=self.spec();shared=np.array([[462004,5556400,225],[462004,5556400,235.]])
        left=np.vstack([shared,[462000,5556400,225]])
        right=np.vstack([shared,[462008,5556400,225]])
        a=projected_photo_uv(left,override);b=projected_photo_uv(right,override)
        np.testing.assert_allclose(a[:2],b[:2],atol=1e-12)
        self.assertTrue(np.all(a>=0) and np.all(a<=1))

    def test_inconsistent_apex_is_rejected_instead_of_stretched_into_place(self):
        override=self.spec();override['projection']['image_points_px'][-1][0]+=90
        with self.assertRaisesRegex(ValueError,'disagree'):
            projected_photo_uv(np.array([[462004,5556400,235.]]),override)

    def test_wrong_plane_and_image_bounds_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'calibrated photo plane'):
            projected_photo_uv(np.array([[462004,5556401,230.]]),self.spec())
        with self.assertRaisesRegex(ValueError,'leaves the source image'):
            projected_photo_uv(np.array([[462020,5556400,225.]]),self.spec())

    def test_collinear_control_points_are_rejected(self):
        with self.assertRaisesRegex(ValueError,'Degenerate'):
            fit_homography([[0,0],[1,0],[2,0],[3,0]],[[0,0],[100,0],[200,0],[300,0]])


if __name__=='__main__':
    unittest.main(verbosity=2)
