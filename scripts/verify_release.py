"""Compare the installed prototype byte-for-byte with the release archive."""
from pathlib import Path, PurePosixPath
import argparse
import hashlib
import json
import zipfile

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser()
p.add_argument('--ac-dir',type=Path,required=True)
args=p.parse_args()
cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
track=root/'build'/cfg['id']
installed=args.ac_dir/'content/tracks'/cfg['id']
archive=root/'dist'/(cfg['id']+'-'+cfg['version']+'.zip')
checks={}
with zipfile.ZipFile(archive) as z:
    checks['zip_integrity']=z.testzip() is None
    for file in sorted(track.rglob('*')):
        if file.is_file():
            relative=file.relative_to(track)
            name='content/tracks/'+cfg['id']+'/'+relative.as_posix()
            actual=installed/relative
            checks['installed:'+relative.as_posix()]=actual.is_file() and actual.read_bytes()==file.read_bytes()
            checks['archive:'+relative.as_posix()]=name in z.namelist() and z.read(name)==file.read_bytes()
    checks['zip_paths']=all(not PurePosixPath(n).is_absolute() and '..' not in PurePosixPath(n).parts for n in z.namelist())
for name in ('geometry','in-game-load','physics-smoke'):
    report=json.loads((root/'test-results'/(name+'.json')).read_text(encoding='utf-8'))
    checks['test:'+name]=report['passed'] and report.get('version',report.get('metadata',{}).get('version'))==cfg['version']
files=[archive,track/(cfg['id']+'.kn5'),root/'dist'/(cfg['id']+'-source')/'altenhain_langstrasse.blend']
hashes={f.relative_to(root).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
(root/'test-results/artifact-hashes.json').write_text(json.dumps(hashes,indent=2),encoding='utf-8')
report={'version':cfg['version'],'passed':all(checks.values()),'checks':checks}
(root/'test-results/release.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(f'{sum(checks.values())}/{len(checks)} release checks passed')
raise SystemExit(0 if report['passed'] else 1)
