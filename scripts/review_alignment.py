"""Reproduce the native-DOP visual audit of the reviewed road reference."""
from pathlib import Path
import json
import sys

import numpy as np
from PIL import Image, ImageDraw
from pyproj import Transformer
import rasterio
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import Polygon

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from roadtoassetto.prepare import ordered_road
from roadtoassetto.road_alignment import load_alignment, widths_at


def main():
    data = ROOT / 'data/altenhain'
    ll = ordered_road(json.loads((data / 'road_ways.json').read_text(encoding='utf-8')))
    transform = Transformer.from_crs(4326, 25832, always_xy=True)
    original = np.array(transform.transform(ll[:, 0], ll[:, 1])).T
    corrected, meta = load_alignment(data, original)
    # Same recommended smoothing as production; exact endpoints retained.
    xy = gaussian_filter1d(corrected, .8, axis=0, mode='nearest')
    xy[[0, -1]] = corrected[[0, -1]]
    tangent = np.gradient(xy, axis=0)
    tangent /= np.linalg.norm(tangent, axis=1)[:, None]
    left = np.c_[-tangent[:, 1], tangent[:, 0]]
    widths = widths_at(xy, meta)
    edge_a = xy + left * widths[:, None] / 2
    edge_b = xy - left * widths[:, None] / 2
    valid = Polygon(np.vstack([edge_a, edge_b[::-1]])).is_valid
    with rasterio.open(data / 'ortho.tif') as ds:
        raster = Image.fromarray(ds.read([1, 2, 3]).transpose(1, 2, 0))
        inv = ~ds.transform
    def pixels(points):
        return np.c_[inv.a * points[:, 0] + inv.c,
                     inv.e * points[:, 1] + inv.f]
    paths = [pixels(a) for a in (original, xy, edge_a, edge_b)]
    for name, bounds, scale in [
        ('overview', (670, 675, 3920, 2578), .7),
        ('junction', (2090, 1790, 2370, 1960), 3.),
        ('widths', (2620, 1960, 3150, 2235), 2.),
    ]:
        image = raster.crop(bounds)
        image = image.resize((int(image.width * scale), int(image.height * scale)), Image.Resampling.LANCZOS)
        draw = ImageDraw.Draw(image)
        for i, path in enumerate(paths):
            p = (path - np.array(bounds[:2])) * scale
            color = [(255, 55, 75), (20, 235, 245), (130, 250, 200), (130, 250, 200)][i]
            draw.line([tuple(v) for v in p], fill=color, width=max(1, round(scale * (1.2 if i > 1 else 1.8))))
        draw.rectangle((0, 0, image.width, 27), fill=(20, 25, 30))
        draw.text((8, 8), 'RED: original OSM | CYAN: reviewed driving path | GREEN: estimated asphalt edges (not surveyed)', fill='white')
        image.save(data / f'road_alignment_{name}.png')
    report = {k: v for k, v in meta.items() if k not in ('reference_xy_utm', 'width_profile')}
    report['geometry_checks'] = {'road_polygon_valid': bool(valid),
                                 'exact_endpoints_preserved': bool(np.array_equal(xy[[0, -1]], original[[0, -1]])),
                                 'minimum_width_m': float(widths.min()),
                                 'maximum_width_m': float(widths.max())}
    (data / 'road_alignment_review.json').write_text(json.dumps(report, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    print(json.dumps(report['geometry_checks'], indent=2))


if __name__ == '__main__':
    main()
