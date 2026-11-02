"""Durability boundaries of the shared state-file replacement primitive."""
import errno
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from unifier.atomic_io import atomic_replace


class AtomicReplacement(unittest.TestCase):
    def test_replacement_preserves_open_old_inode_and_private_mode(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            path.write_bytes(b'old')
            path.chmod(0o644)
            with path.open('rb') as original:
                atomic_replace(path, b'new\x00bytes', prefix='.test-')
                self.assertEqual(original.read(), b'old')
            self.assertEqual(path.read_bytes(), b'new\x00bytes')
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_file_fsync_failure_preserves_old_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            path.write_bytes(b'old')
            with patch('unifier.atomic_io.os.fsync', side_effect=OSError(errno.ENOSPC, 'full')):
                with self.assertRaises(OSError) as caught:
                    atomic_replace(path, b'new', prefix='.test-')
            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_replace_failure_preserves_old_file_and_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            path.write_bytes(b'old')
            with patch.object(Path, 'replace', side_effect=PermissionError('denied')):
                with self.assertRaises(PermissionError):
                    atomic_replace(path, b'new', prefix='.test-')
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_directory_fsync_failure_is_reported_after_publication(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            path.write_bytes(b'old')
            native_fsync = os.fsync
            descriptors = []

            def sync(descriptor):
                if stat.S_ISDIR(os.fstat(descriptor).st_mode):
                    descriptors.append(descriptor)
                    raise OSError(errno.EIO, 'directory sync failed')
                native_fsync(descriptor)

            with patch('unifier.atomic_io.os.fsync', side_effect=sync):
                with self.assertRaises(OSError) as caught:
                    atomic_replace(path, b'new', prefix='.test-')
            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertEqual(path.read_bytes(), b'new')
            self.assertEqual(list(path.parent.iterdir()), [path])
            self.assertEqual(len(descriptors), 1)
            with self.assertRaises(OSError):
                os.fstat(descriptors[0])

    def test_interrupt_cleans_temporary_without_publishing(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'state.json'
            with patch('unifier.atomic_io.os.fsync', side_effect=KeyboardInterrupt):
                with self.assertRaises(KeyboardInterrupt):
                    atomic_replace(path, b'new', prefix='.test-')
            self.assertEqual(list(path.parent.iterdir()), [])
