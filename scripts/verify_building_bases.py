"""Independent checks of the actual exported building-base geometry.

This verifier does not import the building-base planner or renderer. It checks
stored triangles against source seams, exported terrain and accepted records.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import numpy as np
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.inspect_kn5 import inspect
from roadtoassetto.terrain_sampling import ExportedTerrainSampler

BASE_PREFIXES = ('BASEMENT_WALLS', 'BASEMENT_PLINTHS', 'BASEMENT_SOFFITS')


class TriangleIndex:
    """XZ bounds for walls as well as horizontal faces; heights filter later."""
    def __init__(self,triangles):
        from shapely import STRtree,box
        self.triangles=np.asarray(triangles,dtype=float).reshape(-1,3,3)
        lo=self.triangles.min(axis=1) if len(self.triangles) else np.empty((0,3))
        hi=self.triangles.max(axis=1) if len(self.triangles) else np.empty((0,3))
        self.tree=STRtree(box(lo[:,0],lo[:,2],hi[:,0],hi[:,2]))

    def nearby(self,lo,hi):
        from shapely.geometry import box
        ids=self.tree.query(box(lo[0],lo[2],hi[0],hi[2]))
        t=self.triangles[ids]
        return t[(t.max(axis=1)>=lo).all(axis=1)&(t.min(axis=1)<=hi).all(axis=1)]


def stored_coordinates(points, origin, height_origin):
    p = np.asarray(points, dtype=float)
    o = np.asarray(origin, dtype=float)
    return np.c_[p[:, 0]-o[0], p[:, 2]-height_origin, o[1]-p[:, 1]].round(5).astype(np.float32).astype(float)


def triangles_for(model, prefix):
    pieces = [np.asarray(m['positions'], dtype=float)[np.asarray(m['indices']).reshape(-1, 3)]
              for m in model['meshes'] if m['name'].startswith(prefix)]
    return np.concatenate(pieces) if pieces else np.empty((0, 3, 3))


def plane_frame(triangle):
    q = np.asarray(triangle, dtype=float)
    normal = np.cross(q[1]-q[0], q[2]-q[0])
    length = np.linalg.norm(normal)
    if not np.isfinite(q).all() or length <= 1e-8:
        raise ValueError('Degenerate reference face')
    normal /= length
    u = q[1]-q[0]
    u /= np.linalg.norm(u)
    return q[0], u, np.cross(normal, u), normal


def _oriented_face_key(triangle):
    q=np.asarray(triangle,dtype=float)
    return min(q.tobytes(),q[[1,2,0]].tobytes(),q[[2,0,1]].tobytes())


def face_coverage(expected, actual, *, plane_tolerance=.0002):
    """Area coverage of every oriented reference plane by exported triangles.

    No source vertex, boundary or centroid alone can prove a closed surface:
    union/intersection catches holes and missing half-quads between those points.
    """
    expected = np.asarray(expected, dtype=float).reshape(-1, 3, 3)
    actual = np.asarray(actual, dtype=float).reshape(-1, 3, 3)
    index=TriangleIndex(actual)
    exact={_oriented_face_key(t) for t in actual}
    missing = total = 0.
    worst = 0.
    for face in expected:
        # Exact oriented triangles already prove complete coverage. This is
        # a hash of independently read KN5 positions, not a planner success
        # flag. Changed positions, winding, missing faces and alternate
        # triangulations still enter the geometric union calculation below.
        if _oriented_face_key(face) in exact:
            total+=float(np.linalg.norm(np.cross(face[1]-face[0],face[2]-face[0]))/2)
            continue
        origin, u, v, normal = plane_frame(face)
        shape = Polygon(np.c_[(face-origin)@u, (face-origin)@v])
        total += shape.area
        if len(actual):
            lo=face.min(axis=0)-plane_tolerance; hi=face.max(axis=0)+plane_tolerance
            nearby=index.nearby(lo,hi)
            offsets = np.abs((nearby-origin)@normal).max(axis=1)
            n = np.cross(nearby[:, 1]-nearby[:, 0], nearby[:, 2]-nearby[:, 0])
            lengths = np.linalg.norm(n, axis=1)
            orientation = n@normal
            candidate = nearby[(offsets <= plane_tolerance) & (orientation >= lengths*(1-1e-5)) & (lengths > 1e-8)]
        else:
            candidate = actual
        parts = []
        for triangle in candidate:
            p = Polygon(np.c_[(triangle-origin)@u, (triangle-origin)@v])
            if p.area > 1e-12 and p.intersects(shape):
                parts.append(p)
        coverage = unary_union(parts) if parts else Polygon()
        absent = shape.difference(coverage).area
        missing += absent
        worst = max(worst, absent)
    return {'expected_area_m2': float(total), 'uncovered_area_m2': float(missing),
            'maximum_face_uncovered_m2': float(worst)}


def first_ray_hit(triangles, origin, direction, *, front_only=True):
    """Moller-Trumbore intersection on actual triangles, in metres."""
    triangles = np.asarray(triangles, dtype=float).reshape(-1, 3, 3)
    if not len(triangles):
        return None
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    d /= np.linalg.norm(d)
    a = triangles[:, 1]-triangles[:, 0]
    b = triangles[:, 2]-triangles[:, 0]
    h = np.cross(np.broadcast_to(d, b.shape), b)
    det = np.einsum('ij,ij->i', a, h)
    valid = det > 1e-10 if front_only else np.abs(det) > 1e-10
    inverse = np.divide(1., det, out=np.zeros_like(det), where=valid)
    s = o-triangles[:, 0]
    u = inverse*np.einsum('ij,ij->i', s, h)
    q = np.cross(s, a)
    v = inverse*(q@d)
    distance = inverse*np.einsum('ij,ij->i', b, q)
    valid &= (u >= -1e-8) & (v >= -1e-8) & (u+v <= 1+1e-8) & (distance > 1e-6)
    if not valid.any():
        return None
    indexes = np.flatnonzero(valid)
    index = indexes[np.argmin(distance[indexes])]
    return {'triangle': int(index), 'distance_m': float(distance[index]),
            'point_ac': (o+d*distance[index]).tolist()}


def terrain_section(ground, edge_ac):
    """Exact terrain breakpoints along a horizontal segment, no fixed sampling."""
    edge = np.asarray(edge_ac, dtype=float)
    a, b = edge[:, [0, 2]]
    vector = b-a
    length2 = float(vector@vector)
    if length2 < 1e-12:
        raise ValueError('Short terrain contact edge')
    line = LineString([a, b])
    fractions = [0., 1.]
    covered = []
    for index in ground.index.query(line, predicate='intersects'):
        shape = Polygon(ground.horizontal[index])
        cut = line.intersection(shape)
        pieces = getattr(cut, 'geoms', [cut])
        for part in pieces:
            if part.geom_type == 'LineString':
                interval=(np.asarray(part.coords)-a)@vector/length2
                covered.append((float(interval.min()),float(interval.max())))
                fractions.extend(interval.tolist())
            elif part.geom_type == 'Point':
                fractions.append(float((np.asarray(part.coords)[0]-a)@vector/length2))
    # Intersection endpoints are computed independently by GEOS. A second
    # 2-D line difference can mistake sub-ulp deviations from collinearity
    # for an uncovered whole segment. Coverage is inherently one-dimensional.
    end=0.;missing_fraction=0.
    for start,stop in sorted(covered):
        start=max(0.,start);stop=min(1.,stop)
        missing_fraction+=max(0.,start-end)
        end=max(end,stop)
    missing=(missing_fraction+max(0.,1-end))*line.length
    if missing > 1e-5:
        raise ValueError('Terrain does not cover building contact edge')
    t = np.unique(np.clip(fractions, 0., 1.))
    xz = a+t[:, None]*vector
    world = np.c_[xz[:, 0]+ground.origin[0], ground.origin[1]-xz[:, 1]]
    z = ground(world)-ground.height_origin
    return t, np.c_[xz[:, 0], z, xz[:, 1]]


def projection_overlap(triangles, reservation):
    """Actual horizontal area of an addition inside a protected polygon."""
    parts = []
    for triangle in np.asarray(triangles, dtype=float).reshape(-1, 3, 3):
        p = Polygon(triangle[:, [0, 2]])
        if p.is_valid and p.area > 1e-12 and p.intersects(reservation):
            parts.append(p.intersection(reservation))
    return float(unary_union(parts).area) if parts else 0.


def triangle_signature(triangles):
    """Canonical triangle multiset, preserving winding and float32 positions."""
    triangles = np.asarray(triangles, dtype=np.float32).reshape(-1, 3, 3)
    encoded = []
    for t in triangles:
        encoded.append(min(np.roll(t, shift, axis=0).tobytes() for shift in range(3)))
    return hashlib.sha256(b''.join(sorted(encoded))).hexdigest()


def horizontal_intrusion(triangles, reservation):
    """Area and length catch horizontal slabs and zero-width vertical walls."""
    from shapely.geometry import MultiPoint
    cuts = []
    for triangle in np.asarray(triangles, dtype=float).reshape(-1, 3, 3):
        projected = MultiPoint(triangle[:, [0, 2]]).convex_hull
        if projected.intersects(reservation):
            cuts.append(projected.intersection(reservation))
    result = unary_union(cuts)
    return {'area_m2': float(result.area), 'length_m': float(result.length)}


def wall_ground_closure(edge, actual, ground, normal, *, embed_depth=.02,index=None):
    """Close the whole vertical source-to-terrain silhouette, including dips."""
    edge = np.asarray(edge, dtype=float)
    normal = np.asarray(normal, dtype=float)
    normal[1] = 0.
    normal /= np.linalg.norm(normal)
    along = edge[1]-edge[0]
    along[1] = 0.
    length = np.linalg.norm(along)
    along /= length
    t, profile = terrain_section(ground, edge)
    top = edge[0, 1]+t*(edge[1, 1]-edge[0, 1])
    bottom = profile[:, 1]-embed_depth
    required = []
    for i in range(len(t)-1):
        # A source seam can cross terrain. Keep only its exposed subinterval.
        gap = top[i:i+2]-bottom[i:i+2]
        if gap.max() <= 0:
            continue
        lo, hi = 0., 1.
        if gap[0] < 0:
            lo = -gap[0]/(gap[1]-gap[0])
        if gap[1] < 0:
            hi = -gap[0]/(gap[1]-gap[0])
        f = np.array([lo, hi])
        x = (t[i]+f*(t[i+1]-t[i]))*length
        upper = top[i]+f*(top[i+1]-top[i])
        lower = bottom[i]+f*(bottom[i+1]-bottom[i])
        p = Polygon([(x[0],lower[0]), (x[1],lower[1]), (x[1],upper[1]), (x[0],upper[0])])
        if p.area > 1e-12:
            required.append(p)
    actual = np.asarray(actual, dtype=float).reshape(-1, 3, 3)
    lo=edge.min(axis=0)-.025; hi=edge.max(axis=0)+.025
    lo[1]=min(float(bottom.min()),lo[1])-.02
    if index is not None:
        actual=index.nearby(lo,hi)
    parts = []
    if len(actual):
        offset = np.abs((actual-edge[0])@normal).max(axis=1)
        n = np.cross(actual[:,1]-actual[:,0], actual[:,2]-actual[:,0])
        size = np.linalg.norm(n, axis=1)
        for triangle in actual[(offset < .025) & (n@normal > size*.9999) & (size > 1e-8)]:
            delta = triangle-edge[0]
            parts.append(Polygon(np.c_[delta@along, triangle[:,1]]))
    wanted = unary_union(required)
    observed = unary_union(parts)
    return {'required_closure_area_m2':float(wanted.area),
            'unclosed_area_m2':float(wanted.difference(observed).area),
            'terrain_breakpoints':len(t), 'embed_depth_checked_m':embed_depth,
            'maximum_source_ground_gap_m':float((top-profile[:,1]).max())}


def _local_polygon(points, origin):
    p = np.asarray(points, dtype=float)
    return Polygon(np.c_[p[:,0]-origin[0], origin[1]-p[:,1]])


def _local_shape(value, origin):
    """Accept the planner's polygon rings and the source GeoJSON separately."""
    from shapely.geometry import shape
    from shapely import transform
    if isinstance(value,dict) and 'type' in value:
        return transform(shape(value),lambda p:np.c_[p[:,0]-origin[0],origin[1]-p[:,1]])
    if value and isinstance(value[0],dict):
        pieces=[]
        for p in value:
            outer=np.asarray(p['outer']);holes=[np.asarray(h) for h in p.get('holes',[])]
            pieces.append(Polygon(np.c_[outer[:,0]-origin[0],origin[1]-outer[:,1]],
                                  [np.c_[h[:,0]-origin[0],origin[1]-h[:,1]] for h in holes]))
        return unary_union(pieces)
    return _local_polygon(value,origin)


