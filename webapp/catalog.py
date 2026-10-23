"""Prepared templates and validated references to server-owned Hessian routes."""
import json
import hashlib
import threading
from functools import lru_cache
from pathlib import Path
import numpy as np
from PIL import Image
from pyproj import Transformer
import rasterio
from rasterio.transform import array_bounds,from_bounds
from rasterio.warp import calculate_default_transform,reproject,Resampling
from roadtoassetto.prepare import ordered_road
from roadtoassetto.road_alignment import load_alignment

ROOT=Path(__file__).resolve().parents[1]
_map_lock=threading.Lock()


def _map_stamp(data):
    return tuple((name,(data/name).stat().st_mtime_ns,(data/name).stat().st_size)
                 for name in ('bounds.json','textures/ortho.jpg','ortho.tif') if (data/name).is_file())


@lru_cache(maxsize=4)
def _map_layout(data_path,stamp):
    """One shared pixel grid for the JPEG warp and its Leaflet rectangle."""
    data=Path(data_path)
    bounds=json.loads((data/'bounds.json').read_text(encoding='utf-8'))
    if (not isinstance(bounds,list) or len(bounds)!=4 or not np.isfinite(bounds).all()
            or bounds[2]<=bounds[0] or bounds[3]<=bounds[1]):
        raise ValueError('Invalid prepared aerial extent')
    with Image.open(data/'textures/ortho.jpg') as image:
        if image.format!='JPEG' or image.mode!='RGB':
            raise ValueError('Prepared aerial image must be RGB JPEG')
        width,height=image.size
        if min(width,height)<1 or width*height>100_000_000:
            raise ValueError('Prepared aerial dimensions are invalid')
    # prepare.py exports the entire native raster as JPEG. Its exact extent
    # can differ slightly from the integer request bounds (e.g. by 20 cm).
    if (data/'ortho.tif').is_file():
        with rasterio.open(data/'ortho.tif') as raster:
            if (raster.crs is None or raster.crs.to_epsg()!=25832 or raster.count<3
                    or (raster.width,raster.height)!=(width,height)
                    or max(abs(a-b) for a,b in zip(raster.bounds,bounds))>1.01
                    or abs(raster.transform.b)>1e-10 or abs(raster.transform.d)>1e-10):
                raise ValueError('Prepared aerial georeferencing does not match its JPEG')
            bounds=list(raster.bounds)
    source_transform=from_bounds(*bounds,width,height)
    target_transform,target_width,target_height=calculate_default_transform('EPSG:25832','EPSG:3857',width,height,*bounds)
    if min(target_width,target_height)<1 or target_width*target_height>105_000_000:
        raise ValueError('Projected aerial dimensions are invalid')
    projected_bounds=array_bounds(target_height,target_width,target_transform)
    unproject=Transformer.from_crs(3857,4326,always_xy=True)
    west,south=unproject.transform(projected_bounds[0],projected_bounds[1])
    east,north=unproject.transform(projected_bounds[2],projected_bounds[3])
    return {'source_transform':source_transform,'target_transform':target_transform,
            'width':target_width,'height':target_height,'geographic_bounds':[west,south,east,north]}


def projected_map(data_root):
    """Render only when requested; all writes stay in writable appdata."""
    data=ROOT/config()['data_dir']
    stamp=_map_stamp(data)
    layout=_map_layout(str(data),stamp)
    key=hashlib.sha256(json.dumps(['mercator-v1',str(data),stamp]).encode()).hexdigest()[:24]
    path=Path(data_root)/'cache/maps'/('altenhain-'+key+'.jpg')
    with _map_lock:
        if path.is_file():
            return path
        with Image.open(data/'textures/ortho.jpg') as image:
            source=np.moveaxis(np.asarray(image.convert('RGB')),2,0)
        target=np.zeros((3,layout['height'],layout['width']),dtype=np.uint8)
        reproject(source,target,src_transform=layout['source_transform'],src_crs='EPSG:25832',
                  dst_transform=layout['target_transform'],dst_crs='EPSG:3857',
                  resampling=Resampling.bilinear,num_threads=2,warp_mem_limit=128)
        path.parent.mkdir(parents=True,exist_ok=True)
        temporary=path.with_suffix('.tmp')
        Image.fromarray(np.moveaxis(target,0,2)).save(temporary,format='JPEG',quality=94)
        temporary.replace(path)
    return path

def config():
    return json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))

def template():
    cfg=config()
    data=ROOT/cfg['data_dir']
    # The geometry manifest is large. Reuse the public catalog until its
    # inputs change, rather than parsing it for every poll or job request.
    names=('road_ways.json','road_alignment.json','bounds.json','manifest.json','textures/ortho.jpg','ortho.tif')
    stamp=tuple((data/name).stat().st_mtime_ns if (data/name).is_file() else None for name in names)
    return _template(str(data),json.dumps(cfg,sort_keys=True),stamp)


