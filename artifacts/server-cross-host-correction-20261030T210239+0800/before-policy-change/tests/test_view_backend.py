"""Read-only view backend: mount overlay, links, FD safety, uid mapping."""

import errno
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from unifier.view_backend import IdMap, ViewBackend, ViewError, build_id_map, read_subid


def _stat(fd_or_path):
    return os.fstat(fd_or_path) if isinstance(fd_or_path, int) else os.stat(fd_or_path)


def backend_for(root: Path, *, mounts=(), capabilities=("/proc", "/sys", "/dev", "/run"),
                id_map=None):
    merged = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    return ViewBackend(
        mountpoint=str(root / "view"), merged_fd=merged,
        merged_stat=os.fstat(merged), mounts=list(mounts),
        capabilities=list(capabilities), id_map=id_map or IdMap(
            os.getuid(), os.getgid(), 0, 0, 0, 0, 0, 0, "rootless-default"),
        view_uuid="00000000-0000-4000-8000-000000000010")


class LinkSemantics(unittest.TestCase):
    def test_absolute_and_up_relative_links_are_projected_inside_the_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "data").mkdir()
            (root / "etc/os-release").write_text("PRETTY_NAME=view\n", encoding="utf-8")
            (root / "data/file").write_text("data\n", encoding="utf-8")
            os.symlink("/etc/os-release", root / "etc/abs-link")
            os.symlink("../etc/os-release", root / "data/up-link")
            os.symlink("../../../outside", root / "data/escape")
            backend = backend_for(root)
            try:
                raw = backend.readlink_raw("/etc/abs-link")
                self.assertEqual(raw, "/etc/os-release")
                projected = backend.projected_link("/etc/abs-link", raw)
                self.assertEqual(projected, str(root / "view/etc/os-release"))
                self.assertFalse(projected.startswith("/etc/"))
                self.assertEqual(
                    backend.projected_link("/data/up-link", "../etc/os-release"),
                    str(root / "view/etc/os-release"))
                self.assertEqual(
                    backend.projected_link("/data/escape", "../../../outside"),
                    str(root / "view/outside"))
                self.assertEqual(backend.read("/etc/os-release", 0, 1000), b"PRETTY_NAME=view\n")
                with self.assertRaises(ViewError):
                    backend.read("/etc/abs-link", 0, 1000)
            finally:
                backend.close()

    def test_componentwise_link_resolution_preserves_dotdot_and_suffixes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "target").write_bytes(b"ROOT-CORRECT")
            (root / "a").mkdir()
            (root / "a/target").write_bytes(b"A-WRONG")
            (root / "b").mkdir()
            (root / "a/jump").symlink_to("../b")
            (root / "a/link").symlink_to("jump/../target")
            (root / "a/dangling").symlink_to("missing/../target")
            (root / "a/nondir").symlink_to("file/../target")
            (root / "a/deep-dangling").symlink_to(
                "missing/" + "../" * 128 + "host-only-sentinel")
            (root / "a/file").write_text("x", encoding="utf-8")
            (root / "a/regular").write_text("r", encoding="utf-8")
            (root / "a/dot").symlink_to("regular/.")
            (root / "a/slash").symlink_to("regular/")
            (root / "a/loop1").symlink_to("loop2")
            (root / "a/loop2").symlink_to("loop1")
            (root / "a/cycle").symlink_to("loop1/x")
            (root / "etc").mkdir()
            (root / "etc/os-release").write_text("VIEW-ETC\n", encoding="utf-8")
            (root / "abs-clamp").symlink_to("/../../etc/os-release")
            backend = backend_for(root)
            try:
                def projected(link):
                    raw = backend.readlink_raw(link)
                    return backend.projected_link(link, raw)

                def container(value):
                    self.assertTrue(value.startswith(backend.mountpoint))
                    return value[len(backend.mountpoint):] or "/"

                target = projected("/a/link")
                self.assertEqual(container(target), "/target")
                self.assertEqual(
                    backend.read(container(target), 0, 100), b"ROOT-CORRECT")
                self.assertEqual(
                    backend.readlink_raw("/a/link"), "jump/../target")

                clamped = projected("/abs-clamp")
                self.assertEqual(container(clamped), "/etc/os-release")
                self.assertEqual(
                    backend.read(container(clamped), 0, 100), b"VIEW-ETC\n")

                # Missing/non-directory prefixes with unresolved ".." must
                # fail explicitly: neither export an escaping path nor invent
                # a different target by deleting path components.
                for name, expected in (("dangling", errno.ENOENT),
                                       ("nondir", errno.ENOTDIR),
                                       ("deep-dangling", errno.ENOENT)):
                    with self.assertRaises(ViewError) as raised:
                        projected("/a/" + name)
                    self.assertEqual(raised.exception.errno, expected)

                # A trailing slash and "/." both require the final object to
                # be a directory; neither may collapse to the file name.
                self.assertTrue(projected("/a/dot").endswith("/a/regular/."))
                self.assertTrue(projected("/a/slash").endswith("/a/regular/."))

                with self.assertRaises(ViewError) as raised:
                    projected("/a/cycle")
                self.assertEqual(raised.exception.errno, errno.ELOOP)
            finally:
                backend.close()

    def test_link_dotdot_across_mount_is_resolved_in_the_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "target").write_bytes(b"ROOT")
            source = root / "source"
            source.mkdir()
            (source / "target").write_bytes(b"MOUNT")
            (source / "link").symlink_to("../target")
            backend = backend_for(root, mounts=[{
                "destination": "/mnt", "source": str(source)}])
            try:
                raw = backend.readlink_raw("/mnt/link")
                self.assertEqual(raw, "../target")
                projected = backend.projected_link("/mnt/link", raw)
                self.assertEqual(projected, str(root / "view/target"))
                self.assertEqual(backend.read("/target", 0, 100), b"ROOT")
            finally:
                backend.close()

    def test_backend_rejects_dotdot_and_capability_paths(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/os-release").write_text("x", encoding="utf-8")
            backend = backend_for(root)
            try:
                with self.assertRaisesRegex(ViewError, "must not contain"):
                    backend.getattr("/etc/../etc/os-release")
                with self.assertRaisesRegex(ViewError, "capability"):
                    backend.getattr("/proc/self")
                with self.assertRaisesRegex(ViewError, "capability"):
                    backend.read("/sys/kernel", 0, 100)
            finally:
                backend.close()


class MountOverlay(unittest.TestCase):
    def test_longest_destination_wins_and_source_replacement_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged = root / "merged"
            merged.mkdir()
            (merged / "data").mkdir()
            (merged / "data/file").write_text("merged", encoding="utf-8")
            (merged / "data/sub").mkdir()
            (merged / "data/sub/file").write_text("merged-sub", encoding="utf-8")
            source = root / "source"
            source.mkdir()
            (source / "file").write_text("source", encoding="utf-8")
            source_sub = root / "source-sub"
            source_sub.mkdir()
            (source_sub / "file").write_text("source-sub", encoding="utf-8")
            mounts = [
                {"destination": "/data", "source": str(source), "type": "bind",
                 "options": "rw"},
                {"destination": "/data/sub", "source": str(source_sub), "type": "bind",
                 "options": "ro"},
            ]
            backend = backend_for(merged, mounts=mounts)
            try:
                self.assertEqual(backend.read("/data/file", 0, 100), b"source")
                self.assertEqual(backend.read("/data/sub/file", 0, 100), b"source-sub")
                names = [item["name"] for item in backend.readdir("/data")]
                self.assertIn("file", names)
                source.rename(root / "old-source")
                source.mkdir()
                (source / "file").write_text("replaced", encoding="utf-8")
                with self.assertRaisesRegex(ViewError, "changed|replaced"):
                    backend.read("/data/file", 0, 100)
            finally:
                backend.close()

    def test_missing_source_and_file_mount_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            merged = root / "merged"
            merged.mkdir()
            missing = root / "missing"
            with self.assertRaisesRegex(ViewError, "unavailable"):
                backend_for(merged, mounts=[{
                    "destination": "/data", "source": str(missing),
                    "type": "bind", "options": ""}])
            host_file = root / "hosts"
            host_file.write_text("127.0.0.1 localhost\n", encoding="utf-8")
            backend = backend_for(merged, mounts=[{
                "destination": "/etc/hosts", "source": str(host_file),
                "type": "bind", "options": ""}])
            try:
                self.assertEqual(backend.read("/etc/hosts", 0, 100),
                                 b"127.0.0.1 localhost\n")
                with self.assertRaisesRegex(ViewError, "file mount"):
                    backend.read("/etc/hosts/child", 0, 10)
            finally:
                backend.close()

    def test_podman_storage_and_container_id_maps_compose(self):
        record = {"runtime": {"user_namespace": "keep-id"}}
        storage_uid = ["0 1000 1", "1 524288 1000", "1001 525288 10"]
        storage_gid = list(storage_uid)
        id_mappings = {
            "UidMap": ["0:1:1000", "1000:0:1", "1001:1001:10"],
            "GidMap": ["0:1:1000", "1000:0:1", "1001:1001:10"],
        }
        mapping = build_id_map(
            record, 1000, 1000, storage_uid_map=storage_uid,
            storage_gid_map=storage_gid, id_mappings=id_mappings)
        self.assertEqual(mapping.uid(1000), 1000)
        self.assertEqual(mapping.uid(524288), 0)
        self.assertEqual(mapping.uid(524289), 1)
        self.assertEqual(mapping.uid(525288), 1001)
        self.assertEqual(mapping.gid(525288), 1001)

    def test_default_storage_map_and_subid_username_fallback(self):
        record = {"runtime": {"user_namespace": "rootless-default"}}
        mapping = build_id_map(
            record, os.getuid(), os.getgid(),
            storage_uid_map=["0 1000 1", "1 524288 2"],
            storage_gid_map=["0 1000 1", "1 524288 2"])
        self.assertEqual(mapping.uid(1000), 0)
        self.assertEqual(mapping.uid(524288), 1)
        self.assertEqual(mapping.uid(524289), 2)
        ustart, ucount, gstart, gcount = read_subid(os.getuid(), os.getgid())
        self.assertGreater(ucount, 0)
        self.assertGreater(gcount, 0)
        fallback = build_id_map(record, os.getuid(), os.getgid())
        self.assertTrue(fallback.uid_ranges)

    def test_uid_gid_mapping_is_explicit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("x", encoding="utf-8")
            default = backend_for(root)
            keep = backend_for(root, id_map=IdMap(
                os.getuid(), os.getgid(), os.getuid(), os.getgid(),
                0, 0, 0, 0, "keep-id"))
            try:
                self.assertEqual(default.getattr("/file")["uid"], 0)
                self.assertEqual(keep.getattr("/file")["uid"], os.getuid())
                self.assertEqual(default.id_map.uid(os.getuid()), 0)
                self.assertEqual(keep.id_map.uid(os.getuid()), os.getuid())
            finally:
                default.close()
                keep.close()


if __name__ == "__main__":
    unittest.main()
