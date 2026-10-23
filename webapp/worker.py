"""One isolated track build, launched by the local queue."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from webapp.catalog import ROOT,config,validate_request
from roadtoassetto.prepare import prepare
from roadtoassetto.kn5 import write
from roadtoassetto.package import package,archive
from scripts.validate import validate
from webapp.geodata import GeoService,GeoError

def write_progress(folder,stage,percent):
    temp=folder/'progress.tmp'
    temp.write_text(json.dumps({'stage':stage,'progress':percent},ensure_ascii=False),encoding='utf-8')
    # Windows may still have the previous progress file open for a queue read.
    for attempt in range(8):
        try:
            temp.replace(folder/'progress.json')
            return
        except PermissionError:
            if attempt==7:raise
            time.sleep(min(.025*2**attempt,.4))


def run(folder):
    geo=GeoService(folder.parent)
    request=validate_request(json.loads((folder/'request.json').read_text(encoding='utf-8')),data_root=folder.parent,geo_service=geo)
    def progress(stage,percent):
        write_progress(folder,stage,percent)
    progress('Gelände und Gebäude vorbereiten',5)
    cfg=config();source=ROOT/cfg['data_dir'];data=folder/cfg['data_dir']
    data.mkdir(parents=True,exist_ok=True)
    route=None
    if request['template_id']=='hessen':
        route=geo.get_route(request['route_id'])
        provenance=geo.prepare_route(route['id'],data,progress=lambda stage,percent:progress(stage,5+round(float(percent)*.1)))
        route['warnings']=provenance['warnings']
        cfg.update(city='Hessen',road_name=route['name'],
                   geotags=[str(v) for v in route['coordinates'][0]],
                   source_route={k:route[k] for k in ('id','name','length_m','coordinates','bounds','source','warnings') if k in route})
        cfg['source_route']['sources']=provenance['sources']
    else:
        for name in ('road_ways.json','road_alignment.json','marking_observations.json','junction_observations.json','bounds.json','area.osm','dgm1.tif','dom1.tif','dom1_source.json','ortho.tif','buildings_lod2.gml','facade_overrides.json','visual_sources.json','retrieval.json'):
            if (source/name).is_file():
                shutil.copy2(source/name,data/name)
    for asset in ('PaintedPlaster006','surface_photos','PolyHavenRoofs','vegetation_photos'):
        shutil.copytree(source/'materials'/asset,data/'materials'/asset)
    if (source/'materials/AIWindows').is_dir():
        shutil.copytree(source/'materials/AIWindows',data/'materials/AIWindows')
    if (source/'materials/AIDoors').is_dir():
        shutil.copytree(source/'materials/AIDoors',data/'materials/AIDoors')
    if route is None and (source/'photos').exists():
        shutil.copytree(source/'photos',data/'photos')
    cfg.update(id='rtac_'+folder.name,name=request['name'],road_width_m=request['road_width_m'],
               route_start_m=request['start_m'],route_end_m=request['end_m'],
               scenery_quality=request['scenery_quality'],vegetation_density=request['vegetation_density'])
    cfg['allow_outside_photo_overrides']=route is None
    ai_run=None
    if request['ai_materials']:
        from webapp.ai import generate_material_pack
        progress('KI-Materialien erzeugen oder aus Cache laden',16)
        ai_run=generate_material_pack(folder.parent,data/'materials/ai',
            progress=lambda stage,percent:progress(stage,16+round(percent*.04)))
        cfg['ai_materials']=True
    (folder/'tracks').mkdir()
    (folder/'tracks/altenhain.json').write_text(json.dumps(cfg,ensure_ascii=False,indent=2),encoding='utf-8')
    progress('Straße, Häuser mit Kellerhüllen und Hanganschlüsse bauen',22)
    scene=prepare(folder,cfg)
    progress('Assetto-Corsa-Modell erstellen',55)
    track=package(folder,cfg,scene)
    write(track/(cfg['id']+'.kn5'),scene,data/'textures')
    progress('Vorschau berechnen',70)
    configured=os.environ.get('ROADTOASSETTO_BLENDER')
    candidates=[Path(configured)] if configured else []
    candidates+=list((ROOT/'.tools').glob('blender*/blender.exe'))
    found=shutil.which('blender')
    if found:
        candidates.append(Path(found))
    blender=next((p for p in candidates if p.is_file()),None)
    preview_kind='render'
    if blender:
        kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        subprocess.run([str(blender),'--background','--python',str(ROOT/'scripts/blender_source.py'),'--','--root',str(folder)],cwd=ROOT,check=True,timeout=180,**kwargs)
        for distance,name in ((scene['metadata']['length_m']*.26,'driver'),(scene['metadata']['length_m']*.76,'environment')):
            subprocess.run([str(blender),'--background','--python',str(ROOT/'scripts/blender_source.py'),'--','--root',str(folder),
                            '--view','driver','--distance',str(distance),'--preview-only','--output',str(track/'ui'/(name+'.png'))],
                           cwd=ROOT,check=True,timeout=180,**kwargs)
    else:
        # Accurate aerial route view when an optional 3D renderer is not installed.
        from PIL import Image,ImageDraw
        import numpy as np
        im=Image.open(data/'textures/ortho.jpg').convert('RGB')
        im.thumbnail((1280,900))
        bounds=json.loads((data/'bounds.json').read_text())
        points=np.asarray(scene['metadata']['centerline_ac'])
        origin=scene['metadata']['origin_utm32']
        route=[((x+origin[0]-bounds[0])/(bounds[2]-bounds[0])*im.width,
                (bounds[3]-(origin[1]-z))/(bounds[3]-bounds[1])*im.height) for x,_,z in points]
        draw=ImageDraw.Draw(im);draw.line(route,fill='white',width=8);draw.line(route,fill='#d97706',width=4)
        im.save(track/'ui/preview.png');preview_kind='aerial_map'
    progress('Paket und Fahrbahn prüfen',92)
    output=archive(folder,cfg,track)
    if not validate(folder):
        raise RuntimeError('Generated track validation failed')
    # Detailed geometric evidence belongs to the downloadable manifest, not
    # each recurring browser job-list response.
    summary={k:v for k,v in scene['metadata'].items()
             if k not in ('centerline_ac','building_base_records','building_base_source_footprints')}
    if ai_run is not None:
        # Report this run, rather than the historical usage of cached images.
        # The provider helper's absolute file paths stay out of public results.
        summary['ai_generation']={key:ai_run[key] for key in ('cache_hit','usage')}
    summary['preview_kind']=preview_kind
    result={'archive':output.relative_to(folder).as_posix(),'preview':(track/'ui/preview.png').relative_to(folder).as_posix(),'summary':summary}
    for name in ('driver','environment'):
        if (track/'ui'/(name+'.png')).is_file():
            result[name]=(track/'ui'/(name+'.png')).relative_to(folder).as_posix()
    summary['preview_views']=['overview']+([name for name in ('driver','environment') if name in result])
    (folder/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    progress('Fertig',100)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--job-dir',type=Path,required=True)
    folder=parser.parse_args().job_dir.resolve()
    from webapp.ai import AIError
    try:
        run(folder)
    except (AIError,GeoError) as error:
        (folder/'error.json').write_text(json.dumps({'error':getattr(error,'message',str(error)),'code':error.code}),encoding='utf-8')
        raise SystemExit(1)
    except ValueError as error:
        # Geometry diagnostics are safe, actionable errors; paths/provider
        # bodies are never inserted into these validation messages.
        (folder/'error.json').write_text(json.dumps({'error':'Die gewählte Strecke konnte nicht sicher aufgebaut werden: '+str(error)[:250],'code':'geometry_validation'}),encoding='utf-8')
        raise SystemExit(1)
