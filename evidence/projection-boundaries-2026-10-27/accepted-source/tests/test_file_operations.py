"""Durable managed copy/move: journal, no-overwrite publication, recovery."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from multilinux import cli
from multilinux.control import Control
from multilinux.errors import MultilinuxError
from multilinux.identity import binding_for
from multilinux.on_demand import process_stamp
from multilinux.views import ViewRegistry
from test_env import service_in


def _crash_copy(root, source_uuid, target_uuid, phase, *, operation="copy",
                source_relative="a.txt", target_relative="b.txt"):
    script = f"""
import os, sys
sys.path.insert(0, {str(ROOT / 'src')!r})
from pathlib import Path
from multilinux.control import Control
control = Control(Path({str(root)!r}))
def hook(point):
    if point == {phase!r}:
        os._exit(77)
control.{operation}_file(
    {source_uuid!r}, {source_relative!r}, {target_uuid!r}, {target_relative!r},
    fault_hook=hook)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if completed.returncode != 77:
        raise AssertionError(
            f"crash worker for {phase} exited {completed.returncode}: "
            f"{completed.stdout}\n{completed.stderr}")
    return completed


def _run_cli(argv, control):
    original = cli.control_factory
    cli.control_factory = lambda: control
    stdout, stderr = StringIO(), StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
    finally:
        cli.control_factory = original
    return code, stdout.getvalue(), stderr.getvalue()


