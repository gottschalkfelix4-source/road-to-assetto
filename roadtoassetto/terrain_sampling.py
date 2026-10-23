"""Height sampling on the exact exported terrain triangles, without retriangulation."""
from __future__ import annotations

import numpy as np
from shapely import STRtree, points as geometry_points, polygons


class ExportedTerrainSampler:
    """Callable UTM XY -> absolute height on the existing AC terrain mesh.

    Build once using ``from_scene(scene, origin_utm32, height_origin_m)`` after
    terrain export, then pass the instance to building/apron generators. Terrain
    already contains the road grading and its vertical offset; apply neither a
    second time. Out-of-grid points raise ValueError rather than falling back to
    an incompatible analytic surface. Empty input returns an empty array.
    """
    def __init__(self, triangles_ac, origin_utm32, height_origin_m):
        # KN5 stores float32. Test precisely that stored geometry, including
        # the rounding that can matter at very shallow building contacts.
        triangles=np.asarray(triangles_ac,dtype=np.float32).astype(np.float64)
        origin=np.asarray(origin_utm32,dtype=float)
        if (triangles.ndim!=3 or triangles.shape[1:]!=(3,3) or not len(triangles)
                or not np.isfinite(triangles).all() or origin.shape!=(2,)
                or not np.isfinite(origin).all() or not np.isfinite(height_origin_m)):
            raise ValueError('Invalid exported terrain geometry or coordinate origin')
        self.origin=origin.copy();self.height_origin=float(height_origin_m)
        horizontal=triangles[:,:,[0,2]]
        v1=horizontal[:,1]-horizontal[:,0];v2=horizontal[:,2]-horizontal[:,0]
        determinant=v1[:,0]*v2[:,1]-v1[:,1]*v2[:,0]
        keep=np.abs(determinant)>1e-10
        if not keep.any():
            raise ValueError('Exported terrain has no usable horizontal triangles')
        self.triangles=triangles[keep]
        self.horizontal=horizontal[keep]
        self.v1=v1[keep];self.v2=v2[keep];self.determinant=determinant[keep]
        # Index only triangle bounds. The barycentric test below uses the
        # original triangles, including their exact diagonal choices.
        self.index=STRtree(polygons(self.horizontal))

    @classmethod
    def from_scene(cls, scene, origin_utm32, height_origin_m):
        pieces=[]
        for mesh in scene.get('meshes',[]):
            if not mesh.get('name','').startswith('1GRASS_TERRAIN'):
                continue
            vertices=np.asarray(mesh.get('vertices'),dtype=float)
            faces=np.asarray(mesh.get('faces'),dtype=np.int64)
            if (vertices.ndim!=2 or vertices.shape[1]!=3 or faces.ndim!=2
                    or faces.shape[1]!=3 or np.any(faces<0) or np.any(faces>=len(vertices))):
                raise ValueError('Invalid exported terrain mesh indices')
            pieces.append(vertices[faces])
        if not pieces:
            raise ValueError('Scene has no exported terrain meshes')
        return cls(np.concatenate(pieces),origin_utm32,height_origin_m)

    def __call__(self, points_utm):
        xy=np.asarray(points_utm,dtype=float)
        if xy.ndim!=2 or xy.shape[1]!=2 or not np.isfinite(xy).all():
            raise ValueError('Expected finite terrain sample points[n,2]')
        if not len(xy):
            return np.empty(0,dtype=float)
        local=np.c_[xy[:,0]-self.origin[0],self.origin[1]-xy[:,1]]
        output=np.full(len(xy),np.nan)
        for start in range(0,len(local),24000):
            q=local[start:start+24000]
            pairs=self.index.query(geometry_points(q))
            if not pairs.size:
                continue
            point_index,triangle_index=pairs
            delta=q[point_index]-self.horizontal[triangle_index,0]
            a=self.v1[triangle_index];b=self.v2[triangle_index]
            det=self.determinant[triangle_index]
            wa=(delta[:,0]*b[:,1]-delta[:,1]*b[:,0])/det
            wb=(a[:,0]*delta[:,1]-a[:,1]*delta[:,0])/det
            inside=(wa>=-1e-8)&(wb>=-1e-8)&(wa+wb<=1+1e-8)
            point_index=point_index[inside];triangle_index=triangle_index[inside]
            wa=wa[inside];wb=wb[inside]
            if not len(point_index):
                continue
            # Either adjacent face agrees at a shared edge. Choose a stable
            # first triangle to avoid dependence on STRtree traversal order.
            order=np.lexsort((triangle_index,point_index))
            _,first=np.unique(point_index[order],return_index=True)
            selected=order[first]
            point_index=point_index[selected];triangle_index=triangle_index[selected]
            wa=wa[selected];wb=wb[selected]
            height=self.triangles[triangle_index,:,1]
            output[start+point_index]=height[:,0]*(1-wa-wb)+height[:,1]*wa+height[:,2]*wb+self.height_origin
        missing=~np.isfinite(output)
        if missing.any():
            raise ValueError(f'{int(missing.sum())} terrain sample points lie outside the exported mesh')
        return output
