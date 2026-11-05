"""Boundary regressions for the host-issued guest request bridge (no real podman).

These cover the parts the bridge is trusted for: the host-issued policy is the
only scope, the origin project session identity is re-derived from host state on
every request, ``request_uuid`` is deduplicated durably, and a run that cannot
be proven ended is reported as unresolved instead of a false success.
"""

from dataclasses import replace
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import socket
import stat
import struct
import sys
import tempfile
import threading
import time
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier import agent_bridge as bridge_module
from unifier import bridge_client
from unifier.agent_bridge import (
    AgentBridge,
    BridgeError,
    RequestLedger,
    host_info,
    load_policy,
    parse_policy,
    strict_dumps,
    validate_request,
    validate_request_record,
)
from unifier.errors import UnifierError
from unifier.identity import binding_for
from unifier.on_demand import LaunchGroup, process_stamp
from unifier.project_sessions import ProjectSessions
from unifier.runtime import GuestProcess
from test_desktop import SESSION
from test_project_sessions import SessionRuntime, ready


class HoldRuntime(SessionRuntime):
    """A runtime whose guest ends only when its exact identity is signalled.

    That is the whole point of the timeout tests: killing the host proxy alone
    must never look like a finished execution.  A guest identity listed in
    ``alive_guests`` always answers ``alive``, which is how a *live* origin
    session is journalled without every probe having to agree about it.
    """

    def __init__(self):
        super().__init__()
        self.hold = False
        self.alive_guests = set()

    def start_session_exec(self, name, argv, *, project, env_id, workdir, env,
                           user=None, interactive=True, tty=False, on_spawn=None,
                           control_timeout=None):
        execution = super().start_session_exec(
            name, argv, project=project, env_id=env_id, workdir=workdir, env=env,
            user=user, interactive=interactive, tty=tty, on_spawn=on_spawn,
            control_timeout=control_timeout)
        execution.finished = not self.hold
        return execution

    def probe_guest(self, name, guest, *, project, env_id, user=None, timeout=10):
        if guest.pid in self.alive_guests:
            return "alive"
        return super().probe_guest(name, guest, project=project, env_id=env_id,
                                   user=user, timeout=timeout)

    def signal_guest(self, name, guest, signum, *, project, env_id, user=None,
                     timeout=10):
        status = super().signal_guest(name, guest, signum, project=project,
                                      env_id=env_id, user=user, timeout=timeout)
        if status == "signalled" and self.last_execution is not None:
            # The guest itself received the targeted signal, so the execution
            # and therefore its host proxy really do end here.
            self.last_execution.finished = True
            self.last_execution.code = self.signal_return
        return status


def private_directory(root: Path, name: str = "private") -> Path:
    path = root / name
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(str(path), 0o700)
    return path


def deep_directory(root: Path, parts: int = 6) -> Path:
    """A real directory whose full path is long and non-ASCII."""
    path = root
    for index in range(parts):
        path = path / ("桥接口目录-segment-%02d" % index)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(path), 0o700)
    return path


def write_policy(root: Path, document, *, name: str | None = None,
                 mode: int = 0o600, directory: Path | None = None) -> Path:
    directory = directory or private_directory(root, "policy-" + uuid.uuid4().hex[:8])
    path = directory / (name or "policy.json")
    path.write_text(json.dumps(document, sort_keys=True), encoding="utf-8")
    os.chmod(str(path), mode)
    return path


GRANT_SECONDS = 10 * 3600


