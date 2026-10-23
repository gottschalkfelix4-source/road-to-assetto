"""Prepare or restore a temporary local practice session, with byte-exact backups."""
from pathlib import Path
import argparse
import configparser
import json

root=Path(__file__).resolve().parents[1]
cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
docs=Path.home()/'Documents/Assetto Corsa/cfg'
backup=root/'test-results/local-backup/session'
p=argparse.ArgumentParser()
p.add_argument('action',choices=['prepare','restore'])
args=p.parse_args()
targets=['race.ini','video.ini']
if args.action=='restore':
    if not backup.exists():
        raise SystemExit('No saved session')
    for name in targets:
        (docs/name).write_bytes((backup/name).read_bytes())
    print('Original race and video settings restored')
else:
    backup.mkdir(parents=True,exist_ok=True)
    for name in targets:
        dest=backup/name
        if not dest.exists():
            dest.write_bytes((docs/name).read_bytes())
    race=configparser.ConfigParser(interpolation=None,strict=False)
    race.optionxform=str
    race.read(docs/'race.ini',encoding='utf-8-sig')
    updates={'RACE':{'TRACK':cfg['id'],'CONFIG_TRACK':'','MODEL':'abarth500','CARS':'1','PENALTIES':'0'},
             'CAR_0':{'MODEL':'-','MODEL_CONFIG':'','SKIN':'0_white_scorpion','DRIVER_NAME':'Prototype Test'},
             'SESSION_0':{'NAME':'Practice','TYPE':'1','DURATION_MINUTES':'0','SPAWN_SET':'PIT'},
             'GHOST_CAR':{'RECORDING':'0','PLAYING':'0'},'REMOTE':{'ACTIVE':'0'},'REPLAY':{'ACTIVE':'0'},
             'LIGHTING':{'SUN_ANGLE':'16','TIME_MULT':'1'},'WEATHER':{'NAME':'3_clear'}}
    for section,values in updates.items():
        if section not in race:
            race[section]={}
        race[section].update(values)
    with (docs/'race.ini').open('w',encoding='utf-8') as f:
        race.write(f,space_around_delimiters=False)
    video=configparser.ConfigParser(interpolation=None,strict=False)
    video.optionxform=str
    video.read(docs/'video.ini',encoding='utf-8-sig')
    video['VIDEO'].update({'FULLSCREEN':'0','WIDTH':'1280','HEIGHT':'720'})
    with (docs/'video.ini').open('w',encoding='utf-8') as f:
        video.write(f,space_around_delimiters=False)
    print('Temporary 1280x720 practice session prepared. Backups:',backup)
