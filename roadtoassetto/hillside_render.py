"""Metric render geometry for the shared, inferred hillside construction plan.

All plan coordinates are absolute EPSG:25832 / source heights. Render surfaces
keep their declared triangles and heights. The planner owns grading, exclusion
zones and access suitability; this module never moves a road or source house.
"""
from __future__ import annotations

from pathlib import Path
import math

import numpy as np
from PIL import Image
from scipy.ndimage import gaussian_filter
from shapely.geometry import Polygon, Point
from shapely.ops import unary_union

from roadtoassetto.buildings import exportable_mesh, pipe_mesh
from roadtoassetto.scenery import Batches


def hillside_textures(textures,quality='high'):
    """Original masonry shader assets, not edits of aerial or facade imagery.

    The stone sample represents 1.6 x 0.8 metres: four 20-cm courses, with
    40-cm stones, 8-mm mortar joints and 6-mm bevelled stone shoulders.
    """
    textures=Path(textures);textures.mkdir(parents=True,exist_ok=True)
    size=512 if quality=='high' else 256
    rng=np.random.default_rng(110410)
    yy,xx=np.indices((size,size));x=xx*1.6/size;y=yy*.8/size
    row=np.floor(y/.2).astype(int);shifted=x+(row%2)*.2
    col=np.floor(shifted/.4).astype(int)
    hx=np.mod(shifted,.4);hy=np.mod(y,.2)
    distance=np.minimum.reduce([hx,.4-hx,hy,.2-hy])
    block=np.mod(np.sin(col*42.71+row*87.39)*41238.2741,1.)
    grain=gaussian_filter(rng.normal(size=(size,size)),.6,mode='wrap')
    grain/=max(float(grain.std()),.001)
    pores=gaussian_filter(rng.normal(size=(size,size)),1.8,mode='wrap')
    bevel=np.clip((distance-.004)/.006,0,1)
    stone=139+(block-.5)*22+grain*2.3+pores*3
    value=(100+grain)* (1-bevel)+stone*bevel
    pixels=np.clip(value[:,:,None]+np.array([4.,1.,-5.]),0,255).astype('uint8')
    Image.fromarray(pixels).save(textures/'hillside_masonry.png')
    height=bevel*.0045+grain*.00012*bevel
    dx=(np.roll(height,-1,axis=1)-np.roll(height,1,axis=1))/(3.2/size)
    dy=(np.roll(height,-1,axis=0)-np.roll(height,1,axis=0))/(1.6/size)
    normal=np.stack([-dx,dy,np.ones_like(dx)],axis=-1)
    normal/=np.linalg.norm(normal,axis=-1)[:,:,None]
    Image.fromarray(np.clip((normal+1)*127.5,0,255).astype('uint8')).save(textures/'hillside_masonry_normal_dx.png')
    for filename,color in [('hillside_cap.png',(169,165,155)),
                           ('hillside_concrete.png',(152,150,143)),
                           ('hillside_mortar.png',(95,93,86)),
                           ('hillside_metal.png',(64,69,66))]:
        micro=rng.normal(0,1.4,(128,128,1))
        Image.fromarray(np.clip(np.array(color)[None,None,:]+micro,0,255).astype('uint8')).save(textures/filename)


def hillside_materials(textures,scene,material,quality='high'):
    hillside_textures(textures,quality)
    result={}
    for role,texture,ambient,diffuse,specular,roughness in [
        ('masonry','hillside_masonry.png',.30,.47,.025,.9),
        ('cap','hillside_cap.png',.31,.49,.04,.78),
        ('concrete','hillside_concrete.png',.30,.47,.025,.86),
        ('mortar','hillside_mortar.png',.25,.39,.01,.96),
        ('metal','hillside_metal.png',.27,.43,.18,.44),
        ('pavers','de_paving.png',.30,.47,.025,.88),
        ('asphalt','de_sidewalk_asphalt.png',.30,.47,.025,.92),
        ('gravel','shoulder_gravel.jpg',.38,.50,.01,.95)]:
        if not (Path(textures)/texture).is_file():
            raise ValueError('Missing hillside surface material: '+texture)
        mid=material('hillside_'+role,texture,ambient=ambient,diffuse=diffuse,specular=specular)
        scene['materials'][mid].update(roughness=roughness)
        result[role]=mid
    scene['materials'][result['masonry']].update(normal='hillside_masonry_normal_dx.png',normal_strength=.50)
    if (Path(textures)/'shoulder_gravel_normal_dx.jpg').is_file():
        scene['materials'][result['gravel']].update(normal='shoulder_gravel_normal_dx.jpg',normal_strength=.35)
    scene['materials'][result['metal']].update(specular_exp=48.)
    return result


