"""Focused E-stage contracts for bounded project sessions (no real podman)."""

import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier import cli
from unifier import project_sessions as sessions_module
from unifier.desktop import HostDesktop
from unifier.errors import UnifierError
from unifier.file_routes import FileRoutes
from unifier.on_demand import LaunchGroup, process_stamp
from unifier.project_sessions import ProjectSessions, validate_env_keys
from unifier.runtime import (
    GUEST_CLEANUP,
    GUEST_PROBE,
    GUEST_SIGNAL,
    GuestProcess,
    exec_argv,
    guest_session_argv,
    parse_guest_ready,
)
from test_desktop import SESSION
from test_env import FakeRuntime, service_in


class FakeSessionProcess:
    next_pid = 740000

    def __init__(self, code=0):
        type(self).next_pid += 1
        self.pid = type(self).next_pid
        self.code = code
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self):
        if self.returncode is None:
            self.returncode = self.code
        return self.returncode


class FakeGuestExecution:
    """A host proxy plus an already-proven guest identity."""

    next_guest = 810000

    def __init__(self, runtime, code):
        type(self).next_guest += 1
        pid = type(self).next_guest
        self.runtime = runtime
        self.guest = GuestProcess(
            pid=pid, start=1000 + pid, pgrp=pid, sid=pid, token="a" * 32)
        self.process = FakeSessionProcess()
        self.code = code
        self.finished = True
        self.proxy_aborted = False

    def wait(self, timeout=None):
        if self.finished:
            return self.code
        if timeout is None:
            self.finished = True
            return self.code
        raise subprocess.TimeoutExpired("fake-guest", timeout)

    def signal(self, signum):
        self.runtime.signal_calls.append((self.guest, signum,
                                          self.runtime.call_user))
        if self.runtime.signal_status in {"gone", "unknown"}:
            return self.runtime.signal_status
        self.finished = True
        self.code = self.runtime.signal_return
        return "signalled"

    def cleanup(self):
        self.runtime.cleanup_calls.append((self.guest, self.runtime.call_user))
        return self.runtime.cleanup_status

    def abort_proxy(self):
        self.proxy_aborted = True


class SessionRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.session_events = []
        self.session_code = 0
        self.signal_return = 15
        self.signal_status = "signalled"
        self.cleanup_status = "gone"
        self.probe_status = "gone"
        self.signal_calls = []
        self.cleanup_calls = []
        self.probe_calls = []
        self.call_user = None
        self.last_execution = None

    def start_session_exec(self, name, argv, *, project, env_id, workdir, env,
                           user=None, interactive=True, tty=False,
                           on_spawn=None, control_timeout=None):
        self._owned(name, project, env_id)
        self.call_user = user
        self.session_events.append({
            "name": name, "argv": list(argv), "workdir": workdir,
            "env": dict(env), "interactive": interactive, "tty": tty,
            "user": user,
        })
        execution = FakeGuestExecution(self, self.session_code)
        self.last_execution = execution
        if on_spawn is not None:
            on_spawn(execution.process)
        return execution

    def probe_guest(self, name, guest, *, project, env_id, user=None, timeout=10):
        self._owned(name, project, env_id)
        self.call_user = user
        self.probe_calls.append((guest, user))
        return self.probe_status

    def signal_guest(self, name, guest, signum, *, project, env_id, user=None,
                     timeout=10):
        self._owned(name, project, env_id)
        self.call_user = user
        self.signal_calls.append((guest, signum, user))
        return self.signal_status

    def cleanup_guest(self, name, guest, *, project, env_id, user=None,
                      timeout=15):
        self._owned(name, project, env_id)
        self.call_user = user
        self.cleanup_calls.append((guest, user))
        return self.cleanup_status


