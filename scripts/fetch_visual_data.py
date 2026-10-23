"""Fetch open LoD2 geometry and CC0 plaster, with bounded area and caching."""
from pathlib import Path
from xml.etree import ElementTree as ET
import io
import json
import requests
import zipfile
from pyproj import Transformer

ROOT=Path(__file__).resolve().parents[1]
cfg=json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))
out=ROOT/cfg['data_dir']
s=requests.Session();s.headers['User-Agent']='RoadToAssetto/0.2 (small cached public geographic extract)'
path=out/'buildings_lod2.gml'
if not path.exists() or not ET.parse(path).getroot().findall('{http://www.opengis.net/wfs/2.0}member'):
    b=json.loads((out/'bounds.json').read_text())
    tr=Transformer.from_crs(25832,4326,always_xy=True)
    west,south=tr.transform(b[0],b[1]);east,north=tr.transform(b[2],b[3])
    url='https://inspire-hessen.de/ows/services/org.2.ef07833e-78a6-4c2c-a895-e31de788aac3_wfs'
    params={'SERVICE':'WFS','VERSION':'2.0.0','REQUEST':'GetFeature','TYPENAMES':'bu-core3d:Building',
            'SRSNAME':'urn:ogc:def:crs:EPSG::7423','BBOX':f'{south},{west},{north},{east},urn:ogc:def:crs:EPSG::4326','COUNT':1000}
    r=s.get(url,params=params,timeout=90);r.raise_for_status()
    doc=ET.fromstring(r.content)
    members=doc.findall('{http://www.opengis.net/wfs/2.0}member')
    if not members or len(members)>=1000:
        raise RuntimeError('Empty or potentially truncated building extract')
    path.write_bytes(r.content)
    print('LoD2 buildings fetched:',len(members))
asset='PaintedPlaster006'
folder=out/'materials'/asset;folder.mkdir(parents=True,exist_ok=True)
if not list(folder.glob('*Color*')) or not list(folder.glob('*NormalDX*')):
    r=s.get('https://ambientcg.com/api/v2/full_json',params={'id':asset,'include':'downloadData'},timeout=30)
    r.raise_for_status()
    data=r.json()['foundAssets'][0]
    choices=data['downloadFolders']['default']['downloadFiletypeCategories']['zip']['downloads']
    download=next(d for d in choices if d['attribute']=='2K-JPG')
    r=s.get(download['downloadLink'],timeout=60);r.raise_for_status()
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for name in z.namelist():
            if name.endswith('.jpg') and any(s in name for s in ('Color','NormalDX','NormalGL','Roughness','AmbientOcclusion')):
                (folder/Path(name).name).write_bytes(z.read(name))
(out/'visual_sources.json').write_text(json.dumps({'buildings':{'provider':'HVBG Hessen','license':'dl-de/zero-2-0',
    'url':'https://hvbg.hessen.de/landesvermessung/geotopographie/3d-daten/3d-gebaeudemodelle'},
    'plaster':{'provider':'ambientCG','asset':asset,'license':'CC0-1.0','url':'https://ambientcg.com/view?id='+asset},
    'facades':'Generic village facade styles; no local facade photos included; no Google assets'},indent=2),encoding='utf-8')
print('Open visual data ready')
