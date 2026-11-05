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

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import signal
import socket
import stat
import struct
import sys
import threading
import time
import uuid

from unifier.errors import UnifierError
from unifier.identity import binding_for, validate_binding
from unifier.json_records import JsonRecordCollection
from unifier.project_sessions import ProjectSessions, validate_env_keys


PROTOCOL_VERSION = 1
SCHEMA_VERSION = 1
LEDGER_NAME = "agent-requests"
DEFAULT_TOKEN_ENV = "UNIFIER_AGENT_TOKEN"
DEFAULT_REQUEST_TTL_SECONDS = 300
MAX_REQUEST_TTL_SECONDS = 86400
DEFAULT_FRAME_LIMIT = 65536
MIN_FRAME_LIMIT = 512
MIN_BODY_BYTES = 1
MAX_POLICY_BYTES = 1 << 20
MAX_PATH_CHARS = 4096
DEFAULT_HANDSHAKE_TIMEOUT = 5.0
DEFAULT_WRITE_TIMEOUT = 10.0
DEFAULT_ACCEPT_POLL = 0.2
DEFAULT_RUN_GRACE_SECONDS = 5.0
MIN_RUN_TIMEOUT_SECONDS = 1
MAX_RUN_TIMEOUT_SECONDS = 3600
# A claim must outlive a legitimate run: the timeout, the TERM/KILL escalation
# inside ProjectSessions.cancel and the settle grace.  Anything shorter would
# let an in-flight claim expire and freeze the ledger for no reason.
RUN_OVERHEAD_SECONDS = 20.0
MAX_HOST_FILE_BYTES = 8192
MAX_LIST_ITEMS = 32
MAX_VALUE_CHARS = 256
ERROR_MESSAGE_CHARS = 400
REVOKE_FILE_BYTES = 64 * 1024
PEERCRED_SIZE = struct.calcsize("iii")
MAX_TOKEN_CHARS = 256
OUTPUT_REFERENCE_DEFAULT = "host-broker-stream"
# An AF_UNIX sockaddr only carries SUN_PATH bytes; on Linux a full path longer
# than this cannot be passed to bind()/connect() at all.  Long repository paths
# blow straight through it, so the socket is addressed through a pinned
# directory FD instead (see ``socket_address_via_fd``).
UNIX_PATH_LIMIT = 107
PROC_SELF_FD_ROOT = "/proc/self/fd"
# The grant itself has an absolute deadline, separate from any claim TTL.
MAX_EPOCH_SECONDS = 4102444800          # 2100-01-01T00:00:00Z
REDACTED_MARKER = "[redacted]"
ACTION_TYPES = ("host-info", "project-run")
REQUEST_FIELDS = ("protocol_version", "action_id", "request_uuid", "token")
POLICY_FIELDS = (
    "schema_version", "token", "token_env", "socket", "origin", "actions",
    "revoke_file", "request_ttl_seconds", "frame_limit", "state_root",
    "output_reference", "expires_at",
)
ORIGIN_FIELDS = ("session_uuid", "environment_binding")
ACTION_FIELDS = (
    "action_id", "type", "project_uuid", "environment_binding", "argv", "cwd",
    "timeout_seconds", "request_ttl_seconds", "scope_sha256",
)
REQUEST_STATES = ("claimed", "completed", "failed", "unresolved")
RESPONSE_STATUSES = ("ok", "rejected", "failed", "timeout", "unresolved")
RESPONSE_FIELDS = (
    "protocol_version", "request_uuid", "action_id", "status", "receipt",
    "error",
)

ACTION_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
TOKEN_RE = re.compile(r"[0-9a-f]{64,256}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")
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


