"""Sequential geographic acquisition and export for continuous long tracks."""
from __future__ import annotations

import gc
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import zipfile

import numpy as np

from roadtoassetto.long_track import (resample_route, plan_sections, build_profile,
    configure_section, plan_ownership, finalize_section, MultiModelExporter, aggregate_metadata)
from roadtoassetto.package import package, archive
from roadtoassetto.prepare import prepare, Heights
from roadtoassetto.scenery import select_route
from webapp.catalog import ROOT, config
from webapp.geodata import GeoError, TO_UTM, TO_WGS, _digest, _wgs_bounds, _json_write, estimate_storage
from webapp.regional import prepare_regional
from webapp.telemetry import set_phase,retry_wait


def selected_geometry(route, request):
    coordinates=np.asarray(route['coordinates'],dtype=float)
    raw=np.array(TO_UTM.transform(coordinates[:,1],coordinates[:,0])).T
    total=float(np.linalg.norm(np.diff(raw,axis=0),axis=1).sum())
    # Stored preview distances can differ by millimetres after coordinate rounding.
    end=min(float(request['end_m']),total)
    selected,_=select_route(raw,float(request['start_m']),end)
    lon,lat=TO_WGS.transform(selected[:,0],selected[:,1])
    return resample_route(np.c_[lat,lon].tolist(),allow_structures=True,
                          minimum_radius_m=float(request['road_width_m'])/2+.6)


def storage_estimate(sections):
    return estimate_storage(sections)


def copy_materials(source,destination):
    for name in ('PaintedPlaster006','surface_photos','PolyHavenRoofs','vegetation_photos','AIWindows','AIDoors','ai'):
        path=source/'materials'/name
        if path.is_dir():
            # Hard links are immutable input assets; textures are generated elsewhere.
            def link_or_copy(a,b):
                if Path(b).exists() and os.path.samefile(a,b):return b
                try:os.link(a,b)
                except OSError:shutil.copy2(a,b)
            shutil.copytree(path,destination/'materials'/name,copy_function=link_or_copy,dirs_exist_ok=True)


def render_section(section_root,track,progress):
    configured=os.environ.get('ROADTOASSETTO_BLENDER')
    candidates=([Path(configured)] if configured else [])+list((ROOT/'.tools').glob('blender*/blender.exe'))
    found=shutil.which('blender')
    if found:candidates.append(Path(found))
    blender=next((p for p in candidates if p.is_file()),None)
    cfg=json.loads((section_root/'tracks/altenhain.json').read_text(encoding='utf-8'))
    scene=json.loads((section_root/cfg['data_dir']/'scene.json').read_text(encoding='utf-8'))
    if blender:
        kwargs={'creationflags':subprocess.CREATE_NO_WINDOW} if os.name=='nt' else {}
        length=scene['metadata']['length_m']
        for name,view,distance in [('preview','overview',0),('driver','driver',length*.26),('environment','driver',length*.76)]:
            progress('Vorschau des ersten Streckenabschnitts: '+name,94)
            subprocess.run([str(blender),'--background','--python',str(ROOT/'scripts/blender_source.py'),'--',
                            '--root',str(section_root),'--view',view,'--distance',str(distance),'--preview-only',
                            '--output',str(track/'ui'/(name+'.png'))],cwd=ROOT,check=True,timeout=180,**kwargs)
        return 'render',['overview','driver','environment']
    from PIL import Image,ImageDraw
    data=section_root/cfg['data_dir']
    im=Image.open(data/'textures/ortho.jpg').convert('RGB');im.thumbnail((1280,900))
    bounds=json.loads((data/'bounds.json').read_text())
    origin=scene['metadata']['origin_utm32']
    points=[((x+origin[0]-bounds[0])/(bounds[2]-bounds[0])*im.width,
             (bounds[3]-(origin[1]-z))/(bounds[3]-bounds[1])*im.height)
            for x,_,z in scene['metadata']['centerline_ac']]
    draw=ImageDraw.Draw(im);draw.line(points,fill='white',width=8);draw.line(points,fill='#d97706',width=4)
    im.save(track/'ui/preview.png')
    return 'aerial_map',['overview']