def ready(root: Path, *, runtime=None, on_demand=False, user_namespace="rootless-default"):
    runtime = runtime or SessionRuntime()
    service, runtime = service_in(root, runtime=runtime)
    host = root / "host-desktop"
    service.host_desktop = HostDesktop(host / "applications", host / "mime")
    service.cache_runner = lambda argv: type(
        "Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    project = root / "project"
    project.mkdir(exist_ok=True)
    document = project / "notes.txt"
    document.write_text("notes\n", encoding="utf-8")
    service.create("arch", None, [str(project)], desktop=True, session=SESSION,
                   user_namespace=user_namespace)
    service.add_app("arch", "freecad", "/usr/bin/freecad",
                    ["/usr/bin/freecad"], workdir=str(project), on_demand=on_demand)
    routes = FileRoutes(root, service)
    root_record = routes.roots.add(str(project))
    handler = routes.add_handler("freecad", intent="edit",
                                 save_capability="explicit-path")
    project_record = routes.add_project(
        root_record["root_uuid"], "arch", alias="demo",
        default_save_root=str(project),
        intent_handlers={"edit": handler["handler_uuid"]})
    return service, runtime, routes, project_record, root_record, document


class Planning(unittest.TestCase):
    def test_plan_pins_identity_cwd_scope_user_and_has_zero_runtime_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            before = list(runtime.events)
            plan = sessions.prepare(project["project_uuid"], cwd=str(document.parent),
                                    argv=["/bin/echo", "hello"], env_keys=[])
            self.assertEqual(plan["project_uuid"], project["project_uuid"])
            self.assertEqual(plan["root_uuid"], root["root_uuid"])
            self.assertEqual(plan["root_dev"], root["root_dev"])
            self.assertEqual(plan["generation"], 1)
            self.assertEqual(plan["container_id"], "cid-arch")
            self.assertEqual(plan["argv"], ["/bin/echo", "hello"])
            self.assertEqual(plan["session_user"], "0:0")
            self.assertEqual(plan["execution"]["user"], "0:0")
            self.assertEqual(plan["access_scope"]["mappings"]["cwd"]["path"],
                             str(document.parent))
            self.assertTrue(plan["access_scope"]["mappings"]["cwd"]["same_host_object"])
            self.assertFalse(plan["execution"]["shell"])
            self.assertEqual(runtime.events, before)

    def test_keep_id_pins_the_host_document_identity_and_other_namespaces_fail(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(
                Path(temporary), user_namespace="keep-id")
            sessions = ProjectSessions(Path(temporary), service)
            plan = sessions.prepare(project["project_uuid"],
                                    cwd=str(document.parent), argv=["/bin/true"])
            self.assertEqual(plan["session_user"], "1000:1000")
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
            self.assertEqual(record["session_user"], "1000:1000")

        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            record = service.store.load("arch")
            record["runtime"]["user_namespace"] = "host"
            service.store.save(record, None)
            sessions = ProjectSessions(Path(temporary), service)
            with self.assertRaisesRegex(UnifierError, "cannot prove.*user namespace"):
                sessions.prepare(project["project_uuid"], cwd=str(document.parent),
                                 argv=["/bin/true"])

    def test_alias_replacement_does_not_reuse_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, routes, project, root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                    cwd=str(document.parent))
            routes.remove_project(project["project_uuid"])
            replacement = routes.add_project(
                root["root_uuid"], "arch", alias="demo",
                default_save_root=str(document.parent), intent_handlers={})
            self.assertNotEqual(replacement["project_uuid"], project["project_uuid"])
            with self.assertRaisesRegex(UnifierError, "not registered"):
                sessions.prepare(project["project_uuid"], cwd=str(document.parent),
                                 argv=["/bin/true"])
            self.assertEqual(
                sessions.prepare("demo", cwd=str(document.parent), argv=["/bin/true"])
                ["project_uuid"], replacement["project_uuid"])

    def test_nested_root_shadow_mount_and_readonly_mismatch_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, routes, project, root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            nested = document.parent / "nested"
            nested.mkdir()
            routes.roots.add(str(nested), access_policy="read-only")
            with self.assertRaisesRegex(UnifierError, "different registered data root"):
                sessions.prepare(project["project_uuid"], cwd=str(nested),
                                 argv=["/bin/true"])

        with tempfile.TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            service, _runtime, _routes, project, _root, document = ready(root_path)
            sessions = ProjectSessions(root_path, service)
            outside = root_path / "outside"
            outside.mkdir()
            record = service.store.load("arch")
            record["shared_mounts"].append({
                "host": str(outside), "container": str(document.parent), "options": ""})
            service.store.save(record, None)
            with self.assertRaisesRegex(UnifierError, "shadowed|ambiguous"):
                sessions.prepare(project["project_uuid"], cwd=str(document.parent),
                                 argv=["/bin/true"])

        with tempfile.TemporaryDirectory() as temporary:
            root_path = Path(temporary)
            service, _runtime, _routes, project, _root, document = ready(root_path)
            sessions = ProjectSessions(root_path, service)
            record = service.store.load("arch")
            record["shared_mounts"][0]["options"] = "ro"
            service.store.save(record, None)
            with self.assertRaisesRegex(UnifierError, "read-only"):
                sessions.prepare(project["project_uuid"], cwd=str(document.parent),
                                 argv=["/bin/true"])

    def test_root_or_cwd_symlink_replacement_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime, _routes, project, _root, document = ready(root)
            sessions = ProjectSessions(root, service)
            real = document.parent / "real"
            real.mkdir()
            link = document.parent / "link"
            os.symlink(real, link)
            with self.assertRaisesRegex(UnifierError, "symbolic link"):
                sessions.prepare(project["project_uuid"], cwd=str(link),
                                 argv=["/bin/true"])
            moved = root / "old-project"
            document.parent.rename(moved)
            document.parent.mkdir()
            with self.assertRaisesRegex(UnifierError, "replaced"):
                sessions.prepare(project["project_uuid"], cwd=str(document.parent),
                                 argv=["/bin/true"])

    def test_environment_variable_policy_never_inherits_path_or_loaders(self):
        for key in ("PATH", "LD_PRELOAD", "PYTHONPATH", "BASH_FUNC_x%%"):
            with self.subTest(key=key), self.assertRaises(UnifierError):
                validate_env_keys([key])
        self.assertEqual(validate_env_keys(["LANG", "LANG", "TERM"]),
                         ["LANG", "TERM"])


class Execution(unittest.TestCase):
    def test_execute_revalidates_and_runs_literal_argv_with_pinned_user(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(
                Path(temporary), user_namespace="keep-id")
            sessions = ProjectSessions(Path(temporary), service)
            special = document.parent / 'a "b" $c %.txt'
            special.write_text("x", encoding="utf-8")
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/echo", str(special)],
                cwd=str(document.parent), env_keys=["LANG"])
            with patch.dict(os.environ, {"LANG": "C.UTF-8"}):
                code = sessions.execute(record["session_uuid"])
            self.assertEqual(code, 0)
            event = runtime.session_events[-1]
            self.assertEqual(event["argv"], ["/bin/echo", str(special)])
            self.assertTrue(event["interactive"])
            self.assertFalse(event["tty"])
            self.assertEqual(event["user"], "1000:1000")
            self.assertEqual(event["env"], {"LANG": "C.UTF-8"})
            self.assertEqual(event["workdir"], str(document.parent))
            self.assertEqual(runtime.cleanup_calls[-1][1], "1000:1000")
            view = sessions.show(record["session_uuid"])
            self.assertEqual(view["state"], "exited")
            self.assertEqual(view["exit_code"], 0)
            self.assertIsNone(view["lease_token"])
            self.assertEqual(view["execution_pinned"]["session_user"], "1000:1000")

    def test_environment_remove_trusts_confirmed_gone_guest_after_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
            self.assertEqual(sessions.execute(record["session_uuid"]), 0)
            self.assertEqual(service.inspect("arch")["runtime_state"], "stopped")
            service.remove("arch")
            with self.assertRaisesRegex(UnifierError, "does not exist"):
                service.inspect("arch")

    def test_nonzero_and_127_exits_are_exact(self):
        for code in (3, 127):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                service, runtime, _routes, project, _root, document = ready(Path(temporary))
                runtime.session_code = code
                sessions = ProjectSessions(Path(temporary), service)
                record = sessions.create_session(
                    project["project_uuid"], argv=["/bin/false"],
                    cwd=str(document.parent))
                self.assertEqual(sessions.execute(record["session_uuid"]), code)
                self.assertEqual(sessions.show(record["session_uuid"])["exit_code"], code)

    def test_signalled_guest_normalizes_proxy_exit15_but_voluntary_15_is_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/sleep", "30"],
                cwd=str(document.parent))

            def request_cancel(self, name, argv, **kwargs):
                execution = FakeGuestExecution(self, 0)
                self.last_execution = execution
                self.session_events.append({"argv": list(argv), "user": kwargs.get("user")})
                kwargs["on_spawn"](execution.process)
                os.kill(os.getpid(), signal.SIGTERM)
                return execution

            import types
            runtime.start_session_exec = types.MethodType(request_cancel, runtime)
            self.assertEqual(sessions.execute(record["session_uuid"]), 143)
            self.assertEqual(runtime.signal_calls[-1][1], signal.SIGTERM)
            self.assertIn(signal.SIGTERM,
                          sessions.show(record["session_uuid"])["signals"])

        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            runtime.session_code = 15
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/sh", "-c", "exit 15"],
                cwd=str(document.parent))
            self.assertEqual(sessions.execute(record["session_uuid"]), 15)

    def test_unproven_signal_does_not_turn_a_voluntary_exit_into_143(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/sleep", "30"],
                cwd=str(document.parent))
            runtime.signal_status = "unknown"
            runtime.signal_return = 15

            def request_cancel(self, name, argv, **kwargs):
                execution = FakeGuestExecution(self, 0)
                execution.finished = True
                execution.code = 15
                self.last_execution = execution
                self.session_events.append({"argv": list(argv), "user": kwargs.get("user")})
                kwargs["on_spawn"](execution.process)
                os.kill(os.getpid(), signal.SIGTERM)
                return execution

            import types
            runtime.start_session_exec = types.MethodType(request_cancel, runtime)
            # The guest could not be proven signalled, so no cancellation status
            # is invented; the honest proxy completion remains 15.
            self.assertEqual(sessions.execute(record["session_uuid"]), 15)

    def test_stale_pin_is_refused_before_any_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            latest = service.store.load("arch")
            latest["generation"] = latest["generation"] + 1
            service.store.save(latest, None)
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, "stale|binding|changed"):
                sessions.execute(record["session_uuid"])
            self.assertFalse(any(event[0] == "start" for event in runtime.events))
            self.assertEqual(sessions.show(record["session_uuid"])["state"], "failed")

    def test_user_pin_replaced_after_plan_is_refused_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(
                Path(temporary), user_namespace="keep-id")
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            sessions.store.update(record["session_uuid"], session_user="999:999")
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, "document user changed"):
                sessions.execute(record["session_uuid"])
            self.assertEqual(runtime.session_events, [])

    def test_scope_change_after_plan_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            latest = service.store.load("arch")
            latest["shared_mounts"][0]["options"] = "ro"
            service.store.save(latest, None)
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, "scope|read-only"):
                sessions.execute(record["session_uuid"])
            self.assertEqual(runtime.events, [])

    def test_shared_group_single_start_and_no_premature_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            group = LaunchGroup(service, "arch")
            runtime.events.clear()
            session_token = group.acquire_project(
                project["project_uuid"],
                expected_binding=project["environment_binding"])
            app_token = group.acquire("freecad")
            self.assertEqual(sum(1 for event in runtime.events if event[0] == "start"), 1)
            group.release(session_token)
            self.assertEqual(service.inspect("arch")["runtime_state"], "running")
            self.assertEqual(len([event for event in runtime.events if event[0] == "stop"]), 0)
            group.release(app_token)
            self.assertEqual(service.inspect("arch")["runtime_state"], "stopped")
            self.assertEqual(sum(1 for event in runtime.events if event[0] == "stop"), 1)


