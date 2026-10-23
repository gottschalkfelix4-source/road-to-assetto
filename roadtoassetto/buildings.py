"""Hessian INSPIRE LoD2 surfaces with aerial roofs and per-face photo overrides.

The WFS supplies latitude, longitude, height in EPSG:7423. Its numberReturned
attribute is zero even when members are present: count actual members instead.
No Google assets are fetched by this module.
"""
from __future__ import annotations
import hashlib
import json
import shutil
from pathlib import Path
from xml.etree import ElementTree as ET
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
from pyproj import Transformer
from shapely.geometry import Polygon, MultiPoint, Point
from shapely import STRtree, constrained_delaunay_triangles

GML='{http://www.opengis.net/gml/3.2}'
WFS='{http://www.opengis.net/wfs/2.0}'
BU='{http://inspire.ec.europa.eu/schemas/bu-core3d/4.0}'


def triangulate_face(rings):
    """Triangulate a planar 3-D polygon with holes, preserving its orientation."""
    outer=np.asarray(rings[0],dtype=float)
    if np.linalg.norm(outer[0]-outer[-1])<1e-5:
        outer=outer[:-1]
    if len(outer)<3:
        raise ValueError('Short polygon')
    relative=outer-outer[0]
    normal=np.sum(np.cross(relative,np.roll(relative,-1,axis=0)),axis=0)
    length=np.linalg.norm(normal)
    if length<1e-7:
        raise ValueError('Degenerate polygon')
    normal/=length
    vectors=outer-outer[0]
    axis=vectors[np.argmax(np.linalg.norm(vectors,axis=1))]
    axis-=normal*np.dot(axis,normal)
    axis/=np.linalg.norm(axis)
    up=np.cross(normal,axis)
    p0=outer[0]
    def project(points):
        delta=np.asarray(points)-p0
        return np.c_[delta@axis,delta@up]
    projected=project(outer)
    holes=[project(r) for r in rings[1:]]
    polygon=Polygon(projected,holes)
    if not polygon.is_valid or polygon.area<1e-5:
        raise ValueError('Invalid projected polygon')
    vertices=[];faces=[];area=0.
    for triangle in constrained_delaunay_triangles(polygon).geoms:
        if not polygon.covers(triangle):
            continue
        q=np.asarray(triangle.exterior.coords)[:3]
        p=p0+q[:,0,None]*axis+q[:,1,None]*up
        if np.linalg.norm(np.cross(p[1]-p[0],p[2]-p[0]))<1e-5:
            continue
        i=len(vertices)
        if np.dot(np.cross(p[1]-p[0],p[2]-p[0]),normal)<0:
            p=p[[0,2,1]]
        vertices.extend(p.tolist());faces.append((i,i+1,i+2));area+=triangle.area
    if not faces or abs(area-polygon.area)>max(.002,polygon.area*.001):
        raise ValueError('Incomplete polygon triangulation')
    return np.asarray(vertices),faces,normal


def facade_uv(vertices, normal, base_z, floor_height, x_origin=None):
    """Metric texture frame anchored to the building, including sloped gables.

    A wall's highest point must never establish the floor origin: the apex of a
    gable would otherwise move every window on that side of the same house.
    """
    u=np.array([-normal[1],normal[0],0.],dtype=float)
    u/=np.linalg.norm(u)
    x=np.asarray(vertices)@u
    if x_origin is None:
        x_origin=float(x.min())
    return np.c_[(x-x_origin)/6.,1-(np.asarray(vertices)[:,2]-base_z)/floor_height]


def clip_height_band(vertices, faces, lower=-np.inf, upper=np.inf):
    """Clip triangles against a height interval without filling polygon holes."""
    output=[];triangles=[]
    for face in faces:
        polygon=[p.copy() for p in vertices[np.asarray(face)]]
        for height,sign in ((lower,1),(upper,-1)):
            if not np.isfinite(height) or not polygon:
                continue
            clipped=[]
            for a,b in zip(polygon,polygon[1:]+polygon[:1]):
                a_inside=sign*(a[2]-height)>=0
                b_inside=sign*(b[2]-height)>=0
                if a_inside:
                    clipped.append(a)
                if a_inside!=b_inside:
                    point=a+(b-a)*((height-a[2])/(b[2]-a[2]))
                    point[2]=height
                    clipped.append(point)
            polygon=clipped
        if len(polygon)<3:
            continue
        for i in range(1,len(polygon)-1):
            tri=np.asarray([polygon[0],polygon[i],polygon[i+1]])
            if np.linalg.norm(np.cross(tri[1]-tri[0],tri[2]-tri[0]))<1e-5:
                continue
            offset=len(output)
            output.extend(tri.tolist());triangles.append((offset,offset+1,offset+2))
    return np.asarray(output,dtype=float).reshape(-1,3),triangles


def exportable_mesh(points, faces, uv):
    """Remove faces that collapse in prepare's rounding and KN5's float32.

    Height clipping sometimes creates a sliver near an existing source corner.
    Its area in full-precision UTM space can be nonzero while two game-space
    vertices serialize to the same coordinate. Check that actual precision and
    compact vertices so a dropped triangle cannot leave zero-length normals.
    """
    points=np.round(np.asarray(points,dtype=float),5)
    faces=np.asarray(faces,dtype=np.int64).reshape(-1,3)
    if not len(faces):
        return points[:0],[],np.asarray(uv)[:0]
    stored=points.astype(np.float32)
    t=stored[faces]
    area2=np.linalg.norm(np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0]),axis=1)
    faces=faces[np.isfinite(area2)&(area2>=1e-7)]
    if not len(faces):
        return points[:0],[],np.asarray(uv)[:0]
    referenced=np.unique(faces.ravel())
    return points[referenced],np.searchsorted(referenced,faces).tolist(),np.asarray(uv)[referenced]


def floor_layout(base_z, roof_faces, wall_faces):
    """Estimate consistent storeys from low roof edges; this is visual detail."""
    eaves=[float(v[:,2].min()) for v,_,_ in roof_faces if v[:,2].min()>base_z+1.8]
    if eaves:
        # Lower roofs on garages must not pull an entire house's upper storeys
        # down. A robust central eave value also ignores isolated dormers.
        eave=float(np.median(eaves))
    else:
        eave=float(np.median([v[:,2].max() for v,_,_ in wall_faces]))
    height=max(eave-base_z,1.)
    floors=max(1,int(np.floor(height/2.85+.35)))
    floor_height=float(np.clip(height/floors,2.45,3.45))
    return floor_height,base_z+floors*floor_height


def eave_prism(a,b,width=.075,height=.12):
    """Small rectangular fascia on a source roof edge, with hard face normals."""
    a=np.asarray(a,dtype=float);b=np.asarray(b,dtype=float)
    along=b-a;along/=np.linalg.norm(along)
    side=np.cross(along,[0.,0.,1.]);side/=np.linalg.norm(side)
    down=np.array([0.,0.,-height])
    corners=np.asarray([a-side*width/2,b-side*width/2,b+side*width/2,a+side*width/2])
    corners=np.vstack([corners,corners+down])
    output=[];faces=[];uv=[]
    for indices in ((0,1,2,3),(7,6,5,4),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)):
        q=corners[list(indices)]
        # Geometric outward orientation does not depend on the source edge order.
        normal=np.cross(q[1]-q[0],q[2]-q[0])
        if np.dot(normal,q.mean(axis=0)-corners.mean(axis=0))<0:
            q=q[::-1]
        i=len(output);output.extend(q.tolist())
        faces.extend([(i,i+1,i+2),(i,i+2,i+3)])
        uv.extend([(0,0),(1,0),(1,1),(0,1)])
    return np.asarray(output),faces,np.asarray(uv)


def facade_box(origin, horizontal, normal, bounds):
    """A closed trim box in a wall frame: x, outward depth, absolute height."""
    x0,x1,d0,d1,z0,z1=bounds
    q=np.asarray([(x0,d0,z0),(x1,d0,z0),(x1,d1,z0),(x0,d1,z0),
                  (x0,d0,z1),(x1,d0,z1),(x1,d1,z1),(x0,d1,z1)])
    corners=np.asarray(origin)+q[:,0,None]*horizontal+q[:,1,None]*normal
    corners[:,2]=q[:,2]
    output=[];faces=[];uv=[]
    for indices in ((0,1,2,3),(7,6,5,4),(0,4,5,1),(1,5,6,2),(2,6,7,3),(3,7,4,0)):
        points=corners[list(indices)]
        if np.dot(np.cross(points[1]-points[0],points[2]-points[0]),points.mean(axis=0)-corners.mean(axis=0))<0:
            points=points[::-1]
        i=len(output);output.extend(points);faces.extend([(i,i+1,i+2),(i,i+2,i+3)])
        uv.extend([(0,1),(1,1),(1,0),(0,0)])
    return np.asarray(output),faces,np.asarray(uv)


