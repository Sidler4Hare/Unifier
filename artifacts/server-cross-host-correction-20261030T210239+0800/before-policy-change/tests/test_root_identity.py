"""Root identity survives Btrfs device renumbering without accepting replacements."""

import errno
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.control import Control
from unifier.errors import UnifierError
from unifier.project_sessions import ProjectSessions
from unifier.registry import DataRootRegistry
from unifier import root_identity
from test_project_sessions import ready


IDENTITY = {
    "kind": "btrfs-v1",
    "filesystem_uuid": "11111111-2222-3333-4444-555555555555",
    "subvolume_uuid": "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
    "subvolume_id": 257,
}


def rewrite_root(registry, **changes):
    payload = json.loads(registry.path.read_text())
    payload["roots"][0].update(changes)
    registry.path.write_text(json.dumps(payload))


class BtrfsIoctls(unittest.TestCase):
    def test_decodes_filesystem_and_subvolume_from_the_same_fd(self):
        def ioctl(fd, request, buffer, mutate):
            self.assertEqual(fd, 42)
            self.assertTrue(mutate)
            self.assertFalse(any(buffer))
            if request == root_identity.FS_INFO:
                self.assertEqual(len(buffer), 1024)
                buffer[16:32] = uuid.UUID(IDENTITY["filesystem_uuid"]).bytes
            elif request == root_identity.GET_SUBVOL_INFO:
                self.assertEqual(len(buffer), 504)
                struct.pack_into("=Q", buffer, 0, IDENTITY["subvolume_id"])
                buffer[296:312] = uuid.UUID(IDENTITY["subvolume_uuid"]).bytes
            else:
                self.fail(f"unexpected ioctl {request}")
            return 0
        with patch.object(root_identity.fcntl, "ioctl", side_effect=ioctl) as call:
            self.assertEqual(root_identity._btrfs_identity(42), IDENTITY)
            self.assertEqual(call.call_count, 2)

    def test_only_non_btrfs_enotty_uses_the_legacy_contract(self):
        with patch.object(root_identity.fcntl, "ioctl",
                          side_effect=OSError(errno.ENOTTY, "not Btrfs")):
            self.assertIsNone(root_identity._btrfs_identity(42))
        for code in (errno.EACCES, errno.EPERM, errno.EIO, errno.EINVAL):
            with self.subTest(code=code), patch.object(
                    root_identity.fcntl, "ioctl", side_effect=OSError(code, "failure")):
                with self.assertRaises(OSError):
                    root_identity._btrfs_identity(42)

    def test_subvolume_query_failure_cannot_drop_the_pin(self):
        with patch.object(root_identity.fcntl, "ioctl", side_effect=[0, OSError(errno.ENOTTY, "failure")]):
            with self.assertRaises(OSError):
                root_identity._btrfs_identity(42)


