"""Directory-entry path versus pinned object identity after an external rename.

These are offline fixtures: a real temporary directory and the protocol
capture, not a FUSE mount or a container. An open handle keeps the inode it
pinned. A lookup adopts a new path only when its parent path still names that
parent. A stale path does not select mounts and does not replace a path that
was reached through the old directory node.
"""

import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from unifier import fuse_view
from unifier.view_backend import ViewError
from test_projection_support import fixture, parse_dirents


def _names(server, node) -> list[str]:
    fh = server.opendir(node)
    try:
        found = []
        cookie = 0
        while True:
            reply = server.readdir(fh, cookie)
            if reply[2] != 0:
                raise AssertionError(f"readdir failed: {reply!r}")
            page = parse_dirents(reply[1])
            if not page:
                return found
            found.extend(item["name"] for item in page)
            cookie = page[-1]["cookie"]
    finally:
        server.releasedir(fh)


def _readlink(server, node) -> tuple[int, str]:
    reply = server.call(fuse_view.FUSE_READLINK, node, b"")
    text = reply[1].decode() if reply[1] else ""
    return reply[2], text


class RenamePathContext(unittest.TestCase):
    def test_relative_symlink_adopts_confirmed_parent_and_keeps_anchor(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "target").write_text(name, encoding="utf-8")
            (root / "a/link").symlink_to("target")
            with fixture(root) as (_root, backend, server):
                parent = server.lookup("a")
                link = server.lookup("link", parent)
                node = backend.get_node(link)
                anchor = node.anchor_fd
                self.assertEqual(node.path, "/a/link")
                (root / "a/link").rename(root / "b/link")

                # No confirming lookup yet: the last confirmed entry stays,
                # which is also how an in-place replacement keeps projecting.
                errno_before, actual_before = _readlink(server, link)
                self.assertEqual(errno_before, 0)
                self.assertEqual(actual_before, str(root / "view/a/target"))

                other = server.lookup("b")
                again = server.lookup("link", other)
                self.assertEqual(again, link)
                refreshed = backend.get_node(link)
                self.assertEqual(refreshed.anchor_fd, anchor)
                self.assertEqual(refreshed.path, "/b/link")
                self.assertEqual(backend.readlink_node(link), "target")
                errno_after, actual_after = _readlink(server, link)
                self.assertEqual(errno_after, 0)
                self.assertEqual(actual_after, str(root / "view/b/target"))

    def test_nested_mount_stays_on_the_configured_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            source = root / "source"
            source.mkdir()
            (source / "payload").write_bytes(b"mounted")
            mounts = [{
                "destination": "/a/nested",
                "source": str(source),
                "type": "bind",
                "options": "ro",
            }]
            with fixture(root, mounts=mounts) as (_root, backend, server):
                directory = server.lookup("a")
                nested = server.lookup("nested", directory)
                payload = server.lookup("payload", nested)
                fh = server.open_file(payload)
                self.assertEqual(server.read(fh, 0, 100)[1], b"mounted")
                frozen = server.opendir(directory)
                try:
                    self.assertIn("nested", _names(server, directory))
                    (root / "a").rename(root / "b")
                    frozen_page = parse_dirents(server.readdir(frozen, 0)[1])
                    self.assertIn(
                        "nested", [item["name"] for item in frozen_page])
                    self.assertEqual(server.read(fh, 0, 100)[1], b"mounted")
                    # A new snapshot of the old node must not keep injecting
                    # the mount that was configured at /a/nested.
                    self.assertNotIn("nested", _names(server, directory))
                    again = server.lookup("b")
                    self.assertEqual(again, directory)
                    self.assertEqual(backend.get_node(directory).path, "/b")
                    self.assertNotIn("nested", _names(server, directory))
                    names = [item["name"] for item in backend.readdir("/b")]
                    self.assertNotIn("nested", names)
                    with self.assertRaises(ViewError) as caught:
                        backend.read("/b/nested/payload", 0, 100)
                    self.assertEqual(caught.exception.errno, errno.ENOENT)
                    self.assertEqual(server.read(fh, 0, 100)[1], b"mounted")
                finally:
                    server.releasedir(frozen)
                    server.release(fh)

    def test_stale_parent_does_not_rewrite_a_pinned_child_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "dir").mkdir()
            (root / "dir/file").write_bytes(b"OLD-DIR")
            with fixture(root) as (_root, backend, server):
                old_dir = server.lookup("dir")
                old_file = server.lookup("file", old_dir)
                fh = server.open_file(old_file)
                anchor = backend.get_node(old_file).anchor_fd
                (root / "dir").rename(root / "old-dir")
                (root / "dir").mkdir()
                (root / "dir/file").write_bytes(b"NEW-DIR")

                same = server.lookup("file", old_dir)
                self.assertEqual(same, old_file)
                pinned = backend.get_node(old_file)
                self.assertEqual(pinned.path, "/dir/file")
                self.assertEqual(pinned.anchor_fd, anchor)
                self.assertEqual(server.read(fh, 0, 100)[1], b"OLD-DIR")
                self.assertIn("file", _names(server, old_dir))

                renamed = server.lookup("old-dir")
                self.assertEqual(renamed, old_dir)
                self.assertEqual(backend.get_node(old_dir).path, "/old-dir")
                adopted = server.lookup("file", renamed)
                self.assertEqual(adopted, old_file)
                self.assertEqual(backend.get_node(old_file).path, "/old-dir/file")
                self.assertEqual(backend.get_node(old_file).anchor_fd, anchor)
                self.assertEqual(server.read(fh, 0, 100)[1], b"OLD-DIR")

                new_dir = server.lookup("dir")
                self.assertNotEqual(new_dir, old_dir)
                new_file = server.lookup("file", new_dir)
                self.assertNotEqual(new_file, old_file)
                self.assertEqual(
                    server.read(server.open_file(new_file), 0, 100)[1],
                    b"NEW-DIR")
                server.release(fh)

    def test_hardlink_alias_updates_path_without_moving_the_open_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").write_bytes(b"SAME")
            os.link(root / "a", root / "b")
            with fixture(root) as (_root, backend, server):
                first = server.lookup("a")
                fh = server.open_file(first)
                anchor = backend.get_node(first).anchor_fd
                second = server.lookup("b")
                self.assertEqual(second, first)
                node = backend.get_node(first)
                self.assertEqual(node.path, "/b")
                self.assertEqual(node.anchor_fd, anchor)
                self.assertEqual(server.read(fh, 0, 10)[1], b"SAME")
                server.release(fh)

    def test_relative_hardlinked_symlink_stays_ambiguous_after_path_update(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "target").write_text(name, encoding="utf-8")
            os.symlink("target", root / "a/link")
            os.link(root / "a/link", root / "b/link", follow_symlinks=False)
            with fixture(root) as (_root, backend, server):
                parent = server.lookup("a")
                link = server.lookup("link", parent)
                other = server.lookup("b")
                alias = server.lookup("link", other)
                self.assertEqual(alias, link)
                self.assertEqual(backend.get_node(link).path, "/b/link")
                errno_seen, _text = _readlink(server, link)
                self.assertEqual(errno_seen, errno.EOPNOTSUPP)

    def test_forget_reclaims_symlink_and_new_node_projects_the_new_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("a", "b"):
                (root / name).mkdir()
                (root / name / "target").write_text(name, encoding="utf-8")
            (root / "a/link").symlink_to("target")
            with fixture(root) as (_root, backend, server):
                parent = server.lookup("a")
                link = server.lookup("link", parent)
                (root / "a/link").rename(root / "b/link")
                self.assertIsNone(server.forget(link, 1))
                with self.assertRaises(ViewError) as caught:
                    backend.get_node(link)
                self.assertEqual(caught.exception.errno, errno.ENOENT)

                other = server.lookup("b")
                fresh = server.lookup("link", other)
                self.assertGreater(fresh, link)
                self.assertEqual(backend.get_node(fresh).path, "/b/link")
                errno_seen, actual = _readlink(server, fresh)
                self.assertEqual(errno_seen, 0)
                self.assertEqual(actual, str(root / "view/b/target"))
                self.assertIsNone(server.batch_forget([
                    (parent, 1), (other, 1), (fresh, 1)]))
                self.assertEqual(backend.stats()["nodes"], 1)

    def test_open_handle_blocks_reclaim_across_rename(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "a").mkdir()
            (root / "a/file").write_bytes(b"PINNED")
            with fixture(root) as (_root, backend, server):
                directory = server.lookup("a")
                file_node = server.lookup("file", directory)
                fh = server.open_file(file_node)
                anchor = backend.get_node(file_node).anchor_fd
                self.assertIsNone(server.forget(file_node, 1))
                self.assertEqual(backend.get_node(file_node).nodeid, file_node)
                (root / "a").rename(root / "b")

                renamed = server.lookup("b")
                self.assertEqual(renamed, directory)
                again = server.lookup("file", renamed)
                self.assertEqual(again, file_node)
                node = backend.get_node(file_node)
                self.assertEqual(node.path, "/b/file")
                self.assertEqual(node.anchor_fd, anchor)
                self.assertEqual(server.read(fh, 0, 20)[1], b"PINNED")

                server.release(fh)
                self.assertIsNone(server.forget(file_node, 1))
                with self.assertRaises(ViewError):
                    backend.get_node(file_node)
                fresh = server.lookup("file", renamed)
                self.assertGreater(fresh, file_node)
                self.assertEqual(backend.get_node(fresh).path, "/b/file")
                self.assertEqual(
                    server.read(server.open_file(fresh), 0, 20)[1], b"PINNED")
                server.forget(fresh, 1)
                server.forget(directory, 1)


if __name__ == "__main__":
    unittest.main()