def window_layout(rings, normal, base_z, floor_height, window_cap, variant=0):
    """Estimated bays fully contained in the source wall, never across holes."""
    u=np.array([-normal[1],normal[0],0.]);u/=np.linalg.norm(u)
    projected=[np.c_[np.asarray(r)@u,np.asarray(r)[:,2]] for r in rings]
    shape=Polygon(projected[0],projected[1:])
    xmin,zmin,xmax,zmax=shape.bounds
    if not shape.is_valid or xmax-xmin<2.1 or zmax-base_z<2.7:
        return u,[]
    # An elevation keeps aligned bays, but houses have different proportions.
    # Small end bays and unequal first/last setbacks avoid a stamped grid.
    style=variant%8
    width=[1.12,1.34,1.02,1.2,1.42,1.08,1.24,1.16][style]
    step=[2.85,3.3,2.7,3.15,3.55,3.,3.2,2.9][style]
    count=max(1,int((xmax-xmin-.5)/step))
    centers=np.linspace(xmin+(xmax-xmin)/(count*2),xmax-(xmax-xmin)/(count*2),count)
    if count>1:
        centers+=np.array([-.13, .09, -.04, .14, -.11, .06, .03, -.07][style])*np.linspace(-1,1,count)
    result=[]
    for level in range(min(6,max(1,int(np.ceil((window_cap-base_z)/floor_height))))):
        sill=[.29,.30,.31,.28,.34,.30,.32,.29][style]
        height=[.46,.43,.49,.48,.40,.47,.44,.46][style]
        bottom=base_z+level*floor_height+floor_height*sill
        top=bottom+floor_height*height
        if top>window_cap-.08:
            continue
        for bay,center in enumerate(centers):
            bay_width=width*(.88 if count>2 and style in (1,4,6) and bay in (0,count-1) else 1.)
            rect=Polygon([(center-bay_width/2-.11,bottom-.1),(center+bay_width/2+.11,bottom-.1),
                          (center+bay_width/2+.11,top+.1),(center-bay_width/2-.11,top+.1)])
            if shape.covers(rect):
                result.append((float(center),float(bottom),float(top),bay_width,level))
    return u,result


def entrance_layout(rings, normal, base_z, variant=0, terrain_height_sampler=None):
    """One plausible contained doorway, with a threshold tied to nearby ground.

    This is inferred architectural dressing; no original wall is cut or moved.
    A large height discrepancy is deliberately not bridged with invented stairs.
    """
    u=np.array([-normal[1],normal[0],0.]);u/=np.linalg.norm(u)
    projected=[np.c_[np.asarray(r)@u,np.asarray(r)[:,2]] for r in rings]
    shape=Polygon(projected[0],projected[1:])
    if not shape.is_valid:
        return None
    xmin,_,xmax,_=shape.bounds
    width=[1.02,1.08,.96,1.12,1.04,1.,1.14,1.06][variant%8]
    height=[2.12,2.18,2.09,2.2,2.16,2.1,2.22,2.14][variant%8]
    if xmax-xmin<width+1.05:
        return None
    origin=np.asarray(rings[0][0]).copy();origin-=u*np.dot(origin,u)
    choices=(.28,.72,.5) if variant%2 else (.72,.28,.5)
    for fraction in choices:
        center=xmin+(xmax-xmin)*fraction
        ground=base_z
        if terrain_height_sampler is not None:
            q=np.asarray([origin+u*(center-width/2)+normal*.32,
                          origin+u*(center+width/2)+normal*.32])
            heights=np.asarray(terrain_height_sampler(q[:,:2]),dtype=float)
            if heights.shape!=(2,) or not np.isfinite(heights).all():
                raise ValueError('Invalid terrain heights for doorway')
            # A threshold cannot safely join a steep or uncertain local datum.
            if abs(float(heights[1]-heights[0]))>.3:
                continue
            ground=float(heights.mean())
            if abs(ground-base_z)>.55:
                continue
        bottom=max(base_z+.025,ground+.035)
        top=bottom+height
        rect=Polygon([(center-width/2-.12,bottom),(center+width/2+.12,bottom),
                      (center+width/2+.12,top+.12),(center-width/2-.12,top+.12)])
        if shape.covers(rect):
            return {'origin':origin,'u':u,'normal':normal,'center':float(center),
                    'bottom':float(bottom),'top':float(top),'width':width,'ground':ground}
    return None


def atlas_uv(uv, rectangle):
    u0,v0,u1,v1=rectangle
    return np.asarray(uv,dtype=float)*np.array([u1-u0,v1-v0])+np.array([u0,v0])


def architectural_palette(identifier):
    """Stable, independent generic architecture and paint choices per house.

    These colours describe an inferred appearance, never a surveyed paint
    colour. Decoupling them prevents every shutter or window type from having
    the same plaster, and keeps all walls of one building coherent.
    """
    seed=int(hashlib.sha256(identifier.encode()).hexdigest()[:16],16)
    return {'style':int(seed>>32)%8,'paint':int(seed>>8)%8,
            'plinth_height':(.30,.42,.36,.54,.46,.34,.58,.40)[seed%8],
            'uv_offset':((seed>>16)%17/17.,(seed>>24)%13/13.)}


def plaster_uv(vertices,normal,base_z,x_origin,offset=(0.,0.)):
    """A 3 x 1.5 metre plaster sample; unrelated to the estimated storeys."""
    u=np.array([-normal[1],normal[0],0.]);u/=np.linalg.norm(u)
    return np.c_[(np.asarray(vertices)@u-x_origin)/3.+offset[0],
                 1-(np.asarray(vertices)[:,2]-base_z)/1.5+offset[1]]


def pipe_mesh(points,radius=.046,sides=10):
    """Closed, smooth-normal-ready metric pipe along a short 3-D polyline."""
    path=np.asarray(points,dtype=float)
    if len(path)<2 or not np.isfinite(path).all() or radius<=0:
        raise ValueError('Invalid pipe path')
    segments=np.diff(path,axis=0)
    lengths=np.linalg.norm(segments,axis=1)
    if np.any(lengths<.005):
        raise ValueError('Pipe segments must be at least five millimetres')
    tangents=np.vstack([segments[0],path[2:]-path[:-2],segments[-1]])
    tangents/=np.linalg.norm(tangents,axis=1)[:,None]
    reference=np.eye(3)[np.argmin(np.abs(tangents[0]))]
    normals=np.cross(tangents,reference)
    normals/=np.linalg.norm(normals,axis=1)[:,None]
    binormals=np.cross(tangents,normals)
    angle=np.arange(sides)*2*np.pi/sides
    rings=path[:,None,:]+radius*(np.cos(angle)[None,:,None]*normals[:,None,:]+
                                np.sin(angle)[None,:,None]*binormals[:,None,:])
    vertices=rings.reshape(-1,3).tolist();faces=[]
    distances=np.r_[0,np.cumsum(lengths)]
    uv=np.c_[np.tile(np.arange(sides)/sides,len(path)),np.repeat(distances,len(angle))].tolist()
    for row in range(len(path)-1):
        for col in range(sides):
            a=row*sides+col;b=row*sides+(col+1)%sides;c=b+sides;d=a+sides
            faces.extend([(a,b,c),(a,c,d)])
    # Caps have separate vertices so their normals remain hard against the tube.
    for row,reverse in ((0,True),(len(path)-1,False)):
        offset=len(vertices);vertices.extend(rings[row].tolist())
        uv.extend(np.c_[(np.cos(angle)+1)/2,(np.sin(angle)+1)/2].tolist())
        for col in range(1,sides-1):
            triangle=(offset,offset+col,offset+col+1)
            faces.append(triangle[::-1] if reverse else triangle)
    return np.asarray(vertices),faces,np.asarray(uv)


