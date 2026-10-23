"""Separate inferred basement shells on original LoD2 lower exterior contours.

Source walls and roofs are never moved. Terrain supplies the bottom boundary;
existing source wall surfaces exclude storey seams and shared internal walls.
All geometry in plans is EPSG:25832 XY plus the source absolute height datum.
"""
from __future__ import annotations

import hashlib
import numpy as np
from shapely import get_coordinates, constrained_delaunay_triangles
from shapely.geometry import Polygon, MultiPolygon, LineString, Point
from shapely.geometry.polygon import orient
from shapely.ops import unary_union


PROVENANCE=('Inferred basement/plinth closure between original LoD2 lower exterior '
            'edges and exported terrain; not surveyed basements or foundations. '
            'Original source wall and roof coordinates remain unchanged.')
PREFIXES={'wall':'BASEMENT_WALLS','plinth':'BASEMENT_PLINTHS','soffit':'BASEMENT_SOFFITS'}


def _source_faces(source_faces):
    rows=[]
    for identifier,rings in source_faces:
        rings=[np.asarray(r,dtype=float) for r in rings]
        if not rings or any(r.ndim!=2 or r.shape[1]!=3 or len(r)<3 or not np.isfinite(r).all() for r in rings):
            continue
        p=rings[0]
        if len(p)>1 and np.linalg.norm(p[0]-p[-1])<1e-7:p=p[:-1]
        if len(p)<3:continue
        relative=p-p[0]
        normal=np.sum(np.cross(relative,np.roll(relative,-1,axis=0)),axis=0)
        length=float(np.linalg.norm(normal))
        if length<1e-8:continue
        normal/=length
        row={'id':str(identifier),'rings':rings,'normal':normal,'origin':p[0]}
        if abs(normal[2])<.02:
            horizontal=np.array([-normal[1],normal[0],0.]);horizontal/=np.linalg.norm(horizontal)
            project=lambda r:np.c_[r@horizontal,r[:,2]]
            shape=Polygon(project(rings[0]),[project(r) for r in rings[1:]])
            if not shape.is_valid or shape.area<1e-6:continue
            row.update(horizontal=horizontal,shape=shape)
        rows.append(row)
    return rows


def source_footprint(source_faces):
    """Return real source ground polygons (roof polygons only as a fallback).

    Polygon holes and disjoint building parts are retained. No convex hull or
    inferred rectangular lot is used as a replacement for a source contour.
    """
    rows=_source_faces(source_faces)
    for downward in (True,False):
        pieces=[]
        for row in rows:
            nz=row['normal'][2]
            if (nz>=-.25 if downward else nz<=.25):continue
            rings=row['rings'];p=Polygon(rings[0][:,:2],[r[:,:2] for r in rings[1:]])
            if p.is_valid and p.area>1e-6:pieces.append(p)
        if pieces:return unary_union(pieces)
    return Polygon()


def _polygon_coordinates(shape):
    parts=[shape] if isinstance(shape,Polygon) else list(shape.geoms)
    return [{'outer':np.asarray(p.exterior.coords).tolist(),
             'holes':[np.asarray(r.coords).tolist() for r in p.interiors]} for p in parts]


def _terrain_stations(a,b,sampler):
    """Source-edge fractions at every actual exported terrain triangle edge."""
    a=np.asarray(a);b=np.asarray(b);delta=b[:2]-a[:2];length=float(np.linalg.norm(delta))
    values=list(np.linspace(0,1,max(1,int(np.ceil(length/.6)))+1))
    if all(hasattr(sampler,k) for k in ('index','horizontal','origin')):
        origin=np.asarray(sampler.origin)
        local=np.array([[a[0]-origin[0],origin[1]-a[1]],
                        [b[0]-origin[0],origin[1]-b[1]]])
        line=LineString(local);d=local[1]-local[0];denominator=float(d@d)
        for index in sampler.index.query(line):
            cross=line.intersection(Polygon(sampler.horizontal[int(index)]).boundary)
            for p in get_coordinates(cross):values.append(float(np.clip((p-local[0])@d/denominator,0,1)))
    values=np.unique(np.round(values,11))
    # Sub-millimetre overlay fragments cannot export stable wall normals.
    # Their terrain variation remains covered by the 15-cm buried foot.
    keep=[float(values[0])]
    for value in values[1:-1]:
        if (value-keep[-1])*length>=.002:keep.append(float(value))
    if (1-keep[-1])*length<.002 and len(keep)>1:keep.pop()
    return np.array([*keep,1.])