def run_long(folder,request,route,geo,progress):
    from webapp.route_sections import section_route
    xy=selected_geometry(route,request)
    sections=plan_sections(xy)
    estimates=storage_estimate(sections)
    existing=sum(p.stat().st_size for p in folder.rglob('*') if p.is_file() and not p.is_symlink())
    # Fail before starting gigabytes of downloads, without imposing a route length cap.
    remaining=max(256*1024**2,estimates['workspace_bytes']-existing)
    if shutil.disk_usage(folder).free<remaining:
        raise GeoError(f'Für diese Auswahl werden grob {estimates["workspace_bytes"]/2**30:.1f} GiB Arbeitsraum benötigt; auf dem Datenlaufwerk ist weniger frei.', 'insufficient_storage',422)
    cfg=config();source=ROOT/cfg['data_dir']
    cfg.update(id='rtac_'+folder.name,name=request['name'],city='Hessen',road_name=route['name'],
               road_width_m=request['road_width_m'],scenery_quality=request['scenery_quality'],
               vegetation_density=request['vegetation_density'],allow_outside_photo_overrides=False,
               geotags=[str(v) for v in reversed(TO_WGS.transform(*xy[0]))])
    count=len(sections);raw_heights=np.full(len(xy),np.nan);surface_heights=np.full(len(xy),np.nan);inputs=[]
    tangent=np.gradient(xy,axis=0);tangent/=np.linalg.norm(tangent,axis=1)[:,None]
    left=np.c_[-tangent[:,1],tangent[:,0]]
    def acquire(call,label,percent):
        for attempt in range(3):
            try:return call()
            except GeoError as error:
                if error.status!=503 or attempt==2:raise
                progress(label+f': Datenabruf vorübergehend fehlgeschlagen, erneuter Versuch {attempt+2}/3',percent)
                with retry_wait((10,30)[attempt]):time.sleep((10,30)[attempt])
    regional=prepare_regional(geo.cache,sections,progress)
    # Validate each small route window before fetching its large aerial/building data.
    for section in sections:
        i=section['index'];label=f'Abschnitt {i+1}/{count}'
        progress(label+': Straßen und Bauwerke prüfen',5+round(20*i/count))
        window=xy[section['halo_start']:section['halo_end']+1]
        osm_bounds=np.r_[np.floor(window.min(axis=0)-130),np.ceil(window.max(axis=0)+130)].tolist()
        # Rendered scenery is clipped to 120 m. A 130 m OSM window retains its
        # complete context while allowing cached extracts to survive small
        # curve/section changes. DEM/imagery/buildings keep their 160 m halo.
        key,content=regional[i] if regional is not None else acquire(lambda:geo._osm(_wgs_bounds(osm_bounds)),label,5+round(20*i/count))
        selected=section_route(route,xy,section,content)
        context_record=geo.cache/'osm'/(key+'.json')
        context_provider=json.loads(context_record.read_text()).get('provider') if context_record.is_file() else getattr(geo,'overpass',None)
        selected.update(context_key=key,context_bounds_utm=osm_bounds,context_required_bounds_utm=osm_bounds,
                        context_sha256=hashlib.sha256(content).hexdigest(),context_provider=context_provider)
        if context_record.is_file():
            selected['regional_source']=json.loads(context_record.read_text()).get('regional_source')
        inputs.append(selected)
        # A selected range can have different geometry with the same index.
        # Include a deterministic geometry key to keep immutable source windows separate.
        selection_key=hashlib.sha256(xy[section['halo_start']:section['halo_end']+1].tobytes()).hexdigest()[:20]
        selected['_cache_id']=route['id']+'-'+selection_key
        cache=geo.cache/'inputs'/selected['_cache_id']/f'section-{i:04d}'
        cache.mkdir(parents=True,exist_ok=True)
        progress(label+': amtliche Geländehöhen laden',6+round(20*i/count))
        acquire(lambda:geo._raster(cache/'dgm1.tif',section['bounds'],'dgm'),label,6+round(20*i/count))
        dem=Heights(cache/'dgm1.tif')
        a,b=section['core_start'],section['core_end']+1
        raw_heights[a:b]=np.median([dem(xy[a:b]+left[a:b]*offset) for offset in (-.8,0,.8)],axis=0)
        del dem
        from roadtoassetto.structures import structure_kind
        if any(structure_kind(way['tags'])=='bridge' for way in selected['ways']):
            progress(label+': Brückenoberfläche aus DOM1 laden',6+round(20*i/count))
            acquire(lambda:geo._raster(cache/'dom1.tif',section['bounds'],'dom'),label,6+round(20*i/count))
            dom=Heights(cache/'dom1.tif')
            surface_heights[a:b]=np.percentile([dom(xy[a:b]+left[a:b]*offset) for offset in (-1.,-.5,0,.5,1.)],25,axis=0)
            del dom
    from roadtoassetto.structures import collect_spans,resolve_heights
    codes,spans=collect_spans(inputs,len(xy))
    road_heights,structures=resolve_heights(xy,raw_heights,spans,surface_heights)
    profile=build_profile(xy,raw_heights,request['road_width_m'],cfg.get('road_crossfall',.025),road_heights=road_heights,structure_codes=codes)
    progress('Durchgehende Fahrbahn und Geländegrenzen vorbereiten',27)
    ownership=plan_ownership(profile,sections)
    _json_write(folder/'section-plan.json',{'sections':sections,'estimates':estimates,'length_m':float(profile['distance'][-1])})
    track=folder/'build'/cfg['id'];exporter=MultiModelExporter(track,cfg['id'],profile,sections)
    first_metadata=None;totals={};ai_run=None;warnings=list(route.get('warnings',[]))
    shared_assets=folder/'shared-assets'
    fingerprint=hashlib.sha256()
    for module in sorted((ROOT/'roadtoassetto').glob('*.py'))+sorted((ROOT/'webapp').glob('*.py')):
        fingerprint.update(module.read_bytes())
    fingerprint.update(json.dumps(cfg,sort_keys=True).encode())
    fingerprint.update(xy.tobytes());fingerprint.update(profile['road_y'].tobytes());fingerprint.update(codes.tobytes())
    signature=fingerprint.hexdigest()
    # /app and /data are different mounts in Docker: copy once across that
    # boundary, then link per-section read-only inputs on the same data volume.
    copy_materials(source,shared_assets)
    if request['ai_materials']:
        from webapp.ai import generate_material_pack
        ai_run=generate_material_pack(folder.parent,shared_assets/'materials/ai',
             progress=lambda stage,p:progress(stage,28))
        cfg['ai_materials']=True
    for section,selected,domain in zip(sections,inputs,ownership):
        i=section['index'];label=f'Abschnitt {i+1}/{count}'
        section_root=folder/'sections'/f'{i:04d}';data=section_root/cfg['data_dir']
        base=30+round(60*i/count)
        provenance=acquire(lambda:geo.prepare_route(selected['_cache_id'],data,
            progress=lambda stage,p:progress(label+': '+stage,base+round(15*p/(100*count))),
            section_route=selected,section_index=i),label,base)
        copy_materials(shared_assets,data)
        section_cfg=configure_section(cfg,profile,section,ownership=domain)
        section_cfg['_source_way_ids']=selected['source_way_ids']
        section_cfg['source_route']={k:route[k] for k in ('id','name','length_m','source','warnings') if k in route}
        (section_root/'tracks').mkdir(parents=True,exist_ok=True)
        _json_write(section_root/'tracks/altenhain.json',section_cfg)
        checkpoint=section_root/'scene-checkpoint.json'
        reused=False
        if checkpoint.is_file() and (data/'scene.json').is_file():
            saved=json.loads(checkpoint.read_text())
            if saved.get('signature')==signature and saved.get('sha256')==_digest(data/'scene.json'):
                progress(label+': geprüftes Zwischenmodell wiederverwenden',base+round(20/count))
                scene=json.loads((data/'scene.json').read_text(encoding='utf-8'));reused=True
        if not reused:
            progress(label+': Straße, Gelände und Gebäude bauen',base+round(20/count))
            scene=prepare(section_root,section_cfg)
            set_phase('clip')
            scene=finalize_section(scene,profile,section,domain)
        if first_metadata is None:first_metadata=dict(scene['metadata'])
        for field in ('buildings','hillside_retaining_assemblies','hillside_staircases','hillside_driveways',
                      'bridge_segments','tunnel_segments','context_bridges','structure_piers','tunnel_portals'):
            totals[field]=totals.get(field,0)+scene['metadata'].get(field,0)
        warnings.extend(w for w in provenance['warnings'] if w not in warnings)
        # Preserve the preview before the export namespaces the KN5 textures.
        _json_write(data/'scene.json',scene)
        _json_write(data/'manifest.json',scene['metadata'])
        _json_write(checkpoint,{'signature':signature,'sha256':_digest(data/'scene.json')})
        progress(label+': Modell exportieren und Übergang prüfen',base+round(45/count))
        exporter.write_section(scene,section,data/'textures')
        destination=track/'provenance'/'sections'/f'{i:04d}';destination.mkdir(parents=True,exist_ok=True)
        for name in ('area.osm','road_ways.json','bounds.json','retrieval.json'):
            shutil.copy2(data/name,destination/name)
        _json_write(destination/'geometry.json',{'section':section,'model':exporter.models[-1]})
        del scene;gc.collect()
    models=exporter.finish()
    metadata=aggregate_metadata(first_metadata,profile,models)
    metadata.update(totals,sections_count=count,preview_scope='representative_section')
    metadata.update(bridges_count=sum(s['kind']=='bridge' for s in structures),
                    tunnels_count=sum(s['kind']=='tunnel' for s in structures))
    metadata['source_route']={k:route[k] for k in ('id','name','length_m','coordinates','bounds','source') if k in route}
    metadata['source_route']['warnings']=warnings
    cfg['source_route']=metadata['source_route']
    cfg['data_dir']='sections/0000/'+cfg['data_dir']
    track=package(folder,cfg,{'metadata':metadata})
    exporter.finish()  # package's common files must not replace the model list.
    _json_write(track/'provenance/routing.json',{k:route.get('routing',{}).get(k) for k in
        ('provider','attribution','policy_url','exclude','provider_distance_m','data_version')})
    _json_write(track/'provenance/section-plan.json',{'sections':sections,'models':models})
    _json_write(track/'provenance/structures.json',{'structures':structures,'crossings':profile['crossings'],
        'accuracy':'OSM extents; inferred approach/DOM-constrained heights and generic construction, not a structural survey.'})
    longitude,latitude=TO_WGS.transform(xy[:,0],xy[:,1])
    selection={'start_m':request['start_m'],'end_m':request['end_m'],'planned_length_m':route['length_m'],
               'generated_length_m':float(profile['distance'][-1])}
    _json_write(track/'provenance/generated-route.json',{'selection':selection,
        'coordinates':np.c_[latitude,longitude].round(8).tolist(),
        'geometry':'Actual globally resampled and smoothed generated road centerline; latitude, longitude'})
    # The legacy top-level source record describes the first window. Mark its
    # scope explicitly and index the complete per-section provenance beside it.
    section_records=[f'provenance/sections/{s["index"]:04d}/retrieval.json' for s in sections]
    for name in ('retrieval.json','route.json'):
        path=track/'provenance'/name
        record=json.loads(path.read_text(encoding='utf-8'))
        record.update(sources_scope='first_section',section_source_records=section_records,selection=selection,
                      generated_route_record='provenance/generated-route.json')
        _json_write(path,record)
    manifest_path=track/'prototype_manifest.json'
    manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
    manifest['section_source_records']=section_records
    manifest['selection']=selection
    manifest['source_route']['sources_scope']='first_section; see section_source_records for the whole route'
    manifest['detail_statistics_scope']='Per-section detail statistics remain in section sources; length, elevation and building/hillside totals describe the whole track.'
    manifest['provenance_files']={path.relative_to(track).as_posix():{'bytes':path.stat().st_size,'sha256':_digest(path)}
                                  for path in sorted((track/'provenance').rglob('*')) if path.is_file()}
    _json_write(manifest_path,manifest)
    with (track/'CREDITS.txt').open('a',encoding='utf-8') as stream:
        stream.write('\nLong-route planning: OSRM / FOSSGIS and OpenStreetMap contributors (ODbL). '
                     'See provenance/routing.json. Terrain, aerial and building provenance is recorded separately '
                     'for every model in provenance/sections/. The top-level retrieval is the first section only.\n')
    preview_kind,views=render_section(folder/'sections/0000',track,progress)
    metadata.update(preview_kind=preview_kind,preview_views=views)
    progress('Gesamtes Streckenpaket schreiben und prüfen',97)
    output=archive(folder,cfg,track,progress=lambda index,total:progress(f'Streckenpaket schreiben: Datei {index+1}/{total}',97))
    with zipfile.ZipFile(output) as zipped:
        entries=zipped.infolist()
        for index,entry in enumerate(entries):
            progress(f'Streckenpaket prüfen: Datei {index+1}/{len(entries)}',99)
            # ZipExtFile checks CRC at EOF, using bounded memory per entry.
            with zipped.open(entry) as stream:
                while stream.read(1024*1024):pass
        if any(not (track/m['file']).is_file() or _digest(track/m['file'])!=m['sha256'] for m in models):
            raise ValueError('Exported section is missing or changed')
    summary={k:v for k,v in metadata.items() if k not in ('centerline_ac','road_width_profile_m','building_base_records','building_base_source_footprints','models')}
    summary['source_route']={k:v for k,v in metadata['source_route'].items() if k in ('id','name','length_m','source','warnings')}
    if ai_run is not None:summary['ai_generation']={k:ai_run[k] for k in ('cache_hit','usage')}
    result={'archive':output.relative_to(folder).as_posix(),'summary':summary,
            'preview':(track/'ui/preview.png').relative_to(folder).as_posix()}
    for name in ('driver','environment'):
        if name in views:result[name]=(track/'ui'/(name+'.png')).relative_to(folder).as_posix()
    _json_write(folder/'result.json',result)
    progress('Fertig',100)