def downpipe_layout(rings,normal,base_z,eave_z,variant=0,terrain_height_sampler=None):
    """A contained generic rainwater pipe, excluding wall holes and bad datums."""
    if abs(normal[2])>.02 or eave_z-base_z<2.8:
        return None
    u=np.array([-normal[1],normal[0],0.]);u/=np.linalg.norm(u)
    shape=Polygon(np.c_[np.asarray(rings[0])@u,np.asarray(rings[0])[:,2]],
                  [np.c_[np.asarray(r)@u,np.asarray(r)[:,2]] for r in rings[1:]])
    if not shape.is_valid or shape.bounds[2]-shape.bounds[0]<3.5:
        return None
    origin=np.asarray(rings[0][0]).copy();origin-=u*np.dot(origin,u)
    xmin,_,xmax,zmax=shape.bounds
    top=min(eave_z-.16,zmax-.18)
    for center in ((xmin+.22,xmax-.22) if variant%2 else (xmax-.22,xmin+.22)):
        ground=base_z
        if terrain_height_sampler is not None:
            ground=float(np.asarray(terrain_height_sampler(np.asarray([(origin+u*center+normal*.12)[:2]])))[0])
            if not np.isfinite(ground) or abs(ground-base_z)>.6:
                continue
        bottom=max(base_z+.10,ground+.12)
        envelope=Polygon([(center-.07,bottom),(center+.07,bottom),
                          (center+.07,top),(center-.07,top)])
        if top-bottom<2.3 or not shape.covers(envelope):
            continue
        # A wall-fixed shaft and a short upper offset into the inferred gutter.
        path=np.asarray([origin+u*center+normal*.105,origin+u*center+normal*.105,
                         origin+u*center+normal*.18])
        path[:,2]=[bottom,top-.20,top]
        return {'path':path,'origin':origin,'u':u,'normal':normal,'center':float(center),
                'bottom':bottom,'top':top}
    return None


def wall_eave_height(vertices,normal,roof_faces):
    """Find a horizontal lower roof edge supported by this actual wall plane.

    A gable apex, a neighbouring roof or a median storey estimate must not
    create a pipe which ends halfway up a blank wall.
    """
    origin=np.asarray(vertices)[0];u=np.array([-normal[1],normal[0],0.])
    u/=np.linalg.norm(u);wall_x=np.asarray(vertices)@u
    heights=[]
    for points,_,n in roof_faces:
        if not .25<n[2]<.98:
            continue
        low=float(points[:,2].min())
        edge=np.unique(np.round(points[np.abs(points[:,2]-low)<.025],5),axis=0)
        if len(edge)<2 or np.max(np.abs((edge-origin)@normal))>.18:
            continue
        x=edge@u
        if min(float(x.max()),float(wall_x.max()))-max(float(x.min()),float(wall_x.min()))<1.5:
            continue
        heights.append(low)
    return min(heights) if heights else None


def doorway_components(layout, variant=0, textured_leaf=False):
    """Metric closed solids around a door; callers add the albedo leaf plane.

    The returned material roles intentionally separate stone, timber and metal.
    Depth is measured out from the retained source wall, never into the street.
    """
    center=layout['center'];bottom=layout['bottom'];top=layout['top'];width=layout['width']
    left,right=center-width/2,center+width/2
    result=[('reveal',(left-.085,left+.012,.015,.105,bottom,top+.085)),
            ('reveal',(right-.012,right+.085,.015,.105,bottom,top+.085)),
            ('reveal',(left,right,.015,.105,top-.012,top+.085)),
            ('frame',(left-.04,left+.025,.048,.115,bottom,top+.04)),
            ('frame',(right-.025,right+.04,.048,.115,bottom,top+.04)),
            ('frame',(left,right,.048,.115,top-.025,top+.04)),
            ('sill',(left-.1,right+.1,.008,.30,bottom-.035,bottom+.005))]
    # A short lever with two posts has recognizable parallax in driver view.
    handle_x=left+width*.89
    handle_z=bottom+(top-bottom)*.49
    result.extend([('metal',(handle_x-.016,handle_x+.016,.045,.078,handle_z-.07,handle_z+.055)),
                   ('metal',(handle_x-.075,handle_x+.015,.072,.092,handle_z+.015,handle_z+.035))])
    rise=bottom-layout['ground']
    if .075<rise<=.58:
        steps=max(1,int(np.ceil(rise/.18)))
        step_height=rise/steps
        for step in range(steps):
            z=bottom-step_height*(step+1)
            result.append(('sill',(left-.15,right+.15,.025,.30+.26*(step+1),
                                    layout['ground']-.035,z)))
    if not textured_leaf:
        # Raised panels remain useful without an image provider or atlas.
        for low,high in ((bottom+.18,bottom+.86),(bottom+1.11,top-.16)):
            for a,b in ((left+.12,center-.035),(center+.035,right-.12)):
                result.append(('frame',(a,b,.040,.052,low,low+.018)))
                result.append(('frame',(a,b,.040,.052,high-.018,high)))
                result.append(('frame',(a,a+.018,.040,.052,low,high)))
                result.append(('frame',(b-.018,b,.040,.052,low,high)))
    return result


def door_textures(textures,material,scene,atlas=None):
    """Use an unchanged eight-cell generated atlas, or a material-only fallback."""
    result=[]
    if atlas is not None and atlas.is_file():
        shutil.copy2(atlas,textures/'door_atlas.png')
        for variant in range(8):
            mid=material('entrance_leaf_'+str(variant),'door_atlas.png',ambient=.34,diffuse=.52,specular=.05)
            col,row=variant%4,variant//4
            scene['materials'][mid].update(roughness=.65,specular_exp=28.,
                uv_rect=[col/4+.004,row/2+.006,(col+1)/4-.004,(row+1)/2-.006])
            result.append(mid)
        return result
    for variant,color in enumerate(((81,65,49),(60,73,67),(107,85,59),(83,92,83),
                                    (98,71,56),(61,65,64),(128,113,86),(80,89,98))):
        # The fallback has neutral fine timber grain; all panel relief, frame,
        # hardware and threshold remain actual geometry rather than a picture.
        rng=np.random.default_rng(170+variant)
        grain=rng.normal(0,1.15,(512,256,1))+rng.normal(0,.7,(1,256,1))
        pixels=np.clip(np.array(color)[None,None,:]+grain,0,255).astype('uint8')
        filename=f'entrance_leaf_{variant}.jpg'
        Image.fromarray(pixels).save(textures/filename,quality=96)
        mid=material('entrance_leaf_'+str(variant),filename,ambient=.34,diffuse=.52,specular=.05)
        scene['materials'][mid].update(roughness=.72,specular_exp=24.)
        result.append(mid)
    return result


def foundation_strips(rings, normal, terrain_height_sampler, max_gap=2., wall_neighbors=()):
    """Close bounded gaps below existing lower exterior wall edges only.

    The source surface is untouched. Holes, upper edges, buried walls and
    larger datum discrepancies never create a new vertical retaining wall.
    """
    if abs(normal[2])>.02:
        return []
    u=np.array([-normal[1],normal[0],0.]);u/=np.linalg.norm(u)
    projected=[np.c_[np.asarray(r)@u,np.asarray(r)[:,2]] for r in rings]
    shape=Polygon(projected[0],projected[1:])
    if not shape.is_valid:
        return []
    result=[]
    outer=np.asarray(rings[0],dtype=float)
    for a,b in zip(outer[:-1],outer[1:]):
        length=float(np.linalg.norm(b[:2]-a[:2]))
        if length<.25:
            continue
        midpoint=(a+b)/2
        # A lower boundary has wall immediately above and open space below.
        x=float(midpoint@u)
        if not shape.covers(Point(x,midpoint[2]+.04)) or shape.covers(Point(x,midpoint[2]-.04)):
            continue
        steps=max(1,int(np.ceil(length)))
        edge=a+np.linspace(0,1,steps+1)[:,None]*(b-a)
        ground=np.asarray(terrain_height_sampler(edge[:,:2]),dtype=float)
        if ground.shape!=(len(edge),) or not np.isfinite(ground).all():
            raise ValueError('Invalid terrain heights for building foundation')
        gap=edge[:,2]-ground
        for j in range(steps):
            # Never bridge a sample that signals an untrusted datum. A normal
            # ground crossing is different: retain its positive subinterval
            # instead of discarding an entire metre when one end is buried.
            g0,g1=gap[j:j+2]
            if max(g0,g1)<=1e-6 or max(g0,g1)>max_gap:
                continue
            t0,t1=0.,1.
            if g0<=0:
                t0=float(-g0/(g1-g0))
            if g1<=0:
                t1=float(-g0/(g1-g0))
            if t1-t0<1e-6:
                continue
            fractions=np.array([t0,t1])
            clipped_edge=edge[j]+fractions[:,None]*(edge[j+1]-edge[j])
            clipped_ground=ground[j]+fractions*(ground[j+1]-ground[j])
            below=clipped_edge.mean(axis=0);below[2]-=.04
            if any(abs(float(normal@n))>.999 and abs(float((below-origin)@n))<.025
                   and polygon.covers(Point(float(below@horizontal),float(below[2])))
                   for origin,n,horizontal,polygon in wall_neighbors):
                # This is a seam between source wall surfaces, not an exposed
                # building underside. Never cover the lower original surface.
                continue
            top=clipped_edge.copy();top[:,2]+=.012
            low=clipped_edge.copy();low[:,2]=clipped_ground-.1
            points=np.asarray([top[0],low[0],low[1],top[1]])+normal*.003
            faces=[(0,1,2),(0,2,3)]
            if np.dot(np.cross(points[1]-points[0],points[2]-points[0]),normal)<0:
                faces=[(0,2,1),(0,3,2)]
            coordinates=np.c_[(points@u-float(a@u))/2,1-(points[:,2]-float(a[2]))/2]
            result.append((points,faces,coordinates,float(max(g0,g1))))
    return result


