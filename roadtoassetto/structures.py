"""OSM structure spans with explicit, inferred 3-D engineering geometry.

DGM is terrain, not a bridge deck or tunnel floor. Portal/abutment anchors and
robust DOM observations constrain heights; generic construction is not survey.
"""
from __future__ import annotations
import numpy as np
from scipy.ndimage import median_filter, gaussian_filter1d, maximum_filter1d
from shapely import STRtree, linestrings
from shapely.geometry import LineString, Polygon
from shapely.ops import unary_union


def structure_kind(tags):
    active=lambda key: str(tags.get(key,'no')).lower() not in ('no','0','false','')
    if active('bridge'): return 'bridge'
    if active('tunnel') or active('covered'): return 'tunnel'
    # Layer is relative ordering, not proof of a bridge or a tunnel.
    return 'ground'


def collect_spans(inputs, count):
    codes=np.zeros(count,dtype=np.int8)
    tags_by_index={}
    for selected in inputs:
        for way in selected['ways']:
            kind=structure_kind(way['tags'])
            a=-int(way['nodes'][0])-1;b=-int(way['nodes'][-1])-1
            if kind!='ground':
                codes[a:b]=1 if kind=='bridge' else 2
                for index in range(a,b):tags_by_index[index]=way['tags']
    spans=[]
    for index in range(count-1):
        if not codes[index]:continue
        if not spans or spans[-1]['end']!=index or spans[-1]['code']!=int(codes[index]):
            spans.append({'start':index,'end':index+1,'code':int(codes[index]),
                          'kind':'bridge' if codes[index]==1 else 'tunnel','tags':tags_by_index[index]})
        else:spans[-1]['end']=index+1
    return codes,spans


def resolve_heights(xy, ground, spans, surface=None):
    """Continuous portal/abutment interpolation, with bounded robust DOM fit."""
    ground=np.asarray(ground,dtype=float);height=ground.copy()
    distance=np.r_[0,np.cumsum(np.linalg.norm(np.diff(xy,axis=0),axis=1))]
    records=[]
    for span in spans:
        a,b=span['start'],span['end']
        # Anchor outside the mapped structure: DGM at its portal can already
        # describe the roof or river bank. Keep a short explicit approach.
        margin=12 if span['kind']=='bridge' else 8
        lo=max(0,a-margin);hi=min(len(height)-1,b+margin)
        if lo==0 or hi==len(height)-1:
            raise ValueError('Start und Ziel müssen mindestens 15 m außerhalb von Brücken und Tunneln liegen.')
        anchors=np.array([np.median(ground[max(0,lo-2):lo+1]),np.median(ground[hi:min(len(ground),hi+3)])])
        phase=(distance[lo:hi+1]-distance[lo])/(distance[hi]-distance[lo])
        line=anchors[0]*(1-phase)+anchors[1]*phase
        method='DGM1 approach anchors; inferred portal/abutment interpolation'
        if span['kind']=='bridge' and surface is not None:
            observed=median_filter(np.asarray(surface[lo:hi+1],dtype=float),size=9,mode='nearest')
            good=np.isfinite(observed)&(np.abs(observed-line)<3.)
            good[:a-lo]=False;good[b-lo+1:]=False
            if np.count_nonzero(good)>=max(3,(b-a)*.5):
                # Fit a smooth arch correction, never vehicles, parapets or
                # canopy spikes. Zero at anchors ensures road continuity.
                basis=np.sin(np.pi*phase)
                delta=float(np.median((observed[good]-line[good])/np.maximum(.2,basis[good])))
                line+=np.clip(delta,-1.5,1.5)*basis
                method+='; robust DOM1 deck correction'
        height[lo:hi+1]=line
        records.append({**span,'start_m':float(distance[a]),'end_m':float(distance[b]),
                        'height_method':method,'surveyed':False,'headroom_m':5.5 if span['kind']=='tunnel' else None})
    # One final global smoothing, including approach joints.
    height=gaussian_filter1d(height,3.,mode='nearest')
    initial=height.copy()
    # Isolated DGM ridges must not make a sharp crest in an otherwise smooth
    # road. Apply extra smoothing locally, bounded to 50 cm vertical change.
    for _ in range(3):
        grade=np.diff(height)/np.diff(distance)
        abrupt=np.abs(np.diff(grade))>.035
        if not np.any(abrupt):break
        mask=np.r_[False,abrupt,False].astype(float)
        weight=gaussian_filter1d(maximum_filter1d(mask,21),4.)
        height+=(gaussian_filter1d(height,2.5,mode='nearest')-height)*weight
    if np.max(np.abs(height-initial))>.5:
        raise ValueError('Das Straßenhöhenmodell benötigt eine zu große Korrektur. Quelldaten prüfen.')
    if np.max(np.abs(np.diff(height)/np.diff(distance)))>.25:
        raise ValueError('Das Bauwerkshöhenmodell überschreitet 25 % Steigung. Quelldaten prüfen.')
    return height,records