class RecoveryAndCancellation(unittest.TestCase):
    def test_recover_never_releases_a_live_worker_or_child(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            group = LaunchGroup(service, "arch")
            token = group.acquire_project(
                project["project_uuid"], expected_binding=project["environment_binding"])
            sessions.store.update(
                record["session_uuid"], state="running", lease_token=token,
                worker_pid=os.getpid(), worker_stamp=process_stamp(os.getpid()),
                child_pid=os.getpid(), child_stamp=process_stamp(os.getpid()))
            results = sessions.recover(record["session_uuid"])
            self.assertEqual(results[0]["action"], "kept-live")
            self.assertIn(token, group.read()["leases"])
            with self.assertRaisesRegex(UnifierError, "live"):
                sessions.retire(record["session_uuid"])
            with self.assertRaisesRegex(UnifierError, "still live"):
                service.remove("arch")
            group.release(token)

    def test_dead_proxy_without_guest_proof_keeps_the_lease_unresolved(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            group = LaunchGroup(service, "arch")
            token = group.acquire_project(
                project["project_uuid"], expected_binding=project["environment_binding"])
            sessions.store.update(
                record["session_uuid"], state="running", lease_token=token,
                worker_pid=999999, worker_stamp=1, child_pid=None, child_stamp=None)
            results = sessions.recover(record["session_uuid"])
            self.assertEqual(results[0]["action"], "unresolved-guest-unknown")
            self.assertIn(token, group.read()["leases"])
            self.assertEqual(sessions.show(record["session_uuid"])["state"], "unresolved")

            second = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            sessions.store.update(
                second["session_uuid"], state="running",
                worker_pid=999999, worker_stamp=1,
                child_pid=os.getpid(), child_stamp=process_stamp(os.getpid()))
            results = sessions.recover(second["session_uuid"])
            self.assertEqual(results[0]["action"], "unresolved-proxy-alive")
            self.assertEqual(sessions.show(second["session_uuid"])["state"], "unresolved")

    def test_live_and_unknown_guest_keep_the_lease(self):
        for guest_status, action in (("alive", "unresolved-guest-alive"),
                                     ("unknown", "unresolved-guest-unknown")):
            with self.subTest(status=guest_status), tempfile.TemporaryDirectory() as temporary:
                service, runtime, _routes, project, _root, document = ready(Path(temporary))
                runtime.probe_status = guest_status
                sessions = ProjectSessions(Path(temporary), service)
                record = sessions.create_session(
                    project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
                group = LaunchGroup(service, "arch")
                token = group.acquire_project(
                    project["project_uuid"],
                    expected_binding=project["environment_binding"])
                sessions.store.update(
                    record["session_uuid"], state="running", lease_token=token,
                    worker_pid=999999, worker_stamp=1,
                    guest_pid=123456, guest_start=99, guest_pgrp=123456,
                    guest_sid=123456, guest_token="b" * 32, guest_state="alive")
                results = sessions.recover(record["session_uuid"])
                self.assertEqual(results[0]["action"], action)
                self.assertIn(token, group.read()["leases"])
                self.assertEqual(runtime.probe_calls[-1][1], "0:0")
                group.release(token)

    def test_confirmed_gone_guest_releases_its_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            runtime.probe_status = "gone"
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
            group = LaunchGroup(service, "arch")
            token = group.acquire_project(
                project["project_uuid"], expected_binding=project["environment_binding"])
            sessions.store.update(
                record["session_uuid"], state="running", lease_token=token,
                worker_pid=999999, worker_stamp=1,
                guest_pid=123456, guest_start=99, guest_pgrp=123456,
                guest_sid=123456, guest_token="b" * 32, guest_state="alive")
            results = sessions.recover(record["session_uuid"])
            self.assertEqual(results[0]["action"], "recovered")
            self.assertNotIn(token, group.read()["leases"])
            self.assertEqual(sessions.show(record["session_uuid"])["state"], "interrupted")

    def test_confirmed_gone_guest_releases_a_token_without_host_worker_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            runtime.probe_status = "gone"
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
            group = LaunchGroup(service, "arch")
            token = group.acquire_project(
                project["project_uuid"], expected_binding=project["environment_binding"])
            sessions.store.update(
                record["session_uuid"], state="running", lease_token=token,
                worker_pid=None, worker_stamp=None,
                guest_pid=123456, guest_start=99, guest_pgrp=123456,
                guest_sid=123456, guest_token="b" * 32, guest_state="alive")
            results = sessions.recover(record["session_uuid"])
            self.assertEqual(results[0]["action"], "recovered")
            self.assertNotIn(token, group.read()["leases"])

    def test_planned_session_that_never_started_is_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
            results = sessions.recover(record["session_uuid"])
            self.assertEqual(results[0]["action"], "failed-planned")
            self.assertEqual(sessions.show(record["session_uuid"])["state"], "failed")

    def test_direct_cancel_targets_guest_with_same_user_and_releases_only_after_proof(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(
                Path(temporary), user_namespace="keep-id")
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(
                project["project_uuid"], argv=["/bin/true"], cwd=str(document.parent))
            group = LaunchGroup(service, "arch")
            token = group.acquire_project(
                project["project_uuid"], expected_binding=project["environment_binding"])
            guest = GuestProcess(pid=123456, start=99, pgrp=123456,
                                 sid=123456, token="b" * 32)
            sessions.store.update(
                record["session_uuid"], state="running", lease_token=token,
                worker_pid=999999, worker_stamp=1,
                guest_pid=guest.pid, guest_start=guest.start,
                guest_pgrp=guest.pgrp, guest_sid=guest.sid,
                guest_token=guest.token, guest_state="alive")
            runtime.probe_status = "gone"
            result = sessions.cancel(record["session_uuid"], signal.SIGTERM)
            self.assertEqual(result["action"], "cancelled")
            self.assertEqual(runtime.signal_calls[-1][1], signal.SIGTERM)
            self.assertEqual(runtime.signal_calls[-1][2], "1000:1000")
            self.assertEqual(runtime.probe_calls[-1][1], "1000:1000")
            self.assertEqual(runtime.cleanup_calls[-1][1], "1000:1000")
            self.assertNotIn(token, group.read()["leases"])

    def test_launcher_spawns_one_detached_worker_and_maps_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            record = sessions.create_session(project["project_uuid"], argv=["/bin/true"],
                                             cwd=str(document.parent))
            calls = []

            class Worker:
                def poll(self):
                    return None

                def wait(self):
                    return 17

            def popen(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                return Worker()

            with patch.object(sessions_module.subprocess, "Popen", popen):
                result = sessions.launch(record)
            self.assertEqual(result["returncode"], 17)
            command, kwargs = calls[0]
            self.assertEqual(command[1:4], [str(ROOT / "tools/unifier.py"), "project", "_worker"])
            self.assertIn(record["session_uuid"], command)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"][sessions_module.WORKER_MARKER], "1")

    def test_shell_tty_is_only_granted_for_a_real_terminal(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            with patch.object(sessions, "launch", side_effect=lambda record: record):
                with patch.object(sessions_module.sys.stdin, "isatty", return_value=True), \
                     patch.object(sessions_module.sys.stdout, "isatty", return_value=True):
                    record = sessions.shell(project["project_uuid"],
                                            cwd=str(document.parent))
                self.assertTrue(record["tty"])
                self.assertEqual(record["argv"], ["/bin/sh"])
            with patch.object(sessions_module.sys.stdin, "isatty", return_value=False), \
                 patch.object(sessions_module.sys.stdout, "isatty", return_value=False):
                with self.assertRaisesRegex(UnifierError, "--tty"):
                    sessions.shell(project["project_uuid"], tty=True)

    def test_run_cli_special_parses_argv_after_double_dash(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            sessions = ProjectSessions(Path(temporary), service)
            original = cli.session_factory
            cli.session_factory = lambda: sessions
            try:
                with patch.dict(os.environ, {sessions_module.WORKER_MARKER: "1"}):
                    code = cli.main([
                        "project", "run", project["project_uuid"],
                        "--cwd", str(document.parent), "--",
                        "/bin/echo", "a b", "--not-a-cli-option",
                    ])
                self.assertEqual(code, 0)
                self.assertEqual(runtime.session_events[-1]["argv"],
                                 ["/bin/echo", "a b", "--not-a-cli-option"])
            finally:
                cli.session_factory = original
            with patch("sys.stderr", new_callable=__import__("io").StringIO):
                self.assertEqual(
                    cli.main(["project", "run", project["project_uuid"], "true"]), 1)


class RuntimeArguments(unittest.TestCase):
    def test_exec_argv_adds_i_for_pipes_t_only_when_requested_and_user(self):
        base = dict(workdir="/work", env={"LANG": "C"})
        plain = exec_argv("ml-arch", ["/bin/echo", "x"], **base)
        piped = exec_argv("ml-arch", ["/bin/echo", "x"], interactive=True, **base)
        tty = exec_argv("ml-arch", ["/bin/sh"], interactive=True, tty=True, **base)
        user = exec_argv("ml-arch", ["/bin/echo", "x"], user="1000:1000",
                         interactive=True, **base)
        self.assertNotIn("-i", plain)
        self.assertNotIn("-t", plain)
        self.assertEqual(piped.count("-i"), 1)
        self.assertNotIn("-t", piped)
        self.assertEqual(tty.count("-i"), 1)
        self.assertEqual(tty.count("-t"), 1)
        self.assertEqual(user[user.index("--user") + 1], "1000:1000")
        self.assertEqual(piped[-3:], ["ml-arch", "/bin/echo", "x"])


def _proc_start(pid):
    line = Path(f"/proc/{pid}/stat").read_text()
    return int(line.rsplit(")", 1)[1].split()[19])


def _run_guest_script(script, *args):
    result = subprocess.run(
        ["/bin/sh", "-c", script, "ml-test", *[str(item) for item in args]],
        capture_output=True, text=True, timeout=20)
    return result


@unittest.skipUnless(shutil.which("setsid") and Path("/proc/self/stat").is_file(),
                     "the host-local guest protocol needs /proc and setsid")
class GuestProtocol(unittest.TestCase):
    """Run the fixed guest shell helper protocol on this host, without podman."""

    def test_wrapper_reports_pid_start_session_and_keeps_argv_literal(self):
        read_fd, write_fd = os.pipe()
        os.set_inheritable(write_fd, True)
        token = "0123456789abcdef" * 2
        argv = guest_session_argv(
            ["/bin/sh", "-c", 'printf "%s" "$1"', "ml-argv", "a b;c%"],
            token=token, control_fd=write_fd, tty=False)
        process = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            pass_fds=(write_fd,), close_fds=True)
        os.close(write_fd)
        try:
            line = os.read(read_fd, 4096).decode("utf-8")
        finally:
            os.close(read_fd)
        guest = parse_guest_ready(line, token)
        self.assertEqual(guest.pid, guest.sid)
        self.assertEqual(guest.pid, guest.pgrp)
        self.assertGreater(guest.start, 0)
        out, err = process.communicate(timeout=10)
        self.assertEqual(out, b"a b;c%")
        self.assertEqual(err, b"")
        self.assertEqual(process.returncode, 0)

    def test_probe_and_targeted_signal_use_pid_plus_start(self):
        victim = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        bystander = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        try:
            start = _proc_start(victim.pid)
            probe = _run_guest_script(GUEST_PROBE, victim.pid, start)
            self.assertEqual(probe.stdout.strip(), "ALIVE")
            signal_result = _run_guest_script(
                GUEST_SIGNAL, victim.pid, start, "TERM", "session")
            self.assertEqual(signal_result.stdout.strip(), "SIGNALLED")
            victim.wait(timeout=10)
            self.assertEqual(victim.returncode, -signal.SIGTERM)
            self.assertIsNone(bystander.poll())
            self.assertEqual(
                _run_guest_script(GUEST_PROBE, victim.pid, start).stdout.strip(),
                "GONE")
            replaced = _run_guest_script(
                GUEST_PROBE, bystander.pid, _proc_start(bystander.pid) + 1)
            self.assertEqual(replaced.stdout.strip(), "REPLACED")
        finally:
            for process in (victim, bystander):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)

    def test_cleanup_group_mode_covers_an_own_process_group_without_session(self):
        script = (
            "import os, subprocess, sys, time\n"
            "os.setpgid(0, 0)\n"
            "subprocess.Popen(['/bin/sleep', '30'])\n"
            "print(os.getpid(), flush=True)\n"
            "time.sleep(30)\n")
        process = subprocess.Popen(
            [sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        try:
            pid_text = process.stdout.readline().strip()
            pid = int(pid_text)
            start = _proc_start(pid)
            guest = GuestProcess(pid=pid, start=start, pgrp=pid, sid=0,
                                 token="c" * 32)
            self.assertNotEqual(guest.sid, guest.pid)
            result = _run_guest_script(GUEST_CLEANUP, pid, "group")
            self.assertEqual(result.stdout.strip(), "CLEANED", result.stderr)
            process.wait(timeout=10)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def test_cleanup_removes_only_this_sessions_background_child(self):
        marker = tempfile.NamedTemporaryFile(prefix="ml-e-child-", delete=False)
        marker.close()
        blocker = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
        script = ('(sleep 30) & echo $! > "$1"; wait')
        session = subprocess.Popen(
            ["/bin/sh", "-c", script, "ml-cleanup-test", marker.name],
            start_new_session=True)
        try:
            deadline = time.monotonic() + 5
            child_pid = None
            while time.monotonic() < deadline:
                text = Path(marker.name).read_text().strip()
                if text:
                    child_pid = int(text)
                    break
                time.sleep(0.02)
            self.assertIsNotNone(child_pid)
            result = _run_guest_script(GUEST_CLEANUP, session.pid)
            self.assertEqual(result.stdout.strip(), "CLEANED", result.stderr)
            session.wait(timeout=10)
            self.assertIsNone(blocker.poll())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    break
                state = Path(f"/proc/{child_pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
                if state == "Z":
                    break
                time.sleep(0.02)
            else:
                self.fail("background guest child survived session cleanup")
        finally:
            Path(marker.name).unlink(missing_ok=True)
            for process in (session, blocker):
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=5)
            if child_pid is not None:
                try:
                    os.kill(child_pid, 0)
                except ProcessLookupError:
                    pass
                else:
                    try:
                        os.kill(child_pid, signal.SIGKILL)
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
