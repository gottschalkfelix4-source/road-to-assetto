"""Check actual exported paving/terrain contacts without starting the game."""
from pathlib import Path
import argparse
import json
import sys

import numpy as np
from shapely import polygons, intersection, area
from shapely.geometry import Polygon

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.inspect_kn5 import inspect
from roadtoassetto.terrain_sampling import ExportedTerrainSampler


def contact_vertices(triangles,ground):
    """Compare every vertex of intersecting planar faces, including interior cuts.

    The height difference is linear in each overlap polygon, so its extrema
    occur at these vertices. Sparse paving-top samples can miss a terrain peak.
    """
    tops=polygons(triangles[:,:,[0,2]])
    pairs=ground.index.query(tops,predicate='intersects')
    overlaps=intersection(tops[pairs[0]],ground.index.geometries[pairs[1]])
    expected=area(tops)
    covered=np.zeros(len(tops))
    np.add.at(covered,pairs[0],area(overlaps))
    points=[];top_ids=[]
    for i,shape in enumerate(overlaps):
        pieces=[shape] if isinstance(shape,Polygon) else getattr(shape,'geoms',[])
        for part in pieces:
            if not isinstance(part,Polygon) or part.area<1e-10:
                continue
            xy=np.asarray(part.exterior.coords)[:-1]
            points.append(xy)
            top_ids.extend([int(pairs[0,i])]*len(xy))
    if not points:
        raise ValueError('No terrain overlap beneath paving')
    xy=np.vstack(points);t=triangles[np.asarray(top_ids)]
    a=t[:,1][:,[0,2]]-t[:,0][:,[0,2]];b=t[:,2][:,[0,2]]-t[:,0][:,[0,2]]
    q=xy-t[:,0][:,[0,2]]
    det=a[:,0]*b[:,1]-a[:,1]*b[:,0]
    wa=(q[:,0]*b[:,1]-q[:,1]*b[:,0])/det
    wb=(a[:,0]*q[:,1]-a[:,1]*q[:,0])/det
    height=t[:,0,1]*(1-wa-wb)+t[:,1,1]*wa+t[:,2,1]*wb
    coverage={'top_area_m2':float(expected.sum()),'terrain_overlap_area_m2':float(covered.sum()),
              'uncovered_area_m2':float(np.maximum(0.,expected-covered).sum()),
              'maximum_face_uncovered_m2':float(np.maximum(0.,expected-covered).max())}
    return np.c_[xy[:,0],height,xy[:,1]],coverage


