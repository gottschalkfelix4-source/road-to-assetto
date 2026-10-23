"""Inferred hillside construction tied to real lower LoD2 wall edges.

Plans use absolute EPSG:25832 coordinates and source elevations. They are
construction hypotheses, not observations of property boundaries or entrances.
The terrain plan accompanies every visible platform and retaining wall.
"""
from __future__ import annotations

import numpy as np
import hashlib
from shapely import STRtree, get_coordinates
from shapely.geometry import Polygon, Point, LineString, box
from shapely.ops import unary_union
from shapely.prepared import prep


PROVENANCE = ('Inferred hillside construction from lowest official LoD2 wall edges, '
              'existing road/pavement grades and DGM; not surveyed entrances, '
              'property boundaries, walls, stairs or driveways. Road and LoD2 '
              'coordinates remain unchanged.')


def _quad(a, b, near, far, *, id, kind, assembly_id, style='pavers'):
    xy=np.array([a[0],a[1],b[1],b[0]],dtype=float)
    top=np.r_[np.broadcast_to(near,(2,)),np.broadcast_to(far,(2,))[::-1]]
    return dict(id=id,xy=xy.tolist(),top=top.tolist(),faces=[[0,1,2],[0,2,3]],
                kind=kind,style=style,assembly_id=assembly_id,source=PROVENANCE,
                inner_edge_indices=[0,1],outer_edge_indices=[3,2])


def _levels(section,points):
    """Interpolate a planned piecewise plane, including its boundary."""
    xy=np.array(section['xy']);z=np.array(section['top']);p=np.asarray(points)
    result=np.full(len(p),np.nan)
    for face in section['faces']:
        q=xy[face];a=q[1]-q[0];b=q[2]-q[0]
        uv=np.linalg.solve(np.array([a,b]).T,(p-q[0]).T).T
        valid=(uv[:,0]>=-1e-6)&(uv[:,1]>=-1e-6)&(uv.sum(axis=1)<=1+1e-6)
        result[valid]=z[face[0]]+uv[valid,0]*(z[face[1]]-z[face[0]])+uv[valid,1]*(z[face[2]]-z[face[0]])
    return result


def _horizontal_ground_support(section,margin=.004):
    """Expand only a hidden level support, retaining the original top plane.

    A visible step remains unsnapped while terrain contours use a 2-mm grid.
    The small overlap keeps its low tread plane beneath the complete visible
    edge; shared support samples continue to choose the lowest adjacent plane.
    """
    xy=np.asarray(section['xy'],dtype=float);top=np.asarray(section['top'],dtype=float)
    if (xy.shape!=(4,2) or top.shape!=(4,) or np.ptp(top)>1e-8
            or not np.isfinite(np.r_[xy.ravel(),top]).all()):
        raise ValueError('Expected a horizontal quadrilateral ground support')
    expanded=np.asarray(Polygon(xy).buffer(margin,join_style=2).exterior.coords)[:-1]
    if expanded.shape!=xy.shape:
        raise ValueError('Ground support expansion changed its corner topology')
    # Preserve the original index order, triangle diagonal and inner edge.
    order=np.argmin(np.sum((xy[:,None,:]-expanded[None,:,:])**2,axis=2),axis=1)
    if len(np.unique(order))!=len(xy):
        raise ValueError('Ground support expansion has ambiguous corners')
    return dict(section,xy=expanded[order].tolist())


def surface_slopes(section,tangent,outward):
    """Actual plane gradients, rather than endpoint longitudinal estimates."""
    xy=np.asarray(section['xy']);z=np.asarray(section['top'])
    values=[]
    for face in section['faces']:
        delta=xy[np.asarray(face)[1:]]-xy[face[0]]
        gradient=np.linalg.solve(delta,z[np.asarray(face)[1:]]-z[face[0]])
        values.append([abs(float(gradient@outward)),abs(float(gradient@tangent)),float(np.linalg.norm(gradient))])
    return np.max(values,axis=0)


def _clear_length(center,tangent,outward,width,reservation,limit=11.):
    """Largest clear access rectangle, with a short gap to public paving."""
    start=np.array([center-tangent*width/2,center+tangent*width/2])
    def clear(depth):
        p=Polygon([start[0],start[1],start[1]+outward*depth,start[0]+outward*depth])
        return p.is_valid and not reservation.intersects(p)
    if not clear(.25):return 0.
    if clear(limit):return limit
    low=.25;high=limit
    for _ in range(20):
        middle=(low+high)/2
        if clear(middle):low=middle
        else:high=middle
    return max(0.,low-.055)


