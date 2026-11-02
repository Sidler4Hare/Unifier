"""View leases: lifecycle conflicts, stale recovery and raw-link control API."""

import array
import json
import os
from pathlib import Path
import socket
import sys
import subprocess
import tempfile
import threading
import unittest
import uuid
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.errors import UnifierError
from unifier.identity import binding_for
from unifier.on_demand import process_stamp
from unifier.view_journal import read_journal, write_journal
from unifier.views import (
    ViewManager,
    _runtime_dir_for,
    _verify_broker_ready,
    receive_scm_fd,
    ViewRegistry,
    assert_no_active_views,
    is_mounted,
    view_active,
)
from test_env import service_in


def _view_record(root: Path, service, *, state="starting", worker_pid=None,
                 broker_pid=None, owner_pid=None, mountpoint=None):
    record = service.store.load("debian13")
    view_uuid = str(uuid.uuid4())
    view_dir = root / ".lab/state/views" / view_uuid
    view_dir.mkdir(parents=True, mode=0o700)
    mount_dir = Path(mountpoint) if mountpoint else view_dir / "mount"
    mount_dir.mkdir(mode=0o700)
    now = service.clock()
    return {
        "schema_version": 1,
        "view_uuid": view_uuid,
        "environment_id": "debian13",
        "environment_binding": binding_for(record),
        "state": state,
        "mountpoint": str(mount_dir),
        "view_dir": str(view_dir),
        "owner_pid": os.getpid() if owner_pid is None else owner_pid,
        "owner_stamp": process_stamp(os.getpid()) if owner_pid is None else 1,
        "worker_pid": worker_pid,
        "worker_stamp": process_stamp(worker_pid) if worker_pid else None,
        "broker_pid": broker_pid,
        "broker_stamp": process_stamp(broker_pid) if broker_pid else None,
        "control_socket": str(view_dir / "control.sock"),
        "storage_socket": str(view_dir / "storage.sock"),
        "spec_path": str(view_dir / "spec.json"),
        "log_path": str(view_dir / "view.log"),
        "ready_path": str(view_dir / "ready.json"),
        "storage_journal": str(view_dir / "storage.json"),
        "storage_nonce": uuid.uuid4().hex,
        "storage_ref_state": "none",
        "broker_launcher_pid": None,
        "broker_launcher_stamp": None,
        "readonly": True,
        "backend": "podman-overlay",
        "raw_link_semantics": "projected-link",
        "uid_map": {"host_uid": 1000},
        "created_at": now,
        "updated_at": now,
    }


