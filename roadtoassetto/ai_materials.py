"""Apply generated surface albedos to the exported scene, never to road geometry."""
from pathlib import Path
import json
import shutil
import numpy as np
from PIL import Image,ImageFilter


def load_image(path,size=(1024,1024)):
    with Image.open(path) as image:
        return image.convert('RGB').resize(size,Image.Resampling.LANCZOS)


def normal_from_albedo(image):
    # A restrained approximate bump map, not a measured surface normal scan.
    gray=np.asarray(image.convert('L').filter(ImageFilter.GaussianBlur(.8)),dtype=np.float32)/255
    dx=(np.roll(gray,-1,axis=1)-np.roll(gray,1,axis=1))*.7
    dy=(np.roll(gray,-1,axis=0)-np.roll(gray,1,axis=0))*.7
    vectors=np.stack([-dx,dy,np.ones_like(dx)],axis=-1)
    vectors/=np.linalg.norm(vectors,axis=2,keepdims=True)
    return Image.fromarray(np.clip((vectors+1)*127.5,0,255).astype('uint8'))


def apply_material_pack(scene,textures,folder):
    textures,folder=Path(textures),Path(folder)
    provenance=json.loads((folder/'ai_materials.json').read_text(encoding='utf-8'))
    images={kind:load_image(folder/f'ai_{kind}.png') for kind in ('plaster','roof','asphalt')}
    applied=[]
    for kind in ('roof','asphalt'):
        images[kind].save(textures/f'ai_{kind}.png')
        normal_from_albedo(images[kind]).save(textures/f'ai_{kind}_normal_dx.png')
    plaster=images['plaster'].resize((1024,512),Image.Resampling.LANCZOS)
    a=np.asarray(plaster,dtype=np.float32)
    luma=a@np.array([.2126,.7152,.0722],dtype=np.float32)
    detail=np.clip(luma/max(float(luma.mean()),1),.65,1.35)
    normal_from_albedo(plaster).save(textures/'ai_plaster_normal_dx.png')
    for mat in scene['materials']:
        name=mat['name']
        if name=='asphalt' or name=='photographic_roof_tiles':
            kind='asphalt' if name=='asphalt' else 'roof'
            mat.update(texture=f'ai_{kind}.png',normal=f'ai_{kind}_normal_dx.png',normal_strength=.35)
            applied.append(name)
        elif name.startswith(('village_plain_','village_close_ground_','village_foundation_')):
            original=np.asarray(load_image(textures/mat['texture'],(1024,512)),dtype=np.float32)
            # Keep each building's facade colour and its existing base band.
            colour=np.asarray(Image.fromarray(original.astype('uint8')).filter(ImageFilter.GaussianBlur(12)),dtype=np.float32)
            target='ai_'+mat['texture']
            Image.fromarray(np.clip(colour*detail[:,:,None],0,255).astype('uint8')).save(textures/target,quality=96)
            mat.update(texture=target,normal='ai_plaster_normal_dx.png',normal_strength=.2)
            applied.append(name)
    shutil.copy2(folder/'ai_materials.json',textures/'ai_materials.json')
    return {'enabled':True,'applied_materials':applied,'provenance':provenance,
            'accuracy':'Synthetic generic materials, not photographs of the real road or buildings. Bump normals inferred from albedo. Geometry and registered local facade photographs are preserved.'}
