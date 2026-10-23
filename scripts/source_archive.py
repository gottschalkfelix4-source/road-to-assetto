"""Package generator/web sources and cached data, excluding local job state."""
from pathlib import Path
import json
import zipfile

EXCLUDED_PARTS={'__pycache__','local-backup','web-data','.tools','.venv','.git',
                'Facade004','Facade005','Facade009'}

# Research is opt-in: keep the source and licensing evidence actually used by
# v0.4, never local SDK inspection output or proprietary shader disassembly.
PUBLIC_RESEARCH=(
    'research/realism-v04.md',
    'research/german-streets-v07.md',
    'research/markings-v08.md',
    'research/local_facades/Altenhain_Langstrasse_39.JPG',
    'research/local_facades/Altenhain_Langstrasse_13_Vorderbau.JPG',
    'research/local_facades/photo13-review.json',
    'research/local_facades/commons_metadata.json',
    'research/local_facades/sources.json',
    'research/local_facades/building_matches.json',
)


def source_files(root, cfg):
    root=Path(root).resolve()
    files=[root/name for name in ('README.md','requirements.txt','THIRD_PARTY.md',
                                  'TESTEN.txt','LICENSE','Start-Webinterface.cmd',
                                  'Dockerfile','compose.yaml','.dockerignore','DOCKER.md')]
    for folder in ('roadtoassetto','webapp','scripts','tracks','licenses','docker',cfg['data_dir']):
        files.extend(p for p in (root/folder).rglob('*') if p.is_file())
    files.extend(root/name for name in PUBLIC_RESEARCH)
    files.extend((root/'research/markings-v08').glob('*.png'))
    files.extend((root/'research/markings-v08').glob('*.json'))
    # The container's initial catalogue preview is a small build input too.
    files.append(root/'build'/cfg['id']/'ui/preview.png')
    # Include reviewable reports/images, not temporary test projects or private
    # setting/installed-track backups nested beneath test-results.
    files.extend(p for p in (root/'test-results').glob('*')
                 if p.is_file() and p.suffix.lower() in ('.json','.png','.jpg','.jpeg')
                 and p.name!='settings-restored.json'
                 # Host IPC repairs and pre-update snapshots are local
                 # diagnostics, not distributable generator evidence.
                 and not p.name.startswith(('docker-before-', 'docker-start-recovery-',
                                            'docker-web-initial-')))
    return sorted({p for p in files if p.is_file() and p.resolve().is_relative_to(root)
                   and not EXCLUDED_PARTS.intersection(p.relative_to(root).parts)
                   and p.suffix.lower() not in ('.pyc','.pyo','.blend1')})


def create_source_archive(root):
    root=Path(root).resolve()
    cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
    path=root/'dist'/(cfg['id']+'-'+cfg['version']+'-source.zip')
    path.parent.mkdir(parents=True,exist_ok=True)
    blend=root/'dist'/(cfg['id']+'-source')/'altenhain_langstrasse.blend'
    scene=root/cfg['data_dir']/'scene.json'
    # Blender is optional. Do not label a leftover older .blend as the current
    # source when the scene has subsequently been rebuilt without rendering.
    include_blend=blend.is_file() and (not scene.is_file() or blend.stat().st_mtime>=scene.stat().st_mtime)
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in source_files(root,cfg):
            z.write(file,'road-to-assetto/'+file.relative_to(root).as_posix())
        if include_blend:
            z.write(blend,'road-to-assetto/altenhain_langstrasse.blend')
    print(path)
    if not include_blend:
        print('No current optional Blender source included; it can be recreated with scripts/blender_source.py.')
    return path


if __name__=='__main__':
    create_source_archive(Path(__file__).resolve().parents[1])