class LeaseConflicts(unittest.TestCase):
    def test_active_starting_view_blocks_start_and_remove(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            service.create("debian13", None, [])
            registry = ViewRegistry(root)
            record = _view_record(root, service)
            registry.add(record)
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, "active read-only view"):
                service.start("debian13")
            self.assertEqual(runtime.events, [])
            with self.assertRaisesRegex(UnifierError, "active read-only view"):
                service.remove("debian13")
            self.assertTrue(service.store.exists("debian13"))
            self.assertTrue(Path(record["mountpoint"]).is_dir())

    def test_dead_and_unmounted_view_is_not_active(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            record = _view_record(
                root, service, state="ready", worker_pid=999999, broker_pid=999998,
                owner_pid=999997)
            self.assertFalse(view_active(record))
            assert_no_active_views(root, "debian13")

    def test_cleanup_error_does_not_mask_original_and_keeps_recoverable_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            manager = ViewManager(root, service)

            def fail(_view):
                raise OSError(39, "AF_UNIX path too long")

            manager._start_view = fail
            manager._cleanup_resources = lambda _view: ["Directory not empty"]
            with self.assertRaisesRegex(OSError, "AF_UNIX path too long"):
                manager.open("debian13")
            records = ViewRegistry(root).list()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["state"], "failed")
            self.assertIn("AF_UNIX path too long", records[0]["last_error"])
            self.assertEqual(records[0]["cleanup_errors"], ["Directory not empty"])
            self.assertTrue(records[0]["runtime_dir"])
            self.assertLess(len(records[0]["control_socket"].encode()), 108)

    def test_runtime_socket_directory_is_short_and_private(self):
        path = _runtime_dir_for("00000000-0000-4000-8000-000000000099")
        self.assertLess(len(str(path / "c").encode()), 108)
        self.assertEqual(path.name, "00000000-0000-4000-8000-000000000099")

    def test_stale_view_recovery_removes_record_and_private_dirs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            registry = ViewRegistry(root)
            record = _view_record(
                root, service, state="ready", worker_pid=999999, broker_pid=999998,
                owner_pid=999997)
            for key in ("control_socket", "storage_socket", "spec_path", "log_path"):
                Path(record[key]).write_text("stale", encoding="utf-8")
            registry.add(record)
            manager = ViewManager(root, service)
            recovered = manager.recover(record["view_uuid"])
            self.assertEqual(recovered[0]["view_uuid"], record["view_uuid"])
            self.assertEqual(registry.list(), [])
            self.assertFalse(Path(record["mountpoint"]).exists())
            self.assertFalse(Path(record["view_dir"]).exists())

    def test_recover_legacy_revoking_record_with_spec_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            record = _view_record(
                root, service, state="revoking", worker_pid=None, broker_pid=None,
                owner_pid=999997)
            record.pop("runtime_dir", None)
            record["owner_stamp"] = 1
            record["control_socket"] = None
            record["storage_socket"] = None
            record["spec_path"] = None
            record["log_path"] = None
            spec = Path(record["view_dir"]) / "spec.json"
            spec.write_text('{"old": true}\n', encoding="utf-8")
            ViewRegistry(root).add(record)
            recovered = ViewManager(root, service).recover(record["view_uuid"])
            self.assertEqual(recovered[0]["view_uuid"], record["view_uuid"])
            self.assertFalse(spec.exists())
            self.assertFalse(Path(record["view_dir"]).exists())
            self.assertEqual(ViewRegistry(root).list(), [])

    def test_revoke_cleans_a_dead_unmounted_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            registry = ViewRegistry(root)
            record = _view_record(
                root, service, state="ready", worker_pid=999999, broker_pid=999998,
                owner_pid=999997)
            registry.add(record)
            removed = ViewManager(root, service).revoke(record["view_uuid"])
            self.assertEqual(removed["view_uuid"], record["view_uuid"])
            self.assertEqual(registry.list(), [])


class ScmRights(unittest.TestCase):
    def test_receive_one_real_scm_rights_fd(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload"
            path.write_text("payload", encoding="utf-8")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                sender.sendmsg(
                    [b'{"ok": true}\n'],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array.array("i", [descriptor]))])
                payload, received = receive_scm_fd(receiver)
                self.assertTrue(payload["ok"])
                self.assertEqual(os.read(received, 100), b"payload")
                os.close(received)
            finally:
                os.close(descriptor)
                sender.close()
                receiver.close()

    def test_multiple_or_truncated_descriptors_are_refused(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with tempfile.TemporaryDirectory() as temporary:
            first = os.open(Path(temporary) / "a", os.O_CREAT | os.O_RDWR, 0o600)
            second = os.open(Path(temporary) / "b", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                sender.sendmsg(
                    [b'{"ok": true}\n'],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array.array("i", [first, second]))])
                with self.assertRaisesRegex(UnifierError, "expected one"):
                    receive_scm_fd(receiver)
            finally:
                os.close(first)
                os.close(second)
                sender.close()
                receiver.close()