def validate_crossings(xy, height, codes):
    segments=linestrings(np.stack((xy[:-1],xy[1:]),axis=1))
    pairs=STRtree(segments).query(segments,predicate='crosses')
    records=[]
    for a,b in pairs.T:
        if a>=b or b-a<3:continue
        point=segments[a].intersection(segments[b])
        ta=segments[a].project(point,normalized=True);tb=segments[b].project(point,normalized=True)
        ha=height[a]*(1-ta)+height[a+1]*ta;hb=height[b]*(1-tb)+height[b+1]*tb
        gap=abs(float(ha-hb))
        if not (codes[a] or codes[b]) or gap<5.2:
            raise ValueError(f'Kreuzende Fahrbahnen bei Stützpunkt {a}/{b} haben keine belegte freie Durchfahrt (Höhenabstand {gap:.2f} m).')
        records.append({'sample_a':int(a),'sample_b':int(b),'vertical_separation_m':gap})
    return records


def span_polygons(xy, half, codes, padding=2.):
    pieces=[]
    for i in np.flatnonzero(np.asarray(codes[:-1])!=0):
        pieces.append(LineString(xy[i:i+2]).buffer(float(max(half[i:i+2]))+padding,cap_style='square'))
    return unary_union(pieces)


class StructureGround:
    """Do not pull valley ground up to decks or dig open tunnel roofs."""
    def __init__(self, base, xy, codes):
        from scipy.spatial import cKDTree
        self.base=base;self.tree=cKDTree(xy);self.codes=np.asarray(codes)
    def __call__(self, points, raw):
        values=self.base(points,raw)
        _,indices=self.tree.query(points)
        mask=self.codes[indices]!=0
        values[mask]=np.asarray(raw)[mask]
        return values
    def sample(self,*args,**kwargs):return self.base.sample(*args,**kwargs)
    def __getattr__(self,name):return getattr(self.base,name)


def clear_structure_corridors(scene,xy,height,half,codes,origin,height_origin):
    """Remove inferred ground furniture on decks; open only tunnel approaches.

    Tunnel interior terrain remains above the lining. At shallow portals the
    colliding terrain triangles are cut away across the full driving envelope.
    """
    from shapely import polygons, intersects, points
    from scipy.spatial import cKDTree
    area=span_polygons(xy,half,codes,padding=2.)
    tunnel=span_polygons(xy,half,np.where(codes==2,2,0),padding=1.1)
    tree=cKDTree(xy)
    retained=[]
    for mesh in scene['meshes']:
        name=mesh['name']
        if name in ('1ROAD_MAIN','1SHOULDER_LEFT','1SHOULDER_RIGHT') or name.startswith(('LOD2_','BASEMENT_','ROAD_MARK','MARKING','ASPHALT_','OBSERVED_MARKING_','ROAD_ASPHALT_')):
            retained.append(mesh);continue
        p=np.asarray(mesh['vertices']);f=np.asarray(mesh['faces'],dtype=int)
        world=np.c_[p[:,0]+origin[0],origin[1]-p[:,2]]
        centers=world[f].mean(axis=1)
        if name.startswith('1GRASS'):
            if tunnel.is_empty:retained.append(mesh);continue
            _,index=tree.query(centers)
            # Intersections, not just centroid, protect the entire vehicle path.
            overlap=intersects(tunnel,polygons(world[f]))
            low=p[f,1].min(axis=1)+height_origin
            high=p[f,1].max(axis=1)+height_origin
            remove=overlap & (low<height[index]+5.95) & (high>height[index]-.3)
        else:
            remove=intersects(area,points(centers))
        mesh['faces']=f[~remove].tolist()
        if mesh['faces']:
            used=np.unique(mesh['faces']);remap=np.full(len(p),-1,dtype=int);remap[used]=np.arange(len(used))
            mesh['faces']=remap[np.asarray(mesh['faces'])].tolist()
            for key in ('vertices','uv','normals'):
                if key in mesh:mesh[key]=np.asarray(mesh[key])[used].tolist()
            retained.append(mesh)
    scene['meshes']=retained


