"""Local, measurable ground resolution and restrained inferred house aprons.

The DGM is retained outside a narrow road tie-in. Aprons are visual estimates,
never cadastral courtyards or surveyed walls, and never alter the driving mesh.
All inputs use EPSG:25832 metres and absolute DHHN2016 elevations.
"""
from __future__ import annotations

from xml.etree import ElementTree as ET
import numpy as np
from pyproj import Transformer
from scipy.spatial import cKDTree, Delaunay
from shapely import (STRtree, points as geometry_points, covers as geometry_covers,
                     polygons as geometry_polygons, constrained_delaunay_triangles,
                     linestrings as geometry_linestrings, get_parts, get_coordinates,
                     set_precision, union_all, polygonize)
from shapely.geometry import Polygon, MultiPoint, Point, LineString, box
from shapely.ops import unary_union
from shapely.prepared import prep


class RoadGroundProfile:
    """Callable ``(points[n,2], raw_dgm[n]) -> adjusted_heights[n]``.

    ``half_width`` may be one positive number or one value per centreline node.
    Segment projection interpolates elevation and width, avoiding steps caused
    by nearest-node heights. Optional synthetic crossfall matches the road mesh.
    Full grading stops 0.45 m outside the physical road; the next 2 m
    return to the DGM. With a source sampler, fixed outer grades form a
    monotone straight transition instead of a rounded moving-target ramp.
    """
    def __init__(self, xy, road_y, half_width, shoulder=.45, blend=2., crossfall=0.,source_sampler=None):
        self.xy = np.asarray(xy, dtype=float)
        self.road_y = np.asarray(road_y, dtype=float)
        self.half_width = np.broadcast_to(np.asarray(half_width, dtype=float), (len(self.xy),)).copy()
        self.shoulder, self.blend = float(shoulder), float(blend)
        self.crossfall = float(crossfall)
        if source_sampler is not None and not callable(source_sampler):
            raise ValueError('Source terrain sampler must be callable')
        self.source_sampler=source_sampler
        if (self.xy.ndim != 2 or self.xy.shape[1] != 2 or len(self.xy) < 2
                or self.road_y.shape != (len(self.xy),)
                or not np.isfinite(self.xy).all() or not np.isfinite(self.road_y).all()
                or not np.isfinite(self.half_width).all() or np.any(self.half_width <= 0)
                or not np.isfinite([shoulder, blend, crossfall]).all() or shoulder < 0 or blend <= 0
                or not 0 <= crossfall <= .04):
            raise ValueError('Invalid road ground profile')
        self.vectors = np.diff(self.xy, axis=0)
        self.length2 = np.sum(self.vectors ** 2, axis=1)
        if np.any(self.length2 < 1e-4):
            raise ValueError('Road ground profile contains duplicate nodes')
        self.tree = cKDTree(self.xy)

    def sample(self, points, *, return_projection=False):
        """Return nearest segment distance, interpolated road height and width."""
        points = np.asarray(points, dtype=float)
        if points.ndim != 2 or points.shape[1] != 2 or not np.isfinite(points).all():
            raise ValueError('Expected finite ground points[n,2]')
        if len(points) == 0:
            base=(np.empty(0),np.empty(0),np.empty(0))
            return (*base,np.empty((0,2))) if return_projection else base
        output = [[], [], [], []] if return_projection else [[], [], []]
        # Bound temporary memory for a large downloaded DGM footprint.
        for start in range(0, len(points), 24000):
            p = points[start:start+24000]
            _, nearest = self.tree.query(p, k=min(8, len(self.xy)))
            nearest = np.asarray(nearest).reshape(len(p), -1)
            candidates = np.clip(np.concatenate((nearest-1, nearest), axis=1), 0, len(self.xy)-2)
            begin = self.xy[candidates]
            vectors = self.vectors[candidates]
            t = np.clip(np.sum((p[:, None, :]-begin)*vectors, axis=2)/self.length2[candidates], 0, 1)
            projected = begin+t[:, :, None]*vectors
            distances2 = np.sum((p[:, None, :]-projected)**2, axis=2)
            col = np.argmin(distances2, axis=1)
            row = np.arange(len(p)); segment = candidates[row, col]; fraction = t[row, col]
            output[0].append(np.sqrt(distances2[row, col]))
            output[1].append(self.road_y[segment]*(1-fraction)+self.road_y[segment+1]*fraction)
            output[2].append(self.half_width[segment]*(1-fraction)+self.half_width[segment+1]*fraction)
            if return_projection:
                output[3].append(projected[row,col])
        return tuple(np.concatenate(values) for values in output)

    def __call__(self, points, raw_values):
        raw_values = np.asarray(raw_values, dtype=float)
        if self.source_sampler is None:
            lateral,height,half_width=self.sample(points)
        else:
            lateral,height,half_width,projected=self.sample(points,return_projection=True)
        height = height-self.crossfall*np.minimum(lateral, half_width)
        if raw_values.shape != lateral.shape or not np.isfinite(raw_values).all():
            raise ValueError('Expected one finite DGM height per ground point')
        weight = np.clip((lateral-half_width-self.shoulder)/self.blend, 0., 1.)
        if self.source_sampler is not None:
            result=height.copy()
            result[weight>=1]=raw_values[weight>=1]
            local=np.flatnonzero((weight>0)&(weight<1))
            if len(local):
                outward=(np.asarray(points,dtype=float)[local]-projected[local])/lateral[local,None]
                outer=projected[local]+outward*(half_width[local]+self.shoulder+self.blend)[:,None]
                anchor=np.asarray(self.source_sampler(outer),dtype=float)
                if anchor.shape!=(len(local),) or not np.isfinite(anchor).all():
                    raise ValueError('Invalid outer DGM grade')
                phase=weight[local]
                result[local]=height[local]*(1-phase)+anchor*phase
            return result
        weight = weight*weight*(3.-2.*weight)
        return height*(1.-weight)+raw_values*weight


def _surface_data(sections):
    result=[]
    for section in sections:
        xy=np.asarray(section['xy'],dtype=float)
        top=np.asarray(section['top'],dtype=float)
        faces=np.asarray(section.get('faces',[[0,1,2],[0,2,3]]),dtype=int)
        if (xy.ndim!=2 or xy.shape[1]!=2 or top.shape!=(len(xy),)
                or len(xy)<3 or not np.isfinite(xy).all() or not np.isfinite(top).all()
                or faces.ndim!=2 or faces.shape[1]!=3 or np.any(faces<0) or np.any(faces>=len(xy))):
            raise ValueError('Invalid planned contact surface')
        polygon=Polygon(xy)
        if not polygon.is_valid or polygon.area<1e-5:
            raise ValueError('Invalid planned contact polygon')
        triangles=np.dstack([xy[faces],top[faces]])
        for triangle in triangles:
            if abs(np.linalg.det((triangle[1:,:2]-triangle[0,:2]).T))<1e-8:
                raise ValueError('Degenerate planned contact triangle')
        result.append((section,xy,top,faces,polygon,triangles))
    return result


