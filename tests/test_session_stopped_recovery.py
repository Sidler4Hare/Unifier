"""Recovery of a session whose exact container instance is stopped.

The real failure this covers: a project session journaled a real guest identity
(``guest_pid`` + ``guest_start``), its host worker and its exec proxy are both
gone, and the container it ran in has since stopped.  Probing that stopped
container cannot answer, so recovery used to keep ``unknown`` forever and the
lease was never released.  Only the *exact* instance the session is bound to
being stopped is proof; a same-name replacement, a new generation, a foreign
label or an uninspectable container keeps the session unresolved.

No real podman and no container start happens here.
"""

from dataclasses import replace
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup, process_stamp
from unifier.project_sessions import ProjectSessions
from unifier.runtime import ROLE_ENVIRONMENT
from test_project_sessions import SessionRuntime, ready


class IdAwareRuntime(SessionRuntime):
    """A stub that, like podman, answers ``inspect`` for the full container id.

    The shared stubs key containers by name only, so they cannot show that the
    guest probe is aimed at a pinned id.  This one registers the same instance
    under its id as well, which is the behaviour the real runtime has.
    """

    def __init__(self):
        super().__init__()
        self.probe_names = []
        self.inspect_calls = []

    def inspect(self, name):
        info = self.containers.get(name)
        if info is None:
            for stored in self.containers.values():
                if stored.container_id == name:
                    info = stored
                    break
        self.inspect_calls.append(name)
        return info

    def _owned(self, name, project, env_id):
        info = self.inspect(name)
        if info is None:
            raise UnifierError(f"container {name} does not exist")
        if (info.project != project or info.env_id != env_id
                or info.role != ROLE_ENVIRONMENT):
            raise UnifierError(f"refusing to touch {name}")
        return info

    def probe_guest(self, name, guest, *, project, env_id, user=None, timeout=10):
        self.probe_names.append(name)
        return super().probe_guest(name, guest, project=project, env_id=env_id,
                                   user=user, timeout=timeout)


def live_session(root: Path, runtime, sessions=None):
    """Journal a running session with a real guest identity and a live lease.

    The host worker identity is dead on purpose: that is the restart case.  The
    guest identity, the lease and the running container are the state the next
    host process has to recover.
    """
    service, runtime, _routes, project, _root, document = ready(root, runtime=runtime)
    sessions = sessions or ProjectSessions(root, service)
    record = sessions.create_session(project["project_uuid"], argv=["/bin/sh"],
                                     cwd=str(document.parent))
    group = LaunchGroup(service, "arch")
    token = group.acquire_project(
        project["project_uuid"], expected_binding=record["environment_binding"])
    sessions.store.update(
        record["session_uuid"], state="running", lease_token=token,
        worker_pid=999999, worker_stamp=1,
        guest_pid=424242, guest_start=4242, guest_pgrp=424242,
        guest_sid=424242, guest_token="b" * 32, guest_state="alive")
    return service, sessions, group, record, token


def stopped(runtime, container: str):
    return replace(runtime.containers[container], running=False, status="exited",
                   pid=0, finished_at="2026-11-03T12:00:00+08:00")


