"""Minimal KN5 v6 mesh writer for static tracks.

Binary layout follows the Apache-2.0 reference exporter by KinG-InFeT and
RaduMC's KN5 documentation. See THIRD_PARTY.md. All input coordinates already
use Assetto Corsa's Y-up coordinate system; no extra transform is applied.
"""
from __future__ import annotations
import struct
from pathlib import Path
import numpy as np


def string(f, value):
    value = value.encode('utf-8')
    f.write(struct.pack('<i',len(value)))
    f.write(value)


def dummy(f, name, matrix, children):
    f.write(struct.pack('<i',1))
    string(f,name)
    f.write(struct.pack('<iB',children,1))
    f.write(np.asarray(matrix,dtype='<f4').tobytes())


def normals(vertices, faces):
    positions=np.asarray(vertices,dtype=float)
    triangles=np.asarray(faces,dtype=np.int32)
    a,b,c=positions[triangles[:,0]],positions[triangles[:,1]],positions[triangles[:,2]]
    face_normals=np.cross(b-a,c-a)
    if np.any(np.linalg.norm(face_normals,axis=1)<1e-8):
        raise ValueError('Degenerate triangles cannot be exported')
    result=np.zeros_like(positions)
    for i in range(3):
        np.add.at(result,triangles[:,i],face_normals)
    norm=np.linalg.norm(result,axis=1)
    if np.any(norm<1e-10):
        raise ValueError('Unreferenced or cancelling mesh vertices')
    return result/norm[:,None]


def write(path: Path, scene: dict, textures_dir: Path | None = None):
    materials=scene['materials']
    textures=Path(textures_dir or scene['textures_dir'])
    def samplers(material):
        result={'txDiffuse':material['texture']}
        if material.get('normal') and not material.get('alpha_test'):
            result['txNormal']=material['normal']
        result.update(material.get('samplers',{}))
        return result
    unique_textures=list(dict.fromkeys(t for m in materials for t in samplers(m).values() if t))
    with path.open('wb') as f:
        f.write(b'sc6969')
        f.write(struct.pack('<ii',6,0))
        f.write(struct.pack('<i',len(unique_textures)))
        for texture in unique_textures:
            data=(textures/texture).read_bytes()
            f.write(struct.pack('<i',1))
            string(f,texture)
            f.write(struct.pack('<i',len(data)))
            f.write(data)
        f.write(struct.pack('<i',len(materials)))
        for material in materials:
            string(f,material['name'])
            string(f,material.get('shader') or ('ksPerPixelAT' if material.get('alpha_test') else 'ksPerPixelNM' if material.get('normal') else 'ksPerPixel'))
            # BlendMode byte, alphaTested bool, depthMode uint32.
            f.write(struct.pack('<BBi',0,int(bool(material.get('alpha_test'))),0))
            props={'ksAmbient':material['ambient'],'ksDiffuse':material['diffuse'],
                   'ksSpecular':material['specular'],'ksSpecularEXP':material.get('specular_exp',18.0),
                   'ksEmissive':0.0,'ksAlphaRef':material.get('alpha_ref',0.5)}
            if material.get('normal'):
                props['ksNormalMult']=material.get('normal_strength',1.)
            props.update(material.get('shader_properties',{}))
            f.write(struct.pack('<i',len(props)))
            for name,value in props.items():
                string(f,name)
                values=list(value) if isinstance(value,(list,tuple)) else [value]
                if len(values)>10:raise ValueError('Invalid material property size')
                f.write(struct.pack('<10f',*(values+[0.]*(10-len(values)))))
            textures_for_material=samplers(material)
            f.write(struct.pack('<i',len(textures_for_material)))
            for slot,(name,texture) in enumerate(textures_for_material.items()):
                string(f,name)
                f.write(struct.pack('<i',slot))
                string(f,texture)
        dummy(f,scene['metadata']['id'],np.eye(4),len(scene['meshes'])+len(scene['markers']))
        for mesh in scene['meshes']:
            v=np.asarray(mesh['vertices'],dtype=float)
            faces=np.asarray(mesh['faces'],dtype=np.int32)
            uv=np.asarray(mesh['uv'],dtype=float)
            if not np.isfinite(v).all() or not np.isfinite(uv).all():
                raise ValueError('Nonfinite vertex data')
            if len(v)>65535 or faces.min()<0 or faces.max()>=len(v):
                raise ValueError('KN5 mesh index overflow')
            n=normals(v,faces)
            if 'normals' in mesh:
                supplied=np.asarray(mesh['normals'],dtype=float)
                lengths=np.linalg.norm(supplied,axis=1) if supplied.shape==v.shape else np.array([0.])
                if supplied.shape!=v.shape or not np.isfinite(supplied).all() or np.any(lengths<1e-8):
                    raise ValueError('Invalid supplied mesh normals')
                n=supplied/lengths[:,None]
            # Derive tangent direction from the UV derivatives, including on
            # vertical facades. A fixed world-X tangent gives wrong normal maps.
            tri_v=v[faces];tri_uv=uv[faces]
            e1=tri_v[:,1]-tri_v[:,0];e2=tri_v[:,2]-tri_v[:,0]
            du1=tri_uv[:,1]-tri_uv[:,0];du2=tri_uv[:,2]-tri_uv[:,0]
            det=du1[:,0]*du2[:,1]-du1[:,1]*du2[:,0]
            valid=np.abs(det)>1e-12
            face_tangent=np.zeros_like(e1)
            face_tangent[valid]=(e1[valid]*du2[valid,1,None]-e2[valid]*du1[valid,1,None])/det[valid,None]
            tangent=np.zeros_like(v)
            for corner in range(3):
                np.add.at(tangent,faces[:,corner],face_tangent)
            tangent-=n*np.sum(tangent*n,axis=1)[:,None]
            lengths=np.linalg.norm(tangent,axis=1)
            fallback=np.cross(n,np.tile([0.,1,0],(len(v),1)))
            vertical=np.linalg.norm(fallback,axis=1)<.001
            fallback[vertical]=[1,0,0]
            tangent[lengths<.01]=fallback[lengths<.01]
            tangent/=np.linalg.norm(tangent,axis=1)[:,None]
            packed=np.c_[v,n,uv,tangent].astype('<f4')
            f.write(struct.pack('<i',2))
            string(f,mesh['name'])
            f.write(struct.pack('<iB3B',0,1,1,1,0))
            f.write(struct.pack('<i',len(v)))
            f.write(packed.tobytes())
            f.write(struct.pack('<i',faces.size))
            f.write(faces.astype('<u2').tobytes())
            center=(v.min(axis=0)+v.max(axis=0))/2
            radius=float(np.linalg.norm(v-center,axis=1).max())
            f.write(struct.pack('<iiff3ffB',mesh['material'],0,0.,1000000.,*center,radius,1))
        for marker in scene['markers']:
            dummy(f,marker['name'],marker['matrix'],0)
