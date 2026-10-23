"""Verify downloadable artifacts without starting or inspecting the game."""
from pathlib import Path, PurePosixPath
import hashlib
import json
import zipfile
from PIL import Image

ROOT=Path(__file__).resolve().parents[1]


def sha256(path):
    digest=hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):
            digest.update(block)
    return digest.hexdigest()


def verify(root=ROOT):
    cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
    track=root/'build'/cfg['id']
    release=root/'dist'/f"{cfg['id']}-{cfg['version']}.zip"
    source=root/'dist'/f"{cfg['id']}-{cfg['version']}-source.zip"
    checks={}
    with zipfile.ZipFile(release) as archive:
        checks['track_zip_integrity']=archive.testzip() is None
        names=set(archive.namelist())
        for file in sorted(track.rglob('*')):
            if file.is_file():
                relative=file.relative_to(track).as_posix()
                member='content/tracks/'+cfg['id']+'/'+relative
                checks['track:'+relative]=member in names and archive.read(member)==file.read_bytes()
        checks['track_paths']=all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in names)
    views=[track/'ui'/name for name in ('preview.png','driver.png','environment.png')]
    for file in views:
        with Image.open(file) as image:
            image.load()
            checks['image:'+file.name]=image.format=='PNG' and image.size==(1280,720)
    checks['distinct_camera_views']=len({sha256(path) for path in views})==3
    with zipfile.ZipFile(source) as archive:
        checks['source_zip_integrity']=archive.testzip() is None
        names=set(archive.namelist())
        required=['Dockerfile','compose.yaml','.dockerignore','DOCKER.md',
                  'docker/entrypoint.py','webapp/ai.py','roadtoassetto/ai_materials.py',
                  'roadtoassetto/german_streets.py','roadtoassetto/road_wear.py',
                  'roadtoassetto/junction_mouths.py','research/german-streets-v07.md',
                  'roadtoassetto/road_markings.py','research/markings-v08.md',
                  'roadtoassetto/hillside.py','roadtoassetto/hillside_render.py','roadtoassetto/hillside_banks.py',
                  'scripts/test_hillside.py','data/altenhain/hillside_plan.json',
                  'roadtoassetto/building_bases.py','scripts/test_building_bases.py',
                  'scripts/verify_building_bases.py','scripts/test_building_bases_audit.py',
                  'data/altenhain/building_bases.json',
                  'data/altenhain/marking_observations.json',
                  'data/altenhain/junction_observations.json',
                  'build/'+cfg['id']+'/ui/preview.png',
                  'data/altenhain/dgm1.tif','data/altenhain/dom1.tif','data/altenhain/ortho.tif',
                  'data/altenhain/road_alignment.json',
                  'data/altenhain/materials/AIDoors/door_atlas.png',
                  'data/altenhain/materials/AIDoors/prompt.txt',
                  'data/altenhain/materials/AIDoors/provenance.json',
                  'data/altenhain/materials/AIWindows/window_atlas.png',
                  'data/altenhain/materials/AIWindows/prompt.txt',
                  'data/altenhain/materials/AIWindows/provenance.json',
                  'altenhain_langstrasse.blend']
        for name in required:
            checks['source:'+name]='road-to-assetto/'+name in names
        checks['source_paths']=all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in names)
        private_parts={'web-data','ai-cache','ai-settings.json','.env','.git','.tools','local-backup'}
        checks['no_private_runtime_files']=not any(private_parts.intersection(PurePosixPath(n).parts) for n in names)
    geometry=json.loads((root/'test-results/geometry.json').read_text(encoding='utf-8'))
    tag='v'+''.join(cfg['version'].split('.')[:2])
    docker=json.loads((root/'test-results'/f'docker-web-integration-{tag}.json').read_text(encoding='utf-8'))
    checks['current_geometry_passed']=geometry['passed'] and geometry['metadata']['version']==cfg['version']
    checks['docker_generation_passed']=docker['passed'] and docker.get('version')==cfg['version']
    if tuple(int(part) for part in cfg['version'].split('.')) >= (0,8,0):
        contacts=json.loads((root/'test-results'/f'street-contacts-{tag}.json').read_text(encoding='utf-8'))
        checks['exported_paving_terrain_contacts_passed']=contacts['passed'] and contacts['version']==cfg['version']
        docker_contacts=json.loads((root/'test-results'/f'docker-street-contacts-{tag}.json').read_text(encoding='utf-8'))
        checks['docker_paving_terrain_contacts_passed']=docker_contacts['passed'] and docker_contacts['version']==cfg['version']
    if tuple(int(part) for part in cfg['version'].split('.')) >= (0,11,0):
        for name in ('building-bases','local-web-bases','docker-building-bases'):
            audit=json.loads((root/'test-results'/f'{name}-{tag}.json').read_text(encoding='utf-8'))
            checks[name+'_passed']=audit['passed'] and audit['version']==cfg['version']
    report={'version':cfg['version'],'passed':all(checks.values()),'checks':checks,
            'game_test_performed':False,'preview_renderer':'Blender 4.5.9 Cycles, actual exported scene',
            'live_ai_provider_tested':False,
            'sha256':{p.relative_to(root).as_posix():sha256(p) for p in (release,source,track/(cfg['id']+'.kn5'),*views)}}
    destination=root/'test-results'/f'visual-release-{tag}.json'
    destination.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(f'{sum(checks.values())}/{len(checks)} visual release checks passed')
    for name,passed in checks.items():
        if not passed:print('FAIL:',name)
    return report['passed']


if __name__=='__main__':
    raise SystemExit(0 if verify() else 1)
