"""Actual terrain diagonals, coordinate conversion and bounded sampling failures."""
from pathlib import Path
import copy
import sys
import unittest
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.terrain_sampling import ExportedTerrainSampler


class ExportedTerrainTests(unittest.TestCase):
    def scene(self):
        # Nonplanar quad: the chosen diagonal is 0 -> 2. A new Delaunay
        # triangulation could choose 1 -> 3 and yield a different surface.
        vertices=[[0,0,0],[1,2,0],[1,0,1],[0,0,1]]
        return {'meshes':[{'name':'1GRASS_TERRAIN_P0','vertices':vertices,'faces':[[0,1,2]]},
                           {'name':'1GRASS_TERRAIN_P1','vertices':vertices,'faces':[[0,2,3]]},
                           {'name':'1ROAD_MAIN','vertices':[[0,9,0],[1,9,0],[1,9,1]],'faces':[[0,1,2]]}]}

    def test_actual_diagonal_is_preserved_instead_of_interpolating_another_surface(self):
        sampler=ExportedTerrainSampler.from_scene(self.scene(),[100,200],50)
        result=sampler([[100.75,199.75],[100.25,199.25],[100.5,199.5]])
        np.testing.assert_allclose(result,[51,50,50])

    def test_chunk_boundaries_vertices_and_utm_axes_share_exact_heights(self):
        sampler=ExportedTerrainSampler.from_scene(self.scene(),[462000,5556400],225)
        result=sampler([[462000,5556400],[462001,5556400],[462001,5556399],[462000,5556399]])
        np.testing.assert_allclose(result,[225,227,225,225])

    def test_samples_match_kn5_float32_precision_without_mutating_scene(self):
        scene=self.scene();scene['meshes'][0]['vertices'][1][1]=1.123456789
        before=copy.deepcopy(scene)
        sampler=ExportedTerrainSampler.from_scene(scene,[0,0],0)
        self.assertEqual(sampler([[1,0]])[0],float(np.float32(1.123456789)))
        self.assertEqual(scene,before)

    def test_outside_grid_and_nonfinite_points_never_fall_back_to_analytic_ground(self):
        sampler=ExportedTerrainSampler.from_scene(self.scene(),[0,0],0)
        with self.assertRaisesRegex(ValueError,'outside the exported mesh'):
            sampler([[.5,-.5],[2,0]])
        with self.assertRaisesRegex(ValueError,'finite terrain'):
            sampler([[np.nan,0]])
        self.assertEqual(sampler(np.empty((0,2))).shape,(0,))

    def test_scene_without_real_terrain_is_rejected(self):
        with self.assertRaisesRegex(ValueError,'no exported terrain'):
            ExportedTerrainSampler.from_scene({'meshes':[]},[0,0],0)


if __name__=='__main__':
    unittest.main(verbosity=2)