class BridgeHarness(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        self.addCleanup(self._temporary.cleanup)
        self.runtime = HoldRuntime()
        (self.service, self.runtime, self.routes, self.project,
         self.data_root, self.document) = ready(self.root, runtime=self.runtime)
        self.sessions = ProjectSessions(self.root, self.service)
        self.token = secrets.token_hex(32)
        self.now = [1_700_000_000.0]
        self.clock = lambda: self.now[0]
        self._guest_sequence = 500000
        self.origin = self.start_origin()

    # ----- fixture plumbing ------------------------------------------------
    def start_origin(self, project: dict | None = None, environment_id: str = "arch",
                     cwd: str | None = None) -> str:
        """Journal a live origin session the way the host worker would.

        "Live" means what ``ProjectSessions.show`` has to be able to prove: a
        worker identity, a real guest pid plus start time, and a lease that is
        still held in the shared launch group.
        """
        project = project or self.project
        cwd = cwd if cwd is not None else str(self.document.parent)
        record = self.sessions.create_session(
            project["project_uuid"], argv=["/bin/sh"], cwd=cwd, mode="shell")
        token = LaunchGroup(self.service, environment_id).acquire_project(
            project["project_uuid"],
            expected_binding=record["environment_binding"])
        self._guest_sequence += 1
        guest = GuestProcess(pid=self._guest_sequence, start=1000 + self._guest_sequence,
                             pgrp=self._guest_sequence, sid=self._guest_sequence,
                             token="a" * 32)
        self.sessions.store.update(
            record["session_uuid"], state="running", lease_token=token,
            worker_pid=os.getpid(), worker_stamp=process_stamp(os.getpid()),
            guest_pid=guest.pid, guest_start=guest.start, guest_pgrp=guest.pgrp,
            guest_sid=guest.sid, guest_token=guest.token, guest_state="alive")
        self.runtime.alive_guests.add(guest.pid)
        self.lease_token = token
        self.origin_guest = guest
        return record["session_uuid"]

    def add_object(self, environment_id: str = "debian13", alias: str = "deb"):
        """A second managed object: its own environment instance and project.

        The cross-object tests need a target that is genuinely a different
        environment, not the origin wearing another name.
        """
        project = self.root / (alias + "-project")
        project.mkdir(exist_ok=True)
        (project / "notes.txt").write_text("notes\n", encoding="utf-8")
        self.service.create(environment_id, None, [str(project)], desktop=True,
                            session=SESSION, user_namespace="rootless-default")
        root_record = self.routes.roots.add(str(project))
        record = self.routes.add_project(
            root_record["root_uuid"], environment_id, alias=alias,
            default_save_root=str(project), intent_handlers={})
        return record, str(project)

    def binding_of(self, environment_id: str) -> dict:
        return binding_for(self.service.store.load(environment_id))

    def container_of(self, environment_id: str) -> str:
        return self.service.store.load(environment_id)["runtime"]["container_name"]

    def origin_record(self) -> dict:
        return self.sessions.store.read(self.origin)

    def origin_binding(self) -> dict:
        return dict(self.origin_record()["environment_binding"])

    def run_action(self, *, action_id: str = "run-echo", project: dict | None = None,
                   environment_id: str = "arch", **overrides) -> dict:
        action = {
            "action_id": action_id, "type": "project-run",
            "project_uuid": (project or self.project)["project_uuid"],
            "environment_binding": self.binding_of(environment_id),
            "argv": ["/bin/echo", "hello from the host"],
            "cwd": str(self.document.parent if project is None else project.get(
                "default_save_root")),
            "timeout_seconds": 1,
        }
        action.update(overrides)
        return action

    def policy_document(self, *, actions=None, token=None, origin: str | None = None,
                        origin_binding: dict | None = None, expires_at=None,
                        **overrides) -> dict:
        document = {
            "schema_version": 1,
            "token": token or self.token,
            "socket": str(self.root / "bridge" / "bridge.sock"),
            "expires_at": (self.now[0] + GRANT_SECONDS
                           if expires_at is None else expires_at),
            "origin": {"session_uuid": origin or self.origin,
                       "environment_binding": (origin_binding if origin_binding
                                               is not None else self.origin_binding())},
            "actions": actions if actions is not None
            else [{"action_id": "host-info", "type": "host-info"}, self.run_action()],
        }
        document.update(overrides)
        return document

    def policy_file(self, **kwargs) -> Path:
        return write_policy(self.root, self.policy_document(**kwargs))

    def build(self, *, document=None, path=None, **kwargs) -> AgentBridge:
        policy_path = path or write_policy(
            self.root, document if document is not None else self.policy_document())
        bridge = AgentBridge(load_policy(policy_path), self.sessions,
                             clock=self.clock, **kwargs)
        bridge.test_servers = []
        self.addCleanup(self.finish_bridge, bridge)
        return bridge

    def finish_bridge(self, bridge):
        # End the deliberately held fake guest, then join every broker thread
        # before later repository tests fork or the temporary registry vanishes.
        self.runtime.hold = False
        self.runtime.probe_status = "gone"
        self.runtime.cleanup_status = "gone"
        if self.runtime.last_execution is not None:
            self.runtime.last_execution.finished = True
        bridge.stop()
        bridge.close()
        for thread in bridge.test_servers:
            thread.join(2)
            self.assertFalse(thread.is_alive(), "broker listener leaked from test")
        self.assertFalse(any(item["thread_alive"] for item in bridge.unresolved_runs()))

    def serve(self, bridge: AgentBridge, *, requests: int = 1) -> AgentBridge:
        if getattr(bridge, "_server", None) is None:
            bridge.bind()
        thread = threading.Thread(target=bridge.serve_forever,
                                  kwargs={"max_requests": requests}, daemon=True)
        bridge.test_servers.append(thread)
        thread.start()
        return bridge

    def call(self, bridge: AgentBridge, action_id: str, *, request_uuid=None,
             token=None, timeout: float = 30.0) -> dict:
        return bridge_client.request(str(bridge.policy.socket_path), action_id,
                                     token=token or self.token,
                                     request_uuid=request_uuid, timeout=timeout)

    def serve_and_call(self, bridge: AgentBridge, action_id: str, **kwargs) -> dict:
        thread_count = kwargs.pop("expected_requests", 1)
        self.serve(bridge, requests=thread_count)
        return self.call(bridge, action_id, **kwargs)

    def raw_bytes(self, bridge: AgentBridge, payload: bytes, *,
                  timeout: float = 5.0) -> bytes:
        """Send raw bytes and read one answer frame (b"" when the peer closes).

        The connection is anchored on a verified directory FD, exactly like the
        client's, so the raw cases keep working when the socket path is longer
        than the AF_UNIX limit.
        """
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(timeout)
        with bridge_client.anchored_socket_address(bridge.policy.socket_path) as address:
            connection.connect(address)
        try:
            connection.sendall(payload)
            header = connection.recv(4)
            if len(header) < 4:
                return b""
            size = struct.unpack(">I", header)[0]
            chunks = bytearray()
            while len(chunks) < size:
                chunk = connection.recv(size - len(chunks))
                if not chunk:
                    break
                chunks.extend(chunk)
            return bytes(chunks)
        finally:
            connection.close()

    def raw_text(self, bridge: AgentBridge, text: str, **kwargs) -> dict:
        body = self.raw_bytes(
            bridge,
            struct.pack(">I", len(text.encode())) + text.encode("utf-8"),
            **kwargs)
        self.assertNotEqual(body, b"", "the bridge closed the connection early")
        return json.loads(body.decode("utf-8"))

    def request_text(self, *, action_id="host-info", request_uuid=None,
                     token=None, **extra) -> str:
        payload = {"protocol_version": 1, "action_id": action_id,
                   "request_uuid": request_uuid or str(uuid.uuid4()),
                   "token": token or self.token}
        payload.update(extra)
        return json.dumps(payload)

    def ledger(self) -> RequestLedger:
        return RequestLedger(self.root, clock=self.clock)

    def ledger_file(self) -> Path:
        return self.ledger().path

    def ledger_records(self) -> list:
        return self.ledger().records()


# ---------------------------------------------------------------------------
# valid use
# ---------------------------------------------------------------------------
class ValidRequests(BridgeHarness):
    def test_client_rejects_a_response_for_another_request_or_action(self):
        bridge = self.build()
        original = bridge.respond
        wrong = ["request_uuid"]

        def mismatched(payload, *, peer_uid=None):
            answer = original(payload, peer_uid=peer_uid)
            answer[wrong[0]] = str(uuid.uuid4()) if wrong[0] == "request_uuid" else "other-action"
            return answer

        bridge.respond = mismatched
        self.serve(bridge, requests=2)
        for key in ("request_uuid", "action_id"):
            wrong[0] = key
            with self.subTest(key=key), self.assertRaises(BridgeError) as caught:
                self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
            self.assertEqual(caught.exception.code, "bad-response")

    def test_nonzero_target_exit_is_unsuccessful_and_preserves_its_exit_code(self):
        self.runtime.session_code = 7
        answer = self.serve_and_call(self.build(), "run-echo",
                                     request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "failed", answer)
        self.assertEqual(answer["receipt"]["session"]["exit_code"], 7)
        self.assertEqual(answer["receipt"]["session"]["guest_state"], "gone")
        self.assertEqual(bridge_client.STATUS_EXIT[answer["status"]], 3)

    def test_host_info_returns_host_evidence_over_a_bounded_frame(self):
        bridge = self.build()
        answer = self.serve_and_call(bridge, "host-info",
                                     request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "ok")
        self.assertEqual(
            sorted(answer), ["action_id", "error", "protocol_version", "receipt",
                             "request_uuid", "status"])
        receipt = answer["receipt"]
        self.assertEqual(receipt["action_type"], "host-info")
        self.assertEqual(receipt["origin"]["session_uuid"], self.origin)
        self.assertEqual(receipt["origin"]["environment_binding"],
                         self.origin_binding())
        evidence = receipt["host_info"]
        self.assertEqual(evidence["source"], "host-builtin")
        self.assertEqual(evidence["os_release"], host_info()["os_release"])
        self.assertEqual(evidence["identity"]["uid"], os.getuid())
        self.assertEqual(evidence["identity"]["pid"], os.getpid())
        self.assertEqual(evidence["uname"]["machine"], os.uname().machine)
        release = evidence["os_release"]
        if "values" in release:
            self.assertNotIn("text", release)
            self.assertTrue(set(release["values"]) <= {
                "ID", "ID_LIKE", "NAME", "PRETTY_NAME", "VERSION", "VERSION_ID",
                "VARIANT", "VARIANT_ID"})
        else:
            self.assertIn("unavailable", release)
        identity = evidence["process_identity"]
        self.assertIn("status", identity)
        self.assertIn("cgroup", identity)
        self.assertIn("uid_map", identity)
        if "values" in identity["status"]:
            self.assertNotIn("text", identity["status"])
        persisted = self.ledger_records()
        self.assertEqual(len(persisted), 1)
        self.assertEqual(persisted[0]["state"], "completed")
        self.assertEqual(persisted[0]["request_uuid"], answer["request_uuid"])
        self.assertEqual(persisted[0]["receipt"]["status"], "ok")

    def test_project_run_executes_the_host_pinned_argv_and_keeps_lease_rules(self):
        bridge = self.build()
        answer = self.serve_and_call(bridge, "run-echo",
                                     request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "ok", answer.get("error"))
        event = self.runtime.session_events[-1]
        self.assertEqual(event["argv"], ["/bin/echo", "hello from the host"])
        self.assertEqual(event["workdir"], str(self.document.parent))
        self.assertEqual(event["env"], {})
        self.assertEqual(event["user"], "0:0")
        session = answer["receipt"]["session"]
        self.assertEqual(session["argv"], ["/bin/echo", "hello from the host"])
        self.assertEqual(session["cwd"], str(self.document.parent))
        self.assertEqual(session["exit_code"], 0)
        self.assertEqual(session["state"], "exited")
        self.assertEqual(session["guest_state"], "gone")
        self.assertFalse(session["timed_out"])
        self.assertTrue(answer["receipt"]["broker_accepts_runs"])
        view = self.sessions.show(session["session_uuid"])
        self.assertEqual(view["state"], "exited")
        self.assertIsNone(view["lease_token"])
        # The bridge never touches the origin's own lease or the environment:
        # the shared group still owns the running container.
        leases = LaunchGroup(self.service, "arch").read()["leases"]
        self.assertIn(self.origin_record()["lease_token"], leases)
        self.assertEqual(self.service.inspect("arch")["runtime_state"], "running")
        self.assertEqual(answer["receipt"]["target"]["scope_sha256"],
                         view["scope_sha256"])

    def test_the_same_action_id_can_be_used_again_with_a_new_request_id(self):
        bridge = self.build()
        self.serve(bridge, requests=2)
        first = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        second = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok", second.get("error"))
        self.assertNotEqual(first["receipt"]["request_uuid"],
                            second["receipt"]["request_uuid"])


# ---------------------------------------------------------------------------
# authorization and identity
# ---------------------------------------------------------------------------
class Authorization(BridgeHarness):
    def assert_restart_fence(self, state):
        request_uuid = str(uuid.uuid4())
        ledger = self.ledger()
        ledger.reserve(request_uuid, "run-echo", self.origin, ttl_seconds=600)
        if state == "unresolved":
            ledger.settle(request_uuid, "unresolved", error="guest end was not proven")
        restarted = self.build()
        answer = restarted.respond({"protocol_version": 1, "action_id": "run-echo",
                                     "request_uuid": str(uuid.uuid4()), "token": self.token})
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "run-unresolved")
        self.assertEqual(self.runtime.session_events, [])

    def test_restart_cannot_discard_a_claimed_request_fence(self):
        self.assert_restart_fence("claimed")

    def test_restart_cannot_discard_an_unresolved_request_fence(self):
        self.assert_restart_fence("unresolved")

    def test_oversized_revocations_are_refused_instead_of_truncated(self):
        revoke = private_directory(self.root, "large-revocations") / "revoked.txt"
        revoke.write_text("0" * bridge_module.REVOKE_FILE_BYTES + "\n"
                          + hashlib.sha256(self.token.encode()).hexdigest() + "\n")
        bridge = self.build(document=self.policy_document(revoke_file=str(revoke)))
        answer = self.serve_and_call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "revoke-state")
        self.assertEqual(self.ledger_records(), [])

    def test_unknown_action_and_bad_token_are_refused_before_any_claim(self):
        bridge = self.build()
        self.serve(bridge, requests=5)
        unknown = self.call(bridge, "run-ls", request_uuid=str(uuid.uuid4()))
        self.assertEqual(unknown["status"], "rejected")
        self.assertEqual(unknown["error"]["code"], "unknown-action")
        self.assertIsNone(unknown["receipt"])
        wrong = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()),
                          token=secrets.token_hex(32))
        self.assertEqual(wrong["error"]["code"], "token-mismatch")
        self.assertNotIn(self.token, strict_dumps(wrong))
        shaped = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()),
                           token="nope")
        self.assertEqual(shaped["error"]["code"], "bad-token")
        # A refusal did not burn the request id: the same id still works.
        reused = str(uuid.uuid4())
        self.assertEqual(self.call(bridge, "host-info", request_uuid=reused,
                                   token="nope")["error"]["code"], "bad-token")
        accepted = self.call(bridge, "host-info", request_uuid=reused)
        self.assertEqual(accepted["status"], "ok")
        # Nothing was journaled until the request was really accepted.
        self.assertEqual([item["request_uuid"] for item in self.ledger_records()],
                         [accepted["request_uuid"]])
        self.assertEqual(self.runtime.session_events, [])

    def test_a_guest_supplied_target_or_argv_is_refused(self):
        bridge = self.build()
        self.serve(bridge, requests=5)
        for extra in ({"argv": ["/bin/cat", "/etc/shadow"]},
                      {"cwd": "/etc"}, {"env": {"LANG": "C"}},
                      {"target": "fedora44"}, {"project_uuid": str(uuid.uuid4())}):
            answer = self.raw_text(bridge, self.request_text(**extra))
            self.assertEqual(answer["status"], "rejected", answer)
            self.assertEqual(answer["error"]["code"], "unknown-field", answer)
        self.assertEqual(self.ledger_records(), [])

    def test_revoked_token_and_unreadable_revoke_list_fail_closed(self):
        revoke = private_directory(self.root, "revoke") / "revoked.txt"
        digest = hashlib.sha256(self.token.encode("utf-8")).hexdigest()
        revoke.write_text("deadbeef\n", encoding="utf-8")
        bridge = self.build(document=self.policy_document(revoke_file=str(revoke)))
        self.serve(bridge, requests=2)
        self.assertEqual(self.call(bridge, "host-info",
                                   request_uuid=str(uuid.uuid4()))["status"], "ok")
        revoke.write_text(digest + "\n", encoding="utf-8")
        refused = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(refused["error"]["code"], "token-revoked")
        self.assertNotIn(self.token, strict_dumps(refused))
        directory_as_file = self.build(document=self.policy_document(
            revoke_file=str(revoke.parent)))
        answer = directory_as_file.respond(json.loads(self.request_text()))
        self.assertEqual(answer["error"]["code"], "revoke-state")

    def test_origin_that_exited_or_was_replaced_refuses_every_action(self):
        bridge = self.build()
        self.serve(bridge, requests=5)
        container = self.service.store.load("arch")["runtime"]["container_name"]
        # (a) the origin session ended
        self.sessions.store.update(self.origin, state="exited", lease_token=None,
                                   exit_code=0)
        exited = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(exited["error"]["code"], "origin-not-running")
        self.sessions.store.update(self.origin, state="running",
                                   lease_token=self.lease_token)
        # (b) the container instance stopped while the session record still
        # claims to run: the live instance is what decides.
        running = self.runtime.containers[container]
        self.runtime.containers[container] = replace(
            running, running=False, status="exited")
        stopped = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(stopped["error"]["code"], "origin-not-running")
        self.runtime.containers[container] = running
        # (c) the recorded environment identity moved to a new generation
        record = self.service.store.load("arch")
        record["generation"] = 2
        self.service.store.save(record, None)
        bumped = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(bumped["error"]["code"], "origin-binding-changed")
        record = self.service.store.load("arch")
        record["generation"] = 1
        self.service.store.save(record, None)
        # (d) a same-name container instance was replaced
        name = self.service.store.load("arch")["runtime"]["container_name"]
        info = self.runtime.containers[name]
        self.runtime.containers[name] = replace(info, container_id="cid-replacement")
        replaced = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(replaced["error"]["code"], "origin-binding-changed")
        self.runtime.containers[name] = info
        self.assertEqual(self.ledger_records(), [])

    def test_bind_refuses_to_serve_a_dead_origin(self):
        self.sessions.store.update(self.origin, state="exited", lease_token=None,
                                   exit_code=0)
        bridge = self.build()
        with self.assertRaises(BridgeError) as caught:
            bridge.bind()
        self.assertEqual(caught.exception.code, "origin-not-running")