def _finish_plan(surfaces,stairs,edge,outward,ground_sampler,reservation,other_buildings,assembly_id):
    """Bind exposed edges to old ground inside the retaining wall footprint."""
    footprint=unary_union([Polygon(s['xy']) for s in surfaces])
    interior=prep(footprint.buffer(.004))
    walls=[];grade=[_horizontal_ground_support(s) if s['kind']=='stair' else dict(s) for s in surfaces]
    # Close the 18-mm visual joint at the original house plane, without moving
    # any wall. The hidden 40-mm overlap prevents a thin untouched DGM ridge.
    terrace=grade[0]
    terrace['xy']=np.asarray(terrace['xy']).copy().tolist()
    for k in (0,1):
        terrace['xy'][k]=(np.array(terrace['xy'][k])-outward*.058).tolist()
    maximum_cut=0.;maximum_fill=0.
    for section in surfaces:
        xy=np.asarray(section['xy']);top=np.asarray(section['top'])
        sample=np.vstack([xy,xy.mean(axis=0)])
        desired=np.r_[top,float(_levels(section,[xy.mean(axis=0)])[0])]-.035
        adjustment=desired-np.asarray(ground_sampler(sample))
        maximum_cut=max(maximum_cut,float(-adjustment.min()))
        maximum_fill=max(maximum_fill,float(adjustment.max()))
        if maximum_cut>2.5 or maximum_fill>1.8:return None
        polygon=Polygon(xy)
        other_surface_area=unary_union([Polygon(s['xy']) for s in surfaces if s is not section])
        for i in range(len(xy)):
            j=(i+1)%len(xy)
            if section is surfaces[0] and {i,j}=={0,1}:continue
            delta=xy[j]-xy[i];length=float(np.linalg.norm(delta))
            normal=np.array([-delta[1],delta[0]])/length
            middle=(xy[i]+xy[j])/2
            if polygon.covers(Point(middle+normal*.005)):normal=-normal
            # Short segments follow source grade without extruding one huge
            # wall over a varying hillside. Shared endpoints remain identical.
            ts=list(np.linspace(0,1,max(1,int(np.ceil(length/1.4)))+1))
            # Add the exact access opening endpoints before wall subdivision.
            # A 1.15-m stair must not inherit a 1.4-m wall segment across it.
            shared=LineString([xy[i],xy[j]]).intersection(other_surface_area.buffer(.001))
            for point in get_coordinates(shared):
                ts.append(float(np.clip((point-xy[i])@delta/(length*length),0,1)))
            ts=np.unique(np.round(ts,10))
            for low,high in zip(ts[:-1],ts[1:]):
                if (high-low)*length<.015:continue
                inner=xy[i]+np.array([low,high])[:,None]*delta
                inner_z=top[i]+np.array([low,high])*(top[j]-top[i])
                if interior.covers(Point(inner.mean(axis=0)+normal*.018)):continue
                outer=inner+normal*.24
                belt=Polygon([inner[0],inner[1],outer[1],outer[0]])
                if (interior.contains(Point(outer.mean(axis=0))) or
                        other_surface_area.buffer(.006).intersects(belt) or
                        reservation.intersects(belt.buffer(.025)) or other_buildings.intersects(belt.buffer(.025))):
                    continue
                ground=np.asarray(ground_sampler(outer),dtype=float)
                height=np.abs(inner_z-.035-ground)
                if height.max()<.24:continue
                if height.max()>2.5:return None
                wall_id=f'{assembly_id}_wall_{len(walls):03d}'
                walls.append(dict(id=wall_id,xy=inner.tolist(),top=(np.maximum(inner_z,ground)+.10).tolist(),
                                  bottom=(np.minimum(inner_z,ground)-.16).tolist(),
                                  width_m=.24,outward=normal.tolist(),assembly_id=assembly_id,
                                  kind='retaining',source=PROVENANCE))
                # Continuous terrain transitions across the 24-cm body; the
                # construction mask, rather than a decorative wall on a hill,
                # establishes both the cut/fill level and the outside DGM tie.
                strip=_quad(inner,outer,inner_z,ground+.035,id=wall_id+'_ground',
                            kind='retaining_support',assembly_id=assembly_id,style='hidden')
                strip['inner_edge_indices']=[]
                grade.append(strip)
    return dict(surfaces=surfaces,terrain_surfaces=grade,retaining_edges=walls,stairs=stairs,
                maximum_cut_m=round(max(0.,maximum_cut),4),maximum_fill_m=round(max(0.,maximum_fill),4))


