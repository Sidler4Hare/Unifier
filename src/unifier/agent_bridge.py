"""Host-issued, capability-scoped request bridge for one guest origin session.

A guest (a project session that the host started inside a managed environment)
may ask the host to do exactly one of a small set of pre-registered things.
This module is the host side of that conversation:

* the **host** starts the broker and hands the guest one random token through
  the environment; the token is the only credential the guest owns;
* every rule -- socket path, origin project session UUID, the action scope
  (literal argv, cwd, project UUID, environment binding, run timeout) -- lives
  in a private host-issued policy file that is read **once** at start and then
  frozen into immutable dataclasses;
* the guest may only name ``action_id`` plus its own ``request_uuid`` and token.
  It cannot name a target, a path, an argv entry, a cwd or an environment
  variable, so this is not a host shell and never the podman socket.

Authority is re-derived from host state on **every** request: the pinned origin
project session must still be running with its guest execution and its lease
live, its journaled environment binding must still equal the live environment
identity (UUID, generation, container id) and the running container instance
must be that same one.  A guest-supplied string is never treated as identity.
The grant itself also expires: ``expires_at`` is an absolute Unix deadline in
the policy, checked on every request before anything is reserved or started.
(The per-request ``request_ttl_seconds`` is something else entirely -- it is how
long an *open ledger claim* may stay open, not how long the grant is valid.)

The **target** of an action is the host's own decision, not the origin's.  An
action is pinned to one project UUID plus one environment binding chosen by the
host when it issued the policy, and that target may be a different object than
the requesting guest -- a Debian origin asking for an Arch project run is the
whole point of the bridge.  What is re-verified on every request is that the
pinned target still resolves to exactly that project, that binding and that
scope, not that the target is the origin.

Actions are reusable but ``request_uuid`` is deduplicated durably: the request
is reserved atomically in a ledger *before* anything starts, so a restart still
refuses the same id.  Claims expire (TTL); an expired claim, an unreadable
ledger or a revoked token all fail closed.

Two action types ship in this first cut:

``host-info``
    read-only host evidence from builtins over fixed literal paths
    (``/etc/os-release``, ``/proc/self/*``, ``os.uname``).  No guest-chosen
    path, no argv, no command.

``project-run``
    run the policy's literal argv through the existing
    :class:`~unifier.project_sessions.ProjectSessions` managed session, keeping
    the normal lease, start and last-exit auto-stop behaviour.  The run is
    bounded by a timeout; a timeout cancels the exact guest identity via the
    existing session cancellation path, and an unproven cancellation is
    reported as unresolved instead of a false success.  A run that ends
    unresolved with its host proxy still live degrades this broker: later
    ``project-run`` requests are refused, because a broker that still owns a
    live execution cannot promise a clean next one.

Nothing the guest can name -- and nothing that contains its token -- leaves the
bridge unredacted: the token value is scrubbed from responses, receipts and
ledger records recursively.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import sys
import threading
import time
import uuid

from unifier.bootstrap import service_for_root
# Preserve the established bridge imports for callers embedding the broker.
from unifier.bridge_ledger import (
    LEDGER_NAME, MAX_RECEIPT_BYTES, REQUEST_STATES, SCHEMA_VERSION,
    RequestLedger, validate_request_record,
)
from unifier.bridge_policy import (
    ACTION_FIELDS, ACTION_TYPES, DEFAULT_REQUEST_TTL_SECONDS, DEFAULT_TOKEN_ENV,
    ENV_NAME_RE, MAX_EPOCH_SECONDS, MAX_PATH_CHARS, MAX_POLICY_BYTES,
    MAX_REQUEST_TTL_SECONDS,
    MAX_RUN_TIMEOUT_SECONDS, MIN_RUN_TIMEOUT_SECONDS, ORIGIN_FIELDS,
    OUTPUT_REFERENCE_DEFAULT, POLICY_FIELDS, RUN_OVERHEAD_SECONDS, SHA256_RE,
    Action, BridgePolicy, checked_path, load_policy, parse_policy, policy_clock_now,
    require_private_directory, token_environment,
)
from unifier.bridge_protocol import (
    ACTION_ID_RE, DEFAULT_FRAME_LIMIT, ERROR_MESSAGE_CHARS,
    MAX_TOKEN_CHARS, MAX_VALUE_CHARS, MIN_BODY_BYTES, MIN_FRAME_LIMIT, PEERCRED_SIZE,
    PROC_SELF_FD_ROOT, PROTOCOL_VERSION, REDACTED_MARKER, REQUEST_FIELDS,
    RESPONSE_FIELDS, RESPONSE_STATUSES, TOKEN_RE, UNIX_PATH_LIMIT,
    BridgeError, canonical_uuid, copy_json, is_canonical_uuid,
    open_directory_fd, peer_uid, read_frame, redact_token, request_frame, response,
    socket_address_via_fd, strict_dumps, strict_loads, validate_request, write_frame,
)
from unifier.errors import UnifierError
from unifier.identity import binding_for, validate_binding
from unifier.process_identity import process_stamp
from unifier.project_sessions import ProjectSessions


DEFAULT_HANDSHAKE_TIMEOUT = 5.0
DEFAULT_WRITE_TIMEOUT = 10.0
DEFAULT_ACCEPT_POLL = 0.2
DEFAULT_RUN_GRACE_SECONDS = 5.0
MAX_HOST_FILE_BYTES = 8192
MAX_LIST_ITEMS = 32
REVOKE_FILE_BYTES = 64 * 1024

OS_RELEASE_KEY_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
PRINTABLE_RE = re.compile(r"[\x20-\x7e]*\Z")

# host-info reads fixed literal paths only; the whitelists keep the evidence
# structured instead of echoing whatever a file happens to contain.
OS_RELEASE_KEYS = frozenset({
    "ID", "ID_LIKE", "NAME", "PRETTY_NAME", "VERSION", "VERSION_ID",
    "VARIANT", "VARIANT_ID",
})
STATUS_KEYS = frozenset({
    "Name", "Uid", "Gid", "NStgid", "NSpid", "NSpgid", "NSsid", "CapEff",
    "CapBnd", "NoNewPrivs", "Seccomp",
})


def _safe_error(exc) -> dict:
    if isinstance(exc, BridgeError):
        message = exc.message
        code = exc.code
    else:
        message = str(exc)
        code = "host-state"
    if len(message) > ERROR_MESSAGE_CHARS:
        message = message[:ERROR_MESSAGE_CHARS] + "..."
    return {"code": code, "message": message}


# ---------------------------------------------------------------------------
# host-info evidence
# ---------------------------------------------------------------------------
def _read_fixed_file(path: str) -> dict:
    """Read one literal host path with builtins only; never a guest path.

    Only paths named in this module can reach it, so following a system alias
    (Fedora's ``/etc/os-release`` -> ``/usr/lib/os-release``) is not a guest
    choice; the alias target is still reported so the evidence stays honest.
    """
    evidence = {"path": path}
    try:
        observed = os.stat(path)
        if not stat.S_ISREG(observed.st_mode):
            return dict(evidence, unavailable="not a regular file")
        with open(path, "rb") as handle:
            raw = handle.read(MAX_HOST_FILE_BYTES + 1)
    except FileNotFoundError:
        return dict(evidence, unavailable="not present")
    except OSError as exc:
        return dict(evidence, unavailable=f"unreadable ({type(exc).__name__})")
    resolved = os.path.realpath(path)
    if resolved != path:
        evidence["resolved"] = resolved
    if len(raw) > MAX_HOST_FILE_BYTES:
        return dict(evidence, unavailable="larger than the read cap")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return dict(evidence, unavailable="not valid UTF-8")
    if "\x00" in text:
        return dict(evidence, unavailable="contains a NUL byte")
    evidence["text"] = text
    return evidence


def _clean_value(value: str) -> str | None:
    if len(value) > MAX_VALUE_CHARS:
        return None
    if PRINTABLE_RE.fullmatch(value) is None:
        return None
    return value


def _parse_key_value(text: str, allowed: frozenset, *, separator: str) -> dict:
    found = {}
    for line in text.splitlines()[:256]:
        if separator == "=" and (not line or line.startswith("#")):
            continue
        key, _, value = line.partition(separator)
        key = key.strip()
        value = _clean_value(value.strip())
        if key not in allowed or value is None:
            continue
        if separator == "=":
            if OS_RELEASE_KEY_RE.fullmatch(key) is None:
                continue
            if value[:1] in {'"', "'"} and value[-1:] == value[:1] and len(value) >= 2:
                value = value[1:-1]
                if _clean_value(value) is None:
                    continue
        found[key] = value
    return found


def _parse_lines(text: str) -> list:
    lines = []
    for line in text.splitlines()[:MAX_LIST_ITEMS]:
        value = _clean_value(line)
        if value is not None:
            lines.append(value)
    return lines


def _block_without_text(block: dict) -> dict:
    return {key: value for key, value in block.items() if key != "text"}


def host_info(clock=time.time) -> dict:
    """Structured host evidence gathered only from builtins and fixed paths."""
    uname = os.uname()
    evidence = {
        "source": "host-builtin",
        "identity": {
            "uid": os.getuid(), "gid": os.getgid(), "euid": os.geteuid(),
            "egid": os.getegid(), "pid": os.getpid(), "ppid": os.getppid(),
        },
        "uname": {
            "sysname": _clean_value(uname.sysname) or "unknown",
            "release": _clean_value(uname.release) or "unknown",
            "machine": _clean_value(uname.machine) or "unknown",
            "nodename": _clean_value(uname.nodename) or "unknown",
        },
        "os_release": {},
        "process_identity": {},
        "boot_id": None,
        "observed_at": int(clock()),
    }
    release = _read_fixed_file("/etc/os-release")
    if "text" in release:
        evidence["os_release"] = dict(
            _block_without_text(release),
            values=_parse_key_value(release["text"], OS_RELEASE_KEYS, separator="="))
    else:
        evidence["os_release"] = release
    status = _read_fixed_file("/proc/self/status")
    process_identity = {}
    if "text" in status:
        process_identity["status"] = dict(
            _block_without_text(status),
            values=_parse_key_value(status["text"], STATUS_KEYS, separator=":"))
    else:
        process_identity["status"] = status
    for key, path in (("uid_map", "/proc/self/uid_map"),
                      ("gid_map", "/proc/self/gid_map"),
                      ("cgroup", "/proc/self/cgroup")):
        content = _read_fixed_file(path)
        if "text" in content:
            process_identity[key] = dict(_block_without_text(content),
                                         lines=_parse_lines(content["text"]))
        else:
            process_identity[key] = content
    evidence["process_identity"] = process_identity
    boot = _read_fixed_file("/proc/sys/kernel/random/boot_id")
    if "text" in boot:
        value = _clean_value(boot["text"].strip())
        try:
            evidence["boot_id"] = str(uuid.UUID(value or ""))
        except (ValueError, AttributeError, TypeError):
            evidence["boot_id"] = dict(_block_without_text(boot),
                                       unavailable="not a UUID")
    else:
        evidence["boot_id"] = boot
    return evidence


# ---------------------------------------------------------------------------
# the bridge
# ---------------------------------------------------------------------------
class AgentBridge:
    """One broker per origin project session, per host-issued policy."""

    def __init__(self, policy: BridgePolicy, sessions: ProjectSessions, *,
                 clock=time.time, ledger: RequestLedger | None = None,
                 handshake_timeout: float = DEFAULT_HANDSHAKE_TIMEOUT,
                 write_timeout: float = DEFAULT_WRITE_TIMEOUT,
                 accept_poll: float = DEFAULT_ACCEPT_POLL,
                 run_grace_seconds: float = DEFAULT_RUN_GRACE_SECONDS,
                 host_info_source=host_info):
        self.policy = policy
        self.sessions = sessions
        self.service = sessions.service
        self.clock = clock
        self.ledger = ledger or RequestLedger(
            policy.state_root or sessions.root, clock=clock)
        self.handshake_timeout = handshake_timeout
        self.write_timeout = write_timeout
        self.accept_poll = accept_poll
        self.run_grace_seconds = run_grace_seconds
        self.host_info_source = host_info_source
        self.frame_limit = policy.frame_limit
        self._server = None
        self._socket_id = None
        self._socket_parent_fd = None
        self._stopping = False
        # Runs that ended with their host proxy thread still live.  They are not
        # "cleaned up"; they are why this broker refuses to start another run.
        self._unresolved: list = []

    # ----- lifecycle -----------------------------------------------------
    def bind(self) -> Path:
        """Create our own socket: private directory, no symlink, no overwrite.

        The parent directory is 0700 and the socket is 0600, so the socket is
        reachable only by the bridge user; the token is what scopes a request.
        The inode is created in the real directory but the bind goes through a
        pinned FD of that directory, because the full path of a socket inside a
        deep repository checkout does not fit the ``sockaddr_un`` limit.
        """
        path = self.policy.socket_path
        parent = path.parent
        try:
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        except OSError as exc:
            raise BridgeError("socket-dir", f"cannot create {parent}: {exc}") from exc
        try:
            require_private_directory(parent, "socket directory")
        except BridgeError as exc:
            raise BridgeError("socket-dir-insecure", exc.message) from exc
        if path.is_symlink():
            raise BridgeError("socket-alias",
                              f"{path} is a symbolic link; refusing an alias")
        if path.exists():
            raise BridgeError("socket-exists",
                              f"{path} already exists; refusing to overwrite it")
        self._check_grant()
        self.origin_status()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        fd = None
        created = None
        try:
            fd = open_directory_fd(parent, "socket directory", "socket-dir")
            directory = os.fstat(fd)
            if directory.st_uid != os.getuid() or directory.st_mode & 0o077:
                raise BridgeError("socket-dir-insecure", "the opened socket directory is not private")
            address = socket_address_via_fd(fd, path.name, "socket-bind")
            server.bind(address)
            created = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISSOCK(created.st_mode):
                raise BridgeError("socket-verify", "the created socket entry was replaced")
            os.chmod(path.name, 0o600, dir_fd=fd, follow_symlinks=False)
            observed = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
            if (not stat.S_ISSOCK(observed.st_mode)
                    or stat.S_IMODE(observed.st_mode) != 0o600
                    or (observed.st_dev, observed.st_ino) != (created.st_dev, created.st_ino)):
                raise BridgeError("socket-verify", "the private socket identity changed during bind")
            server.listen(8)
            server.settimeout(self.accept_poll)
        except (BridgeError, OSError, ValueError, NotImplementedError) as exc:
            server.close()
            if fd is not None:
                try:
                    current = os.stat(path.name, dir_fd=fd, follow_symlinks=False)
                    if created is not None and (current.st_dev, current.st_ino) == (created.st_dev, created.st_ino):
                        os.unlink(path.name, dir_fd=fd)
                except OSError:
                    pass
                os.close(fd)
            if isinstance(exc, BridgeError):
                raise
            raise BridgeError("socket-bind", f"cannot establish a private socket: {type(exc).__name__}") from exc
        self._server = server
        self._socket_parent_fd = fd
        self._socket_id = (observed.st_dev, observed.st_ino)
        return path

    def close(self) -> None:
        if self._server is not None:
            self._server.close()
            self._server = None
        fd = self._socket_parent_fd
        self._socket_parent_fd = None
        if fd is not None:
            try:
                observed = os.stat(self.policy.socket_path.name, dir_fd=fd, follow_symlinks=False)
                if stat.S_ISSOCK(observed.st_mode) and self._socket_id == (observed.st_dev, observed.st_ino):
                    os.unlink(self.policy.socket_path.name, dir_fd=fd)
            except OSError:
                pass
            finally:
                os.close(fd)
        self._socket_id = None

    def __enter__(self):
        if self._server is None:
            self.bind()
        return self

    def __exit__(self, *_exc_info):
        try:
            self.stop()
        finally:
            self.close()

    def stop(self) -> None:
        """Stop accepting and attempt the precise wrap-up of unresolved runs.

        This does not claim that everything was cleaned: each run this broker
        could not prove ended gets one targeted cancellation of *its own guest
        identity* through ``ProjectSessions.cancel``, and the outcome the runtime
        actually proved is recorded in :meth:`unresolved_runs`.  Anything still
        unproven stays unresolved -- in the journal and in the ledger.
        """
        self._stopping = True
        self._settle_unresolved()

    # ----- origin authority ----------------------------------------------
    def origin_status(self) -> dict:
        """Re-derive the origin identity from host state on every request."""
        session_uuid = self.policy.origin_session_uuid
        try:
            view = self.sessions.show(session_uuid)
        except UnifierError as exc:
            raise BridgeError("origin-unknown",
                              f"origin project session is unusable: {exc}") from exc
        binding = view.get("environment_binding")
        try:
            validate_binding(binding)
        except UnifierError as exc:
            raise BridgeError("origin-unknown",
                              f"origin project session binding is invalid: {exc}") from exc
        if (self.policy.origin_binding is not None
                and dict(binding) != self.policy.origin_binding):
            raise BridgeError(
                "origin-binding-changed",
                "the origin session record names another environment instance "
                "than the host policy pinned")
        environment_id = view.get("environment_id")
        if environment_id != binding["environment_id"]:
            raise BridgeError("origin-binding-changed",
                              "the origin session environment id disagrees with its binding")
        if view.get("effective_state") != "running" or not view.get("worker_live"):
            raise BridgeError(
                "origin-not-running",
                f"the origin project session is {view.get('effective_state')}; "
                "the bridge only serves a live origin session")
        try:
            record = self.service.store.load(environment_id)
            live = binding_for(record)
        except UnifierError as exc:
            raise BridgeError("origin-environment-gone",
                              f"cannot re-read the origin environment identity: {exc}") from exc
        if live != dict(binding):
            raise BridgeError(
                "origin-binding-changed",
                "the environment identity (uuid/generation/container) changed "
                "since the origin session started")
        container = view.get("environment") or {}
        if not container.get("known") or not container.get("running"):
            raise BridgeError("origin-not-running",
                              "the origin environment container is not running")
        if container.get("container_id") != live["container_id"]:
            raise BridgeError(
                "origin-binding-changed",
                "the running container instance is not the one the origin "
                "session is bound to")
        # A worker that is still alive proves nothing on its own: the guest
        # execution the grant was issued for must still be provably live, and
        # the lease that keeps the shared environment owned by this origin must
        # still be held.  Either one lost means the grant no longer describes a
        # serving origin, so the bridge stops instead of answering.
        if view.get("guest_live") is not True:
            raise BridgeError(
                "origin-guest-lost",
                f"the origin session's guest execution is "
                f"{view.get('guest_state') or 'unknown'}, not alive; its host "
                "worker being alive proves nothing")
        if view.get("lease_live") is not True:
            raise BridgeError(
                "origin-lease-lost",
                "the origin session no longer holds a live lease in the shared "
                "launch group")
        return {
            "session_uuid": session_uuid,
            "environment_id": environment_id,
            "environment_binding": dict(live),
            "state": view.get("effective_state"),
            "container": {
                "container_id": container.get("container_id"),
                "running": bool(container.get("running")),
                "status": _clean_value(str(container.get("status") or "unknown"))
                or "unknown",
            },
        }

    # ----- token and scope ------------------------------------------------
    def _check_token(self, token: str) -> None:
        if not hmac.compare_digest(token, self.policy.token):
            raise BridgeError("token-mismatch", "the token is not the one this bridge issued")
        if self._revoked():
            raise BridgeError("token-revoked",
                              "the token is on the host revoke list")

    def _revoked(self) -> bool:
        path = self.policy.revoke_file
        if path is None:
            return False
        try:
            with open(str(path), "rb") as handle:
                raw = handle.read(REVOKE_FILE_BYTES + 1)
        except FileNotFoundError:
            return False
        except OSError as exc:
            # Fail closed: an unreadable revoke list cannot prove the token is live.
            raise BridgeError("revoke-state",
                              f"revoke list is unreadable: {type(exc).__name__}") from exc
        if len(raw) > REVOKE_FILE_BYTES:
            raise BridgeError("revoke-state", "the revoke list exceeds its bounded read limit")
        digest = hashlib.sha256(self.policy.token.encode("utf-8")).hexdigest()
        for line in raw.decode("utf-8", errors="replace").splitlines():
            if hmac.compare_digest(line.strip().lower(), digest):
                return True
        return False

    def _plan_action(self, action: Action) -> dict:
        """Re-resolve the host-pinned target from live host state.

        The target is *the host's* choice, recorded in this policy: it is a
        pinned project UUID plus one environment binding, and that object does
        not have to be the object the request came from -- asking a Debian guest
        to run something in an Arch project is the point of the bridge, not a
        loophole.  What is re-verified on every request is that the pin still
        resolves to exactly that project, that environment instance, that cwd,
        that argv and that scope; ``ProjectSessions.prepare`` is what re-reads
        the host admission of the target environment (distribution acceptance,
        ownership labels, container instance, mount policy), so a moved or
        replaced target is refused here before anything is claimed or started.
        """
        try:
            plan = self.sessions.prepare(
                action.project_uuid, cwd=action.cwd, argv=list(action.argv),
                env_keys=[], mode="run")
        except UnifierError as exc:
            message = str(exc)
            code = "target-changed" if "not registered" in message else "host-state"
            raise BridgeError(code,
                              f"the pinned project could not be revalidated: {message}") from exc
        except OSError as exc:
            raise BridgeError("host-state",
                              f"host state for the pinned project is unavailable: {exc}") from exc
        if plan["project_uuid"] != action.project_uuid:
            raise BridgeError("target-changed",
                              "the pinned project UUID resolves to another project")
        if dict(plan["environment_binding"]) != action.environment_binding:
            raise BridgeError(
                "target-changed",
                "the project environment binding no longer matches the host policy")
        if plan["cwd"] != action.cwd:
            raise BridgeError("target-changed",
                              "the pinned cwd resolves differently than the policy recorded")
        if list(plan["argv"]) != list(action.argv):
            raise BridgeError("target-changed", "the pinned argv no longer matches the policy")
        if action.scope_sha256 is not None and plan["scope_sha256"] != action.scope_sha256:
            raise BridgeError(
                "scope-changed",
                "the project access scope changed since the host issued this action")
        return plan

    # ----- execution ------------------------------------------------------
    def _run_action(self, action: Action, plan: dict, origin: dict,
                    peer_uid: int | None, request_uuid: str) -> tuple:
        """Run the pinned action; returns (status, session_receipt)."""
        try:
            record = self.sessions.create_session(
                action.project_uuid, argv=list(action.argv), cwd=action.cwd,
                env_keys=[], mode="run", tty=False)
        except (UnifierError, OSError) as exc:
            # Nothing ran: report the same session shape with no exit code.
            return "failed", {
                "session_uuid": None,
                "project_uuid": action.project_uuid,
                "environment_binding": dict(action.environment_binding),
                "argv": list(action.argv),
                "cwd": action.cwd,
                "session_user": None,
                "scope_sha256": (plan or {}).get("scope_sha256"),
                "peer_uid": peer_uid,
                "state": "failed",
                "exit_code": None,
                "guest_state": None,
                "timed_out": False,
                "cancel": None,
                "error": f"cannot journal the pinned session: {exc}",
            }
        pinned = {
            "session_uuid": record["session_uuid"],
            "project_uuid": record["project_uuid"],
            "environment_binding": dict(record["environment_binding"]),
            "argv": list(record["argv"]),
            "cwd": record["cwd"],
            "session_user": record.get("session_user"),
            "scope_sha256": record["scope_sha256"],
            "peer_uid": peer_uid,
        }
        for key, expected in (("project_uuid", action.project_uuid),
                              ("cwd", action.cwd),
                              ("argv", list(action.argv)),
                              ("environment_binding", dict(action.environment_binding))):
            if pinned[key] != expected:
                return "failed", dict(
                    pinned, state="failed", exit_code=None, guest_state=None,
                    timed_out=False, cancel=None,
                    error=f"the journaled session {key} does not match the host policy")
        # ``prepare`` ran again inside ``create_session``, so the target's own
        # policy may have moved between this broker's plan and the journaled
        # session.  Nothing has executed yet: refuse, do not run under a scope
        # the host never planned.
        if pinned["scope_sha256"] != (plan or {}).get("scope_sha256") or (
                action.scope_sha256 is not None
                and pinned["scope_sha256"] != action.scope_sha256):
            return "rejected", dict(
                pinned, state=record.get("state") or "planned", exit_code=None,
                guest_state=None, timed_out=False, cancel=None,
                error="the project access scope changed between the host plan "
                      "and the journaled session; nothing was executed")
        self.ledger.bind_execution(request_uuid, pinned)
        session_result = self._execute_bounded(record["session_uuid"],
                                               float(action.timeout_seconds))
        return session_result["status"], dict(pinned, **{
            key: session_result[key] for key in
            ("state", "exit_code", "guest_state", "timed_out", "cancel", "error")})

    def _track_unresolved(self, session_uuid: str, thread) -> None:
        """Remember a run that ended with its host proxy thread still live.

        The broker cannot promise a clean next execution while one of its own
        executions is still running, so this list gates later ``project-run``
        requests on this broker. The ledger also keeps the fence across restart.
        """
        for entry in self._unresolved:
            if entry["session_uuid"] == session_uuid:
                return
        self._unresolved.append({"session_uuid": session_uuid, "thread": thread})

    def unresolved_runs(self) -> list:
        """The runs this broker could not prove ended, with their live status."""
        return [{"session_uuid": entry["session_uuid"],
                 "thread_alive": bool(entry["thread"].is_alive()),
                 "cancel": entry.get("cancel"),
                 "stop_cancel": entry.get("stop_cancel")}
                for entry in self._unresolved]

    def _degraded(self, ignore_request_uuid=None) -> bool:
        if self._unresolved:
            return True
        # Restarting the Python broker must not erase an unproven execution.
        # An interrupted claim may not yet have its session receipt, so both
        # claimed and unresolved records fence further runs on this origin.
        return any(item["origin_session_uuid"] == self.policy.origin_session_uuid
                   and item["request_uuid"] != ignore_request_uuid
                   and item["state"] in {"claimed", "unresolved"}
                   for item in self.ledger.records())

    def recover_requests(self) -> list:
        """Reconcile this origin's dead requests; never replay or cancel work."""
        results = []
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        for item in self.ledger.records():
            if (item['origin_session_uuid'] != self.policy.origin_session_uuid
                    or item['state'] not in {'claimed', 'unresolved'}):
                continue
            answer = {'request_uuid': item['request_uuid'], 'action': 'unresolved'}
            results.append(answer)
            try:
                owner = item.get('owner') or {}
                if not (isinstance(owner.get('pid'), int) and owner['pid'] > 0
                        and isinstance(owner.get('stamp'), int) and owner.get('boot_id')):
                    raise UnifierError('legacy request has no broker process identity')
                if owner['boot_id'] == boot:
                    stamp = process_stamp(owner['pid'])
                    if stamp == owner['stamp']:
                        answer['action'] = 'kept-live-broker'
                        continue
                    if stamp is None and Path(f"/proc/{owner['pid']}").exists():
                        raise UnifierError('broker liveness is ambiguous')
                pinned = item.get('execution')
                if not isinstance(pinned, dict) or not is_canonical_uuid(pinned.get('session_uuid')):
                    raise UnifierError('request has no durable execution binding')
                view = self.sessions.show(pinned['session_uuid'])
                for key in ('project_uuid', 'environment_binding', 'argv', 'cwd', 'scope_sha256'):
                    if view.get(key) != pinned.get(key):
                        raise UnifierError(f'request/session {key} changed')
                self.sessions._verified_instance(view)
                if view.get('worker_live') or view.get('child_live'):
                    answer['action'] = 'kept-live-session'
                    continue
                never_started = (view['state'] == 'planned' and not view.get('guest_pid')
                                 and not view.get('lease_token'))
                if not never_started and view.get('guest_state') != 'gone':
                    raise UnifierError('guest termination is unproven')
                recovery = self.sessions.recover(pinned['session_uuid'])
                after = self.sessions.show(pinned['session_uuid'])
                self.sessions._verified_instance(after)
                if (after.get('worker_live') or after.get('child_live')
                        or after.get('lease_token') or after.get('lease_live')
                        or after['state'] not in {'exited', 'failed', 'interrupted', 'retired'}):
                    raise UnifierError('session recovery is incomplete')
                proof = {'session_uuid': pinned['session_uuid'], 'session_recovery': recovery,
                         'proof': 'never-submitted' if never_started else 'guest-gone',
                         'replayed': False}
                self.ledger.recover_record(item, proof)
                answer.update(action='recovered', **proof)
            except (UnifierError, OSError, ValueError, KeyError) as exc:
                answer['reason'] = str(exc)[:ERROR_MESSAGE_CHARS]
        return results

    def _settle_unresolved(self) -> list:
        """Precise wrap-up on stop: target each unresolved guest identity once.

        This never claims a cleanup happened: it asks the existing session
        cancellation to signal that exact guest identity and journals whatever
        the runtime could actually prove.
        """
        for entry in self._unresolved:
            if entry.get("stop_cancel") is not None:
                continue
            try:
                result = self.sessions.cancel(entry["session_uuid"], signal.SIGKILL)
            except (UnifierError, OSError) as exc:
                result = {"action": "refused",
                          "detail": str(exc)[:ERROR_MESSAGE_CHARS]}
            entry["stop_cancel"] = {"action": str(result.get("action")),
                                    "signal": result.get("signal"),
                                    "lease": result.get("lease")}
            entry["thread"].join(self.run_grace_seconds)
        return self.unresolved_runs()

    def _execute_bounded(self, session_uuid: str, timeout: float) -> dict:
        """Execute with a deadline; only a proven guest end is a success."""
        outcome = {"code": None, "error": None}

        def worker():
            try:
                outcome["code"] = self.sessions.execute(session_uuid)
            except BaseException as exc:  # noqa: BLE001 - reported, never swallowed
                outcome["error"] = str(exc)

        thread = threading.Thread(target=worker, name="unifier-agent-run", daemon=True)
        thread.start()
        deadline = time.monotonic() + timeout
        while thread.is_alive() and not self._stopping:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(min(.1, remaining))
        timed_out = thread.is_alive()
        cancel = None
        if timed_out:
            cancel = self._cancel_session(session_uuid)
            thread.join(self.run_grace_seconds)
        still_running = thread.is_alive()
        if still_running:
            self._track_unresolved(session_uuid, thread)
        view = None
        try:
            view = self.sessions.show(session_uuid)
        except UnifierError:
            view = None
        error = outcome["error"]
        state = (view or {}).get("state")
        exit_code = (view or {}).get("exit_code")
        guest_state = (view or {}).get("guest_state")
        if error is not None and not timed_out:
            return {"status": "failed", "state": state, "exit_code": exit_code,
                    "guest_state": guest_state, "timed_out": False,
                    "cancel": cancel, "error": error}
        if timed_out:
            if still_running:
                return {"status": "unresolved", "state": state,
                        "exit_code": exit_code, "guest_state": guest_state,
                        "timed_out": True, "cancel": cancel,
                        "error": "the run passed its timeout and the guest could "
                                 "not be proven ended; the session is left "
                                 "unresolved instead of claiming success"}
            proven = state in {"exited", "interrupted"} and guest_state == "gone"
            return {"status": "timeout" if proven else "unresolved",
                    "state": state, "exit_code": exit_code,
                    "guest_state": guest_state, "timed_out": True,
                    "cancel": cancel,
                    "error": None if proven else
                    "the run timed out and its cancellation was not proven"}
        if error is not None:
            return {"status": "failed", "state": state, "exit_code": exit_code,
                    "guest_state": guest_state, "timed_out": False,
                    "cancel": cancel, "error": error}
        if state == "exited" and guest_state == "gone" and exit_code is not None:
            return {"status": "ok" if exit_code == 0 else "failed", "state": state,
                    "exit_code": exit_code,
                    "guest_state": guest_state, "timed_out": False,
                    "cancel": cancel, "error": (view or {}).get("error")}
        return {"status": "unresolved", "state": state, "exit_code": exit_code,
                "guest_state": guest_state, "timed_out": False, "cancel": cancel,
                "error": (view or {}).get("error")
                or "the session ended without guest proof; refusing to claim success"}

    def _cancel_session(self, session_uuid: str) -> dict:
        """TERM then KILL through the existing guest-targeted cancellation."""
        steps = []
        for signum in (signal.SIGTERM, signal.SIGKILL):
            try:
                result = self.sessions.cancel(session_uuid, signum)
            except (UnifierError, OSError) as exc:
                steps.append({"signal": int(signum), "action": "refused",
                              "detail": str(exc)[:ERROR_MESSAGE_CHARS]})
                break
            steps.append({"signal": int(signum),
                          "action": str(result.get("action")),
                          "confirmed": result.get("action") in {"cancelled", "already-settled"}})
            if result.get("action") in {"cancelled", "already-settled", "unresolved"}:
                break
        confirmed = any(step.get("confirmed") for step in steps)
        return {"steps": steps, "confirmed": bool(confirmed)}

    # ----- one request ----------------------------------------------------
    def _check_grant(self) -> None:
        """The grant itself has a deadline; a claim TTL says nothing about it."""
        now = policy_clock_now(self.clock)
        if now >= float(self.policy.expires_at):
            raise BridgeError(
                "policy-expired",
                f"the host grant expired at {float(self.policy.expires_at):.0f} and "
                f"the host clock is at {now:.0f}; this bridge serves nothing more")

    def respond(self, payload, *, peer_uid: int | None = None) -> dict:
        """Handle one parsed request.  Never raises for a client fault."""
        request_uuid = None
        action_id = None
        claim = None
        action = None
        # Keep valid public correlation labels even when another field (such
        # as the token) is malformed. The client must never accept an answer
        # for another request, including when this one is refused.
        if isinstance(payload, dict):
            if is_canonical_uuid(payload.get("request_uuid")):
                request_uuid = payload["request_uuid"]
            candidate = payload.get("action_id")
            if isinstance(candidate, str) and ACTION_ID_RE.fullmatch(candidate):
                action_id = candidate
        try:
            request = validate_request(payload)
            request_uuid = request["request_uuid"]
            action_id = request["action_id"]
            self._check_token(request["token"])
            # Before any claim, any plan, any execution: a dead grant is dead,
            # whether this is the first request or the process just restarted.
            self._check_grant()
            action = self.policy.action(action_id)
            if action is None:
                raise BridgeError(
                    "unknown-action",
                    f"action {action_id} is not part of the host-issued scope")
            origin = self.origin_status()
            plan = None
            if action.type == "project-run":
                plan = self._plan_action(action)
                if self._degraded():
                    # A request this broker already saw keeps its own answer
                    # (refusing it as "degraded" would hide the real reason and
                    # a re-send must not look like a fresh attempt); anything
                    # new is refused before it can claim anything.
                    if any(item["request_uuid"] == request_uuid
                           for item in self.ledger.records()):
                        raise BridgeError(
                            "duplicate-request",
                            f"request {request_uuid} was already received")
                    raise BridgeError(
                        "run-unresolved",
                        "an earlier request on this origin remains unproven; "
                        "host recovery is required before another run; restarting "
                        "the broker does not clear this fence")
            claim = self.ledger.reserve(
                request_uuid, action_id, self.policy.origin_session_uuid,
                ttl_seconds=action.request_ttl_seconds)
        except BridgeError as exc:
            return self._answer(request_uuid, action_id, "rejected",
                                error=_safe_error(exc))
        except (UnifierError, OSError) as exc:
            return self._answer(request_uuid, action_id, "rejected",
                                error={"code": "host-state",
                                       "message": str(exc)[:ERROR_MESSAGE_CHARS]})

        base = {
            "schema_version": SCHEMA_VERSION,
            "request_uuid": request_uuid,
            "action_id": action_id,
            "action_type": action.type,
            "origin": {"session_uuid": origin["session_uuid"],
                       "environment_id": origin["environment_id"],
                       "environment_binding": dict(origin["environment_binding"]),
                       "container": dict(origin["container"])},
            "output_reference": self.policy.output_reference,
            "reserved_at": claim["claimed_at"],
            # How long this *claim* may stay open, not when the grant dies.
            "claim_expires_at": claim["expires_at"],
            "grant_expires_at": float(self.policy.expires_at),
        }
        try:
            if action.type == "host-info":
                status = "ok"
                receipt = dict(base, host_info=self.host_info_source(self.clock),
                               target=None, session=None)
                ledger_state = "completed"
                error = None
            else:
                status, session = self._run_action(action, plan, origin, peer_uid, request_uuid)
                receipt = dict(base, host_info=None,
                               target={"project_uuid": action.project_uuid,
                                       "environment_binding": dict(action.environment_binding),
                                       "argv": list(action.argv),
                                       "cwd": action.cwd,
                                       "timeout_seconds": action.timeout_seconds,
                                       "scope_sha256": (plan or {}).get("scope_sha256")},
                               session=session,
                               broker_accepts_runs=not self._degraded(request_uuid))
                ledger_state = ("unresolved" if status == "unresolved" else "completed")
                error = (session or {}).get("error")
        except BridgeError as exc:
            status = "failed"
            receipt = dict(base, host_info=None, target=None, session=None)
            ledger_state = "failed"
            error = _safe_error(exc)["message"]
        except (UnifierError, OSError) as exc:
            status = "failed"
            receipt = dict(base, host_info=None, target=None, session=None)
            ledger_state = "failed"
            error = str(exc)[:ERROR_MESSAGE_CHARS]
        receipt = dict(receipt, status=status, completed_at=float(self.clock()))
        if error:
            receipt["error"] = str(error)[:ERROR_MESSAGE_CHARS]
        # The journal and the answer leave this module with no credential in
        # them, whatever a lower layer put into a message on the way out.
        receipt = redact_token(receipt, self.policy.token)
        error = redact_token(error, self.policy.token)
        try:
            self.ledger.settle(request_uuid, ledger_state, receipt=receipt, error=error)
        except BridgeError as exc:
            return self._answer(request_uuid, action_id, "failed",
                                error={"code": exc.code,
                                       "message": f"the receipt could not be recorded: "
                                                  f"{exc.message}"[:ERROR_MESSAGE_CHARS]})
        return self._answer(request_uuid, action_id, status,
                            receipt=copy_json(receipt))

    def _answer(self, request_uuid, action_id, status: str, *, receipt=None,
                error=None) -> dict:
        """One outbound response, scrubbed of this policy's credential."""
        return redact_token(
            response(request_uuid, action_id, status, receipt=receipt, error=error),
            self.policy.token)

    # ----- transport ------------------------------------------------------
    def serve_one(self, connection) -> None:
        deadline = time.monotonic() + self.handshake_timeout
        uid = peer_uid(connection)
        if uid is not None and uid != os.getuid():
            raise BridgeError("peer-uid",
                              "only the bridge user may talk to this socket")
        # A frame fault closes the connection without echoing guest bytes.
        body = read_frame(connection, limit=self.frame_limit, deadline=deadline,
                          label="request")
        try:
            payload = strict_loads(body.decode("utf-8"))
        except UnicodeDecodeError:
            payload = BridgeError("bad-json", "the request frame is not UTF-8")
        except BridgeError as exc:
            payload = exc
        except ValueError as exc:
            payload = BridgeError("bad-json", f"malformed JSON: {exc}")
        if isinstance(payload, BridgeError):
            # Even a request that never parsed gets a credential-free answer:
            # the guest may have put the token in a field name or a message.
            answer = self._answer(None, None, "rejected", error=_safe_error(payload))
        else:
            answer = self.respond(payload, peer_uid=uid)
        try:
            body = strict_dumps(answer).encode("utf-8")
        except (TypeError, ValueError):
            answer = self._answer(answer["request_uuid"], answer["action_id"], "failed",
                                  error={"code": "bad-receipt",
                                         "message": "the receipt is not serialisable"})
            body = strict_dumps(answer).encode("utf-8")
        if len(body) > self.frame_limit:
            answer = self._answer(
                answer["request_uuid"], answer["action_id"], "failed",
                error={"code": "oversized-receipt",
                                     "message": f"the receipt is {len(body)} bytes, "
                                                f"above the {self.frame_limit}-byte frame limit"})
            body = strict_dumps(answer).encode("utf-8")
        write_frame(connection, body, deadline=time.monotonic() + self.write_timeout)

    def serve_forever(self, *, max_requests: int | None = None) -> int:
        """Serve requests one at a time until stopped or ``max_requests``."""
        if self._server is None:
            self.bind()
        served = 0
        # Snapshot the listener: close() may drop self._server while this
        # thread is parked in accept(), and a closed listener must end the loop.
        server = self._server
        while not self._stopping:
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with connection:
                try:
                    self.serve_one(connection)
                except BridgeError as exc:
                    try:
                        write_frame(
                            connection,
                            strict_dumps(self._answer(None, None, "failed",
                                                     error=_safe_error(exc))).encode("utf-8"),
                            deadline=time.monotonic() + self.write_timeout)
                    except BridgeError:
                        pass
                except (OSError, UnifierError):
                    pass
            served += 1
            if max_requests is not None and served >= max_requests:
                break
        return served

    def __call__(self, payload):  # convenience for an embedding CLI
        return self.respond(payload)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def main(argv=None, *, service_factory=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m unifier.agent_bridge",
        description="host-issued request broker for one guest origin session")
    parser.add_argument("--policy", required=True,
                        help="private host-issued policy file (mode 0600)")
    parser.add_argument("--root", help="unifier state root (default: installed root)")
    parser.add_argument("--once", action="store_true",
                        help="serve exactly one request and exit")
    parser.add_argument('--recover', action='store_true',
                        help='reconcile dead requests without starting a listener or replaying work')
    parser.add_argument("--max-requests", type=int,
                        help="serve at most this many requests and exit")
    args = parser.parse_args(argv)
    bridge = None
    handlers = {}
    try:
        policy = load_policy(args.policy)
        factory = service_for_root if service_factory is None else service_factory
        service = factory(args.root)
        sessions = ProjectSessions(service.root, service)
        bridge = AgentBridge(policy, sessions)
        if args.recover:
            result = bridge.recover_requests()
            print(json.dumps(result, ensure_ascii=False))
            return 1 if any(row['action'] != 'recovered' for row in result) else 0
        def stop_requested(_number, _frame):
            bridge._stopping = True
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            handlers[number] = signal.signal(number, stop_requested)
        path = bridge.bind()
        # Only the path is logged: the token stays out of every log line.
        print(f"agent bridge listening on {path}", flush=True)
        limit = 1 if args.once else args.max_requests
        bridge.serve_forever(max_requests=limit)
        return 0
    except (BridgeError, UnifierError) as exc:
        print(f"agent bridge: {exc}", file=sys.stderr, flush=True)
        return 2
    finally:
        try:
            if bridge is not None:
                try:
                    bridge.stop()
                finally:
                    bridge.close()
        finally:
            for number, handler in handlers.items():
                signal.signal(number, handler)


if __name__ == "__main__":
    raise SystemExit(main())
