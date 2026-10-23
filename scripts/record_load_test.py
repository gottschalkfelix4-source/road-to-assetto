"""Save prototype log evidence after closing the simulation to flush its log."""
from pathlib import Path
import json

root=Path(__file__).resolve().parents[1]
cfg=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
log=(Path.home()/'Documents/Assetto Corsa/logs/log.txt').read_text(encoding='utf-8',errors='replace')
lines=log.splitlines()
relevant=[line for line in lines if (cfg['id'] in line or line=='STARTING PHYSICS ENGINE')
          and not line.startswith('Saving replay:')]
expected_warnings=[line for line in relevant if line=='ERROR: NO STAT FOR '+cfg['id']]
errors=[line for line in relevant if 'ERROR:' in line and line not in expected_warnings]
loaded=any('LOADING MODEL content/tracks/'+cfg['id'] in line for line in lines)
physics='STARTING PHYSICS ENGINE' in lines
report={'passed':loaded and physics and not errors,'track':cfg['id'],'version':cfg['version'],
        'track_model_loaded':loaded,'physics_engine_started':physics,'prototype_ini_errors':errors,
        'expected_session_statistics_warnings':expected_warnings,
        'evidence':relevant,
        'expected_limitations':['No AI or pit spline; no lap timing/position data',
                                'Other warnings from existing base-game apps/cars are outside this track build']}
(root/'test-results/in-game-load.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
print(json.dumps(report,indent=2))
raise SystemExit(0 if report['passed'] else 1)
