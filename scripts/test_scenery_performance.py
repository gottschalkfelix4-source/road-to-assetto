"""Geometry contract captured from the scalar v0.14 foliage generator.

Compare exported precision, topology, UVs, material order and RNG consumption,
including a mesh-capacity flush. No photographs or scene snapshots are needed.
"""
import hashlib
import json
from pathlib import Path
import unittest
import numpy as np
from roadtoassetto.scenery import Batches, grow_plant

CASES=[dict(radius=2.8,height=11.,quality='high',shrub=False,repeat=1),
       dict(radius=3.4,height=13.,quality='standard',shrub=False,repeat=1),
       dict(radius=.2,height=.5,quality='high',shrub=True,repeat=1),
       dict(radius=1.8,height=2.4,quality='standard',shrub=True,repeat=1),
       dict(radius=7.5,height=23.,quality='high',shrub=False,repeat=4)]

def fingerprint(grow,case):
    checksum=hashlib.sha256();counts=[0,0,0]
    def collect(name,vertices,faces,material,uv,collision):
        checksum.update(name.encode());checksum.update(str(material).encode())
        for array in (np.asarray(vertices,dtype='<f8').round(5),np.asarray(faces,dtype='<i8'),np.asarray(uv,dtype='<f8').round(6)):
            checksum.update(array.tobytes())
        counts[0]+=1;counts[1]+=len(vertices);counts[2]+=len(faces)
    batches=Batches(collect);rng=np.random.default_rng(19417)
    for i in range(case['repeat']):
        grow([3.+i,12.,-7.],case['radius'],case['height'],rng,batches,2,[4],case['quality'],case['shrub'])
    batches.finish()
    return dict(sha256=checksum.hexdigest(),counts=counts,next_random=rng.random())

class FoliageGeometryTests(unittest.TestCase):
    def test_same_geometry_materials_and_random_sequence_as_scalar_generator(self):
        reference=json.loads((Path(__file__).parent/'fixtures/foliage-v014.json').read_text())
        for case,expected in zip(CASES,reference,strict=True):
            with self.subTest(case=case):self.assertEqual(fingerprint(grow_plant,case),expected)

if __name__=='__main__':unittest.main()