class OperationFixture(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        (self.source / "a.txt").write_text("hello world\n", encoding="utf-8")
        self.control = Control(self.root)
        self.source_root = self.control.add_root(
            str(self.source), access_policy="read-only")
        self.target_root = self.control.add_root(str(self.target))

    def tearDown(self):
        self.temporary.cleanup()

    def crash(self, phase, *, operation="copy", source_relative="a.txt",
              target_relative="b.txt"):
        return _crash_copy(
            self.root, self.source_root["root_uuid"], self.target_root["root_uuid"],
            phase, operation=operation, source_relative=source_relative,
            target_relative=target_relative)

    def operation(self):
        records = self.control.list_file_operations()
        self.assertEqual(len(records), 1)
        return records[0]


class CopyCompletion(OperationFixture):
    def test_completed_copy_is_read_write_target_visible_to_file_control(self):
        record = self.control.copy_file(
            self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt")
        self.assertEqual(record["state"], "DONE")
        self.assertTrue(record["complete"])
        self.assertEqual(record["target"]["management_domain"], "host-project")
        self.assertEqual(record["provenance"]["source_root_uuid"],
                         self.source_root["root_uuid"])
        self.assertTrue(record["source_retained"])
        target = self.target / "b.txt"
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world\n")
        self.assertEqual(target.stat().st_nlink, 1)
        self.assertFalse(Path(record["real_paths"]["stage"]).exists())
        self.assertTrue((self.source / "a.txt").exists())
        self.assertFalse(record["capabilities"]["atomic_cross_filesystem_move"])
        self.assertFalse(record["capabilities"]["hash_and_rename_is_cas"])
        self.assertFalse(record["capabilities"]["directory_recursive"])
        self.assertFalse(record["capabilities"]["full_metadata_preservation"])
        # A's FileControl must accept the completed target: the internal stage
        # hard link is gone, so st_nlink is one rather than two.
        reference = self.control.resolve_file(
            self.target_root["root_uuid"], "b.txt", "document")
        self.assertEqual(reference["observed_version"]["nlink"], 1)
        self.assertEqual(reference["observed_version"]["size"], 12)
        self.assertEqual(len(self.control.operations.registry_collection.list()), 1)

    def test_completed_move_is_intentionally_source_cleanup_pending(self):
        record = self.control.move_file(
            self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt")
        self.assertEqual(record["state"], "SOURCE_CLEANUP_PENDING")
        self.assertTrue(record["target_copied"])
        self.assertFalse(record["complete"])
        self.assertTrue(record["source_retained"])
        self.assertEqual(record["source_cleanup"]["performed"], False)
        self.assertIn("source is retained", record["source_retention_reason"])
        self.assertTrue((self.source / "a.txt").exists())
        self.assertEqual((self.target / "b.txt").read_text(encoding="utf-8"),
                         "hello world\n")
        self.assertEqual((self.target / "b.txt").stat().st_nlink, 1)
        self.assertEqual(len(self.control.operations.registry_collection.list()), 1)

    def test_target_occupancy_is_preserved_independent_of_content(self):
        existing = self.target / "b.txt"
        existing.write_text("hello world\n", encoding="utf-8")
        inode = existing.stat().st_ino
        record = self.control.copy_file(
            self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt")
        self.assertEqual(record["state"], "CONFLICT")
        self.assertEqual(existing.stat().st_ino, inode)
        self.assertEqual(existing.read_text(encoding="utf-8"), "hello world\n")
        self.assertIsNone(record["real_paths"]["stage"])
        self.assertEqual(self.control.operations.registry_collection.list(), [])

    def test_source_xattr_is_not_inherited_and_source_is_unchanged(self):
        source = self.source / "a.txt"
        try:
            os.setxattr(source, "user.multilinux-test-origin", b"source")
        except OSError as exc:
            self.skipTest(f"user xattrs unavailable: {exc}")
        self.control.copy_file(
            self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt")
        target = self.target / "b.txt"
        with self.assertRaises(OSError):
            os.getxattr(target, "user.multilinux-test-origin")
        self.assertEqual(os.getxattr(source, "user.multilinux-test-origin"), b"source")
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world\n")


class Rejections(OperationFixture):
    def test_read_only_target_is_rejected_with_zero_content_side_effects(self):
        read_only_path = self.root / "read-only-target"
        read_only_path.mkdir()
        read_only = self.control.add_root(
            str(read_only_path), access_policy="read-only")
        with self.assertRaisesRegex(MultilinuxError, "read-only"):
            self.control.copy_file(
                self.source_root["root_uuid"], "a.txt",
                read_only["root_uuid"], "b.txt")
        self.assertEqual(list(read_only_path.iterdir()), [])
        self.assertEqual(self.control.list_file_operations(), [])
        self.assertFalse(self.control.operations.registry_collection.path.exists())

    def test_directory_symlink_and_hardlink_sources_are_refused(self):
        (self.source / "dir").mkdir()
        os.symlink(self.source / "a.txt", self.source / "link")
        os.link(self.source / "a.txt", self.source / "hard")
        for relative in ("dir", "link", "hard"):
            with self.subTest(relative=relative), self.assertRaises(MultilinuxError):
                self.control.copy_file(
                    self.source_root["root_uuid"], relative,
                    self.target_root["root_uuid"], relative + ".out")
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(self.control.list_file_operations(), [])

    def test_unknown_target_policy_and_unregistered_roots_are_refused(self):
        with self.assertRaises(MultilinuxError):
            self.control.copy_file(
                str(uuid.uuid4()), "a.txt",
                self.target_root["root_uuid"], "b.txt")
        with self.assertRaises(MultilinuxError):
            self.control.copy_file(
                self.source_root["root_uuid"], "a.txt",
                str(uuid.uuid4()), "b.txt")
        with self.assertRaises(MultilinuxError):
            self.control.copy_file(
                self.source_root["root_uuid"], "../a.txt",
                self.target_root["root_uuid"], "b.txt")
        self.assertEqual(self.control.list_file_operations(), [])


class CrashRecovery(OperationFixture):
    PHASES = (
        "INTENT_DURABLE", "STAGING", "STAGING_CREATED", "STAGING_READY",
        "TARGET_PUBLISHED", "REGISTRY_COMMITTED", "STAGE_CLEANED",
    )

    def test_each_persistent_phase_recovers_twice_without_duplicate_registry(self):
        for phase in self.PHASES:
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, target = root / "source", root / "target"
                source.mkdir(); target.mkdir()
                (source / "a.txt").write_text("phase bytes\n", encoding="utf-8")
                control = Control(root)
                source_root = control.add_root(str(source), access_policy="read-only")
                target_root = control.add_root(str(target))
                _crash_copy(root, source_root["root_uuid"], target_root["root_uuid"], phase)
                record = control.list_file_operations()[0]
                self.assertNotIn(record["state"], {"DONE", "SOURCE_CLEANUP_PENDING"})
                first = control.recover_file_operations(record["operation_uuid"])
                second = control.recover_file_operations(record["operation_uuid"])
                self.assertEqual(first[0]["state"], "DONE")
                self.assertEqual(second[0]["state"], "DONE")
                self.assertEqual(second[0]["recovery"]["action"], "terminal-noop")
                self.assertEqual((target / "b.txt").read_text(encoding="utf-8"),
                                 "phase bytes\n")
                self.assertEqual((target / "b.txt").stat().st_nlink, 1)
                self.assertTrue((source / "a.txt").exists())
                self.assertEqual(len(control.operations.registry_collection.list()), 1)

    def test_bare_recover_processes_pending_and_then_returns_empty(self):
        self.crash("STAGING_READY")
        first = self.control.recover_file_operations()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]["state"], "DONE")
        self.assertEqual(self.control.recover_file_operations(), [])
        self.assertEqual(len(self.control.operations.registry_collection.list()), 1)

    def test_move_crash_then_recovery_ends_source_cleanup_pending(self):
        self.crash("REGISTRY_COMMITTED", operation="move")
        record = self.operation()
        first = self.control.recover_file_operations(record["operation_uuid"])
        second = self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(first[0]["state"], "SOURCE_CLEANUP_PENDING")
        self.assertEqual(second[0]["state"], "SOURCE_CLEANUP_PENDING")
        self.assertTrue((self.source / "a.txt").exists())
        self.assertEqual((self.target / "b.txt").read_text(encoding="utf-8"),
                         "hello world\n")
        self.assertEqual((self.target / "b.txt").stat().st_nlink, 1)

    def test_concurrent_recovery_registers_only_once(self):
        self.crash("TARGET_PUBLISHED")
        operation = self.operation()["operation_uuid"]

        def recover():
            return Control(self.root).recover_file_operations(operation)[0]

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _index: recover(), (0, 1)))
        self.assertEqual({item["state"] for item in results}, {"DONE"})
        self.assertEqual(len(self.control.operations.registry_collection.list()), 1)


class ConservativeConflicts(OperationFixture):
    def test_same_content_unrelated_target_is_not_claimed(self):
        self.crash("STAGING_READY")
        target = self.target / "b.txt"
        target.write_text("hello world\n", encoding="utf-8")
        inode = target.stat().st_ino
        record = self.operation()
        stage = Path(record["real_paths"]["stage"])
        stage_inode = stage.stat().st_ino
        first = self.control.recover_file_operations(record["operation_uuid"])
        second = self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(first[0]["state"], "CONFLICT")
        self.assertEqual(second[0]["state"], "CONFLICT")
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world\n")
        self.assertEqual(stage.stat().st_ino, stage_inode)
        self.assertEqual(stage.read_text(encoding="utf-8"), "hello world\n")
        self.assertEqual(self.control.operations.registry_collection.list(), [])

    def test_same_content_target_replaced_after_publication_is_not_registered(self):
        self.crash("TARGET_PUBLISHED")
        record = self.operation()
        target = self.target / "b.txt"
        old_inode = target.stat().st_ino
        target.unlink()
        target.write_text("hello world\n", encoding="utf-8")
        replacement_inode = target.stat().st_ino
        self.assertNotEqual(old_inode, replacement_inode)
        stage = Path(record["real_paths"]["stage"])
        stage_inode = stage.stat().st_ino
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertIn(result[0]["state"], {"CONFLICT", "NEEDS_RECONCILIATION"})
        self.assertEqual(target.stat().st_ino, replacement_inode)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello world\n")
        self.assertEqual(stage.stat().st_ino, stage_inode)
        self.assertEqual(self.control.operations.registry_collection.list(), [])

    def test_terminal_recovery_never_claims_a_replaced_target_by_hash(self):
        record = self.control.copy_file(
            self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt")
        target = self.target / "b.txt"
        target.unlink()
        target.write_text("hello world\n", encoding="utf-8")
        replacement_inode = target.stat().st_ino
        registry_before = self.control.operations.registry_collection.list()
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(result[0]["state"], "DONE")
        self.assertEqual(result[0]["recovery"]["action"], "terminal-noop")
        self.assertEqual(target.stat().st_ino, replacement_inode)
        self.assertEqual(self.control.operations.registry_collection.list(),
                         registry_before)

    def test_in_place_target_tamper_is_not_registered_as_the_copied_bytes(self):
        self.crash("TARGET_PUBLISHED")
        record = self.operation()
        target = self.target / "b.txt"
        target.write_text("HELLO world\n", encoding="utf-8")
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(result[0]["state"], "NEEDS_RECONCILIATION")
        self.assertEqual(target.read_text(encoding="utf-8"), "HELLO world\n")
        self.assertEqual(self.control.operations.registry_collection.list(), [])

    def test_replaced_stage_is_not_claimed_or_unlinked(self):
        self.crash("STAGING_READY")
        record = self.operation()
        stage = Path(record["real_paths"]["stage"])
        old = stage.with_name(stage.name + ".old")
        stage.rename(old)
        stage.write_text("hello world\n", encoding="utf-8")
        replacement_inode = stage.stat().st_ino
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertIn(result[0]["state"], {"CONFLICT", "NEEDS_RECONCILIATION"})
        self.assertFalse((self.target / "b.txt").exists())
        self.assertEqual(stage.stat().st_ino, replacement_inode)
        self.assertEqual(stage.read_text(encoding="utf-8"), "hello world\n")
        self.assertTrue(old.exists())

    def test_source_change_preserves_source_and_stage(self):
        self.crash("STAGING_READY")
        record = self.operation()
        stage = Path(record["real_paths"]["stage"])
        (self.source / "a.txt").write_text("external writer bytes\n", encoding="utf-8")
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(result[0]["state"], "CONFLICT")
        self.assertEqual((self.source / "a.txt").read_text(encoding="utf-8"),
                         "external writer bytes\n")
        self.assertEqual(stage.read_text(encoding="utf-8"), "hello world\n")
        self.assertFalse((self.target / "b.txt").exists())

    def test_root_replacement_and_policy_change_are_reported_and_retained(self):
        self.crash("STAGING_READY")
        record = self.operation()
        stage = Path(record["real_paths"]["stage"])
        old_target = self.root / "target-old"
        self.target.rename(old_target)
        self.target.mkdir()
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertIn(result[0]["state"], {"CONFLICT", "NEEDS_RECONCILIATION"})
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual((old_target / stage.name).read_text(encoding="utf-8"),
                         "hello world\n")
        self.assertEqual((self.source / "a.txt").read_text(encoding="utf-8"),
                         "hello world\n")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir()
            (source / "a.txt").write_text("policy bytes\n", encoding="utf-8")
            control = Control(root)
            source_root = control.add_root(str(source), access_policy="read-only")
            target_root = control.add_root(str(target))
            _crash_copy(root, source_root["root_uuid"], target_root["root_uuid"],
                        "STAGING_READY")
            payload = json.loads(control.registry.path.read_text(encoding="utf-8"))
            payload["roots"][1]["access_policy"] = "read-only"
            control.registry.path.write_text(json.dumps(payload), encoding="utf-8")
            operation = control.list_file_operations()[0]["operation_uuid"]
            changed = control.recover_file_operations(operation)
            self.assertEqual(changed[0]["state"], "NEEDS_RECONCILIATION")
            self.assertFalse((target / "b.txt").exists())


class SchemaAndFilesystemBoundaries(OperationFixture):
    def test_unknown_journal_or_registry_schema_is_refused_without_mutation(self):
        self.crash("TARGET_PUBLISHED")
        record = self.operation()
        journal = self.control.operations._journal_path(record["operation_uuid"])
        original = journal.read_bytes()
        payload = json.loads(original)
        payload["schema_version"] = 99
        journal.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(MultilinuxError, "not supported"):
            self.control.list_file_operations()
        with self.assertRaisesRegex(MultilinuxError, "not supported"):
            self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(self.control.operations.registry_collection.list(), [])

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / "source", root / "target"
            source.mkdir(); target.mkdir()
            (source / "a.txt").write_text("schema bytes\n", encoding="utf-8")
            control = Control(root)
            source_root = control.add_root(str(source), access_policy="read-only")
            target_root = control.add_root(str(target))
            _crash_copy(root, source_root["root_uuid"], target_root["root_uuid"],
                        "TARGET_PUBLISHED")
            journal = control.operations._journal_path(
                control.list_file_operations()[0]["operation_uuid"])
            before = journal.read_bytes()
            control.operations.registry_collection.path.parent.mkdir(
                parents=True, exist_ok=True)
            control.operations.registry_collection.path.write_text(
                json.dumps({"schema_version": 99, "records": []}),
                encoding="utf-8")
            with self.assertRaisesRegex(MultilinuxError, "not supported"):
                control.recover_file_operations(
                    control.list_file_operations()[0]["operation_uuid"])
            self.assertEqual(journal.read_bytes(), before)

    def test_early_d_journal_or_registry_without_new_fields_still_reads(self):
        record = self.control.copy_file(
            self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt")
        journal = self.control.operations._journal_path(record["operation_uuid"])
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload.pop("execution", None)
        payload["source"].pop("parent_parts", None)
        payload["target"].pop("parent_parts", None)
        journal.write_text(json.dumps(payload), encoding="utf-8")
        registry = self.control.operations.registry_collection.path
        registry_payload = json.loads(registry.read_text(encoding="utf-8"))
        registry_payload["records"][0]["source"].pop("parent_parts", None)
        registry_payload["records"][0]["target"].pop("parent_parts", None)
        registry.write_text(json.dumps(registry_payload), encoding="utf-8")
        status = self.control.file_operation(record["operation_uuid"])
        self.assertEqual(status["state"], "DONE")
        self.assertFalse(status["execution"]["authorized"])
        self.assertEqual(len(self.control.operations.registry_collection.list()), 1)

    @unittest.skipUnless(os.path.isdir("/dev/shm"), "needs /dev/shm")
    def test_cross_filesystem_copy_stages_in_target_filesystem(self):
        remote = Path(tempfile.mkdtemp(prefix="ml-d-remote-", dir="/dev/shm"))
        self.addCleanup(shutil.rmtree, remote, True)
        if remote.stat().st_dev == self.source.stat().st_dev:
            self.skipTest("source and /dev/shm are on the same device")
        target_root = self.control.add_root(str(remote))
        _crash_copy(self.root, self.source_root["root_uuid"],
                    target_root["root_uuid"], "STAGING_READY",
                    target_relative="cross.txt")
        record = self.operation()
        stage = Path(record["real_paths"]["stage"])
        self.assertEqual(stage.stat().st_dev, remote.stat().st_dev)
        self.assertNotEqual(stage.stat().st_dev, self.source.stat().st_dev)
        result = self.control.recover_file_operations(record["operation_uuid"])
        self.assertEqual(result[0]["state"], "DONE")
        self.assertEqual((remote / "cross.txt").read_text(encoding="utf-8"),
                         "hello world\n")

    def test_cli_copy_status_list_and_recover(self):
        code, out, err = _run_cli([
            "file", "copy", self.source_root["root_uuid"], "a.txt",
            self.target_root["root_uuid"], "b.txt"], self.control)
        self.assertEqual(code, 0, err)
        record = json.loads(out)
        self.assertEqual(record["state"], "DONE")
        code, out, err = _run_cli(
            ["file", "status", record["operation_uuid"]], self.control)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["state"], "DONE")
        code, out, err = _run_cli(["file", "list"], self.control)
        self.assertEqual(code, 0, err)
        self.assertEqual(len(json.loads(out)), 1)
        code, out, err = _run_cli(
            ["file", "recover", record["operation_uuid"]], self.control)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)[0]["recovery"]["action"], "terminal-noop")


