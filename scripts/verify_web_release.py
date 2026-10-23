"""Explicit HTTP build/download verification; creates one real queued track job.

Run only when the intended server is ready:
  python scripts/verify_web_release.py --run-build --url http://127.0.0.1:18765
This is deliberately not a unittest-discovery test. It never starts a game.
"""
from pathlib import Path,PurePosixPath
from datetime import datetime,timezone
from io import BytesIO
import argparse
import hashlib
import json
import math
import re
import struct
import sys
import time
from urllib.parse import urlsplit
import zipfile
import numpy as np
import requests
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.inspect_kn5 import inspect
from roadtoassetto.terrain_sampling import ExportedTerrainSampler
from scripts.verify_street_contacts import contact_vertices,hillside_contact_checks


class MemoryAsset:
    def __init__(self,content):
        self.content=content
    def read_bytes(self):
        return self.content


def release_at_least(version,minimum):
    match=re.fullmatch(r'(\d+)\.(\d+)\.(\d+)',version)
    if match is None:
        raise ValueError('Invalid release version')
    return tuple(map(int,match.groups()))>=minimum


def embedded_textures(content,wanted):
    """Read selected original payloads from an already inspected KN5."""
    stream=BytesIO(content)
    def read(size):
        if not 0<=size<=256_000_000:
            raise ValueError('Invalid embedded texture size')
        data=stream.read(size)
        if len(data)!=size:
            raise ValueError('Truncated embedded texture')
        return data
    def integer():return struct.unpack('<i',read(4))[0]
    if read(6)!=b'sc6969':raise ValueError('Invalid KN5 texture header')
    version=integer()
    if version not in (5,6):raise ValueError('Unsupported KN5 texture version')
    if version==6:integer()
    count=integer()
    if not 0<=count<=10000:raise ValueError('Invalid KN5 texture count')
    result={}
    for _ in range(count):
        integer();name=read(integer()).decode('utf-8');payload=read(integer())
        if name in wanted:result[name]=payload
    return result


