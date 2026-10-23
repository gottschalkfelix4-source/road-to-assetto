"""Validated planar photo projection with explicit, reviewable control points.

The source photograph is kept intact. Coordinates use pixel centres measured
from its upper left; source points are metric coordinates in a recorded facade
frame. A low numerical residual does not establish a surveyed correspondence.
"""
from __future__ import annotations

import numpy as np


def fit_homography(source, target):
    """Return a planar homography and individual target-coordinate residuals."""
    source=np.asarray(source,dtype=float);target=np.asarray(target,dtype=float)
    if source.ndim!=2 or source.shape[1]!=2 or target.shape!=source.shape or len(source)<4:
        raise ValueError('Photo projection requires at least four paired 2-D control points')
    if not np.isfinite(source).all() or not np.isfinite(target).all():
        raise ValueError('Photo projection control points must be finite')
    # Hartley normalisation prevents UTM-sized or high-resolution coordinates
    # from making an otherwise sound camera fit numerically unstable.
    def normalise(points):
        center=points.mean(axis=0);distance=np.linalg.norm(points-center,axis=1).mean()
        if distance<1e-10:
            raise ValueError('Degenerate photo projection control points')
        scale=np.sqrt(2)/distance
        transform=np.array([[scale,0,-scale*center[0]],[0,scale,-scale*center[1]],[0,0,1.]])
        return (points-center)*scale,transform
    a,ta=normalise(source);b,tb=normalise(target)
    rows=[]
    for (x,y),(u,v) in zip(a,b):
        rows.extend([[-x,-y,-1,0,0,0,u*x,u*y,u],
                     [0,0,0,-x,-y,-1,v*x,v*y,v]])
    design=np.asarray(rows)
    if np.linalg.matrix_rank(design)<8:
        raise ValueError('Degenerate photo projection control points')
    _,_,vh=np.linalg.svd(design)
    matrix=np.linalg.inv(tb)@vh[-1].reshape(3,3)@ta
    if abs(matrix[2,2])<1e-12:
        raise ValueError('Invalid photo projection homography')
    matrix/=matrix[2,2]
    predicted=project_points(source,matrix)
    return matrix,np.linalg.norm(predicted-target,axis=1)


def project_points(points, matrix):
    points=np.asarray(points,dtype=float);matrix=np.asarray(matrix,dtype=float)
    if points.ndim!=2 or points.shape[1]!=2 or matrix.shape!=(3,3):
        raise ValueError('Invalid photo projection dimensions')
    homogeneous=np.c_[points,np.ones(len(points))]@matrix.T
    if (not np.isfinite(homogeneous).all() or np.any(np.abs(homogeneous[:,2])<1e-10)
            or (np.any(homogeneous[:,2]<0) and np.any(homogeneous[:,2]>0))):
        raise ValueError('Photo projection crosses a camera singularity')
    return homogeneous[:,:2]/homogeneous[:,2,None]


def projection_frame(spec):
    if spec.get('type')!='planar-homography-v1':
        raise ValueError('Unsupported facade photo projection')
    origin=np.asarray(spec.get('origin_utm32_height'),dtype=float)
    u=np.asarray(spec.get('axis_u'),dtype=float);v=np.asarray(spec.get('axis_v'),dtype=float)
    if any(a.shape!=(3,) or not np.isfinite(a).all() for a in (origin,u,v)):
        raise ValueError('Invalid facade photo coordinate frame')
    if min(np.linalg.norm(u),np.linalg.norm(v))<1e-8:
        raise ValueError('Invalid facade photo coordinate frame')
    u=u/np.linalg.norm(u);v=v/np.linalg.norm(v)
    if abs(float(u@v))>1e-6:
        raise ValueError('Facade photo axes must be orthogonal')
    return origin,u,v,np.cross(u,v)


def projected_photo_uv(vertices, override):
    """Map surveyed vertices into one shared photo and reject untrusted fits.

    Use one identical projection specification for adjacent wall surfaces.
    Long rendering triangles still need tessellation for a perspective map;
    this function provides exact projected coordinates at their vertices.
    """
    spec=override.get('projection')
    if not isinstance(spec,dict):
        raise ValueError('Missing facade photo projection')
    origin,u,v,n=projection_frame(spec)
    vertices=np.asarray(vertices,dtype=float)
    if vertices.ndim!=2 or vertices.shape[1]!=3 or not np.isfinite(vertices).all():
        raise ValueError('Invalid facade photo vertices')
    relative=vertices-origin
    tolerance=float(spec.get('max_plane_distance_m',.03))
    if not np.isfinite(tolerance) or not 0<=tolerance<=.1:
        raise ValueError('Invalid facade photo plane tolerance')
    if np.any(np.abs(relative@n)>tolerance):
        raise ValueError('Facade vertices do not lie on the calibrated photo plane')
    size=np.asarray(spec.get('image_size'),dtype=float)
    if size.shape!=(2,) or not np.isfinite(size).all() or np.any(size<2):
        raise ValueError('Invalid facade photo image dimensions')
    target=np.asarray(spec.get('image_points_px'),dtype=float)
    if target.ndim!=2 or target.shape[1]!=2 or np.any(target<0) or np.any(target>size):
        raise ValueError('Facade photo control points leave the source image')
    matrix,residuals=fit_homography(spec.get('source_points_m'),target)
    allowed=float(spec.get('max_reprojection_error_px',3.))
    if not np.isfinite(allowed) or not 0<=allowed<=10:
        raise ValueError('Invalid facade photo reprojection tolerance')
    if float(residuals.max())>allowed:
        raise ValueError(f'Facade photo control points disagree by {float(residuals.max()):.2f} pixels')
    projected=project_points(np.c_[relative@u,relative@v],matrix)
    uv=projected/size
    if np.any(uv < -1e-6) or np.any(uv > 1+1e-6):
        raise ValueError('Facade photo projection leaves the source image')
    return np.clip(uv,0,1)
