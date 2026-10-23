"""Cache the small public OSM / Hessian WCS extract for this prototype.

No key or account is required. Existing cached files are reused. Bounding boxes
and the street name are explicit in the track configuration; no personal
location data is inferred.
"""
from pathlib import Path
from xml.etree import ElementTree as ET
import json
import math
import sys
from datetime import datetime,timezone
import requests
import rasterio
from pyproj import Transformer

ROOT=Path(__file__).resolve().parents[1]
cfg=json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))
out=ROOT/cfg['data_dir'];out.mkdir(parents=True,exist_ok=True)
session=requests.Session()
session.headers['User-Agent']='RoadToAssettoPrototype/0.1 (small cached public geographic extract)'
road_file=out/'road_ways.json'
if not road_file.exists():
    bbox=','.join(str(v) for v in cfg['search_bbox'])
    query=f'[out:json][timeout:45];way["highway"]["name"={json.dumps(cfg["road_name"],ensure_ascii=False)}]({bbox});out geom;'
    r=session.post('https://overpass-api.de/api/interpreter',data={'data':query},timeout=65)
    r.raise_for_status()
    ways=r.json()['elements']
    if not ways:
        raise SystemExit('No street found; check name and search_bbox')
    road_file.write_text(json.dumps(ways,indent=2,ensure_ascii=False),encoding='utf-8')
ways=json.loads(road_file.read_text(encoding='utf-8'))
project=Transformer.from_crs(4326,25832,always_xy=True)
xy=[project.transform(p['lon'],p['lat']) for w in ways for p in w['geometry']]
padding=160
b=[math.floor(min(p[0] for p in xy))-padding,math.floor(min(p[1] for p in xy))-padding,
   math.ceil(max(p[0] for p in xy))+padding,math.ceil(max(p[1] for p in xy))+padding]
(out/'bounds.json').write_text(json.dumps(b),encoding='utf-8')
for filename,url,coverage,scale in [('dgm1.tif',cfg['dem_source'],'he_dgm1',None),
                                     ('ortho.tif',cfg['ortho_source'],'he_dop20','1')]:
    path=out/filename
    if path.exists():
        print('Cached:',filename,flush=True)
        continue
    params=[('SERVICE','WCS'),('VERSION','2.0.1'),('REQUEST','GetCoverage'),('COVERAGEID',coverage),('FORMAT','GTIFF'),
            ('SUBSET',f'e({b[0]},{b[2]})'),('SUBSET',f'n({b[1]},{b[3]})')]
    if scale:
        params.append(('SCALEFACTOR',scale))
    r=session.get(url,params=params,timeout=90)
    r.raise_for_status()
    if not (r.content.startswith(b'II') or r.content.startswith(b'MM')):
        raise RuntimeError('WCS returned no GeoTIFF: '+r.text[:500])
    temporary=path.with_suffix('.download.tif')
    temporary.write_bytes(r.content)
    with rasterio.open(temporary) as raster:
        if raster.crs.to_epsg()!=25832 or not raster.width or not raster.height:
            raise RuntimeError('Unexpected WCS raster georeferencing')
        if filename=='ortho.tif' and (raster.count<3 or max(raster.res)>.201):
            raise RuntimeError('Expected native DOP20 RGB resolution')
        # Decode the complete download before replacing the cache.
        raster.read()
    temporary.replace(path)
    print('Downloaded:',filename,len(r.content),flush=True)
if not (out/'area.osm').exists():
    unproject=Transformer.from_crs(25832,4326,always_xy=True)
    west,south=unproject.transform(b[0],b[1]);east,north=unproject.transform(b[2],b[3])
    r=session.get(cfg['osm_source'],params={'bbox':f'{west},{south},{east},{north}'},timeout=60)
    r.raise_for_status();ET.fromstring(r.content)
    (out/'area.osm').write_bytes(r.content)
    print('Downloaded OSM area',len(r.content),flush=True)
provenance=out/'retrieval.json'
if not provenance.exists():
    provenance.write_text(json.dumps({'recorded_at_utc':datetime.now(timezone.utc).isoformat(),
                                    'config':cfg,'note':'Existing cached data may have been fetched earlier in the same session.'},
                                   indent=2,ensure_ascii=False),encoding='utf-8')
print('Data ready:',out)