def glass_textures(textures, material, scene, atlas=None):
    """Opaque window layers with restrained reflections and varied curtains.

    These are original illustrations, not photos or a reconstruction of rooms.
    Geometry puts the glass behind projecting frames and stone sills.
    """
    result=[]
    if atlas is not None and atlas.is_file():
        shutil.copy2(atlas,textures/'window_atlas.png')
        for variant in range(8):
            mid=material('window_glass_'+str(variant),'window_atlas.png',ambient=.32,diffuse=.38,specular=.2)
            # Sample equal atlas cells with a small inset to avoid grid lines.
            col,row=variant%4,variant//4
            scene['materials'][mid].update(roughness=.23,specular_exp=48.,
                uv_rect=[col/4+.004,row/2+.006,(col+1)/4-.004,(row+1)/2-.006])
            result.append(mid)
        return result
    y,x=np.mgrid[0:512,0:256];xf=x/255.;yf=y/511.
    rng=np.random.default_rng(7234)
    for variant in range(8):
        base=np.array([(49,62,65),(64,72,74),(53,66,66),(67,73,71),
                       (57,66,68),(66,76,79),(53,62,65),(66,72,69)][variant])
        reflection=(1-yf)*18+np.sin(xf*5+yf*2+variant)*3
        pixels=base+reflection[:,:,None]+rng.normal(0,.65,(512,256,1))
        # Fine vertical folds and an uneven center opening read as curtains
        # behind glass, while alternate panes remain dark and unoccupied.
        if variant%4!=0:
            gap=.12+.025*variant
            mask=(xf<.5-gap)|(xf>.5+gap)
            curtain=np.array([(128,124,110),(113,117,112),(140,134,120)][variant%3])
            folds=(np.cos(xf*113+variant)*9+np.cos(xf*47)*4)*(1-.2*yf)
            layer=curtain+folds[:,:,None]+(1-yf[:,:,None])*7
            pixels=np.where(mask[:,:,None],pixels*.34+layer*.66,pixels)
        if variant in (3,6):
            # A partly lowered roller blind, including narrow horizontal slats.
            mask=yf<(.28 if variant==3 else .48)
            slats=133+np.sin(y*1.9)*5
            blind=np.stack([slats,slats*.99,slats*.94],axis=-1)
            pixels=np.where(mask[:,:,None],blind,pixels)
        # Frame contact shadows are baked only at the four pane edges.
        edge=np.minimum.reduce([x,255-x,y*.5,(511-y)*.5])
        pixels*=np.clip(.57+edge/22,.57,1)[:,:,None]
        filename=f'window_glass_{variant}.jpg'
        Image.fromarray(np.clip(pixels,0,255).astype('uint8')).save(textures/filename,quality=96)
        mid=material('window_glass_'+str(variant),filename,ambient=.32,diffuse=.38,specular=.34)
        scene['materials'][mid].update(roughness=.19,specular_exp=64.)
        result.append(mid)
    return result


def roof_materials(data_dir,textures,scene,material):
    """Optional metric CC0 tile scans; distant and flat roofs retain imagery."""
    result={}
    for asset,scale in (('roof_tiles',2.),('grey_roof_01',8.)):
        folder=data_dir/'materials/PolyHavenRoofs'
        color=folder/(asset+'_diff_2k.jpg');normal=folder/(asset+'_nor_dx_2k.jpg')
        if not color.is_file() or not normal.is_file():
            continue
        target=asset+'_scan.jpg';normal_target=asset+'_normal_dx.jpg'
        Image.open(color).convert('RGB').save(textures/target,quality=96)
        Image.open(normal).convert('RGB').save(textures/normal_target,quality=97)
        mid=material('photographic_'+asset,target,ambient=.26,diffuse=.48,specular=.045)
        scene['materials'][mid].update(normal=normal_target,normal_strength=.65,roughness=.72)
        result['red' if asset=='roof_tiles' else 'grey']=(mid,scale)
    return result