def _catalog_footprints(catalog, origin):
    result = {}
    if not catalog:
        return result
    for building in catalog.get('buildings', []):
        parts = []
        for source in building.get('surfaces', []):
            if source.get('type') != 'roof':
                continue
            p = _local_polygon(np.asarray(source['vertices_utm32_height'])[:,:2], origin)
            if p.is_valid and p.area > .001:
                parts.append(p)
        if parts:
            result[building['building_id']] = unary_union(parts)
    return result


def audit_building_bases(model, metadata, ground=None, *, source_catalog=None,
                         baseline=None, ray_probes=None):
    checks = {}; groups = {}
    records = metadata.get('building_base_records')
    checks['building_base_records_present'] = isinstance(records, list)
    if not isinstance(records, list):
        return checks, groups
    origin = np.asarray(metadata['origin_utm32'], dtype=float)
    h0 = float(metadata['height_origin_m'])
    if ground is None:
        ground = ExportedTerrainSampler(triangles_for(model, '1GRASS_TERRAIN'), origin, h0)
    additions = {prefix:triangles_for(model, prefix) for prefix in BASE_PREFIXES}
    addition_indexes={prefix:TriangleIndex(t) for prefix,t in additions.items()}
    actual = np.concatenate(list(additions.values()))
    actual_meshes = [m for m in model['meshes'] if m['name'].startswith(BASE_PREFIXES)]
    checks['building_base_records_match_mesh_presence'] = bool(records) == bool(actual_meshes)
    checks['building_base_visible_noncolliding'] = all(m.get('visible') and m.get('renderable') and
                                                     not m['name'][0].isdigit() for m in actual_meshes)
    checks['building_base_finite_nondegenerate'] = bool(np.isfinite(actual).all() and
        (not len(actual) or np.all(np.linalg.norm(np.cross(actual[:,1]-actual[:,0],actual[:,2]-actual[:,0]),axis=1)>1e-7)))
    checks['building_base_legacy_strips_replaced'] = not any(m['name'].startswith('LOD2_FOUNDATIONS') for m in model['meshes'])
    identifiers = [r.get('id') for r in records]
    checks['building_base_unique_record_ids'] = all(isinstance(x,str) and x for x in identifiers) and len(set(identifiers))==len(identifiers)
    stats=metadata.get('building_base_stats',{})
    checks['building_base_stats_match_records']=(stats.get('panels')==sum(r.get('kind') in ('wall','plinth') for r in records)
        and stats.get('soffits')==sum(r.get('kind')=='soffit' for r in records)
        and stats.get('buildings')==len({r.get('building_id') for r in records}))
    footprints = _catalog_footprints(source_catalog, origin)
    original_contours=[]
    for source in metadata.get('building_base_source_footprints', []):
        contour=_local_shape(source['geometry'],origin)
        footprints[source['building_id']] = contour
        original_contours.append(contour)
    checks['building_base_neighbour_context_present'] = not records or bool(footprints)
    from shapely import STRtree
    footprint_ids=list(footprints)
    footprint_shapes=list(footprints.values())
    footprint_index=STRtree(footprint_shapes)
    road_parts = [Polygon(t[:,[0,2]]) for prefix in ('1ROAD_MAIN','JUNCTION_ASPHALT')
                  for t in triangles_for(model,prefix) if Polygon(t[:,[0,2]]).area>1e-12]
    shoulders=triangles_for(model,'1SHOULDER')
    shoulder_shape=unary_union([Polygon(t[:,[0,2]]) for t in shoulders
                               if Polygon(t[:,[0,2]]).area>1e-12]).buffer(-.001)
    # The pre-existing synthetic shoulder continues beneath some unchanged
    # official house outlines. Do not remove the desired basement closures
    # there. Only that original contour is exempt, never a new component's
    # footprint. The100-um seam accounts for actual KN5 float32 rounding;
    # measured outward remnants on this route are at most8.336um.
    original_area=unary_union(original_contours).buffer(.0001)
    inherited_shoulder=shoulder_shape.intersection(original_area)
    roads = unary_union([unary_union(road_parts).buffer(-.001),shoulder_shape.difference(original_area)])
    road_overlap = horizontal_intrusion(actual, roads)
    checks['building_base_clear_of_actual_road'] = road_overlap['area_m2']<=1e-6 and road_overlap['length_m']<=.001
    rows = []; seams = {}; reference_parts = {prefix:[] for prefix in BASE_PREFIXES}
    inherited_rows=[]
    shoulder_sampler=ExportedTerrainSampler(shoulders,origin,h0) if len(shoulders) else None
    bad_records = []
    for record in records:
        try:
            prefix = record['mesh_prefix']
            if prefix not in BASE_PREFIXES or record['kind'] not in ('wall','plinth','soffit'):
                raise ValueError('Unknown base kind or mesh prefix')
            points = stored_coordinates(record['vertices_utm_xyz'], origin, h0)
            faces = np.asarray(record['faces'], dtype=int)
            expected = points[faces]
            if faces.ndim!=2 or faces.shape[1]!=3 or not len(faces):
                raise ValueError('Missing reference faces')
            reference_parts[prefix].append(expected)
            lo = expected.min(axis=(0,1))-.001; hi = expected.max(axis=(0,1))+.001
            selected = addition_indexes[prefix].nearby(lo,hi)
            coverage = face_coverage(expected, selected)
            footprint = _local_shape(record['footprint_utm'], origin)
            # Test all actual selected vertices too: a far displaced face may
            # have no area inside the envelope of the permitted footprint.
            from shapely import covers, points as shapely_points
            if len(selected):
                selected=selected[covers(footprint.buffer(.0001),shapely_points(selected.mean(axis=1)[:,[0,2]]))]
            source_footprint=footprints.get(record['building_id'],footprint)
            in_source_footprint = bool(np.all(covers(source_footprint.buffer(.025),shapely_points(selected.reshape(-1,3)[:,[0,2]]))))
            # Spatial selection can include an adjacent shell's shared end
            # cap. The protected boundary is the original building outline;
            # exact component matching is already checked by forward/reverse
            # oriented surface coverage, not a second ambiguous assignment.
            in_footprint=in_source_footprint
            neighbour_parts = [footprint_shapes[i].difference(source_footprint.buffer(.002))
                               for i in footprint_index.query(footprint.buffer(.1)) if footprint_ids[i]!=record['building_id']]
            neighbour = horizontal_intrusion(selected, unary_union(neighbour_parts).buffer(-.001))
            row = {'id':record['id'], 'building_id':record['building_id'], 'kind':record['kind'],
                   'coverage':coverage,'within_source_footprint':in_footprint,
                   'neighbour_intrusion':neighbour}
            row['passed'] = coverage['uncovered_area_m2']<=1e-5 and in_footprint and neighbour['area_m2']<=1e-6 and neighbour['length_m']<=.001
            rows.append(row)
            inherited_part=footprint.intersection(inherited_shoulder)
            if shoulder_sampler is not None and inherited_part.area>1e-10:
                # Use actual stored faces which exactly correspond to this
                # component, excluding an adjacent body's shared end cap.
                keys={_oriented_face_key(t) for t in expected}
                matched=np.array([t for t in selected if _oriented_face_key(t) in keys])
                if len(matched):
                    n=np.cross(matched[:,1]-matched[:,0],matched[:,2]-matched[:,0])
                    horizontal=matched[np.abs(n[:,1])>1e-7]
                    if len(horizontal):
                        from scripts.verify_street_contacts import contact_vertices
                        samples,_=contact_vertices(horizontal,shoulder_sampler)
                        world=np.c_[samples[:,0]+origin[0],origin[1]-samples[:,2]]
                        delta=samples[:,1]+h0-shoulder_sampler(world)
                        inherited_rows.append({'id':record['id'],'building_id':record['building_id'],
                            'kind':record['kind'],'minimum_delta_m':float(delta.min()),
                            'maximum_delta_m':float(delta.max())})
            if record['kind'] in ('wall','plinth') and 'source_edge_xyz' in record:
                edge = stored_coordinates(record['source_edge_xyz'], origin, h0)
                key = (record['building_id'], tuple(sorted(map(tuple,edge.round(5)))))
                if 'outward_xyz' in record:
                    n=np.asarray(record['outward_xyz'],dtype=float);normal=np.array([n[0],n[2],-n[1]])
                else:
                    normal = np.cross(expected[0,1]-expected[0,0],expected[0,2]-expected[0,0])
                if abs(normal[1]) < np.linalg.norm(normal)*.02:
                    seams[key] = {'edge':edge,'normal':normal,'building_id':record['building_id'],
                                  'embed':min(.12,float(record.get('embed_depth_m',.15))-.005)}
        except (KeyError, ValueError, TypeError, IndexError) as exc:
            bad_records.append({'id':record.get('id'),'error':str(exc)})
    checks['building_base_record_schema_valid'] = not bad_records
    checks['building_base_record_surfaces_closed'] = not bad_records and all(r['coverage']['uncovered_area_m2']<=1e-5 for r in rows)
    checks['building_base_within_source_footprints'] = all(r['within_source_footprint'] for r in rows)
    checks['building_base_clear_of_neighbour_additions'] = all(r['neighbour_intrusion']['area_m2']<=1e-6 and r['neighbour_intrusion']['length_m']<=.001 for r in rows)
    unassigned = []
    for prefix, pieces in reference_parts.items():
        expected = np.concatenate(pieces) if pieces else np.empty((0,3,3))
        # Reverse coverage catches extra faces, not just missing planned ones.
        if len(additions[prefix]):
            unassigned.append(face_coverage(additions[prefix], expected))
    checks['building_base_all_exported_faces_accounted_for'] = all(r['uncovered_area_m2']<=1e-5 for r in unassigned)
    seam_rows = []
    vertical = np.concatenate([additions['BASEMENT_WALLS'],additions['BASEMENT_PLINTHS']])
    vertical_index=TriangleIndex(vertical)
    for seam in seams.values():
        try:
            result = wall_ground_closure(seam['edge'],vertical,ground,seam['normal'],embed_depth=seam['embed'],index=vertical_index)
            result['building_id'] = seam['building_id']
            seam_rows.append(result)
        except ValueError as exc:
            seam_rows.append({'building_id':seam['building_id'],'error':str(exc),'unclosed_area_m2':None})
    checks['building_base_source_to_terrain_closed'] = all('error' not in r and r['unclosed_area_m2']<=1e-5 for r in seam_rows)
    checks['building_base_source_ground_gap_bounded'] = all('error' not in r and r['maximum_source_ground_gap_m']<=6.0002 for r in seam_rows)
    ray_rows = []
    for probe in ray_probes or []:
        camera = np.asarray(probe['camera_ac'],dtype=float)
        target = np.asarray(probe['target_ac'],dtype=float)
        hit = first_ray_hit(actual,camera,target-camera)
        blocked = hit is not None and hit['distance_m']<np.linalg.norm(target-camera)-.01
        ray_rows.append({'id':probe.get('id'),'blocked_before_old_backface':bool(blocked),'hit':hit})
    if ray_probes is not None:
        checks['building_base_known_open_view_closed'] = bool(ray_rows) and all(r['blocked_before_old_backface'] for r in ray_rows)
    original_results=[]
    if baseline is not None:
        baseline_hashes=baseline.get('mesh_stored_geometry_sha256',{})
        by_name={m['name']:m for m in model['meshes']}
        for name,expected_hash in baseline_hashes.items():
            mesh=by_name.get(name)
            actual_hash=stored_mesh_hash(mesh) if mesh is not None else None
            original_results.append({'mesh':name,'unchanged':actual_hash==expected_hash})
        checks['building_base_original_wall_roof_photo_geometry_preserved']=bool(original_results) and all(r['unchanged'] for r in original_results)
        previous=baseline.get('road_reference',{})
        road_fields=('centerline_ac','road_width_profile_m','length_m','origin_utm32','height_origin_m')
        checks['building_base_original_road_reference_preserved']=all(k in previous and k in metadata and
            np.asarray(previous[k]).shape==np.asarray(metadata[k]).shape and
            np.allclose(previous[k],metadata[k],atol=1e-8,rtol=0) for k in road_fields)
    groups.update(records=rows, invalid_records=bad_records, source_seams=seam_rows, road_intrusion=road_overlap,
                  known_open_views=ray_rows, neighbour_footprints_available=len(footprints),
                  original_geometry=original_results,
                  counts={kind:sum(r.get('kind')==kind for r in records) for kind in ('wall','plinth','soffit')})
    inherited=horizontal_intrusion(actual,inherited_shoulder)
    groups['inherited_shoulder_under_original_buildings']={**inherited,
        'source_contour_serialization_margin_m':.0001,
        'building_count':len({r['building_id'] for r in inherited_rows}),
        'building_ids':sorted({r['building_id'] for r in inherited_rows}),
        'components':inherited_rows,
        'minimum_delta_m':min((r['minimum_delta_m'] for r in inherited_rows),default=None),
        'maximum_delta_m':max((r['maximum_delta_m'] for r in inherited_rows),default=None),
        'limitation':'Existing synthetic 0.4m shoulder beneath unchanged official building outlines; main road and junction asphalt remain fully reserved; new shoulder intrusion beyond the original outlines is rejected.'}
    return checks, groups


