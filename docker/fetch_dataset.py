"""Verify and unpack the pinned public dataset, at image-build time only."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def extract_verified(archive, destination, manifest):
    with archive.open('rb') as stream:
        digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != manifest['sha256'] or archive.stat().st_size != manifest['archive_bytes']:
        raise ValueError('Dataset checksum or archive size mismatch')
    destination = destination.resolve()
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Dataset destination must be empty; existing data is never overwritten')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.dataset-', dir=destination.parent) as temporary:
        staging = Path(temporary)
        with tarfile.open(archive, 'r:gz') as stream:
            members = stream.getmembers()
            seen = set()
            total = 0
            if len(members) != manifest['file_count']:
                raise ValueError('Dataset file count mismatch')
            for member in members:
                path = PurePosixPath(member.name)
                if (not member.isfile() or path.is_absolute() or '\\' in member.name
                        or '..' in path.parts or not path.parts or path.parts[0] != 'altenhain'
                        or member.name in seen or ':' in member.name
                        or any(part in ('', '.') for part in member.name.split('/'))):
                    raise ValueError('Unsafe or duplicate dataset archive member')
                seen.add(member.name)
                total += member.size
                if total > manifest['unpacked_bytes']:
                    raise ValueError('Dataset exceeds its expected unpacked size')
            if total != manifest['unpacked_bytes']:
                raise ValueError('Dataset unpacked size mismatch')
            # Every member was checked before writing any file. No links or modes
            # from the source archive are trusted.
            for member in members:
                target = staging.joinpath(*PurePosixPath(member.name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                with stream.extractfile(member) as source, target.open('xb') as output:
                    shutil.copyfileobj(source, output)
                target.chmod(0o644)
        destination.mkdir(exist_ok=True)
        (staging / 'altenhain').replace(destination / 'altenhain')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path(__file__).with_name('dataset.json'))
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--url', default='')
    parser.add_argument('--archive', type=Path, help='Use an already downloaded archive, with the same checksum verification')
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text(encoding='utf-8'))
    if args.archive:
        extract_verified(args.archive, args.destination, manifest)
    else:
        url = args.url or manifest['url']
        parsed = urlsplit(url)
        if parsed.scheme not in ('https', 'http') or parsed.username or parsed.password:
            raise ValueError('Dataset URL must be HTTP(S), without embedded credentials')
        with tempfile.TemporaryDirectory(prefix='roadtoassetto-download-') as temporary:
            archive = Path(temporary) / 'dataset.tar.gz'
            request = Request(url, headers={'User-Agent': 'RoadToAssetto-Dataset/1'})
            with urlopen(request, timeout=120) as response, archive.open('wb') as stream:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > manifest['archive_bytes']:
                        raise ValueError('Download exceeds pinned archive size')
                    stream.write(chunk)
            extract_verified(archive, args.destination, manifest)
    print(f"Verified dataset {manifest['id']} {manifest['version']}: {manifest['file_count']} files")


if __name__ == '__main__':
    main()
