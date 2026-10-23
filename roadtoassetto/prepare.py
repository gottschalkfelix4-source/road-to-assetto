"""Prepare a game-coordinate scene from OSM geometry and Hessian DGM1 data.

Coordinates: AC X=east, Y=up, Z=south. Physical road heights are smoothed.
The optional drainage crown is a documented typical profile, not surveyed.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from xml.etree import ElementTree as ET

import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
import rasterio
from scipy.ndimage import gaussian_filter1d, map_coordinates
from scipy.spatial import cKDTree
from shapely.geometry import LineString, Polygon
from shapely.ops import triangulate, unary_union
from roadtoassetto.buildings import build_lod2
from roadtoassetto.scenery import build_scenery, select_route, surface_textures, context_geometries, Aerial
from roadtoassetto.real_surfaces import surface_materials
from roadtoassetto.road_alignment import load_alignment, widths_at
from roadtoassetto.ground_details import (RoadGroundProfile, StreetGroundProfile, build_terrain_mesh,
                                        build_ground_transitions, plan_house_connections, protected_road_surfaces)
from roadtoassetto.german_streets import plan_street_edges
from roadtoassetto.terrain_sampling import ExportedTerrainSampler


class Heights:
    def __init__(self, path):
        with rasterio.open(path) as ds:
            if ds.crs.to_epsg() != 25832:
                raise ValueError('Expected DGM in EPSG:25832')
            self.raster = ds.read(1)
            self.transform = ds.transform
            self.nodata = ds.nodata
        if not np.isfinite(self.raster).all() or np.any(self.raster < -100):
            raise ValueError('DEM contains invalid or missing heights')

    def __call__(self, xy):
        xy = np.asarray(xy, dtype=float)
        inv = ~self.transform
        cols = inv.a * xy[..., 0] + inv.b * xy[..., 1] + inv.c - .5
        rows = inv.d * xy[..., 0] + inv.e * xy[..., 1] + inv.f - .5
        if np.any(cols < 0) or np.any(rows < 0) or np.any(cols > self.raster.shape[1]-1) or np.any(rows > self.raster.shape[0]-1):
            raise ValueError('Geometry outside downloaded DEM')
        return map_coordinates(self.raster, [rows.ravel(), cols.ravel()], order=1, mode='nearest').reshape(cols.shape)


def ordered_road(ways):
    """Join exactly connected OSM ways. Reject branches/disconnected streets."""
    edges = {}
    for way in ways:
        if way.get('tags', {}).get('bridge') == 'yes' or way.get('tags', {}).get('tunnel') == 'yes':
            raise ValueError('Bridges and tunnels require a separate height model')
        for node, p in zip(way['nodes'], way['geometry']):
            edges.setdefault(node, {'p': (p['lon'], p['lat']), 'neighbors': set()})
        for a, b in zip(way['nodes'], way['nodes'][1:]):
            edges[a]['neighbors'].add(b)
            edges[b]['neighbors'].add(a)
    if any(len(v['neighbors']) > 2 for v in edges.values()):
        raise ValueError('Street branches; explicit start/end selection required')
    ends = [n for n, v in edges.items() if len(v['neighbors']) == 1]
    if len(ends) != 2:
        raise ValueError('Street must have exactly two ends')
    # Saved browser routes preserve the user's A -> B direction. Existing
    # prepared routes keep their established south-to-north orientation.
    explicit_start=ways[0].get('route_start_node') if ways else None
    if explicit_start is not None and explicit_start not in ends:
        raise ValueError('Requested route start is not an endpoint')
    current = explicit_start if explicit_start is not None else min(ends, key=lambda n: edges[n]['p'][1])
    previous = None
    result = []
    while True:
        result.append(edges[current]['p'])
        nxt = edges[current]['neighbors'] - {previous}
        if not nxt:
            break
        previous, current = current, next(iter(nxt))
        if len(result) > len(edges):
            raise ValueError('Cyclic street')
    if len(result) != len(edges):
        raise ValueError('Disconnected street geometry')
    return np.array(result)


def prepare(root: Path, config: dict):
    quality = config.get('scenery_quality', 'high')
    density = float(config.get('vegetation_density', .65))
    if quality not in ('standard', 'high'):
        raise ValueError('scenery_quality must be standard or high')
    if not np.isfinite(density) or not 0 <= density <= 1:
        raise ValueError('vegetation_density must be between 0 and 1')
    data_dir = root / config['data_dir']
    textures = data_dir / 'textures'
    textures.mkdir(parents=True, exist_ok=True)
    ways = json.loads((data_dir / 'road_ways.json').read_text(encoding='utf-8'))
    shared=config.get('_road_profile')
    if shared:
        # Long routes compute one canonical profile before loading the heavy
        # scene data. Never re-smooth its windows or reset their UV distance.
        xy=xy_original=np.asarray(shared['xy'],dtype=float)
        if xy.ndim!=2 or xy.shape[1]!=2 or len(xy)<2 or not np.isfinite(xy).all():
            raise ValueError('Invalid shared road profile')
        lonlat=np.empty((len(xy),2))
        alignment={'applied':False,'source':'Shared globally smoothed route profile'}
        route_selection={}
    else:
        lonlat = ordered_road(ways)
        transform = Transformer.from_crs(4326, 25832, always_xy=True)
        xy_original = np.array(transform.transform(lonlat[:, 0], lonlat[:, 1])).T
        xy_original, alignment = load_alignment(data_dir,xy_original)
        xy_original, route_selection = select_route(xy_original, config.get('route_start_m'), config.get('route_end_m'))
        ds = np.linalg.norm(np.diff(xy_original, axis=0), axis=1)
        if np.any(ds < .01):
            raise ValueError('Duplicate or very short centerline segments')
        s_original = np.r_[0, np.cumsum(ds)]
        s = np.linspace(0, s_original[-1], math.ceil(s_original[-1]/config['sample_spacing_m'])+1)
        # Smooth a distance-resampled polyline locally. A global cubic spline
        # can overshoot sparse nodes and cut adjacent building corners.
        xy = np.c_[np.interp(s,s_original,xy_original[:,0]),np.interp(s,s_original,xy_original[:,1])]
        smoothing=alignment.get('recommended_resample_smoothing_sigma_m',2.)/config['sample_spacing_m']
        xy = gaussian_filter1d(xy,sigma=smoothing,axis=0,mode='nearest')
        if alignment.get('applied'):
            xy[[0,-1]]=xy_original[[0,-1]]
    line = LineString(xy)
    if not line.is_simple:
        raise ValueError('Interpolated road crosses itself')
    if not shared and max(LineString(xy_original).distance(__import__('shapely').Point(p)) for p in xy) > 1.5:
        raise ValueError('Road interpolation deviates too far from source')
    tangent = np.asarray(shared['tangent'],dtype=float) if shared else np.gradient(xy, axis=0)
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    left = np.asarray(shared['left'],dtype=float) if shared else np.c_[-tangent[:, 1], tangent[:, 0]]
    dem = Heights(data_dir / 'dgm1.tif')
    raw_heights = (np.asarray(shared['raw_heights'],dtype=float) if shared else
                  np.median(np.array([dem(xy + left*k) for k in (-.8, 0, .8)]), axis=0))
    road_y = np.asarray(shared['road_y'],dtype=float) if shared else gaussian_filter1d(raw_heights, sigma=3, mode='nearest')
    road_widths = np.asarray(shared['road_widths'],dtype=float) if shared else widths_at(xy,alignment,config['road_width_m'])
    if any(value.shape!=(len(xy),) or not np.isfinite(value).all() for value in (raw_heights,road_y,road_widths)):
        raise ValueError('Invalid shared road heights or widths')
    half = road_widths/2
    crossfall=float(config.get('road_crossfall',0.))
    if not np.isfinite(crossfall) or not 0 <= crossfall <= .04:
        raise ValueError('road_crossfall must be between 0 and 0.04')
    left_edge = xy + left * half[:,None]
    right_edge = xy - left * half[:,None]
    road_polygon = Polygon(np.vstack([left_edge, right_edge[::-1]]))
    if not road_polygon.is_valid:
        raise ValueError('Road width creates overlapping edges')
    origin = np.asarray(shared['origin_utm32'],dtype=float) if shared else xy.mean(axis=0)
    height_origin = float(shared['height_origin_m']) if shared else float(road_y.min())
    spacing = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    distance = np.asarray(shared['distance'],dtype=float) if shared else np.r_[0, np.cumsum(spacing)]
    grades = np.diff(road_y) / spacing
    if np.max(np.abs(grades)) > .25:
        raise ValueError('Unexpected road gradient exceeds 25 percent')
    curve = np.abs(np.diff(np.unwrap(np.arctan2(tangent[:, 1], tangent[:, 0])))) / spacing

    scene = {'materials': [], 'meshes': [], 'markers': [], 'textures_dir': textures.relative_to(root).as_posix(), 'metadata': {}}
    def material(name, texture, ambient=.5, diffuse=.65, specular=.05):
        scene['materials'].append({'name':name, 'texture':texture, 'ambient':ambient, 'diffuse':diffuse, 'specular':specular})
        return len(scene['materials'])-1

    rng = np.random.default_rng(41)
    def noise_texture(name, color, noise=8, size=256):
        pix = np.clip(np.array(color)[None,None,:] + rng.normal(0,noise,(size,size,1)),0,255).astype('uint8')
        Image.fromarray(pix).save(textures / name)
    # Keep the old RNG advancement for every existing procedural material.
    # New asphalt uses the same metric UV frame without altering road geometry.
    rng.normal(0,5,(256,256,1))
    surface_textures(textures, quality)
    noise_texture('shoulder.png', (125,119,105), 10)
    noise_texture('concrete.png', (160,155,143), 4)
    noise_texture('roof_red.png', (101,53,39), 7)
    noise_texture('roof_dark.png', (57,61,64), 5)
    noise_texture('barrier.png', (190,76,27), 3)
    facade = Image.new('RGB',(256,256),(202,191,170))
    d = ImageDraw.Draw(facade)
    d.rectangle((0,236,255,255),fill=(125,119,112))
    for x in (40,155):
        d.rectangle((x-4,38,x+49,124),fill=(225,218,201))
        d.rectangle((x,42,x+45,120),fill=(66,82,85))
        d.line((x+22,42,x+22,120),fill=(206,200,188),width=3)
        d.line((x,81,x+45,81),fill=(206,200,188),width=3)
    facade.save(textures/'facade.png')
    with rasterio.open(data_dir/'ortho.tif') as ds:
        ortho_bounds = list(ds.bounds)
        ortho_resolution = list(ds.res)
        arr = ds.read([1,2,3])
        nir = ds.read(4) if ds.count==4 else None
    Image.fromarray(arr.transpose(1,2,0)).save(textures/'ortho.jpg',quality=94)
    asphalt = material('asphalt','asphalt.png',ambient=.33,diffuse=.5,specular=.045)
    scene['materials'][asphalt].update(normal='asphalt_normal_dx.png', normal_strength=.23)
    shoulder_mat = material('roadside','shoulder.png')
    concrete = material('pavement','concrete.png')
    terrain_mat = material('aerial_ground','ortho.jpg',ambient=.3,diffuse=.38,specular=.01)
    wall_mat = material('generic_building','facade.png')
    red_roof = material('red_roof','roof_red.png')
    dark_roof = material('dark_roof','roof_dark.png')
    barrier_mat = material('test_end_barrier','barrier.png')
    surface_stats=surface_materials(data_dir,textures,scene,asphalt,shoulder_mat,terrain_mat,arr,nir,
                                   ortho_bounds=ortho_bounds)
    # Keep the original CC0 scan pixels. Material response supplies a restrained
    # dark-grey asphalt tone, also used by the matching Blender preview shader.
    scene['materials'][asphalt].update(albedo_multiplier=[.88,.88,.88],ambient=.352,diffuse=.4224)
    scene['materials'][shoulder_mat].update(
        texture='asphalt_photo.jpg',normal='asphalt_photo_normal_dx.jpg',
        normal_strength=.35,albedo_multiplier=[.84,.84,.84],
        ambient=.336,diffuse=.4032,specular=.035,roughness=.92)

    def ac(xy_points, heights):
        p = np.asarray(xy_points)
        return np.c_[p[:,0]-origin[0], np.asarray(heights)-height_origin, -(p[:,1]-origin[1])]

    def add_mesh(name, points, faces, mat, uv=None, collision=False):
        p = np.asarray(points, dtype=float)
        f = np.asarray(faces,dtype=int)
        if len(p) < 3 or len(f) == 0:
            return
        if len(p) > 65535:
            raise ValueError(f'Mesh too large: {name}')
        if uv is None:
            uv = p[:,[0,2]]/6
        scene['meshes'].append({'name':name,'vertices':p.round(5).tolist(),'faces':f.tolist(),
                                'uv':np.asarray(uv).round(6).tolist(),'material':mat,'collision':collision})

    def strip(name, offsets, mat, y_offset=0, collision=True):
        offsets = np.asarray(offsets)
        if offsets.ndim==1:offsets=np.broadcast_to(offsets,(len(xy),len(offsets)))
        count=offsets.shape[1]
        pts = (xy[:,None,:] + left[:,None,:]*offsets[:,:,None]).reshape(-1,2)
        # A symmetric, typical drainage crown. Shoulder matches the asphalt
        # edge; the old continuous loose-gravel border is replaced by paving.
        depression=crossfall*np.minimum(np.abs(offsets),half[:,None])
        yy = (road_y[:,None]+y_offset-depression).ravel()
        repeat=surface_stats['asphalt_repeat_m'] if mat in (asphalt,shoulder_mat) else [1.5,1.5]
        uv = np.c_[offsets.ravel()/repeat[0],np.repeat(distance,count)/repeat[1]]
        faces = []
        for i in range(len(xy)-1):
            for j in range(count-1):
                a=i*count+j; b=a+count
                faces.extend([(a,b,a+1),(a+1,b,b+1)])
        # The parametric ordering is checked after conversion to AC coordinates.
        points=ac(pts,yy)
        a,b,c=points[np.array(faces[0])]
        if np.cross(b-a,c-a)[1]<0:
            faces=[(a,c,b) for a,b,c in faces]
        add_mesh(name,points,faces,mat,uv,collision)

    strip('1ROAD_MAIN',half[:,None]*np.linspace(-1,1,7)[None,:],asphalt)
    # The street has a very tight bend: keep prototype shoulders narrow enough
    # that the offset mesh cannot fold across the inside of that bend.
    strip('1SHOULDER_LEFT',half[:,None]+np.array([0,.2,.4]),shoulder_mat,y_offset=-.015)
    strip('1SHOULDER_RIGHT',-half[:,None]+np.array([-.4,-.2,0]),shoulder_mat,y_offset=-.015)

    road_profile=RoadGroundProfile(xy,road_y,half,crossfall=crossfall,source_sampler=dem)
    bounds=json.loads((data_dir/'bounds.json').read_text())
    # Resolve the shared street/building contact elevations before terrain is
    # triangulated. This avoids a separate wavy DGM strip behind each sidewalk.
    street_context=context_geometries(data_dir,{way['id'] for way in ways})
    from roadtoassetto.junction_mouths import reviewed_junction_reservation
    reviewed_junctions=reviewed_junction_reservation(data_dir,xy,road_polygon)
    street_context['roads']=unary_union([street_context['roads'],reviewed_junctions])
    street_context['other_roads']=unary_union([street_context['other_roads'],reviewed_junctions])
    street_aerial=Aerial(data_dir/'ortho.tif')
    street_plan=plan_street_edges(context=street_context,xy=xy,road_y=road_y,left=left,
        distance=distance,half_width=half,road_polygon=road_polygon,dem=dem,flatten=road_profile,
        aerial=street_aerial,data_dir=data_dir,edge_y=road_y-crossfall*half,
        quality=config.get('scenery_quality','high'))
    street_surfaces=street_plan['paving_sections']
    connection_plan=plan_house_connections(data_dir,line,road_polygon,bounds,street_surfaces,
        lambda points:road_profile(points,dem(points))-.08,other_roads=street_context['other_roads'])
    from roadtoassetto.hillside import plan_hillside
    hillside_plan=plan_hillside(data_dir,line,road_polygon,bounds,street_surfaces,
        lambda points:road_profile(points,dem(points))-.08,
        other_roads=street_context['other_roads'],existing_plan=connection_plan)
    connection_plan=hillside_plan.get('remaining_connection_plan',connection_plan)
    (data_dir/'hillside_plan.json').write_text(json.dumps(hillside_plan,indent=2,allow_nan=False),encoding='utf-8')
    flatten=StreetGroundProfile(road_profile,street_surfaces,road_polygon,
        contact_surfaces=connection_plan['contact_surfaces'],
        foundation_surfaces=connection_plan['foundation_surfaces'],source_sampler=dem,
        hillside_surfaces=hillside_plan['terrain_surfaces'],
        protected_surfaces=protected_road_surfaces(scene,origin,height_origin),blend=.8)
    ground_stats=build_terrain_mesh(bounds,config['terrain_spacing_m'],flatten,dem,ac,
                                    add_mesh,terrain_mat,ortho_bounds,scene=scene)
    # House bases must meet the same triangles the game renders and collides
    # with, including export rounding, rather than resampling the source DGM.
    ground_sampler=ExportedTerrainSampler.from_scene(scene,origin,height_origin)

    ownership=None
    if config.get('_ownership_polygon'):
        from shapely.geometry import shape
        ownership=shape(config['_ownership_polygon'])
    building_stats=build_lod2(data_dir,textures,scene,material,add_mesh,ac,line,road_polygon,dem,ortho_bounds,
                              allow_outside_photo_overrides=config.get('allow_outside_photo_overrides',False),
                              terrain_height_sampler=ground_sampler,
                              base_road_reservation=unary_union([road_polygon,street_context['other_roads']]),
                              ownership_polygon=ownership)
    building_count=building_stats['buildings']
    scenery_stats=build_scenery(data_dir,textures,config,scene,material,add_mesh,ac,
                                xy,road_y,tangent,left,distance,road_polygon,dem,flatten,road_widths=road_widths,
                                edge_y=road_y-crossfall*half,street_plan=street_plan,
                                street_context=street_context,street_aerial=street_aerial,
                                terrain_height_sampler=ground_sampler,
                                construction_reservation=unary_union([Polygon(s['xy']) for s in hillside_plan['terrain_surfaces']]))
    from roadtoassetto.road_wear import build_road_wear
    road_wear_stats=build_road_wear(textures,scene,material,add_mesh,ac,xy,road_y,left,distance,half,
                                  asphalt_material=asphalt)
    scenery_stats['scenery_counts']['asphalt_repair_sections']=road_wear_stats['asphalt_repair_sections']
    from roadtoassetto.junction_mouths import build_junction_mouths
    junction_stats=build_junction_mouths(data_dir,scene,material,add_mesh,ac,xy,road_y,half,
        road_polygon,ground_sampler,flatten,asphalt_material=asphalt)
    mouth_footprints=[Polygon(p['exterior'],p.get('holes',[]))
                      for p in junction_stats.get('junction_mouth_footprints_utm32',[])]
    ground_reservation=unary_union([road_polygon,*mouth_footprints])
    ground_stats.update(build_ground_transitions(data_dir,scene,material,add_mesh,ac,line,ground_reservation,
                         ground_sampler,bounds,road_clearance_m=0.,street_surfaces=street_surfaces,
                         connection_plan=connection_plan))
    from roadtoassetto.hillside_render import build_hillside
    ground_stats.update(hillside_plan['stats'])
    ground_stats.update(build_hillside(hillside_plan,textures,scene,material,add_mesh,ac,
                                     ground_sampler,quality=config.get('scenery_quality','high')))
    from roadtoassetto.road_markings import build_road_markings
    marking_stats=build_road_markings(data_dir,textures,scene,material,add_mesh,ac)

    # Physical barriers across the ends clearly mark this finite test area.
    section=config.get('_section',{})
    for end in (0,len(xy)-1):
        if (end==0 and not section.get('first',True)) or (end!=0 and not section.get('last',True)):
            continue
        k=5 if end==0 else len(xy)-6
        center=xy[k]; l=left[k]; t=tangent[k]
        h=half[k]
        p2=np.array([center+l*v+t*u for u,v in ((-.25,-h-1.5),(-.25,h+1.5),(.25,h+1.5),(.25,-h-1.5))])
        low=ac(p2,np.repeat(road_y[k],4));high=low+np.array([0,.75,0])
        p=np.vstack([low,high])
        faces=[(0,2,1),(0,3,2),(4,5,6),(4,6,7),(0,1,5),(0,5,4),(1,2,6),(1,6,5),(2,3,7),(2,7,6),(3,0,4),(3,4,7)]
        add_mesh('1WALL_END_'+str(end),p,faces,barrier_mat,collision=True)

    def marker(name,metres):
        k=int(np.argmin(np.abs(distance-metres)))
        p=ac(xy[[k]],road_y[[k]])[0]+np.array([0,.22,0])
        forward=np.array([tangent[k,0],0,-tangent[k,1]])
        right=np.cross(np.array([0,1,0]),forward)
        matrix=[[*right,0],[0,1,0,0],[*forward,0],[*p,1]]
        scene['markers'].append({'name':name,'matrix':matrix})
    if section.get('first',True):
        for i in range(config['pitboxes']):
            marker(f'AC_PIT_{i}',18+i*9)
            marker(f'AC_START_{i}',18+i*9)
        marker('AC_HOTLAP_START_0',18)
    # Point-to-point practice; no generated AI or lap timing is claimed.
    metadata={'id':config['id'],'name':config['name'],'version':config['version'],
              'length_m':round(float(distance[-1]-distance[0]),2),'elevation_min_m':round(float(road_y.min()),2),
              'elevation_max_m':round(float(road_y.max()),2),'max_gradient_percent':round(float(abs(grades).max()*100),2),
              'minimum_radius_m':round(float(1/max(curve.max(),1e-9)),2),'buildings':building_count,
              'road_width_m':config['road_width_m'],'origin_utm32':origin.tolist(),'height_origin_m':height_origin,
              'centerline_ac':ac(xy,road_y).round(5).tolist(),'source_way_ids':[w['id'] for w in ways],
              'source_nodes':len(lonlat),'estimated_properties':['road width','typified symmetric drainage crown, not surveyed','generic facade styles except explicitly attributed local photo overrides'],
              'limitations':['Practice and multiplayer geometry only; no AI traffic or race timing',
                             'Side streets have short visual asphalt mouths only; main street remains the generated driving route',
                             'Terrain is graded near road; not a laser-scanned road surface'],
              'sources':[{'name':'OpenStreetMap contributors','license':'ODbL-1.0','url':'https://www.openstreetmap.org/copyright'},
                         {'name':'HVBG Hessen DGM1, DOP20 and LoD2','license':'dl-de/zero-2-0','url':'https://hvbg.hessen.de/geoinformation/open-data'},
                         {'name':'ambientCG PaintedPlaster006','license':'CC0-1.0','url':'https://ambientcg.com/view?id=PaintedPlaster006'}]}
    metadata.update(building_stats)
    metadata.update(route_selection)
    metadata.update(scenery_stats)
    metadata.update(ground_stats)
    metadata.update(road_wear_stats)
    metadata.update(junction_stats)
    metadata.update(marking_stats)
    metadata['road_crossfall']={'slope':crossfall,'profile':'symmetric crown',
        'source':'Typical drainage construction profile; no local crossfall survey',
        'measured':False}
    metadata['road_alignment']=alignment
    metadata['road_width_profile_m']=road_widths.round(5).tolist()
    metadata['road_width_range_m']=[round(float(road_widths.min()),3),round(float(road_widths.max()),3)]
    metadata['estimated_properties'][0]=('aerial-interpreted road edge widths and obscured centreline sections'
        if alignment.get('applied') else 'constant user-estimated road width')
    metadata['aerial_resolution_m']=ortho_resolution
    if config.get('source_route'):
        metadata['source_route']=config['source_route']
    metadata['ground_materials']=surface_stats
    metadata['sources'].extend({'name':r['provider']+' '+r['asset'],'license':r['license'],'url':r['url']} for r in surface_stats['source_records'])
    metadata['estimated_properties'].extend(['Tree heights and aerial vegetation classification',
                                             'Pavement/kerb locations and dimensions; illustrative asphalt repairs'])
    metadata['limitations'].extend(scenery_stats['scenery_limitations'])
    metadata['ai_materials']={'enabled':False}
    if config.get('ai_materials',False):
        from roadtoassetto.ai_materials import apply_material_pack
        metadata['ai_materials']=apply_material_pack(scene,textures,data_dir/'materials/ai')
        metadata['limitations'].append(metadata['ai_materials']['accuracy'])
    if (data_dir/'materials/AIWindows/provenance.json').is_file():
        metadata['window_texture_source']=json.loads((data_dir/'materials/AIWindows/provenance.json').read_text(encoding='utf-8'))
    if (data_dir/'materials/AIDoors/provenance.json').is_file():
        metadata['door_texture_source']=json.loads((data_dir/'materials/AIDoors/provenance.json').read_text(encoding='utf-8'))
    scene['metadata']=metadata
    (data_dir/'scene.json').write_text(json.dumps(scene,separators=(',',':')),encoding='utf-8')
    (data_dir/'manifest.json').write_text(json.dumps({k:v for k,v in metadata.items() if k!='centerline_ac'},indent=2,ensure_ascii=False),encoding='utf-8')
    np.savez_compressed(data_dir/'centerline.npz',positions=ac(xy,road_y),distance=distance,raw_heights=raw_heights)
    # Full geometric evidence belongs in scene/manifest files. Printing every
    # basement and terrace record multiplies multi-section build logs by GB.
    log_fields=('id','name','version','length_m','buildings','elevation_min_m',
                'elevation_max_m','max_gradient_percent','minimum_radius_m')
    log={key:metadata[key] for key in log_fields if key in metadata}
    log['source_warning_count']=len(metadata.get('source_route',{}).get('warnings',[]))
    print(json.dumps(log,ensure_ascii=False,indent=2))
    return scene