def plan_frontage(edge,outward,street_surfaces,ground_sampler,reservation,other_buildings,
                  *,assembly_id='hillside',public_boundary=None,_depth_limit=1.65):
    """One bounded terrace and a connected stair/driveway where space permits.

    Public construction and neighbouring footprints are hard exclusions.
    Stair runs always join a house terrace/upper landing and a lower landing;
    at most twelve 0.12--0.18-m risers, with 0.28-m minimum treads. A driveway
    needs 2.6 m width and at most 14 percent grade. No access is invented across
    a reserved public road, sidewalk, junction or neighbouring house.
    """
    edge=np.asarray(edge,dtype=float);outward=np.asarray(outward,dtype=float)
    if edge.shape!=(2,3) or outward.shape!=(2,) or not np.isfinite(edge).all() or not np.isfinite(outward).all():
        raise ValueError('Invalid hillside frontage')
    length=float(np.linalg.norm(edge[1,:2]-edge[0,:2]))
    if length<2. or abs(edge[1,2]-edge[0,2])/length>.08:return None
    if np.linalg.norm(outward)<1e-8:raise ValueError('Invalid zero frontage normal')
    outward=outward/np.linalg.norm(outward);tangent=(edge[1,:2]-edge[0,:2])/length
    if abs(float(tangent@outward))>.02:raise ValueError('Frontage normal is not perpendicular')
    center=edge[:,:2].mean(axis=0)
    width=min(length-.18,11.)
    # A local terrace remains below the real wall base. Separate upper wall
    # seams must be filtered by the caller before this function is invoked.
    wall=np.array([center-tangent*width/2,center+tangent*width/2])
    near_z=np.interp(np.array([.5-width/(2*length),.5+width/(2*length)]),[0,1],edge[:,2])-.045
    start=wall+outward*.018
    obstacles=unary_union([getattr(reservation,'context',reservation),getattr(other_buildings,'context',other_buildings)])
    free=_clear_length(start.mean(axis=0),tangent,outward,width,obstacles,limit=3.)
    depth=min(_depth_limit,free-.28)
    if depth<.9:return None
    terrace=_quad(start,start+outward*depth,near_z,near_z-.015*depth,
                  id=assembly_id+'_terrace',kind='terrace',assembly_id=assembly_id)
    terrace['wall_edge']=edge.tolist()
    initial=np.vstack([start,start+outward*depth])
    gap=np.r_[near_z,near_z-.015*depth]-.035-np.asarray(ground_sampler(initial))
    if np.max(np.abs(gap))<.34:return None
    if gap.min()< -2.5 or gap.max()>1.8:return None
    surfaces=[terrace];stairs=[]
    public=public_boundary if public_boundary is not None else getattr(reservation,'context',reservation)
    # Try a broad vehicle access first only when a gentle gradient is real;
    # otherwise use one narrow stair assembly, never tiled disconnected steps.
    candidates=[]
    from roadtoassetto.ground_details import PavementContactIndex
    index=PavementContactIndex(street_surfaces)
    for access_width in (2.6,1.15):
        if width<access_width+.2:continue
        for shift in (0.,-.46,.46,-.23,.23):
            mid=start.mean(axis=0)+tangent*shift*(width-access_width)
            reach=_clear_length(mid,tangent,outward,access_width,obstacles,limit=11.)
            if reach<depth+.9:continue
            finish=mid+outward*reach
            if public.distance(Point(finish))>.8:continue
            target=np.array([finish-tangent*access_width/2,finish+tangent*access_width/2])
            # Oblique house fronts meet the public boundary at different
            # depths. Project both corners independently, instead of leaving
            # one corner up the DGM bank and inventing excessive crossfall.
            start_corners=np.array([mid-tangent*access_width/2,mid+tangent*access_width/2])
            adapted=[]
            for corner in start_corners:
                corner_reach=_clear_length(corner,tangent,outward,.04,obstacles,limit=11.)
                adapted.append(corner+outward*corner_reach)
            adapted=np.asarray(adapted)
            approach=Polygon([start_corners[0],start_corners[1],adapted[1],adapted[0]])
            if (approach.is_valid and approach.area>.05 and obstacles.intersection(approach).area<1e-6
                    and all(public.distance(Point(p))<.3 for p in adapted)):
                target=adapted
            far_z=np.asarray(ground_sampler(target),dtype=float)+.065
            # Prefer accepted sidewalk rear-edge heights where available.
            for k in range(2):
                contact=index.nearest(target[k]-outward*.10,outward,max_distance=.40)
                if contact is not None and np.linalg.norm(contact['xy']-target[k])<.20:
                    # Reach the actual accepted rear edge: the planning
                    # clearance is not an excuse for an unpaved access gap.
                    target[k]=contact['xy'];far_z[k]=contact['top']
            upper=np.array([mid-tangent*access_width/2,mid+tangent*access_width/2])+outward*depth
            upper_z=_levels(terrace,upper)
            if not np.isfinite(upper_z).all():continue
            run=float(np.min((target-upper)@outward));difference=upper_z-far_z
            if access_width>2 and run>=2 and np.max(abs(difference))/run<=.14:
                surface=_quad(upper,target,upper_z,far_z,id=assembly_id+'_driveway',kind='driveway',
                              assembly_id=assembly_id,style='asphalt')
                longitudinal,crossfall,total=surface_slopes(surface,tangent,outward)
                if crossfall>.05 or total>.14:continue
                surface.update(maximum_gradient=float(total),maximum_crossfall=float(crossfall))
                candidates.append(([surface],[],0));continue
            if access_width>2:continue
            path=_quad(upper,target,upper_z,far_z,id=assembly_id+'_footpath',kind='footpath',
                       assembly_id=assembly_id,style='pavers')
            along,cross,total=surface_slopes(path,tangent,outward)
            if total<=.12 and cross<=.08:
                path.update(maximum_gradient=float(total),maximum_crossfall=float(cross))
                candidates.append(([path],[],1));continue
            # The stair flight uses level cross-sections. Its final landing
            # absorbs the street's longitudinal grade; treads are not warped
            # to match a sloping kerb along their full width.
            step_far=np.repeat(float(far_z.mean()),2)
            step_upper=np.repeat(float(upper_z.mean()),2)
            difference=step_upper-step_far
            rise=float(np.mean(difference));n=int(np.ceil(float(np.max(abs(difference)))/.175))
            if not 2<=n<=12 or abs(rise)/n<.12 or np.ptp(difference)>.10:continue
            tread=.30;lower_depth=None
            # A skewed kerb needs a longer end podium to absorb its true
            # cross-street grade. Use available space, keeping the flight
            # level, instead of rejecting all accesses on steep streets.
            for landing_depth in (.85,1.10,1.40,1.80,2.20):
                proposed_extension=run-landing_depth-n*tread
                if proposed_extension<0:continue
                proposed_bottom=upper+outward*(run-landing_depth)
                transition=_quad(proposed_bottom,target,step_far,far_z,id='landing_probe',
                                 kind='landing',assembly_id=assembly_id)
                if surface_slopes(transition,tangent,outward)[2]<=.14:
                    lower_depth=landing_depth;break
            if lower_depth is None:continue
            extension=run-lower_depth-n*tread
            if extension<.18 and np.ptp(upper_z)>.02:continue
            if extension<0:continue
            additions=[];stair_id=assembly_id+'_stairs'
            if extension>.03:
                upper_end=upper+outward*extension
                additions.append(_quad(upper,upper_end,upper_z,step_upper,id=assembly_id+'_upper_landing',
                                       kind='landing',assembly_id=assembly_id))
                if surface_slopes(additions[-1],tangent,outward)[2]>.14:continue
                upper=upper_end;upper_id=additions[-1]['id']
            else:
                upper_id=terrace['id'];step_upper=upper_z.copy()
                difference=step_upper-step_far
            ids=[]
            for k in range(n):
                a=upper+outward*k*tread;b=upper+outward*(k+1)*tread
                level=step_upper-difference*(k+1)/n
                surface=_quad(a,b,level,level,id=f'{stair_id}_tread_{k:02d}',kind='stair',
                              assembly_id=assembly_id,style='concrete')
                surface.update(stair_id=stair_id,step_index=k,step_count=n,riser_m=abs(rise)/n)
                additions.append(surface);ids.append(surface['id'])
            bottom=upper+outward*n*tread
            lower=_quad(bottom,target,step_far,far_z,id=assembly_id+'_lower_landing',kind='landing',
                        assembly_id=assembly_id)
            if surface_slopes(lower,tangent,outward)[2]>.14:continue
            additions.append(lower)
            rails=[]
            for side in (0,1):
                rail_xy=[upper[side]-outward*.025]
                rail_base=[float(step_upper[side])]
                for k in range(n):
                    rail_xy.append(upper[side]+outward*(k+.5)*tread)
                    rail_base.append(float(step_upper[side]-difference[side]*(k+1)/n))
                # The endpoint may follow a skewed public rear edge. Stay on
                # the actual podium side segment, rather than extrapolating
                # a perpendicular ray outside its tapered footprint.
                landing_side=target[side]-bottom[side]
                fraction=min(.25,.20/float(np.linalg.norm(landing_side)))
                rail_xy.append(bottom[side]+landing_side*fraction)
                final_base=float(_levels(lower,[rail_xy[-1]])[0])
                if not np.isfinite(final_base):
                    raise ValueError('Hillside handrail endpoint is outside its planned landing')
                rail_base.append(final_base)
                rails.append(dict(xy=np.asarray(rail_xy).tolist(),base=rail_base))
            assembly=dict(id=stair_id,tread_surface_ids=ids,upper_landing_id=upper_id,
                          lower_landing_id=lower['id'],width_m=access_width,riser_m=abs(rise)/n,
                          tread_m=tread,handrails=rails,source=PROVENANCE)
            candidates.append((additions,[assembly],2))
    # Reserve the first deterministic feasible construction. If no connected
    # access fits, the local cut/terrace still resolves the embedded wall.
    for additions,new_stairs,_ in sorted(candidates,key=lambda row:row[2]):
        joined=[*surfaces,*additions]
        result=_finish_plan(joined,new_stairs,edge,outward,ground_sampler,reservation,other_buildings,assembly_id)
        if result is not None:return result
    if _depth_limit>1.:
        compact=plan_frontage(edge,outward,street_surfaces,ground_sampler,reservation,other_buildings,
                              assembly_id=assembly_id,public_boundary=public_boundary,_depth_limit=.95)
        if compact is not None and (compact['stairs'] or any(s['kind'] in ('driveway','footpath') for s in compact['surfaces'])):
            return compact
    return _finish_plan(surfaces,stairs,edge,outward,ground_sampler,reservation,other_buildings,assembly_id)


