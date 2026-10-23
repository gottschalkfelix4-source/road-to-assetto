"""Review the Langstrasse 13 photo; never register an inconsistent camera fit.

The source JPG and active facade overrides remain untouched. This review is
deliberately not an automatic facade recogniser or a surveyed camera solution.
"""
from pathlib import Path
import hashlib
import json
import sys
import numpy as np
from PIL import Image,ImageDraw

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from roadtoassetto.photo_projection import fit_homography,project_points


def main():
    folder=ROOT/'research/local_facades'
    image_path=folder/'Altenhain_Langstrasse_13_Vorderbau.JPG'
    before=hashlib.sha256(image_path.read_bytes()).hexdigest()
    photo=Image.open(image_path)
    source=np.array([[0,4.501],[8.177,4.714],[0,0],[8.177,0],[4.123,10.024]])
    # Coordinates were read from a 1927 x 1279 observation rendering. The
    # right-hand base is partly obscured by a foreground garden wall; the
    # visible upper gable apex lies below a deep roof overhang.
    observed=np.array([[180,704],[399,625],[169,1066],[425,1114],[315,303]],float)
    scale=np.array(photo.size)/np.array([1927,1279])
    image_points=observed*scale
    matrix,residuals=fit_homography(source,image_points)
    predicted=project_points(source,matrix)
    four_matrix,_=fit_homography(source[:4],image_points[:4])
    ridge_prediction=project_points(source[4:],four_matrix)[0]
    safe=bool(residuals.max()<=3)
    report={
        'address':'Langstrasse 13, Altenhain','building_id':'Building_DEHE062000009jcl',
        'surface_ids':['GEOMETRY_a4a05971-3013-4311-9fb3-947520ddf6ae',
                       'GEOMETRY_d77514fc-2ba8-4e15-be54-dc8e752b9fd7'],
        'photo':str(image_path.relative_to(ROOT)),'photo_sha256':before,
        'source':'https://commons.wikimedia.org/wiki/File:Altenhain,_Langstra%C3%9Fe_13,_Vorderbau.JPG',
        'author':'Karsten Ratzke','captured':'2012-01-15','license':'CC0',
        'control_labels':['east_eave','west_eave','east_base','west_base_partly_occluded','visible_gable_apex'],
        'source_points_m':source.tolist(),'image_points_px':image_points.tolist(),
        'fit_residuals_px':residuals.tolist(),'maximum_residual_px':float(residuals.max()),
        'four_lower_points_predicted_apex_px':ridge_prediction.tolist(),
        'observed_apex_px':image_points[4].tolist(),
        'apex_mismatch_px':float(np.linalg.norm(ridge_prediction-image_points[4])),
        'registered':False,
        'reason':'No activation: one planar projection cannot reconcile the observed gable apex with the lower facade corners. A partly occluded lower corner, deep roof overhang and approximate LoD2 ridge make calibration uncertain. The original Langstrasse 39 override is preserved.',
        'possible_alternative':'A building-specific inferred material treatment could use masonry below the observed timber gable, without claiming a photographic reconstruction. Not applied: detailed historic timber beam placement would need separate geometry and visual review.',
        'numerical_fit_within_three_pixels':safe,
        'image_unchanged':hashlib.sha256(image_path.read_bytes()).hexdigest()==before,
    }
    (folder/'photo13-review.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    # Diagnostic observation only: source remains intact and this graphic is
    # never copied into game textures or shown as a finished model screenshot.
    display=photo.copy();display.thumbnail((1927,1279))
    draw=ImageDraw.Draw(display)
    for index,(actual,estimate) in enumerate(zip(observed,predicted/scale)):
        a=tuple(actual);b=tuple(estimate)
        draw.line((a,b),fill=(255,30,30),width=4)
        draw.ellipse((a[0]-5,a[1]-5,a[0]+5,a[1]+5),fill=(20,255,40))
        draw.ellipse((b[0]-5,b[1]-5,b[0]+5,b[1]+5),fill=(255,20,20))
        draw.text((a[0]+8,a[1]),report['control_labels'][index],fill=(255,30,30))
    output=ROOT/'test-results/facade13-calibration-review.png'
    display.save(output)
    print(json.dumps({'registered':False,'maximum_residual_px':report['maximum_residual_px'],
                      'apex_mismatch_px':report['apex_mismatch_px'],'report':str(folder/'photo13-review.json')},indent=2))


if __name__=='__main__':
    main()