def verify_realism_v09(manifest,model,content,check,report):
    """Check actual v0.9 texture payloads and new geometry/provenance."""
    ground=next(m for m in model['materials'] if m['name']=='aerial_ground')
    samplers={name:texture for name,slot,texture in ground['samplers']}
    expected={'txDiffuse':'ground_macro_reflectance.png','txMask':'ground_detail_mask.png',
              'txDetailR':'ground_grass_color.jpg','txDetailG':'ground_gravel_color.jpg',
              'txDetailB':'asphalt_photo.jpg','txDetailA':'ground_neutral.png'}
    check('ground_uses_classified_original_colour_materials',all(samplers.get(k)==v for k,v in expected.items()))
    props=ground['properties']
    check('ground_colour_detail_metric_scales',all(math.isclose(props.get(key,[0.])[0],value,abs_tol=1e-6)
          for key,value in {'multR':1/1.4,'multG':1/1.5,'multB':1/1.25,'magicMult':1.}.items()))
    fields=manifest.get('ground_materials',{}).get('terrain_material_fields',{})
    check('ground_rgb_semantics_not_projected_photo',fields.get('photographic_brightness_projected') is False and
          'RGBI' in fields.get('classification','') and 'Inferred' in fields.get('limitation',''))
    check('ground_material_transition_scales',math.isclose(fields.get('transition_sigma_m',0),.55) and
          math.isclose(fields.get('macro_sigma_m',0),8.))
    payloads=embedded_textures(content,set(expected.values()))
    assets={r['asset']:r for r in manifest['ground_materials']['source_records']}
    for asset,filename in [('Grass004','ground_grass_color.jpg'),('Gravel022','ground_gravel_color.jpg'),
                           ('Asphalt023S','asphalt_photo.jpg')]:
        hashes=[value for key,value in assets[asset]['files'].items() if '_Color.' in key]
        check('ground_original_'+asset+'_pixels',len(hashes)==1 and
              hashlib.sha256(payloads[filename]).hexdigest()==hashes[0])
    with Image.open(BytesIO(payloads['ground_detail_mask.png'])) as image:
        check('ground_mask_dimensions',list(image.size)==fields.get('field_size_px'))
        mask=np.asarray(image.convert('RGBA'),dtype=np.int16)
    check('ground_mask_normalized_no_alpha_layer',bool(np.all(mask.sum(axis=2)==255) and np.all(mask[:,:,3]==0)))
    actual=mask[:,:,:3].mean(axis=(0,1))/255.
    stated=np.array([fields.get('mean_material_weights',{}).get(key,-1) for key in ('grass','mineral','impervious')])
    check('ground_mask_agrees_with_manifest',np.isfinite(stated).all() and
          np.allclose(actual,stated,atol=.002) and np.all(actual>0))
    check('independent_paint_families_exported',len([n for n in manifest.get('inferred_paint_families',{}).values() if n>0])>=2)
    check('metric_plinth_geometry_exported',manifest.get('metric_plinth_surfaces',0)>0 and
          any(m['name'].startswith('LOD2_WALLS_PLINTH') for m in model['meshes']))
    check('three_dimensional_downpipes_exported',manifest.get('three_dimensional_downpipes',0)>0 and
          any(m['name'].startswith('LOD2_DOWNPIPES') for m in model['meshes']))
    check('new_architecture_inferred_provenance',all(word in manifest.get('building_detail_accuracy','')
          for word in ('Paint colours','plinth heights','downpipes','inferred','unchanged')))
    check('narrow_wall_footings_exported',manifest.get('house_foundation_ground_panels',0)>0 and
          manifest.get('terrain_foundation_surfaces',0)>0 and
          any(m['name'].startswith('HOUSE_FOUNDATION_GRAVEL') for m in model['meshes']))
    check('wall_footings_inferred_limits',all(word in manifest.get('house_foundation_ground_accuracy','')
          for word in ('Inferred','.45 m wide','.45 m cut','.30 m fill','excluded')))
    check('fixed_outer_terrain_grade',manifest.get('terrain_outer_tie_in')=='Fixed outer DGM grade, monotone straight cross-section')
    report['terrain_material_fields']=fields
    report['new_architecture']={key:manifest.get(key) for key in
        ('inferred_paint_families','metric_plinth_surfaces','three_dimensional_downpipes',
         'house_foundation_ground_panels','terrain_foundation_surfaces','terrain_outer_tie_in')}
    # Test stored paint against stored asphalt, not the manifest's intended
    # 1.8-mm lift. An exporter can retain a correct count while burying paint.
    marking_results=[]
    for record in manifest.get('road_marking_records',[]):
        matched=[m for m in model['meshes'] if m['name'].startswith('OBSERVED_MARKING_'+record['id']+'_')]
        scope=record.get('surface_scope',[])
        selected=[m for m in model['meshes'] if
            ('main' in scope and m['name']=='1ROAD_MAIN') or
            ('junction' in scope and m['name'].startswith('JUNCTION_ASPHALT_'))]
        if not matched or not selected:
            marking_results.append({'id':record['id'],'valid':False});continue
        triangles=np.concatenate([m['positions'][m['indices'].reshape(-1,3)] for m in matched]).astype(float)
        asphalt=np.concatenate([m['positions'][m['indices'].reshape(-1,3)] for m in selected])
        # Work in local AC coordinates; the sampler's UTM adapter only needs
        # the corresponding Z sign inversion when the artificial origin is 0.
        ground_sampler=ExportedTerrainSampler(asphalt,[0.,0.],0.)
        samples,coverage=contact_vertices(triangles,ground_sampler)
        lift=samples[:,1]-ground_sampler(np.c_[samples[:,0],-samples[:,2]])
        up=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])[:,1]
        valid=(float(lift.min())>.0017 and float(lift.max())<.0019 and
               coverage['uncovered_area_m2']<1e-5 and bool(np.all(up>1e-8)) and
               all(m['visible'] and m['renderable'] and
                   model['materials'][m['material']]['name']=='observed_white_road_paint' for m in matched))
        marking_results.append({'id':record['id'],'valid':bool(valid),'minimum_lift_mm':round(float(lift.min()*1000),5),
                                'maximum_lift_mm':round(float(lift.max()*1000),5)})
    check('observed_paint_visible_above_actual_asphalt',bool(marking_results) and
          len(marking_results)==manifest.get('road_markings') and all(r['valid'] for r in marking_results))
    report['actual_marking_height_checks']=marking_results