def protected_road_surfaces(scene, origin, height_origin):
    """Exact world-space driving triangles used only to constrain the ground.

    Read the already created road/shoulder meshes, including scene rounding.
    No second reconstruction of their width, crown or vertical alignment.
    """
    triangles=[]
    for mesh in scene.get('meshes',[]):
        if mesh.get('name') not in ('1ROAD_MAIN','1SHOULDER_LEFT','1SHOULDER_RIGHT'):
            continue
        vertices=np.asarray(mesh['vertices'],dtype=float)
        faces=np.asarray(mesh['faces'],dtype=int)
        world=np.c_[vertices[:,0]+origin[0],origin[1]-vertices[:,2],vertices[:,1]+height_origin]
        triangles.append(world[faces])
    return np.concatenate(triangles) if triangles else np.empty((0,3,3))


class StreetGroundProfile:
    """Grade accepted paving/footing footprints and their short outside tie-in.

    Wraps RoadGroundProfile before terrain generation. Planned surface tops are
    exact piecewise planes. Terrain remains 35 mm underneath their solids; the
    historical 80-mm terrain export offset is accounted for here, not applied
    to the plans. Terrain outside this local footprint/tie-in is unchanged.
    """
    def __init__(self,road_profile,street_surfaces,road_polygon,*,contact_surfaces=(),
                 foundation_surfaces=(),hillside_surfaces=(),protected_surfaces=(),source_sampler=None,blend=.8):
        if not np.isfinite(blend) or not .2<=blend<=1.5:
            raise ValueError('Street-ground blend must lie between 0.2 and 1.5 metres')
        self.road_profile=road_profile
        self.contact_blend=float(blend)
        if source_sampler is not None and not callable(source_sampler):
            raise ValueError('Source terrain sampler must be callable')
        self.source_sampler=source_sampler
        self.foundation_count=len(foundation_surfaces)
        self.hillside_count=len(hillside_surfaces)
        # Footing support extends 40 mm behind the original wall plane. The
        # visible gravel starts 18 mm in front, leaving its separation joint;
        # the hidden extension prevents a thin ungraded ridge inside that gap.
        footing_grade=[dict(section,xy=section.get('terrain_xy',section['xy']),
                            top=section.get('terrain_top',section['top'])) for section in foundation_surfaces]
        self.sections=_surface_data([*street_surfaces,*contact_surfaces,*footing_grade,*hillside_surfaces])
        self.road_reservation=road_polygon.buffer(.49)
        self.contact_polygons=[row[4] for row in self.sections]
        self.contact_area=unary_union(self.contact_polygons)
        self.triangles=np.concatenate([row[5] for row in self.sections]) if self.sections else np.empty((0,3,3))
        self.triangle_polygons=[Polygon(t[:,:2]) for t in self.triangles]
        self.surface_tree=STRtree(self.triangle_polygons)
        protected=np.asarray(protected_surfaces,dtype=float)
        if not protected.size:
            protected=np.empty((0,3,3))
        if protected.ndim!=3 or protected.shape[1:]!=(3,3) or not np.isfinite(protected).all():
            raise ValueError('Invalid protected road triangles')
        self.protected_triangles=protected
        self.protected_tree=STRtree([Polygon(t[:,:2]) for t in protected])
        self.constrained_triangles=np.concatenate([self.triangles,protected])
        self.constrained_polygons=list(set_precision(self.triangle_polygons,.002))
        if len(protected):
            # Driving surfaces need their outside construction contour, not
            # thousands of interior lane triangles. Their exact planes still
            # supply every sampled height, with 80 mm protective clearance.
            reserved=unary_union(set_precision([Polygon(t[:,:2]) for t in protected],.002))
            self.constrained_polygons.extend(get_parts(reserved))
        self.constrained_tree=STRtree(self.constrained_polygons)
        buffered_contacts=prep(self.contact_area.buffer(.001))
        edges=[]
        for section,xy,top,faces,polygon,triangles in self.sections:
            # The inner sidewalk edge meets the kerb, and the rear house edge
            # meets the wall: neither should flatten its other side.
            excluded=set(section.get('inner_edge_indices',[0,1]))
            for i in range(len(xy)):
                j=(i+1)%len(xy)
                if {i,j}==excluded:
                    continue
                delta=xy[j]-xy[i];length=np.linalg.norm(delta)
                if length<.01:
                    continue
                outward=np.array([-delta[1],delta[0]])/length
                midpoint=(xy[i]+xy[j])/2
                if polygon.contains(Point(midpoint+outward*.005)):
                    outward=-outward
                # Shared metre cells must behave as one surface, rather than
                # producing a separate embankment for each tile strip.
                if buffered_contacts.contains(Point(midpoint+outward*.012)):
                    continue
                edges.append({'xy':xy[[i,j]],'top':top[[i,j]],'normal':outward})
        self.edges=edges
        self.edge_tree=STRtree([LineString(edge['xy']) for edge in edges])
        self.constraint_count=0
        self.constrained_face_count=0

    def __getattr__(self,name):
        return getattr(self.road_profile,name)

    def sample(self,points):
        return self.road_profile.sample(points)

    def __call__(self,points,raw_values):
        xy=np.asarray(points,dtype=float)
        result=self.road_profile(xy,raw_values).copy()
        if not len(xy) or not len(self.constrained_triangles):
            return result
        geometry=geometry_points(xy)
        safe=~geometry_covers(self.road_reservation,geometry)
        inside=np.zeros(len(xy),dtype=bool)
        target=np.full(len(xy),np.inf)
        for tree,triangles,offset in ((self.surface_tree,self.triangles,.045),
                                      (self.protected_tree,self.protected_triangles,0.)):
            # Terrain topology uses a 2-mm precision grid to prevent nearly
            # coincident construction contours creating float32 slivers. The
            # visible source surfaces stay untouched; evaluate their original
            # planes at these nearby terrain nodes.
            pairs=tree.query(geometry,predicate='dwithin',distance=.003)
            if not pairs.size:
                continue
            point_index,triangle_index=pairs
            t=triangles[triangle_index]
            a=t[:,1,:2]-t[:,0,:2];b=t[:,2,:2]-t[:,0,:2];q=xy[point_index]-t[:,0,:2]
            determinant=a[:,0]*b[:,1]-a[:,1]*b[:,0]
            wa=(q[:,0]*b[:,1]-q[:,1]*b[:,0])/determinant
            wb=(a[:,0]*q[:,1]-a[:,1]*q[:,0])/determinant
            height=t[:,0,2]*(1-wa-wb)+t[:,1,2]*wa+t[:,2,2]*wb
            # Shared step boundaries must stay below both adjoining solids.
            # Otherwise a terrain vertex may poke through the lower landing.
            np.minimum.at(target,point_index,height+offset)
            inside[point_index]=True
        result[np.isfinite(target)]=target[np.isfinite(target)]
        available=np.flatnonzero(safe & ~inside)
        if len(available) and self.edges:
            nearest,distance=self.edge_tree.query_nearest(geometry[available],all_matches=False,return_distance=True)
            point_index=available[nearest[0]];edge_index=nearest[1]
            within=distance<self.contact_blend
            point_index=point_index[within];edge_index=edge_index[within];distance=distance[within]
            for index in np.unique(edge_index):
                use=edge_index==index;ids=point_index[use];edge=self.edges[int(index)]
                delta=edge['xy'][1]-edge['xy'][0]
                fraction=np.clip((xy[ids]-edge['xy'][0])@delta/np.dot(delta,delta),0,1)
                projected=edge['xy'][0]+fraction[:,None]*delta
                outward=(xy[ids]-projected)@edge['normal']>=-1e-7
                ids=ids[outward];fraction=fraction[outward]
                phase=np.clip(distance[use][outward]/self.contact_blend,0,1)
                height=edge['top'][0]*(1-fraction)+edge['top'][1]*fraction+.045
                if self.source_sampler is None:
                    weight=1-phase*phase*(3-2*phase)
                    result[ids]=result[ids]*(1-weight)+height*weight
                else:
                    # A fixed outer grade prevents the old moving-target
                    # smoothstep ramp from inflating an already rising DGM
                    # into a rounded berm. The cross-section is a straight,
                    # monotone tie-in, ending at the unchanged outer terrain.
                    outer=projected[outward]+edge['normal']*self.contact_blend
                    source=np.asarray(self.source_sampler(outer),dtype=float)
                    if source.shape!=(len(outer),) or not np.isfinite(source).all():
                        raise ValueError('Invalid source grade at terrain tie-in')
                    end=self.road_profile(outer,source)
                    result[ids]=height*(1-phase)+end*phase
        return result

    def constraint_points(self):
        """Retain construction edges and a short transition in terrain mesh."""
        samples=[]
        for _,xy,top,faces,polygon,triangles in self.sections:
            for a,b in zip(xy,np.roll(xy,-1,axis=0)):
                t=np.linspace(0,1,max(2,int(np.ceil(np.linalg.norm(b-a)/.38))+1))
                samples.append(a[None,:]*(1-t[:,None])+b[None,:]*t[:,None])
            # Interior points keep broad accepted forecourts on their declared
            # plane without increasing resolution away from construction.
            lo=np.min(xy,axis=0);hi=np.max(xy,axis=0)
            xx,yy=np.meshgrid(np.arange(lo[0],hi[0],.45),np.arange(lo[1],hi[1],.45))
            p=np.c_[xx.ravel(),yy.ravel()]
            if len(p):
                samples.append(p[np.array([polygon.covers(Point(v)) for v in p])])
        for edge in self.edges:
            a,b=edge['xy'];t=np.linspace(0,1,max(2,int(np.ceil(np.linalg.norm(b-a)/.38))+1))
            line=a[None,:]*(1-t[:,None])+b[None,:]*t[:,None]
            for offset in (.2,.45,self.contact_blend):
                samples.append(line+edge['normal']*offset)
        output=np.vstack(samples) if samples else np.empty((0,2))
        self.constraint_count=len(output)
        return output


