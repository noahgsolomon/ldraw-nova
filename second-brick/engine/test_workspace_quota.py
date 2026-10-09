# SPDX-License-Identifier: AGPL-3.0-only
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import server


class WorkspaceQuotaTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.limit = mock.patch.object(server, 'MAX_FILES', 2)
        self.limit.start()
        self.addCleanup(self.limit.stop)
        self.addCleanup(self.temp.cleanup)

    def paths(self):
        return sorted(path.relative_to(self.root).as_posix() for path in self.root.rglob('*'))

    def test_nested_ancestors_are_refused_before_any_directory_is_created(self):
        with self.assertRaisesRegex(server.Refused, 'entry quota') as refused:
            server.put_file(self.root, 'a/b/c/d/file.txt', b'x')
        self.assertEqual(refused.exception.status, 413)
        self.assertEqual(self.paths(), [])

    def test_directory_and_file_entries_share_the_boundary(self):
        server.put_file(self.root, 'a/b/c/file.txt', b'x')
        self.assertEqual(len(self.paths()), 4)
        before = self.paths()
        with self.assertRaisesRegex(server.Refused, 'entry quota'):
            server.put_file(self.root, 'other/new.txt', b'y')
        self.assertEqual(self.paths(), before)

    def test_inventory_counts_empty_directories(self):
        for index in range(5):
            (self.root / str(index)).mkdir()
        with self.assertRaisesRegex(server.Refused, 'entry quota'):
            server.inventory(self.root)

    def test_rewriting_existing_file_at_entry_boundary_remains_allowed(self):
        server.put_file(self.root, 'a/b/c/file.txt', b'x')
        before = self.paths()
        server.put_file(self.root, 'a/b/c/file.txt', b'updated')
        self.assertEqual(self.paths(), before)
        self.assertEqual(server.inventory(self.root), ([('a/b/c/file.txt', 7)], 7))

    def test_file_count_refusal_does_not_create_ancestors(self):
        server.put_file(self.root, 'first.txt', b'x')
        server.put_file(self.root, 'second.txt', b'y')
        before = self.paths()
        with self.assertRaisesRegex(server.Refused, 'quota'):
            server.put_file(self.root, 'new/third.txt', b'z')
        self.assertEqual(self.paths(), before)

    def test_byte_quota_refusal_does_not_create_ancestors(self):
        with mock.patch.object(server, 'MAX_WORKSPACE', 1):
            with self.assertRaisesRegex(server.Refused, 'quota'):
                server.put_file(self.root, 'new/deep/file.txt', b'xx')
        self.assertEqual(self.paths(), [])

    def test_failed_publication_rolls_back_only_new_empty_directories(self):
        (self.root / 'existing').mkdir()
        with mock.patch.object(server.os, 'replace', side_effect=OSError('publication failed')):
            with self.assertRaisesRegex(OSError, 'publication failed'):
                server.put_file(self.root, 'existing/new/file.txt', b'x')
        self.assertEqual(self.paths(), ['existing'])

    def test_failed_mkdir_rolls_back_previously_created_ancestors(self):
        mkdir = Path.mkdir

        def fail_second(path, *args, **kwargs):
            if path.name == 'second':
                raise OSError('directory creation failed')
            return mkdir(path, *args, **kwargs)

        with mock.patch.object(Path, 'mkdir', new=fail_second):
            with self.assertRaisesRegex(OSError, 'directory creation failed'):
                server.put_file(self.root, 'first/second/file.txt', b'x')
        self.assertEqual(self.paths(), [])


if __name__ == '__main__':
    unittest.main()
