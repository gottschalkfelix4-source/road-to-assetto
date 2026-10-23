"""Bounded, explicitly inferred stepped retaining banks at steep street edges.

The source house and street geometry is a reservation, never moved. Each wall
is accompanied by its actual terrain transition and a metric gravel bench;
the final bench meets the original sampled ground at its outside edge.
"""
from __future__ import annotations

import hashlib
import math
import numpy as np
from shapely.geometry import Polygon,Point
from shapely import STRtree
from shapely.ops import unary_union,nearest_points

PROVENANCE=('Inferred stepped retaining bank from DGM and existing street edge; '
            'not surveyed walls or property boundaries. Road and LoD2 coordinates remain unchanged.')


def _quad(a,b,za,zb,identifier,kind,assembly):
    return dict(id=identifier,xy=np.array([a[0],a[1],b[1],b[0]]).tolist(),
                top=np.r_[za,zb[::-1]].tolist(),faces=[[0,1,2],[0,2,3]],
                kind=kind,style='gravel',assembly_id=assembly,source=PROVENANCE,
                inner_edge_indices=[0,1],outer_edge_indices=[3,2])


def _bank_cell(edge,outward,ground_sampler,reservation,tiers,identifier,offset_vectors=None,end_weight=None):
    """Create one connected wall/bench cross-section with a fixed tier count."""
    wall_width=.28;bench_width=.50;pitch=wall_width+bench_width
    offsets=np.broadcast_to(outward,(2,2)) if offset_vectors is None else offset_vectors
    weight=np.ones(2) if end_weight is None else np.asarray(end_weight,dtype=float)
    low=np.asarray(ground_sampler(edge),dtype=float)
    far=edge+offsets*(pitch*tiers)
    high=np.asarray(ground_sampler(far),dtype=float)
    rise=high-low
    if not np.isfinite(np.r_[low,high]).all() or rise.min()<.55 or rise.max()/tiers>2.2:
        return None
    footprint=Polygon([edge[0],edge[1],far[1],far[0]])
    if not footprint.is_valid or reservation.intersects(footprint.buffer(.025)):
        return None
    surfaces=[];terrain=[];walls=[];maximum_cut=0.;maximum_fill=0.
    for level in range(tiers):
        inner=edge+offsets*(pitch*level)
        outer=edge+offsets*(pitch*level+wall_width)
        bench_end=edge+offsets*(pitch*(level+1))
        before=low+rise*level/tiers;after=low+rise*(level+1)/tiers
        source_inner=np.asarray(ground_sampler(inner));source_outer=np.asarray(ground_sampler(outer))
        source_end=np.asarray(ground_sampler(bench_end))
        before=source_inner*(1-weight)+before*weight
        outer_z=source_outer*(1-weight)+after*weight
        end_z=source_end*(1-weight)+after*weight
        # Keep the complete final bench at the outer DGM endpoint heights.
        # Intermediate benches divide the retained height into bounded tiers.
        wall_id=f'{identifier}_wall_{level}'
        # The terminal coping enters the original bank instead of exposing a
        # blunt, full-height end face. All intervening terrain follows the
        # same 3-m return to the DGM; this is not a wall painted on a slope.
        wall_top=(np.minimum(source_inner,source_outer)-.07)*(1-weight)+(after+.10)*weight
        wall_bottom=np.minimum.reduce([before,outer_z,wall_top-.03])-.16
        walls.append(dict(id=wall_id,xy=inner.tolist(),top=wall_top.tolist(),
                          bottom=wall_bottom.tolist(),width_m=wall_width,
                          outward=outward.tolist(),assembly_id=identifier,
                          kind='retaining',source=PROVENANCE))
        transition=_quad(inner,outer,before+.035,outer_z+.035,wall_id+'_ground',
                         'retaining_support',identifier)
        transition['inner_edge_indices']=[]
        terrain.append(transition)
        ledge=_quad(outer,bench_end,outer_z+.035,end_z+.035,
                    f'{identifier}_ledge_{level}','bank_ledge',identifier)
        surfaces.append(ledge);terrain.append(dict(ledge))
        for p,z in ((inner,before),(outer,outer_z),(bench_end,end_z)):
            delta=z-np.asarray(ground_sampler(p))
            maximum_cut=max(maximum_cut,float(-delta.min()))
            maximum_fill=max(maximum_fill,float(delta.max()))
    if maximum_cut>2.5 or maximum_fill>1.8:return None
    return dict(surfaces=surfaces,terrain_surfaces=terrain,retaining_edges=walls,
                maximum_cut_m=maximum_cut,maximum_fill_m=maximum_fill,
                tiers=tiers,footprint=footprint)


