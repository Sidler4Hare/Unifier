"""A test fixture must be bounded before its first start and exclude the desktop."""

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from tools import guarded_test as guard
from unifier.errors import UnifierError
from unifier.runtime import CreateSpec, ExecResult


class GuardedTestRuntime(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.checkout = Path(temporary.name)
        self.root = self.checkout / ".lab" / "test"
        self.root.mkdir(parents=True)
        self.source = patch.object(guard, "__file__", str(self.checkout / "tools/guarded_test.py"))
        self.source.start()
        self.addCleanup(self.source.stop)
        self.spec = CreateSpec("test", "deb", str(self.root), "image@sha256:" + "a" * 64, ())
        self.calls = []
        self.info = {"Id": "c" * 64, "Config": {"Labels": {
            "multilinux.project": str(self.root), "multilinux.role": "environment"}},
            "HostConfig": {"CgroupParent": guard.SLICE, "Memory": 768 * guard.MIB,
                           "MemorySwap": 896 * guard.MIB, "PidsLimit": 192,
                           "NanoCpus": 1_000_000_000, "IpcMode": "private",
                           "PidMode": "private", "Privileged": False}, "Mounts": []}

        def runner(argv, **_kwargs):
            self.calls.append(argv)
            if argv[:2] == ["podman", "create"]:
                return ExecResult(0, self.info["Id"] + "\n", "")
            return ExecResult(0, json.dumps([self.info]), "")

        self.runtime = guard.GuardedPodmanRuntime(self.root, runner, check_rootless=False)

    def test_daily_installation_path_is_refused_before_runtime(self):
        with self.assertRaises(UnifierError):
            guard.GuardedPodmanRuntime(self.checkout / "daily")

    def test_desktop_devices_and_host_ipc_refuse_before_container_creation(self):
        for changes in ({"session_mounts": (("/run/user/1000/wayland-0", "/socket"),)},
                        {"devices": ("/dev/video0",)}, {"ipc": "host"},
                        {"mounts": (("/run/user/1000", "/run/user/1000"),)}):
            with self.subTest(changes=changes), self.assertRaises(UnifierError):
                self.runtime.create(replace(self.spec, **changes))
        self.assertEqual(self.calls, [])

    def test_low_or_unknown_host_headroom_never_starts_a_fixture(self):
        with self.assertRaises(UnifierError):
            guard.require_headroom(guard.HOST_RESERVE + guard.CONTAINER_MEMORY - 1)
        with patch.object(guard, "host_available", side_effect=UnifierError("unknown")), \
                self.assertRaises(UnifierError):
            self.runtime.create(self.spec)
        self.assertEqual(self.calls, [])

    def test_go_helpers_cannot_use_an_unbounded_scheduler_inside_the_test_slice(self):
        with patch.dict(guard.os.environ, {}, clear=True), self.assertRaises(UnifierError):
            guard.require_slice()

    def test_an_uncontained_controller_is_refused_even_if_the_slice_exists(self):
        group = "/user.slice/unifier-tests.slice"
        fields = ("ActiveState=active\nControlGroup=" + group
                  + "\nMemoryMax=1073741824\nMemorySwapMax=134217728\n"
                    "TasksMax=256\nCPUQuotaPerSecUSec=1s\n")
        with patch.dict(guard.os.environ, {"GOMAXPROCS": "2"}), \
                patch.object(guard.subprocess, "run", return_value=Mock(stdout=fields)), \
                patch.object(guard.Path, "read_text", return_value="0::/uncontained.service\n"), \
                self.assertRaises(UnifierError):
            guard.require_slice()

    def test_abort_interlock_is_shared_by_fresh_runtime_instances(self):
        self.runtime.latch_abort({"status": "ABORTED_HOST_RESERVE"})
        fresh = guard.GuardedPodmanRuntime(self.root, check_rootless=False)
        with self.assertRaises(UnifierError):
            fresh.start("test", project=str(self.root), env_id="deb")
        self.assertEqual(self.calls, [])

    def test_limits_are_applied_at_create_and_verified_before_start(self):
        with patch.object(guard, "require_headroom"), patch.object(guard, "require_slice"), \
                patch.object(self.runtime, "inspect", return_value=None):
            self.runtime.create(self.spec)
        command = self.calls[0]
        for expected in ("--memory=768m", "--memory-swap=896m", "--pids-limit=192",
                         "--cpus=1", "--cgroup-parent=" + guard.SLICE):
            self.assertIn(expected, command)
        self.info["HostConfig"]["Memory"] = 0
        with patch.object(guard, "require_headroom"), patch.object(guard, "require_slice"), \
                self.assertRaises(UnifierError):
            self.runtime.start("test", project=str(self.root), env_id="deb")
        self.assertFalse(any(call[:2] == ["podman", "start"] for call in self.calls))

    def test_substitute_cid_is_never_eligible_for_emergency_cleanup(self):
        with self.assertRaises(UnifierError):
            self.runtime.verify_limits("test", expected_id="d" * 64)
        self.assertFalse(any(call[:2] == ["podman", "stop"] for call in self.calls))

    def test_monitor_metric_failure_stops_only_the_verified_fixture(self):
        runtime = Mock()
        monitor = guard.HostReserveMonitor(
            runtime, "c" * 64, project=str(self.root), env_id="deb",
            receipt=self.root / "abort.json")
        monitor.done = Mock()
        monitor.done.wait.return_value = False
        with patch.object(guard, "host_available", side_effect=OSError("unreadable")):
            monitor._watch()
        runtime.verify_limits.assert_called_once_with("c" * 64, expected_id="c" * 64)
        runtime.stop.assert_called_once_with("c" * 64, project=str(self.root), env_id="deb")
        self.assertTrue(monitor.aborted)
        self.assertEqual(json.loads(monitor.receipt.read_text())["status"],
                         "ABORTED_MONITOR_FAILURE")

    def test_monitor_cannot_stop_a_substitute_after_headroom_failure(self):
        runtime = Mock()
        runtime.verify_limits.side_effect = UnifierError("identity changed")
        monitor = guard.HostReserveMonitor(
            runtime, "c" * 64, project=str(self.root), env_id="deb",
            receipt=self.root / "abort.json")
        monitor.done = Mock()
        monitor.done.wait.return_value = False
        with patch.object(guard, "host_available", return_value=guard.HOST_RESERVE - 1):
            monitor._watch()
        self.assertEqual(monitor.done.wait.call_count, 3)
        runtime.stop.assert_not_called()
        self.assertFalse(json.loads(monitor.receipt.read_text())["fixture_stopped"])


if __name__ == "__main__":
    unittest.main()
