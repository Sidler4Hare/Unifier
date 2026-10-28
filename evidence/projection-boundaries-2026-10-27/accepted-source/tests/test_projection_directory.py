"""Mount topology, directory stream and lifecycle tests for the projection."""

import errno
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from multilinux import fuse_view
from multilinux.view_backend import ViewError
from test_projection_support import fixture, open_fd_count, parse_dirents


def _page(server, fh, cookie=0, size=4096):
    reply = server.readdir(fh, cookie, size)
    if reply is None or reply[2] != 0:
        raise AssertionError(f"readdir failed: {reply!r}")
    return parse_dirents(reply[1])


def _names(server, fh):
    result = []
    cookie = 0
    while True:
        page = _page(server, fh, cookie)
        if not page:
            return result
        result.extend(item["name"] for item in page)
        cookie = page[-1]["cookie"]


def _fuse_names(server, node):
    fh = server.opendir(node)
    try:
        return [name for name in _names(server, fh) if name not in {".", ".."}]
    finally:
        server.releasedir(fh)


class MountTopology(unittest.TestCase):
    def test_nested_mount_injects_missing_ancestor_and_wins_over_placeholder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data/file").write_text("merged", encoding="utf-8")
            source = root / "source"
            source.mkdir()
            (source / "file").write_text("mounted", encoding="utf-8")
            mounts = [{
                "destination": "/data/child", "source": str(source),
                "type": "bind", "options": "ro"}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                root_entries = backend.readdir("/")
                by_name = {item["name"]: item for item in root_entries}
                self.assertIn("data", by_name)
                self.assertEqual(by_name["data"]["kind"], "dir")

                data_entries = {
                    item["name"]: item for item in backend.readdir("/data")}
                self.assertIn("child", data_entries)
                self.assertEqual(data_entries["child"]["kind"], "dir")
                self.assertEqual(
                    backend.read("/data/child/file", 0, 100), b"mounted")

                # A merged file placeholder at the mount point must not change
                # the projected type or shadow the mount.
                (root / "data/child").write_text("placeholder", encoding="utf-8")
                data_entries = {
                    item["name"]: item for item in backend.readdir("/data")}
                self.assertEqual(data_entries["child"]["kind"], "dir")
                self.assertEqual(
                    backend.getattr("/data/child")["mode"] & 0o170000,
                    0o040000)
                self.assertEqual(
                    backend.read("/data/child/file", 0, 100), b"mounted")
                self.assertIn("data", _fuse_names(server, 1))
                data = server.lookup("data")
                self.assertIn("child", _fuse_names(server, data))

    def test_missing_ancestor_is_synthetic_and_file_ancestor_is_forced_dir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "x").write_text("ok", encoding="utf-8")
            # No /data in the merged namespace at all.
            mounts = [{"destination": "/data/child", "source": str(source)}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                self.assertIn("data", _fuse_names(server, 1))
                data = server.lookup("data")
                child = server.lookup("child", data)
                self.assertEqual(
                    backend.node_attr_by_id(data)["mode"] & 0o170000,
                    0o040000)
                self.assertEqual(
                    backend.node_attr_by_id(child)["mode"] & 0o170000,
                    0o040000)
                self.assertEqual(
                    backend.read("/data/child/x", 0, 10), b"ok")

            # A file at the required ancestor name is still projected as a
            # directory so the nested mount is reachable.
            (root / "data").write_text("merged file", encoding="utf-8")
            with fixture(root, mounts=mounts) as (_root, backend, server):
                data_entries = _fuse_names(server, 1)
                self.assertIn("data", data_entries)
                data = server.lookup("data")
                self.assertEqual(
                    backend.node_attr_by_id(data)["mode"] & 0o170000,
                    0o040000)
                self.assertEqual(_fuse_names(server, data), ["child"])
                self.assertEqual(
                    backend.read("/data/child/x", 0, 10), b"ok")
                self.assertEqual(
                    server.call(fuse_view.FUSE_LOOKUP, data, b"other\x00")[2],
                    errno.ENOENT)

    def test_required_ancestor_under_mount_does_not_fall_back_to_merged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data/sub").mkdir()
            (root / "data/sub/leak").write_text(
                "must not be visible", encoding="utf-8")
            data_source = root / "data-source"
            data_source.mkdir()
            (data_source / "top").write_text("mounted", encoding="utf-8")
            deep_source = root / "deep-source"
            deep_source.mkdir()
            (deep_source / "deep-file").write_text("deep", encoding="utf-8")
            mounts = [
                {"destination": "/data", "source": str(data_source)},
                {"destination": "/data/sub/deep", "source": str(deep_source)},
            ]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                data = server.lookup("data")
                sub = server.lookup("sub", data)
                self.assertEqual(
                    backend.node_attr_by_id(sub)["mode"] & 0o170000,
                    0o040000)
                self.assertEqual(_fuse_names(server, sub), ["deep"])
                self.assertEqual(
                    backend.read("/data/sub/deep/deep-file", 0, 100),
                    b"deep")

    def test_file_mount_has_no_children_and_overrides_a_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/hosts").mkdir()
            (root / "etc/hosts/underlying").write_text(
                "hidden", encoding="utf-8")
            host_file = root / "hosts"
            host_file.write_text("127.0.0.1 localhost\n", encoding="utf-8")
            mounts = [{
                "destination": "/etc/hosts", "source": str(host_file)}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                entries = {
                    item["name"]: item for item in backend.readdir("/etc")}
                self.assertIn("hosts", entries)
                self.assertEqual(entries["hosts"]["kind"], "file")
                etc = server.lookup("etc")
                hosts = server.lookup("hosts", etc)
                self.assertEqual(
                    backend.node_attr_by_id(hosts)["mode"] & 0o170000,
                    0o100000)
                self.assertEqual(
                    backend.read("/etc/hosts", 0, 100),
                    b"127.0.0.1 localhost\n")
                with self.assertRaisesRegex(ViewError, "file mount"):
                    backend.readdir("/etc/hosts")
                self.assertEqual(
                    server.call(fuse_view.FUSE_OPENDIR, hosts,
                                struct.pack("<I", os.O_RDONLY))[2],
                    errno.ENOTDIR)
                self.assertEqual(
                    _fuse_names(server, etc), ["hosts"])

    def test_invalid_provider_fails_closed_without_falling_back_to_merged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "data").mkdir()
            (root / "data/file").write_text("merged", encoding="utf-8")
            source = root / "source"
            source.mkdir()
            (source / "file").write_text("mounted", encoding="utf-8")
            mounts = [{"destination": "/data", "source": str(source)}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                node = backend.resolve_path("/data/file")
                fh = backend.open_node(node.nodeid, os.O_RDONLY)
                self.assertEqual(backend.read_handle(fh, 0, 100), b"mounted")

                source.rename(root / "gone")
                source.mkdir()
                (source / "file").write_text("replacement", encoding="utf-8")

                with self.assertRaisesRegex(ViewError, "changed|replaced"):
                    backend.getattr("/data/file")
                with self.assertRaisesRegex(ViewError, "changed|replaced"):
                    backend.read("/data/file", 0, 100)
                # Already pinned objects stay on their own object identity;
                # there is no path fallback and no silent cross-provider read.
                self.assertEqual(backend.read_handle(fh, 0, 100), b"mounted")
                fh2 = backend.open_node(node.nodeid, os.O_RDONLY)
                self.assertEqual(backend.read_handle(fh2, 0, 100), b"mounted")
                backend.release_handle(fh2)

    def test_capability_paths_are_unavailable_unless_a_mount_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "proc").mkdir()
            (root / "proc/self").write_text("host proc", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                self.assertNotIn("proc", _fuse_names(server, 1))
                with self.assertRaisesRegex(ViewError, "capability"):
                    backend.getattr("/proc")
                self.assertEqual(
                    server.call(
                        fuse_view.FUSE_LOOKUP, 1, b"proc\x00")[2],
                    errno.ENOTSUP)

            source = root / "sys-source"
            source.mkdir()
            (source / "entry").write_text("mounted", encoding="utf-8")
            mounts = [{"destination": "/proc/sys", "source": str(source)}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                root_names = _fuse_names(server, 1)
                self.assertIn("proc", root_names)
                proc = server.lookup("proc")
                self.assertEqual(_fuse_names(server, proc), ["sys"])
                self.assertEqual(
                    server.call(
                        fuse_view.FUSE_LOOKUP, proc, b"self\x00")[2],
                    errno.ENOTSUP)
                sys_node = server.lookup("sys", proc)
                self.assertEqual(
                    backend.read("/proc/sys/entry", 0, 100), b"mounted")
                self.assertNotEqual(sys_node, proc)


class ControllerRegressions(unittest.TestCase):
    def test_dirent_identity_is_projected_and_hardlinks_share_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("x", encoding="utf-8")
            os.link(root / "file", root / "alias")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                inode = backend.get_node(node).inode
                fh = server.opendir(1)
                try:
                    entries = {
                        item["name"]: item for item in _page(server, fh, 0)}
                    self.assertEqual(entries["file"]["ino"], inode)
                    self.assertEqual(entries["alias"]["ino"], inode)
                    self.assertNotEqual(entries["file"]["ino"], node)
                finally:
                    server.releasedir(fh)
                self.assertEqual(
                    backend.stats()["dir_entries"], 0)

    def test_directory_entries_are_global_budgeted_and_release_restores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "one").write_text("x", encoding="utf-8")
            with fixture(root, max_dir_entries=5) as (_root, backend, server):
                first = server.opendir(1)
                try:
                    reply = server.call(
                        fuse_view.FUSE_OPENDIR, 1,
                        struct.pack("<I", os.O_RDONLY))
                    self.assertEqual(reply[2], errno.ENOSPC)
                    self.assertEqual(backend.stats()["dir_entries"], 3)
                finally:
                    server.releasedir(first)
                self.assertEqual(backend.stats()["dir_entries"], 0)
                second = server.opendir(1)
                try:
                    self.assertGreaterEqual(
                        backend.stats()["dir_entries"], 3)
                finally:
                    server.releasedir(second)

            with fixture(root, max_dir_bytes=250) as (_root, backend, server):
                first = server.opendir(1)
                try:
                    reply = server.call(
                        fuse_view.FUSE_OPENDIR, 1,
                        struct.pack("<I", os.O_RDONLY))
                    self.assertEqual(reply[2], errno.ENOSPC)
                finally:
                    server.releasedir(first)

    def test_snapshot_failure_rolls_back_entry_refs_and_fds(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b", "c"):
                (root / name).write_text(name, encoding="utf-8")
            with fixture(root, max_nodes=3) as (_root, backend, server):
                before_fds = open_fd_count()
                reply = server.call(
                    fuse_view.FUSE_OPENDIR, 1,
                    struct.pack("<I", os.O_RDONLY))
                self.assertEqual(reply[2], errno.ENOSPC)
                self.assertEqual(open_fd_count(), before_fds)
                stats = backend.stats()
                self.assertEqual(stats["nodes"], 1)
                self.assertEqual(stats["handles"], 0)
                self.assertEqual(stats["dir_entries"], 0)
                self.assertEqual(stats["dir_bytes"], 0)

    def test_releasedir_reclaims_unlooked_up_entry_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b", "c"):
                (root / name).write_text(name, encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                fh = server.opendir(1)
                self.assertGreater(backend.stats()["nodes"], 1)
                self.assertGreater(backend.stats()["dir_entries"], 0)
                server.releasedir(fh)
                stats = backend.stats()
                self.assertEqual(stats["nodes"], 1)
                self.assertEqual(stats["dir_entries"], 0)

    def test_releasedir_inode_is_stable_across_tiny_budget_churn(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            kept = root / "kept"
            kept.mkdir()
            for name in ("a", "b", "c"):
                (kept / name).write_text(name, encoding="utf-8")
            for index in range(120):
                (root / f"churn-{index:03d}").write_text(
                    "x", encoding="utf-8")
            with fixture(root, max_nodes=5) as (_root, backend, server):
                kept_node = server.lookup("kept")
                fh = server.opendir(kept_node)
                try:
                    listed = {
                        item["name"]: item["ino"]
                        for item in _page(server, fh, 0)
                        if item["name"] not in {".", ".."}}
                finally:
                    server.releasedir(fh)
                self.assertEqual(backend.stats()["dir_handles"], 0)
                server.forget(kept_node, 1)
                self.assertEqual(backend.stats()["nodes"], 1)

                for round_index in range(3):
                    for index in range(40):
                        offset = round_index * 40 + index
                        backend.getattr(f"/churn-{offset:03d}")
                    for name in ("a", "b", "c"):
                        observed = backend.getattr(f"/kept/{name}")
                        self.assertEqual(observed["ino"], listed[name])

                stats = backend.stats()
                self.assertEqual(stats["nodes"], 1)
                # The collision table contains live identities only; churn
                # must not leave a path/identity history behind.
                self.assertEqual(stats["inode_ids"], stats["nodes"])
                self.assertEqual(stats["dir_entries"], 0)
                self.assertEqual(stats["handles"], 0)

    def test_releasedir_releases_duplicate_hardlink_entry_refs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("same", encoding="utf-8")
            os.link(root / "a", root / "b")
            with fixture(root) as (_root, backend, server):
                fh = server.opendir(1)
                try:
                    entries = {
                        item["name"]: item for item in _page(server, fh, 0)}
                    self.assertEqual(entries["a"]["ino"], entries["b"]["ino"])
                    self.assertEqual(backend.stats()["nodes"], 2)
                finally:
                    server.releasedir(fh)
                self.assertEqual(backend.stats()["nodes"], 1)

    def test_new_snapshot_rejects_invalid_provider_old_snapshot_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            (source / "file").write_text("x", encoding="utf-8")
            mounts = [{"destination": "/mount", "source": str(source)}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                fh = server.opendir(1)
                try:
                    old_names = [
                        item["name"] for item in _page(server, fh, 0)]
                    self.assertIn("mount", old_names)
                    source.rename(root / "gone")
                    source.mkdir()
                    reply = server.call(
                        fuse_view.FUSE_OPENDIR, 1,
                        struct.pack("<I", os.O_RDONLY))
                    self.assertEqual(reply[2], errno.EIO)
                    self.assertIn(
                        "mount",
                        [item["name"] for item in _page(server, fh, 0)])
                finally:
                    server.releasedir(fh)

    def test_required_ancestor_permission_and_fstat_errors_do_not_go_synthetic(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outer = root / "outer"
            outer.mkdir()
            (outer / "deep").mkdir()
            inner = root / "inner"
            inner.mkdir()
            mounts = [
                {"destination": "/outer", "source": str(outer)},
                {"destination": "/outer/deep/child", "source": str(inner)},
            ]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                outer_node = server.lookup("outer")
                real_open = os.open

                def denied(path, flags, *args, **kwargs):
                    if (path == "deep" and kwargs.get("dir_fd") is not None
                            and not flags & os.O_DIRECTORY):
                        raise OSError(errno.EACCES, "injected permission error")
                    return real_open(path, flags, *args, **kwargs)

                with mock.patch(
                        "multilinux.projection.os.open", side_effect=denied):
                    reply = server.call(
                        fuse_view.FUSE_LOOKUP, outer_node, b"deep\x00")
                self.assertEqual(reply[2], errno.EACCES)

                real_fstat = os.fstat
                tracked = []

                def tracked_open(path, flags, *args, **kwargs):
                    descriptor = real_open(path, flags, *args, **kwargs)
                    if (path == "deep" and kwargs.get("dir_fd") is not None
                            and not flags & os.O_DIRECTORY):
                        tracked.append(descriptor)
                    return descriptor

                def failing_fstat(descriptor):
                    if descriptor in tracked:
                        raise OSError(errno.EIO, "injected fstat error")
                    return real_fstat(descriptor)

                before_fds = open_fd_count()
                with mock.patch(
                        "multilinux.projection.os.open",
                        side_effect=tracked_open), mock.patch(
                        "multilinux.projection.os.fstat",
                        side_effect=failing_fstat):
                    reply = server.call(
                        fuse_view.FUSE_LOOKUP, outer_node, b"deep\x00")
                self.assertEqual(reply[2], errno.EIO)
                self.assertTrue(tracked)
                with self.assertRaises(OSError) as raised:
                    real_fstat(tracked[0])
                self.assertEqual(raised.exception.errno, errno.EBADF)
                self.assertEqual(open_fd_count(), before_fds)


class DirectoryStreams(unittest.TestCase):
    def test_snapshot_cookie_survives_delete_and_is_independent_per_opendir(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b", "c", "d"):
                (root / name).write_text(name, encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                first = server.opendir(1)
                second = None
                try:
                    page1 = _page(server, first, 0, 96)
                    names1 = [item["name"] for item in page1]
                    self.assertIn("a", names1)

                    (root / "a").unlink()
                    (root / "e").write_text("e", encoding="utf-8")
                    second = server.opendir(1)
                    cookie = page1[-1]["cookie"]
                    rest = []
                    while True:
                        page = _page(server, first, cookie)
                        if not page:
                            break
                        rest.extend(item["name"] for item in page)
                        cookie = page[-1]["cookie"]
                    self.assertIn("b", rest)
                    self.assertIn("c", rest)
                    self.assertIn("d", rest)
                    self.assertNotIn("a", rest)
                    self.assertNotIn("e", rest)

                    second_names = _names(server, second)
                    self.assertIn("e", second_names)
                    self.assertNotIn("a", second_names)
                finally:
                    server.releasedir(first)
                    if second is not None:
                        server.releasedir(second)

                third = server.opendir(1)
                try:
                    self.assertIn("e", _names(server, third))
                finally:
                    server.releasedir(third)

    def test_releasedir_clears_entries_and_unknown_handle_is_ebadf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b"):
                (root / name).write_text(name, encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                fh = server.opendir(1)
                before = backend.stats()
                self.assertEqual(before["dir_handles"], 1)
                self.assertGreater(before["dir_entries"], 0)
                self.assertGreater(before["dir_bytes"], 0)
                self.assertEqual(server.releasedir(fh)[2], 0)
                after = backend.stats()
                self.assertEqual(after["dir_handles"], 0)
                self.assertEqual(after["dir_entries"], 0)
                self.assertEqual(after["dir_bytes"], 0)
                self.assertEqual(
                    server.releasedir(fh)[2], errno.EBADF)
                self.assertEqual(
                    server.readdir(fh, 0, 4096)[2], errno.EBADF)

    def test_directory_snapshot_budget_enospc_rolls_back_handle_and_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b", "c"):
                (root / name).write_text(name, encoding="utf-8")
            with fixture(root, max_dir_entries=3) as (_root, backend, server):
                before = open_fd_count()
                reply = server.call(
                    fuse_view.FUSE_OPENDIR, 1,
                    struct.pack("<I", os.O_RDONLY))
                self.assertEqual(reply[2], errno.ENOSPC)
                self.assertEqual(open_fd_count(), before)
                stats = backend.stats()
                self.assertEqual(stats["handles"], 0)
                self.assertEqual(stats["dir_entries"], 0)
                self.assertEqual(stats["dir_bytes"], 0)

            with fixture(root, max_dir_bytes=128) as (_root, backend, server):
                reply = server.call(
                    fuse_view.FUSE_OPENDIR, 1,
                    struct.pack("<I", os.O_RDONLY))
                self.assertEqual(reply[2], errno.ENOSPC)
                self.assertEqual(backend.stats()["handles"], 0)

    def test_close_drops_handles_anchors_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("x", encoding="utf-8")
            backend, merged = None, None
            from test_projection_support import backend_for
            backend, merged = backend_for(root)
            try:
                node = backend.resolve_path("/file")
                fh = backend.open_node(node.nodeid, os.O_RDONLY)
                dh = backend.open_dir(1)
                self.assertEqual(backend.read_handle(fh, 0, 1), b"x")
                backend.close()
                backend.close()
                self.assertEqual(backend.stats()["handles"], 0)
                self.assertEqual(backend.stats()["nodes"], 0)
                with self.assertRaises(ViewError):
                    backend.read_handle(fh, 0, 1)
                with self.assertRaises(ViewError):
                    backend.readdir_handle(dh, 0)
                with self.assertRaises(ViewError):
                    backend.getattr("/file")
            finally:
                if backend is not None:
                    backend.close()
                if merged is not None:
                    os.close(merged)


if __name__ == "__main__":
    unittest.main()
