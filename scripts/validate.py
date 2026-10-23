"""Checks physical geometry and the serialized KN5, not only file existence."""
from pathlib import Path
import json
import sys
import zipfile
import numpy as np
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.inspect_kn5 import inspect

def validate(root=ROOT):
    ROOT=Path(root)
    cfg=json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))
    scene=json.loads((ROOT/cfg['data_dir']/'scene.json').read_text())
    path=ROOT/'build'/cfg['id']/(cfg['id']+'.kn5')
    info=inspect(path,geometry=True)
    road=next(m for m in info['meshes'] if m['name']=='1ROAD_MAIN')
    checks=[]
    def check(label,condition,detail=''):
        checks.append({'check':label,'passed':bool(condition),'detail':detail})
    for mesh in info['meshes']:
        check('unit_normals:'+mesh['name'],np.allclose(np.linalg.norm(mesh['normals'],axis=1),1,atol=1e-4))
        if mesh['name'].startswith(('1ROAD','1SHOULDER','1GRASS')):
            tri=mesh['positions'][mesh['indices'].reshape(-1,3)]
            n=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])
            check('upward_faces:'+mesh['name'],np.all(n[:,1]>1e-6),str(float(n[:,1].min())))
    v=road['positions'].reshape(-1,7,3)
    expected_width=scene['metadata'].get('road_width_profile_m',cfg['road_width_m'])
    check('road_width',np.allclose(np.linalg.norm(v[:,-1]-v[:,0],axis=1),expected_width,atol=.003))
    check('center_matches_kn5',np.allclose(v[:,3],scene['metadata']['centerline_ac'],atol=.001))
    crossfall=scene['metadata'].get('road_crossfall',{}).get('slope',0.)
    offsets=np.linalg.norm(v[:,:,[0,2]]-v[:,3:4,[0,2]],axis=2)
    check('drainage_crossfall',np.allclose(v[:,:,1],v[:,3:4,1]-crossfall*offsets,atol=.0002))
    check('no_reverse_crossfall',np.all(np.diff(v[:,:4,1],axis=1)>=-.0001)
          and np.all(np.diff(v[:,3:,1],axis=1)<=.0001))
    ds=np.linalg.norm(np.diff(v[:,3][:,[0,2]],axis=0),axis=1)
    grade=np.diff(v[:,3,1])/ds
    check('road_grade',np.max(np.abs(grade))<.25,str(float(np.max(np.abs(grade)))))
    check('road_grade_change',np.max(np.abs(np.diff(grade)))<.04,str(float(np.max(np.abs(np.diff(grade))))))
    center=np.array(scene['metadata']['centerline_ac'])
    for i in range(cfg['pitboxes']):
        for prefix in ('AC_PIT_','AC_START_'):
            name=prefix+str(i)
            m=np.array(info['markers'][name]); pos=m[3,:3]
            idx=np.argmin(np.linalg.norm(center[:,[0,2]]-pos[[0,2]],axis=1))
            check('spawn_above_road:'+name, .15<pos[1]-center[idx,1]<.35)
            check('spawn_basis:'+name,np.allclose(m[:3,:3]@m[:3,:3].T,np.eye(3),atol=1e-5) and np.linalg.det(m[:3,:3])>.999)
    archive=ROOT/'dist'/(cfg['id']+'-'+cfg['version']+'.zip')
    with zipfile.ZipFile(archive) as z:
        check('archive_integrity',z.testzip() is None)
        names=z.namelist()
        for required in (cfg['id']+'.kn5','models.ini','data/surfaces.ini','data/map.ini','ui/ui_track.json','ui/preview.png','map.png','CREDITS.txt'):
            check('package:'+required,'content/tracks/'+cfg['id']+'/'+required in names)
    report={'checks':checks,'passed':all(c['passed'] for c in checks),'mesh_count':len(info['meshes']),
            'vertices':sum(m['vertices'] for m in info['meshes']),'triangles':sum(m['triangles'] for m in info['meshes']),
            'metadata':{k:v for k,v in scene['metadata'].items() if k!='centerline_ac'}}
    (ROOT/'test-results').mkdir(exist_ok=True)
    (ROOT/'test-results/geometry.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    for c in checks:
        if not c['passed']:
            print('FAIL:',c)
    print(f'{sum(c["passed"] for c in checks)}/{len(checks)} geometry and packaging checks passed')
    return report['passed']
if __name__=='__main__':
    sys.exit(0 if validate() else 1)