class RawLinkControl(unittest.TestCase):
    def test_readlink_raw_round_trips_over_private_socket(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            registry = ViewRegistry(root)
            record = _view_record(
                root, service, state="ready", worker_pid=999999, broker_pid=999998,
                owner_pid=999997)
            control = Path(record["control_socket"])
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(control))
            server.listen(1)

            def reply():
                client, _ = server.accept()
                request = json.loads(client.recv(4096).decode())
                self.assertEqual(request, {"op": "readlink_raw", "path": "/etc/os-release"})
                client.sendall((json.dumps({
                    "ok": True, "raw": "/etc/os-release",
                    "projected": "/view/etc/os-release",
                    "semantics": "projected-link"}) + "\n").encode())
                client.close()

            thread = threading.Thread(target=reply)
            thread.start()
            registry.add(record)
            try:
                result = ViewManager(root, service).readlink_raw(
                    record["view_uuid"], "/etc/os-release")
            finally:
                thread.join(5)
                server.close()
            self.assertEqual(result["raw"], "/etc/os-release")
            self.assertEqual(result["semantics"], "projected-link")


def _spawn_sleeper():
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL)


def _stop_child(process):
    if process is None:
        return
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


class BrokerIdentity(unittest.TestCase):
    def test_peercred_own_pid_stamp_and_nonce_are_required(self):
        left, right = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            pid = os.getpid()
            payload = {"ok": True, "broker_pid": pid,
                       "broker_stamp": process_stamp(pid), "nonce": "view-nonce"}
            self.assertEqual(
                _verify_broker_ready(left, payload, nonce="view-nonce",
                                     launcher_pid=pid),
                (pid, process_stamp(pid)))
            with self.assertRaisesRegex(UnifierError, "nonce"):
                _verify_broker_ready(
                    left, {**payload, "nonce": "other"}, nonce="view-nonce",
                    launcher_pid=pid)
            with self.assertRaisesRegex(UnifierError, "SO_PEERCRED|PID"):
                _verify_broker_ready(
                    left, {**payload, "broker_pid": pid + 1}, nonce="view-nonce",
                    launcher_pid=pid)
            with self.assertRaisesRegex(UnifierError, "start stamp|live"):
                _verify_broker_ready(
                    left, {**payload, "broker_stamp": (payload["broker_stamp"] or 0) + 1},
                    nonce="view-nonce", launcher_pid=pid)
        finally:
            left.close()
            right.close()


