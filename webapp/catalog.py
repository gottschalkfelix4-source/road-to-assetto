"""Explicit local geographic templates; never fetch user-provided URLs."""
import json
from pathlib import Path
import numpy as np
from pyproj import Transformer
from roadtoassetto.prepare import ordered_road
from roadtoassetto.road_alignment import load_alignment

ROOT=Path(__file__).resolve().parents[1]

def config():
    return json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))

def template():
    cfg=config()
    data=ROOT/cfg['data_dir']
    route=ordered_road(json.loads((data/'road_ways.json').read_text(encoding='utf-8')))
    transformer=Transformer.from_crs(4326,25832,always_xy=True)
    xy=np.array(transformer.transform(route[:,0],route[:,1])).T
    xy,alignment=load_alignment(data,xy)
    distances=np.r_[0,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    # Dense map samples make selection snap to the road rather than sparse OSM nodes.
    s=np.linspace(0,distances[-1],int(distances[-1])+1)
    points=np.c_[np.interp(s,distances,xy[:,0]),np.interp(s,distances,xy[:,1]),s]
    meta=json.loads((data/'manifest.json').read_text(encoding='utf-8'))
    return {'id':'altenhain','name':'Langstraße, Altenhain','city':cfg['city'],
            'length_m':float(distances[-1]),'version':cfg['version'],
            'map':{'image_url':'/api/templates/altenhain/map.jpg',
                   'bounds':json.loads((data/'bounds.json').read_text()),'route':points.round(3).tolist()},
            'preview_url':'/api/templates/altenhain/preview.png',
            'summary':{'buildings':meta['buildings'],'elevation_gain_m':round(meta['elevation_max_m']-meta['elevation_min_m'],1)},
            'capabilities':{'min_segment_m':120,'max_road_width_m':6,'aerial_width_profile':alignment.get('applied',False),
                            'hillside_connections':True,'building_basements':True},
            'limitations':['Derzeit ist der vorbereitete Abschnitt der Langstraße in Altenhain verfügbar.',
                           'Die meisten Fassaden und die Vegetation sind angenähert. Eine zugeordnete Hauswand nutzt ein freies Ortsfoto von 2012.',
                           'Hanganschlüsse mit Mauern, Treppen und Zufahrten werden aus den Geländehöhen abgeleitet; die echten Grundstücksanlagen sind nicht vermessen.',
                           'Sockel und Kellerhüllen schließen geeignete Hausunterseiten bis zum Gelände. Diese Ergänzungen sind aus Gebäude- und Geländedaten abgeleitet.',
                           'Für freies Training. Keine KI, kein Verkehr und keine Zeitmessung.']}

def validate_request(value):
    import math
    if not isinstance(value,dict):
        raise ValueError('Eine Streckenauswahl ist erforderlich.')
    allowed={'template_id','name','start_m','end_m','road_width_m','scenery_quality','vegetation_density','ai_materials'}
    if set(value)-allowed:
        raise ValueError('Unbekannte Streckeneinstellung.')
    if value.get('template_id')!='altenhain':
        raise ValueError('Dieser Bereich ist noch nicht verfügbar.')
    name=value.get('name','Langstraße Altenhain')
    if not isinstance(name,str) or not 1<=len(name.strip())<=60 or any(ord(c)<32 for c in name):
        raise ValueError('Der Name muss 1 bis 60 Zeichen lang sein.')
    length=template()['length_m']
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
    return {'template_id':'altenhain','name':name.strip(),'start_m':start,'end_m':end,
            'road_width_m':number('road_width_m',6,4,6),'scenery_quality':quality,
            'vegetation_density':number('vegetation_density',.65,0,1),'ai_materials':use_ai}