def plan_hillside(data_dir,line,road_polygon,bounds,street_surfaces,ground_sampler,
                  *,other_roads=None,existing_plan=None):
    """Produce one serializable shared terrain/render plan for this route."""
    from roadtoassetto.ground_details import _ground_buildings,_wall_frontage_edges
    buildings=_ground_buildings(data_dir,line,road_polygon,bounds)
    old=dict(existing_plan or {})
    old_contacts=list(old.get('contact_surfaces',[]));old_foundations=list(old.get('foundation_surfaces',[]))
    public_parts=[road_polygon.buffer(.502),*[Polygon(s['xy']).buffer(.035) for s in street_surfaces]]
    if other_roads is not None and not other_roads.is_empty:public_parts.append(other_roads.buffer(.08))
    public=unary_union(public_parts)
    occupied=unary_union([public,*[Polygon(s['xy']).buffer(.025) for s in old_contacts]])
    building_tree=STRtree([b[0] for b in buildings])
    surfaces=[];grade=[];walls=[];stairs=[];records=[];rejected=0
    ordered=sorted(enumerate(buildings),key=lambda row:tuple(np.asarray(row[1][0].centroid.coords)[0].round(3)))
    for building_index,(footprint,rings) in ordered:
        nearby=building_tree.query(footprint.buffer(13))
        neighbours=unary_union([buildings[int(j)][0].buffer(.07) for j in nearby if j!=building_index])
        # LoD2 may repeat the same XY edge at storey/roof seams. Only the
        # lowest occurrence may become a ground-level construction anchor.
        edges={}
        for edge,normal in _wall_frontage_edges(footprint,rings,line):
            key=tuple(sorted(tuple(p.round(3)) for p in edge[:,:2]))
            if key not in edges or edge[:,2].mean()<edges[key][0][:,2].mean():edges[key]=(edge,normal)
        for _,(edge,normal) in sorted(edges.items()):
            station=float(line.project(Point(edge[:,:2].mean(axis=0))))
            if station<8 or station>line.length-8:continue
            world_key=np.asarray(sorted(tuple(p.round(3)) for p in edge)).tobytes()
            identifier='hill_'+hashlib.sha256(world_key).hexdigest()[:16]
            result=plan_frontage(edge,normal,street_surfaces,ground_sampler,occupied,neighbours,
                                 assembly_id=identifier,public_boundary=public)
            if result is None:rejected+=1;continue
            for key,output in [('surfaces',surfaces),('terrain_surfaces',grade),('retaining_edges',walls),('stairs',stairs)]:
                for row in result[key]:row.update(building_index=building_index,station_m=round(station,3))
                output.extend(result[key])
            constructed=unary_union([Polygon(s['xy']) for s in result['terrain_surfaces']])
            occupied=occupied.union(constructed.buffer(.03))
            records.append(dict(id=identifier,building_index=building_index,station_m=round(station,3),
                                maximum_cut_m=result['maximum_cut_m'],maximum_fill_m=result['maximum_fill_m'],
                                source_wall=edge.tolist(),source=PROVENANCE))
    from roadtoassetto.hillside_banks import plan_hillside_banks
    frontage_area=unary_union([Polygon(s['xy']) for s in grade])
    frontage_foundations=[s for s in old_foundations if not frontage_area.buffer(.04).intersects(Polygon(s['xy']))]
    banks=plan_hillside_banks(line,road_polygon,street_surfaces,ground_sampler,
        other_roads=other_roads,buildings=[p for p,_ in buildings],
        occupied_surfaces=[*grade,*old_contacts,*frontage_foundations])
    frontage_assemblies=len({s['assembly_id'] for s in walls})
    surfaces.extend(banks['surfaces']);grade.extend(banks['terrain_surfaces']);walls.extend(banks['retaining_edges'])
    construction=unary_union([Polygon(s['xy']) for s in grade])
    remaining_foundations=[s for s in old_foundations if not construction.buffer(.04).intersects(Polygon(s['xy']))]
    old.update(contact_surfaces=old_contacts,foundation_surfaces=remaining_foundations,
               house_foundation_ground_panels=len(remaining_foundations),
               house_foundation_ground_buildings=len({s.get('building_index') for s in remaining_foundations}))
    stats=dict(hillside_terraces=sum(s['kind']=='terrace' for s in surfaces),
               hillside_retaining_walls=len(walls),hillside_staircases=len(stairs),
               hillside_retaining_assemblies=frontage_assemblies+banks['stats']['hillside_bank_runs'],
               hillside_retaining_assembly_definition='Frontage constructions plus connected retaining-bank runs; individual wall cells counted separately',
               hillside_driveways=sum(s['kind']=='driveway' for s in surfaces),
               hillside_footpaths=sum(s['kind']=='footpath' for s in surfaces),
               hillside_terrain_surfaces=len(grade),hillside_rejected_frontages=rejected,
               hillside_provenance=PROVENANCE,hillside_construction_records=records,
               hillside_limits={'maximum_cut_m':2.5,'maximum_fill_m':1.8,'maximum_wall_height_m':2.76,
                                'maximum_stair_riser_m':.175,'minimum_stair_tread_m':.30,
                                'minimum_landing_depth_m':.85,'maximum_driveway_gradient':.14})
    stats['hillside_limits']['maximum_driveway_crossfall']=.05
    stats.update(banks['stats'])
    stats['hillside_limits'].update(maximum_footpath_gradient=.12,maximum_footpath_crossfall=.08,
                                   maximum_landing_gradient=.14)
    stats['hillside_driveway_records']=[{key:s[key] for key in
        ('id','xy','top','faces','assembly_id','maximum_gradient','maximum_crossfall')}
        for s in surfaces if s['kind']=='driveway']
    stats['hillside_footpath_records']=[{key:s[key] for key in
        ('id','xy','top','faces','assembly_id','maximum_gradient','maximum_crossfall')}
        for s in surfaces if s['kind']=='footpath']
    return dict(surfaces=surfaces,terrain_surfaces=grade,retaining_edges=walls,stairs=stairs,stats=stats,
                remaining_contact_surfaces=old_contacts,remaining_foundation_surfaces=remaining_foundations,
                remaining_connection_plan=old)