def _exposed_intervals(row,a,b,walls):
    """Reject portions of a lower edge which have another source wall below."""
    u=row['horizontal'];normal=row['normal'];probe=np.array([a,b]).copy();probe[:,2]-=.025
    line=LineString(np.c_[probe@u,probe[:,2]]);xy=np.asarray(line.coords);delta=xy[1]-xy[0]
    candidates=[];cuts=[0.,1.]
    for other in walls:
        if abs(float(normal@other['normal']))<.999 or abs(float((a-other['origin'])@normal))>.025:
            continue
        # Project the other wall into this wall's frame, including holes.
        rings=other['rings'];project=lambda r:np.c_[r@u,r[:,2]]
        polygon=Polygon(project(rings[0]),[project(r) for r in rings[1:]])
        if not polygon.is_valid:continue
        candidates.append(polygon)
        for p in get_coordinates(line.intersection(polygon.boundary)):
            cuts.append(float(np.clip((p-xy[0])@delta/(delta@delta),0,1)))
    cuts=np.unique(np.round(cuts,10));result=[]
    for lo,hi in zip(cuts[:-1],cuts[1:]):
        middle=Point(xy[0]+delta*(lo+hi)/2)
        if not any(p.covers(middle) for p in candidates):result.append((float(lo),float(hi)))
    return result


def _clipped_shell(shape,reference):
    """Close a clipped wall body on its unchanged lower/upper affine planes."""
    origin=reference[0,:2]
    basis=np.array([reference[1,:2]-origin,reference[4,:2]-origin])
    lower=np.linalg.solve(basis,reference[[1,4],2]-reference[0,2])
    upper=np.linalg.solve(basis,reference[[2,7],2]-reference[3,2])
    vertices=[];faces=[]
    def points(xy,top):
        xy=np.asarray(xy)
        z=(xy-origin)@(upper if top else lower)+reference[3 if top else 0,2]
        return np.c_[xy,z]
    def triangle(p):
        if np.linalg.norm(np.cross(p[1]-p[0],p[2]-p[0]))<1e-7:return
        i=len(vertices);vertices.extend(p.tolist());faces.append([i,i+1,i+2])
    parts=[shape] if isinstance(shape,Polygon) else list(shape.geoms)
    for polygon in parts:
        polygon=orient(polygon,sign=1.)
        for t in constrained_delaunay_triangles(polygon).geoms:
            xy=np.asarray(t.exterior.coords)[:3]
            d=xy-xy[0]
            if d[1,0]*d[2,1]-d[1,1]*d[2,0]<0:xy=xy[::-1]
            triangle(points(xy,False)[::-1]);triangle(points(xy,True))
        for ring in [polygon.exterior,*polygon.interiors]:
            xy=np.asarray(ring.coords)
            for a,b in zip(xy[:-1],xy[1:]):
                bottom=points([a,b],False);top=points([a,b],True)
                triangle(np.array([bottom[0],bottom[1],top[1]]))
                triangle(np.array([bottom[0],top[1],top[0]]))
    return np.asarray(vertices),np.asarray(faces,dtype=int)