class PersistentRoots(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.identity = patch.object(root_identity, "_btrfs_identity", return_value=dict(IDENTITY))
        self.identity.start()
        self.addCleanup(self.identity.stop)

    def create_root(self):
        data = self.directory / "data"
        data.mkdir()
        (data / "note.txt").write_text("preserved\n")
        registry = DataRootRegistry(self.directory)
        return registry, registry.add(str(data)), data

    def test_device_drift_resolves_files_without_rewriting_the_registry(self):
        registry, record, data = self.create_root()
        actual_dev = data.stat().st_dev
        rewrite_root(registry, root_dev=actual_dev + 1000)
        before = registry.path.read_bytes()
        resolved = registry.show(record["root_uuid"])
        self.assertEqual(resolved["root_dev"], actual_dev)
        self.assertEqual(resolved["root_identity"], IDENTITY)
        matched, relative = registry.match(str(data / "note.txt"))
        self.assertEqual(matched, resolved)
        self.assertEqual(relative, "note.txt")
        reference = Control(self.directory).resolve_file(record["root_uuid"], "note.txt")
        self.assertEqual(reference["observed_version"]["dev"], actual_dev)
        self.assertEqual(registry.path.read_bytes(), before)

    def test_project_launch_planning_accepts_old_device_pin_without_starting_runtime(self):
        service, runtime, _routes, project, record, document = ready(self.directory)
        registry = DataRootRegistry(self.directory)
        rewrite_root(registry, root_dev=record["root_dev"] + 1000)
        before_registry = registry.path.read_bytes()
        before_events = list(runtime.events)
        plan = ProjectSessions(self.directory, service).prepare(
            project["project_uuid"], cwd=str(document.parent), argv=["/bin/true"])
        self.assertEqual(plan["root_dev"], document.stat().st_dev)
        self.assertEqual(plan["root_ino"], record["root_ino"])
        self.assertEqual(runtime.events, before_events)
        self.assertEqual(registry.path.read_bytes(), before_registry)

    def test_distinct_subvolumes_with_reused_device_and_inode_remain_distinct(self):
        registry, record, _data = self.create_root()
        payload = json.loads(registry.path.read_text())
        payload["roots"].append({
            **record, "root_uuid": str(uuid.uuid4()),
            "locator": str(self.directory / "another-subvolume"),
            "root_identity": {**IDENTITY, "subvolume_id": 258},
        })
        registry.path.write_text(json.dumps(payload))
        self.assertEqual(len(registry.list()), 2)

    def test_stable_alias_is_refused_even_if_stored_device_is_stale(self):
        registry, record, data = self.create_root()
        rewrite_root(registry, root_dev=record["root_dev"] + 1000)
        # Model another bind path to the same persistent inode, without mounting.
        alias = self.directory / "alias"
        alias.mkdir()
        observed = data.stat()
        original_stat = os.stat
        def alias_stat(path, *args, **kwargs):
            return observed if os.fspath(path) == str(alias) else original_stat(path, *args, **kwargs)
        with patch("unifier.registry.os.stat", side_effect=alias_stat), patch(
                "unifier.registry.observe_root", return_value=(observed, dict(IDENTITY))):
            with self.assertRaisesRegex(UnifierError, "alias of the same inode"):
                registry.add(str(alias))

    def test_filesystem_and_subvolume_replacements_are_refused_even_if_dev_is_reused(self):
        registry, record, _data = self.create_root()
        for key, value in (("filesystem_uuid", str(uuid.uuid4())),
                           ("subvolume_uuid", str(uuid.uuid4())),
                           ("subvolume_id", 258)):
            with self.subTest(key=key), patch.object(
                    root_identity, "_btrfs_identity", return_value={**IDENTITY, key: value}):
                before = registry.path.read_bytes()
                with self.assertRaisesRegex(UnifierError, "replaced"):
                    registry.show(record["root_uuid"])
                self.assertEqual(registry.path.read_bytes(), before)

    def test_directory_and_symlink_replacements_are_refused_after_device_drift(self):
        registry, record, data = self.create_root()
        rewrite_root(registry, root_dev=record["root_dev"] + 1000)
        old = self.directory / "old-data"
        data.rename(old)
        data.mkdir()
        with self.assertRaisesRegex(UnifierError, "replaced"):
            registry.show(record["root_uuid"])
        data.rmdir()
        data.symlink_to(old, target_is_directory=True)
        with self.assertRaisesRegex(UnifierError, "symbolic link"):
            registry.show(record["root_uuid"])

    def test_missing_or_unreadable_identity_is_refused_without_state_changes(self):
        registry, record, _data = self.create_root()
        before = registry.path.read_bytes()
        with patch.object(root_identity, "_btrfs_identity", return_value=None):
            with self.assertRaisesRegex(UnifierError, "replaced"):
                registry.show(record["root_uuid"])
        with patch.object(root_identity, "_btrfs_identity", side_effect=OSError(errno.EIO, "failure")):
            with self.assertRaisesRegex(UnifierError, "unavailable"):
                registry.show(record["root_uuid"])
        self.assertEqual(registry.path.read_bytes(), before)

    def test_legacy_upgrade_requires_matching_device_and_inode_and_is_idempotent(self):
        registry, record, data = self.create_root()
        payload = json.loads(registry.path.read_text())
        del payload["roots"][0]["root_identity"]
        payload["roots"][0]["root_dev"] += 1000
        registry.path.write_text(json.dumps(payload))
        stale = registry.path.read_bytes()
        # Legacy records do not acquire continuity just because an inode matches.
        with self.assertRaisesRegex(UnifierError, "replaced"):
            registry.pin_identity(record["root_uuid"])
        with self.assertRaisesRegex(UnifierError, "replaced"):
            Control(self.directory).resolve_file(record["root_uuid"], "note.txt")
        self.assertEqual(registry.path.read_bytes(), stale)
        rewrite_root(registry, root_dev=data.stat().st_dev)
        upgraded = registry.pin_identity(record["root_uuid"])
        self.assertEqual(upgraded["root_identity"], IDENTITY)
        before = registry.path.read_bytes()
        self.assertEqual(registry.pin_identity(record["root_uuid"]), upgraded)
        self.assertEqual(registry.path.read_bytes(), before)

    def test_malformed_persistent_pin_is_not_treated_as_a_legacy_record(self):
        registry, record, _data = self.create_root()
        cases = (None, {}, [], {**IDENTITY, "kind": "unknown"},
                 {**IDENTITY, "filesystem_uuid": "invalid"},
                 {**IDENTITY, "subvolume_id": True},
                 {**IDENTITY, "subvolume_id": 0})
        for value in cases:
            with self.subTest(value=value):
                rewrite_root(registry, root_identity=value)
                before = registry.path.read_bytes()
                with self.assertRaises(UnifierError):
                    registry.show(record["root_uuid"])
                self.assertEqual(registry.path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
