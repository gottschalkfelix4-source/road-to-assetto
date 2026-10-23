"""Bounded, continuous multi-model tracks.

The small global road profile is computed once. Expensive buildings, textures
and terrain are prepared one section at a time, with an overlap for planning.
Only owned scenery and the exact core road strip are exported. All models use
the same coordinate frame and Assetto Corsa loads them through models.ini.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path
import shutil

import numpy as np
from pyproj import Transformer
from scipy.ndimage import gaussian_filter1d
from shapely import (MultiPoint, STRtree, constrained_delaunay_triangles,
                     get_parts, linestrings, points, polygons, voronoi_polygons)
from shapely.geometry import LineString, Point, Polygon, box
from shapely.ops import unary_union


def resample_route(coordinates, spacing_m=1., check_simple=True):
    """Project [latitude, longitude] and smooth one distance-resampled route."""
    values=np.asarray(coordinates,dtype=float)
    if values.ndim!=2 or values.shape[1]!=2 or len(values)<2 or not np.isfinite(values).all():
        raise ValueError('Expected finite latitude/longitude route coordinates')
    if not .5<=spacing_m<=5:
        raise ValueError('Road sample spacing must be between 0.5 and 5 m')
    transform=Transformer.from_crs(4326,25832,always_xy=True)
    source=np.array(transform.transform(values[:,1],values[:,0])).T
    distances=np.r_[0,np.cumsum(np.linalg.norm(np.diff(source,axis=0),axis=1))]
    keep=np.r_[True,np.diff(distances)>=.01]
    source,distances=source[keep],distances[keep]
    if len(source)<2 or distances[-1]<120:
        raise ValueError('Route must contain at least 120 metres')
    count=math.ceil(distances[-1]/spacing_m)+1
    s=np.linspace(0,distances[-1],count)
    xy=np.c_[np.interp(s,distances,source[:,0]),np.interp(s,distances,source[:,1])]
    xy=gaussian_filter1d(xy,sigma=2./spacing_m,axis=0,mode='nearest')
    xy[[0,-1]]=source[[0,-1]]
    if check_simple:
        source_tree=STRtree(linestrings(np.stack((source[:-1],source[1:]),axis=1)))
        _,deviations=source_tree.query_nearest(points(xy),return_distance=True)
        if len(deviations) and float(np.max(deviations))>1.5:
            raise ValueError('Die geglättete Fahrbahn weicht an einer engen Kurve mehr als 1,5 m von der Straßenroute ab.')
    if check_simple and not LineString(xy).is_simple:
        tree=STRtree(linestrings(np.stack((xy[:-1],xy[1:]),axis=1)))
        crossings=tree.query(linestrings(np.stack((xy[:-1],xy[1:]),axis=1)),predicate='crosses')
        index=int(crossings[0,0]) if crossings.size else 0
        km=float(np.linalg.norm(np.diff(xy[:index+1],axis=0),axis=1).sum()/1000)
        raise ValueError(f'Die Route kreuzt sich bei etwa km {km:.1f}. Kreuzungsfreie Teilstrecke wählen; Überführungen benötigen ein eigenes Höhenmodell.')
    return xy


def plan_sections(xy, section_m=1000., halo_m=50., padding_m=160.):
    """Return overlapping input windows and contiguous inclusive core indices."""
    xy=np.asarray(xy,dtype=float)
    if (xy.ndim!=2 or xy.shape[1]!=2 or len(xy)<2 or not np.isfinite(xy).all()
            or not 120<=section_m<=2000 or not 20<=halo_m<=200 or padding_m<130):
        raise ValueError('Invalid section plan')
    distance=np.r_[0,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    if np.any(np.diff(distance)<.01):
        raise ValueError('Duplicate road profile samples')
    count=max(1,math.ceil(distance[-1]/section_m))
    cuts=np.searchsorted(distance,np.linspace(0,distance[-1],count+1))
    cuts[0]=0;cuts[-1]=len(xy)-1
    result=[]
    for index,(start,end) in enumerate(zip(cuts[:-1],cuts[1:])):
        first=max(0,int(np.searchsorted(distance,distance[start]-halo_m))-1)
        last=min(len(xy)-1,int(np.searchsorted(distance,distance[end]+halo_m)))
        window=xy[first:last+1]
        lower=np.floor(window.min(axis=0)-padding_m)
        upper=np.ceil(window.max(axis=0)+padding_m)
        result.append({'index':index,'core_start':int(start),'core_end':int(end),
                       'halo_start':first,'halo_end':last,
                       'start_m':float(distance[start]),'end_m':float(distance[end]),
                       'bounds':[float(lower[0]),float(lower[1]),float(upper[0]),float(upper[1])],
                       'first':index==0,'last':index==count-1})
    return result


def build_profile(xy, raw_heights, road_width_m, crossfall=.025):
    """Smooth sampled DGM heights globally, retaining a common origin and UVs."""
    xy=np.asarray(xy,dtype=float)
    raw=np.asarray(raw_heights,dtype=float)
    if (xy.ndim!=2 or xy.shape[1]!=2 or len(xy)<2 or raw.shape!=(len(xy),)
            or not np.isfinite(xy).all() or not np.isfinite(raw).all()
            or not 4<=road_width_m<=8 or not 0<=crossfall<=.04):
        raise ValueError('Invalid global road profile')
    tangent=np.gradient(xy,axis=0)
    norms=np.linalg.norm(tangent,axis=1)
    if np.any(norms<.01):
        raise ValueError('Duplicate or reversing global road samples')
    tangent/=norms[:,None]
    left=np.c_[-tangent[:,1],tangent[:,0]]
    road_polygon=Polygon(np.vstack((xy+left*road_width_m/2,(xy-left*road_width_m/2)[::-1])))
    if not road_polygon.is_valid:
        raise ValueError('Die gewählte Fahrbahnbreite erzeugt überlappende Straßenränder. Schmalere Breite oder andere Route wählen.')
    distance=np.r_[0,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    road_y=gaussian_filter1d(raw,sigma=3.,mode='nearest')
    grades=np.diff(road_y)/np.diff(distance)
    if np.max(np.abs(grades))>.25:
        raise ValueError('Unexpected road gradient exceeds 25 percent')
    return {'xy':xy,'road_y':road_y,'raw_heights':raw,'tangent':tangent,'left':left,
            'distance':distance,'road_widths':np.full(len(xy),float(road_width_m)),
            'origin_utm32':xy.mean(axis=0),'height_origin_m':float(road_y.min()),
            'crossfall':float(crossfall)}


def configure_section(config, profile, section, ownership=None):
    """Private prepare() hook; persisted user requests cannot set these fields."""
    result=deepcopy(config)
    a,b=section['halo_start'],section['halo_end']+1
    result['_road_profile']={key:np.asarray(profile[key])[a:b].tolist()
            for key in ('xy','road_y','raw_heights','tangent','left','distance','road_widths')}
    result['_road_profile'].update(origin_utm32=np.asarray(profile['origin_utm32']).tolist(),
                                  height_origin_m=profile['height_origin_m'])
    result['_section']=dict(section)
    if ownership is not None:
        from shapely.geometry import mapping
        result['_ownership_polygon']=mapping(ownership)
    result['route_start_m']=None;result['route_end_m']=None
    return result


def plan_ownership(profile, sections, corridor_m=120.):
    """Disjoint Voronoi scenery domains, including bends and nearby hairpins.

    Dense seeds either side of a section boundary place that boundary exactly
    between the shared road samples. Other seeds are 30 m apart. The retained
    corridor is bounded; there is no huge rectangular terrain between bends.
    """
    if not 30<=corridor_m<=120:
        raise ValueError('Scenery corridor must be between 30 and 120 m')
    xy=np.asarray(profile['xy']);distance=np.asarray(profile['distance'])
    corridor=LineString(xy).buffer(corridor_m,cap_style='flat')
    if len(sections)==1:
        return [corridor]
    seeds=[];owners=[]
    for section in sections:
        a,b=section['core_start'],section['core_end']
        positions=np.arange(distance[a]+.01,distance[b],30.)
        positions=np.r_[positions,distance[b]-.01]
        for s in positions:
            seeds.append([np.interp(s,distance,xy[:,0]),np.interp(s,distance,xy[:,1])])
            owners.append(section['index'])
    seeds=np.asarray(seeds)
    cells=list(get_parts(voronoi_polygons(MultiPoint(seeds),extend_to=corridor.envelope)))
    tree=STRtree([Point(p) for p in seeds])
    groups=[[] for _ in sections]
    for cell in cells:
        indices=tree.query(cell,predicate='covers')
        if len(indices)!=1:
            raise ValueError('Ambiguous scenery section ownership')
        groups[owners[int(indices[0])]].append(cell.intersection(corridor))
    return [unary_union(group) for group in groups]


def _clip_convex(attributes, clip_xy):
    """Clip 3-D triangle attributes against a vertical convex XY prism."""
    output=np.asarray(attributes,dtype=float)
    shape=np.asarray(clip_xy,dtype=float)
    area=np.sum(shape[:,0]*np.roll(shape[:,1],-1)-shape[:,1]*np.roll(shape[:,0],-1))
    if area<0:shape=shape[::-1]
    for a,b in zip(shape,np.roll(shape,-1,axis=0)):
        if len(output)<3:return np.empty((0,attributes.shape[1]))
        edge=b-a
        value=(output[:,0]-a[0])*edge[1]-(output[:,2]-a[1])*edge[0]
        new=[]
        for p,q,dp,dq in zip(output,np.roll(output,-1,axis=0),value,np.roll(value,-1)):
            pin=dp<=1e-8;qin=dq<=1e-8
            if pin:new.append(p)
            if pin!=qin:
                t=dp/(dp-dq)
                new.append(p+(q-p)*t)
        output=np.asarray(new,dtype=float)
    return output


class SceneClipper:
    """Clip every scenery triangle, including vertical walls, without holes."""
    def __init__(self, polygon, profile):
        origin=profile['origin_utm32']
        from shapely import transform
        self.polygon=transform(polygon,lambda xy:np.c_[xy[:,0]-origin[0],-(xy[:,1]-origin[1])])
        self.cells=list(get_parts(constrained_delaunay_triangles(self.polygon)))
        self.tree=STRtree(self.cells)
        self.cleanup={'triangles':0,'projected_area_m2':0.}

    def clip(self,mesh):
        vertices=np.asarray(mesh['vertices'],dtype=float)
        extent=box(*vertices[:,[0,2]].min(axis=0),*vertices[:,[0,2]].max(axis=0)).buffer(1e-8)
        if self.polygon.covers(extent):return [mesh]
        if not self.polygon.intersects(extent):return []
        uv=np.asarray(mesh['uv'],dtype=float)
        faces=np.asarray(mesh['faces'],dtype=int)
        normals=np.asarray(mesh.get('normals',[]),dtype=float)
        supplied=normals.shape==vertices.shape
        if not supplied:
            # Foliage sheets and rails can deliberately duplicate positions
            # and UVs for opposite-facing triangles. Their source normals are
            # part of vertex identity; merging only XYZ/UV cancels the faces.
            from roadtoassetto.kn5 import normals as compute_normals
            normals=compute_normals(vertices,faces)
            supplied=True
        attributes=np.c_[vertices,uv,normals] if supplied else np.c_[vertices,uv]
        horizontal=vertices[faces][:,:,[0,2]]
        shapes=polygons(horizontal)
        inside=np.asarray(self.polygon.covers(shapes))
        # A vertical wall has zero projected area; polygon predicates on its
        # degenerate triangle are undefined, so process it as a prism clip.
        first=horizontal[:,1]-horizontal[:,0];second=horizontal[:,2]-horizontal[:,0]
        area=np.abs(first[:,0]*second[:,1]-first[:,1]*second[:,0])
        vertical=np.flatnonzero(area<1e-9)
        for index in vertical:
            # A wall entirely inside needs no expensive prism decomposition.
            # The convex hull can be a line; covers handles that geometry.
            inside[index]=self.polygon.covers(MultiPoint(horizontal[index]).convex_hull)
        output=[];outfaces=[]
        for face in faces[inside]:
            offset=len(output);output.extend(attributes[face]);outfaces.append([offset,offset+1,offset+2])
        for index in np.flatnonzero(~inside):
            tri=attributes[faces[index]]
            lower=horizontal[index].min(axis=0);upper=horizontal[index].max(axis=0)
            if not self.polygon.intersects(box(*lower,*upper).buffer(1e-8)):
                continue
            for candidate in self.tree.query(box(*lower,*upper).buffer(1e-8)):
                cell=np.asarray(self.cells[int(candidate)].exterior.coords)[:-1]
                clipped=_clip_convex(tri,cell)
                if len(clipped)<3:continue
                for j in range(1,len(clipped)-1):
                    triangle=clipped[[0,j,j+1]]
                    normal=np.cross(triangle[1,:3]-triangle[0,:3],triangle[2,:3]-triangle[0,:3])
                    if np.linalg.norm(normal)<1e-8:
                        if mesh['name'].startswith('1GRASS_TERRAIN'):
                            self.cleanup['triangles']+=1
                            self.cleanup['projected_area_m2']+=float(abs(normal[1])/2)
                        continue
                    offset=len(output);output.extend(triangle);outfaces.append([offset,offset+1,offset+2])
        if not output:return []
        packed=np.asarray(output)
        # Deduplicate attribute-identical vertices. Flat windows deliberately
        # retain different UVs/normals even where their positions coincide.
        packed=np.round(packed,7)
        unique,inverse=np.unique(packed,axis=0,return_inverse=True)
        allfaces=inverse[np.asarray(outfaces)]
        cleanup={}
        if mesh['name'].startswith('1GRASS_TERRAIN'):
            # Clipping can create micrometre-wide border needles below the
            # source mesh's 2-mm construction grid. Account for those tiny
            # remnants explicitly instead of accepting unstable physical
            # faces or relaxing the exporter winding check.
            serialized=unique[:,:3].astype(np.float32).astype(float)
            tri=serialized[allfaces]
            doubled=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])[:,1]
            tiny=(np.abs(doubled)<=1e-8)&(np.arange(len(allfaces))>=int(inside.sum()))
            if np.any(tiny):
                removed_area=float(np.abs(doubled[tiny]).sum()/2)
                if removed_area>1e-5:
                    raise ValueError('Too many sub-resolution terrain clipping remnants')
                cleanup={'triangles':int(tiny.sum()),'projected_area_m2':removed_area}
                self.cleanup['triangles']+=cleanup['triangles']
                self.cleanup['projected_area_m2']+=removed_area
                allfaces=allfaces[~tiny]
        results=[]
        for start in range(0,len(allfaces),18000):
            local=allfaces[start:start+18000]
            used,remap=np.unique(local,return_inverse=True)
            p=unique[used]
            result={**mesh,'name':mesh['name']+f'_C{start//18000}',
                    'vertices':p[:,:3].tolist(),'uv':p[:,3:5].tolist(),'faces':remap.reshape(-1,3).tolist()}
            if supplied:
                n=p[:,5:8];n/=np.maximum(np.linalg.norm(n,axis=1)[:,None],1e-12)
                result['normals']=n.tolist()
            if cleanup and start==0:result['subresolution_clipping_cleanup']=cleanup
            results.append(result)
        return results


def _boundary_edges(mesh, boundary):
    """Mesh boundary edges on the scenery ownership border, in AC X/Z."""
    vertices=np.asarray(mesh['vertices'],dtype=float)
    faces=np.asarray(mesh['faces'],dtype=int)
    edges,count=np.unique(np.sort(np.concatenate((faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]])),axis=1),
                          axis=0,return_counts=True)
    edges=edges[count==1]
    if not len(edges):return edges
    xz=vertices[edges][:,:,[0,2]]
    endpoints=points(xz.reshape(-1,2))
    near=np.asarray(boundary.distance(endpoints)).reshape(-1,2).max(axis=1)<2e-5
    near&=np.asarray(boundary.distance(points(xz.mean(axis=1))))<2e-5
    return edges[near]


class TerrainSeams:
    """Reuse exact border heights and all border nodes across adjacent models.

    Merely averaging new vertices is insufficient: a previous border can have
    additional knots. Insert those knots into the new triangles as well, so
    both sides represent the same piecewise-linear physical surface.
    """
    def __init__(self):
        self.segments=[]
        self.normal_segments=[]

    def weld(self,scene,boundary):
        previous=np.asarray(self.segments,dtype=float)
        previous_normals=np.asarray(self.normal_segments,dtype=float)
        edge_tree=STRtree(linestrings(previous[:,:,[0,2]])) if len(previous) else None
        old_points=previous.reshape(-1,3) if len(previous) else np.empty((0,3))
        point_tree=STRtree(points(old_points[:,[0,2]])) if len(old_points) else None
        old_normals=previous_normals.reshape(-1,3) if len(previous) else np.empty((0,3))
        pending=[];pending_normals=[];welded=0;inserted=0
        for mesh in scene['meshes']:
            if not mesh['name'].startswith('1GRASS_TERRAIN'):continue
            edges=_boundary_edges(mesh,boundary)
            vertices=np.asarray(mesh['vertices'],dtype=float)
            uv=np.asarray(mesh['uv'],dtype=float)
            faces=np.asarray(mesh['faces'],dtype=int)
            from roadtoassetto.kn5 import normals
            normal=np.asarray(mesh.get('normals',normals(vertices,faces)),dtype=float)
            if edge_tree is not None and len(edges):
                boundary_nodes=np.unique(edges)
                pairs=edge_tree.query(points(vertices[boundary_nodes][:,[0,2]]),predicate='dwithin',distance=3e-5)
                changed=set()
                for node_index,edge_index in pairs.T:
                    node=int(boundary_nodes[node_index])
                    if node in changed:continue
                    edge=previous[edge_index];delta=edge[1,[0,2]]-edge[0,[0,2]]
                    denominator=float(delta@delta)
                    if denominator<1e-12:continue
                    t=float(np.clip((vertices[node,[0,2]]-edge[0,[0,2]])@delta/denominator,0,1))
                    vertices[node,1]=edge[0,1]*(1-t)+edge[1,1]*t
                    n=previous_normals[edge_index,0]*(1-t)+previous_normals[edge_index,1]*t
                    normal[node]=n/max(np.linalg.norm(n),1e-12)
                    changed.add(node);welded+=1
                attributes=np.c_[vertices,uv,normal].tolist()
                extra={}
                for a,b in edges:
                    edge=vertices[[a,b]][:,[0,2]];delta=edge[1]-edge[0]
                    denominator=float(delta@delta)
                    if denominator<1e-12:continue
                    edge_length=math.sqrt(denominator)
                    candidates=point_tree.query(LineString(edge),predicate='dwithin',distance=3e-5)
                    knots=[]
                    for candidate in candidates:
                        t=float((old_points[candidate,[0,2]]-edge[0])@delta/denominator)
                        # Source clipping rounds coordinates. The neighboring
                        # knot can consequently be a micrometre from a node
                        # already on this edge: that is the same boundary
                        # knot at the 30-um matching tolerance, not a new face.
                        if not 3e-5<t*edge_length<edge_length-3e-5:continue
                        if any(abs(t-previous_t)*edge_length<3e-5 for previous_t,_ in knots):continue
                        row=np.r_[vertices[a]*(1-t)+vertices[b]*t,uv[a]*(1-t)+uv[b]*t,
                                  old_normals[candidate]]
                        row[:3]=old_points[candidate]
                        index=len(attributes);attributes.append(row.tolist());knots.append((t,index));inserted+=1
                    if knots:extra[(int(a),int(b))]=[i for _,i in sorted(knots)]
                output=[]
                for face in faces:
                    loop=[];changed_edges=[]
                    for local,(a,b) in enumerate(zip(face,np.roll(face,-1))):
                        loop.append(int(a));key=tuple(sorted((int(a),int(b))))
                        if key in extra:
                            knots=extra[key] if a==key[0] else extra[key][::-1]
                            loop.extend(knots);changed_edges.append((local,[int(a),*knots,int(b)]))
                    if not changed_edges:
                        output.append(face.tolist());continue
                    if len(changed_edges)==1:
                        local,edge_nodes=changed_edges[0]
                        opposite=int(face[(local+2)%3])
                        # Maximise the fan altitude using the existing vertex.
                        # An added centroid unnecessarily reduces tiny border
                        # triangles to one third of their projected area.
                        output.extend([opposite,int(a),int(b)] for a,b in zip(edge_nodes[:-1],edge_nodes[1:]))
                        continue
                    center=np.asarray([attributes[int(i)] for i in face]).mean(axis=0)
                    center_index=len(attributes);attributes.append(center.tolist())
                    output.extend([center_index,int(a),int(b)] for a,b in zip(loop,np.roll(loop,-1)))
                packed=np.asarray(attributes)
                mesh['vertices']=packed[:,:3].tolist();mesh['uv']=packed[:,3:5].tolist()
                mesh['normals']=(packed[:,5:8]/np.maximum(np.linalg.norm(packed[:,5:8],axis=1)[:,None],1e-12)).tolist()
                mesh['faces']=output
                edges=_boundary_edges(mesh,boundary)
                vertices=np.asarray(mesh['vertices']);normal=np.asarray(mesh['normals'])
            pending.extend(vertices[edges].tolist());pending_normals.extend(normal[edges].tolist())
        self.segments.extend(pending);self.normal_segments.extend(pending_normals)
        return {'terrain_border_vertices_welded':welded,'terrain_border_knots_inserted':inserted}


def _core_strip(mesh, start, end, count):
    result=deepcopy(mesh)
    a,b=start*count,(end+1)*count
    faces=np.asarray(mesh['faces'],dtype=int)
    faces=faces[np.all((faces>=a)&(faces<b),axis=1)]-a
    for key in ('vertices','uv','normals'):
        if key in result:result[key]=result[key][a:b]
    result['faces']=faces.tolist()
    return result


def finalize_section(scene, profile, section, ownership=None):
    """Own each driving face exactly once and remove the planning overlap."""
    result={**scene,'meshes':[],'markers':scene['markers'] if section['first'] else [],
            'metadata':deepcopy(scene['metadata'])}
    a=section['core_start']-section['halo_start']
    b=section['core_end']-section['halo_start']
    clipper=SceneClipper(ownership,profile) if ownership is not None else None
    for mesh in scene['meshes']:
        if mesh['name'] in ('1ROAD_MAIN','1SHOULDER_LEFT','1SHOULDER_RIGHT'):
            count=7 if mesh['name']=='1ROAD_MAIN' else 3
            if 'normals' not in mesh:
                from roadtoassetto.kn5 import normals
                mesh['normals']=normals(mesh['vertices'],mesh['faces']).tolist()
            parts=[_core_strip(mesh,a,b,count)]
        elif mesh['name'].startswith('1WALL_END_'):
            # prepare suppresses internal barriers. Retain only an endpoint
            # wall that belongs to this endpoint section.
            parts=[mesh] if ((mesh['name']=='1WALL_END_0' and section['first']) or
                            (mesh['name']!='1WALL_END_0' and section['last'])) else []
        elif mesh['name'].startswith(('LOD2_','BASEMENT_')) and ownership is not None:
            # build_lod2 already assigns an entire house by source footprint
            # centroid before batching its facades, roof and basement.
            parts=[mesh]
        else:
            parts=clipper.clip(mesh) if clipper else [mesh]
        for part in parts:
            part['name']+=f'_S{section["index"]:04d}'
            result['meshes'].append(part)
    for key in ('centerline_ac','road_width_profile_m'):
        if key in result['metadata']:result['metadata'][key]=result['metadata'][key][a:b+1]
    result['metadata']['section']=dict(section)
    result['metadata']['length_m']=section['end_m']-section['start_m']
    if clipper:
        from shapely.geometry import mapping
        result['_ownership_ac']=mapping(clipper.polygon)
        if clipper.cleanup['projected_area_m2']>1e-4:
            raise ValueError('Too much terrain would be removed by sub-resolution cleanup')
        if clipper.cleanup['triangles']:
            result['metadata']['subresolution_clipping_cleanup']=dict(clipper.cleanup)
    return result


def namespace_textures(scene, textures_dir):
    """Hash texture identities: shared scans reuse names, aerial tiles cannot."""
    textures_dir=Path(textures_dir)
    destination=textures_dir/'kn5'
    destination.mkdir(exist_ok=True)
    names={}
    used={mesh['material'] for mesh in scene['meshes']}
    remap={old:new for new,old in enumerate(sorted(used))}
    scene['materials']=[deepcopy(scene['materials'][old]) for old in sorted(used)]
    for mesh in scene['meshes']:mesh['material']=remap[mesh['material']]
    def resolve(name):
        if name not in names:
            source=textures_dir/name
            with source.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()[:20]
            replacement=digest+'_'+Path(name).name
            target=destination/replacement
            if not target.exists():
                try:target.hardlink_to(source)
                except OSError:shutil.copy2(source,target)
            names[name]=replacement
        return names[name]
    for material in scene['materials']:
        for key in ('texture','normal'):
            if material.get(key):material[key]=resolve(material[key])
        material['samplers']={key:resolve(value) for key,value in material.get('samplers',{}).items()}
    return destination


class MultiModelExporter:
    """Write/release one scene at a time; retain only compact model records."""
    def __init__(self, track, track_id, profile, sections):
        self.track=Path(track);self.track.mkdir(parents=True,exist_ok=True)
        self.track_id=track_id;self.profile=profile;self.sections=sections
        self.models=[];self.previous_road_end=None;self.marker_names=set()
        self.previous_road_uv=None;self.previous_road_normals=None
        self.terrain_seams=TerrainSeams()

    def write_section(self, scene, section, textures_dir):
        from roadtoassetto.kn5 import write
        from roadtoassetto.inspect_kn5 import inspect
        if section['index']!=len(self.models):
            raise ValueError('Sections must be exported in route order')
        seam_stats={}
        if scene.get('_ownership_ac'):
            from shapely.geometry import shape
            seam_stats=self.terrain_seams.weld(scene,shape(scene['_ownership_ac']).boundary)
        road=next(m for m in scene['meshes'] if m['name'].startswith('1ROAD_MAIN'))
        rows=np.asarray(road['vertices']).reshape(-1,7,3)
        if self.previous_road_end is not None and not np.allclose(rows[0],self.previous_road_end,atol=1e-5,rtol=0):
            raise ValueError('Road section seam is not continuous')
        uv=np.asarray(road['uv']).reshape(-1,7,2)
        supplied_normals=np.asarray(road.get('normals',[]))
        if self.previous_road_uv is not None and not np.allclose(uv[0],self.previous_road_uv,atol=1e-5,rtol=0):
            raise ValueError('Road texture seam is not continuous')
        if supplied_normals.size:
            supplied_normals=supplied_normals.reshape(-1,7,3)
            if self.previous_road_normals is not None and not np.allclose(supplied_normals[0],self.previous_road_normals,atol=1e-5,rtol=0):
                raise ValueError('Road shading seam is not continuous')
            self.previous_road_normals=supplied_normals[-1].copy()
        for marker in scene['markers']:
            if marker['name'] in self.marker_names:
                raise ValueError('Duplicate global spawn marker')
            self.marker_names.add(marker['name'])
        self.previous_road_end=rows[-1].copy()
        self.previous_road_uv=uv[-1].copy()
        textures=namespace_textures(scene,textures_dir)
        scene['textures_dir']=str(Path(scene['textures_dir'])/'kn5').replace('\\','/')
        for material in scene['materials']:
            material['name']=f'S{section["index"]:04d}_'+material['name']
        scene['metadata']['id']=f'{self.track_id}_s{section["index"]:04d}'
        filename=f'{self.track_id}_s{section["index"]:04d}.kn5'
        path=self.track/filename
        write(path,scene,textures)
        info=inspect(path,geometry=True)
        exported=next(m for m in info['meshes'] if m['name'].startswith('1ROAD_MAIN'))
        exported_rows=exported['positions'].reshape(-1,7,3)
        # Float32 centimetre-level precision is the actual engine boundary on
        # very large maps; compare serialized bytes, not only Python doubles.
        if not np.array_equal(exported_rows,np.asarray(rows,dtype=np.float32)):
            raise ValueError('Serialized road differs from shared profile')
        a,b=section['core_start'],section['core_end']+1
        expected_width=np.asarray(self.profile['road_widths'])[a:b]
        if not np.allclose(np.linalg.norm(exported_rows[:,-1]-exported_rows[:,0],axis=1),expected_width,atol=.006,rtol=0):
            raise ValueError('Exported road width differs from shared profile')
        lateral=np.linalg.norm(exported_rows[:,:,[0,2]]-exported_rows[:,3:4,[0,2]],axis=2)
        if not np.allclose(exported_rows[:,:,1],exported_rows[:,3:4,1]-lateral*self.profile['crossfall'],atol=.001,rtol=0):
            raise ValueError('Exported road drainage crown differs from shared profile')
        center=exported_rows[:,3]
        step=np.linalg.norm(np.diff(center[:,[0,2]],axis=0),axis=1)
        grades=np.diff(center[:,1])/step
        if np.any(step<=0) or np.max(np.abs(grades))>.25 or (len(grades)>1 and np.max(np.abs(np.diff(grades)))>.04):
            raise ValueError('Exported road gradient is unsafe')
        for mesh in info['meshes']:
            if not np.allclose(np.linalg.norm(mesh['normals'],axis=1),1,atol=1e-4):
                raise ValueError('Invalid serialized mesh normals')
            if mesh['name'].startswith(('1ROAD','1SHOULDER','1GRASS')):
                tri=mesh['positions'][mesh['indices'].reshape(-1,3)]
                if np.any(np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])[:,1]<=1e-8):
                    raise ValueError('Reversed or degenerate physical surface')
        for name,matrix in info['markers'].items():
            matrix=np.asarray(matrix);position=matrix[3,:3]
            if not np.allclose(matrix[:3,:3]@matrix[:3,:3].T,np.eye(3),atol=1e-5) or np.linalg.det(matrix[:3,:3])<.999:
                raise ValueError('Invalid spawn orientation')
            if name.startswith(('AC_PIT_','AC_START_','AC_HOTLAP_START_')):
                nearest=np.argmin(np.linalg.norm(center[:,[0,2]]-position[[0,2]],axis=1))
                if not .15<position[1]-center[nearest,1]<.35:
                    raise ValueError('Spawn must be above the generated road')
        with path.open('rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        record={'file':filename,'index':section['index'],'start_m':section['start_m'],
                'end_m':section['end_m'],'bytes':path.stat().st_size,'sha256':digest,
                'vertices':sum(m['vertices'] for m in info['meshes']),
                'triangles':sum(m['triangles'] for m in info['meshes']),**seam_stats}
        if scene['metadata'].get('subresolution_clipping_cleanup'):
            record['subresolution_clipping_cleanup']=dict(scene['metadata']['subresolution_clipping_cleanup'])
        self.models.append(record)
        return record

    def finish(self):
        if len(self.models)!=len(self.sections):
            raise ValueError('Cannot package an incomplete route')
        content='\n'.join(f'[MODEL_{i}]\nFILE={model["file"]}\nPOSITION=0,0,0\nROTATION=0,0,0\n'
                          for i,model in enumerate(self.models))
        (self.track/'models.ini').write_text(content,encoding='utf-8')
        return deepcopy(self.models)


def aggregate_metadata(first_metadata, profile, models):
    """Compact whole-track description; detailed records stay per section."""
    result={key:deepcopy(value) for key,value in first_metadata.items()
            if key not in ('building_base_records','building_base_source_footprints','section','subresolution_clipping_cleanup')}
    xy=np.asarray(profile['xy']);height=np.asarray(profile['road_y'])
    origin=profile['origin_utm32'];height_origin=profile['height_origin_m']
    spacing=np.diff(profile['distance'])
    grade=np.diff(height)/spacing
    tangent=np.asarray(profile['tangent'])
    curve=np.abs(np.diff(np.unwrap(np.arctan2(tangent[:,1],tangent[:,0]))))/spacing
    result.update(length_m=float(profile['distance'][-1]),
                  elevation_min_m=float(height.min()),elevation_max_m=float(height.max()),
                  max_gradient_percent=float(np.max(np.abs(grade))*100),
                  minimum_radius_m=float(1/max(float(curve.max()),1e-9)),
                  origin_utm32=np.asarray(origin).tolist(),height_origin_m=height_origin,
                  centerline_ac=np.c_[xy[:,0]-origin[0],height-height_origin,-(xy[:,1]-origin[1])].round(5).tolist(),
                  road_width_profile_m=np.asarray(profile['road_widths']).round(5).tolist(),
                  models=models,section_count=len(models),sections_count=len(models),model_count=len(models),
                  section_export='One continuous route; bounded section generation; one shared global road profile',
                  preview_scope='representative_section')
    cleanup=[m['subresolution_clipping_cleanup'] for m in models if m.get('subresolution_clipping_cleanup')]
    if cleanup:
        result['subresolution_clipping_cleanup']={'triangles':sum(v['triangles'] for v in cleanup),
            'projected_area_m2':sum(v['projected_area_m2'] for v in cleanup)}
    return result
