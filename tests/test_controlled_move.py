"""Native controlled-move cleanup: authorization, leases, guards, recovery.

These tests use ordinary temporary files on the host filesystem.  They do not
claim cross-filesystem atomicity or power-loss durability; the controlled
protocol here is the same bounded lease-and-quarantine path used by Control.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from unifier.control import Control
from unifier.errors import UnifierError
from unifier.lease import LeaseBroken, LeaseUnsupported, WriteLease


PAYLOAD = (b"controlled move payload\n" * 4096)[:98304]


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _crash_move(root: Path, source_uuid: str, target_uuid: str, phase: str, *,
                cleanup_source: bool = True,
                source_relative: str = "payload.bin",
                target_relative: str = "published.bin"):
    script = f"""
import os, sys
sys.path.insert(0, {str(ROOT / 'src')!r})
from pathlib import Path
from unifier.control import Control
control = Control(Path({str(root)!r}))
def hook(point):
    if point == {phase!r}:
        os._exit(77)
record = control.move_file(
    {source_uuid!r}, {source_relative!r}, {target_uuid!r}, {target_relative!r},
    cleanup_source={cleanup_source!r}, fault_hook=hook)
raise SystemExit(0)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script], cwd=ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if completed.returncode != 77:
        raise AssertionError(
            f"crash worker for {phase} exited {completed.returncode}: "
            f"{completed.stdout[-1000:]}\n{completed.stderr[-2000:]}")
    return completed


def _files_with_digest(root: Path, expected: str) -> list[Path]:
    return [path for path in root.rglob("*")
            if path.is_file() and digest(path) == expected]


@unittest.skipUnless(
    sys.platform.startswith("linux") and hasattr(fcntl, "F_SETLEASE"),
    "controlled source cleanup needs Linux F_SETLEASE")