def _constrain_contact_faces(points,faces,profile):
    """Build one common, noded terrain/construction planar line network.

    Independent per-face clipping with rounding can leave gaps between two
    neighbours. Noding the whole shared network once makes the intersection
    vertices identical on both sides; polygonisation partitions it without
    overlaps or holes. Away from construction, original DGM edges are retained.
    """
    if not isinstance(profile,StreetGroundProfile) or not len(profile.constrained_triangles):
        return points,faces
    edges=np.unique(np.sort(np.concatenate([faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]]),axis=1),axis=0)
    lines=list(geometry_linestrings(points[edges]))
    hull=MultiPoint(points).convex_hull
    for polygon in profile.constrained_polygons:
        boundary=polygon.boundary.intersection(hull,grid_size=.002)
        if not boundary.is_empty:
            lines.append(boundary)
    # All linework uses the same precision model in one union operation.
    # Per-polygon rounding after clipping would break the shared arrangement.
    network=union_all(np.asarray(lines,dtype=object),grid_size=.002)
    cells=get_parts(polygonize(get_parts(network)))
    triangles=get_parts(constrained_delaunay_triangles(cells))
    coordinates=get_coordinates(triangles).reshape(-1,4,2)[:,:3]
    combined=coordinates.reshape(-1,2)
    origin=combined.min(axis=0)
    keys=np.round((combined-origin)*1e7).astype(np.int64)
    _,first,inverse=np.unique(keys,axis=0,return_index=True,return_inverse=True)
    output=combined[first];output_faces=inverse.reshape(-1,3)
    # CDT may simplify a collinear point along one of two adjacent cells.
    # Reinsert it before export, closing potential float32 T-junction cracks.
    output,output_faces=_node_terrain_edges(output,output_faces,np.arange(len(output)))
    p=output[output_faces];a,b=p[:,1]-p[:,0],p[:,2]-p[:,0]
    signed=a[:,0]*b[:,1]-a[:,1]*b[:,0]
    output_faces=output_faces[np.abs(signed)>1e-10]
    signed=signed[np.abs(signed)>1e-10]
    output_faces[signed<0]=output_faces[signed<0,::-1]
    used,remap=np.unique(output_faces,return_inverse=True)
    profile.constrained_face_count=len(cells)
    return output[used],remap.reshape(-1,3)


def _node_terrain_edges(points,faces,candidates):
    edges,inverse=np.unique(np.sort(np.concatenate([faces[:,[0,1]],faces[:,[1,2]],faces[:,[2,0]]]),axis=1),
                            axis=0,return_inverse=True)
    edge_tree=STRtree(geometry_linestrings(points[edges]))
    pairs=edge_tree.query(geometry_points(points[candidates]),predicate='dwithin',distance=.000001)
    if not pairs.size:
        return points,faces
    node,edge_index=candidates[pairs[0]],pairs[1]
    use=(node!=edges[edge_index,0])&(node!=edges[edge_index,1])
    node,edge_index=node[use],edge_index[use]
    delta=points[edges[edge_index,1]]-points[edges[edge_index,0]]
    length2=np.sum(delta*delta,axis=1)
    fraction=np.sum((points[node]-points[edges[edge_index,0]])*delta,axis=1)/np.maximum(length2,1e-20)
    use=(fraction>1e-8)&(fraction<1-1e-8)&(length2>1e-12)
    node,edge_index,fraction=node[use],edge_index[use],fraction[use]
    if not len(node):
        return points,faces
    inserted={}
    for edge in np.unique(edge_index):
        select=np.flatnonzero(edge_index==edge)
        inserted[int(edge)]=node[select[np.argsort(fraction[select])]].tolist()
    face_edges=inverse.reshape(3,-1).T
    changed=np.any(np.isin(face_edges,list(inserted)),axis=1)
    new_points=[];new_faces=[]
    for face_index in np.flatnonzero(changed):
        face=faces[face_index];boundary=[]
        for local,(a,b) in enumerate(zip(face,np.roll(face,-1))):
            boundary.append(a)
            edge=int(face_edges[face_index,local])
            if edge in inserted:
                extra=inserted[edge]
                boundary.extend(extra if a==edges[edge,0] else extra[::-1])
        center=len(points)+len(new_points)
        new_points.append(points[face].mean(axis=0))
        new_faces.extend((center,a,b) for a,b in zip(boundary,np.roll(boundary,-1)))
    return np.vstack([points,new_points]),np.vstack([faces[~changed],new_faces])