class StorageJournalOwnership(unittest.TestCase):
    def _manager_view(self, root, service, *, journal_state=None):
        manager = ViewManager(root, service)
        view = _view_record(root, service, state="ready", worker_pid=999999,
                            broker_pid=999998, owner_pid=999997)
        if journal_state is not None:
            write_journal(view["storage_journal"], {
                "schema_version": 1,
                "state": journal_state,
                "cid": view["environment_binding"]["container_id"],
                "view_uuid": view["view_uuid"],
                "nonce": view["storage_nonce"],
                "broker_pid": 999998,
                "broker_stamp": 1,
                "merged_path": "/merged",
            })
        return manager, view

    def test_mounted_journal_releases_one_own_reference_and_keeps_external(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            manager, view = self._manager_view(root, service, journal_state="mounted")
            references = {"count": 2}
            calls = []

            def fake_umount(cid):
                calls.append(cid)
                references["count"] -= 1
                return True, ""

            manager._storage_umount = fake_umount
            self.assertEqual(manager._reconcile_storage(view), [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(references["count"], 1)
            self.assertEqual(read_journal(view["storage_journal"])["state"], "released")
            self.assertEqual(view["storage_ref_state"], "released")
            # A second recovery must not release the external reference again.
            self.assertEqual(manager._reconcile_storage(view), [])
            self.assertEqual(len(calls), 1)
            self.assertEqual(references["count"], 1)

    def test_ambiguous_journal_windows_block_without_umount(self):
        for state in ("mounting", "releasing", "release-failed"):
            with self.subTest(state=state), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                service, _runtime = service_in(root)
                service.create("debian13", None, [])
                manager, view = self._manager_view(root, service, journal_state=state)
                calls = []
                manager._storage_umount = lambda cid: (calls.append(cid) or (True, ""))
                errors = manager._reconcile_storage(view)
                self.assertTrue(errors)
                self.assertEqual(calls, [])
                self.assertEqual(view["state"], "needs-reconciliation")
                self.assertEqual(view["storage_ref_state"], "needs-reconciliation")
                self.assertEqual(
                    read_journal(view["storage_journal"])["state"], state)

    def test_missing_or_foreign_journal_blocks_a_possible_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            manager, view = self._manager_view(root, service)
            view["storage_ref_state"] = "mounted"
            calls = []
            manager._storage_umount = lambda cid: (calls.append(cid) or (True, ""))
            self.assertTrue(manager._reconcile_storage(view))
            self.assertEqual(calls, [])
            self.assertEqual(view["state"], "needs-reconciliation")

            write_journal(view["storage_journal"], {
                "schema_version": 1, "state": "mounted",
                "cid": view["environment_binding"]["container_id"],
                "view_uuid": "00000000-0000-4000-8000-000000000001",
                "nonce": view["storage_nonce"], "broker_pid": 999998,
                "broker_stamp": 1, "merged_path": "/merged"})
            self.assertTrue(manager._reconcile_storage(view))
            self.assertEqual(calls, [])

    def test_unresolved_revoke_keeps_lease_and_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            manager, view = self._manager_view(root, service)
            view["storage_ref_state"] = "mounted"
            spec = Path(view["spec_path"])
            spec.write_text("{}\n", encoding="utf-8")
            ViewRegistry(root).add(view)
            with self.assertRaisesRegex(UnifierError, "revoke incomplete"):
                manager.revoke(view["view_uuid"])
            records = ViewRegistry(root).list()
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["state"], "needs-reconciliation")
            self.assertTrue(spec.exists())
            self.assertTrue(Path(view["view_dir"]).is_dir())

    def test_broker_sigkill_recovery_is_repeatable_and_releases_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            manager, view = self._manager_view(root, service, journal_state="mounted")
            ViewRegistry(root).add(view)
            calls = []
            manager._storage_umount = lambda cid: (calls.append(cid) or (True, ""))
            recovered = manager.recover(view["view_uuid"])
            self.assertEqual(recovered[0]["view_uuid"], view["view_uuid"])
            self.assertEqual(len(calls), 1)
            self.assertEqual(ViewRegistry(root).list(), [])
            self.assertFalse(Path(view["mountpoint"]).exists())
            self.assertEqual(manager.recover(), [])
            self.assertEqual(len(calls), 1)

    def test_worker_sigkill_with_released_storage_recovers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            manager, view = self._manager_view(root, service, journal_state="released")
            ViewRegistry(root).add(view)
            calls = []
            manager._storage_umount = lambda cid: (calls.append(cid) or (True, ""))
            manager.recover(view["view_uuid"])
            self.assertEqual(calls, [])
            self.assertEqual(ViewRegistry(root).list(), [])


class CleanupOrder(unittest.TestCase):
    def test_busy_mount_preserves_processes_reference_and_files(self):
        worker = broker = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            worker, broker = _spawn_sleeper(), _spawn_sleeper()
            try:
                view = _view_record(
                    root, service, state="ready", worker_pid=worker.pid,
                    broker_pid=broker.pid)
                spec = Path(view["spec_path"])
                spec.write_text("{}\n", encoding="utf-8")
                ViewRegistry(root).add(view)
                manager = ViewManager(root, service)
                mounted = [True]
                attempts = []

                def busy_unmount(_path):
                    attempts.append("unmount")
                    return False, "Device or resource busy"

                with mock.patch("unifier.views.is_mounted",
                                side_effect=lambda _path: mounted[0]), \
                        mock.patch("unifier.views._unmount_fuse",
                                   side_effect=busy_unmount):
                    with self.assertRaisesRegex(UnifierError, "revoke incomplete"):
                        manager.revoke(view["view_uuid"])
                    self.assertEqual(attempts, ["unmount"])
                    self.assertEqual(process_stamp(worker.pid), view["worker_stamp"])
                    self.assertEqual(process_stamp(broker.pid), view["broker_stamp"])
                    self.assertTrue(spec.exists())
                    self.assertEqual(len(ViewRegistry(root).list()), 1)
                    self.assertEqual(ViewRegistry(root).list()[0]["state"], "ready")

                    def successful_unmount(_path):
                        attempts.append("unmount")
                        mounted[0] = False
                        return True, ""

                    with mock.patch("unifier.views._unmount_fuse",
                                    side_effect=successful_unmount):
                        manager.revoke(view["view_uuid"])
                self.assertEqual(ViewRegistry(root).list(), [])
                self.assertFalse(spec.exists())
                self.assertIsNotNone(worker.poll())
                self.assertIsNotNone(broker.poll())
            finally:
                _stop_child(worker)
                _stop_child(broker)

    def test_fuse_is_unmounted_before_worker_and_broker_are_signalled(self):
        worker = broker = None
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            worker, broker = _spawn_sleeper(), _spawn_sleeper()
            try:
                view = _view_record(
                    root, service, state="ready", worker_pid=worker.pid,
                    broker_pid=broker.pid)
                ViewRegistry(root).add(view)
                manager = ViewManager(root, service)
                mounted = [True]
                observed = []

                def ordered_unmount(_path):
                    observed.append(
                        (process_stamp(worker.pid) == view["worker_stamp"],
                         process_stamp(broker.pid) == view["broker_stamp"]))
                    mounted[0] = False
                    return True, ""

                with mock.patch("unifier.views.is_mounted",
                                side_effect=lambda _path: mounted[0]), \
                        mock.patch("unifier.views._unmount_fuse",
                                   side_effect=ordered_unmount):
                    manager.revoke(view["view_uuid"])
                self.assertEqual(observed, [(True, True)])
                self.assertEqual(ViewRegistry(root).list(), [])
                self.assertIsNotNone(worker.poll())
                self.assertIsNotNone(broker.poll())
            finally:
                _stop_child(worker)
                _stop_child(broker)


class StartWiring(unittest.TestCase):
    def test_start_view_passes_journal_nonce_and_readiness_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            view = _view_record(root, service, state="starting")
            ViewRegistry(root).add(view)
            manager = ViewManager(root, service)
            merged = os.open(Path(temporary) / "merged", os.O_CREAT | os.O_RDONLY, 0o600)
            broker_calls = {}
            worker_calls = {}

            def fake_broker(cid, socket_path, log_path, journal_path, nonce,
                            view_uuid):
                broker_calls.update(
                    cid=cid, socket_path=socket_path, log_path=log_path,
                    journal_path=journal_path, nonce=nonce, view_uuid=view_uuid)
                return (merged, 111, 1, 112, 2,
                        {"storage_uid_map": [], "storage_gid_map": []})

            def fake_worker(current, merged_fd, spec_path, control_socket,
                            log_path, ready_path, nonce):
                worker_calls.update(
                    merged_fd=merged_fd, spec_path=str(spec_path),
                    control_socket=control_socket, log_path=log_path,
                    ready_path=ready_path, nonce=nonce)
                return 333, 3

            manager._collect_mounts = lambda _record: ([], set())
            manager._inspect_id_mappings = lambda _cid: None
            manager._start_broker = fake_broker
            manager._start_worker = fake_worker
            manager._start_view(view)
            self.assertEqual(
                broker_calls,
                {"cid": view["environment_binding"]["container_id"],
                 "socket_path": view["storage_socket"],
                 "log_path": view["log_path"],
                 "journal_path": view["storage_journal"],
                 "nonce": view["storage_nonce"],
                 "view_uuid": view["view_uuid"]})
            self.assertEqual(worker_calls["merged_fd"], merged)
            self.assertEqual(worker_calls["ready_path"], view["ready_path"])
            self.assertEqual(worker_calls["nonce"], view["storage_nonce"])
            self.assertEqual(view["state"], "ready")
            self.assertEqual(view["worker_pid"], 333)
            self.assertEqual(view["storage_ref_state"], "mounted")


if __name__ == "__main__":
    unittest.main()
