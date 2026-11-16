import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from collections import namedtuple
from unifier.portal_delivery import SelectionStore, local_path, DeliveryError, mapped_path
from unifier.errors import UnifierError

Disk = namedtuple('Disk', 'total used free')

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / '实验二 (测试) %.doc'
        self.source.write_bytes(b'fixture\x00\xff' * 101)
        self.bridge = self.root / 'bridge'
        self.bridge.mkdir()
        self.events = []
        self.store = SelectionStore(self.bridge, '/guest/bridge', emit=lambda **v: self.events.append(v))
        self.addCleanup(self.store.close)
        self.disk = patch('unifier.portal_delivery.shutil.disk_usage', return_value=Disk(100*1024**3, 0, 90*1024**3))
        self.disk.start()
        self.addCleanup(self.disk.stop)

    def select(self, **kwargs):
        return self.store.deliver([self.source.as_uri()], **kwargs)

    def test_snapshot_contents_basename_and_revoke(self):
        result = self.select()
        guest = local_path(result[0])
        host = self.bridge / guest.relative_to('/guest/bridge')
        self.assertEqual(host.name, self.source.name)
        self.assertEqual(host.read_bytes(), self.source.read_bytes())
        self.assertEqual(host.stat().st_mode & 0o777, 0o400)
        self.assertEqual(self.store.used, self.source.stat().st_size)
        self.store.revoke(result)
        self.assertFalse(host.exists())
        self.assertEqual(self.store.used, 0)

    def test_static_translates_without_copy(self):
        self.store.shares = [(str(self.root), '/different', True)]
        result = self.select()
        self.assertEqual(local_path(result[0]), Path('/different') / self.source.name)
        self.assertEqual(self.events[0]['files'][0]['kind'], 'static')
        self.assertEqual(self.store.used, 0)

    def test_uri_rejection(self):
        for value in ('https://x/a', 'file://remote/a', 'file:///a/../b', 'file:///a/%2e%2e/b',
                      'file:///a//b', 'file:///a%00b', 'file:///a%', 'file:///a?b', 'file:///a#b'):
            with self.subTest(value=value), self.assertRaises((ValueError, UnicodeError)):
                local_path(value)
        self.assertEqual(local_path(self.source.as_uri()), self.source)

    def test_unshared_writable_save_and_directory_refused(self):
        for options in ({'writable': True}, {'method': 'SaveFile'}, {'directory': True}):
            with self.subTest(options=options), self.assertRaises(DeliveryError):
                self.select(**options)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_static_new_save_path(self):
        self.store.shares = [(str(self.root), '/different', True)]
        path = self.root / 'new.txt'
        self.assertEqual(self.store.deliver([path.as_uri()], method='SaveFile'), ['file:///different/new.txt'])
        self.assertFalse(path.exists())
        self.store.shares = [(str(self.root), '/different', False)]
        with self.assertRaises(DeliveryError):
            self.store.deliver([path.as_uri()], method='SaveFile')

    def test_symlink_and_hardlink_refused(self):
        linked = self.root / 'link'
        linked.symlink_to(self.source)
        with self.assertRaises(UnifierError):
            self.store.deliver([linked.as_uri()])
        linked.unlink()
        os.link(self.source, linked)
        with self.assertRaises(UnifierError):
            self.select()
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_batch_atomic_and_count(self):
        for uris, kwargs in (([self.source.as_uri()]*2, {}),
                              ([self.source.as_uri(), 'file:///nonexistent-fixture'], {'multiple': True})):
            with self.assertRaises((DeliveryError, UnifierError)):
                self.store.deliver(uris, **kwargs)
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_cancel_and_quota(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(DeliveryError):
            self.select(cancelled=event)
        self.store.max_file_bytes = 1
        with self.assertRaises(DeliveryError):
            self.select()
        self.store.max_file_bytes = 1024**2
        self.store.max_session_bytes = 1
        with self.assertRaises(DeliveryError):
            self.select()
        self.assertEqual(list(self.store.directory.iterdir()), [])

    def test_cancel_during_copy_removes_partial_batch(self):
        cancelled = threading.Event()
        real_read = os.read
        def interrupt(fd, size):
            data = real_read(fd, size)
            cancelled.set()
            return data
        with patch('unifier.portal_delivery.os.read', side_effect=interrupt):
            with self.assertRaises(DeliveryError):
                self.select(cancelled=cancelled)
        self.assertFalse(list(self.store.directory.iterdir()))
        self.assertEqual(self.store.used, 0)

    def test_disk_reserve(self):
        self.disk.return_value = Disk(100*1024**3, 95*1024**3, 5*1024**3)
        with patch('unifier.portal_delivery.shutil.disk_usage', return_value=Disk(100*1024**3, 95*1024**3, 5*1024**3)):
            with self.assertRaises(DeliveryError):
                self.select()

    def test_ambiguous_mapping_refused(self):
        with self.assertRaises(DeliveryError):
            mapped_path(self.source, [(str(self.root), '/a', True), (str(self.root), '/b', True)])

if __name__ == '__main__':
    unittest.main()