def _validated_surface(xy,top,faces=None):
    xy=np.asarray(xy,dtype=float);top=np.asarray(top,dtype=float)
    if (xy.ndim!=2 or xy.shape[1]!=2 or len(xy)<3 or top.shape!=(len(xy),)
            or not np.isfinite(np.r_[xy.ravel(),top]).all()):
        raise ValueError('Invalid hillside surface coordinates')
    polygon=Polygon(xy)
    if not polygon.is_valid or polygon.area<.005:
        raise ValueError('Invalid hillside surface polygon')
    if faces is None:
        if len(xy)!=4:
            raise ValueError('Non-quad hillside surfaces require explicit triangles')
        faces=[[0,1,2],[0,2,3]]
    faces=np.asarray(faces,dtype=np.int64)
    if faces.ndim!=2 or faces.shape[1]!=3 or faces.min()<0 or faces.max()>=len(xy):
        raise ValueError('Invalid hillside surface triangle indices')
    area=sum(Polygon(xy[face]).area for face in faces)
    if abs(area-polygon.area)>max(1e-7,polygon.area*1e-6):
        raise ValueError('Incomplete hillside surface triangulation')
    return xy,top,faces,polygon


def surface_prism_parts(xy,bottom,top,faces,ac,*,uv_origin=None,uv_scale=(1.,1.)):
    """Separate top/body of a closed slab, retaining every planned top face."""
    xy,top,faces,polygon=_validated_surface(xy,top,faces)
    bottom=np.broadcast_to(np.asarray(bottom,dtype=float),top.shape)
    if not np.isfinite(bottom).all() or np.any(top-bottom<.005):
        raise ValueError('Hillside slab bottom must lie below its top')
    origin=xy[0] if uv_origin is None else np.asarray(uv_origin)
    horizontal=xy[1]-xy[0];horizontal/=np.linalg.norm(horizontal)
    cross=np.array([-horizontal[1],horizontal[0]])
    uv=np.c_[(xy-origin)@horizontal/uv_scale[0],(xy-origin)@cross/uv_scale[1]]
    high=ac(xy,top);low=ac(xy,bottom)
    top_faces=faces.copy();t=high[top_faces]
    flip=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])[:,1]<0
    top_faces[flip]=top_faces[flip,::-1]
    top_part=(high,top_faces,uv)
    vertices=low.tolist();body_faces=top_faces[:,::-1].tolist();body_uv=uv.tolist()
    center=np.vstack([low,high]).mean(axis=0)
    for i in range(len(xy)):
        j=(i+1)%len(xy);q=np.asarray([low[i],low[j],high[j],high[i]])
        tris=np.array([[0,1,2],[0,2,3]])
        if np.dot(np.cross(q[1]-q[0],q[2]-q[0]),q.mean(axis=0)-center)<0:
            tris=tris[:,::-1]
        offset=len(vertices);vertices.extend(q.tolist());body_faces.extend((tris+offset).tolist())
        length=float(np.linalg.norm(xy[j]-xy[i]))
        body_uv.extend([[0,bottom[i]],[length,bottom[j]],[length,top[j]],[0,top[i]]])
    return top_part,(np.asarray(vertices),np.asarray(body_faces),np.asarray(body_uv))


def wall_outline(edge):
    """Plan inner edge plus outward thickness, with no centreline ambiguity."""
    xy=np.asarray(edge['xy'],dtype=float);top=np.asarray(edge['top'],dtype=float)
    bottom=np.asarray(edge['bottom'],dtype=float);out=np.asarray(edge['outward'],dtype=float)
    width=float(edge.get('width_m',.24))
    if (xy.shape!=(2,2) or top.shape!=(2,) or bottom.shape!=(2,) or out.shape!=(2,)
            or not np.isfinite(np.r_[xy.ravel(),top,bottom,out,width]).all()
            or not .10<=width<=.8 or np.linalg.norm(out)<.9 or np.any(top-bottom<.02)):
        raise ValueError('Invalid retaining wall plan')
    out/=np.linalg.norm(out);direction=xy[1]-xy[0]
    if np.linalg.norm(direction)<.08 or abs(float(direction@out))/np.linalg.norm(direction)>.02:
        raise ValueError('Retaining wall outward must be perpendicular to edge')
    footprint=np.array([xy[0],xy[1],xy[1]+out*width,xy[0]+out*width])
    return footprint,bottom[[0,1,1,0]],top[[0,1,1,0]],out


