"""Bounds-checked KN5 reader used to inspect generated AND stock game files.

Kept separate from the writer: accounts for v5/v6 layouts and validates the
entire file, materials, textures, indices, normals, and dummy transforms.
"""
import io
import struct
from pathlib import Path
import numpy as np


def inspect(path: Path, geometry=False):
    stream=io.BytesIO(path.read_bytes())
    size=len(stream.getbuffer())
    def read(n):
        data=stream.read(n)
        if len(data)!=n:
            raise ValueError(f'Truncated KN5 at {stream.tell()}')
        return data
    def unpack(fmt):
        return struct.unpack('<'+fmt,read(struct.calcsize('<'+fmt)))
    def count(limit=10000000):
        x=unpack('i')[0]
        if not 0<=x<=limit:
            raise ValueError(f'Invalid count {x} at {stream.tell()}')
        return x
    def text():
        return read(count(1000000)).decode('utf-8')
    if read(6)!=b'sc6969':
        raise ValueError('Invalid KN5 magic')
    version=unpack('i')[0]
    if version not in (5,6):
        raise ValueError(f'Unsupported KN5 version {version}')
    if version>5:
        unpack('i')
    textures=[]
    for _ in range(count(10000)):
        unpack('i'); name=text(); read(count(500000000));textures.append(name)
    materials=[]
    for _ in range(count(10000)):
        name=text();shader=text();blend,alpha=unpack('BB');depth=unpack('i')[0]
        props={}
        for _ in range(count(1000)):
            key=text();props[key]=unpack('10f')
        samplers=[]
        for _ in range(count(100)):
            key=text();slot=unpack('i')[0];texture=text()
            if texture and texture not in textures:
                raise ValueError('Sampler references absent texture')
            samplers.append((key,slot,texture))
        materials.append({'name':name,'shader':shader,'blend':blend,'alpha':alpha,'depth':depth,'properties':props,'samplers':samplers})
    meshes=[];markers={};node_count=0
    def node(depth=0):
        nonlocal node_count
        if depth>100:
            raise ValueError('Node hierarchy too deep')
        node_count+=1
        kind=unpack('i')[0];name=text();children=count(1000000);active=unpack('B')[0]
        if kind==1:
            matrix=np.array(unpack('16f')).reshape(4,4)
            if name.startswith('AC_'):
                markers[name]=matrix.tolist()
        elif kind==2:
            cast,visible,transparent=unpack('3B')
            nv=count(65536)
            v=np.frombuffer(read(nv*44),dtype='<f4').reshape(nv,11)
            ni=count()
            indices=np.frombuffer(read(ni*2),dtype='<u2')
            if ni%3 or (ni and indices.max()>=nv) or not np.isfinite(v).all():
                raise ValueError('Invalid mesh data')
            mat=unpack('i')[0];layer=unpack('i')[0];lodin,lodout=unpack('2f');sphere=unpack('4f');renderable=unpack('B')[0]
            if not 0<=mat<len(materials):
                raise ValueError('Invalid material index')
            mesh={'name':name,'vertices':nv,'triangles':ni//3,'material':mat,'cast_shadows':cast,'visible':visible,
                  'renderable':renderable,'lod_in':lodin,'lod_out':lodout}
            if geometry:
                mesh.update(positions=v[:,:3].copy(),normals=v[:,3:6].copy(),uv=v[:,6:8].copy(),tangents=v[:,8:11].copy(),indices=indices.copy())
            meshes.append(mesh)
        else:
            raise ValueError(f'Unsupported node kind {kind}')
        for _ in range(children):
            node(depth+1)
    node()
    if stream.tell()!=size:
        raise ValueError(f'Unexpected trailing bytes: {size-stream.tell()}')
    return {'version':version,'textures':textures,'materials':materials,'meshes':meshes,'markers':markers,'nodes':node_count,'bytes':size}
