"""Metric CC0 ground materials, classified using georeferenced RGBI evidence.

Aerial pixels remain source evidence rather than a photograph projected onto
the ground: original material photographs supply local surface colour/detail.
"""
import json
import shutil
from pathlib import Path
import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter, zoom


def _smoothstep(low,high,value):
    t=np.clip((value-low)/(high-low),0.,1.)
    return t*t*(3-2*t)


def terrain_material_fields(ortho,nir=None,*,ortho_bounds=None,max_size=1536):
    """Generate semantic material weights and broad reflectance modulation.

    These are newly calculated shader-control rasters, not retouched source
    photographs. NDVI separates vegetation even in blue photographic shadows.
    The RGB-only fallback is explicitly less certain. The macro field uses
    only broad vegetation density and inferred mineral warmth, never source
    brightness: cars, roof colours and baked shadows are not albedo detail.
    """
    source=np.asarray(ortho)
    if source.ndim!=3 or source.shape[0]!=3 or not np.isfinite(source).all():
        raise ValueError('Expected a finite three-channel orthophoto')
    if nir is not None and (np.asarray(nir).shape!=source.shape[1:] or not np.isfinite(nir).all()):
        raise ValueError('Infrared channel must match the orthophoto')
    if max_size<16:
        raise ValueError('Material field resolution is too small')
    factor=min(1.,float(max_size)/max(source.shape[1:]))
    rgb=zoom(source.astype(np.float32),(1.,factor,factor),order=1,prefilter=False)/255.
    r,g,b=rgb
    height,width=r.shape
    if ortho_bounds is None:
        width_m,height_m=source.shape[2]*.2,source.shape[1]*.2
    else:
        bounds=np.asarray(ortho_bounds,dtype=float)
        if bounds.shape!=(4,) or not np.isfinite(bounds).all() or np.any(bounds[2:]<=bounds[:2]):
            raise ValueError('Invalid orthophoto bounds')
        width_m,height_m=bounds[2]-bounds[0],bounds[3]-bounds[1]
    resolution=np.array([height_m/height,width_m/width])
    if nir is None:
        excess=(g-r)/np.maximum(r+g,.08)
        green_blue=(g-b)/np.maximum(g+b,.08)
        vegetation=_smoothstep(.005,.16,excess)*_smoothstep(-.06,.17,green_blue)
        method='RGB colour evidence fallback; lower confidence than RGBI'
    else:
        infrared=zoom(np.asarray(nir,dtype=np.float32),(factor,factor),order=1,prefilter=False)/255.
        ndvi=(infrared-r)/np.maximum(infrared+r,.025)
        vegetation=_smoothstep(.16,.43,ndvi)
        method='RGBI NDVI vegetation evidence, with RGB mineral classification'
    # Decimetre-scale transitions blend materials rather than exposing pixel
    # outlines; width is in metres, independent of the source raster size.
    vegetation=gaussian_filter(vegetation,.55/resolution,mode='nearest')
    warmth=_smoothstep(.025,.18,(r-b)/np.maximum(r+b,.08))
    mineral=np.clip(.32+.58*warmth,.32,.90)*(1-vegetation)
    weights=np.stack([vegetation,mineral,np.maximum(0.,1-vegetation-mineral)],axis=-1)
    weights/=np.maximum(weights.sum(axis=-1,keepdims=True),1e-8)
    first=np.rint(weights[...,:2]*255).astype(np.int16)
    first[:,:,1]=np.minimum(first[:,:,1],255-first[:,:,0])
    last=255-first[:,:,0]-first[:,:,1]
    mask=np.concatenate([first,last[:,:,None],np.zeros_like(last[:,:,None])],axis=2).astype('uint8')
    broad_green=gaussian_filter(vegetation,8./resolution,mode='nearest')
    broad_warm=gaussian_filter(warmth*(1-vegetation),8./resolution,mode='nearest')
    # A weak neutral-to-warm modulation preserves broad land-cover changes
    # without multiplying a second green photograph onto green grass scans.
    reflectance=np.clip(.96-.08*broad_green,.86,.98)
    macro=np.stack([reflectance,reflectance*(1-.018*broad_warm),
                    reflectance*(1-.052*broad_warm)],axis=-1)
    # The output is an sRGB texture. Encode linear multipliers explicitly.
    srgb=np.where(macro<=.0031308,12.92*macro,1.055*np.power(macro,1/2.4)-.055)
    macro_rgb=np.rint(np.clip(srgb,0,1)*255).astype('uint8')
    stats={'classification':method,'field_size_px':[width,height],
           'field_resolution_m':[round(float(resolution[1]),5),round(float(resolution[0]),5)],
           'transition_sigma_m':.55,'macro_sigma_m':8.,
           'mean_material_weights':dict(zip(('grass','mineral','impervious'),np.mean(weights,axis=(0,1),dtype=np.float64).round(5).tolist())),
           'photographic_brightness_projected':False,
           'limitation':'Inferred ground-cover materials; no reconstruction of exact gardens, paving or buried soil'}
    return mask,macro_rgb,stats