def facade_materials(data_dir, textures, material, scene):
    """Muted original village facades; scanned plaster supplies microdetail only."""
    original=data_dir/'materials/PaintedPlaster006/PaintedPlaster006_2K-JPG_Color.jpg'
    if not original.exists():
        raise ValueError('Run scripts/fetch_visual_data.py before building')
    base=Image.open(original).convert('RGB').resize((1024,512))
    # Distinct but muted mineral-paint families. These are generic render
    # choices, deliberately independent of the inferred architectural style.
    colors=[(202,184,154),(226,220,204),(190,170,146),(173,187,176),
            (207,178,164),(182,187,182),(219,205,174),(204,211,201)]
    result={'upper':[],'ground':[],'plain':[],'door':[],'close_ground':[],'foundation':[]}
    for variant,color in enumerate(colors):
        a=np.asarray(base).astype(float)
        gray=np.mean(a,axis=2)
        # Keep photographic fine grain but suppress broad damaged-paint stains.
        # Repeating the source's low-frequency patches on every nearby house
        # looks like water damage rather than ordinary painted village plaster.
        low=np.asarray(Image.fromarray(gray.astype('uint8')).filter(ImageFilter.GaussianBlur(18)),dtype=float)
        mean=max(float(gray.mean()),1)
        variation=1+.12*(gray-low)/mean+.025*(low/mean-1)
        pixels=np.clip(variation[:,:,None]*np.array(color)[None,None,:],0,255).astype('uint8')
        plain=Image.fromarray(pixels)
        image=plain.copy();draw=ImageDraw.Draw(image)
        for x in (256,768):
            # Two window bays per six metres. Soft frame shadows and modest
            # glass reflections avoid the former solid blue billboard windows.
            draw.rectangle((x-91,117,x+94,369),fill=tuple(int(c*.72) for c in color))
            draw.rectangle((x-88,111,x+88,362),fill=(221,219,209))
            draw.rectangle((x-77,123,x+77,352),fill=(66+variant*2,77+variant*2,79+variant))
            draw.polygon([(x-73,126),(x+73,126),(x+73,209),(x-73,276)],fill=(105,117,118))
            draw.rectangle((x-72,277,x-43,348),fill=(158,157,145))
            draw.line((x,123,x,352),fill=(209,208,197),width=6)
            draw.line((x-76,231,x+76,231),fill=(209,208,197),width=5)
            draw.rectangle((x-96,361,x+97,369),fill=(176,174,162))
            if variant in (0,2,3):
                shutter=(75,89,74) if variant!=2 else (102,83,66)
                for left,right in ((x-146,x-98),(x+98,x+146)):
                    draw.rectangle((left,113,right,360),fill=shutter)
                    for y in range(121,356,12):
                        draw.line((left+3,y,right-3,y),fill=tuple(c+11 for c in shutter),width=2)
        ground=image.copy();draw=ImageDraw.Draw(ground)
        draw.rectangle((0,479,1024,512),fill=tuple(int(c*.78) for c in color))
        close_ground=plain.copy();draw=ImageDraw.Draw(close_ground)
        draw.rectangle((0,473,1024,512),fill=tuple(int(c*.73) for c in color))
        draw.line((0,473,1024,473),fill=tuple(int(c*.6) for c in color),width=2)
        foundation=Image.fromarray(np.clip(pixels.astype(float)*.72,0,255).astype('uint8'))
        door=plain.resize((256,512));draw=ImageDraw.Draw(door)
        draw.rectangle((37,91,219,511),fill=tuple(int(c*.66) for c in color))
        draw.rectangle((42,91,214,511),fill=(189,185,171))
        timber=[(90,76,59),(73,82,76),(114,98,74),(87,103,93),(104,85,76),(76,83,82),(123,111,88),(81,91,90)][variant]
        draw.rectangle((50,101,206,506),fill=timber)
        draw.rectangle((67,121,188,264),fill=(88,103,105))
        for x in (67,128):
            draw.rectangle((x,289,x+48,466),outline=tuple(min(c+18,255) for c in timber),width=3)
        draw.rectangle((183,292,190,322),fill=(171,170,157))
        for kind,picture in (('upper',image),('ground',ground),('plain',plain),('door',door),('close_ground',close_ground),('foundation',foundation)):
            filename=f'village_{kind}_{variant}.jpg'
            picture.save(textures/filename,quality=94)
            mid=material(f'village_{kind}_{variant}',filename,ambient=.29,diffuse=.43,specular=.015)
            scene['materials'][mid].update(roughness=(.87,.82,.91,.86,.89,.92,.85,.88)[variant])
            result[kind].append(mid)
    # Reduce both encoded slope and shader strength. Full scanned normals at
    # building scale made flat painted walls look like deeply eroded stone.
    normal=data_dir/'materials/PaintedPlaster006/PaintedPlaster006_2K-JPG_NormalDX.jpg'
    if normal.exists():
        vectors=np.asarray(Image.open(normal).convert('RGB').resize((1024,512)),dtype=float)/127.5-1
        vectors[:,:,:2]*=.06
        vectors[:,:,2]=np.sqrt(np.maximum(1-np.sum(vectors[:,:,:2]**2,axis=2),0))
        Image.fromarray(np.clip((vectors+1)*127.5,0,255).astype('uint8')).save(textures/'plaster_normal_dx.jpg',quality=98)
        for mids in result.values():
            for mid in mids:
                scene['materials'][mid]['normal']='plaster_normal_dx.jpg'
                # The normal image already has 6% of the scan's slope. Avoid
                # multiplying by another 6%, which erased all plaster relief.
                scene['materials'][mid]['normal_strength']=.34
    Image.new('RGB',(16,16),(91,91,85)).save(textures/'eave_fascia.jpg',quality=94)
    result['trim']=material('roof_edge_fascia','eave_fascia.jpg',ambient=.24,diffuse=.35,specular=.025)
    result['frames']=[]
    for name,color in (('ivory',(215,213,203)),('timber',(82,68,55)),('charcoal',(62,66,65))):
        pixels=np.broadcast_to(np.array(color,dtype=float),(128,128,3)).copy()
        grain=np.random.default_rng(65).normal(0,1.5,(128,128,1))
        Image.fromarray(np.clip(pixels+grain,0,255).astype('uint8')).save(textures/f'window_frame_{name}.jpg',quality=95)
        mid=material('window_frame_'+name,f'window_frame_{name}.jpg',ambient=.29,diffuse=.47,specular=.075)
        scene['materials'][mid].update(roughness=.55,specular_exp=28.)
        result['frames'].append(mid)
    Image.new('RGB',(32,32),(157,155,145)).save(textures/'window_sill.jpg',quality=96)
    result['sill']=material('stone_window_sill','window_sill.jpg',ambient=.29,diffuse=.45,specular=.025)
    result['glass']=glass_textures(textures,material,scene,data_dir/'materials/AIWindows/window_atlas.png')
    result['entrance']=door_textures(textures,material,scene,data_dir/'materials/AIDoors/door_atlas.png')
    result['entrance_frames']=[]
    for variant,color in enumerate(((204,197,177),(96,63,41),(105,120,97),(57,63,68),
                                    (128,88,48),(201,195,176),(117,65,48),(207,207,201))):
        Image.new('RGB',(16,16),color).save(textures/f'door_frame_{variant}.jpg',quality=96)
        mid=material('door_frame_'+str(variant),f'door_frame_{variant}.jpg',ambient=.30,diffuse=.50,specular=.065)
        scene['materials'][mid].update(roughness=.68,specular_exp=28.)
        result['entrance_frames'].append(mid)
    result['shutters']=[]
    for name,color in (('sage',(72,87,73)),('ochre',(100,82,63)),('slate',(74,84,86))):
        Image.new('RGB',(32,32),color).save(textures/f'shutter_{name}.jpg',quality=96)
        mid=material('painted_shutter_'+name,f'shutter_{name}.jpg',ambient=.3,diffuse=.5,specular=.045)
        scene['materials'][mid].update(roughness=.74)
        result['shutters'].append(mid)
    Image.new('RGB',(16,16),(110,108,99)).save(textures/'door_reveal.jpg',quality=96)
    result['reveal']=material('doorway_reveals','door_reveal.jpg',ambient=.28,diffuse=.45,specular=.015)
    Image.new('RGB',(16,16),(133,135,133)).save(textures/'door_hardware.jpg',quality=96)
    result['metal']=material('brushed_door_hardware','door_hardware.jpg',ambient=.3,diffuse=.49,specular=.32)
    scene['materials'][result['metal']].update(roughness=.33,specular_exp=64.)
    Image.new('RGB',(16,16),(91,94,93)).save(textures/'zinc_gutter.jpg',quality=96)
    result['gutter']=material('zinc_gutters','zinc_gutter.jpg',ambient=.28,diffuse=.43,specular=.16)
    scene['materials'][result['gutter']].update(roughness=.46,specular_exp=36.)
    return result


def photo_source(data_dir, override):
    """Validate local photo provenance and image data, including excluded photos."""
    filename=override.get('texture')
    if not isinstance(filename,str) or not filename:
        raise ValueError('Invalid facade photo path')
    path=(data_dir/filename).resolve()
    if not path.is_relative_to(data_dir.resolve()) or not path.is_file():
        raise ValueError('Invalid facade photo path')
    if any(not isinstance(override.get(key),str) or not override[key].strip() for key in ('license','source')):
        raise ValueError('Photo source and license are required')
    try:
        with Image.open(path) as image:
            image.verify()
    except (OSError,ValueError,SyntaxError) as error:
        raise ValueError('Invalid facade photo image') from error
    return path


