"""FD pressure and directory identity ownership, using real low-limit children."""
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from multilinux import fuse_view, projection
from multilinux.projection import ViewError
from multilinux.views import _control_request
from test_projection_support import fixture, open_fd_count


class LightweightDirectories(unittest.TestCase):
    def test_control_accept_recovers_from_real_resource_errno(self):
        import socket
        with fixture() as (root, backend, _server):
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            class FlakyListener:
                failures = [errno.EMFILE, errno.ENFILE]
                def __getattr__(self, name):
                    return getattr(listener, name)
                def accept(self):
                    if self.failures:
                        raise OSError(self.failures.pop(0), 'transient resource error')
                    return listener.accept()
            server = fuse_view.FuseServer(-1, backend, mountpoint=backend.mountpoint,
                         control_socket=str(root/'control.sock'), view_uuid='fd-test')
            try:
                with patch.object(fuse_view.socket, 'socket', return_value=FlakyListener()):
                    server._start_control()
                self.assertTrue(_control_request(str(root/'control.sock'), {'op':'status'})['ok'])
            finally:
                server.close()

    def test_provider_path_walk_has_constant_temporary_fd_cost(self):
        with fixture() as (root, backend, _server):
            current = root
            parts = ['d'] * 80
            for part in parts:
                current = current / part
                current.mkdir()
            before = open_fd_count()
            peak = before
            real_open = os.open
            def observed_open(*args, **kwargs):
                nonlocal peak
                fd = real_open(*args, **kwargs)
                peak = max(peak, open_fd_count())
                return fd
            with patch.object(projection.os, 'open', side_effect=observed_open):
                fd, _ = backend.objects._open_provider_path(backend.merged, '/'.join(parts))
            os.close(fd)
            self.assertLessEqual(peak-before, 2)
            self.assertEqual(open_fd_count(), before)

    def test_low_nofile_large_directories_and_lookup_recovery(self):
        # Do not lower the test runner's process-wide limit (or affect peers).
        child = subprocess.run([sys.executable, str(Path(__file__)), '--pressure'],
                               capture_output=True, text=True, timeout=45)
        self.assertEqual(child.returncode, 0, child.stdout + child.stderr)
        result = json.loads(child.stdout)
        self.assertEqual([r['files'] for r in result['directories']], [300, 3000, 10000])
        for row in result['directories']:
            self.assertLessEqual(row['fd_delta'], 2)
            self.assertEqual(row['nodes'], 1)
        self.assertEqual(result['pressure']['errno'], errno.EMFILE)
        self.assertGreaterEqual(result['pressure']['headroom'], 16)
        self.assertTrue(result['pressure']['recovered'])

    def test_directory_claim_blocks_collision_after_child_collection(self):
        with fixture() as (root, backend, _server):
            (root / 'a').touch()
            (root / 'elsewhere').mkdir()
            (root / 'elsewhere/b').touch()
            (root / 'dir').mkdir()
            (root / 'a').rename(root / 'dir/a')
            objects = backend.objects
            elsewhere = objects.lookup(1, 'elsewhere')
            directory = objects.lookup(1, 'dir')
            with patch.object(projection, '_external_inode', return_value=123):
                fh = objects.opendir(directory.nodeid)
                self.assertEqual(objects.stats()['nodes'], 3)
                before = open_fd_count()
                with self.assertRaises(ViewError) as caught:
                    objects.lookup(elsewhere.nodeid, 'b')
                self.assertEqual(caught.exception.errno, errno.EOVERFLOW)
                self.assertEqual(open_fd_count(), before)
                objects.release(fh)
                b = objects.lookup(elsewhere.nodeid, 'b')
                objects.forget(b.nodeid, 1)
            objects.forget(directory.nodeid, 1)
            objects.forget(elsewhere.nodeid, 1)
            self.assertEqual(objects.stats()['inode_ids'], 1)

    def test_intra_snapshot_collision_and_partial_failure_roll_back(self):
        with fixture() as (root, backend, _server):
            (root / 'a').touch()
            (root / 'b').touch()
            objects = backend.objects
            before = open_fd_count()
            with patch.object(projection, '_external_inode', return_value=123):
                with self.assertRaises(ViewError) as caught:
                    objects.opendir(1)
            self.assertEqual(caught.exception.errno, errno.EOVERFLOW)
            self.assertEqual(open_fd_count(), before)
            self.assertEqual(objects.stats()['inode_ids'], 1)
            self.assertEqual(objects.stats()['snapshot_identity_refs'], 0)
            # Include-stat failure after earlier records have been assembled.
            real_stat = objects.node_stat
            def failing(node, *args):
                if node.path == '/b':
                    raise ViewError(errno.EIO, 'injected fstat failure')
                return real_stat(node, *args)
            with patch.object(objects, 'node_stat', side_effect=failing):
                with self.assertRaises(ViewError):
                    backend.readdir('/')
            self.assertEqual(open_fd_count(), before)
            self.assertEqual(objects.stats()['nodes'], 1)
            self.assertEqual(objects.stats()['inode_ids'], 1)

    def test_two_streams_hardlinks_forget_and_lookup_preserve_claims(self):
        with fixture() as (root, backend, _server):
            (root / 'a').touch()
            os.link(root / 'a', root / 'b')
            o = backend.objects
            f1, f2 = o.opendir(1), o.opendir(1)
            a = o.lookup(1, 'a')
            inode = a.inode
            o.forget(a.nodeid, 1)
            self.assertEqual(o.stats()['nodes'], 1)
            self.assertEqual(o.stats()['inode_ids'], 2)
            self.assertEqual(o.stats()['snapshot_identity_refs'], 4)
            o.release(f1)
            self.assertEqual(o.stats()['snapshot_identity_refs'], 2)
            b = o.lookup(1, 'b')
            self.assertEqual(b.inode, inode)
            o.release(f2)
            self.assertEqual(o.stats()['inode_ids'], 2)
            self.assertEqual(o.stats()['snapshot_identity_refs'], 0)
            o.forget(b.nodeid, 1)
            self.assertEqual(o.stats()['inode_ids'], 1)

    def test_real_resource_errors_are_preserved_and_existing_read_survives(self):
        with fixture() as (root, backend, _server):
            (root / 'a').write_bytes(b'old')
            o = backend.objects
            a = o.lookup(1, 'a')
            fh = o.open_file(a.nodeid, os.O_RDONLY)
            before = open_fd_count()
            for err in (errno.ENFILE, errno.EMFILE):
                with patch.object(projection.os, 'scandir', side_effect=OSError(err, 'injected')):
                    with self.assertRaises(ViewError) as caught:
                        o.lookup(1, 'a')
                    self.assertEqual(caught.exception.errno, err)
                    self.assertIsNone(o.stats()['process_fds'])
                    self.assertEqual(o.stats()['fd_measurement_errno'], err)
                    self.assertEqual(o.read_file(fh, 0, 3), b'old')
            self.assertEqual(open_fd_count(), before)
            o.release(fh)
            o.forget(a.nodeid, 1)