def hillside_contact_checks(model, metadata, ground=None):
    """Check each advertised construction against stored float32 terrain.

    Empty categories are valid for a cropped route. A nonzero category must
    have its actual top mesh; vertical masonry is deliberately not a top.
    A solid stair tread can bridge one bounded riser at the ground seam.
    """
    if ground is None:
        terrain=np.concatenate([m['positions'][m['indices'].reshape(-1,3)]
                                for m in model['meshes'] if m['name'].startswith('1GRASS_TERRAIN')])
        ground=ExportedTerrainSampler(terrain,[0.,0.],0.)
    counts=metadata.get('hillside_rendered_surface_counts',{})
    limits=metadata.get('hillside_limits',{})
    checks={};groups={}
    for kind,prefix in [('terrace','HILLSIDE_TERRACE_'),('landing','HILLSIDE_LANDING_'),
                        ('stair','HILLSIDE_STAIR_TREAD_'),('driveway','HILLSIDE_DRIVEWAY_'),
                        ('bank_ledge','HILLSIDE_BANK_LEDGE_'),('footpath','HILLSIDE_FOOTPATH_')]:
        label='hillside_'+kind
        meshes=[m for m in model['meshes'] if m['name'].startswith(prefix)]
        count=counts.get(kind)
        checks[label+'_count_matches_mesh_presence']=isinstance(count,int) and count>=0 and bool(count)==bool(meshes)
        if not meshes:
            groups[label]={'declared_surfaces':count,'triangles':0}
            continue
        triangles=np.concatenate([m['positions'][m['indices'].reshape(-1,3)] for m in meshes]).astype(float)
        up=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])[:,1]
        checks[label+'_all_faces_upward']=bool(np.all(up>1e-6))
        checks[label+'_visible_noncolliding']=all(m.get('visible',False) and m.get('renderable',False)
                                                 and not m['name'][0].isdigit() for m in meshes)
        samples,coverage=contact_vertices(triangles,ground)
        xy=np.c_[samples[:,0]+ground.origin[0],ground.origin[1]-samples[:,2]]
        clearance=samples[:,1]+ground.height_origin-ground(xy)
        # The geometry planner limits risers to175mm. Do not let a corrupted
        # manifest increase the accepted support distance.
        support_limit=.215 if kind in ('stair','landing') else .12
        checks[label+'_terrain_covers_footprint']=coverage['uncovered_area_m2']<=1e-5
        checks[label+'_terrain_not_above_top']=bool(clearance.min()>=-.003)
        checks[label+'_ground_supported']=bool(clearance.max()<=support_limit)
        groups[label]={'declared_surfaces':count,'triangles':len(triangles),'samples':len(samples),
                       'coverage':coverage,'support_limit_m':support_limit,
                       'minimum_clearance_m':round(float(clearance.min()),6),
                       'maximum_clearance_m':round(float(clearance.max()),6)}
        if kind in ('driveway','footpath','stair','landing'):
            # Gradient of every exported plane; detects steepness even when
            # an otherwise correct longitudinal field hides sideways tilt.
            a=triangles[:,1]-triangles[:,0];b=triangles[:,2]-triangles[:,0]
            matrix=np.stack([a[:,[0,2]],b[:,[0,2]]],axis=1)
            rhs=np.stack([a[:,1],b[:,1]],axis=1)
            gradient=np.linalg.solve(matrix,rhs[...,None])[...,0]
            steepest=np.linalg.norm(gradient,axis=1)
            plane_limit={'driveway':.14,'footpath':.12,'stair':.02,'landing':.14}[kind]
            checks[label+'_actual_planes_bounded']=bool(steepest.max()<=plane_limit+.0002)
            groups[label]['maximum_actual_plane_gradient']=round(float(steepest.max()),6)
            if kind in ('stair','landing'):
                continue
            crossfall_limit=.05 if kind=='driveway' else .08
            records=metadata.get('hillside_'+kind+'_records',[])
            checks[label+'_records_complete']=len(records)==count
            assigned=np.zeros(len(triangles),dtype=int)
            record_results=[]
            origin=np.asarray(metadata['origin_utm32']);h0=float(metadata['height_origin_m'])
            centroids=triangles.mean(axis=1)
            from shapely import contains_xy
            for record in records:
                p=np.asarray(record['xy'],dtype=float);z=np.asarray(record['top'],dtype=float)
                local=np.c_[p[:,0]-origin[0],z-h0,origin[1]-p[:,1]]
                # Apply the documented scene→KN5 serialization to the
                # independent plan too. Comparing an unrounded footprint
                # with round(5)→float32 falsely reports micrometre strips.
                local=local.round(5).astype(np.float32).astype(float)
                footprint=Polygon(local[:,[0,2]])
                selected=contains_xy(footprint.buffer(.0001),centroids[:,0],centroids[:,2])
                assigned+=selected
                actual_triangles=triangles[selected]
                if not len(actual_triangles):
                    record_results.append({'id':record['id'],'valid':False});continue
                planned=ExportedTerrainSampler(local[np.asarray(record['faces'])],[0.,0.],0.)
                samples,coverage=contact_vertices(actual_triangles,planned)
                delta=samples[:,1]-planned(np.c_[samples[:,0],-samples[:,2]])
                # Actual exported gradients in world XY. The ramp's first
                # edge is its transverse direction, independent of yaw.
                actual_gradient=gradient[selected]*np.array([1.,-1.])
                across=p[1]-p[0];across/=np.linalg.norm(across)
                sideways=np.abs(actual_gradient@across)
                actual_area=sum(Polygon(t[:,[0,2]]).area for t in actual_triangles)
                valid=(coverage['uncovered_area_m2']<=1e-5 and abs(actual_area-footprint.area)<=.002 and
                       np.abs(delta).max()<=.0001 and sideways.max()<=crossfall_limit+.0002 and
                       np.linalg.norm(actual_gradient,axis=1).max()<=plane_limit+.0002)
                record_results.append({'id':record['id'],'valid':bool(valid),
                    'maximum_crossfall':round(float(sideways.max()),6),
                    'uncovered_plan_area_m2':float(coverage['uncovered_area_m2']),
                    'maximum_plan_height_error_m':round(float(np.abs(delta).max()),7)})
            checks[label+'_actual_record_geometry']=bool(record_results) and all(r['valid'] for r in record_results)
            checks[label+'_every_face_accounted_for']=bool(np.all(assigned==1))
            groups[label]['record_checks']=record_results
    checks['hillside_declared_limits_bounded']=(0<limits.get('maximum_stair_riser_m',0)<=.175 and
        limits.get('minimum_stair_tread_m',0)>=.30 and 0<limits.get('maximum_driveway_gradient',0)<=.14 and
        0<limits.get('maximum_driveway_crossfall',0)<=.05 and
        0<limits.get('maximum_footpath_gradient',0)<=.12 and
        0<limits.get('maximum_footpath_crossfall',0)<=.08)
    return checks,groups