def terrain_samples(bounds, base_spacing, profile, fine_spacing=1., corridor=18.):
    """Adaptive Delaunay input: coarse far ground, 1-m DGM near the street.

    Road edges and grading limits are explicit samples so coarse triangles
    cannot erase the small road/ground transition. Does not oversample DGM1
    with an invented high-resolution terrain surface.
    """
    bounds = np.asarray(bounds, dtype=float)
    if (bounds.shape != (4,) or not np.isfinite(bounds).all()
            or np.any(bounds[2:]-bounds[:2] <= 5)
            or not np.isfinite([base_spacing, fine_spacing, corridor]).all()
            or base_spacing < fine_spacing or fine_spacing <= 0 or corridor < 3):
        raise ValueError('Invalid adaptive terrain extent or spacing')
    lower, upper = bounds[:2]+2., bounds[2:]-2.
    x = np.arange(lower[0], upper[0], base_spacing)
    y = np.arange(lower[1], upper[1], base_spacing)
    xx, yy = np.meshgrid(x, y)
    coarse = np.c_[xx.ravel(), yy.ravel()]
    local_low = np.maximum(lower, profile.xy.min(axis=0)-corridor)
    local_low = lower+np.floor((local_low-lower)/fine_spacing)*fine_spacing
    local_high = np.minimum(upper, profile.xy.max(axis=0)+corridor)
    xx, yy = np.meshgrid(np.arange(local_low[0], local_high[0], fine_spacing),
                         np.arange(local_low[1], local_high[1], fine_spacing))
    near = np.c_[xx.ravel(), yy.ravel()]
    lateral, _, _ = profile.sample(near)
    near = near[lateral <= corridor]
    tangent = np.gradient(profile.xy, axis=0)
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    left = np.c_[-tangent[:, 1], tangent[:, 0]]
    points = [coarse, near]
    for side in (-1, 1):
        for extra in (0., profile.shoulder, profile.shoulder+profile.blend):
            points.append(profile.xy+left*(side*(profile.half_width+extra))[:, None])
    points = np.vstack(points)
    points = points[np.all(points >= lower, axis=1) & np.all(points <= upper, axis=1)]
    points = np.unique(points.round(4), axis=0)
    # Avoid hairline triangles where an exact road-edge sample almost hits a
    # grid sample. Two centimetres are far below the DGM1 source resolution.
    close = cKDTree(points).query_pairs(.025, output_type='ndarray')
    if len(close):
        keep = np.ones(len(points), dtype=bool); keep[np.unique(close[:, 1])] = False
        points = points[keep]
    if hasattr(profile,'constraint_points'):
        # Construction contours take precedence over background DGM samples.
        # Never merge them into a grid point with the old 25-mm distance rule.
        constraints=profile.constraint_points()
        constraints=constraints[np.all(constraints>=lower,axis=1)&np.all(constraints<=upper,axis=1)]
        if len(constraints):
            distance,_=cKDTree(constraints).query(points)
            points=np.unique(np.vstack([points[distance>=.025],constraints]),axis=0)
    if isinstance(profile,StreetGroundProfile):
        points=np.unique(np.round(points/.002)*.002,axis=0)
    faces = Delaunay(points).simplices
    # scipy promises counter-clockwise planar faces; enforce this explicitly.
    tri = points[faces]
    sign = (tri[:, 1, 0]-tri[:, 0, 0])*(tri[:, 2, 1]-tri[:, 0, 1])-(tri[:, 1, 1]-tri[:, 0, 1])*(tri[:, 2, 0]-tri[:, 0, 0])
    faces[sign < 0] = faces[sign < 0, ::-1]
    faces = faces[np.abs(sign) > 1e-5]
    used, remap = np.unique(faces, return_inverse=True)
    points, faces = points[used], remap.reshape(-1, 3)
    return _constrain_contact_faces(points,faces,profile)


def build_terrain_mesh(bounds, base_spacing, profile, dem, ac, add_mesh, material,
                       ortho_bounds, fine_spacing=1., corridor=18., scene=None):
    """Emit AC ground pieces <= 60k vertices; replacement for prepare's grid.

    ``add_mesh`` has prepare's ``(name, points, faces, mat, uv, collision)`` API.
    Only this terrain is colliding. Decorative house aprons below are not.
    Pass the scene dict to attach global vertex normals to every exported
    piece, preventing artificial lighting seams at the 16-bit split boundary.
    """
    points, faces = terrain_samples(bounds, base_spacing, profile, fine_spacing, corridor)
    heights = profile(points, dem(points))-.08
    vertices = ac(points, heights)
    from roadtoassetto.kn5 import normals
    global_normals = normals(vertices, faces) if scene is not None else None
    ortho_bounds = np.asarray(ortho_bounds, dtype=float)
    uv = np.c_[(points[:, 0]-ortho_bounds[0])/(ortho_bounds[2]-ortho_bounds[0]),
                1-(points[:, 1]-ortho_bounds[1])/(ortho_bounds[3]-ortho_bounds[1])]
    parts = 0
    # 18k independent faces cannot exceed the KN5 16-bit vertex-index limit.
    for start in range(0, len(faces), 18000):
        f = faces[start:start+18000]
        used, remap = np.unique(f, return_inverse=True)
        name = f'1GRASS_TERRAIN_P{parts}'
        add_mesh(name, vertices[used], remap.reshape(-1, 3),
                 material, uv[used], True)
        if scene is not None:
            if scene['meshes'][-1]['name'] != name:
                raise ValueError('Terrain callback did not append expected scene mesh')
            scene['meshes'][-1]['normals'] = global_normals[used].round(7).tolist()
        parts += 1
    result={'terrain_vertices': len(points), 'terrain_triangles': len(faces),
            'terrain_near_spacing_m': fine_spacing, 'terrain_near_corridor_m': corridor,
            'terrain_grade_shoulder_m': profile.shoulder, 'terrain_grade_blend_m': profile.blend,
            'terrain_resolution_method': 'DGM1 samples at 1 m near road, original configured spacing away from road; measured terrain retained outside the local tie-in'}
    if isinstance(profile,StreetGroundProfile):
        result.update(terrain_contact_surfaces=len(profile.sections),
                      terrain_contact_support_points=profile.constraint_count,
                      terrain_construction_partition_cells=profile.constrained_face_count,
                      terrain_construction_precision_m=.002,
                      terrain_contact_triangulation='One shared noded planar network, 2-mm terrain precision, constrained paving and house-contact planes, protected road and shoulder outer contours; visible surface meshes unchanged',
                      terrain_contact_blend_m=profile.contact_blend,
                      terrain_contact_clearance_m=.035,
                      terrain_foundation_surfaces=profile.foundation_count,
                      terrain_hillside_surfaces=profile.hillside_count,
                      terrain_outer_tie_in=('Fixed outer DGM grade, monotone straight cross-section' if profile.source_sampler is not None
                                            else 'Legacy local smoothstep blend'),
                      terrain_contact_accuracy='Inferred local grading under accepted paving and house connections; source DGM retained beyond the short outside tie-in; road mesh unchanged')
    return result