def build_structures(xy,height,left,half,distance,codes,ac,add_mesh,material,dem,core=None,prefix=''):
    """Closed slabs, physical parapets, lining and open portal rings.

    Segment ownership is by route chainage rather than 2-D terrain ownership,
    so crossing decks and tunnels survive multi-model clipping intact.
    """
    mat=material('structure_concrete','concrete.png',ambient=.48,diffuse=.65)
    rail=material('structure_steel','concrete.png',ambient=.35,diffuse=.5,specular=.25)
    stats={'bridge_segments':0,'tunnel_segments':0,'structure_piers':0,'tunnel_portals':0}
    def prism(name,poly,lower,upper,mat_id):
        poly=np.asarray(poly);n=len(poly)
        p=np.vstack((ac(poly,np.broadcast_to(lower,n)),ac(poly,np.broadcast_to(upper,n))))
        f=[]
        for j in range(1,n-1):f.extend([(0,j+1,j),(n,n+j,n+j+1)])
        for j in range(n):k=(j+1)%n;f.extend([(j,k,n+k),(j,n+k,n+j)])
        # Orient outward independent of UTM -> AC handedness.
        center=p.mean(axis=0)
        for k,face in enumerate(f):
            q=p[list(face)]
            if np.dot(np.cross(q[1]-q[0],q[2]-q[0]),q.mean(axis=0)-center)<0:f[k]=(face[0],face[2],face[1])
        add_mesh('1STRUCT_'+prefix+name,p,f,mat_id,collision=True)
    first,last=core if core else (0,len(xy)-1)
    for i in range(first,last):
        code=int(codes[i])
        if not code:continue
        sl=slice(i,i+2);h=half[sl];y=height[sl];l=left[sl];x=xy[sl]
        def band(u,v):
            p=np.array([x[0]+l[0]*u[0],x[1]+l[1]*u[1],x[1]+l[1]*v[1],x[0]+l[0]*v[0]])
            return p,np.r_[y,y[::-1]]
        if code==1:
            stats['bridge_segments']+=1
            p,z=band(-h-.55,h+.55);prism(f'DECK_{i}',p,z-.65,z-.16,mat)
            for side in (-1,1):
                p,z=band(side*(h+.4),side*(h+.58));prism(f'PARAPET_{i}_{side}',p,z-.16,z+.9,mat)
                p,z=band(side*(h+.42),side*(h+.56));prism(f'RAIL_{i}_{side}',p,z+1.1,z+1.2,rail)
            # Supports are illustrative. Avoid supports over unknown road/water
            # crossings: end abutments suffice; no arbitrary mid-span piers.
            for endpoint in (i,i+1):
                if (endpoint>0 and codes[endpoint-1]==1) and (endpoint<len(codes)-1 and codes[endpoint]==1):continue
                p=xy[endpoint]+np.array([-left[endpoint]*(half[endpoint]+.55),left[endpoint]*(half[endpoint]+.55)])
                t=np.array([-left[endpoint,1],left[endpoint,0]])*.45
                poly=np.array([p[0]-t,p[1]-t,p[1]+t,p[0]+t]);ground=dem(poly)
                prism(f'ABUTMENT_{endpoint}',poly,np.minimum(ground-.4,height[endpoint]-.7),height[endpoint]-.16,mat)
        else:
            stats['tunnel_segments']+=1
            for side in (-1,1):
                p,z=band(side*(h+.5),side*(h+.95));prism(f'TUNNEL_WALL_{i}_{side}',p,z-.3,z+5.75,mat)
            p,z=band(-h-.95,h+.95);prism(f'TUNNEL_ROOF_{i}',p,z+5.5,z+5.95,mat)
            # Reflective guide strips remain legible with AC's base lighting.
            for side in (-1,1):
                p,z=band(side*(h+.48),side*(h+.51));prism(f'TUNNEL_GUIDE_{i}_{side}',p,z+.7,z+.78,rail)
            for endpoint,direction in ((i,-1),(i+1,1)):
                if direction==-1 and endpoint>0 and codes[endpoint-1]==2:continue
                if direction==1 and endpoint<len(codes)-1 and codes[endpoint]==2:continue
                stats['tunnel_portals']+=1
                c=xy[endpoint];lateral=left[endpoint];width=half[endpoint];floor=height[endpoint]
                outward=np.array([lateral[1],-lateral[0]])*direction
                def portal_band(a,b):
                    return np.array([c+lateral*a-outward*.6,c+lateral*b-outward*.6,
                                     c+lateral*b+outward*.6,c+lateral*a+outward*.6])
                prism(f'PORTAL_HEADER_{endpoint}',portal_band(-width-1.25,width+1.25),floor+5.5,floor+6.2,mat)
                for side in (-1,1):
                    prism(f'PORTAL_PIER_{endpoint}_{side}',portal_band(side*(width+.48),side*(width+1.25)),floor-.5,floor+6.2,mat)
                    # Flared retaining wings close the cut slope beside the
                    # opening; all their vertices stay outside the drive lane.
                    inner=c+outward*.6+lateral*side*(width+.75)
                    outer=c+outward*10+lateral*side*(width+3.)
                    poly=np.array([inner,outer,outer+lateral*side*.5,inner+lateral*side*.5])
                    top=np.clip(dem(poly)+.3,floor+1.,floor+6.2)
                    top[[0,3]]=floor+6.2
                    prism(f'PORTAL_WING_{endpoint}_{side}',poly,np.minimum(dem(poly)-.4,floor-.5),top,mat)
    return stats


