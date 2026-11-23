"""Bounded project sessions over one accepted environment.

A session is a private journal record plus a real lease in the same
per-environment ``LaunchGroup`` used by on-demand applications.  The project
registry, data root and environment binding are re-read and revalidated before
any start, after waits and immediately before exec.  A copied project record or
a reused alias is not authority: every execution is resolved from the pinned
project UUID and the live registries.

The execution itself is a foreground ``podman exec`` with an argv array.  A
detached worker owns the lease and the child process group, so killing the
launcher does not falsely release a live execution.  The worker forwards
SIGINT/SIGTERM/SIGHUP only to that execution, never to the shared container.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import signal
import subprocess
import sys
import tempfile
import time
import uuid

from unifier import __version__
from unifier.desktop_launch import integration_collection, validate_launcher
from unifier.errors import UnifierError
from unifier.record_ids import validate_uuid
from unifier.file_routes import project_session_scope
from unifier.identity import binding_for, validate_binding
from unifier.json_records import JsonRecordCollection, fsync_directory
from unifier.on_demand import LaunchGroup, exit_status, process_stamp
from unifier.runtime import ROLE_ENVIRONMENT, GuestProcess, project_label
from unifier.projects import ProjectRegistry, validate_project_record
from unifier.registry import DataRootRegistry


SCHEMA_VERSION = 1
WORKER_MARKER = "MULTILINUX_PROJECT_SESSION_WORKER"
SESSION_STATES = {
    "planned", "starting", "running", "exited", "failed", "interrupted",
    "unresolved", "retired",
}
DENIED_EXACT = {
    "PATH", "SHELL", "ENV", "BASH_ENV", "CDPATH", "PYTHONPATH", "PYTHONHOME",
    "PERL5LIB", "RUBYLIB", "NODE_PATH", "PWD", "OLDPWD", "IFS",
    "GCONV_PATH", "LOCPATH", "NLSPATH",
}
DENIED_PREFIXES = (
    "LD_", "DYLD_", "LIBRARY_PATH", "CPATH", "C_INCLUDE_PATH",
    "CPLUS_INCLUDE_PATH", "PKG_CONFIG_PATH", "BASH_FUNC_",
)
SIGNAL_GRACE_SECONDS = 5
SESSION_USER_RE = re.compile(r"(0|[1-9][0-9]*):(0|[1-9][0-9]*)\Z")


def _canonical_uuid(value, label: str) -> str:
    return validate_uuid(value, f"session {label}", form="canonical lowercase UUID form")


def _scope_fingerprint(scope: dict) -> str:
    used = sorted(
        ({"host": item["host"], "container": item["container"],
          "options": item.get("options", "")} for item in scope.get("used_mounts", [])),
        key=lambda item: (item["host"], item["container"], item["options"]))
    mappings = {
        name: {"path": item["path"],
               "expected_host": item["expected_host"],
               "same_host_object": item["same_host_object"]}
        for name, item in sorted((scope.get("mappings") or {}).items())
    }
    payload = {
        "used_mounts": used,
        "mappings": mappings,
        "runtime_read_only": bool(scope.get("runtime_read_only")),
        "any_used_mount_read_only": bool(scope.get("any_used_mount_read_only")),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_env_keys(keys) -> list[str]:
    if keys is None:
        return []
    if not isinstance(keys, list):
        raise UnifierError("session env keys must be a list")
    seen = set()
    result = []
    for key in keys:
        if not isinstance(key, str) or not key:
            raise UnifierError("session env key must be a non-empty string")
        if (not key.replace("_", "a").isalnum() or not (key[0].isalpha() or key[0] == "_")
                or not key.isascii()):
            raise UnifierError(f"invalid environment variable name: {key!r}")
        if key in DENIED_EXACT or key.startswith(DENIED_PREFIXES):
            raise UnifierError(
                f"environment variable {key} is not allowed in a project session")
        if key in seen:
            continue
        seen.add(key)
        result.append(key)
    return result


def capture_env(keys: list[str]) -> dict[str, str]:
    """Read only explicitly allowlisted host values at execution time.

    There is no fallback to the host environment and no command lookup.  A key
    that disappeared since planning is an error rather than an empty value.
    """
    values = {}
    for key in validate_env_keys(keys):
        if key not in os.environ:
            raise UnifierError(f"allowlisted variable is not set: {key}")
        value = os.environ[key]
        if "\x00" in value or "\n" in value:
            raise UnifierError(f"environment variable {key} contains a newline or NUL")
        values[key] = value
    return values


def validate_session_record(record) -> dict:
    if not isinstance(record, dict):
        raise UnifierError("project session record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise UnifierError(f"project session schema_version {version!r} is not supported")
    _canonical_uuid(record.get("session_uuid"), "session_uuid")
    _canonical_uuid(record.get("project_uuid"), "project_uuid")
    _canonical_uuid(record.get("root_uuid"), "root_uuid")
    for key in ("root_dev", "root_ino", "cwd_dev", "cwd_ino"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnifierError(f"project session {key} must be an integer")
    for key in ("environment_id", "cwd", "scope_sha256", "created_at", "updated_at"):
        value = record.get(key)
        if not isinstance(value, str) or not value:
            raise UnifierError(f"project session {key} must be a non-empty string")
    if not record["cwd"].startswith("/") or "\x00" in record["cwd"] or "\n" in record["cwd"]:
        raise UnifierError("project session cwd must be an absolute path")
    binding = record.get("environment_binding")
    validate_binding(binding)
    if binding["environment_id"] != record["environment_id"]:
        raise UnifierError("project session environment binding names another environment")
    for key in ("environment_uuid", "generation", "container_id"):
        if record.get(key) != binding[key]:
            raise UnifierError(f"project session {key} disagrees with its binding")
    argv = record.get("argv")
    if not isinstance(argv, list) or not argv or any(
            not isinstance(item, str) or item == "" or "\x00" in item for item in argv):
        raise UnifierError("project session argv must be a non-empty string array")
    keys = record.get("env_keys")
    if validate_env_keys(keys) != keys:
        raise UnifierError("project session env_keys are not canonical")
    state = record.get("state")
    if state not in SESSION_STATES:
        raise UnifierError(f"unknown project session state: {state!r}")
    if not isinstance(record.get("access_scope"), dict):
        raise UnifierError("project session access_scope must be an object")
    if not isinstance(record.get("tty"), bool):
        raise UnifierError("project session tty must be a boolean")
    if not isinstance(record.get("mode"), str) or record["mode"] not in {"run", "shell"}:
        raise UnifierError("project session mode must be run or shell")
    session_user = record.get("session_user")
    if not isinstance(session_user, str) or not SESSION_USER_RE.fullmatch(session_user):
        raise UnifierError(
            "project session session_user must be a numeric uid:gid identity")
    integration_uuid = record.get("integration_uuid")
    if integration_uuid is not None:
        _canonical_uuid(integration_uuid, "integration_uuid")
    signals = record.get("signals")
    if not isinstance(signals, list) or any(
            isinstance(item, bool) or not isinstance(item, int) or item <= 0
            for item in signals):
        raise UnifierError("project session signals must be a list of positive integers")
    for key in ("worker_pid", "worker_stamp", "child_pid", "child_stamp",
                "exit_code", "guest_pid", "guest_start", "guest_pgrp", "guest_sid"):
        value = record.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise UnifierError(f"project session {key} must be an integer or null")
    token = record.get("lease_token")
    if token is not None and (not isinstance(token, str) or not token):
        raise UnifierError("project session lease_token must be a string or null")
    guest_token = record.get("guest_token")
    if guest_token is not None and (
            not isinstance(guest_token, str) or not guest_token
            or any(ch not in "0123456789abcdef" for ch in guest_token)):
        raise UnifierError("project session guest_token must be a lowercase hex string or null")
    guest_state = record.get("guest_state")
    if guest_state is not None and guest_state not in {"alive", "gone", "unknown", "unsupported"}:
        raise UnifierError(f"unknown project session guest_state: {guest_state!r}")
    return record


class SessionStore:
    def __init__(self, root: Path):
        self.collection = JsonRecordCollection(
            Path(root), "project-sessions", schema_version=SCHEMA_VERSION,
            validator=validate_session_record, unique_keys=("session_uuid",))

    def list(self) -> list[dict]:
        return self.collection.list()

    def read(self, session_uuid: str) -> dict:
        _canonical_uuid(session_uuid, "session_uuid")
        for record in self.list():
            if record["session_uuid"] == session_uuid:
                return dict(record)
        raise UnifierError(f"project session {session_uuid} is not recorded")

    def write(self, record: dict) -> dict:
        validate_session_record(record)
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            replaced = False
            for index, existing in enumerate(records):
                if existing["session_uuid"] == record["session_uuid"]:
                    records[index] = dict(record)
                    replaced = True
                    break
            if not replaced:
                records.append(dict(record))
            self.collection.write_unlocked(records)
        return dict(record)

    def update(self, session_uuid: str, **changes) -> dict:
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            for index, existing in enumerate(records):
                if existing["session_uuid"] == session_uuid:
                    updated = dict(existing)
                    updated.update(changes)
                    validate_session_record(updated)
                    records[index] = updated
                    self.collection.write_unlocked(records)
                    return dict(updated)
        raise UnifierError(f"project session {session_uuid} is not recorded")


def _stat_anchored(path: str, label: str) -> os.stat_result:
    if (not isinstance(path, str) or not path.startswith("/")
            or "\x00" in path or "\n" in path or os.path.normpath(path) != path):
        raise UnifierError(f"{label} must be a normalized absolute path")
    parts = PurePosixPath(path).parts
    if ".." in parts:
        raise UnifierError(f"{label} must not contain '..' components")
    real = os.path.realpath(path)
    if real != path:
        raise UnifierError(f"{label} must not resolve through a symbolic link")
    try:
        observed = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise UnifierError(f"{label} is unavailable: {exc}") from exc
    return observed


def _inside(path: str, root: str) -> bool:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    return len(path_parts) >= len(root_parts) and path_parts[:len(root_parts)] == root_parts


class ProjectSessions:
    def __init__(self, root: Path, service, *, store=None):
        self.root = Path(root)
        self.service = service
        self.store = store or SessionStore(self.root)
        self.roots = DataRootRegistry(self.root)
        self.projects = ProjectRegistry(self.root)

    # ----- resolution and validation --------------------------------------
    def _root_record(self, project: dict) -> dict:
        root = self.roots.show(project["root_uuid"])
        if root["root_uuid"] != project["root_uuid"]:
            raise UnifierError("project root registration changed")
        locator = root["locator"]
        observed = _stat_anchored(locator, "project root")
        if not os.path.isdir(locator):
            raise UnifierError("project root is not a directory")
        if (observed.st_dev, observed.st_ino) != (root["root_dev"], root["root_ino"]):
            raise UnifierError("project root path was replaced since registration")
        matched, _relative = self.roots.match(locator)
        if matched is None or matched["root_uuid"] != root["root_uuid"]:
            raise UnifierError("project root is shadowed by a different registered root")
        return root

    def _cwd(self, root: dict, cwd: str | None, project: dict) -> tuple[str, os.stat_result]:
        cwd = cwd if cwd is not None else project.get("default_save_root")
        if not isinstance(cwd, str) or not cwd:
            raise UnifierError("project session needs an explicit cwd")
        observed = _stat_anchored(cwd, "project session cwd")
        if not os.path.isdir(cwd):
            raise UnifierError("project session cwd must be a directory")
        if not _inside(cwd, root["locator"]):
            raise UnifierError("project session cwd must stay inside the project root")
        matched, _relative = self.roots.match(cwd)
        if matched is None or matched["root_uuid"] != root["root_uuid"]:
            target = None if matched is None else matched["root_uuid"]
            raise UnifierError(
                "project session cwd enters a different registered data root"
                + (f" ({target})" if target else "")
                + "; choose a directory inside the project root")
        return cwd, observed

    def _environment(self, project: dict, root: dict, environment: dict, cwd: str) -> dict:
        self.service._require_host_compatibility(environment)
        binding = project.get("environment_binding")
        validate_binding(binding)
        if environment.get("id") != binding["environment_id"]:
            raise UnifierError("project environment binding names another environment")
        self.service._require_record_binding(environment, binding)
        if environment.get("target") != project["toolchain"].get("target"):
            raise UnifierError("project toolchain target no longer matches the environment")
        info = self.service._require_owned(environment)
        if info.container_id != binding["container_id"]:
            raise UnifierError(
                "environment container instance changed; create a new project session")
        scope = project_session_scope(environment, root["locator"], cwd)
        from unifier.file_access import require_registered_permission
        from unifier.file_access_store import AccessStore
        purpose = 'read' if project['access_policy'] == 'read-only' or root['access_policy'] == 'read-only' else 'edit'
        require_registered_permission(project, purpose)
        require_registered_permission(root, purpose)
        policy = AccessStore(self.root).policy()
        policy.require(root['locator'], purpose, directory=True)
        policy.require(cwd, purpose, directory=True)
        read_only_policy = (project["access_policy"] == "read-only"
                            or root["access_policy"] == "read-only")
        if read_only_policy:
            if not scope["runtime_read_only"]:
                raise UnifierError(
                    "read-only project policy requires every used environment mount "
                    "to be OS read-only; refusing a writable session")
        elif scope["any_used_mount_read_only"]:
            raise UnifierError(
                "a used environment mount is read-only while the project is "
                "read-write; refusing the session")
        return scope

    def _session_user(self, environment: dict) -> str:
        """Return the numeric guest uid:gid that owns documents.

        keep-id maps the host user to itself, so the host document identity is
        passed explicitly.  Rootless Podman's default namespace maps container
        root to the host user, so ``0:0`` is that namespace's proven identity.
        No other namespace is guessed: a wrong user silently creates files as
        a subordinate UID.
        """
        runtime = environment.get("runtime") or {}
        namespace = runtime.get("user_namespace")
        if namespace == "keep-id":
            return f"{self.service.uid}:{self.service.gid}"
        if namespace == "rootless-default":
            return "0:0"
        raise UnifierError(
            f"cannot prove the document user for user namespace {namespace!r}; "
            "refusing to run a project session with an unpinned owner")

    def prepare(self, project_identifier: str, *, cwd: str | None = None,
                argv=None, env_keys=None, mode: str = "run",
                integration_uuid: str | None = None) -> dict:
        """Read-only plan. No start, bridge or exec happens here."""
        project = self.projects.show(project_identifier)
        validate_project_record(project)
        root = self._root_record(project)
        cwd, observed = self._cwd(root, cwd, project)
        environment = self.service.store.load(project["environment_binding"]["environment_id"])
        binding = binding_for(environment)
        if binding != project["environment_binding"]:
            raise UnifierError("project environment binding changed; re-register the project")
        scope = self._environment(project, root, environment, cwd)
        session_user = self._session_user(environment)
        if integration_uuid is not None:
            _canonical_uuid(integration_uuid, "integration_uuid")
        argv = list(argv or [])
        if argv and any(not isinstance(item, str) or not item or "\x00" in item
                        for item in argv):
            raise UnifierError("project session argv must be a non-empty string array")
        env_keys = validate_env_keys(env_keys or [])
        return {
            "schema_version": SCHEMA_VERSION,
            "operation": "project.plan",
            "project_uuid": project["project_uuid"],
            "project_alias": project.get("alias"),
            "root_uuid": root["root_uuid"],
            "root_dev": root["root_dev"],
            "root_ino": root["root_ino"],
            "root_path": root["locator"],
            "environment_id": binding["environment_id"],
            "environment_uuid": binding["environment_uuid"],
            "generation": binding["generation"],
            "container_id": binding["container_id"],
            "environment_binding": dict(binding),
            "target": environment["target"],
            "cwd": cwd,
            "cwd_dev": observed.st_dev,
            "cwd_ino": observed.st_ino,
            "argv": argv,
            "env_keys": env_keys,
            "mode": mode,
            "session_user": session_user,
            "integration_uuid": integration_uuid,
            "access_scope": scope,
            "scope_sha256": _scope_fingerprint(scope),
            "execution": {
                "mode": "argv-array",
                "shell": False,
                "stdin": "pipe",
                "tty": "explicit",
                "lease": "shared-per-environment-launch-group",
                "user": session_user,
                "revalidate_on_start_and_exec": True,
                "guest_identity": "pid+kernel-start+session over a preserved control fd",
                "guest_signal_user": session_user,
                "cancel": "targeted guest signal, never the podman proxy alone",
                "background_children": "cleaned inside the same guest session",
            },
        }

    def create_session(self, project_identifier: str, *, argv, cwd=None,
                       env_keys=None, mode="run", tty=False,
                       timestamp=None, integration_uuid=None) -> dict:
        prepared = self.prepare(
            project_identifier, cwd=cwd, argv=argv, env_keys=env_keys, mode=mode,
            integration_uuid=integration_uuid)
        prepared["argv"] = list(argv)
        stamp = timestamp or self.service.clock()
        record = {
            "schema_version": SCHEMA_VERSION,
            "session_uuid": str(uuid.uuid4()),
            "project_uuid": prepared["project_uuid"],
            "root_uuid": prepared["root_uuid"],
            "root_dev": prepared["root_dev"],
            "root_ino": prepared["root_ino"],
            "environment_id": prepared["environment_id"],
            "environment_binding": prepared["environment_binding"],
            "environment_uuid": prepared["environment_uuid"],
            "generation": prepared["generation"],
            "container_id": prepared["container_id"],
            "cwd": prepared["cwd"],
            "cwd_dev": prepared["cwd_dev"],
            "cwd_ino": prepared["cwd_ino"],
            "argv": list(prepared["argv"]),
            "env_keys": list(prepared["env_keys"]),
            "env_policy": {
                "allowlist": "explicit-host-keys-only",
                "host_environment_inherited": False,
                "fallback_commands": False,
                "dynamic_loader_variables": False,
            },
            "access_scope": prepared["access_scope"],
            "scope_sha256": prepared["scope_sha256"],
            "mode": mode,
            "tty": bool(tty),
            "session_user": prepared["session_user"],
            "integration_uuid": prepared["integration_uuid"],
            "state": "planned",
            "lease_token": None,
            "worker_pid": None,
            "worker_stamp": None,
            "child_pid": None,
            "child_stamp": None,
            "guest_pid": None,
            "guest_start": None,
            "guest_pgrp": None,
            "guest_sid": None,
            "guest_token": None,
            "guest_state": None,
            "exit_code": None,
            "signals": [],
            "error": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        return self.store.write(record)

    def _validate_integration_pin(self, record: dict) -> None:
        integration_uuid = record.get("integration_uuid")
        if integration_uuid is None:
            return
        validate_launcher(
            integration_collection(self.root), self.projects.show, self.service,
            integration_uuid, project_uuid=record["project_uuid"])

    def _validate_session(self, record: dict) -> dict:
        validate_session_record(record)
        if record["state"] in {"retired", "exited", "failed"}:
            raise UnifierError(
                f"project session {record['session_uuid']} is {record['state']}; "
                "create a new session")
        prepared = self.prepare(
            record["project_uuid"], cwd=record["cwd"], argv=record["argv"],
            env_keys=record["env_keys"], mode=record["mode"],
            integration_uuid=record.get("integration_uuid"))
        if prepared["root_uuid"] != record["root_uuid"]:
            raise UnifierError("project session root changed; create a new session")
        if prepared["environment_binding"] != record["environment_binding"]:
            raise UnifierError("project session environment binding changed")
        if (prepared["root_dev"], prepared["root_ino"]) != (
                record["root_dev"], record["root_ino"]):
            raise UnifierError("project session root inode changed")
        if (prepared["cwd_dev"], prepared["cwd_ino"]) != (
                record["cwd_dev"], record["cwd_ino"]):
            raise UnifierError("project session cwd was replaced")
        if prepared["scope_sha256"] != record["scope_sha256"]:
            raise UnifierError(
                "project session access scope changed; create a new session")
        if prepared["env_keys"] != record["env_keys"]:
            raise UnifierError("project session env policy changed")
        if prepared["argv"] != record["argv"]:
            raise UnifierError("project session argv changed")
        if prepared["session_user"] != record.get("session_user"):
            raise UnifierError(
                "project session document user changed; create a new session")
        self._validate_integration_pin(record)
        return prepared

    # ----- runtime --------------------------------------------------------
    def _worker_command(self, session_uuid: str) -> list[str]:
        entry = Path(__file__).resolve().parents[2] / "tools/unifier.py"
        return [sys.executable, str(entry), "project", "_worker",
                "--session", session_uuid]

    def run(self, project_identifier: str, argv: list[str], *, cwd=None,
            env_keys=None, tty=False, integration_uuid=None) -> dict:
        if not argv or any(not isinstance(item, str) or not item or "\x00" in item
                           for item in argv):
            raise UnifierError("project run requires a non-empty argv array after --")
        if tty and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise UnifierError("--tty requires an actual interactive terminal")
        record = self.create_session(
            project_identifier, argv=argv, cwd=cwd, env_keys=env_keys,
            mode="run", tty=tty, integration_uuid=integration_uuid)
        return self.launch(record)

    def shell(self, project_identifier: str, *, cwd=None, env_keys=None,
              tty=None, integration_uuid=None) -> dict:
        requested = bool(tty) if tty is not None else (
            sys.stdin.isatty() and sys.stdout.isatty())
        if requested and not (sys.stdin.isatty() and sys.stdout.isatty()):
            raise UnifierError("--tty requires an actual interactive terminal")
        record = self.create_session(
            project_identifier, argv=["/bin/sh"], cwd=cwd, env_keys=env_keys,
            mode="shell", tty=requested, integration_uuid=integration_uuid)
        return self.launch(record)

    def _wait_forwarding(self, process, *, tty: bool = False) -> int:
        """Wait for the session worker and forward terminal signals to it.

        The worker, not this launcher, owns the lease.  For a TTY the terminal
        byte stream and the guest pty own Ctrl-C; forwarding SIGINT to the
        worker would kill the interactive shell instead of interrupting its
        foreground command.  TERM/HUP and TTY resize are forwarded.  Host termios is
        restored even when the proxy exits unexpectedly.
        """
        old = {}
        holder = {"process": process}
        saved_termios = None
        if tty:
            try:
                import termios
                saved_termios = (sys.stdin.fileno(), termios.tcgetattr(sys.stdin.fileno()))
            except (ImportError, OSError, ValueError):
                saved_termios = None

        def forward(signum, _frame):
            target = holder.get("process")
            if target is not None and target.poll() is None:
                try:
                    os.kill(target.pid, signum)
                except OSError:
                    pass

        def ignore(_signum, _frame):
            return None

        handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        if tty:
            handled += (signal.SIGWINCH,)
        try:
            for signum in handled:
                try:
                    old[signum] = signal.signal(
                        signum, ignore if tty and signum == signal.SIGINT else forward)
                except (ValueError, OSError):
                    pass
            return exit_status(process.wait())
        finally:
            for signum, handler in old.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass
            if saved_termios is not None:
                try:
                    import termios
                    termios.tcsetattr(saved_termios[0], termios.TCSADRAIN,
                                      saved_termios[1])
                except (ImportError, OSError, ValueError):
                    pass

    def launch(self, record: dict) -> dict:
        if os.environ.get(WORKER_MARKER) == "1":
            code = self.execute(record["session_uuid"])
            return {"session_uuid": record["session_uuid"], "returncode": code}
        from unifier.install import worker_environment
        environment = worker_environment(self.service.root, os.environ)
        environment[WORKER_MARKER] = "1"
        try:
            process = subprocess.Popen(
                self._worker_command(record["session_uuid"]),
                start_new_session=True, env=environment,
                stdin=None, stdout=None, stderr=None)
        except OSError as exc:
            self.store.update(record["session_uuid"], state="failed", error=str(exc),
                              updated_at=self.service.clock())
            raise UnifierError(f"cannot start the project session worker: {exc}") from exc
        code = self._wait_forwarding(process, tty=bool(record.get("tty")))
        return {"session_uuid": record["session_uuid"], "returncode": code}

    def _supervise(self, execution, record: dict, pending: dict):
        """Wait while delivering cancellation to the guest, never the proxy."""
        signals = list(record.get("signals") or [])
        delivered = None
        deadline = None
        escalated = False
        unknown_cancel = False
        while True:
            if pending.get("resize"):
                pending["resize"] = False
                if record.get("tty"):
                    execution.resize()
            signum = pending.get("signal")
            if signum is not None:
                pending["signal"] = None
                if record.get("tty") and signum == signal.SIGINT:
                    # The guest pty handles Ctrl-C. Sending SIGINT to the
                    # interactive shell pid from outside would terminate the
                    # shell instead of just its foreground command.
                    pass
                else:
                    status = execution.signal(signum)
                    if status == "signalled":
                        if delivered is None:
                            delivered = signum
                        if signum not in signals:
                            signals.append(signum)
                        self.store.update(
                            record["session_uuid"], signals=signals,
                            updated_at=self.service.clock())
                        deadline = time.monotonic() + SIGNAL_GRACE_SECONDS
                        escalated = False
                        unknown_cancel = False
                    elif status == "unknown":
                        unknown_cancel = True
                        deadline = time.monotonic() + SIGNAL_GRACE_SECONDS
                        escalated = False
            if deadline is not None and time.monotonic() >= deadline and not escalated:
                if unknown_cancel:
                    # No proof that the guest accepted a signal. Abort only the
                    # host proxy so this worker cannot hang, and let the caller
                    # record an unresolved execution without releasing its lease.
                    execution.abort_proxy()
                else:
                    kill_status = execution.signal(signal.SIGKILL)
                    if kill_status == "unknown":
                        execution.abort_proxy()
                escalated = True
                deadline = time.monotonic() + 2
            try:
                code = execution.wait(timeout=0.1)
                return exit_status(code), delivered, signals
            except subprocess.TimeoutExpired:
                continue
            except (OSError, ValueError) as exc:
                raise UnifierError(f"cannot wait for podman exec: {exc}") from exc

    def _cancel_execution(self, execution) -> bool:
        """Best-effort targeted cancellation; True only when guest is gone."""
        try:
            status = execution.signal(signal.SIGTERM)
            if status != "gone":
                try:
                    execution.wait(timeout=SIGNAL_GRACE_SECONDS)
                except (subprocess.TimeoutExpired, OSError, ValueError):
                    execution.signal(signal.SIGKILL)
                    try:
                        execution.wait(timeout=2)
                    except (subprocess.TimeoutExpired, OSError, ValueError):
                        execution.abort_proxy()
        except BaseException:
            try:
                execution.abort_proxy()
            except BaseException:
                pass
        try:
            cleanup = execution.cleanup()
            return cleanup == "gone"
        except BaseException:
            return False

    def execute(self, session_uuid: str) -> int:
        """Worker entry: hold the lease, track the real guest, then settle."""
        record = self.store.read(session_uuid)
        try:
            self._validate_session(record)
        except BaseException as exc:
            self.store.update(
                session_uuid, state="failed", error=str(exc),
                updated_at=self.service.clock())
            raise
        pending = {"signal": None, "resize": False}
        old_handlers = {}

        def forward(signum, _frame):
            # Only record the request. The main-thread supervisor performs the
            # targeted guest signal; no host proxy is killed from a handler.
            if signum == signal.SIGWINCH:
                pending["resize"] = True
            else:
                pending["signal"] = signum

        handled = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        if record.get("tty"):
            handled += (signal.SIGWINCH,)
        for signum in handled:
            try:
                old_handlers[signum] = signal.signal(signum, forward)
            except (ValueError, OSError):
                pass

        group = LaunchGroup(self.service, record["environment_id"])
        token = None
        execution = None
        confirmed = False
        spawn_attempted = False
        try:
            token = group.acquire_project(
                record["project_uuid"],
                expected_binding=record["environment_binding"],
                validate=lambda _current: self._validate_session(record))
            # Re-read the journal and registries after the start/wait boundary.
            record = self.store.read(session_uuid)
            self._validate_session(record)
            record = self.store.update(
                session_uuid, state="running", lease_token=token,
                worker_pid=os.getpid(), worker_stamp=process_stamp(os.getpid()),
                worker_version=__version__,
                updated_at=self.service.clock())

            def spawned(process):
                self.store.update(
                    session_uuid, child_pid=process.pid,
                    child_stamp=process_stamp(process.pid),
                    state="running", updated_at=self.service.clock())

            runtime = self.service.store.load(record["environment_id"])["runtime"]
            env = capture_env(record["env_keys"])
            for attribute in ("start_session_exec", "probe_guest", "signal_guest",
                              "cleanup_guest"):
                if not hasattr(self.service.runtime, attribute):
                    raise UnifierError(
                        "runtime cannot prove or target guest execution identity; "
                        "refusing to start an untracked session")
            # Immediate final revalidation after the start/wait boundary and
            # before the exec, including the exact document user pin.
            boundary = self._validate_session(record)
            if boundary["session_user"] != record["session_user"]:
                raise UnifierError(
                    "project session document user changed at the exec boundary")
            spawn_attempted = True
            execution = self.service.runtime.start_session_exec(
                runtime["container_name"], list(record["argv"]),
                project=runtime["project"], env_id=record["environment_id"],
                workdir=record["cwd"], env=env, user=record["session_user"],
                interactive=True, tty=bool(record["tty"]), on_spawn=spawned)
            guest = execution.guest
            record = self.store.update(
                session_uuid, guest_pid=guest.pid, guest_start=guest.start,
                guest_pgrp=guest.pgrp, guest_sid=guest.sid,
                guest_token=guest.token, guest_state="alive",
                state="running", updated_at=self.service.clock())
            code, delivered, signals = self._supervise(execution, record, pending)
            cleanup = execution.cleanup()
            confirmed = cleanup == "gone"
            if delivered is not None and confirmed:
                # podman/setsid can report 0, 1 or the raw signal value from
                # the proxy even after the guest was really signalled.  Report
                # the shell convention once, and only with guest proof.
                code = 128 + int(delivered)
            state = "exited" if confirmed else "unresolved"
            error = (None if confirmed else
                     "guest execution could not be confirmed ended; the lease is "
                     "kept unresolved instead of pretending a proxy exit stopped it")
            self.store.update(
                session_uuid, state=state, exit_code=code,
                child_pid=None, child_stamp=None,
                guest_state="gone" if confirmed else "unknown",
                signals=signals, error=error,
                finished_at=(self.service.clock() if confirmed else None),
                updated_at=self.service.clock())
            return code
        except BaseException as exc:
            if execution is not None:
                confirmed = self._cancel_execution(execution)
            else:
                # A failure before any spawn attempt cannot have created a
                # guest process. A failed readiness after spawn is deliberately
                # not treated as proof that the guest is gone.
                confirmed = not spawn_attempted
            state = "failed" if confirmed else "unresolved"
            self.store.update(
                session_uuid, state=state, error=str(exc),
                child_pid=None, child_stamp=None,
                guest_state=("gone" if confirmed else
                             ("unknown" if spawn_attempted else None)),
                updated_at=self.service.clock())
            raise
        finally:
            for signum, handler in old_handlers.items():
                try:
                    signal.signal(signum, handler)
                except (ValueError, OSError):
                    pass
            if token is not None and confirmed:
                try:
                    group.release(token)
                except Exception as exc:  # keep the exit record honest
                    try:
                        self.store.update(
                            session_uuid, error=f"lease release failed: {exc}",
                            updated_at=self.service.clock())
                    except Exception:
                        pass
                else:
                    try:
                        latest = self.store.read(session_uuid)
                        self.store.update(
                            session_uuid, lease_token=None,
                            updated_at=latest["updated_at"])
                    except Exception:
                        pass

    # ----- inspection and recovery ---------------------------------------
    def _container_state(self, record: dict) -> dict:
        try:
            environment = self.service.store.load(record["environment_id"])
            info = self.service.runtime.inspect(environment["runtime"]["container_name"])
        except Exception as exc:
            return {"known": False, "error": str(exc)}
        if info is None:
            return {"known": False, "error": "container does not exist"}
        return {
            "known": True,
            "container_id": info.container_id,
            "running": bool(info.running),
            "status": info.status,
            "started_at": info.started_at,
            "project": info.project,
            "env_id": info.env_id,
            "role": info.role,
        }

    def _guest_from_record(self, record: dict):
        if record.get("guest_pid") is None or record.get("guest_start") is None:
            return None
        return GuestProcess(
            pid=record["guest_pid"], start=record["guest_start"],
            pgrp=record.get("guest_pgrp") or record["guest_pid"],
            sid=record.get("guest_sid") or record["guest_pid"],
            token=record.get("guest_token") or "")

    def _verified_instance(self, record: dict) -> tuple:
        """Re-read the environment and pin the exact container instance.

        Nothing is started or changed here: recovery must not wake an old
        graphical container just to ask it a question.  The session's journaled
        binding (UUID, generation, container id) is compared with the live
        environment record and with the live container's own project/env/role
        labels, so a same-name replacement, a new generation or a foreign label
        can never be mistaken for the instance this session executed in.

        Returns ``(container_id, project, environment_id, running)``. A runtime
        that cannot inspect the pinned id cannot prove this instance's state.
        """
        binding = validate_binding(record.get("environment_binding"))
        environment = self.service.store.load(binding["environment_id"])
        live = binding_for(environment)
        if live != binding:
            raise UnifierError(
                "the environment instance moved to another generation or "
                "container since this session was bound")
        runtime = environment.get("runtime")
        if not isinstance(runtime, dict):
            raise UnifierError("environment runtime record is missing")
        project = runtime.get("project")
        if not isinstance(project, str) or project != project_label(self.root):
            raise UnifierError("environment runtime record is incomplete")
        # A name can be reused between inspection and the subsequent probe.
        address = binding["container_id"]
        info = self.service.runtime.inspect(address)
        if info is None:
            raise UnifierError("the environment container does not exist")
        if info.container_id != binding["container_id"]:
            raise UnifierError(
                "the live container instance is not the one this session is bound to")
        if (info.project != project or info.env_id != binding["environment_id"]
                or info.role != ROLE_ENVIRONMENT):
            raise UnifierError(
                "the container is not owned as this project's environment")
        return address, project, binding["environment_id"], bool(info.running)

    def _guest_state(self, record: dict) -> str:
        guest = self._guest_from_record(record)
        if guest is None:
            return "unknown"
        try:
            if not hasattr(self.service.runtime, "probe_guest"):
                return "unknown"
            address, project, environment_id, running = self._verified_instance(record)
            if not running:
                # The exact instance the session was bound to is stopped and is
                # still that same instance, so nothing inside it can still be
                # running.  An unknown, replaced or foreign container proves
                # nothing and stays unknown.
                return "gone"
            # Probe inside the instance verified above, addressed by its full
            # id, never by a name that could have been re-pointed at another
            # container in the meantime.
            return self.service.runtime.probe_guest(
                address, guest, project=project, env_id=environment_id,
                user=record.get("session_user"))
        except Exception:
            return "unknown"

    def show(self, session_uuid: str) -> dict:
        record = self.store.read(session_uuid)
        worker_live = bool(
            record.get("worker_pid") is not None
            and record.get("worker_stamp") is not None
            and process_stamp(record["worker_pid"]) == record["worker_stamp"])
        child_live = bool(
            record.get("child_pid") is not None
            and record.get("child_stamp") is not None
            and process_stamp(record["child_pid"]) == record["child_stamp"])
        state = record["state"]
        guest_state = record.get("guest_state") or "unknown"
        # A guest identity already confirmed gone by the worker is historical
        # evidence, even if the environment has since stopped. Anything else is
        # re-probed from the guest's own /proc identity; a dead host proxy is
        # never evidence that the guest ended.
        if self._guest_from_record(record) is not None and guest_state != "gone":
            guest_state = self._guest_state(record)
        view = dict(record)
        view.setdefault("worker_version", None)
        view["worker_live"] = worker_live
        view["child_live"] = child_live
        view["guest_state"] = guest_state
        view["guest_live"] = guest_state == "alive"
        view["environment"] = self._container_state(record)
        token = record.get("lease_token")
        view["lease_recorded"] = bool(token)
        if token:
            try:
                group = LaunchGroup(self.service, record["environment_id"])
                view["lease_live"] = token in group.read().get("leases", {})
            except Exception:
                view["lease_live"] = False
        else:
            view["lease_live"] = False
        if worker_live:
            effective = state
        elif child_live or guest_state == "alive":
            effective = "unresolved"
        elif guest_state == "unknown" and (
                token or state in {"planned", "starting", "running"}):
            effective = "unresolved"
        elif state in {"planned", "starting", "running"}:
            effective = "interrupted"
        else:
            effective = state
        view["effective_state"] = effective
        view["execution_pinned"] = {
            "project_uuid": record["project_uuid"],
            "root_uuid": record["root_uuid"],
            "environment_uuid": record["environment_uuid"],
            "generation": record["generation"],
            "container_id": record["container_id"],
            "argv": list(record["argv"]),
            "cwd": record["cwd"],
            "session_user": record.get("session_user"),
            "integration_uuid": record.get("integration_uuid"),
            "scope_sha256": record["scope_sha256"],
            "guest_pid": record.get("guest_pid"),
            "guest_start": record.get("guest_start"),
            "guest_sid": record.get("guest_sid"),
        }
        return view

    def list(self) -> list[dict]:
        return [self.show(record["session_uuid"]) for record in self.store.list()]

    def _release_dead_lease(self, record: dict):
        token = record.get("lease_token")
        if not token:
            return None
        # The journaled token plus confirmed guest termination is the proof.
        # A missing host worker identity is not itself evidence that a guest
        # survived, and withholding a dead token forever would keep auto-stop
        # ownership on a session nobody can resolve.
        try:
            group = LaunchGroup(self.service, record["environment_id"])
            if token in group.read().get("leases", {}):
                group.release(token)
                return "released"
            return "already-absent"
        except Exception as exc:
            return f"release-error: {exc}"

    def recover(self, session_uuid: str | None = None) -> list[dict]:
        records = ([self.store.read(session_uuid)] if session_uuid is not None
                   else self.store.list())
        results = []
        for record in records:
            current = self.show(record["session_uuid"])
            if current["worker_live"]:
                results.append({"session_uuid": record["session_uuid"],
                                "action": "kept-live"})
                continue
            if current["child_live"]:
                self.store.update(
                    record["session_uuid"], state="unresolved",
                    error="worker is gone but a host exec proxy is still live; "
                          "refusing to release or stop anything",
                    updated_at=self.service.clock())
                results.append({"session_uuid": record["session_uuid"],
                                "action": "unresolved-proxy-alive"})
                continue
            if record["state"] in {"exited", "retired", "failed", "interrupted",
                                   "unresolved"} and not record.get("lease_token"):
                results.append({"session_uuid": record["session_uuid"],
                                "action": "already-settled"})
                continue
            if (record["state"] == "planned" and self._guest_from_record(record) is None
                    and not record.get("lease_token")):
                # A plan that never acquired a lease or spawned anything cannot
                # have a live guest. It is failed, not silently recovered.
                self.store.update(
                    record["session_uuid"], state="failed",
                    error="project session was planned but never started",
                    updated_at=self.service.clock())
                results.append({"session_uuid": record["session_uuid"],
                                "action": "failed-planned"})
                continue
            guest_state = current["guest_state"]
            if guest_state in {"alive", "unknown", "unsupported"}:
                reason = (
                    "guest execution is still alive" if guest_state == "alive"
                    else "execution ended cannot be proven from the guest "
                         "(dead host proxy is not proof); keeping the lease unresolved")
                self.store.update(
                    record["session_uuid"], state="unresolved", error=reason,
                    guest_state=guest_state,
                    updated_at=self.service.clock())
                results.append({"session_uuid": record["session_uuid"],
                                "action": "unresolved-guest-" + guest_state})
                continue
            if (self._guest_from_record(record) is None
                    and record["state"] in {"planned", "starting", "running"}
                    and record["state"] != "planned"):
                self.store.update(
                    record["session_uuid"], state="unresolved",
                    error="no guest pid/start identity was recorded; cannot prove "
                          "the execution ended, so the lease is kept",
                    updated_at=self.service.clock())
                results.append({"session_uuid": record["session_uuid"],
                                "action": "unresolved-guest-identity"})
                continue
            release = self._release_dead_lease(record)
            if release not in {None, "released", "already-absent"}:
                self.store.update(
                    record["session_uuid"], state="unresolved", error=release,
                    updated_at=self.service.clock())
                results.append({"session_uuid": record["session_uuid"],
                                "action": "unresolved-lease-release"})
                continue
            if record["state"] in {"running", "starting", "unresolved"}:
                new_state = "interrupted"
            elif record["state"] == "planned":
                new_state = "failed"
            else:
                new_state = record["state"]
            self.store.update(
                record["session_uuid"], state=new_state, lease_token=None,
                child_pid=None, child_stamp=None,
                error="recovered after the worker was no longer live and the "
                      "guest identity was confirmed gone",
                updated_at=self.service.clock())
            results.append({"session_uuid": record["session_uuid"],
                            "action": "recovered", "state": new_state,
                            "lease": release})
        return results

    def cancel(self, session_uuid: str, signum=signal.SIGTERM) -> dict:
        """Cancel one execution by targeting its worker or exact guest identity."""
        if signum not in {signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGKILL}:
            raise UnifierError("session cancel supports INT, TERM, HUP and KILL")
        current = self.show(session_uuid)
        record = self.store.read(session_uuid)
        if current["worker_live"] and record.get("worker_pid") != os.getpid():
            try:
                os.kill(record["worker_pid"], signum)
            except OSError as exc:
                raise UnifierError(f"cannot signal the session worker: {exc}") from exc
            return {"session_uuid": session_uuid, "action": "signalled-worker",
                    "signal": int(signum)}
        guest = self._guest_from_record(record)
        if guest is None:
            if record.get("lease_token") or record["state"] in {"running", "starting"}:
                raise UnifierError(
                    "session has no guest pid/start identity; refusing to claim "
                    "cancellation or release its unresolved lease")
            return {"session_uuid": session_uuid, "action": "already-settled"}
        container, project, environment_id, running = self._verified_instance(record)
        user = record.get("session_user")
        # A stopped exact instance has no guest left to signal. Never signal a
        # replacement, even if it reused the same name and guest PID/start.
        status = (self.service.runtime.signal_guest(
            container, guest, signum, project=project,
            env_id=environment_id, user=user) if running else "gone")
        if not running:
            confirmed = True
        elif status == "gone":
            confirmed = self.service.runtime.cleanup_guest(
                container, guest, project=project,
                env_id=environment_id, user=user) == "gone"
        else:
            deadline = time.monotonic() + SIGNAL_GRACE_SECONDS
            confirmed = False
            while time.monotonic() < deadline:
                if self.service.runtime.probe_guest(
                        container, guest, project=project,
                        env_id=environment_id, user=user) == "gone":
                    confirmed = True
                    break
                time.sleep(0.1)
            if not confirmed and signum != signal.SIGKILL:
                self.service.runtime.signal_guest(
                    container, guest, signal.SIGKILL,
                    project=project, env_id=environment_id, user=user)
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    if self.service.runtime.probe_guest(
                            container, guest,
                            project=project, env_id=environment_id, user=user) == "gone":
                        confirmed = True
                        break
                    time.sleep(0.1)
            if confirmed:
                confirmed = self.service.runtime.cleanup_guest(
                    container, guest, project=project,
                    env_id=environment_id, user=user) == "gone"
        if not confirmed:
            self.store.update(
                session_uuid, state="unresolved",
                error="guest cancellation was not confirmed; keeping the lease",
                guest_state="unknown", updated_at=self.service.clock())
            return {"session_uuid": session_uuid, "action": "unresolved",
                    "signal": int(signum)}
        release = self._release_dead_lease(record)
        self.store.update(
            session_uuid, state="interrupted", lease_token=None,
            child_pid=None, child_stamp=None, guest_state="gone",
            error=None, updated_at=self.service.clock())
        return {"session_uuid": session_uuid, "action": "cancelled",
                "signal": int(signum), "lease": release}

    def retire(self, session_uuid: str) -> dict:
        current = self.show(session_uuid)
        if current["worker_live"] or current["child_live"]:
            raise UnifierError(
                f"project session {session_uuid} is live; stop its execution explicitly "
                "before retiring the record (the environment is never deleted here)")
        if current.get("guest_state") in {"alive", "unknown", "unsupported"}:
            raise UnifierError(
                f"project session {session_uuid} has an unproven guest execution; "
                "recover it before retiring")
        record = self.store.read(session_uuid)
        release = self._release_dead_lease(record)
        if isinstance(release, str) and release.startswith("lease-without"):
            raise UnifierError(
                f"project session {session_uuid} has an unowned lease record; recover first")
        return self.store.update(
            session_uuid, state="retired", lease_token=None, child_pid=None,
            child_stamp=None, retired_at=self.service.clock(),
            updated_at=self.service.clock())

    def validate_app_project(self, project_identifier: str, app_id: str) -> dict:
        """Check that an existing app still belongs to the accepted project."""
        project = self.projects.show(project_identifier)
        launch = self.service.show_app(app_id)
        if launch.get("environment_id") != project["environment_binding"]["environment_id"]:
            raise UnifierError("application belongs to a different environment than the project")
        self.service.validate_app_binding(app_id)
        if launch.get("environment_binding") != project["environment_binding"]:
            raise UnifierError("application environment instance differs from the project")
        self.prepare(project["project_uuid"], cwd=project.get("default_save_root"),
                     argv=list(launch.get("argv") or ["/bin/true"]), env_keys=[])
        return {"project_uuid": project["project_uuid"], "app_id": app_id}


def assert_environment_not_live(root: Path, env_id: str, service=None) -> None:
    """Guard destructive environment removal from a real project session.

    Without a runtime this fails closed for any recorded guest identity or
    active lease: a dead host pid is not proof that a guest stopped.
    """
    store = SessionStore(Path(root))
    for record in store.list():
        if record.get("environment_id") != env_id:
            continue
        worker_live = (record.get("worker_pid") is not None
                       and record.get("worker_stamp") is not None
                       and process_stamp(record["worker_pid"]) == record["worker_stamp"])
        child_live = (record.get("child_pid") is not None
                      and record.get("child_stamp") is not None
                      and process_stamp(record["child_pid"]) == record["child_stamp"])
        terminal_done = (
            record.get("state") in {"exited", "failed", "retired", "interrupted"}
            and record.get("guest_state") == "gone"
            and not record.get("lease_token"))
        if (worker_live or child_live) and not terminal_done:
            raise UnifierError(
                f"refusing to remove environment {env_id}: project session "
                f"{record['session_uuid']} is still live; stop it explicitly first")
        if terminal_done:
            # Historical confirmation from the worker; unlike a dead proxy,
            # this remains evidence even when the environment is now stopped.
            continue
        if record.get("guest_pid") is None and not record.get("lease_token"):
            continue
        if service is None or not hasattr(service.runtime, "probe_guest"):
            raise UnifierError(
                f"refusing to remove environment {env_id}: project session "
                f"{record['session_uuid']} has no provable guest termination")
        manager = ProjectSessions(Path(root), service)
        guest = manager._guest_from_record(record)
        if guest is None:
            raise UnifierError(
                f"refusing to remove environment {env_id}: project session "
                f"{record['session_uuid']} lacks a guest pid/start identity")
        environment = service.store.load(record["environment_id"])
        container = environment["runtime"]["container_name"]
        status = service.runtime.probe_guest(
            container, guest, project=environment["runtime"]["project"],
            env_id=record["environment_id"], user=record.get("session_user"))
        if status != "gone":
            raise UnifierError(
                f"refusing to remove environment {env_id}: project session "
                f"{record['session_uuid']} guest execution is {status}; "
                "stop it explicitly first")
