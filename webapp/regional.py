"""One regional OSM download, streamed into complete small route extracts.

Large routes must not depend on dozens of public Overpass requests. Inputs are
the operator's fixed Geofabrik source and server-generated section rectangles.
Only selected objects (without contributor metadata) enter exported archives.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
from xml.etree import ElementTree as ET

import requests
from shapely import STRtree
from shapely.geometry import box, LineString, Point

from webapp.geodata import GeoError, USER_AGENT, CACHE_SECONDS, _digest, _json_write, _wgs_bounds

SOURCE = 'https://download.geofabrik.de/europe/germany/hessen-latest.osm.pbf'
MAX_BYTES = 1024**3
EXTRACT_VERSION = 1


def regional_file(cache, progress):
    directory = Path(cache) / 'regional'
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'hessen-latest.osm.pbf'
    manifest = path.with_suffix('.json')
    if path.is_file() and manifest.is_file() and time.time() - manifest.stat().st_mtime < CACHE_SECONDS:
        saved = json.loads(manifest.read_text())
        if saved.get('sha256') == _digest(path) and saved.get('provider') == SOURCE:
            return path, saved
    progress('Hessen-OSM laden (einmalig etwa 330 MB, anschließend im Cache)', 3)
    temporary = path.with_suffix('.download')
    started = last = time.monotonic()
    sha = hashlib.sha256(); size = 0
    try:
        with requests.get(SOURCE, headers={'User-Agent': USER_AGENT}, stream=True, timeout=(20, 60)) as response:
            response.raise_for_status()
            expected = int(response.headers.get('Content-Length', '0'))
            if expected > MAX_BYTES:
                raise ValueError('Regionaldatei überschreitet das Größenlimit.')
            with temporary.open('wb') as stream:
                for chunk in response.iter_content(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_BYTES or time.monotonic() - started > 900:
                        raise ValueError('Regionaldownload überschreitet das Größen- oder Zeitlimit.')
                    stream.write(chunk); sha.update(chunk)
                    if time.monotonic() - last > 8:
                        progress(f'Hessen-OSM laden: {size / 1024**2:.0f} MiB', 3)
                        last = time.monotonic()
            if size < 1024 or (expected and size != expected):
                raise ValueError('Regionaldownload ist unvollständig.')
        import osmium
        with osmium.io.Reader(osmium.io.File(str(temporary), 'pbf'), osmium.osm.NOTHING) as reader:
            stamp = reader.header().get('osmosis_replication_timestamp')
        saved = {'provider': SOURCE, 'bytes': size, 'sha256': sha.hexdigest(),
                 'data_timestamp': stamp, 'downloaded_at_utc': datetime.now(timezone.utc).isoformat()}
        temporary.replace(path)
        _json_write(manifest, saved)
        return path, saved
    except (requests.RequestException, OSError, RuntimeError, ValueError) as error:
        temporary.unlink(missing_ok=True)
        raise GeoError('Der regionale Hessen-OSM-Datensatz konnte nicht geladen werden. Ein erneuter Versuch verwendet bereits fertige Abschnittsdaten.', 'regional_unavailable', 503) from error


def extract_windows(path, bounds, progress):
    """Keep complete ways crossing each rectangle, including outside endpoints."""
    import osmium
    rectangles = [box(*b) for b in bounds]
    tree = STRtree(rectangles)
    overall = box(min(b[0] for b in bounds), min(b[1] for b in bounds),
                  max(b[2] for b in bounds), max(b[3] for b in bounds))
    west, south, east, north = overall.bounds
    nodes = [{} for _ in bounds]; ways = [{} for _ in bounds]

    class Extract(osmium.SimpleHandler):
        def __init__(self):
            super().__init__(); self.last = time.monotonic(); self.count = 0

        def node(self, node):
            if not (node.tags.get('natural') == 'tree' or 'barrier' in node.tags or 'access' in node.tags):
                return
            lon, lat = node.location.lon, node.location.lat
            if not west <= lon <= east or not south <= lat <= north:
                return
            tags = dict(node.tags)
            for index in tree.query(Point(lon, lat), predicate='intersects'):
                nodes[index][node.id] = (lon, lat, tags)

        def way(self, way):
            self.count += 1
            if time.monotonic() - self.last > 8:
                progress(f'Hessen-OSM auf {len(bounds)} Abschnitte zuschneiden ({self.count:,} Wege geprüft)', 4)
                self.last = time.monotonic()
            if not any(key in way.tags for key in ('highway', 'building', 'natural', 'landuse', 'barrier')):
                return
            coords = [(n.ref, n.lon, n.lat) for n in way.nodes if n.location.valid()]
            if len(coords) < 2 or len(coords) != len(way.nodes):
                return
            if (max(n[1] for n in coords) < west or min(n[1] for n in coords) > east or
                    max(n[2] for n in coords) < south or min(n[2] for n in coords) > north):
                return
            shape = LineString([(n[1], n[2]) for n in coords])
            # Closed landuse polygons can surround a window without an edge
            # crossing it. Their full ring is required by the scenery engine.
            if coords[0][0] == coords[-1][0] and len(coords) >= 4:
                from shapely.geometry import Polygon
                shape = Polygon(shape).buffer(0)
            matches = tree.query(shape, predicate='intersects')
            if not len(matches):
                return
            copied = ([n[0] for n in coords], dict(way.tags))
            for index in matches:
                ways[index][way.id] = copied
                for identifier, lon, lat in coords:
                    nodes[index].setdefault(identifier, (lon, lat, {}))

    Extract().apply_file(str(path), locations=True, idx='flex_mem',
        filters=[osmium.filter.KeyFilter('highway', 'building', 'natural', 'landuse', 'barrier', 'access')])
    result = []
    for index in range(len(bounds)):
        if not ways[index]:
            raise GeoError(f'Abschnitt {index + 1} liegt außerhalb des regionalen OSM-Datensatzes.', 'regional_coverage', 422)
        document = ET.Element('osm', version='0.6', generator='RoadToAssetto regional extractor')
        ET.SubElement(document, 'bounds', minlon=str(bounds[index][0]), minlat=str(bounds[index][1]),
                      maxlon=str(bounds[index][2]), maxlat=str(bounds[index][3]))
        for identifier, (lon, lat, tags) in sorted(nodes[index].items()):
            element = ET.SubElement(document, 'node', id=str(identifier), lon=str(lon), lat=str(lat))
            for key, value in sorted(tags.items()):
                ET.SubElement(element, 'tag', k=key, v=value)
        for identifier, (references, tags) in sorted(ways[index].items()):
            element = ET.SubElement(document, 'way', id=str(identifier))
            for reference in references:
                ET.SubElement(element, 'nd', ref=str(reference))
            for key, value in sorted(tags.items()):
                ET.SubElement(element, 'tag', k=key, v=value)
        content = ET.tostring(document, encoding='utf-8', xml_declaration=True)
        if len(content) > 32_000_000:
            raise GeoError('Ein regionaler OSM-Abschnitt ist zu groß.', 'regional_extract_size', 422)
        result.append(content)
    return result


def prepare_regional(cache, sections, progress):
    mode = os.environ.get('ROADTOASSETTO_OSM_MODE', 'auto').lower()
    if mode not in ('auto', 'regional', 'overpass'):
        raise GeoError('ROADTOASSETTO_OSM_MODE muss auto, regional oder overpass sein.', 'regional_configuration', 500)
    if mode == 'overpass' or (mode == 'auto' and len(sections) < 10):
        return None
    path, source = regional_file(cache, progress)
    bounds = [_wgs_bounds(section['bounds']) for section in sections]
    keys = [hashlib.sha256(json.dumps([EXTRACT_VERSION, source['sha256'], b]).encode()).hexdigest() for b in bounds]
    directory = Path(cache) / 'osm'; directory.mkdir(parents=True, exist_ok=True)
    result = []
    for key in keys:
        content_path = directory / (key + '.osm'); record_path = directory / (key + '.json')
        if not content_path.is_file() or not record_path.is_file():
            break
        saved = json.loads(record_path.read_text())
        content = content_path.read_bytes()
        if saved.get('sha256') != hashlib.sha256(content).hexdigest():
            break
        result.append((key, content))
    if len(result) == len(sections):
        return result
    progress('Regionalen Hessen-Datensatz auf den Streckenkorridor zuschneiden', 4)
    try:
        extracts = extract_windows(path, bounds, progress)
    except (RuntimeError, OSError) as error:
        raise GeoError('Der regionale OSM-Datensatz konnte nicht verarbeitet werden.', 'regional_invalid', 502) from error
    result = []
    for key, content, b, section in zip(keys, extracts, bounds, sections):
        target = directory / (key + '.osm')
        temporary = target.with_suffix('.tmp'); temporary.write_bytes(content); temporary.replace(target)
        _json_write(target.with_suffix('.json'), {'key': key, 'bounds': b, 'bounds_utm': section['bounds'],
            'provider': SOURCE, 'sha256': hashlib.sha256(content).hexdigest(), 'regional_source': source})
        result.append((key, content))
    return result
