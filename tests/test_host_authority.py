"""Host policy and per-installation target uniqueness at execution boundaries."""
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import contextlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from test_compatibility import host
from test_env import FakeRuntime, PROFILES, service_in
from test_cli_shortcuts import ready_context, run_ctx
from unifier import cli
from unifier.errors import UnifierError


class UniqueEnvironmentTarget(unittest.TestCase):
    def test_custom_alias_cannot_create_a_second_target_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            runtime.identities["first"] = dict(PROFILES["debian13"])
            service.create("debian13", "first", [])
            original = service.store.path("first").read_bytes()
            before = list(runtime.events)
            with patch.object(runtime, "inspect", side_effect=AssertionError("runtime reached")):
                with self.assertRaisesRegex(UnifierError, "only one environment per target"):
                    service.create("debian13", "second", [])
            self.assertEqual(runtime.events, before)
            self.assertEqual(service.store.path("first").read_bytes(), original)
            self.assertFalse(service.store.exists("second"))

    def test_retained_record_reserves_target_even_if_container_is_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.create("debian13", None, [])
            runtime.containers.clear()
            with self.assertRaisesRegex(UnifierError, "already has an environment"):
                service.create("debian13", "second", [])
            self.assertTrue(service.store.exists("debian13"))

    def test_failed_create_releases_target_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            runtime.fail_inventory = True
            with self.assertRaises(UnifierError):
                service.create("debian13", None, [])
            runtime.fail_inventory = False
            self.assertEqual(service.create("debian13", None, [])["target"], "debian13")

    def test_adopted_target_reserves_the_installation_without_a_local_record(self):
        from unifier.install_state import InstallState
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            state = InstallState(service.root, service.config["targets"])
            record = state.load()
            record["targets"]["debian13"].update(
                source="adopted", state="adopted", selected=False,
                source_root=str(Path(temporary) / "external"), source_environment="legacy")
            state.save(record)
            before = state.path.read_bytes()
            with patch.object(runtime, "inspect", side_effect=AssertionError("runtime reached")):
                with self.assertRaisesRegex(UnifierError, "already has an adopted environment"):
                    service.create("debian13", "other", [])
            self.assertEqual(runtime.events, [])
            self.assertEqual(state.path.read_bytes(), before)

    def test_parallel_aliases_are_serialized_until_record_is_durable(self):
        entered, release, second_started = threading.Event(), threading.Event(), threading.Event()

        class PausingRuntime(FakeRuntime):
            def create(self, spec):
                entered.set()
                if not release.wait(5):
                    raise AssertionError("creation lock test timed out")
                return super().create(spec)

        with tempfile.TemporaryDirectory() as temporary:
            runtime = PausingRuntime()
            for name in ("first", "second"):
                runtime.identities[name] = dict(PROFILES["debian13"])
            first, _ = service_in(Path(temporary), runtime)
            second, _ = service_in(Path(temporary), runtime)

            def create_second():
                second_started.set()
                return second.create("debian13", "second", [])

            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(first.create, "debian13", "first", [])
                try:
                    self.assertTrue(entered.wait(5))
                    b = pool.submit(create_second)
                    self.assertTrue(second_started.wait(5))
                    with self.assertRaises(TimeoutError):
                        b.result(timeout=0.1)
                finally:
                    release.set()
                self.assertEqual(a.result(timeout=5)["id"], "first")
                with self.assertRaisesRegex(UnifierError, "only one environment per target"):
                    b.result(timeout=5)
            self.assertEqual(sum(e[0] == "create" for e in runtime.events), 1)
            self.assertEqual([r["id"] for r in first.store.all()], ["first"])

    def test_policy_is_rechecked_after_waiting_for_creation_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            with patch.object(service, "compatibility_plan", side_effect=[
                {"allowed": True}, {"allowed": False, "reasons": ["policy changed"]},
            ]):
                with self.assertRaisesRegex(UnifierError, "policy changed"):
                    service.create("debian13", None, [])
            self.assertEqual(runtime.events, [])
            self.assertFalse(service.store.exists("debian13"))


class HostExecutionBoundaries(unittest.TestCase):
    def test_running_ls_refuses_same_family_without_guest_execution_or_state_changes(self):
        for distro in ("ubuntu", "debian"):
            with self.subTest(distro=distro), tempfile.TemporaryDirectory() as temporary:
                ctx, runtime = ready_context(Path(temporary))
                service = ctx.service_for("debian13")
                service.start("debian13")
                service.host_facts = host(distro)
                ctx.service_kwargs = {**ctx.service_kwargs, "host_facts": host(distro)}
                state = ctx.state.path.read_bytes()
                record = service.store.path("debian13").read_bytes()
                before = list(runtime.events)
                with patch.object(runtime, "exec", side_effect=AssertionError("guest reached")):
                    code, _out, err = run_ctx(ctx, ["ls", "debian13", "/", "--json"])
                self.assertEqual(code, 1)
                self.assertIn("same-family containers are forbidden", err)
                self.assertEqual(runtime.events, before)
                self.assertEqual(ctx.state.path.read_bytes(), state)
                self.assertEqual(service.store.path("debian13").read_bytes(), record)

    def test_stopped_same_family_view_remains_available_for_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            service = ctx.service_for("debian13")
            service.host_facts = host("ubuntu")
            ctx.service_kwargs = {**ctx.service_kwargs, "host_facts": host("ubuntu")}
            mount = root / "recovery-view"
            (mount / "home").mkdir(parents=True)
            manager = Mock()
            manager.list.return_value = []
            manager.open.return_value = {"environment_id": "debian13", "view_uuid": "recovery",
                                         "mountpoint": str(mount), "state": "ready"}
            ctx.view_managers[str(root)] = manager
            before = list(runtime.events)
            code, out, err = run_ctx(ctx, ["ls", "debian13", "/", "--json"])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)["entries"], ["home"])
            self.assertEqual(runtime.events, before)
            manager.revoke.assert_called_once_with("recovery")

    def test_capture_helper_cannot_execute_on_a_newly_disallowed_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            service.start("debian13")
            service.host_facts = host("ubuntu")
            before = list(runtime.events)
            with self.assertRaisesRegex(UnifierError, "same-family containers are forbidden"):
                service._exec_record(record, ["true"], timeout=5, native=False)
            self.assertEqual(runtime.events, before)

    def test_matrix_succeeds_with_cross_family_choices_but_check_keeps_target_refusal(self):
        for distro in ("ubuntu", "debian", "fedora", "arch", "manjaro", "unknown"):
            with self.subTest(distro=distro), \
                 patch("unifier.compatibility.observe_host", return_value=host(distro)), \
                 contextlib.redirect_stdout(io.StringIO()) as output:
                code = cli.main(["compat", "matrix"])
                rows = json.loads(output.getvalue())
                self.assertEqual(code, 0 if any(row["allowed"] for row in rows) else 1)
                if distro != "unknown":
                    self.assertTrue(any(not row["allowed"] for row in rows))


if __name__ == "__main__":
    unittest.main()