# ---------------------------------------------------------------------------
# scope drift
# ---------------------------------------------------------------------------
class ScopeDrift(BridgeHarness):
    def test_pinned_scope_change_is_refused(self):
        actions = [{"action_id": "host-info", "type": "host-info"},
                   self.run_action(scope_sha256="0" * 64)]
        bridge = self.build(document=self.policy_document(actions=actions))
        answer = self.serve_and_call(bridge, "run-echo",
                                     request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "scope-changed")
        self.assertEqual(self.runtime.session_events, [])
        self.assertEqual(self.ledger_records(), [])

    def test_project_replacement_under_the_same_alias_is_refused(self):
        bridge = self.build()
        thread = self.serve(bridge, requests=1)
        replacement = None
        # Replace the registration after the policy froze the pinned project UUID.
        self.routes.remove_project(self.project["project_uuid"])
        replacement = self.routes.add_project(
            self.data_root["root_uuid"], "arch", alias="demo",
            default_save_root=str(self.document.parent), intent_handlers={})
        self.assertNotEqual(replacement["project_uuid"], self.project["project_uuid"])
        answer = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "target-changed")
        self.assertEqual(self.runtime.session_events, [])

    def test_target_environment_instance_replacement_is_refused(self):
        stale = self.run_action(environment_binding=dict(
            self.origin_binding(), container_id="cid-other-instance"))
        bridge = self.build(document=self.policy_document(
            actions=[{"action_id": "host-info", "type": "host-info"}, stale]))
        answer = self.serve_and_call(bridge, "run-echo",
                                     request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["error"]["code"], "target-changed")
        self.assertEqual(self.runtime.session_events, [])

    def test_environment_mount_drift_refuses_the_run(self):
        bridge = self.build()
        self.serve(bridge, requests=1)
        record = self.service.store.load("arch")
        record["shared_mounts"][0]["options"] = "ro"
        self.service.store.save(record, None)
        answer = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "host-state")
        self.assertIn("read-only", answer["error"]["message"])
        self.assertEqual(self.runtime.session_events, [])


