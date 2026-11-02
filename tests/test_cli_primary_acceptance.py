"""Controller counterexamples beyond the implementation's own test cases."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from unifier import shortcuts
from unifier.errors import UnifierError
from test_cli_shortcuts import ready_context, run_ctx


class ControllerCounterexamples(unittest.TestCase):
    def test_local_package_name_comes_from_native_metadata_not_filename(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            runtime.copy_in = lambda *_args, **_kwargs: None
            service = ctx.service_for("debian13")
            package = Path(temporary) / "unrelated-file-name.deb"
            package.write_bytes(b"!<arch>\n")
            from unifier.runtime import ExecResult
            runtime.exec_handler = lambda _name, argv, _env: (
                ExecResult(0, "real-native-name\n", "")
                if argv[0] == "dpkg-deb" else None)
            self.assertEqual(service.local_package_name("debian13", str(package)),
                             "real-native-name")
            self.assertEqual(service.inspect("debian13")["runtime_state"], "stopped")

    def test_failed_native_metadata_query_releases_its_started_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            runtime.copy_in = lambda *_args, **_kwargs: None
            service = ctx.service_for("debian13")
            package = Path(temporary) / "invalid.deb"
            package.write_bytes(b"!<arch>\n")
            from unifier.runtime import ExecResult
            runtime.exec_handler = lambda _name, argv, _env: (
                ExecResult(1, "", "invalid metadata")
                if argv[0] == "dpkg-deb" else None)
            with self.assertRaises(UnifierError):
                service.local_package_name("debian13", str(package))
            self.assertEqual(service.inspect("debian13")["runtime_state"], "stopped")

    def test_selinux_label_policy_is_limited_to_owned_private_area(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, _runtime = ready_context(Path(temporary))
            files = shortcuts._files_dir(ctx, "debian13")
            self.assertEqual(shortcuts._private_share_policy(
                ctx, "debian13", files, selinux_enabled=True), "shared-relabel")
            self.assertEqual(shortcuts._private_share_policy(
                ctx, "debian13", files, selinux_enabled=False), "preserve-host")
            with self.assertRaises(UnifierError):
                shortcuts._private_share_policy(
                    ctx, "debian13", Path(temporary), selinux_enabled=True)

    def test_normal_cli_stop_does_not_lock_the_package_lock_twice(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            service = ctx.service_for("debian13")
            service.start("debian13")
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["stop", "debian13"])
            self.assertEqual(code, 0, (out, err))
            self.assertIn("stop", [event[0] for event in runtime.events])

    def test_unreadable_process_identity_is_unknown_not_a_dead_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            service = ctx.service_for("debian13")
            service.start("debian13")
            from unifier.on_demand import LaunchGroup, process_stamp
            group = LaunchGroup(service, "debian13")
            group.path.write_text(json.dumps({"leases": {"probe": {
                "pid": os.getpid(), "stamp": process_stamp(os.getpid()),
                "app_id": "probe"}}}))
            original = Path.read_text

            def deny_proc(path, *args, **kwargs):
                if str(path) == f"/proc/{os.getpid()}/stat":
                    raise PermissionError("injected process visibility failure")
                return original(path, *args, **kwargs)

            runtime.events.clear()
            with patch.object(Path, "read_text", deny_proc):
                code, _out, _err = run_ctx(ctx, ["stop", "debian13"])
            self.assertNotEqual(code, 0)
            self.assertNotIn("stop", [event[0] for event in runtime.events])

    def test_existing_pin_is_checked_in_addition_to_catalog_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            service = ctx.service_for("debian13")
            record = service.store.load("debian13")
            pin = {"identity": {
                "environment_uuid": str(uuid.uuid4()),
                "generation": record["generation"],
                "container_id": record["runtime"]["container_id"],
            }}
            before = list(runtime.events)
            with self.assertRaises(UnifierError):
                shortcuts._validate_new_record(
                    ctx, service, "debian13", "debian13", pin)
            self.assertEqual(runtime.events, before)

    def test_long_distinct_aliases_do_not_share_an_internal_id(self):
        prefix = "a" * 25
        self.assertNotEqual(
            shortcuts._internal_app_id("debian13", prefix + "-first"),
            shortcuts._internal_app_id("debian13", prefix + "-second"))

    def test_corrupt_other_record_is_not_an_empty_container_inventory(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            service = ctx.service_for("debian13")
            (service.store.directory / "other.json").write_text("{broken")
            before = list(runtime.events)
            code, _out, _err = run_ctx(ctx, [
                "init", "--target", "debian13", "--yes"])
            self.assertNotEqual(code, 0)
            self.assertEqual(runtime.events, before)


if __name__ == "__main__":
    unittest.main()