def _wall_box(edge,lower,upper,normal,thickness=.12,uv_origin=None,footprint_limit=None):
    """A thin closed shell extending inward from the unchanged wall plane."""
    xy=np.asarray(edge)[:,:2];lower=np.asarray(lower);upper=np.asarray(upper)
    front=np.array([[xy[0,0],xy[0,1],lower[0]],[xy[1,0],xy[1,1],lower[1]],
                    [xy[1,0],xy[1,1],upper[1]],[xy[0,0],xy[0,1],upper[0]]])
    back=front-np.asarray(normal)*thickness;vertices=np.vstack([front,back])
    quads=((0,1,2,3),(7,6,5,4),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0))
    center=vertices.mean(axis=0);faces=[]
    for q in quads:
        for face in ((q[0],q[1],q[2]),(q[0],q[2],q[3])):
            p=vertices[list(face)];cross=np.cross(p[1]-p[0],p[2]-p[0])
            if np.linalg.norm(cross)<1e-7:continue
            if cross@(p.mean(axis=0)-center)<0:face=face[::-1]
            faces.append(list(face))
    footprint=Polygon([front[0,:2],front[1,:2],back[1,:2],back[0,:2]])
    front_on_source=(footprint_limit is not None and
                     footprint_limit.buffer(1e-7).covers(LineString(front[:2,:2])))
    # Some source wall/ground surfaces disagree by millimetres. That does not
    # authorize moving the original wall: this corner repair applies only
    # when its unchanged front edge is already supported by the source outline.
    if front_on_source and footprint.difference(footprint_limit).area>1e-9:
        clipped=footprint.intersection(footprint_limit)
        if isinstance(clipped,(Polygon,MultiPolygon)) and clipped.area>1e-9:
            # An inward offset can cross an acute or re-entrant source corner.
            # Cut only that body; preserve the original visible wall plane and
            # both height planes instead of moving the source/front edge.
            vertices,faces=_clipped_shell(clipped,vertices)
            footprint=clipped
        else:
            return vertices,np.empty((0,3),dtype=int),np.zeros((len(vertices),2)),clipped
    horizontal=np.array([-normal[1],normal[0],0.]);horizontal/=np.linalg.norm(horizontal)
    # Every interval and both material bands share the original source-wall
    # anchor. Resetting at each terrain station would visibly repeat the scan.
    anchor=np.asarray(edge[0] if uv_origin is None else uv_origin,dtype=float)
    uv=np.c_[(vertices-anchor)@horizontal/3.,-(vertices[:,2]-anchor[2])/1.5]
    return vertices,np.asarray(faces,dtype=int),uv,footprint


def _blocked(footprint,reservation,neighbours):
    if reservation is not None and not reservation.is_empty and footprint.intersection(reservation).area>1e-6:
        return True
    return any(footprint.intersection(p).area>1e-6 for p in neighbours)


