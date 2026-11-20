"""Guest-side client for the host-issued request bridge.

Usage from a guest project session::

    python -m unifier.bridge_client --socket /run/host-bridge/bridge.sock \
        --action host-info

The token is read from the environment (default ``UNIFIER_AGENT_TOKEN``),
never from argv: an argv entry is visible to any process through ``/proc``,
while the environment entry is what the host deliberately handed to this guest.
The token value is never printed, echoed or included in an error message.

A request may only name an ``action_id`` plus its own ``request_uuid``; there
is no way to pass a path, an argv array, a cwd or an environment variable, and
the server refuses anything else.

Exit codes:

* ``0`` -- the bridge answered ``ok``
* ``1`` -- usage or transport fault (no usable answer)
* ``2`` -- the bridge refused the request (``rejected``)
* ``3`` -- the request ran but the outcome is not a success
  (``failed`` / ``timeout`` / ``unresolved``)
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import socket
import stat
import sys
import time
import uuid

from unifier.bridge_protocol import (
    ACTION_ID_RE,
    DEFAULT_FRAME_LIMIT,
    ERROR_MESSAGE_CHARS,
    MAX_TOKEN_CHARS,
    MIN_FRAME_LIMIT,
    PROTOCOL_VERSION,
    RESPONSE_STATUSES,
    TOKEN_RE,
    BridgeError,
    canonical_uuid,
    open_directory_fd,
    read_frame,
    redact_token,
    socket_address_via_fd,
    strict_dumps,
    strict_loads,
    validate_response,
    write_frame,
)
from unifier.errors import UnifierError


DEFAULT_TIMEOUT = 60.0
MIN_TIMEOUT = 0.1
MAX_TIMEOUT = 7200.0
EXIT_OK = 0
EXIT_FAULT = 1
EXIT_REFUSED = 2
EXIT_UNSUCCESSFUL = 3
STATUS_EXIT = {
    "ok": EXIT_OK,
    "rejected": EXIT_REFUSED,
    "failed": EXIT_UNSUCCESSFUL,
    "timeout": EXIT_UNSUCCESSFUL,
    "unresolved": EXIT_UNSUCCESSFUL,
}


def _token_from_environment(name: str) -> str:
    if name not in os.environ:
        # Deliberately name the variable, never its value.
        raise BridgeError("missing-token",
                          f"environment variable {name} is not set")
    token = os.environ[name]
    if len(token) > MAX_TOKEN_CHARS:
        raise BridgeError("bad-token",
                          f"token must be at most {MAX_TOKEN_CHARS} characters")
    if TOKEN_RE.fullmatch(token) is None or len(token) % 2:
        raise BridgeError("bad-token",
                          f"{name} must hold 64..256 lowercase hex characters")
    return token


def checked_socket_path(value) -> Path:
    """Our own view of the socket: real socket, no alias, private directory."""
    if not isinstance(value, str) or not value.startswith("/"):
        raise BridgeError("bad-socket", "--socket must be an absolute path")
    if "\x00" in value or "\n" in value or len(value) > 4096:
        raise BridgeError("bad-socket", "--socket must be a plain absolute path")
    path = Path(value)
    if str(path) != value or ".." in path.parts:
        raise BridgeError("bad-socket", "--socket must be a normalized absolute path")
    try:
        observed = os.lstat(str(path))
    except OSError as exc:
        raise BridgeError("bad-socket", f"{path} is unavailable: {exc}") from exc
    if stat.S_ISLNK(observed.st_mode):
        raise BridgeError("bad-socket", f"{path} is a symbolic link; refusing an alias")
    if not stat.S_ISSOCK(observed.st_mode):
        raise BridgeError("bad-socket", f"{path} is not a socket")
    if stat.S_IMODE(observed.st_mode) != 0o600:
        mode = stat.S_IMODE(observed.st_mode)
        raise BridgeError("bad-socket",
                          f"{path} must be a 0600 socket, found {mode:04o}")
    try:
        parent = os.stat(str(path.parent))
    except OSError as exc:
        raise BridgeError("bad-socket", f"{path.parent} is unavailable: {exc}") from exc
    if not stat.S_ISDIR(parent.st_mode) or parent.st_mode & 0o077:
        raise BridgeError("bad-socket",
                          f"the socket directory of {path} is reachable by group or other")
    if parent.st_uid != os.geteuid() or observed.st_uid != os.geteuid():
        raise BridgeError("bad-socket", f"{path} is not owned by the current user")
    return path


@contextlib.contextmanager
def anchored_socket_address(path: Path, code: str = "bad-socket"):
    """A short AF_UNIX address for ``path``, anchored on a verified directory FD.

    ``checked_socket_path`` has already proved the real path is our 0600 socket
    in a private directory.  Connecting through ``/proc/self/fd/<fd>/<name>``
    instead of the full path keeps the request inside the 107-byte AF_UNIX limit
    when the repository lives under a long non-ASCII path; the FD pins the exact
    directory we validated, so the name cannot be re-pointed elsewhere on the
    way in.
    """
    fd = open_directory_fd(Path(path).parent, "socket directory", code)
    try:
        parent = os.fstat(fd)
        observed = os.stat(Path(path).name, dir_fd=fd, follow_symlinks=False)
        if (parent.st_uid != os.geteuid() or parent.st_mode & 0o077
                or observed.st_uid != os.geteuid()
                or not stat.S_ISSOCK(observed.st_mode)
                or stat.S_IMODE(observed.st_mode) != 0o600):
            raise BridgeError(code, "the opened socket directory or socket is no longer private")
        yield socket_address_via_fd(fd, Path(path).name, code)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass


def request(socket_path, action_id, *, token: str, request_uuid: str | None = None,
            timeout: float = DEFAULT_TIMEOUT, frame_limit: int = DEFAULT_FRAME_LIMIT,
            connect_timeout: float = 5.0) -> dict:
    """Send one request over the bridge socket and return the validated answer.

    The caller supplies only an ``action_id`` and (optionally) its own
    ``request_uuid``; the bridge owns every other decision.  ``timeout`` bounds
    the whole exchange, including a host-side run: choose it above the action's
    own timeout so the bridge, not the client, decides when a run is over.
    """
    if not isinstance(action_id, str) or ACTION_ID_RE.fullmatch(action_id) is None:
        raise BridgeError("bad-action-id", "action_id has an invalid form")
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool):
        raise BridgeError("bad-timeout", "timeout must be a number")
    if not MIN_TIMEOUT <= timeout <= MAX_TIMEOUT:
        raise BridgeError("bad-timeout",
                          f"timeout must be {MIN_TIMEOUT}..{MAX_TIMEOUT} seconds")
    if not isinstance(frame_limit, int) or isinstance(frame_limit, bool) or not (
            MIN_FRAME_LIMIT <= frame_limit <= DEFAULT_FRAME_LIMIT):
        raise BridgeError("bad-frame-limit",
                          f"frame_limit must be {MIN_FRAME_LIMIT}..{DEFAULT_FRAME_LIMIT} bytes")
    if request_uuid is None:
        request_uuid = str(uuid.uuid4())
    else:
        canonical_uuid(request_uuid, "request_uuid", "bad-uuid")
    path = checked_socket_path(socket_path)
    payload = {"protocol_version": PROTOCOL_VERSION, "action_id": action_id,
               "request_uuid": request_uuid, "token": token}
    deadline = time.monotonic() + float(timeout)
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        connection.settimeout(min(float(connect_timeout), max(0.1, timeout)))
        try:
            # The directory FD stays open until the connect has completed.
            with anchored_socket_address(path) as address:
                connection.connect(address)
        except socket.timeout:
            raise BridgeError("client-timeout",
                              "timed out connecting to the bridge socket") from None
        except BridgeError:
            raise
        except OSError as exc:
            raise BridgeError("connect",
                              f"cannot connect to {path}: {type(exc).__name__}") from exc
        write_frame(connection, strict_dumps(payload).encode("utf-8"), deadline=deadline)
        body = read_frame(connection, limit=frame_limit, deadline=deadline,
                          label="response")
    finally:
        try:
            connection.close()
        except OSError:
            pass
    try:
        document = strict_loads(body.decode("utf-8"))
    except UnicodeDecodeError:
        raise BridgeError("bad-response", "the response frame is not UTF-8") from None
    except BridgeError as exc:
        raise BridgeError("bad-response", f"the response is not valid JSON: {exc.message}") from exc
    except ValueError as exc:
        raise BridgeError("bad-response", f"the response is not valid JSON: {exc}") from exc
    answer = validate_response(document, frame_bytes=len(body), frame_limit=frame_limit,
                               secret=token)
    if answer.get("request_uuid") != request_uuid or answer.get("action_id") != action_id:
        raise BridgeError("bad-response", "the response does not match this request and action")
    return answer


def parser() -> argparse.ArgumentParser:
    client = argparse.ArgumentParser(
        prog="python -m unifier.bridge_client",
        description="ask the host bridge for exactly one pre-registered action")
    client.add_argument("--socket", required=True,
                        help="bridge UNIX socket path (0600, private directory)")
    client.add_argument("--action", required=True,
                        help="host-issued action_id to invoke")
    client.add_argument("--request-uuid",
                        help="durable request id (default: a fresh UUID; a replay is refused)")
    client.add_argument("--token-env", default="UNIFIER_AGENT_TOKEN",
                        help="environment variable holding the host-issued token")
    client.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"bound for the whole exchange ({MIN_TIMEOUT}..{MAX_TIMEOUT}s)")
    client.add_argument("--frame-limit", type=int, default=DEFAULT_FRAME_LIMIT,
                        help=f"response frame bound in bytes ({MIN_FRAME_LIMIT}..{DEFAULT_FRAME_LIMIT})")
    return client


def main(argv=None) -> int:
    try:
        args = parser().parse_args(argv)
        answer = request(
            args.socket, args.action, token=_token_from_environment(args.token_env),
            request_uuid=args.request_uuid, timeout=args.timeout,
            frame_limit=args.frame_limit)
    except (BridgeError, UnifierError) as exc:
        message = exc.message if isinstance(exc, BridgeError) else str(exc)
        print(json.dumps({"status": "error", "error": {
            "code": getattr(exc, "code", "usage"),
            "message": message[:ERROR_MESSAGE_CHARS]}},
            ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return EXIT_FAULT
    print(strict_dumps(answer))
    return STATUS_EXIT.get(answer["status"], EXIT_FAULT)


if __name__ == "__main__":
    raise SystemExit(main())