def build_lod2(data_dir, textures, scene, material, add_mesh, ac, line, road_polygon, dem, ortho_bounds,
               allow_outside_photo_overrides=False, terrain_height_sampler=None, base_road_reservation=None,
               ownership_polygon=None):
    if not isinstance(allow_outside_photo_overrides,bool):
        raise ValueError('allow_outside_photo_overrides must be a boolean')
    if terrain_height_sampler is not None and not callable(terrain_height_sampler):
        raise ValueError('terrain_height_sampler must be callable')
    facades=facade_materials(data_dir,textures,material,scene)
    roof=material('lod2_aerial_roofs','ortho.jpg',ambient=.2,diffuse=.38,specular=.01)
    tile_materials=roof_materials(data_dir,textures,scene,material)
    aerial=np.asarray(Image.open(textures/'ortho.jpg').convert('RGB')) if tile_materials and (textures/'ortho.jpg').is_file() else None
    path=data_dir/'buildings_lod2.gml'
    if not path.is_file():
        raise ValueError('Missing LoD2 buildings; run scripts/fetch_visual_data.py')
    doc=ET.parse(path).getroot()
    transform=Transformer.from_crs(4326,25832,always_xy=True)
    overrides_path=data_dir/'facade_overrides.json'
    overrides=json.loads(overrides_path.read_text(encoding='utf-8')) if overrides_path.exists() else {'facades':[]}
    overrides={(item['building_id'],item['surface_id']):item for item in overrides.get('facades',[])}
    used=set();ignored_outside=set();validated_photos={}
    catalog=[];seen=set();skipped=[];groups={};count=0;roofs=0;walls=0;photos=0
    entrance_count=0;eave_count=0;gable_count=0;window_count=0;tile_roof_count=0;gutter_count=0
    foundation_count=0;foundation_max_gap=0.;shutter_count=0;door_component_count=0
    downpipe_count=0;plinth_count=0;paint_counts={}
    def append(name,mid,points,faces,uv):
        points,faces,uv=exportable_mesh(points,faces,uv)
        if not faces:
            return
        key=(name,mid)
        group=groups.setdefault(key,{'v':[],'f':[],'uv':[],'part':0})
        if len(group['v'])+len(points)>64000:
            add_mesh(name+f'_M{mid}_P{group["part"]}',group['v'],group['f'],mid,group['uv'])
            part=group['part']+1;group={'v':[],'f':[],'uv':[],'part':part};groups[key]=group
        offset=len(group['v'])
        group['v'].extend(points.tolist());group['f'].extend([(a+offset,b+offset,c+offset) for a,b,c in faces]);group['uv'].extend(uv.tolist())
    def window_details(origin,u,normal,layout,variant,seed,wall_shape):
        nonlocal shutter_count
        center,bottom,top,width,_=layout
        left,right=center-width/2,center+width/2
        # The pane is on the source wall, surrounded by a projecting reveal.
        # It is recessed relative to trim without changing official wall faces.
        pane=np.asarray([origin+u*left+normal*.022,origin+u*right+normal*.022,
                         origin+u*right+normal*.022,origin+u*left+normal*.022])
        pane[:,2]=[bottom,bottom,top,top]
        glass=facades['glass'][seed%len(facades['glass'])]
        uv=np.asarray([(0,1),(1,1),(1,0),(0,0)],dtype=float)
        if 'uv_rect' in scene['materials'][glass]:
            u0,v0,u1,v1=scene['materials'][glass]['uv_rect']
            uv=uv*np.array([u1-u0,v1-v0])+np.array([u0,v0])
        append('LOD2_WINDOW_GLASS',glass,ac(pane[:,:2],pane[:,2]),[(0,1,2),(0,2,3)],uv)
        frame=facades['frames'][[0,0,1,0,2,0,0,1][variant%8]]
        # Dark, narrow joints and recessed returns give each opening depth.
        # Every addition stays outside the retained official wall plane.
        reveals=[(left-.064,left-.035,.008,.080,bottom-.060,top+.060),
                 (right+.035,right+.064,.008,.080,bottom-.060,top+.060),
                 (left-.035,right+.035,.008,.080,top+.035,top+.060)]
        for bounds in reveals:
            points,tris,uv=facade_box(origin,u,normal,bounds)
            append('LOD2_WINDOW_REVEALS',facades['reveal'],ac(points[:,:2],points[:,2]),tris,uv)
        strips=[(left-.052,left+.035,.018,.09,bottom-.052,top+.052),
                (right-.035,right+.052,.018,.09,bottom-.052,top+.052),
                (left,right,.018,.09,top-.035,top+.052),
                (left,right,.018,.09,bottom-.052,bottom+.035)]
        if variant%8 not in (1,5):
            strips.append((center-.022,center+.022,.024,.072,bottom,top))
        if variant%8 in (0,2,3,7):
            crossbar=bottom+(top-bottom)*.62
            strips.append((left,right,.025,.072,crossbar-.018,crossbar+.018))
        for bounds in strips:
            points,tris,uv=facade_box(origin,u,normal,bounds)
            append('LOD2_WINDOW_FRAMES',frame,ac(points[:,:2],points[:,2]),tris,uv)
        points,tris,uv=facade_box(origin,u,normal,(left-.10,right+.10,.01,.18,bottom-.09,bottom-.045))
        # A stone sill sheds rain away from the opening; its outer edge slopes
        # down by 12 mm instead of reading as a perfectly flat floating shelf.
        depth=(points-origin)@normal
        points[:,2]-=.012*np.clip((depth-.01)/.17,0,1)
        append('LOD2_WINDOW_SILLS',facades['sill'],ac(points[:,:2],points[:,2]),tris,uv)
        # Painted timber shutters are common, but only some house styles receive
        # them and every board must fit the real source wall, including holes.
        if variant%8 in (2,6):
            shutter_width=width*.43
            for a,b in ((left-.08-shutter_width,left-.08),(right+.08,right+.08+shutter_width)):
                rectangle=Polygon([(a,bottom-.035),(b,bottom-.035),(b,top+.035),(a,top+.035)])
                if not wall_shape.covers(rectangle):
                    continue
                shutter=facades['shutters'][0 if variant%8==6 else 1]
                pieces=[(a,b,.024,.054,bottom-.025,top+.025)]
                for z in np.arange(bottom+.08,top-.04,.105):
                    pieces.append((a+.025,b-.025,.054,.065,z,z+.027))
                for bounds in pieces:
                    points,tris,uv=facade_box(origin,u,normal,bounds)
                    append('LOD2_WINDOW_SHUTTERS',shutter,ac(points[:,:2],points[:,2]),tris,uv)
                shutter_count+=1

    def entrance_details(layout,variant):
        nonlocal door_component_count
        origin=layout['origin'];u=layout['u'];normal=layout['normal']
        center=layout['center'];width=layout['width'];bottom=layout['bottom'];top=layout['top']
        left,right=center-width/2,center+width/2
        pane=np.asarray([origin+u*left+normal*.04,origin+u*right+normal*.04,
                         origin+u*right+normal*.04,origin+u*left+normal*.04])
        pane[:,2]=[bottom,bottom,top,top]
        mid=facades['entrance'][variant%len(facades['entrance'])]
        uv=np.asarray([(0,1),(1,1),(1,0),(0,0)],dtype=float)
        textured='uv_rect' in scene['materials'][mid]
        if textured:
            uv=atlas_uv(uv,scene['materials'][mid]['uv_rect'])
        append('LOD2_ENTRANCES',mid,ac(pane[:,:2],pane[:,2]),[(0,1,2),(0,2,3)],uv)
        materials={'frame':facades['entrance_frames'][variant%8] if textured else facades['frames'][[1,2,1,0,1,2,1,2][variant%8]],
                   'reveal':facades['reveal'],'sill':facades['sill'],'metal':facades['metal']}
        for role,bounds in doorway_components(layout,variant,textured):
            points,tris,uv=facade_box(origin,u,normal,bounds)
            append('LOD2_DOOR_'+role.upper(),materials[role],ac(points[:,:2],points[:,2]),tris,uv)
            door_component_count+=1
    source_buildings=[]
    for member in doc.findall(WFS+'member'):
        building=member.find(BU+'Building')
        if building is None:
            continue
        identifier=building.get(GML+'id')
        faces=[]
        for polygon in building.findall('.//'+GML+'Polygon'):
            sid=polygon.get(GML+'id')
            if sid in seen:
                continue
            rings=[]
            for tag in ('exterior','interior'):
                for ring in polygon.findall(GML+tag+'/'+GML+'LinearRing'):
                    pos=ring.find(GML+'posList')
                    if pos is None or not pos.text:
                        continue
                    values=np.fromstring(pos.text,sep=' ')
                    if len(values)%3:
                        raise ValueError('LoD2 coordinates lost their height dimension')
                    latlon=values.reshape(-1,3)
                    if not (np.all((latlon[:,0]>49)&(latlon[:,0]<52)) and np.all((latlon[:,1]>7)&(latlon[:,1]<11))):
                        raise ValueError('Unexpected LoD2 axis order')
                    x,y=transform.transform(latlon[:,1],latlon[:,0])
                    rings.append(np.c_[x,y,latlon[:,2]])
            if rings:
                faces.append((sid,rings));seen.add(sid)
        if not faces:
            continue
        source_buildings.append((identifier,faces))
    from roadtoassetto.building_bases import source_footprint
    base_footprints=[source_footprint(faces) for _,faces in source_buildings]
    base_footprint_tree=STRtree(base_footprints)
    base_plans=[]
    for source_index,(identifier,faces) in enumerate(source_buildings):
        # Validate against the complete source extract before selecting the
        # requested route corridor. A misspelled ID or invalid photo must not
        # silently disappear merely because the chosen section is elsewhere.
        building_photos=set()
        for sid,rings in faces:
            key=(identifier,sid)
            if key not in overrides:
                continue
            try:
                _,_,photo_normal=triangulate_face(rings)
            except ValueError as error:
                raise ValueError(f'Facade photo references an invalid source wall: {key}') from error
            if abs(photo_normal[2])>.25:
                raise ValueError(f'Facade photo must reference a wall, not a roof or ground surface: {key}')
            validated_photos[key]=photo_source(data_dir,overrides[key])
            building_photos.add(key)
        all_vertices=np.vstack([rings[0] for _,rings in faces])
        footprint=MultiPoint(all_vertices[:,:2]).convex_hull
        if not isinstance(footprint,Polygon) or footprint.area<5:
            continue
        if ownership_polygon is not None and not ownership_polygon.covers(footprint.centroid):
            ignored_outside.update(building_photos)
            continue
        if footprint.distance(line)>120:
            if allow_outside_photo_overrides:
                ignored_outside.update(building_photos)
            continue
        # Crop to downloaded terrain; keep actual building positions, not a stretched substitute.
        lower=np.array(ortho_bounds[:2])+2;upper=np.array(ortho_bounds[2:])-2
        if np.any(all_vertices[:,:2]<lower) or np.any(all_vertices[:,:2]>upper):
            continue
        if footprint.intersection(road_polygon).area>2:
            skipped.append({'building_id':identifier,'reason':'overlap with estimated-width drivable road'})
            continue
        road_distance=footprint.distance(line)
        index_entry={'building_id':identifier,'height_min_m':float(all_vertices[:,2].min()),
                     'height_max_m':float(all_vertices[:,2].max()),'surfaces':[]}
        appearance=architectural_palette(identifier)
        variant=appearance['style'];paint=appearance['paint']
        base_z=float(all_vertices[:,2].min())
        prepared=[]
        for sid,rings in faces:
            try:
                v,triangles,normal=triangulate_face(rings)
            except ValueError as error:
                skipped.append({'building_id':identifier,'surface_id':sid,'reason':str(error)})
                continue
            if normal[2]<-.25:
                continue
            kind='roof' if normal[2]>.25 else 'wall'
            prepared.append((sid,rings,v,triangles,normal,kind))
        wall_faces=[(v,t,n) for _,_,v,t,n,k in prepared if k=='wall']
        roof_faces=[(v,t,n) for _,_,v,t,n,k in prepared if k=='roof']
        if not wall_faces:
            continue
        close_building=road_distance<48
        floor_height,window_cap=floor_layout(base_z,roof_faces,wall_faces)
        index_entry['visual_floor_height_m']=round(floor_height,3)
        index_entry['visual_window_cap_m']=round(window_cap,3)
        index_entry['inferred_paint_family']=paint
        index_entry['inferred_plinth_height_m']=appearance['plinth_height']
        paint_counts[str(paint)]=paint_counts.get(str(paint),0)+1
        roof_edges={};entrance_candidates=[];accepted=False
        for sid,rings,v,triangles,normal,kind in prepared:
            if kind=='roof':
                uv=np.c_[(v[:,0]-ortho_bounds[0])/(ortho_bounds[2]-ortho_bounds[0]),
                         1-(v[:,1]-ortho_bounds[1])/(ortho_bounds[3]-ortho_bounds[1])]
                mid=roof;name='LOD2_ROOFS';roofs+=1
                if aerial is not None and close_building and .3<normal[2]<.96:
                    # Retain the aerial classification, while close roof planes
                    # receive actual metric tile detail and a matching normal.
                    center=v.mean(axis=0)
                    col=int(np.clip((center[0]-ortho_bounds[0])/(ortho_bounds[2]-ortho_bounds[0])*aerial.shape[1],0,aerial.shape[1]-1))
                    row=int(np.clip((1-(center[1]-ortho_bounds[1])/(ortho_bounds[3]-ortho_bounds[1]))*aerial.shape[0],0,aerial.shape[0]-1))
                    sample=np.median(aerial[max(0,row-3):row+4,max(0,col-3):col+4].reshape(-1,3),axis=0)
                    choice='red' if sample[0]>sample[1]*1.14 and sample[0]>sample[2]*1.16 else 'grey'
                    if choice in tile_materials:
                        mid,scale=tile_materials[choice]
                        # U follows a level roof contour, V climbs the slope.
                        # Rows therefore lie parallel to the eave, never uphill.
                        contour=np.array([-normal[1],normal[0],0.]);contour/=np.linalg.norm(contour)
                        uphill=np.cross(normal,contour)
                        if uphill[2]<0:
                            contour=-contour;uphill=-uphill
                        local=v-v[0]
                        uv=np.c_[local@contour/scale,-local@uphill/scale]
                        name='LOD2_TILE_ROOFS';tile_roof_count+=1
                for ring in rings:
                    for a,b in zip(ring[:-1],ring[1:]):
                        key=tuple(sorted((tuple(np.round(a,3)),tuple(np.round(b,3)))))
                        if key in roof_edges:
                            roof_edges[key]['count']+=1
                        else:
                            roof_edges[key]={'a':a,'b':b,'count':1}
            else:
                # Horizontal metric coordinate follows the outward-oriented facade.
                u=np.array([-normal[1],normal[0],0.]);u/=np.linalg.norm(u)
                x=v@u;minx=x.min();zmax=v[:,2].max()
                walls+=1
                override=overrides.get((identifier,sid))
                if override:
                    p=validated_photos[(identifier,sid)]
                    target='photo_'+hashlib.sha256((identifier+sid).encode()).hexdigest()[:12]+'.jpg'
                    Image.open(p).convert('RGB').save(textures/target,quality=96)
                    mid=material('photo_'+sid,target,ambient=.4,diffuse=.6,specular=.025)
                    uv=np.c_[(x-minx)/max(x.max()-minx,.01),(zmax-v[:,2])/max(zmax-v[:,2].min(),.01)]
                    if override.get('flip_u'):
                        uv[:,0]=1-uv[:,0]
                    if 'projection' in override:
                        from roadtoassetto.photo_projection import projected_photo_uv
                        uv=projected_photo_uv(v,override)
                    name='LOD2_PHOTO';photos+=1;used.add((identifier,sid))
                else:
                    nearest=line.interpolate(line.project(Point(v[:,:2].mean(axis=0))))
                    toward=np.asarray(nearest.coords[0])-v[:,:2].mean(axis=0)
                    facing=float(normal[:2]@toward/max(np.linalg.norm(toward),.001))
                    close_wall=close_building and abs(normal[2])<.02 and facing>-.35 and np.linalg.norm(toward)<58
                    window_u,layouts=window_layout(rings,normal,base_z,floor_height,window_cap,variant) if close_wall else (u,[])
                    projected=[np.c_[np.asarray(r)@u,np.asarray(r)[:,2]] for r in rings]
                    shape=Polygon(projected[0],projected[1:])
                    # A blank scanned wall supports real trim geometry. Small
                    # source faces which cannot fit a window stay blank too.
                    # The plain upper band gives gables a clean triangle instead
                    # of cutting an arbitrary window row through the roof line.
                    # The near-wall plinth is a metric material band, not a
                    # painted strip repeated at every storey in the texture.
                    # Its triangles retain exactly the original wall surface.
                    plinth_top=base_z+appearance['plinth_height']
                    bands=(('plinth',-np.inf,plinth_top),
                           ('ground',plinth_top,min(base_z+floor_height,window_cap)),
                                       ('upper',base_z+floor_height,window_cap),
                           ('plain',window_cap,np.inf)) if close_wall else (
                           ('ground',-np.inf,min(base_z+floor_height,window_cap)),
                           ('upper',base_z+floor_height,window_cap),('plain',window_cap,np.inf))
                    for band,lo,hi in bands:
                        if hi<=lo:
                            continue
                        points,tris=clip_height_band(v,triangles,lo,hi)
                        if not tris:
                            continue
                        uv=(plaster_uv(points,normal,base_z,minx,appearance['uv_offset']) if close_wall else
                            facade_uv(points,normal,base_z,floor_height,minx))
                        kind_material=('foundation' if band=='plinth' else 'plain') if close_wall else band
                        append('LOD2_WALLS_'+band.upper(),facades[kind_material][paint],ac(points[:,:2],points[:,2]),tris,uv)
                        if band=='plain':
                            gable_count+=1
                        if band=='plinth':
                            plinth_count+=1
                    # Put at most one inferred entrance on a near, road-facing
                    # wall. Its whole rectangle must fit inside the source face,
                    # including holes, and it never overlays an imported photo.
                    if footprint.distance(line)<75 and abs(normal[2])<.02:
                        entrance=entrance_layout(rings,normal,base_z,variant,terrain_height_sampler)
                        if entrance:
                            center=entrance['center'];center_xy=(entrance['origin']+u*center)[:2]
                            nearest=line.interpolate(line.project(Point(center_xy)))
                            toward=np.asarray(nearest.coords[0])-center_xy
                            facing=float(normal[:2]@toward/max(np.linalg.norm(toward),.001))
                            if facing>.2:
                                score=float(np.linalg.norm(toward))+(1-facing)*12
                                entrance_candidates.append((score,entrance))
                                layouts=[item for item in layouts if item[4]!=0 or
                                         abs(item[0]-center)>(entrance['width']+item[3])/2+.22]
                    if layouts:
                        origin=v[0]-window_u*np.dot(v[0],window_u)
                        seed=int(hashlib.sha256((identifier+sid).encode()).hexdigest()[:8],16)
                        for wi,layout in enumerate(layouts):
                            window_details(origin,window_u,normal,layout,variant,seed+wi*3,shape)
                            window_count+=1
                    if close_wall and facing>.05:
                        eave=wall_eave_height(v,normal,roof_faces)
                        drain=downpipe_layout(rings,normal,base_z,eave,variant,terrain_height_sampler) if eave else None
                        if drain:
                            # Reject even a narrow overlap with inferred doors
                            # or windows instead of routing pipes through them.
                            center=drain['center']
                            blocked=any(abs(item[0]-center)<item[3]/2+.14 for item in layouts)
                            blocked|=any(abs(item[1]['center']-center)<item[1]['width']/2+.14
                                         for item in entrance_candidates if np.allclose(item[1]['normal'],normal))
                            if not blocked:
                                points,tris,uv=pipe_mesh(drain['path'])
                                append('LOD2_DOWNPIPES',facades['gutter'],ac(points[:,:2],points[:,2]),tris,uv)
                                for z in np.arange(drain['bottom']+.35,drain['top']-.2,1.65):
                                    points,tris,uv=facade_box(drain['origin'],drain['u'],normal,
                                        (center-.059,center+.059,.005,.11,z-.014,z+.014))
                                    append('LOD2_DOWNPIPE_BRACKETS',facades['gutter'],ac(points[:,:2],points[:,2]),tris,uv)
                                downpipe_count+=1
                    mid=None
            if kind=='roof' or override:
                world=ac(v[:,:2],v[:,2])
                append(name,mid,world,triangles,uv)
            index_entry['surfaces'].append({'surface_id':sid,'type':kind,'normal_enu':normal.round(5).tolist(),
                                           'vertices_utm32_height':np.asarray(rings[0]).round(3).tolist()})
            accepted=True
        if accepted:
            if road_distance<90 and terrain_height_sampler is not None:
                from roadtoassetto.building_bases import plan_building_base
                base_footprint=base_footprints[source_index]
                neighbours=[base_footprints[int(i)] for i in base_footprint_tree.query(base_footprint.buffer(.25))
                            if int(i)!=source_index]
                base_plan=plan_building_base(identifier,faces,terrain_height_sampler,
                    road_reservation=road_polygon if base_road_reservation is None else base_road_reservation,
                    neighbour_footprints=neighbours)
                base_plans.append((paint,base_plan))
            if entrance_candidates:
                _,entrance=min(entrance_candidates,key=lambda pair:pair[0])
                entrance_details(entrance,variant)
                entrance_count+=1
            if footprint.distance(line)<90:
                for edge in roof_edges.values():
                    a,b=edge['a'],edge['b']
                    if edge['count']!=1 or np.linalg.norm(b-a)<1.25 or abs(a[2]-b[2])>.06:
                        continue
                    points,tris,uv=eave_prism(a,b)
                    append('LOD2_EAVES',facades['trim'],ac(points[:,:2],points[:,2]),tris,uv)
                    eave_count+=1
                    if close_building and np.linalg.norm(b-a)>2:
                        # Wider zinc gutter below the surveyed horizontal edge.
                        # The edge position is observed; this profile is inferred.
                        points,tris,uv=eave_prism(a-np.array([0,0,.06]),b-np.array([0,0,.06]),width=.14,height=.1)
                        append('LOD2_GUTTERS',facades['gutter'],ac(points[:,:2],points[:,2]),tris,uv)
                        gutter_count+=1
            count+=1;catalog.append(index_entry)
    # New basement materials are appended after all original wall/photo
    # materials so imported facade bindings and their source meshes stay stable.
    basement_materials=[]
    for paint,mid in enumerate(facades['plain']):
        base_material=dict(scene['materials'][mid])
        base_material.update(name=f'mineral_basement_paint_{paint}',
            ambient=base_material['ambient']*.86,diffuse=base_material['diffuse']*.86,
            albedo_multiplier=[.86,.86,.86],roughness=.91)
        basement_materials.append(len(scene['materials']))
        scene['materials'].append(base_material)
    from roadtoassetto.building_bases import emit_building_base, PROVENANCE
    from shapely.geometry import mapping
    base_records=[]
    base_rejected=[]
    base_stats={'buildings':0,'panels':0,'soffits':0,'maximum_gap_m':0.,'rejected_intervals':0}
    for paint,plan in base_plans:
        emit_building_base(plan,{'wall':basement_materials[paint],
            'plinth':facades['foundation'][paint],'soffit':facades['foundation'][paint]},append,ac)
        base_records.extend(plan['records'])
        base_rejected.extend(plan.get('skipped',[]))
        stats=plan['stats']
        base_stats['buildings']+=int(bool(plan['records']))
        base_stats['panels']+=stats.get('building_base_panels',0)
        base_stats['soffits']+=stats.get('building_base_soffits',0)
        base_stats['maximum_gap_m']=max(base_stats['maximum_gap_m'],stats.get('building_base_maximum_gap_m',0.))
        base_stats['rejected_intervals']+=stats.get('building_base_rejected_intervals',0)
    accepted_ids={entry['building_id'] for entry in catalog}
    base_source_footprints=[{'building_id':identifier,'geometry':mapping(footprint)}
        for (identifier,_),footprint in zip(source_buildings,base_footprints)
        if identifier in accepted_ids and not footprint.is_empty]
    base_metadata={'building_base_stats':base_stats,'building_base_records':base_records,
        'building_base_accuracy':PROVENANCE,'building_base_source_footprints':base_source_footprints}
    (data_dir/'building_bases.json').write_text(json.dumps({**base_metadata,
        'rejected_intervals':base_rejected},indent=2,allow_nan=False),encoding='utf-8')
    foundation_count=base_stats['panels'];foundation_max_gap=base_stats['maximum_gap_m']
    for (name,mid),group in groups.items():
        add_mesh(name+f'_M{mid}_P{group["part"]}',group['v'],group['f'],mid,group['uv'])
    unused=set(overrides)-used-ignored_outside
    if unused:
        raise ValueError(f'Facade photo references unknown or excluded wall: {sorted(unused)}')
    (data_dir/'building_surfaces.json').write_text(json.dumps({'coordinate_system':'EPSG:25832 horizontal; EVRF2007 heights as delivered by WFS',
                                                            'buildings':catalog,'skipped':skipped},indent=2),encoding='utf-8')
    return {**base_metadata,'buildings':count,'lod2_roof_surfaces':roofs,'lod2_wall_surfaces':walls,'photo_facades':photos,
            'photo_facades_outside_segment':len(ignored_outside),
            'building_data':'HVBG INSPIRE LoD2','building_height_reference':'EVRF2007 as delivered; no high-precision vertical datum transformation',
            'facade_materials':'CC0 photographed fine plaster with eight independent muted mineral-paint families, metric plinths, recessed window profiles, sloped sills, 3D shutters and optional synthetic window/door atlases',
            'inferred_paint_families':paint_counts,'metric_plinth_surfaces':plinth_count,
            'three_dimensional_downpipes':downpipe_count,
            'procedural_entrances':entrance_count,'roof_edge_details':eave_count,'plain_gable_surfaces':gable_count,
            'three_dimensional_windows':window_count,'photographic_tiled_roof_surfaces':tile_roof_count,'zinc_gutter_edges':gutter_count,
            'three_dimensional_shutters':shutter_count,'three_dimensional_door_components':door_component_count,
            'synthetic_door_atlas':(data_dir/'materials/AIDoors/door_atlas.png').is_file(),
            'foundation_gap_panels':foundation_count,'foundation_max_gap_m':round(foundation_max_gap,3),
            'building_detail_accuracy':'Paint colours, plinth heights, window positions, shutters, curtains, entrances, thresholds, gutters, downpipes, ground-gap plinths and near roof tile type are inferred, not surveyed facade features. Generated door/window atlases are synthetic appearance assets. Official LoD2 wall/roof geometry and imported photo facades remain unchanged; distant roofs retain aerial imagery.',
            'building_surfaces_skipped':len(skipped),'source_buildings_in_extract':len(doc.findall(WFS+'member')),
            'photo_sources':[{k:f[k] for k in ('building_id','surface_id','license','source')} for key,f in overrides.items() if key in used]}