# ---------------------------------------------------------------------------
# durability: dedupe, restart, TTL, corruption
# ---------------------------------------------------------------------------
class RequestDurability(BridgeHarness):
    def test_replay_restart_expiry_and_corruption_all_fail_closed(self):
        bridge = self.build()
        self.serve(bridge, requests=2)
        request_uuid = str(uuid.uuid4())
        first = self.call(bridge, "host-info", request_uuid=request_uuid)
        self.assertEqual(first["status"], "ok")
        replay = self.call(bridge, "host-info", request_uuid=request_uuid)
        self.assertEqual(replay["status"], "rejected")
        self.assertEqual(replay["error"]["code"], "duplicate-request")
        self.assertNotIn(self.token, strict_dumps(replay))
        # A restart with a brand new bridge object still refuses the old id:
        # the durable ledger, not the process, owns the deduplication.
        restarted = self.build(document=self.policy_document(
            socket=str(self.root / "bridge-2" / "bridge.sock")))
        self.serve(restarted, requests=1)
        self.assertNotEqual(restarted.policy.socket_path, bridge.policy.socket_path)
        after_restart = self.call(restarted, "host-info", request_uuid=request_uuid)
        self.assertEqual(after_restart["error"]["code"], "duplicate-request")
        self.assertEqual(after_restart["status"], "rejected")

    def test_a_settled_request_is_refused_even_when_the_ledger_grew_elsewhere(self):
        bridge = self.build()
        self.serve(bridge, requests=1)
        request_uuid = str(uuid.uuid4())
        self.assertEqual(self.call(bridge, "run-echo",
                                   request_uuid=request_uuid)["status"], "ok")
        fresh = self.build(document=self.policy_document(
            socket=str(self.root / "bridge-2" / "bridge.sock")))
        self.serve(fresh, requests=1)
        self.assertEqual(self.call(fresh, "run-echo",
                                   request_uuid=request_uuid)["error"]["code"],
                         "duplicate-request")
        persisted = self.ledger_records()
        self.assertEqual([item["state"] for item in persisted], ["completed"])
        self.assertEqual(persisted[0]["receipt"]["session"]["exit_code"], 0)

    def test_expired_claim_corrupt_and_lost_ledger_fail_closed(self):
        ledger = self.ledger()
        claim = ledger.reserve(str(uuid.uuid4()), "host-info", self.origin,
                               ttl_seconds=30)
        self.now[0] += 31
        bridge = self.build()
        self.serve(bridge, requests=4)
        expired = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(expired["error"]["code"], "expired-claim")
        self.assertNotIn(self.token, strict_dumps(expired))
        self.now[0] -= 31
        # An unreadable ledger is a refusal, never a fresh start.
        ledger.path.write_text("{not json", encoding="utf-8")
        corrupt = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(corrupt["error"]["code"], "ledger-corrupt")
        ledger.path.unlink()
        settled = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(settled["status"], "ok")
        self.now[0] += 10_000
        late = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(late["status"], "ok")
        self.assertEqual(late["receipt"]["completed_at"], self.now[0])
        self.now[0] -= 10_000
        self.assertEqual(claim["state"], "claimed")

    def test_settling_requires_an_open_claim(self):
        ledger = self.ledger()
        request_uuid = str(uuid.uuid4())
        ledger.reserve(request_uuid, "host-info", self.origin, ttl_seconds=30)
        ledger.settle(request_uuid, "completed", receipt={"status": "ok"})
        with self.assertRaises(BridgeError) as caught:
            ledger.settle(request_uuid, "completed", receipt={"status": "ok"})
        self.assertEqual(caught.exception.code, "duplicate-request")
        with self.assertRaises(BridgeError) as missing:
            ledger.settle(str(uuid.uuid4()), "completed")
        self.assertEqual(missing.exception.code, "ledger-lost")
        # A malformed record never becomes a "successful" ledger entry.
        with self.assertRaises(BridgeError) as bad_action:
            ledger.reserve(str(uuid.uuid4()), "not an id", self.origin,
                           ttl_seconds=30)
        self.assertEqual(bad_action.exception.code, "ledger-write")
        for broken in ({"schema_version": 99},
                       {"schema_version": 1, "request_uuid": "x"}):
            with self.subTest(msg=str(broken)), self.assertRaises(UnifierError):
                validate_request_record(dict(
                    {"schema_version": 1, "request_uuid": str(uuid.uuid4()),
                     "action_id": "host-info", "origin_session_uuid": self.origin,
                     "state": "claimed", "claimed_at": 1.0, "expires_at": 2.0},
                    **broken))


# ---------------------------------------------------------------------------
# protocol boundaries
# ---------------------------------------------------------------------------
class ProtocolBoundaries(BridgeHarness):
    def test_the_request_validator_rejects_what_a_guest_could_forge(self):
        good = {"protocol_version": 1, "action_id": "host-info",
                "request_uuid": str(uuid.uuid4()), "token": self.token}
        self.assertEqual(validate_request(good), good)
        cases = [
            ({"protocol_version": True}, "bad-protocol"),
            ({"protocol_version": 1.0}, "bad-protocol"),
            ({"protocol_version": "1"}, "bad-protocol"),
            ({"protocol_version": 2}, "bad-protocol"),
            ({"action_id": "Host Info"}, "bad-action-id"),
            ({"action_id": "x" * 65}, "bad-action-id"),
            ({"action_id": ""}, "bad-action-id"),
            ({"request_uuid": "urn:uuid:" + str(uuid.uuid4())}, "bad-uuid"),
            ({"request_uuid": self.token}, "bad-uuid"),
            ({"token": "z" * 64}, "bad-token"),
            ({"token": self.token + "0"}, "bad-token"),
            ({"token": True}, "bad-token"),
            ({"cwd": "/etc"}, "unknown-field"),
            ({"argv": ["/bin/sh"]}, "unknown-field"),
        ]
        for change, code in cases:
            with self.subTest(msg=str(change)):
                with self.assertRaises(BridgeError) as caught:
                    validate_request(dict(good, **change))
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(BridgeError) as missing:
            validate_request({"protocol_version": 1})
        self.assertEqual(missing.exception.code, "missing-field")
        with self.assertRaises(BridgeError) as scalar:
            validate_request("/bin/id")
        self.assertEqual(scalar.exception.code, "bad-json")

    def test_non_canonical_ids_types_and_bad_json_are_refused(self):
        bridge = self.build()
        self.serve(bridge, requests=7)
        cases = [
            (self.request_text(request_uuid=str(uuid.uuid4()).upper()), "bad-uuid"),
            (self.request_text(request_uuid="not-a-uuid"), "bad-uuid"),
            (self.request_text(request_uuid=uuid.uuid4().hex), "bad-uuid"),
            ('{"protocol_version": 1, "action_id": "host-info", "request_uuid": 7, '
             f'"token": "{self.token}"}}', "bad-uuid"),
            ('{"protocol_version": true, "action_id": "host-info", "request_uuid": '
             f'"{uuid.uuid4()}", "token": "{self.token}"}}', "bad-protocol"),
            ('{"protocol_version": 1, "protocol_version": 1, "action_id": '
             f'"host-info", "request_uuid": "{uuid.uuid4()}", "token": "{self.token}"}}',
             "bad-json"),
            ('{"protocol_version": 1, "action_id": "host-info", "request_uuid": '
             f'"{uuid.uuid4()}", "token": "{self.token}" ', "bad-json"),
        ]
        for text, code in cases:
            answer = self.raw_text(bridge, text)
            self.assertEqual(answer["status"], "rejected", text)
            self.assertEqual(answer["error"]["code"], code, f"{text} -> {answer}")
        self.assertEqual(self.ledger_records(), [])

    def test_missing_fields_and_nan_are_refused_without_echoing_bytes(self):
        bridge = self.build()
        self.serve(bridge, requests=3)
        missing = json.dumps({"protocol_version": 1, "action_id": "host-info",
                              "request_uuid": str(uuid.uuid4())})
        answer = self.raw_text(bridge, missing)
        self.assertEqual(answer["error"]["code"], "missing-field")
        # A non-finite number is refused as malformed, never as a host value.
        nan = ('{"protocol_version": 1, "action_id": "host-info", "request_uuid": '
               f'"{uuid.uuid4()}", "token": "{self.token}", "latency": NaN}}')
        answer = self.raw_text(bridge, nan)
        self.assertEqual(answer["error"]["code"], "bad-json")
        body = self.raw_bytes(bridge, struct.pack(">I", 2) + b"no")
        answer = json.loads(body.decode("utf-8"))
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "bad-json")
        self.assertEqual(self.ledger_records(), [])

    def test_frame_size_is_bounded_by_the_policy_and_the_stream_stays_usable(self):
        bridge = self.build(document=self.policy_document(frame_limit=4096))
        self.serve(bridge, requests=3)
        oversized = struct.pack(">I", 8192) + b"x" * 8192
        answer = json.loads(self.raw_bytes(bridge, oversized).decode("utf-8"))
        self.assertEqual(answer["error"]["code"], "frame-too-large")
        self.assertNotIn(self.token, strict_dumps(answer))
        good = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(good["status"], "ok")
        empty = self.raw_bytes(bridge, struct.pack(">I", 0))
        answer = json.loads(empty.decode("utf-8"))
        self.assertEqual(answer["error"]["code"], "frame-too-large")

    def test_a_stalled_frame_times_out_the_handshake(self):
        bridge = self.build(handshake_timeout=0.4)
        self.serve(bridge, requests=1)
        body = self.raw_bytes(bridge, struct.pack(">I", 4096) + b"partial",
                              timeout=5.0)
        answer = json.loads(body.decode("utf-8"))
        self.assertEqual(answer["error"]["code"], "frame-timeout")

    def test_client_refuses_bad_socket_aliases_and_oversized_frames(self):
        bridge = self.build()
        bridge.bind()
        path = bridge.policy.socket_path
        alias = path.parent / "alias.sock"
        os.symlink(path, alias)
        with self.assertRaises(BridgeError) as caught:
            bridge_client.request(str(alias), "host-info", token=self.token)
        self.assertEqual(caught.exception.code, "bad-socket")
        plain = private_directory(self.root, "plain") / "not-a-socket"
        plain.write_text("x", encoding="utf-8")
        with self.assertRaises(BridgeError) as caught:
            bridge_client.request(str(plain), "host-info", token=self.token)
        self.assertEqual(caught.exception.code, "bad-socket")
        self.serve(bridge, requests=1)
        with self.assertRaises(BridgeError) as too_small:
            bridge_client.request(str(path), "host-info", token=self.token,
                                  frame_limit=bridge_module.MIN_FRAME_LIMIT,
                                  timeout=10.0)
        self.assertEqual(too_small.exception.code, "frame-too-large")