def verify_german_road(manifest,model,check,report):
    """Check exported 0.7 geometry, not only names in a scene JSON document."""
    crown=manifest.get('road_crossfall',{})
    check('crown_typified_not_surveyed',crown.get('measured') is False and
          crown.get('profile')=='symmetric crown' and
          'no local crossfall survey' in crown.get('source','').lower())
    check('crown_declared_two_point_five_percent',math.isclose(crown.get('slope',0),.025,abs_tol=1e-8))
    road=next(mesh for mesh in model['meshes'] if mesh['name']=='1ROAD_MAIN')
    rows=road['positions'].reshape(-1,7,3).astype(float)
    center=rows[:,3]
    distance=np.linalg.norm(rows[:,:,[0,2]]-center[:,None,[0,2]],axis=2)
    drop=center[:,None,1]-rows[:,:,1]
    check('physical_crown_matches_each_road_column',np.allclose(drop,distance*.025,atol=.00008))
    check('physical_crown_center_between_edges',np.allclose(center[:,[0,2]],
          (rows[:,0,[0,2]]+rows[:,-1,[0,2]])/2,atol=.00008))
    # The public manifest intentionally omits centerline_ac. Preservation of
    # that scene reference is checked independently by the container runtime
    # verification, rather than inventing a reference from the exported mesh.
    names=[mesh['name'] for mesh in model['meshes']]
    check('old_periodic_repair_billboards_absent',not any(name.startswith('ASPHALT_REPAIR_') for name in names))
    groups={'manholes':'ROAD_MANHOLE_COVER_', 'manhole_frames':'ROAD_MANHOLE_FRAME_',
            'asphalt_patches':'ROAD_ASPHALT_PATCH_', 'sealed_cracks':'ROAD_RESEALED_CRACK_',
            'stone_drainage':'DE_STONE_DRAINAGE_', 'kerb_units':'DE_KERB_UNITS_',
            'gully_grates':'DE_GULLY_GRATES_', 'pavements':'DE_PAVING_'}
    details=[]; report['street_detail_meshes']={}
    for label,prefix in groups.items():
        matched=[mesh for mesh in model['meshes'] if mesh['name'].startswith(prefix)]
        check(label+'_exported',bool(matched) and all(mesh['triangles']>0 for mesh in matched))
        report['street_detail_meshes'][label]=[mesh['name'] for mesh in matched]
        details.extend(matched)
    # AC's physical surfaces use the numeric prefix. Decorative groups must
    # never acquire that prefix, even when a material name contains "ROAD".
    check('street_details_noncolliding',all(not re.match(r'\d',mesh['name']) for mesh in details))
    check('road_wear_inferred_provenance','not mapped' in manifest.get('road_wear_provenance','') and
          'non-colliding' in manifest.get('road_wear_conformity','') and
          0<manifest.get('road_wear_max_lift_mm',0)<=1.8)
    edge_info=manifest.get('street_edges',{}).get('street_edge_provenance',{})
    check('street_edges_inferred_provenance',edge_info.get('surveyed_street_furniture') is False and
          edge_info.get('collision') is False and bool(edge_info.get('local_material_references')))
    counts=manifest.get('scenery_counts',{})
    check('street_detail_counts',all(manifest.get(key,0)>0 for key in
          ('asphalt_repair_sections','road_resealed_cracks','road_manhole_covers')) and
          all(counts.get(key,0)>0 for key in ('pavement_sections','kerb_sections','drainage_sections','gullies')))
    check('cast_iron_material_embedded','road_cast_iron.png' in model['textures'] and
          'road_cast_iron_normal_dx.png' in model['textures'])
    report['road_crossfall']=crown
    report['physical_crown_max_edge_drop_m']=round(float(drop[:,[0,-1]].max()),5)
    report['street_details']={key:manifest.get(key) for key in
        ('asphalt_repair_sections','road_resealed_cracks','road_manhole_covers','road_wear_area_m2','road_wear_max_lift_mm')}
    report['street_details'].update({key:counts.get(key) for key in
        ('pavement_sections','kerb_sections','drainage_sections','gullies')})
    report['street_edge_provenance']=edge_info
    if release_at_least(manifest.get('version',''),(0,8,0)):
        check('shared_paving_terrain_plan',manifest.get('terrain_contact_surfaces',0)>0 and
              manifest.get('terrain_contact_support_points',0)>0 and
              math.isclose(manifest.get('terrain_contact_clearance_m',0),.035))
        check('paving_uses_exported_ground',edge_info.get('underside_height_source')=='exact exported terrain triangles')
        check('frontage_joints_generated',manifest.get('house_connection_mortar_joints',0)>0)
        check('observed_markings_exported',manifest.get('road_markings',0)>0 and
              any(mesh['name'].startswith('OBSERVED_MARKING_') for mesh in model['meshes']) and
              'observed_road_paint.png' in model['textures'])
        marking_source=manifest.get('road_marking_source',{})
        check('marking_source_and_uncertainty',marking_source.get('provider')=='HVBG Hessen DOP20' and
              marking_source.get('license')=='dl-de/zero-2-0' and
              bool(re.fullmatch(r'[a-f0-9]{64}',marking_source.get('sha256',''))) and
              all(record.get('position_uncertainty_m',0)>0 for record in manifest.get('road_marking_records',[])))
        check('marking_noncollision_and_lift',manifest.get('road_marking_collision') is False and
              0<manifest.get('road_marking_max_lift_mm',0)<=1.8)
        report['frontage_and_markings']={key:manifest.get(key) for key in
            ('terrain_contact_surfaces','terrain_contact_support_points','house_connection_mortar_joints',
             'road_markings','road_marking_area_m2')}