def apron_panels(edge, outward, ground_sampler, road_reservation, other_buildings,
                 width=.72, max_gap=.6):
    """Short, closed terrace cells against an observed LoD2 wall bottom edge.

    Reject buried floors, large datum mismatches and road/neighbor overlap.
    Returns world XY, sampled base height and source-edge-derived top height.
    Source wall coordinates and all street geometry remain untouched.
    """
    edge = np.asarray(edge, dtype=float)
    outward = np.asarray(outward, dtype=float)
    if edge.shape != (2, 3) or outward.shape != (2,) or not np.isfinite(edge).all() or not np.isfinite(outward).all():
        raise ValueError('Invalid wall edge for ground apron')
    length = np.linalg.norm(edge[1, :2]-edge[0, :2])
    if length < .8 or abs(edge[1, 2]-edge[0, 2])/length > .12:
        return []
    outward = outward/max(np.linalg.norm(outward), 1e-8)
    samples = np.linspace(0., 1., int(np.ceil(length/1.25))+1)
    panels = []
    for a, b in zip(samples[:-1], samples[1:]):
        first = edge[0]*(1-a)+edge[1]*a; last = edge[0]*(1-b)+edge[1]*b
        # A small recessed gap avoids coplanar walls and closes in the plinth.
        p = np.array([first[:2]+outward*.025, last[:2]+outward*.025,
                      last[:2]+outward*width, first[:2]+outward*width])
        poly = Polygon(p)
        if (not poly.is_valid or poly.area < .1 or road_reservation.intersects(poly)
                or other_buildings.intersects(poly)):
            continue
        ground = np.asarray(ground_sampler(p), dtype=float)
        top = np.array([first[2], last[2], last[2], first[2]])+.028
        gap = top-ground
        if (ground.shape != (4,) or not np.isfinite(ground).all()
                or gap.min() < -.035 or gap.max() > max_gap):
            continue
        panels.append((p, np.minimum(ground-.09, top-.075), top))
    return panels


def _ground_buildings(data_dir,line,road_polygon,bounds):
    from roadtoassetto.buildings import GML,WFS,BU
    transform=Transformer.from_crs(4326,25832,always_xy=True)
    buildings=[]
    for member in ET.parse(data_dir/'buildings_lod2.gml').getroot().findall(WFS+'member'):
        building=member.find(BU+'Building')
        if building is None:
            continue
        rings=[]
        for pos in building.findall('.//'+GML+'exterior/'+GML+'LinearRing/'+GML+'posList'):
            if not pos.text:
                continue
            coords=np.fromstring(pos.text,sep=' ').reshape(-1,3)
            x,y=transform.transform(coords[:,1],coords[:,0])
            rings.append(np.c_[x,y,coords[:,2]])
        if not rings:
            continue
        footprint=MultiPoint(np.vstack(rings)[:,:2]).convex_hull
        if (isinstance(footprint,Polygon) and footprint.distance(line)<22.
                and box(*bounds).buffer(-2).covers(footprint)
                and footprint.intersection(road_polygon).area<.02):
            buildings.append((footprint,rings))
    return buildings


def _wall_frontage_edges(footprint,rings,line):
    seen=set()
    for ring in rings:
        if np.ptp(ring[:,2])<1.5:
            continue
        min_z=ring[:,2].min()
        for first,last in zip(ring[:-1],ring[1:]):
            edge=np.array([first,last]);direction=last[:2]-first[:2];length=np.linalg.norm(direction)
            if length<.8 or max(edge[:,2])>min_z+.4 or abs(last[2]-first[2])/length>.12:
                continue
            midpoint=edge[:,:2].mean(axis=0)
            if line.distance(Point(midpoint))>18:
                continue
            normal=np.array([-direction[1],direction[0]])/length
            if footprint.contains(Point(midpoint+normal*.2)):
                normal=-normal
            nearest=np.array(line.interpolate(line.project(Point(midpoint))).coords[0])
            if np.dot(normal,nearest-midpoint)<np.linalg.norm(nearest-midpoint)*.35:
                continue
            key=tuple(sorted((tuple(first.round(3)),tuple(last.round(3)))))
            if key in seen:
                continue
            seen.add(key)
            yield edge,normal


class PavementContactIndex:
    """Actual accepted pavement rear edges, never a fixed road buffer."""
    def __init__(self,street_surfaces):
        self.edges=[]
        for section,xy,top,_,_,_ in _surface_data(street_surfaces):
            index=section.get('outer_edge_indices',[3,2])
            self.edges.append({'xy':xy[index],'top':top[index],'section':section})
        self.tree=STRtree([LineString(row['xy']) for row in self.edges])

    def nearest(self,point,outward,max_distance=2.8):
        point=np.asarray(point,dtype=float);best=None
        for index in self.tree.query(Point(point).buffer(max_distance)):
            edge=self.edges[int(index)];a,b=edge['xy'];delta=b-a
            fraction=float(np.clip(np.dot(point-a,delta)/np.dot(delta,delta),0,1))
            target=a+fraction*delta;vector=target-point;distance=float(np.linalg.norm(vector))
            if distance<.035 or distance>max_distance or np.dot(vector,outward)<distance*.6:
                continue
            if best is None or distance<best['distance']:
                best={'xy':target,'top':float(edge['top'][0]*(1-fraction)+edge['top'][1]*fraction),
                      'distance':distance,'section':edge['section']}
        return best