# ---------------------------------------------------------------------------
# timeout and cancellation
# ---------------------------------------------------------------------------
class TimeoutAndCancellation(BridgeHarness):
    def test_timeout_cancels_the_exact_guest_and_is_reported_as_timeout(self):
        self.runtime.hold = True
        actions = [{"action_id": "host-info", "type": "host-info"},
                   self.run_action(timeout_seconds=1, request_ttl_seconds=600)]
        bridge = self.build(document=self.policy_document(actions=actions),
                            run_grace_seconds=1.0)
        self.serve(bridge, requests=1)
        with self.mock_patch():
            answer = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "timeout", answer.get("error"))
        session = answer["receipt"]["session"]
        self.assertTrue(session["timed_out"])
        self.assertIn(session["state"], {"exited", "interrupted"})
        self.assertEqual(session["guest_state"], "gone")
        steps = session["cancel"]["steps"]
        self.assertEqual(steps[0]["signal"], 15)
        self.assertTrue(session["cancel"]["confirmed"])
        self.assertTrue(self.runtime.signal_calls)
        guest = self.runtime.signal_calls[-1][0]
        self.assertEqual(guest.pid, self.runtime.last_execution.guest.pid)
        self.assertFalse(self.runtime.last_execution.proxy_aborted)
        view = self.sessions.show(session["session_uuid"])
        self.assertIn(view["state"], {"exited", "interrupted"})
        self.assertEqual(view["guest_state"], "gone")
        persisted = self.ledger_records()
        self.assertEqual(persisted[0]["state"], "completed")
        self.assertEqual(persisted[0]["receipt"]["status"], "timeout")

    def test_an_unproven_cancellation_is_unresolved_not_a_success(self):
        self.runtime.hold = True
        self.runtime.signal_status = "unknown"
        self.runtime.probe_status = "alive"
        self.runtime.cleanup_status = "unknown"
        actions = [{"action_id": "host-info", "type": "host-info"},
                   self.run_action(timeout_seconds=1, request_ttl_seconds=600)]
        bridge = self.build(document=self.policy_document(actions=actions),
                            run_grace_seconds=0.2)
        self.serve(bridge, requests=1)
        with self.mock_patch():
            request_uuid = str(uuid.uuid4())
            answer = self.call(bridge, "run-echo", request_uuid=request_uuid)
        self.assertEqual(answer["status"], "unresolved")
        session = answer["receipt"]["session"]
        self.assertTrue(session["timed_out"])
        self.assertFalse(session["cancel"]["confirmed"])
        self.assertIn("proven", answer["receipt"]["error"])
        # Let the still-held guest end so the worker thread can settle.
        self.runtime.last_execution.finished = True
        persisted = self.ledger_records()
        self.assertEqual(persisted[0]["state"], "unresolved")
        replay = bridge.respond({"protocol_version": 1, "action_id": "run-echo",
                                 "request_uuid": request_uuid, "token": self.token})
        self.assertEqual(replay["error"]["code"], "duplicate-request")

    def mock_patch(self):
        # Keep the host-side escalation loops short without changing the code.
        import unittest.mock as mock

        return mock.patch.object(
            sys.modules["unifier.project_sessions"], "SIGNAL_GRACE_SECONDS", 0.2)

    def test_a_run_without_a_timeout_is_not_allowed_by_the_policy(self):
        action = self.run_action()
        del action["timeout_seconds"]
        document = self.policy_document(
            actions=[{"action_id": "host-info", "type": "host-info"}, action])
        with self.assertRaises(BridgeError) as caught:
            parse_policy(document)
        self.assertIn("timeout_seconds", caught.exception.message)