def plan_building_base(building_id,source_faces,ground_sampler,*,road_reservation=None,
                       neighbour_footprints=(),max_gap_m=6.):
    """Plan new lower shells for one building, retaining all original surfaces.

    Existing source ground faces become explicit soffits. Exterior lower edges
    receive inward-thickened basement/plinth bodies only where above terrain.
    Upper-storey seams, hole rims, neighbours and road reservations are excluded.
    """
    if not callable(ground_sampler) or not np.isfinite(max_gap_m) or not 0<max_gap_m<=6.:
        raise ValueError('Invalid building-base terrain sampler or gap limit')
    rows=_source_faces(source_faces);footprint=source_footprint(source_faces)
    panels=[];soffits=[];records=[];skipped=[];covered_edges=[];maximum_gap=0.
    walls=[r for r in rows if 'shape' in r]
    neighbours=list(neighbour_footprints)
    if footprint.is_empty:return {'panels':[],'soffits':[],'records':[],'stats':{},'footprint':footprint}
    def append(kind,row,vertices,faces,uv,shape,source_edge=None,contact=None):
        world=np.asarray(vertices,dtype=float)
        key=str(building_id)+'|'+row['id']+'|'+kind+'|'+repr(world.round(5).tolist())
        identifier='base_'+hashlib.sha256(key.encode()).hexdigest()[:20]
        record={'id':identifier,'building_id':str(building_id),'source_surface_id':row['id'],
                'kind':kind,'mesh_prefix':PREFIXES[kind],'vertices_utm_xyz':world.tolist(),
                'faces':np.asarray(faces,dtype=int).tolist(),'footprint_utm':_polygon_coordinates(shape),
                'outward_xyz':row['normal'].tolist(),'source':PROVENANCE}
        if source_edge is not None:record['source_edge_xyz']=np.asarray(source_edge).tolist()
        if contact is not None:
            record.update(ground_contact_xy=np.asarray(contact)[:,:2].tolist(),
                          ground_contact_z=np.asarray(contact)[:,2].tolist(),embed_depth_m=.15)
        component=dict(record,uv=np.asarray(uv).tolist())
        (soffits if kind=='soffit' else panels).append(component);records.append(record)
    # The existing LoD2 downward surfaces are the exact missing lower lids.
    # Render them separately instead of modifying any original wall or roof.
    from roadtoassetto.buildings import triangulate_face
    for row in sorted(rows,key=lambda r:r['id']):
        if row['normal'][2]>=-.25:continue
        p=Polygon(row['rings'][0][:,:2],[r[:,:2] for r in row['rings'][1:]])
        if not p.is_valid or p.area<1e-5 or _blocked(p,road_reservation,neighbours):continue
        try:v,f,n=triangulate_face(row['rings'])
        except ValueError:continue
        append('soffit',row,v,f,(v[:,:2]-v[0,:2])/3.,p)
    for row in sorted(walls,key=lambda r:r['id']):
        ring=row['rings'][0];normal=row['normal'];u=row['horizontal']
        for a,b in zip(ring[:-1],ring[1:]):
            length=float(np.linalg.norm(b[:2]-a[:2]))
            if length<.15:continue
            midpoint=(a+b)/2;x=float(midpoint@u)
            if not row['shape'].covers(Point(x,midpoint[2]+.025)) or row['shape'].covers(Point(x,midpoint[2]-.025)):
                continue
            # Interior/party-wall edges are not part of the exterior shell.
            if footprint.boundary.distance(Point(midpoint[:2]))>.035:continue
            if footprint.contains(Point(midpoint[:2]+normal[:2]*.025)):continue
            for start,end in _exposed_intervals(row,a,b,walls):
                edge=np.array([a+(b-a)*start,a+(b-a)*end]);edge_length=np.linalg.norm(edge[1,:2]-edge[0,:2])
                if edge_length<.01:continue
                stations=_terrain_stations(edge[0],edge[1],ground_sampler)
                points=edge[0]+stations[:,None]*(edge[1]-edge[0])
                ground=np.asarray(ground_sampler(points[:,:2]),dtype=float)
                if ground.shape!=(len(points),) or not np.isfinite(ground).all():
                    raise ValueError('Invalid exported terrain at building-base edge')
                for k in range(len(points)-1):
                    segment=points[k:k+2];z=ground[k:k+2];gap=segment[:,2]-z
                    if gap.max()<=.003:continue
                    if gap.max()>max_gap_m:
                        skipped.append({'source_surface_id':row['id'],'reason':'gap exceeds bounded basement height',
                                        'source_edge_xyz':segment.tolist(),'maximum_gap_m':float(gap.max())});continue
                    lo,hi=0.,1.
                    if gap[0]<=0:lo=float(-gap[0]/(gap[1]-gap[0]))
                    if gap[1]<=0:hi=float(-gap[0]/(gap[1]-gap[0]))
                    if (hi-lo)*np.linalg.norm(segment[1,:2]-segment[0,:2])<.002:continue
                    fraction=np.array([lo,hi]);e=segment[0]+fraction[:,None]*(segment[1]-segment[0]);g=z[0]+fraction*(z[1]-z[0])
                    # A low, terrain-following mineral plinth; the taller
                    # upper part keeps the matching house's plaster family.
                    split=np.minimum(e[:,2],g+.42);contact=np.c_[e[:,:2],g]
                    for kind,lower,upper in (('plinth',g-.15,split),('wall',split,e[:,2])):
                        if np.max(upper-lower)<.005:continue
                        v,f,uv,p=_wall_box(e,lower,upper,normal,uv_origin=row['origin'],footprint_limit=footprint)
                        if p.is_empty or not p.is_valid or not len(f) or _blocked(p,road_reservation,neighbours):
                            skipped.append({'source_surface_id':row['id'],'reason':'road or neighbour reservation',
                                            'source_edge_xyz':e.tolist()});continue
                        append(kind,row,v,f,uv,p,e,contact if kind=='plinth' else None)
                    maximum_gap=max(maximum_gap,float(np.max(e[:,2]-g)))
                    covered_edges.append(e.tolist())
    stats={'building_base_panels':len(panels),'building_base_soffits':len(soffits),
           'building_base_maximum_gap_m':round(maximum_gap,4),'building_base_rejected_intervals':len(skipped),
           'building_base_provenance':PROVENANCE}
    return {'panels':panels,'soffits':soffits,'records':records,'stats':stats,'footprint':footprint,
            'skipped':skipped,'source_footprint_utm':_polygon_coordinates(footprint)}


def emit_building_base(plan,material_ids,append,ac):
    """Emit into build_lod2's existing grouped-mesh callback and materials."""
    for component in [*plan['panels'],*plan['soffits']]:
        p=np.asarray(component['vertices_utm_xyz']);kind=component['kind']
        append(PREFIXES[kind],material_ids[kind],ac(p[:,:2],p[:,2]),
               component['faces'],np.asarray(component['uv']))
    return plan['stats']