def connection_surfaces(edge,outward,pavement_index,ground_sampler,road_reservation,
                        other_buildings,max_reach=2.8):
    """Join one real wall bottom edge to reachable, accepted sidewalk edges.

    Short source-to-pavement differences become gently graded paving. Positive
    differences up to 34 cm can become at most two shallow, embedded steps.
    Larger or uncertain differences are rejected rather than stretched ramps.
    """
    edge=np.asarray(edge,dtype=float);outward=np.asarray(outward,dtype=float)
    if edge.shape!=(2,3) or outward.shape!=(2,) or not np.isfinite(edge).all() or not np.isfinite(outward).all():
        raise ValueError('Invalid source wall connection edge')
    length=np.linalg.norm(edge[1,:2]-edge[0,:2])
    if length<.8 or abs(edge[1,2]-edge[0,2])/length>.12:
        return []
    outward=outward/max(np.linalg.norm(outward),1e-9)
    output=[]
    fractions=np.linspace(0,1,int(np.ceil(length/1.15))+1)
    for low,high in zip(fractions[:-1],fractions[1:]):
        wall=np.array([edge[0]*(1-low)+edge[1]*low,edge[0]*(1-high)+edge[1]*high])
        contacts=[pavement_index.nearest(p[:2],outward,max_reach) for p in wall]
        if any(c is None for c in contacts):
            continue
        # Matching both ends to the same physical street side prevents a
        # connecting quad from jumping across an intersection or a bend.
        if contacts[0]['section'].get('side')!=contacts[1]['section'].get('side'):
            continue
        start=wall[:,:2]+outward*.018
        finish=np.array([c['xy'] for c in contacts])
        polygon=Polygon([start[0],start[1],finish[1],finish[0]])
        if (not polygon.is_valid or polygon.area<.035 or road_reservation.intersects(polygon)
                or other_buildings.intersects(polygon)):
            continue
        depths=np.linalg.norm(finish-start,axis=1)
        if depths.min()<.045 or np.linalg.norm(finish[1]-finish[0])<.25:
            continue
        near_top=wall[:,2]+.008
        far_top=np.array([c['top'] for c in contacts])
        difference=near_top-far_top
        ground=np.asarray(ground_sampler(np.vstack([start,finish])),dtype=float)
        if ground.shape!=(4,) or not np.isfinite(ground).all():
            raise ValueError('Invalid ground sampling for planned house connections')
        if max(np.max(abs(near_top-ground[:2])),np.max(abs(far_top-ground[2:])))>.75:
            continue
        graded=np.max(abs(difference)/depths)<=.12 and np.max(abs(difference))<=.26
        steps=0
        if not graded:
            # Avoid invented sunken basements, long stairs, or a giant curb.
            if difference.min()<.015 or difference.max()>.34 or depths.min()<.8:
                continue
            steps=int(np.ceil(difference.max()/.17))
            if depths.min()<.35+steps*.25:
                continue
        cuts=[0.,1.] if not steps else [0.]+[(.35+k*.25)/float(depths.min()) for k in range(steps)]+[1.]
        style=contacts[0]['section'].get('style','neutral_concrete')
        for index,(a,b) in enumerate(zip(cuts[:-1],cuts[1:])):
            p0=start*(1-a)+finish*a;p1=start*(1-b)+finish*b
            xy=np.array([p0[0],p0[1],p1[1],p1[0]])
            if steps:
                level=near_top-difference*(index/steps)
                top=level[[0,1,1,0]]
            else:
                top=np.r_[near_top,far_top[::-1]]
            output.append({'xy':xy.tolist(),'top':top.tolist(),'faces':[[0,1,2],[0,2,3]],
                           'style':style,'source':'Inferred connection from official LoD2 wall bottom to accepted sidewalk back edge',
                           'inner_edge_indices':[0,1],'outer_edge_indices':[3,2],
                           'wall_edge':wall.tolist() if index==0 else None,
                           'step_index':index,'step_count':steps,
                           'side':contacts[0]['section'].get('side'),
                           'contact_kind':'shallow_step' if steps else 'graded_paving'})
    return output


def foundation_ground_surfaces(edge,outward,ground_sampler,reservation,other_buildings,width=.45):
    """A narrow inferred drainage/footing strip at a real lower wall edge.

    Correct only modest terrain conflicts: at most 45 cm of local excavation
    or 30 cm of fill. Upper-storey seams, basements on a high hillside and
    existing public paving never become invented terraces or large walls.
    """
    edge=np.asarray(edge,dtype=float);outward=np.asarray(outward,dtype=float)
    if (edge.shape!=(2,3) or outward.shape!=(2,) or not np.isfinite(edge).all()
            or not np.isfinite(outward).all() or not .2<=width<=.55):
        raise ValueError('Invalid foundation ground edge')
    length=float(np.linalg.norm(edge[1,:2]-edge[0,:2]))
    if length<.8 or abs(edge[1,2]-edge[0,2])/length>.12:
        return []
    outward=outward/max(np.linalg.norm(outward),1e-9)
    output=[]
    fractions=np.linspace(0,1,int(np.ceil(length/1.2))+1)
    for start,end in zip(fractions[:-1],fractions[1:]):
        wall=edge[0]+np.array([start,end])[:,None]*(edge[1]-edge[0])
        near=wall[:,:2]+outward*.018;far=wall[:,:2]+outward*width
        xy=np.array([near[0],near[1],far[1],far[0]])
        polygon=Polygon(xy)
        if (not polygon.is_valid or polygon.area<.08 or reservation.intersects(polygon)
                or other_buildings.intersects(polygon)):
            continue
        # 45 mm below the unchanged source wall base. The exported terrain
        # will be a further 35 mm below this shallow gravel surface.
        top=wall[:,2][[0,1,1,0]]-.045
        top[2:]-=.015*(width-.018)
        queries=np.vstack([xy,(xy[0]+xy[1])/2,(xy[2]+xy[3])/2])
        expected=np.r_[top,(top[0]+top[1])/2,(top[2]+top[3])/2]-.035
        ground=np.asarray(ground_sampler(queries),dtype=float)
        if ground.shape!=(6,) or not np.isfinite(ground).all():
            raise ValueError('Invalid sampled foundation ground')
        adjustment=expected-ground
        if adjustment.min()<-.45 or adjustment.max()>.30:
            continue
        output.append({'xy':xy.tolist(),'top':top.tolist(),'faces':[[0,1,2],[0,2,3]],
                       'style':'foundation_gravel','source':'Inferred 45-cm footing/drainage strip anchored to an official LoD2 lower wall edge',
                       'inner_edge_indices':[0,1],'outer_edge_indices':[3,2],
                       'wall_edge':wall.tolist(),'contact_kind':'foundation_drainage',
                       'maximum_cut_m':round(float(max(0,-adjustment.min())),5),
                       'maximum_fill_m':round(float(max(0,adjustment.max())),5)})
        grade_xy=xy.copy();grade_xy[:2]=wall[:,:2]-outward*.04
        grade_top=top.copy();grade_top[:2]+=.015*(.04+.018)
        # The shared terrain contour is quantized to 2 mm. Keep its hidden
        # support 4 mm beyond every visible edge, so an unsnapped gravel
        # corner cannot enter a steep adjacent transition triangle. Extend
        # the same drainage plane; neither the visible strip nor wall moves.
        margin=.004;tangent=(edge[1,:2]-edge[0,:2])/length
        longitudinal=(edge[1,2]-edge[0,2])/length
        grade_xy[[0,3]]-=tangent*margin;grade_xy[[1,2]]+=tangent*margin
        grade_xy[:2]-=outward*margin;grade_xy[2:]+=outward*margin
        grade_top[[0,3]]-=longitudinal*margin;grade_top[[1,2]]+=longitudinal*margin
        grade_top[:2]+=.015*margin;grade_top[2:]-=.015*margin
        output[-1].update(terrain_xy=grade_xy.tolist(),terrain_top=grade_top.tolist())
    return output