def stored_mesh_hash(mesh):
    payload=(np.asarray(mesh['positions'],dtype='<f4').tobytes()
             +np.asarray(mesh['indices'],dtype='<u2').tobytes()
             +np.asarray(mesh['uv'],dtype='<f4').tobytes())
    return hashlib.sha256(payload).hexdigest()


def _saved_ray_probes(root,metadata):
    comparison=root/'test-results/black-facade-comparison-v010.json'
    sources=root/'test-results/black-facade-source-comparison-v010.json'
    if not comparison.is_file() or not sources.is_file():
        return None
    data=json.loads(comparison.read_text(encoding='utf-8'))
    backfaces={tuple(r['pixel']) for r in json.loads(sources.read_text(encoding='utf-8')) if r['viewed_from_back']}
    probes=[]
    for row in data['rows']:
        if row['version']!='v010' or tuple(row['pixel']) not in backfaces:
            continue
        target=next((h for h in row['hits'] if h['mesh'].startswith('LOD2_WALLS')),None)
        if target is not None:
            p=stored_coordinates([target['point_world']],metadata['origin_utm32'],metadata['height_origin_m'])[0]
            probes.append({'id':str(row['pixel']),'camera_ac':data['camera_ac'],'target_ac':p.tolist()})
    return probes


def verify(root=ROOT, *, output=None, use_baseline=True, use_known_rays=True):
    root=Path(root).resolve()
    config=json.loads((root/'tracks/altenhain.json').read_text(encoding='utf-8'))
    scene=json.loads((root/'data/altenhain/scene.json').read_text(encoding='utf-8'))
    metadata=scene['metadata']
    model=inspect(root/'build'/config['id']/(config['id']+'.kn5'),geometry=True)
    catalog_path=root/'data/altenhain/building_surfaces.json'
    catalog=json.loads(catalog_path.read_text(encoding='utf-8')) if catalog_path.is_file() else None
    baseline_path=root/'test-results/original-buildings-v010-baseline.json'
    baseline=json.loads(baseline_path.read_text(encoding='utf-8')) if use_baseline and baseline_path.is_file() else None
    probes=_saved_ray_probes(root,metadata) if use_known_rays else None
    checks,groups=audit_building_bases(model,metadata,source_catalog=catalog,baseline=baseline,ray_probes=probes)
    if baseline is not None:
        hashes=baseline.get('mesh_geometry_sha256',{})
        current={m['name']:hashlib.sha256(json.dumps({key:m[key] for key in ('vertices','faces','uv')},
            sort_keys=True,separators=(',',':')).encode()).hexdigest() for m in scene['meshes'] if m['name'] in hashes}
        checks['building_base_original_scene_geometry_preserved']=bool(hashes) and all(current.get(k)==v for k,v in hashes.items())
    report={'version':config['version'],'passed':all(checks.values()),'checks':checks,'groups':groups,
            'measurement':'Actual KN5 float32 triangles; oriented face union, exact terrain-edge intersections and saved view rays',
            'game_test_performed':False}
    tag='v'+''.join(config['version'].split('.')[:2])
    path=Path(output) if output else root/'test-results'/f'building-bases-{tag}.json'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(report,indent=2,allow_nan=False),encoding='utf-8')
    print(f'{sum(checks.values())}/{len(checks)} building-base checks passed; report: {path}')
    for key,value in checks.items():
        if not value:print('FAIL:',key)
    return report['passed']


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--root',type=Path,default=ROOT)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--no-baseline',action='store_true',help='Skip full-route v0.10 original mesh baseline for isolated crops')
    parser.add_argument('--no-known-rays',action='store_true',help='Skip saved 530-m camera probes for isolated crops')
    args=parser.parse_args()
    raise SystemExit(0 if verify(args.root,output=args.output,use_baseline=not args.no_baseline,use_known_rays=not args.no_known_rays) else 1)
