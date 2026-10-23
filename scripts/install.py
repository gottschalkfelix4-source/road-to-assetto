"""Install only this generated track. Refuse to overwrite an unrelated folder."""
from pathlib import Path
import argparse
import json
import shutil

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser()
p.add_argument('--ac-dir',type=Path,required=True)
args=p.parse_args()
cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
source=root/'build'/cfg['id']
destination=args.ac_dir/'content/tracks'/cfg['id']
if not (args.ac_dir/'acs.exe').is_file():
    raise SystemExit('This does not appear to be an Assetto Corsa installation')
if destination.exists():
    manifest=destination/'prototype_manifest.json'
    if not manifest.is_file() or json.loads(manifest.read_text(encoding='utf-8')).get('id')!=cfg['id']:
        raise SystemExit('Refusing to overwrite an unrelated track')
    backup=root/'test-results/local-backup'/('installed-'+__import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S'))
    shutil.copytree(destination,backup)
    print('Previous prototype backed up:',backup)
shutil.copytree(source,destination,dirs_exist_ok=True)
print('Installed:',destination)