def plan_house_connections(data_dir,line,road_polygon,bounds,street_surfaces,
                           ground_sampler,other_roads=None):
    """Serializable house/sidewalk contact plan, used by terrain and rendering."""
    buildings=_ground_buildings(data_dir,line,road_polygon,bounds)
    index=PavementContactIndex(street_surfaces)
    reserve=road_polygon.buffer(.012)
    if other_roads is not None and not other_roads.is_empty:
        reserve=reserve.union(other_roads.buffer(.015))
    reservation=prep(reserve)
    building_tree=STRtree([b[0] for b in buildings])
    output=[];touched=0
    for building_index,(footprint,rings) in enumerate(buildings):
        neighbours=building_tree.query(footprint.buffer(3.))
        others=prep(unary_union([buildings[int(j)][0].buffer(.035) for j in neighbours if j!=building_index]))
        before=len(output)
        for edge,normal in _wall_frontage_edges(footprint,rings,line):
            cells=connection_surfaces(edge,normal,index,ground_sampler,reservation,others)
            for cell in cells:
                cell['building_index']=building_index
            output.extend(cells)
        touched+=len(output)>before
    # The narrow physical shoulders/drainage bands are already part of the
    # street even where OSM has no accepted sidewalk. Never put a footing
    # strip on them and then force its terrain down to the driving plane.
    occupied=unary_union([reserve,road_polygon.buffer(.502),
                          *[row[4] for row in _surface_data([*street_surfaces,*output])]])
    foundation_reservation=prep(occupied.buffer(.008))
    foundations=[];foundation_buildings=0
    for building_index,(footprint,rings) in enumerate(buildings):
        neighbours=building_tree.query(footprint.buffer(1.))
        others=prep(unary_union([buildings[int(j)][0].buffer(.035) for j in neighbours if j!=building_index]))
        before=len(foundations)
        for edge,normal in _wall_frontage_edges(footprint,rings,line):
            for cell in foundation_ground_surfaces(edge,normal,ground_sampler,foundation_reservation,others):
                cell['building_index']=building_index
                foundations.append(cell)
        foundation_buildings+=len(foundations)>before
    return {'contact_surfaces':output,'foundation_surfaces':foundations,
            'house_ground_apron_panels':len(output),
            'house_ground_apron_buildings':touched,
            'house_foundation_ground_panels':len(foundations),
            'house_foundation_ground_buildings':foundation_buildings,
            'house_foundation_ground_accuracy':'Inferred narrow gravel/footing strips at real LoD2 wall bases; at most 0.45 m wide, 0.45 m cut and 0.30 m fill; public paving, roads and neighbours excluded',
            'house_connection_step_surfaces':sum(s['step_count']>0 for s in output),
            'ground_transition_accuracy':'Locally inferred small-format paving and at most two shallow steps joining actual LoD2 wall-bottom edges to accepted sidewalk boundaries; not surveyed property access or courtyards; road and side streets reserved'}


def _emit_connected_ground(scene,material,add_mesh,ac,ground_sampler,plan):
    from roadtoassetto.scenery import Batches
    styles={'neutral_concrete':('de_small_format_concrete_pavers','de_paving.png'),
            'red_concrete':('de_local_red_concrete_pavers','de_paving_red.png'),
            'asphalt':('de_asphalt_sidewalk','de_sidewalk_asphalt.png')}
    materials={}
    for style,(name,texture) in styles.items():
        materials[style]=next((i for i,m in enumerate(scene['materials']) if m['name']==name),None)
        if materials[style] is None:
            materials[style]=material(name,texture,ambient=.3,diffuse=.47,specular=.025)
    foundations=plan.get('foundation_surfaces',[])
    if foundations:
        gravel=material('house_foundation_draining_gravel','shoulder_gravel.jpg',ambient=.3,diffuse=.47,specular=.015)
        scene['materials'][gravel].update(normal='shoulder_gravel_normal_dx.jpg',normal_strength=.4,roughness=.94)
        materials['foundation_gravel']=gravel
    edge=next((i for i,m in enumerate(scene['materials']) if m['name']=='de_chamfered_concrete_kerb'),None)
    if edge is None:
        edge=material('house_connection_embedded_edge','de_kerb.png',ambient=.3,diffuse=.46,specular=.02)
    mortar=material('house_connection_mortar_joint','de_drain_shadow.png',ambient=.3,diffuse=.4,specular=.01)
    batches=Batches(add_mesh)
    count=0;joints=0;retaining=0;foundation_count=0;foundation_joints=0
    for section,xy,top,faces,polygon,triangles in _surface_data([*plan['contact_surfaces'],*foundations]):
        is_foundation=section.get('contact_kind')=='foundation_drainage'
        top_name='HOUSE_FOUNDATION_GRAVEL' if is_foundation else 'HOUSE_CONTACT_PAVING'
        edge_name='HOUSE_FOUNDATION_EDGE' if is_foundation else 'HOUSE_CONTACT_EDGE'
        ground=np.asarray(ground_sampler(xy),dtype=float)
        if ground.shape!=top.shape or not np.isfinite(ground).all():
            raise ValueError('Invalid final terrain below planned contact surface')
        # Embed the retaining foot; surface tops never move after terrain was
        # generated because doors and sidewalks now share this same plan.
        bottom=np.minimum(ground-.075,top-.10)
        direction=xy[1]-xy[0];direction/=np.linalg.norm(direction)
        normal=np.array([-direction[1],direction[0]])
        local=xy-xy[0]
        uv=np.c_[local@direction,local@normal]
        high=ac(xy,top);low=ac(xy,bottom)
        top_faces=faces.copy()
        tri=high[top_faces]
        flip=np.cross(tri[:,1]-tri[:,0],tri[:,2]-tri[:,0])[:,1]<0
        top_faces[flip]=top_faces[flip,::-1]
        # Preserve the plan's triangle diagonal even when winding flips. The
        # terrain profile and this visible surface must interpolate identically.
        batches.append(top_name,materials.get(section.get('style'),materials['neutral_concrete']),high,top_faces,uv)
        batches.append(edge_name,edge,low,top_faces[:,::-1],uv)
        center=np.vstack([low,high]).mean(axis=0)
        for i in range(len(xy)):
            j=(i+1)%len(xy)
            side=np.array([low[i],low[j],high[j],high[i]])
            side_faces=np.array([[0,1,2],[0,2,3]])
            if np.dot(np.cross(side[1]-side[0],side[2]-side[0]),side.mean(axis=0)-center)<0:
                side_faces=side_faces[:,::-1]
            length=np.linalg.norm(xy[j]-xy[i])
            side_uv=np.array([[0,bottom[i]],[length,bottom[j]],[length,top[j]],[0,top[i]]])
            batches.append(edge_name,edge,side,side_faces,side_uv)
        retaining+=int(np.max(top-ground)>.14)
        wall=section.get('wall_edge')
        if wall is not None:
            wall=np.asarray(wall,dtype=float)
            # Deliberate narrow separation joint at the source wall, replacing
            # the accidental dark void behind the old disconnected apron.
            delta=xy[:2]-wall[:,:2]
            inner=wall[:,:2]+delta*.07;outer=xy[:2]-delta*.04
            p=np.array([inner[0],inner[1],outer[1],outer[0]])
            z=top[[0,1,1,0]]-.0015
            points=ac(p,z);joint_faces=np.array([[0,1,2],[0,2,3]])
            if np.cross(points[1]-points[0],points[2]-points[0])[1]<0:
                joint_faces=joint_faces[:,::-1]
            batches.append('HOUSE_FOUNDATION_MORTAR' if is_foundation else 'HOUSE_CONTACT_MORTAR',mortar,points,joint_faces,p)
            if is_foundation:foundation_joints+=1
            else:joints+=1
        if is_foundation:foundation_count+=1
        else:count+=1
    batches.finish()
    result={key:value for key,value in plan.items() if key not in ('contact_surfaces','foundation_surfaces')}
    result.update(house_ground_apron_panels=count,house_connection_mortar_joints=joints,
                  house_connection_retaining_sections=retaining,
                  house_foundation_ground_panels=foundation_count,
                  house_foundation_mortar_joints=foundation_joints,
                  house_connection_surface_style='Metric 20x10 cm pavers or dark asphalt matched to adjoining sidewalk; embedded edges and 18-mm wall separation joint')
    return result


