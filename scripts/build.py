"""Build an installable track without requiring Blender for the export."""
from pathlib import Path
import argparse
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.prepare import prepare
from roadtoassetto.kn5 import write
from roadtoassetto.package import package, archive
from roadtoassetto.inspect_kn5 import inspect

p=argparse.ArgumentParser()
p.add_argument('--config',default='tracks/altenhain.json')
p.add_argument('--reuse-scene',action='store_true')
args=p.parse_args()
cfg=json.loads((ROOT/args.config).read_text(encoding='utf-8'))
scene=json.loads((ROOT/cfg['data_dir']/'scene.json').read_text()) if args.reuse_scene else prepare(ROOT,cfg)
track=package(ROOT,cfg,scene)
write(track/(cfg['id']+'.kn5'),scene,ROOT/cfg['data_dir']/'textures')
info=inspect(track/(cfg['id']+'.kn5'))
print('KN5 verified:',len(info['meshes']),'meshes,',len(info['markers']),'markers,',info['bytes'],'bytes')
print('ZIP:',archive(ROOT,cfg,track))