class ControlledMoveNative(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.target = self.root / "target"
        self.source.mkdir()
        self.target.mkdir()
        self.source_file = self.source / "payload.bin"
        self.source_file.write_bytes(PAYLOAD)
        self.expected = hashlib.sha256(PAYLOAD).hexdigest()
        self.control = Control(self.root)
        self.source_root = self.control.add_root(
            str(self.source), access_policy="read-write",
            cleanup_policy="controlled")
        self.target_root = self.control.add_root(
            str(self.target), access_policy="read-write")

    def tearDown(self):
        self.temporary.cleanup()

    def move(self, **kwargs):
        return self.control.move_file(
            self.source_root["root_uuid"], "payload.bin",
            self.target_root["root_uuid"], "published.bin", **kwargs)

    def assert_source_bytes_preserved(self):
        self.assertTrue(
            _files_with_digest(self.source, self.expected),
            "no copy of the original source bytes remains under the source root")

    def test_normal_controlled_move_deletes_only_the_captured_source(self):
        observed = []
        record = self.move(cleanup_source=True, fault_hook=observed.append)
        self.assertEqual(record["state"], "DONE")
        self.assertTrue(record["complete"])
        self.assertTrue(record["source_cleanup"]["performed"])
        self.assertFalse(record["source_retained"])
        self.assertEqual(record["source_cleanup"]["state"], "done")
        self.assertFalse(self.source_file.exists())
        published = self.target / "published.bin"
        self.assertEqual(digest(published), self.expected)
        self.assertFalse(list(self.source.glob(".ml-quarantine-*")))
        for state in ("SOURCE_QUARANTINE_READY", "SOURCE_CAPTURED",
                      "SOURCE_ISOLATED", "SOURCE_DELETION_INTENT",
                      "SOURCE_REMOVED", "DONE"):
            self.assertIn(state, observed)
        self.assertEqual(
            record["source_cleanup"]["quarantine"]["captured_sha256"],
            self.expected)
        self.assertFalse(record["source_cleanup"]["lease"]["acquired"])

    def test_move_without_explicit_authorization_retains_a_controlled_source(self):
        record = self.move()
        self.assertEqual(record["state"], "SOURCE_CLEANUP_PENDING")
        self.assertFalse(record["complete"])
        self.assertTrue(record["source_retained"])
        self.assertFalse(record["source_cleanup"]["requested"])
        self.assertFalse(record["source_cleanup"]["performed"])
        self.assertEqual(self.source_file.read_bytes(), PAYLOAD)
        self.assertEqual(digest(self.target / "published.bin"), self.expected)
        self.assertFalse(list(self.source.glob(".ml-quarantine-*")))

    def test_old_journal_without_cleanup_block_never_gains_delete_rights(self):
        _crash_move(
            self.root, self.source_root["root_uuid"], self.target_root["root_uuid"],
            "TARGET_PUBLISHED", cleanup_source=False)
        pending = self.control.list_file_operations()
        self.assertEqual(len(pending), 1)
        operation_uuid = pending[0]["operation_uuid"]
        journal = self.control.operations._journal_path(operation_uuid)
        payload = json.loads(journal.read_text(encoding="utf-8"))
        payload.pop("source_cleanup", None)
        journal.write_text(json.dumps(payload), encoding="utf-8")

        recovered = self.control.recover_file_operations(operation_uuid)
        self.assertEqual(recovered[0]["state"], "SOURCE_CLEANUP_PENDING")
        self.assertFalse(recovered[0]["complete"])
        self.assertTrue(recovered[0]["source_retained"])
        self.assertEqual(self.source_file.read_bytes(), PAYLOAD)
        self.assertEqual(digest(self.target / "published.bin"), self.expected)
        again = self.control.recover_file_operations(operation_uuid)
        self.assertEqual(again[0]["recovery"]["action"], "terminal-noop")
        self.assertEqual(self.source_file.read_bytes(), PAYLOAD)

    def test_read_only_root_cannot_be_registered_for_controlled_cleanup(self):
        read_only = self.root / "read-only"
        read_only.mkdir()
        with self.assertRaisesRegex(UnifierError, "read-write"):
            self.control.add_root(
                str(read_only), access_policy="read-only",
                cleanup_policy="controlled")
        retain_root = self.control.add_root(
            str(read_only), access_policy="read-only", cleanup_policy="retain")
        before = self.control.list_file_operations()
        with self.assertRaisesRegex(UnifierError, "not registered"):
            self.control.move_file(
                retain_root["root_uuid"], "payload.bin",
                self.target_root["root_uuid"], "other.bin",
                cleanup_source=True)
        self.assertEqual(self.control.list_file_operations(), before)

    def test_late_policy_revocation_retains_the_captured_bytes(self):
        def revoke(point):
            if point != "SOURCE_DELETION_INTENT":
                return
            path = self.control.registry.path
            payload = json.loads(path.read_text(encoding="utf-8"))
            next(item for item in payload["roots"]
                 if item["root_uuid"] == self.source_root["root_uuid"]
                 )["cleanup_policy"] = "retain"
            path.write_text(json.dumps(payload), encoding="utf-8")

        record = self.move(cleanup_source=True, fault_hook=revoke)
        self.assertEqual(record["state"], "SOURCE_CLEANUP_PENDING")
        self.assertFalse(record["complete"])
        self.assertTrue(record["source_retained"])
        self.assertFalse(self.source_file.exists())
        self.assert_source_bytes_preserved()
        self.assertEqual(digest(self.target / "published.bin"), self.expected)
        recovered = self.control.recover_file_operations(record["operation_uuid"])
        self.assertTrue(recovered[0]["source_retained"])
        self.assert_source_bytes_preserved()

    def test_final_target_replacement_blocks_deletion_and_recovery(self):
        def replace_target(point):
            if point != "SOURCE_DELETION_INTENT":
                return
            (self.target / "published.bin").rename(self.target / "ours.bin")
            (self.target / "published.bin").write_bytes(b"replacement bytes")

        record = self.move(cleanup_source=True, fault_hook=replace_target)
        self.assertEqual(record["state"], "NEEDS_RECONCILIATION")
        self.assertFalse(record["complete"])
        self.assertTrue(record["source_retained"])
        self.assertEqual((self.target / "published.bin").read_bytes(),
                         b"replacement bytes")
        self.assertEqual(digest(self.target / "ours.bin"), self.expected)
        self.assertFalse(self.source_file.exists())
        self.assert_source_bytes_preserved()
        for _ in range(2):
            recovered = self.control.recover_file_operations(
                record["operation_uuid"])
            self.assertTrue(recovered[0]["source_retained"])
            self.assertEqual((self.target / "published.bin").read_bytes(),
                             b"replacement bytes")
            self.assert_source_bytes_preserved()

    def test_final_source_hardlink_blocks_deletion(self):
        def add_alias(point):
            if point != "SOURCE_DELETION_INTENT":
                return
            captured = next(self.source.glob(".ml-quarantine-*/source"))
            os.link(captured, self.source / "new-alias.bin")

        record = self.move(cleanup_source=True, fault_hook=add_alias)
        self.assertEqual(record["state"], "NEEDS_RECONCILIATION")
        self.assertFalse(record["complete"])
        self.assertTrue(record["source_retained"])
        self.assertFalse(record["source_cleanup"]["performed"])
        self.assertFalse(self.source_file.exists())
        self.assertEqual(digest(self.source / "new-alias.bin"), self.expected)
        self.assert_source_bytes_preserved()
        self.assertEqual(digest(self.target / "published.bin"), self.expected)
        for _ in range(2):
            recovered = self.control.recover_file_operations(
                record["operation_uuid"])
            self.assertTrue(recovered[0]["source_retained"])
            self.assertEqual(digest(self.source / "new-alias.bin"), self.expected)
            self.assert_source_bytes_preserved()

    def test_held_reader_or_writer_prevents_capture_without_deleting(self):
        script = (
            "import mmap, os, sys\n"
            "mode = sys.argv[2]\n"
            "flags = os.O_RDONLY if mode == 'reader' else (\n"
            "    os.O_RDWR | os.O_CREAT)\n"
            "descriptor = os.open(sys.argv[1], flags)\n"
            "mapping = None\n"
            "if mode == 'mmap':\n"
            "    mapping = mmap.mmap(descriptor, 0, access=mmap.ACCESS_WRITE)\n"
            "print('ready', flush=True)\n"
            "sys.stdin.read()\n")
        for mode in ("reader", "writer", "mmap"):
            with self.subTest(mode=mode):
                self.source_file.write_bytes(PAYLOAD)
                child = subprocess.Popen(
                    [sys.executable, "-c", script, str(self.source_file), mode],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, text=True)
                try:
                    self.assertEqual(child.stdout.readline().strip(), "ready")
                    record = self.control.move_file(
                        self.source_root["root_uuid"], "payload.bin",
                        self.target_root["root_uuid"], f"published-{mode}.bin",
                        cleanup_source=True)
                    self.assertEqual(record["state"], "SOURCE_CLEANUP_PENDING")
                    self.assertFalse(record["complete"])
                    self.assertTrue(record["source_retained"])
                    self.assertFalse(record["source_cleanup"]["performed"])
                    self.assertEqual(self.source_file.read_bytes(), PAYLOAD)
                    name = record["source_cleanup"]["quarantine"]["dir_name"]
                    quarantine = self.source / name
                    self.assertTrue(quarantine.is_dir())
                    self.assertFalse((quarantine / "source").exists())
                    self.assertEqual(
                        digest(self.target / f"published-{mode}.bin"),
                        self.expected)
                finally:
                    child.terminate()
                    try:
                        child.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        child.kill()
                        child.wait(timeout=5)
                    for stream in (child.stdin, child.stdout, child.stderr):
                        if stream is not None:
                            stream.close()

    def test_real_lease_break_is_observed_before_deletion(self):
        descriptor = os.open(self.source_file, os.O_RDONLY)
        lease = WriteLease(descriptor, "break test")
        child = None
        try:
            lease.acquire()
            code = (
                "import os, sys\n"
                "descriptor = os.open(sys.argv[1], os.O_RDONLY)\n"
                "print('opened', flush=True)\n")
            child = subprocess.Popen(
                [sys.executable, "-c", code, str(self.source_file)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True)
            deadline = time.monotonic() + 3
            observed = "held"
            while time.monotonic() < deadline:
                observed = lease.observe()
                if observed == "broken":
                    break
                time.sleep(0.02)
            self.assertEqual(observed, "broken")
            with self.assertRaises(LeaseBroken):
                lease.assert_held()
        finally:
            if child is not None:
                child.kill()
                child.wait(timeout=5)
                child.stdin.close()
                child.stdout.close()
                child.stderr.close()
            lease.release()
            os.close(descriptor)

    def test_recovery_from_captured_state_finishes_and_is_idempotent(self):
        _crash_move(
            self.root, self.source_root["root_uuid"], self.target_root["root_uuid"],
            "SOURCE_CAPTURED")
        pending = self.control.list_file_operations()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["state"], "SOURCE_CAPTURED")
        self.assertFalse(self.source_file.exists())
        self.assert_source_bytes_preserved()

        recovered = self.control.recover_file_operations(
            pending[0]["operation_uuid"])
        self.assertEqual(recovered[0]["state"], "DONE")
        self.assertTrue(recovered[0]["source_cleanup"]["performed"])
        self.assertFalse(self.source_file.exists())
        self.assertFalse(list(self.source.glob(".ml-quarantine-*")))
        self.assertEqual(digest(self.target / "published.bin"), self.expected)

        again = self.control.recover_file_operations(
            pending[0]["operation_uuid"])
        self.assertEqual(again[0]["recovery"]["action"], "terminal-noop")
        self.assertEqual(digest(self.target / "published.bin"), self.expected)
        self.assertFalse(list(self.source.glob(".ml-quarantine-*")))

    def test_recovery_at_each_cleanup_journal_boundary(self):
        for phase in ("SOURCE_CLEANUP_INTENT", "SOURCE_QUARANTINE_INTENT",
                      "SOURCE_QUARANTINE_READY", "SOURCE_CAPTURE_INTENT",
                      "SOURCE_CAPTURED", "SOURCE_ISOLATED",
                      "SOURCE_DELETION_INTENT", "SOURCE_REMOVED", "DONE"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, target = root / "source", root / "target"
                source.mkdir()
                target.mkdir()
                (source / "payload.bin").write_bytes(PAYLOAD)
                control = Control(root)
                sr = control.add_root(str(source), access_policy="read-write",
                                      cleanup_policy="controlled")
                tr = control.add_root(str(target), access_policy="read-write")
                _crash_move(root, sr["root_uuid"], tr["root_uuid"], phase)
                pending, = control.list_file_operations()
                self.assertEqual(pending["state"], phase)
                recovered, = control.recover_file_operations(pending["operation_uuid"])
                self.assertEqual(recovered["state"], "DONE")
                self.assertTrue(recovered["source_cleanup"]["performed"])
                self.assertFalse((source / "payload.bin").exists())
                self.assertFalse(list(source.glob(".ml-quarantine-*")))
                self.assertEqual(digest(target / "published.bin"), self.expected)
                again, = control.recover_file_operations(pending["operation_uuid"])
                self.assertEqual(again["recovery"]["action"], "terminal-noop")
                self.assertEqual(digest(target / "published.bin"), self.expected)

    def test_cleanup_lease_failures_preserve_bytes_and_release_resources(self):
        acquire = WriteLease.acquire
        for label in ("source", "captured source", "published target"):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source, target = root / "source", root / "target"
                source.mkdir()
                target.mkdir()
                (source / "payload.bin").write_bytes(PAYLOAD)
                control = Control(root)
                sr = control.add_root(str(source), access_policy="read-write",
                                      cleanup_policy="controlled")
                tr = control.add_root(str(target), access_policy="read-write")
                if label == "captured source":
                    _crash_move(root, sr["root_uuid"], tr["root_uuid"], "SOURCE_CAPTURED")

                def fail_lease(lease):
                    if lease.label == label:
                        raise LeaseUnsupported("injected lease failure")
                    return acquire(lease)

                before_fds = set(os.listdir("/proc/self/fd"))
                before_mask = signal.pthread_sigmask(signal.SIG_BLOCK, set())
                with patch.object(WriteLease, "acquire", fail_lease):
                    if label == "captured source":
                        pending, = control.list_file_operations()
                        record, = control.recover_file_operations(pending["operation_uuid"])
                    else:
                        record = control.move_file(
                            sr["root_uuid"], "payload.bin", tr["root_uuid"],
                            "published.bin", cleanup_source=True)
                self.assertEqual(record["state"], "SOURCE_CLEANUP_PENDING"
                                 if label == "source" else "NEEDS_RECONCILIATION")
                self.assertFalse(record["source_cleanup"]["performed"])
                self.assertTrue(_files_with_digest(source, self.expected))
                self.assertEqual(digest(target / "published.bin"), self.expected)
                self.assertEqual(set(os.listdir("/proc/self/fd")), before_fds)
                self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, set()), before_mask)
                # Opening every remaining file would self-break or block if
                # this attempt had leaked one of its kernel write leases.
                for path in source.rglob("*"):
                    if path.is_file():
                        self.assertEqual(digest(path), self.expected)

    def test_pinned_quarantine_identity_cannot_be_reused_by_a_new_directory(self):
        _crash_move(
            self.root, self.source_root["root_uuid"], self.target_root["root_uuid"],
            "SOURCE_QUARANTINE_READY")
        pending = self.control.list_file_operations()
        self.assertEqual(len(pending), 1)
        operation_uuid = pending[0]["operation_uuid"]
        journal_path = self.control.operations._journal_path(operation_uuid)
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        quarantine = journal["source_cleanup"]["quarantine"]
        name = quarantine["dir_name"]
        original = self.source / name
        self.assertTrue(original.is_dir())
        parked = self.source / (name + "-parked")
        original.rename(parked)
        original.mkdir(mode=0o700)

        recovered = self.control.recover_file_operations(operation_uuid)
        self.assertEqual(recovered[0]["state"], "NEEDS_RECONCILIATION")
        self.assertTrue(recovered[0]["source_retained"])
        self.assertEqual(self.source_file.read_bytes(), PAYLOAD)
        self.assertTrue(parked.is_dir())
        self.assertTrue(original.is_dir())
        again = self.control.recover_file_operations(operation_uuid)
        self.assertEqual(again[0]["recovery"]["action"], "terminal-noop")
        self.assertEqual(self.source_file.read_bytes(), PAYLOAD)


if __name__ == "__main__":
    unittest.main()
