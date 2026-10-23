"""Cache a bounded free Hessen DOM1 extract for canopy-height estimation.

The original terrain remains unchanged. DOM1 also contains buildings and must
only be interpreted as vegetation after footprint and road exclusion.
"""
from pathlib import Path
from datetime import datetime,timezone
from xml.etree import ElementTree as ET
import hashlib
import json
import numpy as np
import rasterio
from rasterio.warp import reproject,Resampling
import requests

ROOT=Path(__file__).resolve().parents[1]
URL='https://inspire-hessen.de/raster/dom1/ows'
MAX_BYTES=64_000_000


def fetch(root=ROOT):
    data=root/'data/altenhain'
    target=data/'dom1.tif'
    if target.is_file():
        with rasterio.open(target) as raster:
            if raster.crs.to_epsg()!=25832 or raster.count!=1 or raster.res!=(1.,1.):
                raise ValueError('Existing DOM1 cache does not match expected geometry')
        print('Cached DOM1:',target)
        return target
    session=requests.Session()
    session.headers['User-Agent']='RoadToAssetto/0.4 (bounded cached open DOM1 extract)'
    common={'SERVICE':'WCS','VERSION':'2.0.1'}
    response=session.get(URL,params={**common,'REQUEST':'GetCapabilities'},timeout=40)
    response.raise_for_status()
    capabilities=ET.fromstring(response.content)
    ids=[e.text for e in capabilities.iter() if e.tag.endswith('CoverageId')]
    license_text=next((e.text for e in capabilities.iter() if e.tag.endswith('Fees')),'')
    if 'dom1' not in ids or 'dl-de-zero-2.0' not in license_text:
        raise ValueError('Public DOM1 coverage or open license not confirmed')
    research = root / 'research'
    research.mkdir(parents=True, exist_ok=True)
    (research/'dom1_capabilities.xml').write_bytes(response.content)
    description=session.get(URL,params={**common,'REQUEST':'DescribeCoverage','COVERAGEID':'dom1'},timeout=40)
    description.raise_for_status()
    document=ET.fromstring(description.content)
    envelope=next(e for e in document.iter() if e.tag=='{http://www.opengis.net/gml/3.2}Envelope')
    if envelope.attrib.get('axisLabels')!='E N' or not envelope.attrib.get('srsName','').endswith('/25832'):
        raise ValueError('Unexpected DOM1 axes or projection')
    (research/'dom1_description.xml').write_bytes(description.content)
    bounds=json.loads((data/'bounds.json').read_text())
    if (bounds[2]-bounds[0])*(bounds[3]-bounds[1])>1_000_000:
        raise ValueError('DOM1 request exceeds one square kilometre')
    params=list(common.items())+[('REQUEST','GetCoverage'),('COVERAGEID','dom1'),('FORMAT','image/tiff'),
        ('SUBSET',f'E({bounds[0]},{bounds[2]})'),('SUBSET',f'N({bounds[1]},{bounds[3]})')]
    temporary=target.with_suffix('.download.tif')
    size=0
    try:
        with session.get(URL,params=params,timeout=90,stream=True) as response:
            response.raise_for_status()
            if int(response.headers.get('Content-Length','0'))>MAX_BYTES:
                raise ValueError('DOM1 response exceeds 64 MB cap')
            with temporary.open('wb') as file:
                for chunk in response.iter_content(1024*1024):
                    size+=len(chunk)
                    if size>MAX_BYTES:
                        raise ValueError('DOM1 response exceeds 64 MB cap')
                    file.write(chunk)
            request_url=response.url
        with rasterio.open(temporary) as dom, rasterio.open(data/'dgm1.tif') as dgm:
            if dom.crs.to_epsg()!=25832 or dom.count!=1 or dom.res!=(1.,1.):
                raise ValueError('Unexpected downloaded DOM1 geometry')
            if max(abs(a-b) for a,b in zip(dom.bounds,bounds))>1.01:
                raise ValueError('DOM1 extent differs from requested bounded area')
            surface=dom.read(1,masked=True)
            if surface.count()<surface.size*.99 or not np.isfinite(surface.compressed()).all():
                raise ValueError('DOM1 contains too many missing/invalid height samples')
            terrain=np.full(surface.shape,np.nan,dtype='float32')
            reproject(rasterio.band(dgm,1),terrain,src_transform=dgm.transform,src_crs=dgm.crs,
                src_nodata=dgm.nodata,dst_transform=dom.transform,dst_crs=dom.crs,dst_nodata=np.nan,
                resampling=Resampling.bilinear)
            difference=surface.filled(np.nan)-terrain
            valid=difference[np.isfinite(difference)]
            record={'recorded_at_utc':datetime.now(timezone.utc).isoformat(),'provider':'HVBG Hessen',
                'license':'dl-de/zero-2-0','license_evidence':license_text,'coverage_id':'dom1','capabilities_ids':ids,
                'capabilities_url':URL+'?SERVICE=WCS&VERSION=2.0.1&REQUEST=GetCapabilities','request_url':request_url,
                'product_url':'https://hvbg.hessen.de/landesvermessung/geotopographie/3d-daten/digitale-oberflaechenmodelle',
                'crs':'EPSG:25832','vertical_reference':'DHHN2016_NH, metres; HVBG DOM1 product description',
                'width':dom.width,'height':dom.height,'bounds':list(dom.bounds),'resolution_m':list(dom.res),
                'nodata':dom.nodata,'dtype':dom.dtypes[0],'bytes':size,
                'dom_minus_dgm_percentiles_m':dict(zip(['p0','p50','p90','p95','p99','p100'],map(float,np.percentile(valid,[0,50,90,95,99,100])))),
                'note':'Surface heights include roofs and vegetation; infer canopy heights only with building/road masks. Acquisition dates may differ from DGM and aerial imagery.'}
        record['sha256']=hashlib.sha256(temporary.read_bytes()).hexdigest()
        temporary.replace(target)
        (data/'dom1_source.json').write_text(json.dumps(record,indent=2,ensure_ascii=False),encoding='utf-8')
        print(json.dumps(record,indent=2,ensure_ascii=False))
        return target
    finally:
        if temporary.exists():
            temporary.unlink()


if __name__=='__main__':
    fetch()