class FuseSourceRootRegistration(unittest.TestCase):
    def _view(self, root, service, mount):
        record = service.store.load("debian13")
        now = service.clock()
        return {
            "schema_version": 1,
            "view_uuid": str(uuid.uuid4()),
            "environment_id": "debian13",
            "environment_binding": binding_for(record),
            "state": "ready",
            "mountpoint": str(mount),
            "view_dir": str(root / ".lab/state/views" / str(uuid.uuid4())),
            "owner_pid": os.getpid(),
            "owner_stamp": process_stamp(os.getpid()),
            "worker_pid": None,
            "worker_stamp": None,
            "broker_pid": None,
            "broker_stamp": None,
            "control_socket": None,
            "storage_socket": None,
            "spec_path": None,
            "log_path": None,
            "ready_path": None,
            "storage_journal": None,
            "storage_nonce": uuid.uuid4().hex,
            "storage_ref_state": "none",
            "broker_launcher_pid": None,
            "broker_launcher_stamp": None,
            "readonly": True,
            "backend": "podman-overlay",
            "raw_link_semantics": "projected-link",
            "uid_map": {"host_uid": os.getuid()},
            "created_at": now,
            "updated_at": now,
        }

    def test_empty_view_mount_is_not_registered_as_a_data_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            mount = root / "empty-view"
            mount.mkdir()
            view = self._view(root, service, mount)
            ViewRegistry(root).add(view)
            control = Control(root)
            with self.assertRaisesRegex(MultilinuxError, "not a live FUSE mount"):
                control.add_root(str(mount), source_view_uuid=view["view_uuid"])
            self.assertEqual(control.list_roots(), [])
