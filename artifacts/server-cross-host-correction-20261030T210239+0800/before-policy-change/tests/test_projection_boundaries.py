"""Independent controller regressions for failure and lifecycle boundaries."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'));sys.path.insert(0,str(ROOT/'tests'))
from unifier import fuse_view, projection
from unifier.view_backend import ViewError
from test_projection_support import fixture,open_fd_count

class RepairFailureBoundaries(unittest.TestCase):
    def test_inode_collision_rejection_rolls_back_every_anchor(self):
        with fixture() as (root,b,server):
            (root/'a').touch();(root/'b').touch()
            with patch.object(projection,'_external_inode',return_value=123):
                held=b.lookup_node(1,'a');before=open_fd_count()
                for _ in range(10):
                    with self.assertRaises(ViewError) as caught:b.lookup_node(1,'b')
                    self.assertEqual(caught.exception.errno,errno.EOVERFLOW)
                self.assertEqual(open_fd_count(),before)
                b.forget_node(held.nodeid,1)
            self.assertEqual(b.stats()['nodes'],1)
            self.assertEqual(b.stats()['inode_ids'],1)

    def test_dangling_intermediate_suffix_cannot_return_escaping_path(self):
        with fixture() as (root,b,server):
            (root/'a').mkdir()
            try:value=b.projected_link('/a/link','missing/'+('../'*32)+'tmp/host-only')
            except ViewError as exc:self.assertIn(exc.errno,(errno.ENOENT,errno.ENOTDIR,errno.EOPNOTSUPP))
            else:
                self.assertTrue(value.startswith(b.mountpoint+'/'))
                self.assertNotIn('..',Path(value).parts)
            # A plain dangling suffix still has a usable display path.
            self.assertEqual(b.projected_link('/a/link','missing/child'),b.mountpoint+'/a/missing/child')

    def test_failed_invalidation_is_not_acknowledged_and_is_retried(self):
        with fixture() as (root,b,server):
            path=root/'file';path.write_bytes(b'O'*4096)
            node=server.lookup('file');fh=server.open_file(node);before=path.stat()
            try:
                with path.open('r+b') as stream:
                    stream.write(b'N'*4096);stream.flush();os.fsync(stream.fileno())
                os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
                with patch.object(server,'_notify',return_value=False):
                    self.assertEqual(server.getattr(node,fh)[2],errno.EIO)
                self.assertEqual(server.getattr(node,fh)[2],0)
                self.assertEqual(len(server.notifications),1)
                self.assertEqual(server.getattr(node,fh)[2],0)
                self.assertEqual(server.notifications,[])
            finally:server.release(fh)

    def test_releasedir_collects_forgotten_child_attribute_versions(self):
        with fixture() as (root,b,server):
            (root/'file').touch();child=server.lookup('file');directory=server.opendir()
            server.forget(child)
            # A name record keeps collision metadata, not the child's anchor
            # or attributes: FORGET can collect it before RELEASEDIR.
            self.assertEqual(b.stats()['nodes'],1)
            self.assertEqual(b.stats()['snapshot_identity_refs'],1)
            self.assertEqual(server._status()['notifications']['tracked_inodes'],0)
            server.releasedir(directory)
            self.assertEqual(b.stats()['nodes'],1)
            self.assertEqual(b.stats()['snapshot_identity_refs'],0)
            self.assertEqual(server._status()['notifications']['tracked_inodes'],0)

    def test_failed_lookup_invalidation_does_not_leak_lookup_reference(self):
        with fixture() as (root,b,server):
            path=root/'file';path.write_bytes(b'O'*4096)
            child=server.lookup('file');before=path.stat()
            path.write_bytes(b'N'*4096)
            os.utime(path,ns=(before.st_atime_ns,before.st_mtime_ns))
            with patch.object(server,'_notify',return_value=False):
                reply=server.call(fuse_view.FUSE_LOOKUP,1,b'file\0')
                self.assertEqual(reply[2],errno.EIO)
            self.assertEqual(b.get_node(child).nlookup,1)
            server.forget(child)
            self.assertEqual(b.stats()['nodes'],1)

    def test_absent_kernel_inode_is_benign_but_other_notify_errors_fail(self):
        with fixture() as (root,b,server):
            wire=fuse_view.FuseServer(-1,b,mountpoint=b.mountpoint,
                control_socket='',view_uuid='notify-error-test')
            payload=fuse_view._NOTIFY_INVAL_INODE.pack(2,0,0)
            with patch.object(wire,'_write_message',side_effect=OSError(errno.ENOENT,'inode absent')):
                self.assertTrue(wire._notify(fuse_view.FUSE_NOTIFY_INVAL_INODE,payload))
            self.assertEqual(wire.notification_absent_inodes,1)
            self.assertEqual(wire.notification_failures,0)
            with patch.object(wire,'_write_message',side_effect=OSError(errno.EIO,'device failure')):
                with self.assertRaises(OSError):wire._notify(fuse_view.FUSE_NOTIFY_INVAL_INODE,payload)
            self.assertEqual(wire.notification_failures,1)

if __name__=='__main__':unittest.main()