def verify(root=ROOT):
    root=Path(root)
    config=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
    scene=json.loads((root/config['data_dir']/'scene.json').read_text(encoding='utf-8'))
    meta=scene['metadata']
    model=inspect(root/'build'/config['id']/(config['id']+'.kn5'),geometry=True)
    terrain=np.concatenate([mesh['positions'][mesh['indices'].reshape(-1,3)]
                            for mesh in model['meshes'] if mesh['name'].startswith('1GRASS_TERRAIN')])
    ground=ExportedTerrainSampler(terrain,meta['origin_utm32'],meta['height_origin_m'])
    checks={}; groups={}
    for label,prefix in (('sidewalk','DE_PAVING_'),('house_contact','HOUSE_CONTACT_PAVING_'),
                         ('foundation','HOUSE_FOUNDATION_GRAVEL'),
                         ('drainage','DE_STONE_DRAINAGE'),('road','1ROAD_MAIN'),
                         ('shoulder','1SHOULDER')):
        selected=[]
        for mesh in model['meshes']:
            if not mesh['name'].startswith(prefix):
                continue
            triangles=mesh['positions'][mesh['indices'].reshape(-1,3)].astype(float)
            normal=np.cross(triangles[:,1]-triangles[:,0],triangles[:,2]-triangles[:,0])
            selected.append(triangles[normal[:,1]>1e-6])
        triangles=np.concatenate(selected) if selected else np.empty((0,3,3))
        checks[label+'_top_faces_present']=len(triangles)>0
        if not len(triangles):
            continue
        samples,coverage=contact_vertices(triangles,ground)
        checks[label+'_terrain_covers_footprint']=coverage['uncovered_area_m2']<=1e-5
        xy=np.c_[samples[:,0]+meta['origin_utm32'][0],meta['origin_utm32'][1]-samples[:,2]]
        clearance=samples[:,1]+meta['height_origin_m']-ground(xy)
        checks[label+'_terrain_not_above_top']=bool(clearance.min()>=-.003)
        if label in ('sidewalk','house_contact','foundation'):
            checks[label+'_ground_supported']=bool(clearance.max()<=.12)
        groups[label]={'triangles':len(triangles),'samples':len(samples),'coverage':coverage,
                       'minimum_clearance_m':round(float(clearance.min()),6),
                       'median_clearance_m':round(float(np.median(clearance)),6),
                       'maximum_clearance_m':round(float(clearance.max()),6),
                       'terrain_above_top_samples':int(np.count_nonzero(clearance<-.003)),
                       'deep_support_samples':int(np.count_nonzero(clearance>.12)),
                       'worst_contacts':[{'clearance_m':round(float(clearance[i]),6),
                                          'xy_utm':xy[i].round(4).tolist(),
                                          'top_m':round(float(samples[i,1]+meta['height_origin_m']),6)}
                                         for i in np.argsort(clearance)[:5]]}
    if tuple(map(int,config['version'].split('.'))) >= (0,10,0):
        hillside_checks,hillside_groups=hillside_contact_checks(model,meta,ground)
        checks.update(hillside_checks);groups.update(hillside_groups)
    baseline=root/'test-results/road-v09-baseline.json'
    if not baseline.exists():
        baseline=root/'test-results/road-v08-baseline.json'
    if not baseline.exists():
        baseline=root/'test-results/road-v07-baseline.json'
    if baseline.is_file():
        previous=json.loads(baseline.read_text(encoding='utf-8'))
        for key in ('centerline_ac','road_width_profile_m','length_m','origin_utm32','height_origin_m'):
            checks['preserved_'+key]=bool(np.allclose(previous[key],meta[key],atol=1e-8))
    report={'version':config['version'],'passed':all(checks.values()),'checks':checks,
            'groups':groups,'measurement':'KN5 float32 triangles; height extrema at all vertices of intersecting paving and terrain faces',
            'game_test_performed':False}
    tag='v'+''.join(config['version'].split('.')[:2])
    output=root/'test-results'/f'street-contacts-{tag}.json'
    output.write_text(json.dumps(report,indent=2),encoding='utf-8')
    print(json.dumps(report,indent=2))
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=ROOT)
    args=parser.parse_args()
    raise SystemExit(0 if verify(args.root.resolve()) else 1)