def build_context_bridges(data_dir,main_xy,main_y,excluded,ac,add_mesh,material,dem,bounds):
    """Render mapped crossing bridges even when our route travels underneath."""
    from xml.etree import ElementTree as ET
    from pyproj import Transformer
    from scipy.spatial import cKDTree
    from roadtoassetto.prepare import Heights
    from shapely.geometry import box
    path=data_dir/'dom1.tif'
    if not path.is_file():return {'context_bridges':0,'context_bridge_warning':'DOM1 unavailable; surrounding overpasses not reconstructed'}
    dom=Heights(path);document=ET.parse(data_dir/'area.osm').getroot()
    transform=Transformer.from_crs(4326,25832,always_xy=True)
    nodes={int(n.attrib['id']):transform.transform(float(n.attrib['lon']),float(n.attrib['lat'])) for n in document.findall('node')}
    main=LineString(main_xy);coverage=box(*bounds).buffer(-3);tree=cKDTree(main_xy);count=0
    for way in document.findall('way'):
        if int(way.attrib['id']) in excluded:continue
        tags={t.attrib['k']:t.attrib['v'] for t in way.findall('tag')}
        if structure_kind(tags)!='bridge' or 'highway' not in tags:continue
        coords=[nodes[int(n.attrib['ref'])] for n in way.findall('nd') if int(n.attrib['ref']) in nodes]
        if len(coords)<2:continue
        line=LineString(coords)
        if line.length<3 or line.distance(main)>100 or not coverage.covers(line):continue
        s=np.linspace(0,line.length,max(3,int(np.ceil(line.length/2))+1))
        xy=np.array([line.interpolate(float(d)).coords[0] for d in s])
        tangent=np.gradient(xy,axis=0);tangent/=np.linalg.norm(tangent,axis=1)[:,None]
        left=np.c_[-tangent[:,1],tangent[:,0]]
        observed=np.percentile([dom(xy+left*offset) for offset in (-.7,0,.7)],25,axis=0)
        smooth=median_filter(observed,7,mode='nearest')
        # A low-order robust fit suppresses cars, lamps and isolated canopy.
        good=np.isfinite(smooth)
        for _ in range(3):
            fit=np.polyfit(s[good]/s[-1],smooth[good],min(2,np.count_nonzero(good)-1))
            height=np.polyval(fit,s/s[-1]);good=np.abs(smooth-height)<1.8
            if np.count_nonzero(good)<3:break
        if np.count_nonzero(good)<3 or np.max(np.abs(np.diff(height)/np.diff(s)))>.2:continue
        lateral,nearest=tree.query(xy)
        close=lateral<3.
        if np.any(close) and np.min(height[close]-main_y[nearest[close]])<5.2:
            # This can be a parallel/underpassing bridge, not an overhead one.
            if np.max(height[close]-main_y[nearest[close]])>1.:raise ValueError('DOM1 bestätigt die Durchfahrtshöhe einer kreuzenden Überführung nicht eindeutig.')
            continue
        try:width=float(tags.get('width',6.))
        except ValueError:width=6.
        half=np.full(len(xy),np.clip(width,3,14)/2)
        codes=np.ones(len(xy),dtype=np.int8);codes[-1]=0
        build_structures(xy,height,left,half,s,codes,ac,add_mesh,material,dem,prefix=f'CONTEXT_{way.attrib["id"]}_')
        # The deck top also carries asphalt; its generic slab alone has no top
        # at road height and would otherwise show an open gap between rails.
        verts=ac(np.stack((xy-left*half[:,None],xy+left*half[:,None]),axis=1).reshape(-1,2),np.repeat(height,2))
        faces=[]
        for i in range(len(xy)-1):
            a=2*i;faces.extend([(a,a+2,a+1),(a+1,a+2,a+3)])
        if np.cross(verts[faces[0][1]]-verts[faces[0][0]],verts[faces[0][2]]-verts[faces[0][0]])[1]<0:faces=[(a,c,b) for a,b,c in faces]
        mat=material('context_bridge_asphalt','asphalt.png',ambient=.4,diffuse=.6)
        add_mesh(f'1STRUCT_CONTEXT_{way.attrib["id"]}_TOP',verts,faces,mat,collision=True)
        count+=1
    return {'context_bridges':count}