def build_ground_transitions(data_dir, scene, material, add_mesh, ac, line,
                             road_polygon, ground_sampler, bounds, road_clearance_m=1.4,
                             *,street_surfaces=None,connection_plan=None):
    """Inferred near-house paved aprons with embedded masonry edges.

    Only horizontal/low-slope source bottom edges facing the street qualify.
    The road reservation includes its current variable width plus 1.4 m for
    shoulder and sidewalk; decorations can never enter the driving surface.
    """
    if connection_plan is not None:
        return _emit_connected_ground(scene,material,add_mesh,ac,ground_sampler,connection_plan)
    if street_surfaces is not None:
        plan=plan_house_connections(data_dir,line,road_polygon,bounds,street_surfaces,ground_sampler)
        return _emit_connected_ground(scene,material,add_mesh,ac,ground_sampler,plan)
    from roadtoassetto.scenery import prism
    from roadtoassetto.buildings import GML, WFS, BU
    transform = Transformer.from_crs(4326, 25832, always_xy=True)
    buildings = []
    for member in ET.parse(data_dir/'buildings_lod2.gml').getroot().findall(WFS+'member'):
        building = member.find(BU+'Building')
        if building is None:
            continue
        rings = []
        for pos in building.findall('.//'+GML+'exterior/'+GML+'LinearRing/'+GML+'posList'):
            if not pos.text:
                continue
            coords = np.fromstring(pos.text, sep=' ').reshape(-1, 3)
            x, y = transform.transform(coords[:, 1], coords[:, 0])
            rings.append(np.c_[x, y, coords[:, 2]])
        if not rings:
            continue
        footprint = MultiPoint(np.vstack(rings)[:, :2]).convex_hull
        if (isinstance(footprint, Polygon) and footprint.distance(line) < 22.
                and box(*bounds).buffer(-2).covers(footprint)
                and footprint.intersection(road_polygon).area < .02):
            buildings.append((footprint, rings))
    paving = material('house_paved_aprons', 'paving.png', ambient=.34, diffuse=.5, specular=.02)
    plinth = material('house_apron_masonry_edges', 'concrete.png', ambient=.31, diffuse=.47, specular=.015)
    for index in (paving, plinth):
        scene['materials'][index]['roughness'] = .91
    road_exclusion = prep(road_polygon.buffer(road_clearance_m))
    groups = {paving: [[], [], []], plinth: [[], [], []]}
    count = 0; touched = 0; seen = set()
    for i, (footprint, rings) in enumerate(buildings):
        others = prep(unary_union([f.buffer(.07) for j, (f, _) in enumerate(buildings) if i != j]))
        accepted = False
        for ring in rings:
            if np.ptp(ring[:, 2]) < 1.5:  # roof and ground polygons are not wall bases
                continue
            min_z = ring[:, 2].min()
            for first, last in zip(ring[:-1], ring[1:]):
                edge = np.array([first, last])
                if max(edge[:, 2]) > min_z+.4:
                    continue
                direction = last[:2]-first[:2]; length = np.linalg.norm(direction)
                if length < 1.2:
                    continue
                midpoint = edge[:, :2].mean(axis=0)
                if line.distance(Point(midpoint)) > 18:
                    continue
                normal = np.array([-direction[1], direction[0]])/length
                if footprint.contains(Point(midpoint+normal*.2)):
                    normal *= -1
                nearest = np.array(line.interpolate(line.project(Point(midpoint))).coords[0])
                if np.dot(normal, nearest-midpoint) < np.linalg.norm(nearest-midpoint)*.35:
                    continue
                key = tuple(sorted((tuple(np.round(first, 3)), tuple(np.round(last, 3)))))
                if key in seen:
                    continue
                seen.add(key)
                for p, bottom, top in apron_panels(edge, normal, ground_sampler, road_exclusion, others):
                    v, f = prism(p, bottom, top, ac)
                    # Top-only paving has metric tile UVs; vertical retaining
                    # edges use concrete, with a 9-cm embedded foot below DGM.
                    for mid, face_indices in ((paving, (2, 3)), (plinth, (0, 1, 4, 5, 6, 7, 8, 9, 10, 11))):
                        group = groups[mid]; base = len(group[0]); used_faces = np.array(f)[list(face_indices)]
                        group[0].extend(v.tolist()); group[1].extend((used_faces+base).tolist())
                        group[2].extend((v[:, [0, 2]]/2.).tolist())
                    count += 1; accepted = True
        touched += accepted
    for mid, (v, f, uv) in groups.items():
        if not f:
            continue
        vertices = np.asarray(v); faces = np.asarray(f); uv = np.asarray(uv)
        for start in range(0, len(faces), 12000):
            used, remap = np.unique(faces[start:start+12000], return_inverse=True)
            add_mesh(f'HOUSE_APRONS_M{mid}_P{start//12000}', vertices[used], remap.reshape(-1, 3), mid, uv[used], False)
    return {'house_ground_apron_panels': count, 'house_ground_apron_buildings': touched,
            'ground_transition_accuracy': 'Short paved wall-foot aprons and embedded retaining edges are inferred visual detail, not surveyed courtyards; road and neighboring houses excluded'}
