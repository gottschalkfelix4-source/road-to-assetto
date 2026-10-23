"""Cache a bounded set of CC0 photographic surface materials from ambientCG."""
from pathlib import Path
from datetime import datetime,timezone
import hashlib
import io
import json
import zipfile
import requests

ROOT=Path(__file__).resolve().parents[1]
ASSETS=('Asphalt023S','Grass004','Gravel022')

def fetch():
    target=ROOT/'data/altenhain/materials/surface_photos'
    target.mkdir(parents=True,exist_ok=True)
    session=requests.Session()
    session.headers['User-Agent']='RoadToAssetto/0.4 (bounded CC0 material cache)'
    records=[]
    for asset in ASSETS:
        folder=target/asset
        folder.mkdir(exist_ok=True)
        meta=folder/'source.json'
        if meta.exists() and list(folder.glob('*Color*')) and list(folder.glob('*NormalDX*')):
            records.append(json.loads(meta.read_text(encoding='utf-8')))
            print('Cached:',asset,flush=True)
            continue
        response=session.get('https://ambientcg.com/api/v2/full_json',params={'id':asset,'include':'downloadData'},timeout=30)
        response.raise_for_status()
        info=next(a for a in response.json()['foundAssets'] if a['assetId']==asset)
        options=info['downloadFolders']['default']['downloadFiletypeCategories']['zip']['downloads']
        download=next(d for d in options if d['attribute']=='2K-JPG')
        response=session.get(download['downloadLink'],timeout=60,stream=True)
        response.raise_for_status()
        content=bytearray()
        for chunk in response.iter_content(256*1024):
            content.extend(chunk)
            if len(content)>64*1024*1024:raise ValueError('Material exceeds bounded download size')
        files={}
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            for item in archive.infolist():
                name=Path(item.filename).name
                if name.endswith('.jpg') and any(part in name for part in ('Color','NormalDX','NormalGL','Roughness','AmbientOcclusion')):
                    if item.file_size>32*1024*1024:raise ValueError('Image exceeds size limit')
                    data=archive.read(item)
                    (folder/name).write_bytes(data)
                    files[name]=hashlib.sha256(data).hexdigest()
        record={'asset':asset,'provider':'ambientCG','url':'https://ambientcg.com/view?id='+asset,
                'license':'CC0-1.0','license_url':'https://docs.ambientcg.com/license/',
                'download_url':download['downloadLink'],'download_sha256':hashlib.sha256(content).hexdigest(),
                'creation_method':info.get('creationMethod'),'dimensions_cm':[info.get('dimensionX'),info.get('dimensionY')],
                'retrieved_at':datetime.now(timezone.utc).isoformat(),'files':files}
        meta.write_text(json.dumps(record,indent=2),encoding='utf-8')
        records.append(record)
        print('Downloaded:',asset,len(content),flush=True)
    (target/'asset_sources.json').write_text(json.dumps(records,indent=2),encoding='utf-8')

if __name__=='__main__':fetch()