def plan_hillside_banks(line,road_polygon,street_surfaces,ground_sampler,*,
                       other_roads=None,buildings=(),occupied_surfaces=()):
    """Find connected steep runs behind the actual shoulder or sidewalk edge.

    Two-metre maximum segments follow the public-edge polygon itself. Absolute
    endpoint hashes identify unchanged construction cells. A 0.5-m
    bench separates walls; at most three tiers and 2.2 m retained rise per tier
    are allowed. Runs must span at least 8 m near houses on their own side;
    ordinary open grass slopes and downhill verges remain untouched.
    """
    public=unary_union([road_polygon.buffer(.502),*[Polygon(s['xy']) for s in street_surfaces]])
    # Buffer unions contain sub-millimetre bevel edges between their metre
    # cells. Simplify only this construction offset, never the public source
    # geometry. A 95-mm offset leaves >55mm clearance after simplification
    # and the 25-mm coping reservation; the road itself remains untouched.
    edge_area=public.buffer(.095,join_style=2).simplify(.015,preserve_topology=True)
    parts=[edge_area] if isinstance(edge_area,Polygon) else list(edge_area.geoms)
    blockers=[public,*[p.buffer(.08) for p in buildings],
              *[Polygon(s['xy']).buffer(.025) for s in occupied_surfaces]]
    if other_roads is not None and not other_roads.is_empty:blockers.append(other_roads.buffer(.08))
    reservation=unary_union(blockers)
    houses=list(buildings);house_tree=STRtree(houses)
    accepted=[];rejected=0;runs=0
    for area in parts:
        coords=np.asarray(area.exterior.coords)
        cells=[]
        for a,b in zip(coords[:-1],coords[1:]):
            direction=b-a;length=float(np.linalg.norm(direction))
            if length<.35:
                cells.append(None);continue
            normal=np.array([-direction[1],direction[0]])/length
            if area.covers(Point((a+b)/2+normal*.01)):normal=-normal
            count=max(1,int(math.ceil(length/2.)))
            for i in range(count):
                edge=a+np.array([i/count,(i+1)/count])[:,None]*direction
                midpoint=Point(edge.mean(axis=0));station=float(line.project(midpoint))
                if not 8<=station<=line.length-8:
                    cells.append(None);continue
                near_house=False
                for index in house_tree.query(midpoint.buffer(8.)):
                    house=houses[int(index)]
                    nearest=np.array(nearest_points(midpoint,house)[1].coords[0])
                    # Opposite-side houses must not justify a structure in
                    # an otherwise open grassy hillside across the road.
                    if house.distance(midpoint)<=8. and (nearest-edge.mean(axis=0))@normal>.15:
                        near_house=True;break
                if not near_house:cells.append(None);continue
                low=np.asarray(ground_sampler(edge))
                one=np.asarray(ground_sampler(edge+normal))
                two=np.asarray(ground_sampler(edge+normal*2.))
                # A visibly steep source bank, not merely a small curb rise.
                if not (np.max(one-low)>=.80 or np.max(two-low)>=1.20):
                    cells.append(None);continue
                key=np.asarray(sorted(tuple(p.round(4)) for p in edge),dtype='<f8').tobytes()
                identifier='bank_'+hashlib.sha256(key).hexdigest()[:16]
                options={}
                for tiers in (1,2,3):
                    result=_bank_cell(edge,normal,ground_sampler,reservation,tiers,identifier)
                    if result is not None:
                        result.update(station_m=round(station,3),edge=edge,outward=normal)
                        options[tiers]=result
                if options:cells.append(options)
                else:cells.append(None);rejected+=1
        # Keep continuous runs, rather than scattered wall patches. Choose a
        # common tier count wherever possible so adjacent benches line up.
        start=0
        while start<len(cells):
            if cells[start] is None:start+=1;continue
            end=start+1;common=set(cells[start])
            while end<len(cells) and cells[end] is not None:
                shared=common.intersection(cells[end])
                prev=next(iter(cells[end-1].values()));cur=next(iter(cells[end].values()))
                if not shared or np.linalg.norm(prev['edge'][1]-cur['edge'][0])>.025 or prev['outward']@cur['outward']<.995:
                    break
                common=shared;end+=1
            choice=max(common)
            length=sum(float(np.linalg.norm(cells[i][choice]['edge'][1]-cells[i][choice]['edge'][0])) for i in range(start,end))
            if length>=8.:
                runs+=1
                # Shared miter vectors keep every bench boundary watertight
                # on gentle bends. Independent normal offsets leave wedges.
                selected=[cells[i][choice] for i in range(start,end)]
                vectors=[selected[0]['outward']]
                for left,right in zip(selected[:-1],selected[1:]):
                    a=left['outward'];b=right['outward'];vectors.append((a+b)/(1+float(a@b)))
                vectors.append(selected[-1]['outward'])
                distances=np.r_[0.,np.cumsum([np.linalg.norm(r['edge'][1]-r['edge'][0]) for r in selected])]
                weight=np.clip(np.minimum(distances,distances[-1]-distances)/3.,0.,1.)
                weight=weight*weight*(3.-2.*weight)
                for i,row in enumerate(selected):
                    identifier=row['surfaces'][0]['assembly_id']
                    revised=_bank_cell(row['edge'],row['outward'],ground_sampler,reservation,choice,
                                       identifier,np.array(vectors[i:i+2]),weight[i:i+2])
                    if revised is None:rejected+=1;continue
                    revised['station_m']=row['station_m'];accepted.append(revised)
            else:rejected+=end-start
            start=end
    surfaces=[];terrain=[];walls=[];records=[]
    for row in accepted:
        for key,target in [('surfaces',surfaces),('terrain_surfaces',terrain),('retaining_edges',walls)]:
            for section in row[key]:section['station_m']=row['station_m']
            target.extend(row[key])
        records.append(dict(id=row['surfaces'][0]['assembly_id'],station_m=row['station_m'],
            tiers=row['tiers'],maximum_cut_m=round(row['maximum_cut_m'],4),
            maximum_fill_m=round(row['maximum_fill_m'],4),source=PROVENANCE))
    return dict(surfaces=surfaces,terrain_surfaces=terrain,retaining_edges=walls,
        stats={'hillside_bank_ledges':len(surfaces),'hillside_bank_runs':runs,
               'hillside_bank_walls':len(walls),'hillside_bank_rejected_cells':rejected,
               'hillside_bank_records':records,'hillside_bank_provenance':PROVENANCE,
               'hillside_bank_limits':{'maximum_tiers':3,'maximum_retained_rise_per_tier_m':2.2,
                 'bench_width_m':.5,'wall_width_m':.28,'maximum_cut_m':2.5,'maximum_fill_m':1.8,
                 'minimum_run_m':8.,'same_side_house_radius_m':8.,'end_return_m':3.}})