def terrain_albedo(ortho,nir=None):
    """Reduce baked blue skylight only where infrared also indicates vegetation.

    Keep the original aerial image for roof sampling and provenance. This is a
    restrained rendering approximation: local brightness variation remains and
    nonvegetated buildings/roads are not recoloured.
    """
    rgb=ortho.transpose(1,2,0).astype(np.float32)
    if nir is None:return rgb.astype('uint8'),0.
    r,g,b=np.moveaxis(rgb,-1,0)
    ndvi=(nir.astype(np.float32)-r)/(nir.astype(np.float32)+r+1)
    vegetation=np.clip((ndvi-.19)/.20,0,1)
    blue_shadow=np.clip((b-r-5)/35,0,1)
    weight=vegetation*blue_shadow*.9
    luma=rgb@np.array([.2126,.7152,.0722],dtype=np.float32)
    brightness=np.clip(luma*1.18,38,160)
    target=brightness[:,:,None]*np.array([.93,1.08,.76],dtype=np.float32)
    derived=rgb*(1-weight[:,:,None])+target*weight[:,:,None]
    return np.clip(derived,0,255).astype('uint8'),float(np.mean(weight>.1))


def surface_materials(data_dir,textures,scene,asphalt,shoulder,terrain,ortho,nir=None,*,ortho_bounds=None):
    source=Path(data_dir)/'materials/surface_photos'
    asphalt_source=source/'Asphalt023S'
    if not list(asphalt_source.glob('*Color*')):
        raise FileNotFoundError('CC0 surface assets missing. Run scripts/fetch_surface_materials.py')
    records=json.loads((source/'asset_sources.json').read_text(encoding='utf-8'))
    for kind,target in [('Color','asphalt_photo.jpg'),('NormalDX','asphalt_photo_normal_dx.jpg')]:
        shutil.copy2(next(asphalt_source.glob('*'+kind+'*')),Path(textures)/target)
    scene['materials'][asphalt].update(texture='asphalt_photo.jpg',normal='asphalt_photo_normal_dx.jpg',
                                     normal_strength=.48,ambient=.4,diffuse=.48,specular=.06,specular_exp=28.,roughness=.87)
    # Restore the source scan's measured 1.25 m square scale, instead of
    # stretching aggregate over the previous procedural 3 x 4 m repeat.
    asphalt_scale=next(r['dimensions_cm'] for r in records if r['asset']=='Asphalt023S')
    repeats=np.array([float(asphalt_scale[0])/100,float(asphalt_scale[1])/100])
    if not np.isfinite(repeats).all() or np.any(repeats<=0):raise ValueError('Invalid metric scan scale')
    # Copy original colour images byte-for-byte. A prior version
    # discarded the colour/contrast and projected the aerial photo on top;
    # that made old shadows and cars look melted across road embankments.
    detail_files={}
    for asset,label in [('Grass004','grass'),('Gravel022','gravel')]:
        for kind,suffix in [('Color','color')]:
            output=f'ground_{label}_{suffix}.jpg'
            shutil.copy2(next((source/asset).glob('*'+kind+'*')),Path(textures)/output)
            detail_files[(label,suffix)]=output
    Image.new('RGB',(4,4),(255,255,255)).save(Path(textures)/'ground_neutral.png')
    mask,macro,field_stats=terrain_material_fields(ortho,nir,ortho_bounds=ortho_bounds)
    Image.fromarray(mask,'RGBA').save(Path(textures)/'ground_detail_mask.png')
    Image.fromarray(macro).save(Path(textures)/'ground_macro_reflectance.png')
    scene['materials'][terrain].update(texture='ground_macro_reflectance.png',
        shader='ksMultilayer',ambient=.36,diffuse=.44,specular=.005,
        samplers={'txMask':'ground_detail_mask.png','txDetailR':detail_files[('grass','color')],
                  'txDetailG':detail_files[('gravel','color')],'txDetailB':'asphalt_photo.jpg','txDetailA':'ground_neutral.png'},
        shader_properties={'multR':1/1.4,'multG':1/1.5,'multB':1/1.25,'multA':[0.,1.,1.], 'magicMult':1.},
        detail_color_space='sRGB',roughness=.98,
        terrain_material_model='Original CC0 detail colours; RGBI semantic weights and broad inferred reflectance; no projected aerial photo')
    gravel_source=source/'Gravel022'
    for kind,target in [('Color','shoulder_gravel.jpg'),('NormalDX','shoulder_gravel_normal_dx.jpg')]:
        shutil.copy2(next(gravel_source.glob('*'+kind+'*')),Path(textures)/target)
    scene['materials'][shoulder].update(texture='shoulder_gravel.jpg',normal='shoulder_gravel_normal_dx.jpg',
                                      normal_strength=.35,ambient=.38,diffuse=.5,specular=.01,roughness=.95)
    return {'asphalt_repeat_m':repeats.tolist(),'source_records':records,
            'aerial_shadow_correction':{'method':'Aerial brightness is excluded from terrain albedo; RGBI supplies inferred material weights only',
                                       'corrected_pixel_fraction':0.},
            'terrain_material_fields':field_stats,
            'terrain_detail':'Original metric CC0 grass/mineral/impervious colours; inferred RGBI ground-cover classes, not a surface survey'}
