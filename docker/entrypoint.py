"""Validate bundled data and writable state, then start the HTTP server."""
from pathlib import Path
import json
import os
import sys
import tempfile


def main():
    root=Path('/app')
    config=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
    source=root/config['data_dir']
    required=('road_ways.json','bounds.json','manifest.json','area.osm','dgm1.tif',
              'ortho.tif','buildings_lod2.gml','textures/ortho.jpg',
              'materials/PaintedPlaster006','materials/surface_photos',
              'materials/PolyHavenRoofs','materials/vegetation_photos')
    missing=[name for name in required if not (source/name).exists()]
    if missing:
        raise SystemExit('Bundled geographic input is missing from /app/data. If using a custom data mount, it must contain the complete prepared dataset. Missing: '+', '.join(missing))
    arguments=sys.argv[1:]
    data=Path(os.environ.get('ROADTOASSETTO_DATA_DIR','/data'))
    if '--data-dir' in arguments:
        index=arguments.index('--data-dir')
        if index+1>=len(arguments):
            raise SystemExit('--data-dir requires a path')
        data=Path(arguments[index+1])
    try:
        data.mkdir(parents=True,exist_ok=True)
        for name in ('home','cache','ai-cache'):
            (data/name).mkdir(exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=data,prefix='.write-check-',delete=True) as stream:
            stream.write(b'container write check')
    except OSError as error:
        raise SystemExit(f'Persistent data volume is not writable by uid {os.getuid()}, gid {os.getgid()}. Set the host appdata directory ownership to this uid/gid (Unraid template: 99:100; default image user: 10001:10001). {error}') from error
    os.environ['ROADTOASSETTO_DATA_DIR']=str(data)
    os.execvp(sys.executable,[sys.executable,'-m','webapp.server',*arguments])


if __name__=='__main__':
    main()