@lru_cache(maxsize=4)
def _template(data_path,cfg_json,stamp):
    cfg=json.loads(cfg_json)
    data=Path(data_path)
    route=ordered_road(json.loads((data/'road_ways.json').read_text(encoding='utf-8')))
    transformer=Transformer.from_crs(4326,25832,always_xy=True)
    xy=np.array(transformer.transform(route[:,0],route[:,1])).T
    xy,alignment=load_alignment(data,xy)
    distances=np.r_[0,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    # Dense map samples make selection snap to the road rather than sparse OSM nodes.
    s=np.linspace(0,distances[-1],int(distances[-1])+1)
    points=np.c_[np.interp(s,distances,xy[:,0]),np.interp(s,distances,xy[:,1]),s]
    unproject=Transformer.from_crs(25832,4326,always_xy=True)
    lons,lats=unproject.transform(points[:,0],points[:,1])
    bounds=json.loads((data/'bounds.json').read_text())
    aerial=_map_layout(str(data),_map_stamp(data))
    meta=json.loads((data/'manifest.json').read_text(encoding='utf-8'))
    return {'id':'altenhain','name':'Langstraße, Altenhain','city':cfg['city'],
            'length_m':float(distances[-1]),'version':cfg['version'],
            'map':{'image_url':'/api/templates/altenhain/map.jpg',
                   'bounds':bounds,'route':points.round(3).tolist(),'crs':'EPSG:25832',
                   'coordinates':np.c_[lats,lons,s].round(7).tolist(),
                   'geographic_bounds':aerial['geographic_bounds'],'image_crs':'EPSG:3857'},
            'preview_url':'/api/templates/altenhain/preview.png',
            'summary':{'buildings':meta['buildings'],'elevation_gain_m':round(meta['elevation_max_m']-meta['elevation_min_m'],1)},
            'capabilities':{'min_segment_m':120,'max_road_width_m':6,'aerial_width_profile':alignment.get('applied',False),
                            'hillside_connections':True,'building_basements':True},
            'limitations':['Derzeit ist der vorbereitete Abschnitt der Langstraße in Altenhain verfügbar.',
                           'Die meisten Fassaden und die Vegetation sind angenähert. Eine zugeordnete Hauswand nutzt ein freies Ortsfoto von 2012.',
                           'Hanganschlüsse mit Mauern, Treppen und Zufahrten werden aus den Geländehöhen abgeleitet; die echten Grundstücksanlagen sind nicht vermessen.',
                           'Sockel und Kellerhüllen schließen geeignete Hausunterseiten bis zum Gelände. Diese Ergänzungen sind aus Gebäude- und Geländedaten abgeleitet.',
                           'Für freies Training. Keine KI, kein Verkehr und keine Zeitmessung.']}

def validate_request(value,data_root=None,geo_service=None):
    import math
    if not isinstance(value,dict):
        raise ValueError('Eine Streckenauswahl ist erforderlich.')
    allowed={'template_id','name','start_m','end_m','road_width_m','scenery_quality','vegetation_density','ai_materials','route_id'}
    if set(value)-allowed:
        raise ValueError('Unbekannte Streckeneinstellung.')
    source=value.get('template_id')
    if source not in ('altenhain','hessen'):
        raise ValueError('Dieser Bereich ist noch nicht verfügbar.')
    route=None
    if source=='hessen':
        if geo_service is None:
            from webapp.geodata import GeoService
            geo_service=GeoService(data_root or ROOT/'web-data')
        route=geo_service.get_route(value.get('route_id'))
    elif 'route_id' in value:
        raise ValueError('Eine neue Route muss als Hessen-Strecke erstellt werden.')
    name=value.get('name',route['name'] if route else 'Langstraße Altenhain')
    if not isinstance(name,str) or not 1<=len(name.strip())<=60 or any(ord(c)<32 for c in name):
        raise ValueError('Der Name muss 1 bis 60 Zeichen lang sein.')
    length=route['length_m'] if route else template()['length_m']
    def number(key,default,low,high):
        v=value.get(key,default)
        if isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) or not low<=v<=high:
            raise ValueError(f'Ungültiger Wert für {key}.')
        return float(v)
    start=number('start_m',0,0,length)
    end=number('end_m',length,0,length+0.01)
    end=min(end,length)
    # Match the generator's metre-scale floating-point boundary tolerance.
    if end-start<120-1e-8:
        raise ValueError('Der Abschnitt muss mindestens 120 Meter lang sein.')
    quality=value.get('scenery_quality','high')
    if quality not in ('standard','high'):
        raise ValueError('Unbekannte Detailstufe.')
    use_ai=value.get('ai_materials',False)
    if not isinstance(use_ai,bool):
        raise ValueError('KI-Materialien müssen ausdrücklich aktiviert werden.')
    result={'template_id':source,'name':name.strip(),'start_m':start,'end_m':end,
            'road_width_m':number('road_width_m',6,4,8 if route else 6),'scenery_quality':quality,
            'vegetation_density':number('vegetation_density',.65,0,1),'ai_materials':use_ai}
    if route:result['route_id']=route['id']
    return result