# ---------------------------------------------------------------------------
# policy shape, privacy and the CLI
# ---------------------------------------------------------------------------
class PolicyAndClient(BridgeHarness):
    def test_policy_shape_and_privileges_fail_closed(self):
        base = self.policy_document()
        document_cases = [
            (dict(base, shell=True), "unknown fields"),
            (dict(base, schema_version=2), "schema_version"),
            (dict(base, token=secrets.token_hex(16)), "32 random bytes"),
            (dict(base, token="A" * 64), "lowercase hex"),
            (dict(base, origin={"session_uuid": self.origin.upper()}), "UUID"),
            (dict(base, actions=[]), "non-empty"),
            (dict(base, actions=[{"action_id": "host-info", "type": "host-info"},
                                 {"action_id": "host-info", "type": "host-info"}]),
             "duplicate action_id"),
            (dict(base, actions=[dict(self.run_action(), cwd="relative/inside")]),
             "absolute path"),
            (dict(base, actions=[{"action_id": "host-info", "type": "host-info",
                                  "argv": ["/bin/true"]}]), "takes no argv"),
            (dict(base, actions=[dict(self.run_action(), timeout_seconds=59,
                                      request_ttl_seconds=60)]),
             "request_ttl_seconds must exceed"),
            (dict(base, frame_limit=1024 * 1024), "frame_limit"),
        ]
        for document, expected in document_cases:
            with self.subTest(expected=expected):
                with self.assertRaises(BridgeError) as caught:
                    parse_policy(document)
                self.assertIn(expected, caught.exception.message)
        # Permission and alias checks happen on the file, not the document.
        loose = write_policy(self.root, base, mode=0o640,
                             directory=private_directory(self.root, "loose"))
        with self.assertRaises(BridgeError) as caught:
            load_policy(loose)
        self.assertIn("0600", caught.exception.message)
        shared = private_directory(self.root, "shared")
        os.chmod(str(shared), 0o755)
        world = write_policy(self.root, base, directory=shared)
        with self.assertRaises(BridgeError) as caught:
            load_policy(world)
        self.assertIn("group or other", caught.exception.message)
        os.chmod(str(shared), 0o700)
        aliased = shared / "alias.json"
        os.symlink(world, aliased)
        with self.assertRaises(BridgeError) as caught:
            load_policy(aliased)
        self.assertIn("symbolic link", caught.exception.message)

    def test_socket_is_private_and_existing_paths_are_not_overwritten(self):
        document = self.policy_document()
        bridge = self.build(document=document)
        path = bridge.bind()
        self.assertEqual(stat.S_IMODE(os.lstat(str(path)).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(str(path.parent)).st_mode), 0o700)
        bridge.close()
        self.assertFalse(path.exists())
        second = self.build(document=document)
        second.bind()
        self.serve(second, requests=1)
        third = self.build(document=document)
        with self.assertRaises(BridgeError) as caught:
            third.bind()
        self.assertEqual(caught.exception.code, "socket-exists")
        alias = path.parent.parent / "alias-dir" / "bridge.sock"
        (alias.parent).mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(str(alias.parent), 0o700)
        os.symlink(path, alias)
        # A symlinked socket path is refused even before any bind: the broker
        # never follows an alias to somebody else's inode.
        with self.assertRaises(BridgeError) as caught:
            self.build(path=write_policy(self.root,
                                        dict(document, socket=str(alias))))
        self.assertIn("symbolic link", caught.exception.message)
        exposed = alias.parent
        os.chmod(str(exposed), 0o755)
        fifth = self.build(path=write_policy(self.root, dict(
            document, socket=str(exposed / "other.sock"))))
        with self.assertRaises(BridgeError) as caught:
            fifth.bind()
        self.assertEqual(caught.exception.code, "socket-dir-insecure")
        os.chmod(str(exposed), 0o700)

    def test_token_never_reaches_the_receipt_ledger_or_client_output(self):
        bridge = self.build()
        self.serve(bridge, requests=2)
        host = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        run = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(host["status"], "ok")
        self.assertEqual(run["status"], "ok", run.get("error"))
        ledger_text = self.ledger_file().read_text(encoding="utf-8")
        self.assertNotIn(self.token, ledger_text)
        self.assertNotIn(self.token, strict_dumps(host))
        self.assertNotIn(self.token, strict_dumps(run))
        policy_text = Path(self.policy_file()).read_text(encoding="utf-8")
        self.assertIn(self.token, policy_text)
        exported = bridge_module.token_environment(bridge.policy)
        self.assertEqual(exported[bridge.policy.token_env], self.token)
        self.assertNotIn(self.token, strict_dumps(
            self.ledger_records()))

    def test_client_cli_exit_codes_and_stdout_never_leak_the_token(self):
        bridge = self.build()
        self.serve(bridge, requests=2)
        environ = {bridge.policy.token_env: self.token}
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock_env(environ), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            good = bridge_client.main([
                "--socket", str(bridge.policy.socket_path), "--action", "host-info",
                "--request-uuid", str(uuid.uuid4())])
        answer = json.loads(stdout.getvalue())
        self.assertEqual(good, bridge_client.EXIT_OK)
        self.assertEqual(answer["status"], "ok")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock_env(environ), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            refused = bridge_client.main([
                "--socket", str(bridge.policy.socket_path), "--action", "no-such",
                "--request-uuid", str(uuid.uuid4())])
        self.assertEqual(refused, bridge_client.EXIT_REFUSED)
        self.assertEqual(json.loads(stdout.getvalue())["error"]["code"],
                         "unknown-action")
        stdout, stderr = io.StringIO(), io.StringIO()
        with mock_env({}), contextlib.redirect_stdout(stdout), \
                contextlib.redirect_stderr(stderr):
            missing = bridge_client.main([
                "--socket", str(bridge.policy.socket_path), "--action", "host-info"])
        self.assertEqual(missing, bridge_client.EXIT_FAULT)
        self.assertNotIn(self.token, stderr.getvalue())
        self.assertIn(bridge.policy.token_env, stderr.getvalue())


class mock_env:
    """Patch exactly one environment entry for the duration of a block."""

    def __init__(self, values: dict):
        self.values = values

    def __enter__(self):
        self.saved = {}
        for key in list(self.values) + [bridge_module.DEFAULT_TOKEN_ENV]:
            self.saved[key] = os.environ.get(key)
            os.environ.pop(key, None)
        os.environ.update(self.values)
        return self

    def __exit__(self, *_exc_info):
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# the target is the host's choice, not the origin's
# ---------------------------------------------------------------------------
class CrossObjectRequests(BridgeHarness):
    """A request from one object may run in another accepted object.

    These are the two directions that matter: a Debian origin asking for an Arch
    project, and an Arch origin asking for a Debian project.  The host-issued
    policy fixes the target; the bridge only re-verifies that the pin still
    resolves to that project, that environment instance and that scope.
    """

    def setUp(self):
        super().setUp()
        self.deb_project, self.deb_root_dir = self.add_object("debian13", "deb")

    def test_a_debian_origin_runs_the_action_in_the_arch_object(self):
        debian_origin = self.start_origin(project=self.deb_project,
                                          environment_id="debian13",
                                          cwd=self.deb_root_dir)
        debian_binding = self.binding_of("debian13")
        arch_binding = self.binding_of("arch")
        self.assertNotEqual(debian_binding, arch_binding)
        actions = [{"action_id": "host-info", "type": "host-info"},
                   self.run_action(project=self.project, environment_id="arch")]
        bridge = self.build(document=self.policy_document(
            actions=actions, origin=debian_origin,
            origin_binding=debian_binding))
        answer = self.serve_and_call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "ok", answer.get("error"))
        event = self.runtime.session_events[-1]
        # The exec happened in the *other* object's container, and the answer
        # says which object asked and which object ran.
        self.assertEqual(event["name"], self.container_of("arch"))
        self.assertNotEqual(event["name"], self.container_of("debian13"))
        receipt = answer["receipt"]
        self.assertEqual(receipt["origin"]["environment_binding"], debian_binding)
        self.assertEqual(receipt["target"]["environment_binding"], arch_binding)
        self.assertEqual(receipt["session"]["environment_binding"], arch_binding)
        self.assertEqual(receipt["session"]["exit_code"], 0)
        self.assertEqual(receipt["session"]["project_uuid"],
                         self.project["project_uuid"])
        # The requesting object keeps its own lease and its own running instance:
        # a cross-object run never reaches back into the origin's environment.
        self.assertEqual(self.service.inspect("debian13")["runtime_state"], "running")
        leases = LaunchGroup(self.service, "debian13").read()["leases"]
        self.assertIn(self.sessions.store.read(debian_origin)["lease_token"], leases)

    def test_an_arch_origin_runs_the_action_in_another_debian_object(self):
        actions = [{"action_id": "host-info", "type": "host-info"},
                   self.run_action(project=self.deb_project,
                                   environment_id="debian13")]
        bridge = self.build(document=self.policy_document(actions=actions))
        answer = self.serve_and_call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "ok", answer.get("error"))
        event = self.runtime.session_events[-1]
        self.assertEqual(event["name"], self.container_of("debian13"))
        self.assertNotEqual(event["name"], self.container_of("arch"))
        receipt = answer["receipt"]
        self.assertEqual(receipt["origin"]["environment_binding"],
                         self.origin_binding())
        self.assertEqual(receipt["target"]["environment_binding"],
                         self.binding_of("debian13"))
        self.assertEqual(event["workdir"], self.deb_root_dir)
        self.assertEqual(self.service.inspect("arch")["runtime_state"], "running")

    def test_a_cross_target_that_moved_is_still_refused(self):
        actions = [{"action_id": "host-info", "type": "host-info"},
                   self.run_action(project=self.deb_project, environment_id="debian13",
                                   environment_binding=dict(
                                       self.binding_of("debian13"),
                                       container_id="cid-not-there"))]
        bridge = self.build(document=self.policy_document(actions=actions))
        answer = self.serve_and_call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "rejected", answer)
        self.assertEqual(answer["error"]["code"], "target-changed")
        self.assertEqual(self.runtime.session_events, [])


# ---------------------------------------------------------------------------
# the grant is alive only while the origin -- and its deadline -- are
# ---------------------------------------------------------------------------
class OriginLiveness(BridgeHarness):
    def test_a_live_worker_with_a_dead_guest_or_lease_stops_serving(self):
        bridge = self.build()
        self.serve(bridge, requests=6)
        live = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(live["status"], "ok", live.get("error"))
        # (a) the host worker is still alive, but the guest execution is gone:
        #     a proxy that outlives its guest proves nothing about the grant.
        self.runtime.alive_guests.discard(self.origin_guest.pid)
        gone = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(gone["error"]["code"], "origin-guest-lost")
        # (b) and a probe that cannot answer is refused just the same.
        self.runtime.probe_status = "unknown"
        unknown = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(unknown["error"]["code"], "origin-guest-lost")
        self.runtime.probe_status = "gone"
        self.runtime.alive_guests.add(self.origin_guest.pid)
        # (c) the record still claims a lease the shared group no longer holds.
        self.sessions.store.update(self.origin, lease_token="no-longer-in-the-group")
        lost = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(lost["error"]["code"], "origin-lease-lost")
        self.sessions.store.update(self.origin, lease_token=self.lease_token)
        recovered = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(recovered["status"], "ok", recovered.get("error"))
        # Only the two requests that were served ever claimed anything; the
        # three refusals never wrote to the ledger.
        self.assertEqual([item["request_uuid"] for item in self.ledger_records()],
                         [live["request_uuid"], recovered["request_uuid"]])


