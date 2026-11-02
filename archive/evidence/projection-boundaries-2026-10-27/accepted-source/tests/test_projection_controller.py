"""Controller regressions for exception cleanup and preserved resource errors."""
import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multilinux.view_backend import IdMap, ViewBackend, ViewError
from multilinux import fuse_view


class ProjectionFailureCleanup(unittest.TestCase):
    def make_backend(self, root, **kwargs):
        fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
        self.addCleanup(os.close, fd)
        backend = ViewBackend(mountpoint=str(root/'view'), merged_fd=fd,
            merged_stat=os.fstat(fd), mounts=kwargs.pop('mounts', []), capabilities=[],
            id_map=IdMap(os.getuid(),os.getgid(),0,0,0,0,0,0,'default'),
            view_uuid='controller-regression', **kwargs)
        self.addCleanup(backend.close)
        return backend

    def test_failed_nested_queries_release_temporary_parent_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend = self.make_backend(root, max_nodes=4)
            fds_before = len(os.listdir('/proc/self/fd'))
            for i in range(30):
                (root/str(i)).mkdir()
                with self.assertRaises(ViewError) as caught:
                    backend.getattr('/'+str(i)+'/missing')
                self.assertEqual(caught.exception.errno, errno.ENOENT)
                self.assertEqual(backend.stats()['nodes'], 1)
            self.assertEqual(len(os.listdir('/proc/self/fd')), fds_before)

    def test_source_verification_resource_failure_preserves_errno_and_open_fd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root/'source'
            source.mkdir()
            (source/'file').write_bytes(b'original object')
            backend = self.make_backend(root, mounts=[dict(destination='/mount',source=str(source))])
            mount = backend.lookup_node(1,'mount')
            node = backend.lookup_node(mount.nodeid,'file')
            fh = backend.open_node(node.nodeid,os.O_RDONLY)
            original_open = os.open
            try:
                for code in (errno.EMFILE, errno.ENFILE, errno.ENOMEM):
                    def limited(path,*args,**kwargs):
                        if str(path)==str(source):
                            raise OSError(code,'source verification resource pressure')
                        return original_open(path,*args,**kwargs)
                    with patch('multilinux.projection.os.open',side_effect=limited):
                        with self.assertRaises(ViewError) as caught:
                            backend.lookup_node(1,'mount')
                        self.assertEqual(caught.exception.errno,code)
                        self.assertEqual(backend.read_handle(fh,0,100),b'original object')
                self.assertEqual(backend.lookup_node(1,'mount').nodeid,mount.nodeid)
            finally:
                backend.release_handle(fh)

    def test_dot_entries_obey_directory_byte_budget_at_exact_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            backend = self.make_backend(Path(temporary), max_dir_bytes=130)
            with self.assertRaises(ViewError) as caught:
                backend.open_dir(1)
            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertEqual(backend.stats()['handles'], 0)
            self.assertEqual(backend.stats()['dir_bytes'], 0)

    def test_relative_hardlinked_symlink_refuses_ambiguous_alias_projection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('a', 'b'):
                (root/name).mkdir()
                (root/name/'target').write_text(name)
            os.symlink('target',root/'a/link')
            os.link(root/'a/link',root/'b/link',follow_symlinks=False)
            backend = self.make_backend(root)
            parent = backend.lookup_node(1,'a')
            link = backend.lookup_node(parent.nodeid,'link')
            other_parent = backend.lookup_node(1,'b')
            alias = backend.lookup_node(other_parent.nodeid,'link')
            self.assertEqual(link.nodeid,alias.nodeid)
            server = fuse_view.FuseServer(-1,backend,mountpoint=str(root/'view'),
                                         control_socket='',view_uuid='alias-regression')
            with self.assertRaises(ViewError) as caught:
                server._handle(fuse_view.FUSE_READLINK,alias.nodeid,1,b'')
            self.assertEqual(caught.exception.errno,errno.EOPNOTSUPP)


if __name__ == '__main__':
    unittest.main()