class StoppedInstanceReleasesItsLease(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.runtime = IdAwareRuntime()
        (self.service, self.sessions, self.group, self.record,
         self.token) = live_session(self.root, self.runtime)
        self.env_record = self.service.store.load("arch")
        self.container = self.env_record["runtime"]["container_name"]
        self.container_id = self.env_record["runtime"]["container_id"]

    def leases(self) -> list:
        return list(self.group.read().get("leases", {}))

    def test_exactly_stopped_instance_is_gone_and_releases_the_lease(self):
        info = self.runtime.containers[self.container]
        self.assertTrue(info.running)
        # The container stopped on its own: the same instance, same id, same
        # labels.  That, not a dead worker pid, is the proof we need.
        self.runtime.containers[self.container] = stopped(self.runtime, self.container)
        starts = [event for event in self.runtime.events if event[0] == "start"]
        self.assertEqual(self.sessions.show(self.record["session_uuid"])["guest_state"],
                         "gone")
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results], ["recovered"])
        view = self.sessions.show(self.record["session_uuid"])
        self.assertEqual(view["state"], "interrupted")
        self.assertEqual(view["effective_state"], "interrupted")
        self.assertEqual(view["guest_state"], "gone")
        self.assertFalse(view["guest_live"])
        self.assertIsNone(view["lease_token"])
        self.assertNotIn(self.token, self.leases())
        # Recovery never probes a stopped container and never starts it again.
        self.assertEqual(self.runtime.probe_calls, [])
        self.assertEqual([event for event in self.runtime.events if event[0] == "start"],
                         starts)
        self.assertFalse(self.runtime.containers[self.container].running)
        self.assertEqual(self.service.inspect("arch")["runtime_state"], "stopped")

    def test_relative_state_root_uses_the_same_canonical_project_label(self):
        self.runtime.containers[self.container] = stopped(self.runtime, self.container)
        relative = ProjectSessions(Path(os.path.relpath(self.root)), self.service)
        results = relative.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results], ["recovered"])
        self.assertNotIn(self.token, self.leases())

    def test_replacement_of_the_same_name_keeps_the_session_unresolved(self):
        info = self.runtime.containers[self.container]
        self.runtime.containers[self.container] = replace(
            stopped(self.runtime, self.container), container_id="cid-replacement")
        view = self.sessions.show(self.record["session_uuid"])
        self.assertEqual(view["guest_state"], "unknown")
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results],
                         ["unresolved-guest-unknown"])
        self.assertIn(self.token, self.leases())
        self.assertEqual(self.runtime.probe_calls, [])
        # A stopped replacement is never allowed to prove the old execution ended.
        self.runtime.containers[self.container] = replace(
            info, container_id="cid-replacement")
        self.assertEqual(self.sessions.show(self.record["session_uuid"])["guest_state"],
                         "unknown")
        with self.assertRaises(UnifierError):
            self.sessions.cancel(self.record["session_uuid"])
        self.assertEqual(self.runtime.signal_calls, [])

    def test_id_inspection_failure_never_falls_back_to_a_mutable_name(self):
        original = self.runtime.inspect
        self.runtime.inspect = lambda name: None if name == self.container_id else original(name)
        self.assertEqual(self.sessions.show(self.record["session_uuid"])["guest_state"],
                         "unknown")
        self.assertEqual(self.runtime.probe_calls, [])

    def test_generation_move_and_label_mismatch_are_not_proof(self):
        self.runtime.containers[self.container] = stopped(self.runtime, self.container)
        record = self.service.store.load("arch")
        record["generation"] = 2
        self.service.store.save(record, None)
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results],
                         ["unresolved-guest-unknown"])
        self.assertIn(self.token, self.leases())
        record = self.service.store.load("arch")
        record["generation"] = 1
        self.service.store.save(record, None)
        for change in ({"project": "another-project"}, {"role": "application"},
                       {"env_id": "another-environment"}):
            with self.subTest(**change):
                info = self.runtime.containers[self.container]
                self.runtime.containers[self.container] = replace(info, **change)
                self.assertEqual(
                    self.sessions.show(self.record["session_uuid"])["guest_state"],
                    "unknown")
                results = self.sessions.recover(self.record["session_uuid"])
                self.assertEqual([item["action"] for item in results][:1],
                                 ["unresolved-guest-unknown"])
                self.assertIn(self.token, self.leases())
                self.runtime.containers[self.container] = info

    def test_uninspectable_container_stays_unknown(self):
        self.runtime.containers[self.container] = stopped(self.runtime, self.container)
        # The container record is gone entirely.
        name = self.container
        instance = self.runtime.containers.pop(name)
        self.assertEqual(self.sessions.show(self.record["session_uuid"])["guest_state"],
                         "unknown")
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results],
                         ["unresolved-guest-unknown"])
        self.assertIn(self.token, self.leases())
        self.runtime.containers[name] = instance

        def explode(_name):
            raise UnifierError("podman inspect failed")

        original = self.runtime.inspect
        self.runtime.inspect = explode
        try:
            self.assertEqual(self.sessions.show(self.record["session_uuid"])["guest_state"],
                             "unknown")
        finally:
            self.runtime.inspect = original
        self.assertIn(self.token, self.leases())

    def test_a_live_instance_is_probed_at_the_pinned_container_id(self):
        self.runtime.probe_status = "alive"
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results],
                         ["unresolved-guest-alive"])
        self.assertIn(self.token, self.leases())
        # The probe is aimed at the pinned full id, not at the replaceable name.
        self.assertEqual(self.runtime.probe_names[-1], self.container_id)
        self.assertNotIn(self.container, self.runtime.probe_names)
        self.assertEqual(self.runtime.probe_calls[-1][1], "0:0")
        self.assertTrue(self.runtime.containers[self.container].running)
        # And the same pinned id is what a proven-gone answer releases on.
        self.runtime.probe_status = "gone"
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results], ["recovered"])
        self.assertNotIn(self.token, self.leases())
        self.assertEqual(self.runtime.probe_names[-1], self.container_id)

    def test_recovery_keeps_worker_and_proxy_boundaries(self):
        # A live host worker is never touched by the stopped-container proof.
        self.sessions.store.update(
            self.record["session_uuid"], worker_pid=os.getpid(),
            worker_stamp=process_stamp(os.getpid()))
        self.runtime.containers[self.container] = stopped(self.runtime, self.container)
        results = self.sessions.recover(self.record["session_uuid"])
        self.assertEqual([item["action"] for item in results], ["kept-live"])
        self.assertIn(self.token, self.leases())
        self.assertEqual(self.runtime.probe_calls, [])


if __name__ == "__main__":
    unittest.main()