class GrantExpiry(BridgeHarness):
    def test_an_expired_grant_is_refused_before_anything_is_claimed(self):
        document = self.policy_document(expires_at=self.now[0] + 120)
        bridge = self.build(document=document)
        self.serve(bridge, requests=3)
        first = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(first["status"], "ok", first.get("error"))
        self.assertEqual(first["receipt"]["grant_expires_at"], self.now[0] + 120)
        self.now[0] += 121
        refused = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(refused["status"], "rejected")
        self.assertEqual(refused["error"]["code"], "policy-expired")
        self.assertIsNone(refused["receipt"])
        run = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(run["error"]["code"], "policy-expired")
        self.assertEqual(self.runtime.session_events, [])
        self.assertEqual([item["request_uuid"] for item in self.ledger_records()],
                         [first["request_uuid"]])
        # The claim TTL is not the deadline: that claim was still open in time
        # when the grant died, and the grant is what stopped the bridge.
        self.assertGreater(first["receipt"]["claim_expires_at"], self.now[0])
        self.assertLessEqual(first["receipt"]["grant_expires_at"], self.now[0])

    def test_a_restarted_process_refuses_the_same_expired_grant(self):
        expired = self.now[0] + 60
        document = self.policy_document(expires_at=expired)
        bridge = self.build(document=document)
        self.serve(bridge, requests=1)
        self.assertEqual(self.call(bridge, "host-info",
                                   request_uuid=str(uuid.uuid4()))["status"], "ok")
        self.now[0] = expired + 1
        # A brand new broker re-reads the frozen policy and will not even bind.
        restarted = self.build(document=self.policy_document(
            expires_at=expired,
            socket=str(self.root / "bridge-2" / "bridge.sock")))
        with self.assertRaises(BridgeError) as caught:
            restarted.bind()
        self.assertEqual(caught.exception.code, "policy-expired")
        answer = restarted.respond({"protocol_version": 1, "action_id": "host-info",
                                    "request_uuid": str(uuid.uuid4()),
                                    "token": self.token})
        self.assertEqual(answer["status"], "rejected")
        self.assertEqual(answer["error"]["code"], "policy-expired")
        self.assertEqual(self.runtime.session_events, [])

    def test_the_policy_needs_an_absolute_finite_deadline(self):
        base = self.policy_document()
        for label, value in (("missing", ...), ("bool", True), ("string", "tomorrow"),
                             ("zero", 0), ("negative", -5), ("nan", float("nan")),
                             ("inf", float("inf")), ("far future", 99_999_999_999)):
            document = dict(base)
            if value is ...:
                document.pop("expires_at")
            else:
                document["expires_at"] = value
            with self.subTest(msg=label):
                with self.assertRaises(BridgeError) as caught:
                    parse_policy(document)
                self.assertIn("expires_at", caught.exception.message)
        for value in (self.now[0] + 60, int(self.now[0] + 60)):
            with self.subTest(msg=str(value)):
                self.assertEqual(parse_policy(dict(base, expires_at=value)).expires_at,
                                 float(value))
        origin_only = dict(base, origin={"session_uuid": self.origin})
        with self.assertRaises(BridgeError) as unpinned:
            parse_policy(origin_only)
        self.assertIn("environment_binding", unpinned.exception.message)


# ---------------------------------------------------------------------------
# the socket is addressed through a pinned directory FD, not its full path
# ---------------------------------------------------------------------------
class LongSocketPath(BridgeHarness):
    def test_bind_and_cleanup_stay_on_the_parent_fd_after_its_path_is_replaced(self):
        from unittest.mock import patch
        bridge = self.build()
        path = bridge.policy.socket_path
        moved = path.parent.with_name("original-socket-directory")
        decoy = private_directory(self.root, "decoy-directory")
        sentinel = decoy / path.name
        sentinel.write_text("preserve this file")
        sentinel.chmod(0o640)
        native = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)

        class MovingSocket:
            def bind(self, address):
                native.bind(address)
                path.parent.rename(moved)
                path.parent.symlink_to(decoy, target_is_directory=True)

            def __getattr__(self, name):
                return getattr(native, name)

        with patch.object(bridge_module.socket, "socket", return_value=MovingSocket()):
            bridge.bind()
        self.assertEqual(stat.S_IMODE((moved / path.name).stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o640)
        bridge.close()
        self.assertFalse((moved / path.name).exists())
        self.assertEqual(sentinel.read_text(), "preserve this file")
        self.assertEqual(stat.S_IMODE(sentinel.stat().st_mode), 0o640)

    def test_directory_anchor_rejects_symlink_ancestors(self):
        real = private_directory(self.root, "real-ancestor")
        child = real / "private-child"
        child.mkdir(mode=0o700)
        alias = self.root / "alias-ancestor"
        alias.symlink_to(real, target_is_directory=True)
        with self.assertRaises(BridgeError):
            bridge_module.open_directory_fd(alias / child.name, "socket directory", "bad-socket")

    def test_a_deep_non_ascii_socket_path_still_hands_shakes(self):
        directory = deep_directory(self.root)
        path = directory / "桥-agent.sock"
        document = self.policy_document(socket=str(path))
        self.assertGreater(len(str(path).encode("utf-8")), 180)
        self.assertGreater(len(str(path).encode("utf-8")),
                           bridge_module.UNIX_PATH_LIMIT)
        bridge = self.build(document=document)
        self.serve(bridge, requests=2)
        answer = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "ok", answer.get("error"))
        # The inode really lives in the real directory, with the real ownership.
        observed = os.lstat(str(path))
        self.assertTrue(stat.S_ISSOCK(observed.st_mode))
        self.assertEqual(stat.S_IMODE(observed.st_mode), 0o600)
        self.assertEqual(os.stat(str(path.parent)).st_ino,
                         os.lstat(str(path.parent)).st_ino)
        self.assertEqual(stat.S_IMODE(os.stat(str(path.parent)).st_mode), 0o700)
        # A raw request over the same anchored address works too.
        reply = json.loads(self.raw_bytes(bridge, bridge_module.request_frame(
            {"protocol_version": 1, "action_id": "host-info",
             "request_uuid": str(uuid.uuid4()), "token": self.token})).decode("utf-8"))
        self.assertEqual(reply["status"], "ok")
        # An alias in that directory is still refused, long path or not.
        alias = directory / "alias.sock"
        os.symlink(path, alias)
        with self.assertRaises(BridgeError) as caught:
            bridge_client.request(str(alias), "host-info", token=self.token)
        self.assertEqual(caught.exception.code, "bad-socket")

    def test_the_anchored_address_refuses_anything_but_one_entry_name(self):
        directory = private_directory(self.root, "anchor")
        fd = bridge_module.open_directory_fd(directory, "socket directory", "bad-socket")
        try:
            for name in ("", "sub/dir.sock", "a" * 256, "bad\nname"):
                with self.subTest(msg=name[:12]):
                    with self.assertRaises(BridgeError) as caught:
                        bridge_module.socket_address_via_fd(fd, name)
                    self.assertEqual(caught.exception.code, "bad-socket")
            address = bridge_module.socket_address_via_fd(fd, "bridge.sock")
            self.assertLessEqual(len(address.encode("utf-8")),
                                 bridge_module.UNIX_PATH_LIMIT)
            self.assertTrue(address.startswith(bridge_module.PROC_SELF_FD_ROOT))
        finally:
            os.close(fd)
        plain = directory / "plain.sock"
        plain.write_text("not a socket", encoding="utf-8")
        with self.assertRaises(BridgeError) as regular:
            bridge_module.open_directory_fd(plain, "socket", "bad-socket")
        self.assertIn("not a directory", regular.exception.message)
        with self.assertRaises(BridgeError) as absent:
            bridge_module.open_directory_fd(directory / "gone", "socket", "bad-socket")
        self.assertIn("unavailable", absent.exception.message)


