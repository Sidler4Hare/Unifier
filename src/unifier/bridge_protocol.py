"""Wire protocol and Unix socket helpers shared by the request broker and client."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import socket
import struct
import time
import uuid

from unifier.errors import UnifierError


PROTOCOL_VERSION = 1
DEFAULT_FRAME_LIMIT = 65536
MIN_FRAME_LIMIT = 512
MIN_BODY_BYTES = 1
MAX_VALUE_CHARS = 256
ERROR_MESSAGE_CHARS = 400
PEERCRED_SIZE = struct.calcsize("iii")
MAX_TOKEN_CHARS = 256

# An AF_UNIX sockaddr only carries SUN_PATH bytes; on Linux a full path longer
# than this cannot be passed to bind()/connect() at all.  Long repository paths
# blow straight through it, so the socket is addressed through a pinned
# directory FD instead (see ``socket_address_via_fd``).
UNIX_PATH_LIMIT = 107
PROC_SELF_FD_ROOT = "/proc/self/fd"
REDACTED_MARKER = "[redacted]"
REQUEST_FIELDS = ("protocol_version", "action_id", "request_uuid", "token")
RESPONSE_STATUSES = ("ok", "rejected", "failed", "timeout", "unresolved")
RESPONSE_FIELDS = (
    "protocol_version", "request_uuid", "action_id", "status", "receipt",
    "error",
)

ACTION_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}\Z")
TOKEN_RE = re.compile(r"[0-9a-f]{64,256}\Z")


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


def copy_json(payload):
    return json.loads(strict_dumps(payload))


def checked_text(value, label: str, code: str, *, pattern=None,
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
    action_id = checked_text(payload["action_id"], "action_id", "bad-action-id",
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


def validate_response(document, *, frame_bytes: int, frame_limit: int,
                      secret: str | None = None) -> dict:
    if not isinstance(document, dict):
        raise BridgeError("bad-response", "the response must be a JSON object")
    if frame_bytes > frame_limit:
        raise BridgeError("bad-response",
                          f"the response frame is {frame_bytes} bytes, above the "
                          f"{frame_limit}-byte limit")
    if secret:
        # A response we cannot read is still not allowed to carry our credential
        # back into logs: scrub it before any message quotes a field name.
        document = redact_token(document, secret)
    unknown = sorted(set(document) - set(RESPONSE_FIELDS))
    if unknown:
        raise BridgeError("bad-response",
                          "the response carries unexpected fields: " + ", ".join(unknown))
    version = document.get("protocol_version")
    if isinstance(version, bool) or version != PROTOCOL_VERSION:
        raise BridgeError("bad-response",
                          f"the response protocol_version is not {PROTOCOL_VERSION}")
    status = document.get("status")
    if status not in RESPONSE_STATUSES:
        raise BridgeError("bad-response", f"the response status {status!r} is unknown")
    error = document.get("error")
    if error is not None:
        if not isinstance(error, dict) or set(error) != {"code", "message"}:
            raise BridgeError("bad-response", "the response error must name a code and a message")
        for key in ("code", "message"):
            value = error[key]
            if not isinstance(value, str) or not value or len(value) > ERROR_MESSAGE_CHARS + 3:
                raise BridgeError("bad-response", f"the response error {key} is not a bounded string")
    receipt = document.get("receipt")
    if receipt is not None and not isinstance(receipt, dict):
        raise BridgeError("bad-response", "the response receipt must be an object or null")
    return document


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
