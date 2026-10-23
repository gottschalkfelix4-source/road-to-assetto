"""Create the deterministic, source-only Altenhain dataset used by Docker builds.

The archive is published as a GitHub release asset, not committed to Git. User
jobs, API settings, generated meshes and local tools are intentionally excluded.
"""
import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import tarfile

ROOT = Path(__file__).resolve().parents[1]
FILES = (
    'road_ways.json', 'road_alignment.json', 'marking_observations.json',
    'junction_observations.json', 'bounds.json', 'area.osm', 'dgm1.tif',
    'dom1.tif', 'dom1_source.json', 'ortho.tif', 'buildings_lod2.gml',
    'facade_overrides.json', 'visual_sources.json', 'retrieval.json',
    'textures/ortho.jpg',
)
DIRECTORIES = (
    'materials/PaintedPlaster006', 'materials/surface_photos',
    'materials/PolyHavenRoofs', 'materials/vegetation_photos',
    'materials/AIWindows', 'materials/AIDoors', 'photos',
)
CATALOG_KEYS = (
    'id', 'name', 'version', 'length_m', 'elevation_min_m', 'elevation_max_m',
    'buildings', 'sources', 'limitations', 'estimated_properties',
)


def selected_files(source):
    result = {name: source / name for name in FILES}
    for name in DIRECTORIES:
        directory = source / name
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError(f'Missing or linked source directory: {name}')
        for path in sorted(directory.rglob('*')):
            if path.is_symlink():
                raise ValueError(f'Linked source file: {path.name}')
            if path.is_file():
                result[path.relative_to(source).as_posix()] = path
    for name, path in result.items():
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing or linked source file: {name}')
        if path.suffix.lower() not in ('.json', '.txt', '.jpg', '.png', '.tif', '.gml', '.osm'):
            raise ValueError(f'Unexpected source file type: {name}')
        if path.suffix.lower() in ('.json', '.txt'):
            content = path.read_text(encoding='utf-8')
            # Fail with the filename only, never reproduce a credential in logs.
            if re.search(r'(?i)(?:[a-z]:[\\/](?:Users|Ai Projekte)|sk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})', content):
                raise ValueError(f'Private path or credential-like value in {name}; review before publishing')
    return result


def add_bytes(archive, name, content):
    info = tarfile.TarInfo('altenhain/' + name)
    info.size = len(content)
    info.mode = 0o644
    info.mtime = 0
    archive.addfile(info, io.BytesIO(content))


def build(source, output, manifest, url, version):
    files = selected_files(source)
    original = json.loads((source / 'manifest.json').read_text(encoding='utf-8'))
    catalog = {key: original[key] for key in CATALOG_KEYS}
    if catalog['version'] != version:
        raise ValueError('Dataset version differs from the prepared source manifest')
    catalog_bytes = (json.dumps(catalog, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    notice = (ROOT / 'THIRD_PARTY.md').read_bytes()
    output.parent.mkdir(parents=True, exist_ok=True)
    total = len(catalog_bytes) + len(notice) + sum(path.stat().st_size for path in files.values())
    with output.open('wb') as stream:
        with gzip.GzipFile(filename='', mode='wb', fileobj=stream, mtime=0, compresslevel=6) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.USTAR_FORMAT) as archive:
                for name, path in sorted(files.items()):
                    info = tarfile.TarInfo('altenhain/' + name)
                    info.size = path.stat().st_size
                    info.mode = 0o644
                    info.mtime = 0
                    with path.open('rb') as content:
                        archive.addfile(info, content)
                add_bytes(archive, 'manifest.json', catalog_bytes)
                add_bytes(archive, 'THIRD_PARTY.md', notice)
    with output.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    result = {'schema_version': 1, 'id': 'altenhain', 'version': version,
              'url': url, 'sha256': digest, 'archive_bytes': output.stat().st_size,
              'unpacked_bytes': total, 'file_count': len(files) + 2,
              'description': 'Open geographic inputs and attributed materials; no generated jobs or user settings. See altenhain/THIRD_PARTY.md inside the archive.'}
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    output.with_suffix(output.suffix + '.sha256').write_text(f'{digest}  {output.name}\n', encoding='ascii')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=ROOT / 'data/altenhain')
    parser.add_argument('--output', type=Path, default=ROOT / 'dist/altenhain-v0.11.0.tar.gz')
    parser.add_argument('--manifest', type=Path, default=ROOT / 'docker/dataset.json')
    parser.add_argument('--version', default='0.11.0')
    parser.add_argument('--url', default='https://github.com/gottschalkfelix4-source/road-to-assetto/releases/download/data-v0.11.0/altenhain-v0.11.0.tar.gz')
    args = parser.parse_args()
    print(json.dumps(build(args.source, args.output, args.manifest, args.url, args.version), indent=2))
