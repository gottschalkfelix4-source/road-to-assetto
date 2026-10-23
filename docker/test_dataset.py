"""The downloaded release archive must never escape or overwrite its data root."""
import hashlib
import io
from pathlib import Path
import tarfile
import tempfile
import unittest

from fetch_dataset import extract_verified


class DatasetExtractionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.archive = self.root / 'dataset.tar.gz'
        self.destination = self.root / 'data'

    def pack(self, names=('altenhain/bounds.json',), symlink=False):
        with tarfile.open(self.archive, 'w:gz') as stream:
            for name in names:
                item = tarfile.TarInfo(name)
                if symlink:
                    item.type = tarfile.SYMTYPE
                    item.linkname = '../../outside'
                    stream.addfile(item)
                else:
                    item.size = 2
                    stream.addfile(item, io.BytesIO(b'[]'))
        content = self.archive.read_bytes()
        return {'sha256': hashlib.sha256(content).hexdigest(),
                'archive_bytes': len(content), 'file_count': len(names),
                'unpacked_bytes': 0 if symlink else len(names) * 2}

    def test_verified_regular_files_are_extracted(self):
        manifest = self.pack()
        extract_verified(self.archive, self.destination, manifest)
        self.assertEqual((self.destination / 'altenhain/bounds.json').read_bytes(), b'[]')

    def test_checksum_mismatch_does_not_create_destination(self):
        manifest = self.pack()
        manifest['sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'checksum'):
            extract_verified(self.archive, self.destination, manifest)
        self.assertFalse(self.destination.exists())

    def test_existing_data_is_preserved(self):
        manifest = self.pack()
        self.destination.mkdir()
        existing = self.destination / 'keep.txt'
        existing.write_text('keep')
        with self.assertRaisesRegex(ValueError, 'empty'):
            extract_verified(self.archive, self.destination, manifest)
        self.assertEqual(existing.read_text(), 'keep')

    def test_archive_cannot_escape_root_or_create_links(self):
        for name in ('../outside', 'altenhain/../../outside', '/altenhain/absolute',
                     'other/file', 'altenhain\\outside', 'altenhain/C:/outside',
                     'altenhain/./file', 'altenhain//file'):
            with self.subTest(name=name):
                manifest = self.pack((name,))
                with self.assertRaisesRegex(ValueError, 'Unsafe'):
                    extract_verified(self.archive, self.destination, manifest)
                self.assertFalse(self.destination.exists())
        manifest = self.pack(symlink=True)
        with self.assertRaisesRegex(ValueError, 'Unsafe'):
            extract_verified(self.archive, self.destination, manifest)

    def test_duplicate_members_rejected_before_writing(self):
        manifest = self.pack(('altenhain/bounds.json', 'altenhain/bounds.json'))
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            extract_verified(self.archive, self.destination, manifest)
        self.assertFalse(self.destination.exists())

    def test_pinned_size_and_file_count_are_enforced(self):
        for key in ('archive_bytes', 'unpacked_bytes', 'file_count'):
            with self.subTest(key=key):
                manifest = self.pack()
                manifest[key] += 1
                with self.assertRaises(ValueError):
                    extract_verified(self.archive, self.destination, manifest)
                self.assertFalse(self.destination.exists())


if __name__ == '__main__':
    unittest.main()