class BridgeError(UnifierError):
    """A refusal or transport fault the bridge can report as a code + message."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


# ---------------------------------------------------------------------------
# strict JSON and scalar helpers
# ---------------------------------------------------------------------------
def _reject_constant(name: str):
    raise BridgeError("bad-json", f"JSON constant {name} is not a finite number")


def _reject_duplicates(pairs):
    seen = set()
    for key, _value in pairs:
        if key in seen:
            raise BridgeError("bad-json", f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def strict_loads(text: str):
    """Parse JSON refusing NaN/Infinity and duplicate keys."""
    return json.loads(text, parse_constant=_reject_constant,
                      object_pairs_hook=_reject_duplicates)


def strict_dumps(payload) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True,
                      allow_nan=False, separators=(",", ":"))


def _copy_json(payload):
    return json.loads(strict_dumps(payload))


def _text(value, label: str, code: str, *, pattern=None,
          max_chars: int = MAX_VALUE_CHARS) -> str:
    # A bool is not a str, so ``true`` can never masquerade as text here;
    # numeric validators reject bool separately for the same reason.
    if not isinstance(value, str):
        raise BridgeError(code, f"{label} must be a string")
    if not value or len(value) > max_chars:
        raise BridgeError(code, f"{label} must be 1..{max_chars} characters")
    if "\x00" in value or "\n" in value or "\r" in value:
        raise BridgeError(code, f"{label} must not contain a NUL or newline")
    if pattern is not None and pattern.fullmatch(value) is None:
        raise BridgeError(code, f"{label} has an invalid form")
    return value


def _int(value, label: str, code: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeError(code, f"{label} must be an integer (not a boolean)")
    if value < low or value > high:
        raise BridgeError(code, f"{label} must be {low}..{high}")
    return int(value)


def canonical_uuid(value, label: str, code: str) -> str:
    """Accept only the canonical lowercase hyphenated UUID spelling."""
    if not isinstance(value, str) or len(value) != 36:
        raise BridgeError(code, f"{label} must be a canonical lowercase UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise BridgeError(code, f"{label} is not a UUID") from exc
    if str(parsed) != value:
        raise BridgeError(
            code, f"{label} must be the canonical lowercase UUID spelling")
    return value


def checked_path(value, label: str, code: str) -> Path:
    """A normalized absolute path whose every component is symlink-free."""
    text = _text(value, label, code, max_chars=MAX_PATH_CHARS)
    if not text.startswith("/"):
        raise BridgeError(code, f"{label} must be an absolute path")
    path = Path(text)
    if str(path) != text or ".." in path.parts:
        raise BridgeError(code, f"{label} must be a normalized absolute path")
    if path.is_symlink():
        raise BridgeError(code, f"{label} must not be a symbolic link")
    parent = str(path.parent)
    if os.path.realpath(parent) != parent:
        raise BridgeError(code, f"{label} resolves through a symbolic link")
    return path


def open_directory_fd(path, label: str, code: str) -> int:
    """Open one directory and prove the FD really points at that directory.

    The FD is what lets a socket be addressed as ``/proc/self/fd/<fd>/<name>``
    instead of its full path: a repository checked out under a deep, non-ASCII
    path blows through the 107-byte ``sockaddr_un`` limit, while this address
    stays short whatever the real path is.  ``O_NOFOLLOW`` refuses an alias, and
    the ``fstat``/``lstat`` comparison refuses a directory that moved underneath
    the open.  Nothing here changes the process cwd, which would not be
    thread-safe in a broker that also runs executions in worker threads.
    """
    target = Path(path)
    if not target.is_absolute() or ".." in target.parts:
        raise BridgeError(code, f"{label} must be an absolute normalized directory")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW
    fd = os.open("/", flags)
    try:
        # Anchor each component: a symlink swapped into an ancestor cannot
        # redirect a host-side bind/chmod outside the explicitly shared tree.
        for part in target.parts[1:]:
            next_fd = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        return fd
    except OSError as exc:
        os.close(fd)
        detail = ("is not a directory" if isinstance(exc, NotADirectoryError)
                  else "is unavailable" if isinstance(exc, FileNotFoundError)
                  else "cannot be opened without following aliases")
        raise BridgeError(code, f"{label} {target} {detail}") from exc


def socket_address_via_fd(fd: int, name: str, code: str = "bad-socket") -> str:
    """Address one entry inside a pinned directory FD within the sockaddr limit."""
    if (not isinstance(name, str) or not name or "/" in name
            or "\x00" in name or "\n" in name or len(name) > 255):
        raise BridgeError(code, f"{name!r} is not a plain entry name")
    if isinstance(fd, bool) or not isinstance(fd, int) or fd < 0:
        raise BridgeError(code, f"directory fd {fd!r} is not usable")
    address = f"{PROC_SELF_FD_ROOT}/{fd}/{name}"
    if len(address.encode("utf-8")) > UNIX_PATH_LIMIT:
        raise BridgeError(code,
                          f"the anchored address {address!r} is above the "
                          f"{UNIX_PATH_LIMIT}-byte socket limit")
    return address


def redact_token(value, token: str):
    """Scrub one credential from anything the bridge is about to hand out.

    A guest may smuggle the token into a field *name* (an unknown-field refusal
    lists the names it got) or into a message it provoked, so the scrub is
    recursive over keys and strings.  Only this policy's own token value is
    rewritten: ordinary identity fields keep their content.
    """
    if not isinstance(token, str) or not token:
        return value
    if isinstance(value, str):
        return REDACTED_MARKER if value == token else value.replace(token, REDACTED_MARKER)
    if isinstance(value, bool):
        return value
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            safe_key = (REDACTED_MARKER if isinstance(key, str) and token in key
                        else key)
            redacted[safe_key] = redact_token(item, token)
        return redacted
    if isinstance(value, (list, tuple)):
        return [redact_token(item, token) for item in value]
    return value


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


def token_environment(policy) -> dict[str, str]:
    """The single environment entry the host passes into the guest."""
    return {policy.token_env: policy.token}


# ---------------------------------------------------------------------------
# host-issued policy
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Action:
    action_id: str
    type: str
    request_ttl_seconds: int
    project_uuid: str | None = None
    environment_binding: dict | None = None
    argv: tuple = ()
    cwd: str | None = None
    timeout_seconds: int | None = None
    scope_sha256: str | None = None


@dataclass(frozen=True)
class BridgePolicy:
    token: str
    token_env: str
    socket_path: Path
    origin_session_uuid: str
    actions: tuple
    expires_at: float
    origin_binding: dict | None = None
    revoke_file: Path | None = None
    state_root: Path | None = None
    output_reference: str = OUTPUT_REFERENCE_DEFAULT
    request_ttl_seconds: int = DEFAULT_REQUEST_TTL_SECONDS
    frame_limit: int = DEFAULT_FRAME_LIMIT

    def action(self, action_id: str):
        for item in self.actions:
            if item.action_id == action_id:
                return item
        return None


def _binding_object(value, label: str) -> dict:
    try:
        binding = validate_binding(dict(value) if isinstance(value, dict) else value)
    except (UnifierError, TypeError) as exc:
        raise BridgeError("bad-policy", f"{label} is not a launch binding: {exc}") from exc
    return {
        "environment_id": binding["environment_id"],
        "environment_uuid": binding["environment_uuid"],
        "generation": binding["generation"],
        "container_id": binding["container_id"],
    }


def _validate_token(value) -> str:
    if not isinstance(value, str):
        raise BridgeError("bad-policy", "token must be a string")
    if len(value) > MAX_TOKEN_CHARS:
        raise BridgeError("bad-policy", f"token must be at most {MAX_TOKEN_CHARS} characters")
    if TOKEN_RE.fullmatch(value) is None or len(value) % 2:
        raise BridgeError(
            "bad-policy", "token must be 64..256 lowercase hex characters "
            "(>= 32 random bytes, e.g. secrets.token_hex(32))")
    return value


def _validate_token_env(value):
    try:
        keys = validate_env_keys([value])
    except UnifierError as exc:
        raise BridgeError("bad-policy", f"token_env is not usable: {exc}") from exc
    if not keys or ENV_NAME_RE.fullmatch(keys[0]) is None:
        raise BridgeError("bad-policy", "token_env must be a plain variable name")
    return keys[0]


def _expiry_at(value, label: str, code: str) -> float:
    """An absolute authorisation deadline: finite positive Unix seconds.

    This is the deadline of the *grant*, not of a request: the request-level
    ``request_ttl_seconds`` bounds how long one open ledger claim may stay open,
    and the two are deliberately separate.  ``true`` and ``NaN``/``inf`` are not
    dates, so they are refused rather than coerced.
    """
    if value is None:
        raise BridgeError(code, f"{label} is required: a grant must expire")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BridgeError(code,
                          f"{label} must be a Unix timestamp in seconds, not "
                          f"{type(value).__name__}")
    seconds = float(value)
    if seconds != seconds or seconds in (float("inf"), float("-inf")):
        raise BridgeError(code, f"{label} must be a finite Unix timestamp")
    if seconds <= 0 or seconds > MAX_EPOCH_SECONDS:
        raise BridgeError(code,
                          f"{label} must be a positive Unix timestamp at or below "
                          f"{MAX_EPOCH_SECONDS}")
    return seconds


def _policy_clock_now(clock=time.time) -> float:
    now = float(clock())
    if now != now or now in (float("inf"), float("-inf")):
        raise BridgeError("host-state", "the host clock is not usable")
    return now


def _parse_action(raw) -> Action:
    if not isinstance(raw, dict):
        raise BridgeError("bad-policy", "each action must be a JSON object")
    unknown = sorted(set(raw) - set(ACTION_FIELDS))
    if unknown:
        raise BridgeError("bad-policy",
                          f"action has fields the guest cannot use: {', '.join(unknown)}")
    action_id = _text(raw.get("action_id"), "action_id", "bad-policy",
                      pattern=ACTION_ID_RE)
    kind = raw.get("type")
    if kind not in ACTION_TYPES:
        raise BridgeError("bad-policy",
                          f"action {action_id} type must be one of: {', '.join(ACTION_TYPES)}")
    ttl = raw.get("request_ttl_seconds", DEFAULT_REQUEST_TTL_SECONDS)
    ttl = _int(ttl, f"action {action_id} request_ttl_seconds", "bad-policy",
               low=1, high=MAX_REQUEST_TTL_SECONDS)
    extra = sorted(set(raw) - {"action_id", "type", "request_ttl_seconds"})
    if kind == "host-info":
        if extra:
            raise BridgeError(
                "bad-policy",
                f"host-info action {action_id} takes no {', '.join(extra)}")
        return Action(action_id=action_id, type=kind, request_ttl_seconds=ttl)
    for key in ("project_uuid", "environment_binding", "argv", "cwd", "timeout_seconds"):
        if key not in raw:
            raise BridgeError("bad-policy",
                              f"project-run action {action_id} needs {key}")
    project_uuid = canonical_uuid(raw["project_uuid"],
                                  f"action {action_id} project_uuid", "bad-policy")
    binding = _binding_object(raw["environment_binding"],
                              f"action {action_id} environment_binding")
    argv = raw["argv"]
    if not isinstance(argv, list) or not argv:
        raise BridgeError("bad-policy",
                          f"project-run action {action_id} argv must be a non-empty array")
    frozen_argv = tuple(_text(item, f"action {action_id} argv entry", "bad-policy",
                              max_chars=4096) for item in argv)
    if any(item == "-" or item.startswith("--") for item in frozen_argv[:1]):
        raise BridgeError("bad-policy",
                          f"project-run action {action_id} argv must start with a program, not a flag")
    cwd = checked_path(raw["cwd"], f"action {action_id} cwd", "bad-policy")
    timeout = _int(raw["timeout_seconds"], f"action {action_id} timeout_seconds",
                   "bad-policy", low=MIN_RUN_TIMEOUT_SECONDS,
                   high=MAX_RUN_TIMEOUT_SECONDS)
    scope = raw.get("scope_sha256")
    if scope is not None:
        scope = _text(scope, f"action {action_id} scope_sha256", "bad-policy",
                      pattern=SHA256_RE)
    if ttl < timeout + RUN_OVERHEAD_SECONDS:
        raise BridgeError(
            "bad-policy",
            f"project-run action {action_id} request_ttl_seconds must exceed "
            f"timeout_seconds by at least {int(RUN_OVERHEAD_SECONDS)}s to cover "
            "TERM/KILL escalation and settlement")
    return Action(action_id=action_id, type=kind, request_ttl_seconds=ttl,
                  project_uuid=project_uuid, environment_binding=binding,
                  argv=frozen_argv, cwd=str(cwd), timeout_seconds=timeout,
                  scope_sha256=scope)


def parse_policy(raw) -> BridgePolicy:
    """Validate one parsed policy document into frozen host rules."""
    if not isinstance(raw, dict):
        raise BridgeError("bad-policy", "policy must be a JSON object")
    unknown = sorted(set(raw) - set(POLICY_FIELDS))
    if unknown:
        raise BridgeError("bad-policy", f"policy has unknown fields: {', '.join(unknown)}")
    version = raw.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise BridgeError("bad-policy",
                          f"policy schema_version {version!r} is not supported")
    token = _validate_token(raw.get("token"))
    token_env = _validate_token_env(raw.get("token_env", DEFAULT_TOKEN_ENV))
    socket_path = checked_path(raw.get("socket"), "socket", "bad-policy")
    origin = raw.get("origin")
    if not isinstance(origin, dict):
        raise BridgeError("bad-policy", "origin must be a JSON object")
    unknown_origin = sorted(set(origin) - set(ORIGIN_FIELDS))
    if unknown_origin:
        raise BridgeError("bad-policy", f"origin has unknown fields: {', '.join(unknown_origin)}")
    origin_session_uuid = canonical_uuid(origin.get("session_uuid"),
                                         "origin session_uuid", "bad-policy")
    # The origin is who may ask; without a pinned environment instance the
    # record alone would not say which container instance was accepted.
    if origin.get("environment_binding") is None:
        raise BridgeError(
            "bad-policy",
            "origin environment_binding is required: the host pins the exact "
            "environment instance the grant was issued for")
    origin_binding = _binding_object(origin["environment_binding"],
                                     "origin environment_binding")
    expires_at = _expiry_at(raw.get("expires_at"), "policy expires_at", "bad-policy")
    actions = raw.get("actions")
    if not isinstance(actions, list) or not actions:
        raise BridgeError("bad-policy", "policy actions must be a non-empty array")
    parsed_actions = tuple(_parse_action(item) for item in actions)
    seen = set()
    for item in parsed_actions:
        if item.action_id in seen:
            raise BridgeError("bad-policy", f"duplicate action_id: {item.action_id}")
        seen.add(item.action_id)
    revoke_file = None
    if raw.get("revoke_file") is not None:
        revoke_file = checked_path(raw["revoke_file"], "revoke_file", "bad-policy")
    state_root = None
    if raw.get("state_root") is not None:
        state_root = checked_path(raw["state_root"], "state_root", "bad-policy")
    ttl = _int(raw.get("request_ttl_seconds", DEFAULT_REQUEST_TTL_SECONDS),
               "request_ttl_seconds", "bad-policy", low=1, high=MAX_REQUEST_TTL_SECONDS)
    frame_limit = _int(raw.get("frame_limit", DEFAULT_FRAME_LIMIT), "frame_limit",
                       "bad-policy", low=MIN_FRAME_LIMIT, high=DEFAULT_FRAME_LIMIT)
    output_reference = _text(raw.get("output_reference", OUTPUT_REFERENCE_DEFAULT),
                             "output_reference", "bad-policy", max_chars=MAX_PATH_CHARS)
    if output_reference != OUTPUT_REFERENCE_DEFAULT:
        checked_path(output_reference, "output_reference", "bad-policy")
    return BridgePolicy(
        token=token, token_env=token_env, socket_path=socket_path,
        origin_session_uuid=origin_session_uuid, origin_binding=origin_binding,
        actions=parsed_actions, expires_at=expires_at, revoke_file=revoke_file,
        state_root=state_root, output_reference=output_reference,
        request_ttl_seconds=ttl, frame_limit=frame_limit)


def _require_private_directory(path: Path, label: str) -> None:
    observed = os.stat(str(path))
    if not stat.S_ISDIR(observed.st_mode):
        raise BridgeError("bad-policy", f"{label} is not a directory")
    if stat.S_IMODE(observed.st_mode) & 0o077:
        raise BridgeError("bad-policy",
                          f"{label} must not be accessible to group or other")
    if observed.st_uid != os.getuid():
        raise BridgeError("bad-policy", f"{label} is not owned by the bridge user")


def load_policy(path) -> BridgePolicy:
    """Read the private host policy exactly once and freeze it.

    The file must be a regular, owner-only (0600) file inside a directory that
    is not reachable by group or other, and it must be symlink-free: a policy
    is the capability grant, so an alias or a world-readable copy is refused
    rather than trusted.
    """
    target = checked_path(str(path), "policy path", "bad-policy")
    try:
        observed = os.lstat(str(target))
    except OSError as exc:
        raise BridgeError("bad-policy", f"policy file is unavailable: {exc}") from exc
    if not stat.S_ISREG(observed.st_mode):
        raise BridgeError("bad-policy", "policy file must be a regular file")
    if stat.S_IMODE(observed.st_mode) != 0o600:
        raise BridgeError("bad-policy",
                          f"policy file must be mode 0600, found "
                          f"{stat.S_IMODE(observed.st_mode):04o}")
    if observed.st_uid != os.getuid():
        raise BridgeError("bad-policy", "policy file is not owned by the bridge user")
    _require_private_directory(target.parent, "policy directory")
    try:
        with open(str(target), "rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
    except OSError as exc:
        raise BridgeError("bad-policy", f"policy file is unreadable: {exc}") from exc
    if len(raw) > MAX_POLICY_BYTES:
        raise BridgeError("bad-policy", "policy file is larger than the read cap")
    try:
        document = strict_loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise BridgeError("bad-policy", f"policy file is not UTF-8: {exc}") from exc
    except (BridgeError, ValueError) as exc:
        raise BridgeError("bad-policy", f"policy file is not valid JSON: {exc}") from exc
    return parse_policy(document)


# ---------------------------------------------------------------------------
# request validation
# ---------------------------------------------------------------------------
def validate_request(payload) -> dict:
    """Validate one guest request.  Nothing here is trusted identity."""
    if not isinstance(payload, dict):
        raise BridgeError("bad-json", "a request must be a single JSON object")
    unknown = sorted(set(payload) - set(REQUEST_FIELDS))
    if unknown:
        raise BridgeError(
            "unknown-field",
            "a request may only carry protocol_version, action_id, request_uuid "
            "and token; got " + ", ".join(unknown))
    missing = [key for key in REQUEST_FIELDS if key not in payload]
    if missing:
        raise BridgeError("missing-field", "missing request field(s): " + ", ".join(missing))
    version = payload["protocol_version"]
    if (isinstance(version, bool) or not isinstance(version, int)
            or version != PROTOCOL_VERSION):
        raise BridgeError(
            "bad-protocol",
            f"protocol_version must be the integer {PROTOCOL_VERSION}, not "
            f"{type(version).__name__}")
    action_id = _text(payload["action_id"], "action_id", "bad-action-id",
                      pattern=ACTION_ID_RE)
    request_uuid = canonical_uuid(payload["request_uuid"], "request_uuid", "bad-uuid")
    token = payload["token"]
    if not isinstance(token, str):
        raise BridgeError("bad-token", "token must be a string")
    if len(token) > MAX_TOKEN_CHARS:
        raise BridgeError("bad-token", f"token must be at most {MAX_TOKEN_CHARS} characters")
    if TOKEN_RE.fullmatch(token) is None or len(token) % 2:
        raise BridgeError("bad-token",
                          "token must be 64..256 lowercase hex characters")
    return {"protocol_version": PROTOCOL_VERSION, "action_id": action_id,
            "request_uuid": request_uuid, "token": token}


MAX_RECEIPT_BYTES = 32768


def _json_safe(value, depth: int = 0) -> bool:
    if depth > 7:
        return False
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_json_safe(item, depth + 1) for item in value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return False
        return all(_json_safe(item, depth + 1) for item in value.values())
    return False


def validate_request_record(record) -> dict:
    """Validate one persisted request/receipt record."""
    if not isinstance(record, dict):
        raise UnifierError("request record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise UnifierError(f"request schema_version {version!r} is not supported")
    for key, label in (("request_uuid", "request"), ("origin_session_uuid", "origin")):
        if not is_canonical_uuid(record.get(key)):
            raise UnifierError(f"request {label} must be a canonical lowercase UUID")
    action_id = record.get("action_id")
    if not isinstance(action_id, str) or ACTION_ID_RE.fullmatch(action_id) is None:
        raise UnifierError("request action_id is invalid")
    state = record.get("state")
    if state not in REQUEST_STATES:
        raise UnifierError(f"unknown request state: {state!r}")
    for key in ("claimed_at", "expires_at"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UnifierError(f"request {key} must be a number")
    if record["expires_at"] <= record["claimed_at"]:
        raise UnifierError("request expires_at must be after claimed_at")
    finished = record.get("finished_at")
    if finished is not None and (isinstance(finished, bool)
                                 or not isinstance(finished, (int, float))):
        raise UnifierError("request finished_at must be a number or null")
    receipt = record.get("receipt")
    if receipt is not None and (not isinstance(receipt, dict) or not _json_safe(receipt)):
        raise UnifierError("request receipt must be a bounded JSON object")
    error = record.get("error")
    if error is not None and (not isinstance(error, str) or not error
                              or len(error) > ERROR_MESSAGE_CHARS + 3):
        raise UnifierError("request error must be a bounded string or null")
    return record


def _try_uuid(value):
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        return None


def is_canonical_uuid(value) -> bool:
    """True only for the canonical lowercase hyphenated UUID spelling."""
    parsed = _try_uuid(value)
    return parsed is not None and str(parsed) == value


# ---------------------------------------------------------------------------
# durable request ledger
# ---------------------------------------------------------------------------
class RequestLedger:
    """Durable claim + receipt store; the deduplication is the point.

    ``reserve`` writes the claim atomically before an action starts, so the
    same ``request_uuid`` is still refused after a crash or a restart.  A claim
    that is still open past its TTL means an execution whose end nobody can
    prove, so every later request fails closed until the host resolves the
    ledger; the same holds for an unreadable ledger.
    """

    def __init__(self, root: Path, *, name: str = LEDGER_NAME, clock=time.time):
        self.root = Path(root)
        self.collection = JsonRecordCollection(
            self.root, name, schema_version=SCHEMA_VERSION,
            validator=validate_request_record, unique_keys=("request_uuid",))
        self.clock = clock

    @property
    def path(self) -> Path:
        return self.collection.path

    def _read(self) -> list:
        try:
            return self.collection.read_unlocked()
        except UnifierError as exc:
            raise BridgeError("ledger-corrupt",
                              f"request ledger is unreadable: {exc}") from exc

    def records(self) -> list:
        with self.collection.locked(False):
            return self._read()

    def reserve(self, request_uuid: str, action_id: str, origin_session_uuid: str,
                *, ttl_seconds: int) -> dict:
        with self.collection.locked(True):
            records = self._read()
            now = float(self.clock())
            stale = sorted(item["request_uuid"] for item in records
                           if item["state"] == "claimed" and item["expires_at"] < now)
            if stale:
                raise BridgeError(
                    "expired-claim",
                    "request ledger holds an unresolved expired claim "
                    f"({', '.join(stale[:3])}); refusing to start anything else "
                    "until the host resolves it")
            if any(item["request_uuid"] == request_uuid for item in records):
                raise BridgeError("duplicate-request",
                                  f"request {request_uuid} was already received")
            claim = {
                "schema_version": SCHEMA_VERSION,
                "request_uuid": request_uuid,
                "action_id": action_id,
                "origin_session_uuid": origin_session_uuid,
                "state": "claimed",
                "claimed_at": now,
                "expires_at": now + float(ttl_seconds),
            }
            records.append(claim)
            try:
                validate_request_record(dict(claim))
                self.collection.write_unlocked(records)
            except (UnifierError, OSError) as exc:
                raise BridgeError("ledger-write",
                                  f"cannot reserve the request: {exc}") from exc
            return dict(claim)

    def settle(self, request_uuid: str, state: str, *, receipt=None,
               error: str | None = None) -> dict:
        if state not in REQUEST_STATES or state == "claimed":
            raise BridgeError("ledger-state", f"cannot settle a request as {state!r}")
        if receipt is not None and (not isinstance(receipt, dict)
                                    or not _json_safe(receipt)):
            raise BridgeError("ledger-state", "a receipt must be a bounded JSON object")
        if receipt is not None and len(strict_dumps(receipt).encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise BridgeError("oversized-receipt",
                              f"the receipt is above {MAX_RECEIPT_BYTES} bytes")
        with self.collection.locked(True):
            records = self._read()
            for index, existing in enumerate(records):
                if existing["request_uuid"] != request_uuid:
                    continue
                if existing["state"] != "claimed":
                    raise BridgeError(
                        "duplicate-request",
                        f"request {request_uuid} already settled as {existing['state']}")
                updated = dict(existing)
                updated["state"] = state
                updated["finished_at"] = float(self.clock())
                if receipt is not None:
                    updated["receipt"] = _copy_json(receipt)
                if error is not None:
                    trimmed = error[:ERROR_MESSAGE_CHARS]
                    updated["error"] = trimmed
                try:
                    validate_request_record(updated)
                    records[index] = updated
                    self.collection.write_unlocked(records)
                except (UnifierError, OSError) as exc:
                    raise BridgeError("ledger-write",
                                      f"cannot record the receipt: {exc}") from exc
                return updated
            raise BridgeError("ledger-lost",
                              f"request {request_uuid} is no longer in the ledger")


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
def response(request_uuid, action_id, status: str, *, receipt=None,
             error=None) -> dict:
    if status not in RESPONSE_STATUSES:
        raise BridgeError("bad-status", f"unknown response status {status!r}")
    document = {
        "protocol_version": PROTOCOL_VERSION,
        "request_uuid": request_uuid,
        "action_id": action_id,
        "status": status,
        "receipt": receipt,
        "error": error,
    }
    for key in RESPONSE_FIELDS:
        if key not in document:
            raise BridgeError("bad-status", f"response is missing {key}")
    return document


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
            _require_private_directory(parent, "socket directory")
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
                    peer_uid: int | None) -> tuple:
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
        thread.join(timeout)
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
        now = _policy_clock_now(self.clock)
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
                status, session = self._run_action(action, plan, origin, peer_uid)
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
                            receipt=_copy_json(receipt))

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
        self._stopping = False
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
# framing
# ---------------------------------------------------------------------------
def peer_uid(connection):
    """The connecting process' real uid where the platform can prove it.

    Linux proves it on a connected AF_UNIX stream socket via ``SO_PEERCRED``;
    that is a bound the guest cannot forge.  A platform without the option is
    not treated as a pass: the caller still has the 0700 parent directory, the
    0600 socket and the host-issued token, but the receipt honestly records
    ``peer_uid: null`` instead of inventing one.
    """
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        return None
    try:
        raw = connection.getsockopt(socket.SOL_SOCKET, option, PEERCRED_SIZE)
    except OSError:
        raise BridgeError("peer-uid",
                          "cannot verify the peer identity on this socket") from None
    if len(raw) != PEERCRED_SIZE:
        raise BridgeError("peer-uid", "the peer credential is malformed")
    _pid, uid, _gid = struct.unpack("iii", raw)
    return int(uid)


def _recv_exact(stream, count: int, deadline: float, label: str) -> bytes:
    chunks = []
    got = 0
    while got < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeError("frame-timeout",
                              f"timed out waiting for the {label} frame")
        stream.settimeout(min(remaining, 1.0))
        try:
            chunk = stream.recv(count - got)
        except socket.timeout:
            # One recv slice expiring is not a deadline: only the overall
            # deadline may fail the frame, so a slow peer still completes.
            continue
        except OSError as exc:
            raise BridgeError("frame-error",
                              f"cannot read the {label} frame: {type(exc).__name__}") from exc
        if not chunk:
            raise BridgeError("frame-truncated",
                              f"the peer closed the {label} frame early")
        chunks.append(chunk)
        got += len(chunk)
    return b"".join(chunks)


def read_frame(stream, *, limit: int, deadline: float, label: str = "request") -> bytes:
    """Read one length-prefixed frame, bounded by ``limit`` and the deadline."""
    header = _recv_exact(stream, 4, deadline, label)
    size = struct.unpack(">I", header)[0]
    if size < MIN_BODY_BYTES or size > limit:
        raise BridgeError("frame-too-large",
                          f"{label} frame of {size} bytes is outside "
                          f"{MIN_BODY_BYTES}..{limit}")
    return _recv_exact(stream, size, deadline, label)


def write_frame(stream, payload: bytes, *, deadline: float | None = None) -> None:
    if deadline is not None:
        remaining = max(0.1, deadline - time.monotonic())
        stream.settimeout(remaining)
    try:
        stream.sendall(struct.pack(">I", len(payload)) + payload)
    except socket.timeout:
        raise BridgeError("frame-timeout", "timed out writing the response frame") from None
    except OSError as exc:
        raise BridgeError("frame-error",
                          f"cannot write the response frame: {type(exc).__name__}") from exc


def request_frame(payload) -> bytes:
    """Encode one request as the bytes a client sends."""
    body = strict_dumps(payload).encode("utf-8")
    return struct.pack(">I", len(body)) + body


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def service_for_root(root: str | None):
    if root is None:
        from unifier import cli

        return cli.service_factory()
    from unifier.install import load_catalog
    from unifier.runtime import PodmanRuntime
    from unifier.service import EnvironmentService

    config, lock = load_catalog()
    return EnvironmentService(Path(root), PodmanRuntime(), config, lock)


def build_bridge(policy_path, sessions: ProjectSessions) -> AgentBridge:
    """Integration helper for a host CLI: policy file + live sessions."""
    return AgentBridge(load_policy(policy_path), sessions)


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m unifier.agent_bridge",
        description="host-issued request broker for one guest origin session")
    parser.add_argument("--policy", required=True,
                        help="private host-issued policy file (mode 0600)")
    parser.add_argument("--root", help="unifier state root (default: installed root)")
    parser.add_argument("--once", action="store_true",
                        help="serve exactly one request and exit")
    parser.add_argument("--max-requests", type=int,
                        help="serve at most this many requests and exit")
    args = parser.parse_args(argv)
    bridge = None
    try:
        policy = load_policy(args.policy)
        service = service_for_root(args.root)
        sessions = ProjectSessions(service.root, service)
        bridge = AgentBridge(policy, sessions)
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
        if bridge is not None:
            try:
                bridge.stop()
            finally:
                bridge.close()


if __name__ == "__main__":
    raise SystemExit(main())
