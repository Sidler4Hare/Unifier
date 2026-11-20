"""Load and validate the host-issued capability policy for an agent request bridge."""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat
import time

from unifier.bridge_protocol import (
    ACTION_ID_RE, DEFAULT_FRAME_LIMIT, MAX_TOKEN_CHARS,
    MIN_FRAME_LIMIT, TOKEN_RE, BridgeError, canonical_uuid, checked_text,
    strict_loads,
)
from unifier.errors import UnifierError
from unifier.identity import validate_binding
from unifier.project_sessions import validate_env_keys


SCHEMA_VERSION = 1
DEFAULT_TOKEN_ENV = "UNIFIER_AGENT_TOKEN"
DEFAULT_REQUEST_TTL_SECONDS = 300
MAX_REQUEST_TTL_SECONDS = 86400
MAX_POLICY_BYTES = 1 << 20
MAX_PATH_CHARS = 4096
MIN_RUN_TIMEOUT_SECONDS = 1
MAX_RUN_TIMEOUT_SECONDS = 3600
# A claim must outlive a legitimate run: the timeout, the TERM/KILL escalation
# inside ProjectSessions.cancel and the settle grace.  Anything shorter would
# let an in-flight claim expire and freeze the ledger for no reason.
RUN_OVERHEAD_SECONDS = 20.0
OUTPUT_REFERENCE_DEFAULT = "host-broker-stream"
# The grant itself has an absolute deadline, separate from any claim TTL.
MAX_EPOCH_SECONDS = 4102444800          # 2100-01-01T00:00:00Z
ACTION_TYPES = ("host-info", "project-run")
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

SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}\Z")


def _int(value, label: str, code: str, *, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise BridgeError(code, f"{label} must be an integer (not a boolean)")
    if value < low or value > high:
        raise BridgeError(code, f"{label} must be {low}..{high}")
    return int(value)


def checked_path(value, label: str, code: str) -> Path:
    """A normalized absolute path whose every component is symlink-free."""
    text = checked_text(value, label, code, max_chars=MAX_PATH_CHARS)
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


def policy_clock_now(clock=time.time) -> float:
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
    action_id = checked_text(raw.get("action_id"), "action_id", "bad-policy",
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
    frozen_argv = tuple(checked_text(item, f"action {action_id} argv entry", "bad-policy",
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
        scope = checked_text(scope, f"action {action_id} scope_sha256", "bad-policy",
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
    output_reference = checked_text(raw.get("output_reference", OUTPUT_REFERENCE_DEFAULT),
                             "output_reference", "bad-policy", max_chars=MAX_PATH_CHARS)
    if output_reference != OUTPUT_REFERENCE_DEFAULT:
        checked_path(output_reference, "output_reference", "bad-policy")
    return BridgePolicy(
        token=token, token_env=token_env, socket_path=socket_path,
        origin_session_uuid=origin_session_uuid, origin_binding=origin_binding,
        actions=parsed_actions, expires_at=expires_at, revoke_file=revoke_file,
        state_root=state_root, output_reference=output_reference,
        request_ttl_seconds=ttl, frame_limit=frame_limit)


def require_private_directory(path: Path, label: str) -> None:
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
    require_private_directory(target.parent, "policy directory")
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
