"""Behavioral tests for geometry conversion, UV frames, and photo rectification."""
from pathlib import Path
import importlib.util
import json
import sys
import tempfile
import unittest
import numpy as np
from PIL import Image
from pyproj import Transformer
from shapely.geometry import LineString, Point

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.buildings import triangulate_face
from roadtoassetto.inspect_kn5 import inspect
spec=importlib.util.spec_from_file_location('photo_import',ROOT/'scripts/import_facade_photo.py')
photo_import=importlib.util.module_from_spec(spec);spec.loader.exec_module(photo_import)


class VisualTests(unittest.TestCase):
    def test_vertical_concave_face_area_and_winding(self):
        ring=np.array([[0,0,0],[4,0,0],[4,0,2],[2,0,2],[2,0,4],[0,0,4],[0,0,0]],dtype=float)
        v,f,n=triangulate_face([ring])
        t=v[np.asarray(f)]
        cross=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])
        self.assertAlmostEqual(float(np.linalg.norm(cross,axis=1).sum()/2),12,places=5)
        self.assertTrue(np.all(cross@n>0))

    def test_polygon_hole_stays_open(self):
        outer=[[0,0,5],[4,0,5],[4,4,5],[0,4,5],[0,0,5]]
        hole=[[1,1,5],[1,2,5],[2,2,5],[2,1,5],[1,1,5]]
        v,f,n=triangulate_face([outer,hole])
        t=v[np.asarray(f)]
        self.assertAlmostEqual(float(np.linalg.norm(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0]),axis=1).sum()/2),15,places=5)

    def test_photo_corners_are_mapped_in_correct_order(self):
        source=np.array([[12,14],[90,9],[82,85],[17,92]])
        a,b,c,d,e,f,g,h=photo_import.perspective_coefficients(source,100,100)
        points=np.array([[0,0],[100,0],[100,100],[0,100]])
        x,y=points.T
        mapped=np.c_[(a*x+b*y+c)/(g*x+h*y+1),(d*x+e*y+f)/(g*x+h*y+1)]
        np.testing.assert_allclose(mapped,source,atol=1e-7)

    def test_photo_is_registered_only_to_existing_wall(self):
        parent=ROOT/'test-results';parent.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=parent,prefix='photo-test-') as temp:
            data=Path(temp).resolve()
            self.assertTrue(data.is_relative_to(parent.resolve()))
            (data/'building_surfaces.json').write_text(json.dumps({'buildings':[{'building_id':'house','surfaces':[{'surface_id':'wall','type':'wall'}]}]}))
            Image.new('RGB',(100,100),(212,43,27)).save(data/'input.png')
            dest=photo_import.import_photo(data,'house','wall',data/'input.png','CC0-1.0','Synthetic test fixture')
            self.assertTrue(dest.is_file())
            saved=json.loads((data/'facade_overrides.json').read_text())['facades']
            self.assertEqual(len(saved),1)
            with self.assertRaises(ValueError):
                photo_import.import_photo(data,'wrong','wall',data/'input.png','CC0-1.0','Synthetic test fixture')

    def test_normal_map_tangents_are_finite_and_orthogonal(self):
        path=ROOT/'build/rtac_altenhain_langstrasse/rtac_altenhain_langstrasse.kn5'
        info=inspect(path,geometry=True)
        count=0
        for mesh in info['meshes']:
            if mesh['name'].startswith('LOD2_WALLS'):
                self.assertEqual(info['materials'][mesh['material']]['shader'],'ksPerPixelNM')
                self.assertTrue(np.isfinite(mesh['tangents']).all())
                np.testing.assert_allclose(np.linalg.norm(mesh['tangents'],axis=1),1,atol=1e-4)
                np.testing.assert_allclose(np.sum(mesh['normals']*mesh['tangents'],axis=1),0,atol=1e-4)
                count+=1
        self.assertGreaterEqual(count,3)

    def test_reviewed_road_alignment_widths_and_spawn_support(self):
        """Check the shipped physics against external image-review controls.

        Version 0.6 deliberately changes road physics. The reviewed image-edge
        picks, rather than frozen prototype vertices or the interpolation
        implementation, are the reference for useful regression assertions.
        """
        from roadtoassetto.prepare import ordered_road
        data=ROOT/'data/altenhain'
        metadata=json.loads((data/'manifest.json').read_text())
        reference=json.loads((data/'road_alignment.json').read_text())
        self.assertTrue(metadata['road_alignment']['applied'])
        info=inspect(ROOT/'build/rtac_altenhain_langstrasse/rtac_altenhain_langstrasse.kn5',geometry=True)
        road=next(m for m in info['meshes'] if m['name']=='1ROAD_MAIN')
        positions=road['positions'].reshape(-1,7,3)
        self.assertTrue(np.isfinite(positions).all())
        centers=positions[:,3]
        widths=np.linalg.norm(positions[:,-1]-positions[:,0],axis=1)
        self.assertGreater(float(np.ptp(widths)),1.5)
        self.assertTrue(np.all((widths>=3.5)&(widths<=9)))
        np.testing.assert_allclose(widths,metadata['road_width_profile_m'],atol=.003)
        tri=road['positions'][road['indices'].reshape(-1,3)]
        self.assertTrue(np.all(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])[:,1]>1e-6))
        origin=np.asarray(metadata['origin_utm32'])
        world=centers[:,[0,2]]*np.array([1,-1])+origin
        actual=LineString(world)
        self.assertTrue(actual.is_simple)
        self.assertGreater(actual.length,680)
        self.assertLess(actual.length,720)

        lonlat=ordered_road(json.loads((data/'road_ways.json').read_text()))
        transform=Transformer.from_crs(4326,25832,always_xy=True)
        east,north=transform.transform(lonlat[:,0],lonlat[:,1])
        source=LineString(np.c_[east,north])
        checked=0
        for section in reference['cross_sections']:
            if section['confidence']!='medium':
                continue
            s=float(section['station_m'])
            start,end=reference['junction']['blend_source_station_m']
            if start<=s<=end:
                continue
            p=np.array(source.interpolate(s).coords[0])
            direction=np.array(source.interpolate(min(source.length,s+1)).coords[0])-np.array(source.interpolate(max(0,s-1)).coords[0])
            direction/=np.linalg.norm(direction)
            observed=p+np.array([-direction[1],direction[0]])*section['center_offset_left_m']
            self.assertLessEqual(actual.distance(Point(observed)),float(section['edge_uncertainty_m'])+.15)
            k=int(np.argmin(np.linalg.norm(world-observed,axis=1)))
            self.assertAlmostEqual(float(widths[k]),float(section['width_m']),delta=.22)
            checked+=1
        self.assertGreaterEqual(checked,10)
        affine=reference['pixel_to_utm']
        for px,py in reference['junction']['reviewed_pixel_points']:
            point=Point(affine[0]*px+affine[2],affine[4]*py+affine[5])
            self.assertLessEqual(actual.distance(point),float(reference['junction']['uncertainty_m']))
        # The old graph-node kink has intentionally moved by several metres.
        self.assertGreater(max(source.distance(Point(p)) for p in world),2.)
        np.testing.assert_allclose(world[[0,-1]],reference['source_endpoints_utm'],atol=.01)
        for name,matrix in info['markers'].items():
            if not name.startswith(('AC_PIT_','AC_START_','AC_HOTLAP_START_')):
                continue
            matrix=np.asarray(matrix)
            self.assertTrue(np.isfinite(matrix).all())
            p=matrix[3,:3]
            k=int(np.argmin(np.linalg.norm(centers[:,[0,2]]-p[[0,2]],axis=1)))
            self.assertLess(np.linalg.norm(centers[k,[0,2]]-p[[0,2]]),.01)
            self.assertGreater(p[1]-centers[k,1],.15)
            self.assertLess(p[1]-centers[k,1],.35)
            np.testing.assert_allclose(matrix[:3,:3]@matrix[:3,:3].T,np.eye(3),atol=1e-5)


if __name__=='__main__':
    unittest.main(verbosity=2)