# ---------------------------------------------------------------------------
# credential privacy on the way out
# ---------------------------------------------------------------------------
class CredentialPrivacy(BridgeHarness):
    def test_a_token_used_as_a_field_name_never_comes_back(self):
        bridge = self.build()
        self.serve(bridge, requests=4)
        leaked = json.dumps({"protocol_version": 1, "action_id": "host-info",
                             "request_uuid": str(uuid.uuid4()), "token": self.token,
                             self.token: "the guest echoed its own grant"})
        answer = self.raw_text(bridge, leaked)
        self.assertEqual(answer["error"]["code"], "unknown-field")
        text = strict_dumps(answer)
        self.assertNotIn(self.token, text)
        self.assertIn(bridge_module.REDACTED_MARKER, text)
        # A value that merely contains the token is scrubbed too.
        embedded = self.raw_text(bridge, json.dumps(
            {"protocol_version": 1, "action_id": "host-info",
             "request_uuid": str(uuid.uuid4()), "token": self.token,
             "note": "grant " + self.token}))
        self.assertEqual(embedded["error"]["code"], "unknown-field")
        self.assertNotIn(self.token, strict_dumps(embedded))
        # The successful answer and the journal stay clean as well.
        good = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(good["status"], "ok", good.get("error"))
        self.assertNotIn(self.token, strict_dumps(good))
        self.assertNotIn(self.token, self.ledger_file().read_text(encoding="utf-8"))

    def test_redaction_only_touches_this_policies_token(self):
        token = "c" * 64
        document = {"session": {"session_uuid": str(uuid.uuid4()),
                                "argv": ["/bin/echo", token, "identity"],
                                "cwd": "/home/omg/文档"},
                    token: "a field named after the credential"}
        scrubbed = bridge_module.redact_token(document, token)
        self.assertEqual(list(scrubbed), ["session", bridge_module.REDACTED_MARKER])
        self.assertEqual(scrubbed["session"]["argv"],
                         ["/bin/echo", bridge_module.REDACTED_MARKER, "identity"])
        self.assertEqual(scrubbed["session"]["cwd"], "/home/omg/文档")
        self.assertEqual(bridge_module.redact_token(document, "d" * 64), document)


class ScopeMoveBeforeExecute(BridgeHarness):
    def test_a_scope_move_between_plan_and_execute_refuses_the_run(self):
        bridge = self.build()
        self.serve(bridge, requests=1)
        original = self.sessions.prepare
        state = {"calls": 0}

        def prepare(*args, **kwargs):
            state["calls"] += 1
            if state["calls"] == 2:
                # The host already planned this run; the target policy moves
                # before the session is journaled.
                record = self.service.store.load("arch")
                record["shared_mounts"][0]["options"] = "nosuid"
                self.service.store.save(record, None)
            return original(*args, **kwargs)

        self.sessions.prepare = prepare
        answer = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(answer["status"], "rejected", answer)
        self.assertIn("scope", answer["receipt"]["error"])
        self.assertEqual(state["calls"], 2)
        # Nothing executed, and the journaled session never left "planned".
        self.assertEqual(self.runtime.session_events, [])
        session = answer["receipt"]["session"]
        self.assertEqual(self.sessions.show(session["session_uuid"])["state"],
                         "planned")
        self.assertIsNone(self.sessions.show(session["session_uuid"])["lease_token"])
        self.assertEqual(answer["receipt"]["session"]["exit_code"], None)


class DegradedAfterUnresolvedRun(BridgeHarness):
    """A broker that still owns a live execution is not a normal service."""

    def unresolved_run(self, bridge: AgentBridge) -> dict:
        self.runtime.hold = True
        self.runtime.signal_status = "unknown"
        self.runtime.probe_status = "alive"
        self.runtime.cleanup_status = "unknown"
        # A held guest keeps its host proxy thread spinning; let it end once the
        # test has looked at the degraded state, or it steals the whole process.
        self.addCleanup(self.settle_guest, bridge)
        with self.mock_patch():
            return self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))

    def settle_guest(self, bridge):
        # A held guest would otherwise keep its host proxy thread spinning after
        # the test, stealing the process from every later test.
        self.runtime.hold = False
        if self.runtime.last_execution is not None:
            self.runtime.last_execution.finished = True
        deadline = time.monotonic() + 5.0
        while (time.monotonic() < deadline
               and any(item["thread_alive"] for item in bridge.unresolved_runs())):
            time.sleep(0.05)

    def mock_patch(self):
        import unittest.mock as mock

        return mock.patch.object(
            sys.modules["unifier.project_sessions"], "SIGNAL_GRACE_SECONDS", 0.2)

    def policy_actions(self):
        return [{"action_id": "host-info", "type": "host-info"},
                self.run_action(timeout_seconds=1, request_ttl_seconds=600)]

    def test_an_unresolved_run_refuses_the_next_project_run_on_this_broker(self):
        bridge = self.build(document=self.policy_document(actions=self.policy_actions()),
                            run_grace_seconds=0.2)
        self.serve(bridge, requests=3)
        first = self.unresolved_run(bridge)
        self.assertEqual(first["status"], "unresolved", first)
        self.assertFalse(first["receipt"]["broker_accepts_runs"])
        self.assertTrue(bridge.unresolved_runs())
        started = len(self.runtime.session_events)
        refused = self.call(bridge, "run-echo", request_uuid=str(uuid.uuid4()))
        self.assertEqual(refused["status"], "rejected")
        self.assertEqual(refused["error"]["code"], "run-unresolved")
        # No second execution started, and the refusal did not burn a claim.
        self.assertEqual(len(self.runtime.session_events), started)
        self.assertEqual([item["request_uuid"] for item in self.ledger_records()],
                         [first["request_uuid"]])
        # The read-only action still answers: what the broker refuses to do is
        # start another run, not serve at all.
        still = self.call(bridge, "host-info", request_uuid=str(uuid.uuid4()))
        self.assertEqual(still["status"], "ok", still.get("error"))
        # The unresolved claim stays in the journal, not hidden as completed-ok.
        self.assertEqual(self.ledger_records()[0]["state"], "unresolved")

    def test_stopping_the_broker_targets_the_exact_guest_and_says_what_it_proved(self):
        bridge = self.build(document=self.policy_document(actions=self.policy_actions()),
                            run_grace_seconds=0.2)
        self.serve(bridge, requests=1)
        first = self.unresolved_run(bridge)
        session_uuid = first["receipt"]["session"]["session_uuid"]
        self.assertTrue(bridge.unresolved_runs()[0]["thread_alive"])
        # The guest is only willing to end when its exact identity is signalled.
        self.runtime.signal_status = "signalled"
        self.runtime.probe_status = "gone"
        self.runtime.cleanup_status = "gone"
        with self.mock_patch():
            bridge.stop()
        evidence = bridge.unresolved_runs()[0]
        self.assertEqual(evidence["session_uuid"], session_uuid)
        self.assertEqual(evidence["stop_cancel"]["action"], "cancelled")
        self.assertFalse(evidence["thread_alive"])
        view = self.sessions.show(session_uuid)
        # The worker and direct cancellation both publish terminal evidence.
        # Either writer may settle last; the guest proof and released lease
        # are the contract, rather than which terminal label won the race.
        self.assertIn(view["state"], {"interrupted", "exited"})
        self.assertEqual(view["guest_state"], "gone")
        self.assertIsNone(view["lease_token"])
        if view["state"] == "exited":
            self.assertNotEqual(view["exit_code"], 0)
        # The wrap-up only ever targets this run's own guest identity.
        self.assertEqual(self.runtime.signal_calls[-1][0].pid,
                         self.runtime.last_execution.guest.pid)

    def test_an_unproven_wrap_up_is_recorded_as_unproven(self):
        bridge = self.build(document=self.policy_document(actions=self.policy_actions()),
                            run_grace_seconds=0.2)
        self.serve(bridge, requests=1)
        first = self.unresolved_run(bridge)
        session_uuid = first["receipt"]["session"]["session_uuid"]
        with self.mock_patch():
            bridge.stop()
        evidence = bridge.unresolved_runs()[0]
        self.assertEqual(evidence["stop_cancel"]["action"], "unresolved")
        # Stop does not claim a cleanup it could not prove: the session record
        # still keeps its lease and its unresolved state.
        view = self.sessions.show(session_uuid)
        self.assertEqual(view["state"], "unresolved")
        self.assertIsNotNone(view["lease_token"])
        self.assertTrue(view["lease_live"])


if __name__ == "__main__":
    unittest.main()