def verify_hillsides_v010(manifest,model,check,report):
    """Check current construction in the downloaded browser-job export."""
    counts=manifest.get('hillside_rendered_surface_counts',{})
    check('hillside_surface_counts_complete',set(counts)=={'terrace','landing','stair','driveway','bank_ledge','footpath'} and
          all(isinstance(v,int) and v>=0 for v in counts.values()))
    check('hillside_planned_and_rendered_counts',
          counts.get('terrace')==manifest.get('hillside_terraces') and
          counts.get('driveway')==manifest.get('hillside_driveways') and
          counts.get('footpath')==manifest.get('hillside_footpaths') and
          counts.get('bank_ledge')==manifest.get('hillside_bank_ledges') and
          sum(counts.values())==manifest.get('hillside_rendered_surfaces') and
          manifest.get('hillside_rendered_walls')==manifest.get('hillside_retaining_walls'))
    stairs=manifest.get('hillside_staircases',-1)
    check('hillside_stair_counts_consistent',isinstance(stairs,int) and stairs>=0 and
          2*stairs<=counts.get('stair',-1)<=12*stairs and counts.get('landing',-1)>=stairs)
    check('hillside_inference_and_road_preservation_disclosed',
          all(word in manifest.get('hillside_provenance','') for word in
              ('Inferred','lowest','not surveyed','coordinates remain unchanged')) and
          manifest.get('hillside_render_collision') is False)
    records=manifest.get('hillside_construction_records',[])
    check('hillside_construction_records_present',len(records)==counts.get('terrace') and
          all(record.get('source_wall') and record.get('id') and
              0<=record.get('maximum_cut_m',-1)<=2.5 and
              0<=record.get('maximum_fill_m',-1)<=1.8 for record in records))
    walls=[m for m in model['meshes'] if m['name'].startswith('HILLSIDE_RETAINING_WALL_')]
    check('hillside_wall_geometry_matches_count',bool(walls)==bool(manifest.get('hillside_retaining_walls')))
    if walls:
        check('hillside_original_masonry_embedded',
              {'hillside_masonry.png','hillside_masonry_normal_dx.png','hillside_cap.png'}.issubset(model['textures']) and
              any(m['name'].startswith('HILLSIDE_WALL_CAP_') for m in model['meshes']))
    details=[m for m in model['meshes'] if m['name'].startswith('HILLSIDE_')]
    check('hillside_details_visible_noncolliding',all(m['visible'] and m['renderable'] and
          not re.match(r'\d',m['name']) for m in details))
    contacts,groups=hillside_contact_checks(model,manifest)
    for name,passed in contacts.items():
        check(name,passed)
    report['hillside_contacts']=groups
    report['hillside_construction']={key:manifest.get(key) for key in
        ('hillside_terraces','hillside_retaining_walls','hillside_retaining_assemblies',
         'hillside_staircases','hillside_driveways','hillside_footpaths','hillside_bank_ledges',
         'hillside_limits','hillside_provenance')}