def wall_cap_segments(edge,max_length=1.,joint=.005):
    """Individual 60-mm coping stones with slight lateral overhang and joints."""
    xy,_,_,out=wall_outline(edge)
    inner=xy[:2];length=float(np.linalg.norm(inner[1]-inner[0]));count=max(1,int(math.ceil(length/max_length)))
    top=np.asarray(edge['top'],dtype=float)
    result=[];width=float(edge.get('width_m',.24))
    for index in range(count):
        a=index/count+(joint/2/length if index else 0.)
        b=(index+1)/count-(joint/2/length if index<count-1 else 0.)
        ends=inner[0]+np.array([a,b])[:,None]*(inner[1]-inner[0])
        p=np.array([ends[0]-out*.02,ends[1]-out*.02,ends[1]+out*(width+.02),ends[0]+out*(width+.02)])
        bottom=(top[0]+np.array([a,b])*(top[1]-top[0]))[[0,1,1,0]]
        high=bottom+.06
        # A small crossfall drains away from the retained platform.
        high[2:]-=.006
        result.append((p,bottom,high))
    return result


def handrail_geometry(rail,ac,height=.95):
    """Tubular handrail and posts at bounded intervals, anchored to plan levels."""
    xy=np.asarray(rail['xy'],dtype=float);base=np.asarray(rail['base'],dtype=float)
    if (xy.ndim!=2 or xy.shape[1]!=2 or len(xy)<2 or base.shape!=(len(xy),)
            or not np.isfinite(np.r_[xy.ravel(),base]).all()):
        raise ValueError('Invalid hillside handrail')
    output=[];top=np.c_[xy,base+height]
    for a,b in zip(top[:-1],top[1:]):
        if np.linalg.norm(b-a)<.015:
            continue
        points,faces,uv=pipe_mesh([a,b],radius=.021,sides=10)
        output.append((ac(points[:,:2],points[:,2]),faces,uv))
    distance=np.r_[0.,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    if distance[-1]<.08 or np.any(np.diff(distance)<.005):
        raise ValueError('Hillside handrail has coincident plan points')
    # Preserve the planner's support stations instead of inventing post feet
    # halfway between two step levels. Only long level spans need subdivision.
    stations=[distance[0]]
    for i in range(len(distance)-1):
        if abs(base[i+1]-base[i])<.005:
            count=max(1,int(math.ceil((distance[i+1]-distance[i])/1.2)))
            stations.extend(np.linspace(distance[i],distance[i+1],count+1)[1:].tolist())
        elif i==len(distance)-2 or distance[i+1]-stations[-1]>=.85:
            stations.append(distance[i+1])
    for station in stations:
        point=np.array([np.interp(station,distance,xy[:,0]),np.interp(station,distance,xy[:,1])])
        low=float(np.interp(station,distance,base))-.035
        points,faces,uv=pipe_mesh([np.r_[point,low],np.r_[point,low+.035+height]],radius=.018,sides=8)
        output.append((ac(points[:,:2],points[:,2]),faces,uv))
    return output


def build_hillside(plan,textures,scene,material,add_mesh,ac,ground_sampler,quality='high'):
    """Render only accepted construction plans using their shared terrain datum."""
    surfaces=plan.get('surfaces',[]);walls=plan.get('retaining_edges',[])
    if not surfaces and not walls:
        return {'hillside_rendered_surfaces':0,
                'hillside_rendered_surface_counts':dict.fromkeys(('terrace','landing','stair','driveway','bank_ledge','footpath'),0),
                'hillside_rendered_walls':0,'hillside_wall_coping_stones':0,
                'hillside_rendered_handrails':0,'hillside_flush_edging_parts':0,'hillside_render_collision':False}
    mats=hillside_materials(textures,scene,material,quality);batches=Batches(add_mesh)
    counts={kind:0 for kind in ('terrace','landing','stair','driveway','bank_ledge','footpath')};wall_count=0;cap_count=0;rail_count=0;edging_count=0
    def append(name,mid,part):
        vertices,faces,uv=exportable_mesh(*part)
        if faces:
            batches.append(name,mid,vertices,faces,uv)
    surface_union=unary_union([Polygon(s['xy']) for s in surfaces])
    surface_names={'terrace':'HILLSIDE_TERRACE','landing':'HILLSIDE_LANDING',
                   'stair':'HILLSIDE_STAIR_TREAD','driveway':'HILLSIDE_DRIVEWAY',
                   'bank_ledge':'HILLSIDE_BANK_LEDGE','footpath':'HILLSIDE_FOOTPATH'}
    for section in surfaces:
        kind=section['kind']
        if kind not in surface_names:
            raise ValueError('Unknown hillside surface kind: '+kind)
        xy,top,faces,polygon=_validated_surface(section['xy'],section['top'],section['faces'])
        sampled=np.asarray(ground_sampler(xy),dtype=float)
        if sampled.shape!=top.shape or not np.isfinite(sampled).all():
            raise ValueError('Invalid terrain beneath hillside construction')
        bottom=np.minimum(sampled-.06,top-.10)
        bank=kind=='bank_ledge'
        high,body=surface_prism_parts(xy,bottom,top,faces,ac,uv_scale=(1.5,1.5) if bank else (1.,1.))
        top_material=mats['gravel'] if bank else mats.get(section.get('style','pavers'),mats['pavers'])
        append(surface_names[kind],top_material,high)
        append('HILLSIDE_RISERS' if kind=='stair' else 'HILLSIDE_SURFACE_BASE',mats['gravel'] if bank else mats['concrete'],body)
        counts[kind]+=1
        # A flush concrete perimeter, never a raised obstacle across an access.
        # Shared step/podium boundaries are excluded from this decorative rim.
        if kind not in ('stair','bank_ledge'):
            for i in range(len(xy)):
                j=(i+1)%len(xy);a,b=xy[i],xy[j];delta=b-a;length=float(np.linalg.norm(delta))
                if length<.25:continue
                inward=np.array([-delta[1],delta[0]])/length
                if not polygon.covers(Point((a+b)/2+inward*.02)):inward=-inward
                if surface_union.covers(Point((a+b)/2-inward*.02)):continue
                # Clip the narrow strip to each planned triangle, retaining its
                # exact plane on warped ramps. No extrapolated flat edging.
                rim=Polygon([a,b,b+inward*.08,a+inward*.08]).intersection(polygon)
                for face in faces:
                    tri=xy[face];piece=rim.intersection(Polygon(tri))
                    if not isinstance(piece,Polygon) or piece.area<.002:continue
                    p=np.asarray(piece.exterior.coords)[:-1]
                    if len(p)<3:continue
                    matrix=np.c_[tri-tri[0],np.ones(3)];coef=np.linalg.solve(matrix,top[face])
                    heights=np.c_[p-tri[0],np.ones(len(p))]@coef+.0015
                    f=np.array([[0,k,k+1] for k in range(1,len(p)-1)])
                    verts=ac(p,heights);t=verts[f];flip=np.cross(t[:,1]-t[:,0],t[:,2]-t[:,0])[:,1]<0;f[flip]=f[flip,::-1]
                    append('HILLSIDE_EDGE',mats['cap'],(verts,f,p-a));edging_count+=1
    for edge in walls:
        xy,bottom,top,out=wall_outline(edge)
        high,body=surface_prism_parts(xy,bottom,top,[[0,1,2],[0,2,3]],ac)
        # Wall-side UVs represent 1.6 x 0.8 m, including an absolute-height
        # phase so adjoining wall cells keep aligned mortar courses.
        v,f,uv=body;uv=uv/np.array([1.6,.8]);uv[:,1]-=math.floor(float(np.min(bottom))/.8)
        append('HILLSIDE_RETAINING_WALL',mats['masonry'],(v,f,uv))
        append('HILLSIDE_RETAINING_WALL',mats['mortar'],high)
        for p,lo,hi in wall_cap_segments(edge):
            cap,body=surface_prism_parts(p,lo,hi,[[0,1,2],[0,2,3]],ac)
            append('HILLSIDE_WALL_CAP',mats['cap'],cap);append('HILLSIDE_WALL_CAP',mats['cap'],body);cap_count+=1
        wall_count+=1
    for stairs in plan.get('stairs',[]):
        for rail_index,rail in enumerate(stairs.get('handrails',[])):
            try:
                parts=handrail_geometry(rail,ac)
            except ValueError as error:
                raise ValueError(f"Hillside stair {stairs.get('id','<unnamed>')} handrail {rail_index}: {error}") from error
            for part in parts:
                append('HILLSIDE_HANDRAIL',mats['metal'],part)
            rail_count+=1
    batches.finish()
    return {'hillside_rendered_surfaces':sum(counts.values()),'hillside_rendered_surface_counts':counts,
            'hillside_rendered_walls':wall_count,'hillside_wall_coping_stones':cap_count,
            'hillside_rendered_handrails':rail_count,'hillside_flush_edging_parts':edging_count,
            'hillside_render_collision':False,
            'hillside_materials':'Original procedural 40x20-cm coursed stone with 8-mm mortar, metric concrete coping, existing small-format pavers/asphalt and original CC0 gravel at 1.5-m repeat on bank ledges; no projected aerial image',
            'hillside_render_accuracy':'Inferred construction dressing from a shared source-height plan, not surveyed individual walls, stairs or property access. Original road and LoD2 geometry remain unchanged.'}
