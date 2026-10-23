"""Assign a local licensed photo to a stable LoD2 wall; optional rectification."""
from pathlib import Path
import argparse
import hashlib
import json
import sys
import numpy as np
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]


def perspective_coefficients(source_corners,width,height):
    """Pillow maps output pixels back to source coordinates."""
    destination=[(0,0),(width,0),(width,height),(0,height)]
    a=[];b=[]
    for (x,y),(u,v) in zip(destination,source_corners):
        a.extend([[x,y,1,0,0,0,-u*x,-u*y],[0,0,0,x,y,1,-v*x,-v*y]])
        b.extend([u,v])
    return np.linalg.solve(np.asarray(a,dtype=float),np.asarray(b,dtype=float))


def import_photo(data_dir,building_id,surface_id,photo,license_name,source,corners=None,flip_u=False):
    if not license_name.strip() or not source.strip():
        raise ValueError('License and source are required')
    catalog=json.loads((data_dir/'building_surfaces.json').read_text(encoding='utf-8'))
    building=next((b for b in catalog['buildings'] if b['building_id']==building_id),None)
    if building is None or not any(s['surface_id']==surface_id and s['type']=='wall' for s in building['surfaces']):
        raise ValueError('Unknown building/wall; see building_surfaces.json')
    image=Image.open(photo).convert('RGB')
    if corners is not None:
        corners=np.asarray(corners,dtype=float).reshape(4,2)
        if not np.isfinite(corners).all() or np.any(corners<0) or np.any(corners[:,0]>=image.width) or np.any(corners[:,1]>=image.height):
            raise ValueError('Corners must lie inside the photo')
        coeff=perspective_coefficients(corners,2048,2048)
        image=image.transform((2048,2048),Image.Transform.PERSPECTIVE,tuple(coeff),Image.Resampling.BICUBIC)
    else:
        image.thumbnail((4096,4096),Image.Resampling.LANCZOS)
    target_dir=data_dir/'photos';target_dir.mkdir(exist_ok=True)
    filename=hashlib.sha256((building_id+surface_id).encode()).hexdigest()[:16]+'.jpg'
    image.save(target_dir/filename,quality=96)
    path=data_dir/'facade_overrides.json'
    config=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {'facades':[]}
    items=[f for f in config['facades'] if (f['building_id'],f['surface_id'])!=(building_id,surface_id)]
    items.append({'building_id':building_id,'surface_id':surface_id,'texture':'photos/'+filename,
                  'license':license_name,'source':source,'flip_u':bool(flip_u)})
    path.write_text(json.dumps({'facades':items},indent=2,ensure_ascii=False),encoding='utf-8')
    return target_dir/filename


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--building-id',required=True)
    p.add_argument('--surface-id',required=True)
    p.add_argument('--photo',type=Path,required=True)
    p.add_argument('--license',required=True,dest='license_name')
    p.add_argument('--source',required=True)
    p.add_argument('--corners',type=float,nargs=8,metavar='PX')
    p.add_argument('--flip-u',action='store_true')
    args=p.parse_args()
    path=import_photo(ROOT/'data/altenhain',args.building_id,args.surface_id,args.photo,args.license_name,args.source,args.corners,args.flip_u)
    print('Facade imported:',path)
    print('Rebuild the track and Blender source to apply it.')