def verify_building_bases_v011(manifest,model,check,report):
    """Inspect the real downloaded basement meshes, including source seams."""
    from scripts.verify_building_bases import audit_building_bases
    records=manifest.get('building_base_records')
    check('building_base_records_exported',isinstance(records,list))
    check('building_base_inference_and_source_preservation_disclosed',all(
        phrase in manifest.get('building_base_accuracy','') for phrase in
        ('Inferred','original LoD2','exported terrain','not surveyed','coordinates remain unchanged')))
    footprints=manifest.get('building_base_source_footprints')
    check('building_base_source_footprints_exported',isinstance(footprints,list) and
          {record.get('building_id') for record in records}.issubset(
              {entry.get('building_id') for entry in footprints}))
    counts={kind:sum(record.get('kind')==kind for record in records)
            for kind in ('wall','plinth','soffit')}
    stats=manifest.get('building_base_stats',{})
    check('building_base_declared_counts_match_records',
          stats.get('panels')==counts['wall']+counts['plinth'] and
          stats.get('soffits')==counts['soffit'] and
          stats.get('buildings')==len({record.get('building_id') for record in records}))
    gap=stats.get('maximum_gap_m',-1)
    check('building_base_gap_limit_bounded',isinstance(gap,(int,float)) and 0<=gap<=6.)
    checks,groups=audit_building_bases(model,manifest)
    report['building_base_geometry']=groups
    check('building_base_independent_audit_nonempty',bool(checks))
    for name,passed in checks.items():
        check(name,passed)
    meshes=[mesh for mesh in model['meshes'] if mesh['name'].startswith(
        ('BASEMENT_WALLS','BASEMENT_PLINTHS','BASEMENT_SOFFITS'))]
    check('building_base_existing_textures_embedded',all(
        any(sampler=='txDiffuse' and texture in model['textures']
            for sampler,_,texture in model['materials'][mesh['material']]['samplers'])
        for mesh in meshes))
    paint={material['name']:material for material in model['materials']}
    matched=[]
    for material in model['materials']:
        match=re.fullmatch(r'mineral_basement_paint_(\d+)',material['name'])
        if match is None:
            continue
        original=paint.get('village_plain_'+match.group(1))
        matched.append(original is not None and material['samplers']==original['samplers'] and
            all(math.isclose(material['properties'].get(key,[0])[0],
                             original['properties'].get(key,[0])[0]*.86,abs_tol=1e-6)
                for key in ('ksAmbient','ksDiffuse')))
    check('building_base_mineral_paint_reuses_original_plaster',bool(matched) and all(matched))
    report['building_base_record_counts']=counts
    report['building_base_construction']={'stats':stats,'accuracy':manifest.get('building_base_accuracy')}


