# SPDX-License-Identifier: AGPL-3.0-only
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from seed_cache import normalize_mtime, normalize_part_index_inputs


class SeedTimestampTest(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / 'parts' / 's').mkdir(parents=True)

    def file(self, name, stamp=1_700_000_000_999_999_999):
        path = self.root / name
        path.write_bytes(b'0 Public test part\n')
        path.chmod(0o640)
        os.utime(path, ns=(1_600_000_000_123_456_789, stamp))
        return path

    def test_exact_nanoseconds_are_floored_without_float_rounding(self):
        path = self.file('parts/a.dat')
        before = path.stat()
        normalize_mtime(path)
        after = path.stat()
        self.assertEqual(after.st_mtime_ns, 1_700_000_000_000_000_000)
        self.assertEqual(after.st_atime_ns, before.st_atime_ns)
        self.assertEqual(stat.S_IMODE(after.st_mode), stat.S_IMODE(before.st_mode))
        self.assertEqual(after.st_size, before.st_size)
        self.assertEqual(path.read_bytes(), b'0 Public test part\n')

    def test_normalization_is_idempotent(self):
        path = self.file('parts/a.dat')
        with mock.patch('seed_cache.os.utime', wraps=os.utime) as touch:
            normalize_mtime(path)
            normalize_mtime(path)
        self.assertEqual(touch.call_count, 1)

    def test_only_the_native_top_level_dat_signature_inputs_change(self):
        included = self.file('parts/a.dat')
        excluded = [self.file('parts/notes.txt'), self.file('parts/s/subpart.dat')]
        normalize_part_index_inputs(self.root)
        self.assertEqual(included.stat().st_mtime_ns, 1_700_000_000_000_000_000)
        self.assertTrue(all(path.stat().st_mtime_ns == 1_700_000_000_999_999_999 for path in excluded))

    def test_native_signature_survives_whole_second_archive_projection(self):
        self.file('parts/a.dat', 1_700_000_000_999_999_999)
        self.file('parts/b.dat', 1_700_000_001_123_456_789)
        files = sorted((self.root / 'parts').glob('*.dat'))

        def signature(projected):
            rows = []
            for path in files:
                metadata = path.stat()
                stamp = metadata.st_mtime_ns
                if projected:
                    stamp = stamp // 1_000_000_000 * 1_000_000_000
                rows.append(f'{path.name}:{metadata.st_size}:{stamp}')
            return hashlib.sha256('\n'.join(rows).encode()).hexdigest()

        self.assertNotEqual(signature(False), signature(True), 'fixture reproduces the archive mismatch')
        normalize_part_index_inputs(self.root)
        self.assertEqual(signature(False), signature(True))

    def test_build_normalization_does_not_follow_links(self):
        target = self.file('outside.dat')
        (self.root / 'parts' / 'alias.dat').symlink_to(target)
        with self.assertRaisesRegex(RuntimeError, 'ordinary files'):
            normalize_part_index_inputs(self.root)
        self.assertEqual(target.stat().st_mtime_ns, 1_700_000_000_999_999_999)


if __name__ == '__main__':
    unittest.main()
