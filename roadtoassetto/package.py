from __future__ import annotations
from pathlib import Path
import hashlib
import json
import shutil
import zipfile
from PIL import Image, ImageDraw
import numpy as np


def write_track_map(track, centerline, max_size=4096):
    """Bound raster memory while preserving AC's world-to-map transform.

    SCALE_FACTOR is metres per pixel; offsets remain world metres, matching
    Content Manager's TrackMapRenderer.TrackMapInformation convention.
    """
    center=np.asarray(centerline,dtype=float)[:,[0,2]]
    if len(center)<2 or not np.isfinite(center).all():
        raise ValueError('Invalid track map centerline')
    margin=20
    span=np.maximum(center.max(axis=0)-center.min(axis=0),1.)
    scale=min(1.,(max_size-2*margin)/float(span.max()))
    lo=center.min(axis=0)-margin/scale
    width,height=np.ceil(span*scale+2*margin).astype(int)
    image=Image.new('RGBA',(int(width),int(height)),(0,0,0,0))
    draw=ImageDraw.Draw(image)
    path=[tuple((p-lo)*scale) for p in center]
    draw.line(path,fill=(0,0,0,200),width=9,joint='curve')
    draw.line(path,fill='white',width=5,joint='curve')
    image.save(track/'map.png')
    image.thumbnail((512,512))
    outline=Image.new('RGBA',(512,512),(0,0,0,0))
    outline.alpha_composite(image,((512-image.width)//2,(512-image.height)//2))
    outline.save(track/'ui/outline.png')
    (track/'data/map.ini').write_text(f'[PARAMETERS]\nWIDTH={width}\nHEIGHT={height}\nMARGIN={margin}\nSCALE_FACTOR={1/scale:.9f}\nMAX_SIZE={max_size}\nX_OFFSET={-lo[0]:.9f}\nZ_OFFSET={-lo[1]:.9f}\nDRAWING_SIZE=5\n',encoding='utf-8')


def route_provenance(root, config, metadata, track):
    """Ship the route's small source records, never the surrounding job/cache."""
    route = metadata.get('source_route') or config.get('source_route')
    if not route:
        return None
    source = (root / config['data_dir']).resolve()
    names = ('area.osm', 'road_ways.json', 'bounds.json', 'retrieval.json')
    for name in names:
        path = source / name
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError('New route provenance is missing or unsafe: ' + name)
    record = json.loads((source / 'retrieval.json').read_text(encoding='utf-8'))
    # A fixed schema prevents a future internal cache path or provider diagnostic
    # from silently becoming part of a friend's exported track.
    source_fields = ('provider', 'license', 'url', 'coverage_id', 'bounds', 'crs',
                     'resolution_m', 'width', 'height', 'bytes', 'sha256',
                     'recorded_at_utc', 'building_count', 'pages')
    retrieval = {key: record[key] for key in ('recorded_at_utc', 'route_id', 'warnings') if key in record}
    retrieval['sources'] = {
        kind: {key: value[key] for key in source_fields if key in value}
        for kind, value in record.get('sources', {}).items()
        if kind in ('dgm1', 'dop20', 'lod2', 'dom1') and isinstance(value, dict)}
    retrieval['osm'] = {key: record.get('osm', {})[key]
                        for key in ('provider', 'license', 'url', 'source_way_ids')
                        if key in record.get('osm', {})}
    public_route = {key: route[key] for key in ('id', 'name', 'length_m', 'coordinates',
                    'bounds', 'source', 'warnings') if key in route}
    public_route['sources'] = retrieval['sources']
    public_route['source_way_ids'] = retrieval['osm'].get('source_way_ids', [])
    public_route['retrieved_at_utc'] = retrieval.get('recorded_at_utc')
    destination = track / 'provenance'
    destination.mkdir(exist_ok=True)
    for name in names[:-1]:
        shutil.copy2(source / name, destination / name)
    for name, value in (('retrieval.json', retrieval), ('route.json', public_route)):
        (destination / name).write_text(json.dumps(value, ensure_ascii=False,
                                                  allow_nan=False, indent=2), encoding='utf-8')
    files = {}
    for path in sorted(destination.iterdir()):
        if path.is_file():
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            files['provenance/' + path.name] = {'bytes': path.stat().st_size, 'sha256': digest}
    metadata['source_route'] = public_route
    metadata['provenance_files'] = files
    return retrieval


def package(root, config, scene):
    track=root/'build'/config['id']
    for folder in ('data','ui'):
        (track/folder).mkdir(parents=True,exist_ok=True)
    meta=dict(scene['metadata'])
    retrieval=route_provenance(root,config,meta,track)
    new_route=retrieval is not None
    surfaces=[]
    # The v0.7 edge strip is paved rather than loose gravel. Friction remains
    # a prototype estimate, not an independently measured local tyre surface.
    paved_edge=meta.get('road_crossfall') is not None
    for i,(key,friction,valid,dirt) in enumerate([('ROAD',1.,1,0),('SHOULDER',.95 if paved_edge else .87,1,0 if paved_edge else .05),('GRASS',.65,0,.2),('WALL',.7,0,0)]):
        surfaces.append(f'''[SURFACE_{i}]
KEY={key}
FRICTION={friction}
DAMPING=0
WAV=
WAV_PITCH=0
FF_EFFECT=NULL
DIRT_ADDITIVE={dirt}
IS_VALID_TRACK={valid}
BLACK_FLAG_TIME=0
SIN_HEIGHT=0
SIN_LENGTH=0
IS_PITLANE=0
VIBRATION_GAIN=0
VIBRATION_LENGTH=0
''')
    (track/'data/surfaces.ini').write_text('\n'.join(surfaces),encoding='utf-8')
    models=meta.get('models') or [{'file':config['id']+'.kn5'}]
    for model in models:
        if not isinstance(model.get('file'),str) or Path(model['file']).name!=model['file'] or not model['file'].endswith('.kn5'):
            raise ValueError('Invalid packaged model filename')
    (track/'models.ini').write_text('\n'.join(f'[MODEL_{index}]\nFILE={model["file"]}\nPOSITION=0,0,0\nROTATION=0,0,0\n' for index,model in enumerate(models)),encoding='utf-8')
    (track/'data/lighting.ini').write_text('[LIGHTING]\nSUN_PITCH_ANGLE=50\nSUN_HEADING_ANGLE=16\n',encoding='utf-8')
    (track/'data/crew.ini').write_text('[HEADER]\nSIDE=1\n',encoding='utf-8')
    (track/'data/groove.ini').write_text('[HEADER]\nGROOVES_NUMBER=0\n',encoding='utf-8')
    rise=meta['elevation_max_m']-meta['elevation_min_m']
    ui={'name':config['name'],'description':f'{config["city"]}. {meta["length_m"]:.0f} m mit rund {rise:.0f} m Hoehenunterschied. Prototyp fuer freies Training.',
        'tags':['real road','prototype','point to point','Germany'], 'geotags':config.get('geotags',['50.1588','8.4680']),
        'country':config['country'],'city':config['city'],'length':str(round(meta['length_m'])),
        'width':str(config['road_width_m']),'pitboxes':str(config['pitboxes']),'run':'practice',
        'author':'Road to Assetto - private prototype','version':config['version']}
    (track/'ui/ui_track.json').write_text(json.dumps(ui,indent=2),encoding='utf-8')
    if meta.get('road_alignment',{}).get('applied'):
        low,high=meta['road_width_range_m']
        ui['width']=f'{low:.1f}-{high:.1f}'
        (track/'ui/ui_track.json').write_text(json.dumps(ui,indent=2),encoding='utf-8')
    write_track_map(track,meta['centerline_ac'])
    source_notice=('OSM extract, route way IDs, geographic bounds and retrieval provenance are included\n'
                   'in this track under provenance/; see prototype_manifest.json for file hashes.'
                   if new_route else 'OSM extracts and route way IDs are included in the source package.')
    retrieval_notice=(f'Data retrieval recorded at {retrieval.get("recorded_at_utc", "an unspecified time")}.\n'
                      'See provenance/retrieval.json for the sources actually used; DOM1 is optional.'
                      if new_route else 'Data retrieved from inspire-hessen.de via WCS and WFS on 2026-10-21.')
    base_notice=('Basement planning records are included in prototype_manifest.json.' if new_route else
                 'The source archive includes the building_bases.json plan and independent exported-geometry checks.')
    credits=f'''Road to Assetto - {config['name']} prototype

Road centerline: (c) OpenStreetMap contributors
https://www.openstreetmap.org/copyright - ODbL 1.0
{source_notice}

Terrain DGM1 / surface model DOM1 / aerial imagery DOP20 / buildings LoD2: Hessische Verwaltung fuer Bodenmanagement
und Geoinformation (HVBG), https://hvbg.hessen.de/geoinformation/open-data
Datenlizenz Deutschland - Zero - Version 2.0.
{retrieval_notice}

The road surface has been smoothed and road width is estimated.
This is not a laser-scanned or survey-accurate road model.
LoD2 building heights/roof shapes come from the official dataset. Nearby pitched
roofs use generic CC0 tile scans selected by aerial colour; other roofs use DOP20.
Facades, windows and architectural details are approximate unless an individual
facade photograph is explicitly attributed below.

Facade plaster base: PaintedPlaster006, ambientCG / Lennart Demes, CC0 1.0.
https://ambientcg.com/view?id=PaintedPlaster006
https://docs.ambientcg.com/license/
Windows/shutters and facade color variations: original prototype graphics.
Asphalt023S (photogrammetry), Grass004 and Gravel022: ambientCG, CC0 1.0.
https://ambientcg.com/view?id=Asphalt023S
https://ambientcg.com/view?id=Grass004
https://ambientcg.com/view?id=Gravel022
Roof and vegetation photographs: Poly Haven, CC0.
https://polyhaven.com/a/roof_tiles
https://polyhaven.com/a/grey_roof_01
https://polyhaven.com/a/tree_small_02
https://polyhaven.com/a/tree_bark_03
https://polyhaven.com/license
Material provenance is recorded in prototype_manifest.json. Original material
sources and hashes are also available in the project's versioned dataset:
https://github.com/gottschalkfelix4-source/road-to-assetto/blob/main/docker/dataset.json
Where available, tree height and canopy locations use DOM1 minus DGM1 and aerial
vegetation checks; species and individual crown shapes remain approximate.
Vegetation, pavement, repairs and architectural details are estimated scenery,
not a survey of individual trees, pavements or building features.
No Google Street View, Google Maps or Google Earth assets are included.
Basement and plinth shells are inferred between original LoD2 lower exterior
edges and the exported terrain. Existing source ground faces supply separate
underside closures. These are visual additions, not surveyed basement layouts;
original wall/roof coordinates stay unchanged. {base_notice}

Static KN5 binary layout reference:
KinG-InFeT/blender-assetto-corsa-track-generator (Apache-2.0).
See THIRD_PARTY.md in the source project.
'''
    for photo in meta.get('photo_sources',[]):
        credits+=f'\nFacade photo {photo["building_id"]}/{photo["surface_id"]}: {photo["source"]}; license: {photo["license"]}\n'
    if meta.get('window_texture_source'):
        credits+='\nWindow interior atlas: AI-generated generic artwork using built-in OpenAI ImageGen on 2026-10-21. Not photographs of these houses. Source hash is recorded in prototype_manifest.json; the original prompt is in the versioned material dataset.\n'
    if meta.get('door_texture_source'):
        credits+='\nDoor leaf atlas: AI-generated generic artwork using built-in OpenAI ImageGen on 2026-10-21. Not photographs of these houses. Source hash is recorded in prototype_manifest.json; the original atlas and prompt are in the versioned material dataset.\n'
    if meta.get('road_alignment',{}).get('applied'):
        credits+='\nRoad centreline and changing widths manually interpreted from DOP20; OSM retained in obscured sections. Local edge uncertainty is approximately 0.5-0.8 m and 1.5 m at the reviewed junction, not survey accuracy. Reference control points are included in road_alignment.json in the source package.\n'
    if meta.get('road_crossfall'):
        pavement_notice=('Pavement materials are generic inferred scenery, not photographic records of this street.' if new_route else
                         'Selected pavement materials reference historical local photographs; see research/german-streets-v07.md in the source project.')
        credits+=f'\nGerman street construction details: inferred kerbs, drainage strips, pavement modules, gullies, manholes and asphalt repairs. Sidewalk sides follow OSM tags. {pavement_notice} The {100*meta["road_crossfall"]["slope"]:g} percent drainage crown is a typical construction assumption, not a local measurement. Short side-street asphalt mouths are visual scenery, not additional driving routes. See prototype_manifest.json for evidence and limitations.\n'
    if meta.get('road_marking_source'):
        credits+='\nIndividual white markings were traced from visible HVBG DOP20 aerial imagery. Positions, paint widths and wear are approximate; the raster acquisition date is unknown. No continuous centreline is inferred. Original observations and uncertainty are in marking_observations.json; visual evidence in research/markings-v08/.\n'
    if meta.get('street_ground_contact_sections',0) or meta.get('ground_transition_accuracy'):
        credits+='\nSidewalk, house-front paving and their immediate ground tie-ins share planned heights. Frontage extents, joints and edging are inferred from road and LoD2 building geometry, not cadastral or surveyed construction detail. See prototype_manifest.json.\n'
    if meta.get('hillside_provenance'):
        hillside_notice=('See prototype_manifest.json for planning records.' if new_route else
                         'See hillside_plan.json in the source package and prototype_manifest.json.')
        credits+='\nHillside terraces, retaining walls, stepped gravel banks, stairs, footpaths and suitable driveways are inferred from source heights. They share a terrain-construction plan but are not surveyed property boundaries or observed access arrangements. Masonry, coping and handrail materials are original procedural assets; gravel uses the existing attributed CC0 material. New construction details are visual scenery, not separate collision objects. '+hillside_notice+'\n'
    if meta.get('ai_materials',{}).get('enabled'):
        credits+='\nOptional AI material pack applied: synthetic plaster, red roof tiles and asphalt. Generated using the user-configured image provider; provider terms apply. Provenance is recorded in prototype_manifest.json. Not survey or facade photographs.\n'
    (track/'CREDITS.txt').write_text(credits,encoding='utf-8')
    (track/'prototype_manifest.json').write_text(json.dumps({k:v for k,v in meta.items() if k!='centerline_ac'},indent=2,ensure_ascii=False),encoding='utf-8')
    return track


def archive(root, config, track, progress=None):
    dist=root/'dist'
    dist.mkdir(exist_ok=True)
    path=dist/(config['id']+'-'+config['version']+'.zip')
    files=[file for file in sorted(track.rglob('*')) if file.is_file()]
    with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for index,file in enumerate(files):
            if progress:progress(index,len(files))
            z.write(file,Path('content/tracks')/config['id']/file.relative_to(track))
    return path