def verify(base_url,timeout_seconds=900,existing_job=None):
    cfg=json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))
    report={'started_at_utc':datetime.now(timezone.utc).isoformat(),'url':base_url,
            'version':cfg['version'],'passed':False,'checks':{},'transport':'real HTTP; no game or browser automation'}
    started=time.monotonic()
    deadline=started+timeout_seconds
    session=requests.Session()
    session.trust_env=False
    session.headers['Origin']=base_url
    def remaining():
        left=deadline-time.monotonic()
        if left<=0:
            raise TimeoutError('HTTP integration run exceeded its bounded timeout')
        return left
    def check(name,condition):
        report['checks'][name]=bool(condition)
        if not condition:
            raise AssertionError(name)
    def json_request(method,path,**kwargs):
        response=session.request(method,base_url+path,timeout=min(20.,remaining()),**kwargs)
        response.raise_for_status()
        return response.status_code,response.json()
    def download(path,limit):
        chunks=[];size=0
        with session.get(base_url+path,stream=True,timeout=min(20.,remaining())) as response:
            response.raise_for_status()
            if int(response.headers.get('Content-Length','0'))>limit:
                raise ValueError('HTTP artifact exceeds size cap')
            for chunk in response.iter_content(1024*1024):
                remaining();size+=len(chunk)
                if size>limit:
                    raise ValueError('HTTP artifact exceeds size cap')
                chunks.append(chunk)
        return b''.join(chunks)
    try:
        _,health=json_request('GET','/api/health')
        check('health_version',health.get('status')=='ok' and health.get('version')==cfg['version'])
        if release_at_least(cfg['version'],(0,10,0)):
            _,catalog=json_request('GET','/api/catalog')
            template=next(t for t in catalog['templates'] if t['id']=='altenhain')
            check('browser_hillside_capability',template.get('capabilities',{}).get('hillside_connections') is True)
            if release_at_least(cfg['version'],(0,11,0)):
                check('browser_building_base_capability',template.get('capabilities',{}).get('building_basements') is True)
        _,before=json_request('GET','/api/jobs')
        previous_ids={item['id'] for item in before['jobs']}
        settings={'template_id':'altenhain','name':'Altenhain HTTP-Test '+datetime.now(timezone.utc).strftime('%H%M%S'),
                  'start_m':100,'end_m':400,'road_width_m':5.5,'scenery_quality':'high','vegetation_density':.65,
                  'ai_materials':False}
        if existing_job:
            status,job=json_request('GET','/api/jobs/'+existing_job)
            check('existing_job_retrieved',status==200 and job.get('id')==existing_job)
            settings['name']=job['settings']['name']
        else:
            status,job=json_request('POST','/api/jobs',json=settings)
        report['request']=settings
        job_id=job['id'];report['job_id']=job_id
        report['created_by_this_verification']=not bool(existing_job)
        if not existing_job:
            check('job_created',status==202 and re.fullmatch(r'[a-f0-9]{32}',job_id) is not None)
            check('new_isolated_job_id',job_id not in previous_ids and 'rtac_'+job_id!=cfg['id'])
        else:
            check('existing_isolated_job_id',re.fullmatch(r'[a-f0-9]{32}',job_id) is not None and 'rtac_'+job_id!=cfg['id'])
        stages=[];last_stage=None
        while job['status'] in ('queued','running'):
            stage=(job['status'],job.get('stage'),job.get('progress'))
            if stage!=last_stage:
                stages.append({'status':stage[0],'stage':stage[1],'progress':stage[2]})
                print(f"{job_id}: {stage[0]} {stage[2]}% {stage[1]}",flush=True)
                last_stage=stage
            time.sleep(min(2.,remaining()))
            _,job=json_request('GET','/api/jobs/'+job_id)
        report['stages']=stages;report['final_status']=job['status']
        if job['status']!='succeeded':
            report['job_error']=job.get('error')
        check('completed',job['status']=='succeeded' and job.get('progress')==100)
        check('settings_preserved',all(job['settings'].get(k)==v for k,v in settings.items()))
        check('preview_is_render',job['summary'].get('preview_kind')=='render')
        expected_id='rtac_'+job_id
        expected_download=f'/api/jobs/{job_id}/download'
        expected_preview=f'/api/jobs/{job_id}/preview.png'
        check('artifact_urls',job['download_url']==expected_download and job['preview_url']==expected_preview)
        archive=download(expected_download,256_000_000)
        report['zip_bytes']=len(archive);report['zip_sha256']=hashlib.sha256(archive).hexdigest()
        expected_views={'overview':expected_preview,'driver':f'/api/jobs/{job_id}/driver.png',
                        'environment':f'/api/jobs/{job_id}/environment.png'}
        check('three_preview_urls',job.get('preview_urls')==expected_views)
        previews={};report['previews']={}
        for view,url in expected_views.items():
            content=download(url,16_000_000);previews[view]=content
            result={'url':url,'bytes':len(content),'sha256':hashlib.sha256(content).hexdigest()}
            report['previews'][view]=result
            check(view+'_png_signature',content.startswith(b'\x89PNG\r\n\x1a\n'))
            with Image.open(BytesIO(content)) as image:
                check(view+'_png_decodable',image.format=='PNG' and image.width>=640 and image.height>=360)
                result['size']=list(image.size)
                image.verify()
            with Image.open(BytesIO(content)) as image:
                extrema=image.convert('RGB').getextrema()
                check(view+'_has_scene_content',any(high-low>40 for low,high in extrema))
        check('three_distinct_views',len({item['sha256'] for item in report['previews'].values()})==3)
        prefix=f'content/tracks/{expected_id}/'
        with zipfile.ZipFile(BytesIO(archive)) as package:
            names=package.namelist()
            check('zip_integrity',package.testzip() is None)
            check('single_isolated_track',all(name.startswith(prefix) and '..' not in PurePosixPath(name).parts for name in names))
            manifest=json.loads(package.read(prefix+'prototype_manifest.json'))
            check('manifest_identity',manifest['id']==expected_id and manifest['version']==cfg['version'])
            check('selected_length',280<=manifest['length_m']<=301)
            check('selected_width',math.isclose(manifest['road_width_m'],5.5))
            alignment=manifest.get('road_alignment',{})
            check('reviewed_road_alignment_applied',alignment.get('applied') is True and
                  alignment.get('reference_file')=='road_alignment.json' and
                  bool(re.fullmatch(r'[a-f0-9]{64}',alignment.get('reference_sha256',''))))
            check('alignment_uncertainty_disclosed',bool(alignment.get('accuracy')) and
                  0<alignment.get('dop_interpreted_source_length_fraction',0)<1 and
                  alignment.get('observation_counts',{}).get('fallback',0)>0)
            check('terrain_one_metre_near_road',math.isclose(manifest.get('terrain_near_spacing_m',0),1.) and
                  manifest.get('terrain_near_corridor_m',0)>=15 and
                  manifest.get('terrain_grade_blend_m',0)<=2.5)
            check('three_dimensional_entrances_generated',manifest.get('procedural_entrances',0)>0 and
                  manifest.get('three_dimensional_door_components',0)>manifest.get('procedural_entrances',0))
            check('house_ground_transitions_generated',manifest.get('house_ground_apron_panels',0)>0 and
                  manifest.get('house_ground_apron_buildings',0)>0)
            report['road_alignment']={key:alignment.get(key) for key in
                ('applied','source_length_m','corrected_length_m','maximum_lateral_deviation_from_osm_m',
                 'width_range_m','cross_section_count','observation_counts','dop_interpreted_source_length_fraction','accuracy')}
            report['near_terrain_spacing_m']=manifest.get('terrain_near_spacing_m')
            report['procedural_entrances']=manifest.get('procedural_entrances')
            report['three_dimensional_door_components']=manifest.get('three_dimensional_door_components')
            report['house_ground_apron_panels']=manifest.get('house_ground_apron_panels')
            resolution=manifest.get('aerial_resolution_m',[])
            check('native_aerial_20cm',len(resolution)==2 and all(math.isclose(v,.2,abs_tol=1e-6) for v in resolution))
            report['track_length_m']=manifest['length_m']
            for view,content in previews.items():
                filename='preview' if view=='overview' else view
                check(view+'_http_matches_archive',package.read(prefix+'ui/'+filename+'.png')==content)
            credits=package.read(prefix+'CREDITS.txt').decode('utf-8')
            canopy=manifest.get('canopy_height_model',{})
            check('dom_height_provenance',canopy.get('status')=='available' and 'DOM1' in canopy.get('source','') and
                  'DHHN2016' in canopy.get('height_reference','') and 'DOM1' in credits and 'Zero' in credits)
            report['canopy_height_model']=canopy
            counts=manifest.get('scenery_counts',{})
            check('dom_trees_generated',counts.get('dom_trees',0)>0)
            report['dom_trees']=counts.get('dom_trees')
            assets=manifest.get('ground_materials',{}).get('source_records',[])
            check('ground_asset_provenance',{'Asphalt023S','Grass004','Gravel022'}.issubset({a.get('asset') for a in assets})
                  and all(a.get('license')=='CC0-1.0' and a.get('url') and a.get('files') for a in assets))
            kn5_content=package.read(prefix+expected_id+'.kn5')
            model=inspect(MemoryAsset(kn5_content),geometry=True)
            road=next((mesh for mesh in model['meshes'] if mesh['name']=='1ROAD_MAIN'),None)
            check('physical_road_mesh_present',road is not None and road['vertices']%7==0)
            road_positions=road['positions'].reshape(-1,7,3)
            widths=np.linalg.norm(road_positions[:,-1,[0,2]]-road_positions[:,0,[0,2]],axis=1)
            declared=np.asarray(manifest.get('road_width_profile_m',[]),dtype=float)
            check('physical_width_matches_profile',widths.shape==declared.shape and
                  np.allclose(widths,declared,atol=.003))
            check('physical_width_varies',float(np.ptp(widths))>.5 and
                  np.all((widths>3.5)&(widths<7)))
            check('physical_width_range_matches_manifest',np.allclose(
                [widths.min(),widths.max()],manifest.get('road_width_range_m',[]),atol=.005))
            report['physical_road_width_range_m']=[round(float(widths.min()),3),round(float(widths.max()),3)]
            triangles=road['positions'][road['indices'].reshape(-1,3)]
            check('physical_road_no_folded_faces',np.all(np.cross(
                triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])[:,1]>1e-6))
            verify_german_road(manifest,model,check,report)
            check('synthetic_door_atlas_embedded',manifest.get('synthetic_door_atlas') is True and
                  'door_atlas.png' in model['textures'] and
                  manifest.get('door_texture_source',{}).get('type')=='ai_generated_generic_artwork')
            check('door_atlas_used_by_entrance_mesh',any(
                mesh['name'].startswith('LOD2_ENTRANCES') and
                any(texture=='door_atlas.png' for _,_,texture in model['materials'][mesh['material']]['samplers'])
                for mesh in model['meshes']))
            check('door_components_embedded',any(mesh['name'].startswith('LOD2_DOOR_') for mesh in model['meshes']))
            ground=next((m for m in model['materials'] if m['name']=='aerial_ground'),None)
            check('ground_multilayer_shader',ground is not None and ground['shader']=='ksMultilayer')
            samplers={name:texture for name,slot,texture in ground['samplers']}
            expected={'txDiffuse','txMask','txDetailR','txDetailG','txDetailB','txDetailA'}
            check('ground_texture_samplers',expected.issubset(samplers) and all(samplers[k] in model['textures'] for k in expected))
            report['ground_samplers']=samplers
            if release_at_least(manifest['version'],(0,9,0)):
                verify_realism_v09(manifest,model,kn5_content,check,report)
            if release_at_least(manifest['version'],(0,10,0)):
                verify_hillsides_v010(manifest,model,check,report)
            if release_at_least(manifest['version'],(0,11,0)):
                verify_building_bases_v011(manifest,model,check,report)
            check('photographic_foliage_alpha',any(m['shader']=='ksPerPixelAT' and m['alpha']==1 for m in model['materials']))
            report['material_count']=len(model['materials']);report['mesh_count']=len(model['meshes'])
        _,after=json_request('GET','/api/jobs')
        after_ids={item['id'] for item in after['jobs']}
        for old_id in previous_ids-after_ids:
            # The listing is capped at 100; an older job may simply move off page.
            _,old_job=json_request('GET','/api/jobs/'+old_id)
            check('previous_job_'+old_id,old_job.get('id')==old_id)
        check('history_preserved',job_id in after_ids)
        report['passed']=all(report['checks'].values())
    except Exception as error:
        report['error']=f'{type(error).__name__}: {error}'
    finally:
        session.close()
        report['elapsed_seconds']=round(time.monotonic()-started,2)
        report['finished_at_utc']=datetime.now(timezone.utc).isoformat()
    return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--run-build',action='store_true',help='Explicitly create one real build job')
    mode.add_argument('--existing-job',help='Verify an already created job without starting another build')
    parser.add_argument('--url',default='http://127.0.0.1:8765')
    parser.add_argument('--timeout-seconds',type=int,default=900)
    version=json.loads((ROOT/'tracks/altenhain.json').read_text(encoding='utf-8'))['version']
    tag='v'+''.join(version.split('.')[:2])
    parser.add_argument('--report',type=Path,default=ROOT/'test-results'/f'web-integration-{tag}.json')
    args=parser.parse_args()
    if args.existing_job and not re.fullmatch(r'[a-f0-9]{32}',args.existing_job):
        parser.error('--existing-job must be a 32-character job ID')
    parsed=urlsplit(args.url)
    if parsed.scheme!='http' or parsed.hostname not in {'localhost','127.0.0.1','::1'} or parsed.path not in {'','/'} or parsed.query or parsed.fragment:
        parser.error('--url must be a local HTTP server origin')
    if not 1<=args.timeout_seconds<=900:
        parser.error('--timeout-seconds must be between 1 and 900')
    report=verify(args.url.rstrip('/'),args.timeout_seconds,args.existing_job)
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,indent=2,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'report':str(args.report),'job_id':report.get('job_id'),'passed':report['passed'],
                      'error':report.get('error'),'checks':report['checks']},indent=2,ensure_ascii=False))
    raise SystemExit(0 if report['passed'] else 1)


if __name__=='__main__':
    main()
