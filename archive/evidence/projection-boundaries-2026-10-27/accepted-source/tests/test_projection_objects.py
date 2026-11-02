"""Object identity, handles, refcounts, budgets and legacy entry point."""

import errno
import mmap
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from multilinux import fuse_view
from multilinux.view_backend import ViewError
from test_projection_support import (
    attr_from_payload, fixture, open_fd_count, parse_dirents)


class ObjectIdentity(unittest.TestCase):
    def test_replace_before_read_keeps_old_object_and_new_lookup_sees_new(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "file"
            path.write_bytes(b"OLD")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                fh = server.open_file(node)
                replacement = root / "replacement"
                replacement.write_bytes(b"NEW-CONTENT")
                os.replace(replacement, path)

                reply = server.read(fh, 0, 100)
                self.assertEqual(reply[2], 0)
                self.assertEqual(reply[1], b"OLD")
                self.assertEqual(backend.read_handle(fh, 0, 100), b"OLD")

                new_node = server.lookup("file")
                self.assertNotEqual(new_node, node)
                self.assertEqual(
                    attr_from_payload(server.getattr(new_node)[1])["size"],
                    len(b"NEW-CONTENT"))
                self.assertEqual(
                    attr_from_payload(server.getattr(node)[1])["size"],
                    len(b"OLD"))
                self.assertEqual(
                    attr_from_payload(server.getattr(node, fh)[1])["size"],
                    len(b"OLD"))

                path.unlink()
                self.assertEqual(backend.read_handle(fh, 0, 100), b"OLD")
                failed = server.call(
                    fuse_view.FUSE_LOOKUP, 1, b"file\x00")
                self.assertEqual(failed[2], errno.ENOENT)

    def test_same_size_replacement_after_read_keeps_old_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "file"
            path.write_bytes(b"AAAA")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                fh = server.open_file(node)
                self.assertEqual(server.read(fh, 0, 10)[1], b"AAAA")
                replacement = root / "replacement"
                replacement.write_bytes(b"BBBB")
                os.replace(replacement, path)
                self.assertEqual(server.read(fh, 0, 10)[1], b"AAAA")
                self.assertEqual(
                    attr_from_payload(server.getattr(node, fh)[1])["size"], 4)
                new_node = server.lookup("file")
                self.assertNotEqual(new_node, node)
                new_fh = server.open_file(new_node)
                self.assertEqual(server.read(new_fh, 0, 10)[1], b"BBBB")
                server.release(new_fh)

                # A different-size replacement after an earlier read follows
                # the same rule: the old handle never adopts the new content.
                other = root / "other"
                other.write_bytes(b"CC")
                node2 = server.lookup("other")
                fh2 = server.open_file(node2)
                self.assertEqual(server.read(fh2, 0, 10)[1], b"CC")
                replacement = root / "other-new"
                replacement.write_bytes(b"DDDDDD")
                os.replace(replacement, other)
                self.assertEqual(server.read(fh2, 0, 10)[1], b"CC")
                self.assertEqual(
                    attr_from_payload(server.getattr(node2, fh2)[1])["size"], 2)
                new_node2 = server.lookup("other")
                new_fh2 = server.open_file(new_node2)
                self.assertEqual(
                    server.read(new_fh2, 0, 10)[1], b"DDDDDD")
                server.release(new_fh2)

    def test_open_uses_pinned_object_not_the_path_again(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "file"
            path.write_bytes(b"OLD")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                replacement = root / "new"
                replacement.write_bytes(b"NEW")
                os.replace(replacement, path)
                fh = server.open_file(node)
                self.assertEqual(server.read(fh, 0, 10)[1], b"OLD")

                path.unlink()
                # A node looked up before the unlink can still be opened from
                # its pinned anchor (the path no longer exists).
                path.write_bytes(b"OLD-AGAIN")
                before_unlink = server.lookup("file")
                path.unlink()
                fh2 = server.open_file(before_unlink)
                self.assertEqual(server.read(fh2, 0, 20)[1], b"OLD-AGAIN")

    def test_hardlinks_share_node_and_ino_but_cross_provider_is_independent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("same", encoding="utf-8")
            os.link(root / "a", root / "b")
            source = root / "source"
            source.mkdir()
            (source / "file").write_text("source", encoding="utf-8")
            mounts = [
                {"destination": "/mnt/a", "source": str(source)},
                {"destination": "/mnt/b", "source": str(source)},
            ]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                node_a = server.lookup("a")
                node_b = server.lookup("b")
                self.assertEqual(node_a, node_b)
                attr_a = attr_from_payload(server.getattr(node_a)[1])
                attr_b = attr_from_payload(server.getattr(node_b)[1])
                self.assertEqual(attr_a["ino"], attr_b["ino"])
                self.assertNotEqual(attr_a["ino"], node_a)
                self.assertEqual(attr_a["ino"], backend.get_node(node_a).inode)

                mount_node = server.lookup("mnt")
                node_left = server.lookup("a", mount_node)
                node_right = server.lookup("b", mount_node)
                self.assertNotEqual(node_left, node_right)
                left_attr = attr_from_payload(server.getattr(node_left)[1])
                right_attr = attr_from_payload(server.getattr(node_right)[1])
                self.assertNotEqual(left_attr["ino"], right_attr["ino"])
                self.assertEqual(
                    left_attr["ino"], backend.get_node(node_left).inode)
                self.assertEqual(
                    right_attr["ino"], backend.get_node(node_right).inode)
                self.assertIsNotNone(mount_node)

    def test_child_lookup_and_readdir_stay_on_pinned_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dir").mkdir()
            (root / "dir/file").write_text("OLD-DIR", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                old_dir = server.lookup("dir")
                old_file = server.lookup("file", old_dir)
                self.assertEqual(
                    server.read(server.open_file(old_file), 0, 100)[1],
                    b"OLD-DIR")

                (root / "dir").rename(root / "old-dir")
                (root / "dir").mkdir()
                (root / "dir/file").write_text("NEW-DIR", encoding="utf-8")

                same_file = server.lookup("file", old_dir)
                self.assertEqual(same_file, old_file)
                self.assertEqual(
                    server.read(server.open_file(same_file), 0, 100)[1],
                    b"OLD-DIR")

                fh = server.opendir(old_dir)
                try:
                    names = []
                    cookie = 0
                    while True:
                        reply = server.readdir(fh, cookie)
                        page = parse_dirents(reply[1])
                        if not page:
                            break
                        names.extend(item["name"] for item in page)
                        cookie = page[-1]["cookie"]
                    self.assertIn("file", names)
                finally:
                    server.releasedir(fh)

                new_dir = server.lookup("dir")
                self.assertNotEqual(new_dir, old_dir)
                new_file = server.lookup("file", new_dir)
                self.assertNotEqual(new_file, old_file)
                self.assertEqual(
                    server.read(server.open_file(new_file), 0, 100)[1],
                    b"NEW-DIR")

    def test_readlink_uses_pinned_symlink_after_replacement(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            os.symlink("old-target", root / "link")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("link")
                self.assertEqual(backend.readlink_node(node), "old-target")
                reply = server.call(fuse_view.FUSE_READLINK, node, b"")
                self.assertEqual(
                    reply[1].decode(), str(root / "view/old-target"))
                os.unlink(root / "link")
                os.symlink("new-target", root / "link")
                self.assertEqual(backend.readlink_node(node), "old-target")
                reply = server.call(fuse_view.FUSE_READLINK, node, b"")
                self.assertEqual(
                    reply[1].decode(), str(root / "view/old-target"))
                new_node = server.lookup("link")
                reply = server.call(fuse_view.FUSE_READLINK, new_node, b"")
                self.assertEqual(
                    reply[1].decode(), str(root / "view/new-target"))


class LiveContentAndHandles(unittest.TestCase):
    def test_in_place_growth_and_shrink_are_live_and_mmap_survives_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "file"
            path.write_bytes(b"OLD")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                fh = server.open_file(node)
                native = os.open(path, os.O_RDWR)
                try:
                    os.pwrite(native, b"LIVE-CONTENT", 0)
                    self.assertEqual(
                        backend.read_handle(fh, 0, 100), b"LIVE-CONTENT")
                    self.assertEqual(
                        attr_from_payload(
                            server.getattr(node, fh)[1])["size"],
                        len(b"LIVE-CONTENT"))
                    os.ftruncate(native, 4)
                    self.assertEqual(backend.read_handle(fh, 0, 100), b"LIVE")

                    os.pwrite(native, b"LIVE", 0)
                    mapping = mmap.mmap(
                        backend.handle_fd(fh), 4, access=mmap.ACCESS_READ)
                    try:
                        self.assertEqual(mapping[:], b"LIVE")
                        replacement = root / "replacement"
                        replacement.write_bytes(b"NEW")
                        os.replace(replacement, path)
                        # The handle and its mmap still name the old object.
                        self.assertEqual(mapping[:], b"LIVE")
                        self.assertEqual(
                            backend.read_handle(fh, 0, 100), b"LIVE")
                        new_node = server.lookup("file")
                        new_fh = server.open_file(new_node)
                        self.assertEqual(
                            server.read(new_fh, 0, 100)[1], b"NEW")
                        server.release(new_fh)
                    finally:
                        mapping.close()
                finally:
                    os.close(native)

    def test_getattr_fh_mismatch_and_unknown_handles_are_ebadf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("a", encoding="utf-8")
            (root / "b").write_text("b", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                node_a = server.lookup("a")
                node_b = server.lookup("b")
                fh = server.open_file(node_a)
                self.assertEqual(server.getattr(node_a, fh)[2], 0)
                self.assertEqual(server.getattr(node_b, fh)[2], errno.EBADF)
                # FUSE_GETATTR_FH is an explicit protocol flag. An fh field
                # without the flag must be ignored in favour of the node.
                self.assertEqual(
                    server.getattr(
                        node_b, fh, getattr_flags=0)[2], 0)
                self.assertEqual(
                    attr_from_payload(server.getattr(
                        node_b, fh, getattr_flags=0)[1])["ino"],
                    backend.get_node(node_b).inode)
                self.assertEqual(
                    server.read(999999, 0, 1)[2], errno.EBADF)
                self.assertEqual(
                    server.getattr(999999)[2], errno.ENOENT)


class ControllerApiRegressions(unittest.TestCase):
    def test_zero_lookup_path_api_reclaims_temporary_nodes_and_anchors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for index in range(30):
                (root / f"f{index}").write_text("x", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                before = backend.stats()["nodes"]
                for index in range(30):
                    self.assertEqual(
                        backend.getattr(f"/f{index}")["size"], 1)
                    self.assertEqual(
                        backend.read(f"/f{index}", 0, 1), b"x")
                os.symlink("f0", root / "link")
                self.assertEqual(backend.readlink_raw("/link"), "f0")
                backend.readdir("/")
                # A public resolve_path leaves an administration node until
                # the next path-level operation; that operation reclaims it.
                for index in range(10):
                    backend.resolve_path(f"/f{index}")
                backend.getattr("/f1")
                after = backend.stats()["nodes"]
                self.assertLessEqual(after, before + 2, (before, after))

                held = server.lookup("f0")
                backend.getattr("/f1")
                self.assertEqual(backend.get_node(held).nodeid, held)
                self.assertEqual(
                    backend.getattr(f"/f1")["ino"] != held, True)

    def test_resource_errno_is_not_reported_as_provider_identity_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("payload", encoding="utf-8")
            (root / "other").write_text("other", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                fh = server.open_file(node)
                real_read_text = Path.read_text

                def limited_text(path, *args, **kwargs):
                    if str(path).startswith("/proc/self/fdinfo/"):
                        raise OSError(
                            errno.EMFILE, "injected descriptor exhaustion")
                    return real_read_text(path, *args, **kwargs)

                with mock.patch.object(Path, "read_text", limited_text):
                    reply = server.call(
                        fuse_view.FUSE_LOOKUP, 1, b"other\x00")
                    self.assertEqual(reply[2], errno.EMFILE)
                    # Existing handles must stay readable during closure
                    # exhaustion and must not be converted to EIO.
                    self.assertEqual(
                        backend.read_handle(fh, 0, 100), b"payload")
                reply = server.call(
                    fuse_view.FUSE_LOOKUP, 1, b"other\x00")
                self.assertEqual(reply[2], 0)

                with mock.patch(
                        "multilinux.projection.reopen_readonly",
                        side_effect=ViewError(
                            errno.EMFILE, "injected handle exhaustion")):
                    reply = server.call(
                        fuse_view.FUSE_OPEN, node,
                        struct.pack("<II", os.O_RDONLY, 0))
                self.assertEqual(reply[2], errno.EMFILE)
                fh2 = server.open_file(node)
                self.assertEqual(server.read(fh2, 0, 100)[1], b"payload")

    def test_node_attr_uses_current_mode_while_synthetic_mode_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "file"
            path.write_text("x", encoding="utf-8")
            path.chmod(0o640)
            with fixture(root) as (_root, backend, server):
                node = server.lookup("file")
                self.assertEqual(
                    attr_from_payload(server.getattr(node)[1])["mode"] & 0o777,
                    0o640)
                path.chmod(0o600)
                self.assertEqual(
                    attr_from_payload(server.getattr(node)[1])["mode"] & 0o777,
                    0o600)

            source = root / "source"
            source.mkdir()
            mounts = [{"destination": "/missing/child", "source": str(source)}]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                missing = server.lookup("missing")
                mode = backend.node_attr_by_id(missing)["mode"]
                self.assertEqual(mode & 0o170000, 0o040000)
                self.assertEqual(mode & 0o777, 0o555)


class ReferenceLifecycle(unittest.TestCase):
    def test_forget_waits_for_open_handles_and_never_reuses_nodeid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("a", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("a")
                fh = server.open_file(node)
                self.assertIsNone(server.forget(node, 1))
                # The lookup reference is gone, but the live open handle keeps
                # the anchor, fd and node alive.
                self.assertEqual(backend.get_node(node).nodeid, node)
                server.release(fh)
                with self.assertRaises(ViewError) as raised:
                    backend.get_node(node)
                self.assertEqual(raised.exception.errno, errno.ENOENT)

                # A node with an outstanding lookup reference also survives
                # until FORGET, and its node id is never recycled.
                new_node = server.lookup("a")
                self.assertGreater(new_node, node)
                self.assertIsNone(server.forget(new_node, 1))
                with self.assertRaises(ViewError):
                    backend.get_node(new_node)
                newer_node = server.lookup("a")
                self.assertGreater(newer_node, new_node)
                self.assertIsNone(server.forget(newer_node, 1))

    def test_internal_reference_pins_anchor_until_released(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("a", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                node = server.lookup("a")
                fh = server.open_file(node)
                self.assertIsNone(server.forget(node, 1))
                backend.retain_node_internal(node)
                server.release(fh)
                # Open count is zero and nlookup is zero, but the internal
                # reference still protects the anchor.
                self.assertEqual(backend.get_node(node).nodeid, node)
                backend.release_node_internal(node)
                with self.assertRaises(ViewError):
                    backend.get_node(node)

    def test_batch_forget_has_no_reply_and_reclaims_all_listed_nodes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("b", "c"):
                (root / name).write_text(name, encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                node_b = server.lookup("b")
                node_c = server.lookup("c")
                self.assertIsNone(
                    server.batch_forget([(node_b, 1), (node_c, 1)]))
                for node in (node_b, node_c):
                    with self.assertRaises(ViewError):
                        backend.get_node(node)
                self.assertEqual(backend.stats()["nodes"], 1)


class StableInodeIdentity(unittest.TestCase):
    def test_external_inode_is_pure_identity_and_rejects_live_collision(self):
        from multilinux.projection import _external_inode
        key = ("mount:0", 42, 7, 9)
        first = _external_inode("view-a", key)
        self.assertEqual(first, _external_inode("view-a", key))
        self.assertGreaterEqual(first, 2)
        self.assertNotEqual(first, _external_inode("view-b", key))
        synthetic = _external_inode("view-a", ("synthetic", "/injected"))
        self.assertNotEqual(first, synthetic)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("a", encoding="utf-8")
            (root / "b").write_text("b", encoding="utf-8")
            with fixture(root) as (_root, backend, server):
                with mock.patch(
                        "multilinux.projection._external_inode",
                        return_value=123456789):
                    node_a = server.lookup("a")
                    self.assertEqual(
                        backend.get_node(node_a).inode, 123456789)
                    before_fds = open_fd_count()
                    failed = server.call(
                        fuse_view.FUSE_LOOKUP, 1, b"b\x00")
                self.assertEqual(failed[2], errno.EOVERFLOW)
                self.assertEqual(open_fd_count(), before_fds)
                self.assertEqual(
                    backend.stats()["inode_collisions"], 1)
                # Only the simultaneous active collision is refused; the
                # deterministic mapping remains usable after it is resolved.
                node_b = server.lookup("b")
                self.assertNotEqual(
                    backend.get_node(node_b).inode,
                    backend.get_node(node_a).inode)


class ResourceBudgets(unittest.TestCase):
    def test_node_budget_enospc_and_failed_lookup_does_not_leak_fd(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_text("a", encoding="utf-8")
            (root / "b").write_text("b", encoding="utf-8")
            with fixture(root, max_nodes=2) as (_root, backend, server):
                server.lookup("a")
                before = open_fd_count()
                failed = server.call(
                    fuse_view.FUSE_LOOKUP, 1, b"b\x00")
                self.assertEqual(failed[2], errno.ENOSPC)
                self.assertEqual(open_fd_count(), before)

    def test_handle_budget_emfile_and_release_reopens_capacity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("x", encoding="utf-8")
            with fixture(root, max_handles=1) as (_root, backend, server):
                node = server.lookup("file")
                fh = server.open_file(node)
                failed = server.call(
                    fuse_view.FUSE_OPEN, node,
                    struct.pack("<II", os.O_RDONLY, 0))
                self.assertEqual(failed[2], errno.EMFILE)
                server.release(fh)
                fh2 = server.open_file(node)
                self.assertNotEqual(fh, fh2)
                server.release(fh2)

    def test_permission_and_type_errors_are_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dir").mkdir()
            (root / "file").write_text("x", encoding="utf-8")
            no_read = root / "no-read"
            no_read.write_text("x", encoding="utf-8")
            no_read.chmod(0o000)
            with fixture(root) as (_root, backend, server):
                directory = server.lookup("dir")
                file_node = server.lookup("file")
                self.assertEqual(
                    server.call(fuse_view.FUSE_OPEN, directory,
                                struct.pack("<II", os.O_RDONLY, 0))[2],
                    errno.EISDIR)
                self.assertEqual(
                    server.call(fuse_view.FUSE_OPENDIR, file_node,
                                struct.pack("<I", os.O_RDONLY))[2],
                    errno.ENOTDIR)
                self.assertEqual(
                    server.call(fuse_view.FUSE_ACCESS, file_node,
                                struct.pack("<I", os.W_OK))[2], errno.EROFS)
                locked = server.lookup("no-read")
                self.assertEqual(
                    server.call(fuse_view.FUSE_ACCESS, locked,
                                struct.pack("<I", os.R_OK))[2],
                    errno.EACCES)
                self.assertEqual(
                    server.call(fuse_view.FUSE_LOOKUP, 999999,
                                b"x\x00")[2], errno.ENOENT)


class LegacyEntryPoint(unittest.TestCase):
    def test_frozen_legacy_imports_its_own_backend_and_keeps_cli_arguments(self):
        from multilinux import fuse_view_legacy
        from multilinux import view_backend_legacy
        self.assertEqual(
            fuse_view_legacy.ViewBackend.__module__,
            "multilinux.view_backend_legacy")
        self.assertIs(
            fuse_view_legacy.ViewBackend, view_backend_legacy.ViewBackend)
        self.assertNotEqual(
            fuse_view_legacy.ViewBackend.__module__,
            fuse_view.ViewBackend.__module__)

        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(ROOT / "src")
        for module in (
                "multilinux.fuse_view_legacy", "multilinux.fuse_view"):
            result = subprocess.run(
                [sys.executable, "-m", module, "--help"],
                cwd=str(ROOT), env=environment, capture_output=True,
                text=True, timeout=20, check=False)
            self.assertEqual(
                result.returncode, 0,
                f"{module}: {result.stderr or result.stdout}")
            self.assertIn("--merged-fd", result.stdout)
            self.assertIn("--mountpoint", result.stdout)


if __name__ == "__main__":
    unittest.main()