def pressure_probe():
    import resource
    import tempfile
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
    result = dict(directories=[])
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for count in (300, 3000, 10000):
            directory = root / str(count)
            directory.mkdir()
            for i in range(count):
                (directory / f'f{i:05d}').touch()
            with fixture(directory, max_nodes=3) as (_, b, _server):
                before = open_fd_count()
                fh = b.objects.opendir(1)
                delta = open_fd_count() - before
                stats = b.stats()
                entries = b.objects.readdir(fh, 0)[1]
                assert len(entries) == count + 2
                assert stats['snapshot_identity_refs'] == count
                assert stats['anchor_fds'] == 1
                result['directories'].append(dict(files=count, fd_delta=delta,
                                                  nodes=stats['nodes']))
                b.objects.release(fh)
                assert open_fd_count() == before
                assert b.stats()['inode_ids'] == 1
                assert len(b.readdir('/')) == count + 2
        with fixture(root / '300') as (_, b, _server):
            o = b.objects
            first = o.lookup(1, 'f00000')
            fh = o.open_file(first.nodeid, os.O_RDONLY)
            ids = [first.nodeid]
            err = None
            for i in range(1, 300):
                try:
                    ids.append(o.lookup(1, f'f{i:05d}').nodeid)
                except ViewError as exc:
                    err = exc.errno
                    break
            assert err == errno.EMFILE
            stats = o.stats()
            assert 256 - stats['process_fds'] >= stats['fd_control_reserve']
            assert stats['fd_budget_rejections'] > 0
            assert o.read_file(fh, 0, 1) == b''
            o.release(fh)
            o.batch_forget([(nodeid, 1) for nodeid in ids])
            again = o.lookup(1, 'f00299')
            o.forget(again.nodeid, 1)
            assert o.stats()['nodes'] == o.stats()['inode_ids'] == 1
            # Lower the actual limit again: admission must not cache it.
            resource.setrlimit(resource.RLIMIT_NOFILE, (32, hard))
            unrelated = []
            try:
                for _ in range(12):
                    unrelated.append(os.open('/dev/null', os.O_RDONLY))
                try:
                    o.lookup(1, 'f00001')
                except ViewError as exc:
                    assert exc.errno == errno.EMFILE
                else:
                    raise AssertionError('unrelated process FDs bypassed admission')
            finally:
                for fd in unrelated:
                    os.close(fd)
                resource.setrlimit(resource.RLIMIT_NOFILE, (256, hard))
            result['pressure'] = dict(errno=err, lookups=len(ids),
                                      headroom=256-stats['process_fds'], recovered=True)
    resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
    return result


if __name__ == '__main__':
    if '--pressure' in sys.argv:
        print(json.dumps(pressure_probe()))
    else:
        unittest.main()
