"""Stopped-environment read-only view leases and lifecycle.

The view lease is separate from application leases. It is written under the
environment lifecycle lock before the FUSE mount starts, so start/remove cannot
race the view creation. The FUSE worker and the Podman storage broker are
tracked by exact pid+start-stamp; revoke never kills an arbitrary same-name
process and never uses lazy unmount.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import re
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import time
import uuid

from unifier.errors import UnifierError
from unifier.identity import binding_for, validate_binding
from unifier.json_records import JsonRecordCollection
from unifier.on_demand import process_stamp
from unifier.view_backend import build_id_map
from unifier.view_journal import read_journal, write_journal


SCHEMA_VERSION = 1
STATES = {"starting", "ready", "revoking", "failed", "stale", "needs-reconciliation"}
TERMINAL = {"failed"}
VIEW_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\Z")
PID_FIELDS = ("owner_pid", "worker_pid", "broker_pid", "broker_launcher_pid")
STAMP_FIELDS = ("owner_stamp", "worker_stamp", "broker_stamp", "broker_launcher_stamp")


def _canonical_uuid(value, label):
    if not isinstance(value, str):
        raise UnifierError(f"view {label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UnifierError(f"view {label} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise UnifierError(f"view {label} must be canonical lowercase UUID")
    return value


def _absolute(value, label):
    if (not isinstance(value, str) or not value.startswith("/")
            or "\x00" in value or "\n" in value):
        raise UnifierError(f"view {label} must be an absolute path")
    return value


def validate_view_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise UnifierError("view record must be an object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise UnifierError(f"view schema_version {version!r} is not supported")
    _canonical_uuid(record.get("view_uuid"), "view_uuid")
    if not isinstance(record.get("environment_id"), str) or not record["environment_id"]:
        raise UnifierError("view environment_id must be a non-empty string")
    validate_binding(record.get("environment_binding"))
    if record.get("state") not in STATES:
        raise UnifierError(f"unknown view state: {record.get('state')!r}")
    _absolute(record.get("mountpoint"), "mountpoint")
    _absolute(record.get("view_dir"), "view_dir")
    if record.get("readonly") is not True:
        raise UnifierError("view must be readonly")
    if record.get("backend") != "podman-overlay":
        raise UnifierError("view backend must be podman-overlay")
    for key in PID_FIELDS:
        value = record.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value <= 0):
            raise UnifierError(f"view {key} must be a positive integer or null")
    for key in STAMP_FIELDS:
        value = record.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise UnifierError(f"view {key} must be an integer or null")
    for key in ("control_socket", "storage_socket", "spec_path", "log_path",
                "runtime_dir", "storage_journal", "ready_path"):
        if record.get(key) is not None:
            _absolute(record[key], key)
    for key in ("runtime_dev", "runtime_ino"):
        if record.get(key) is not None and (
                isinstance(record[key], bool) or not isinstance(record[key], int)):
            raise UnifierError(f"view {key} must be an integer or null")
    if record.get("cleanup_errors") is not None and not isinstance(
            record["cleanup_errors"], list):
        raise UnifierError("view cleanup_errors must be a list")
    if record.get("storage_ref_state") not in {
            None, "none", "mounting", "mounted", "releasing", "released",
            "release-failed", "needs-reconciliation"}:
        raise UnifierError("view storage_ref_state is invalid")
    if record.get("storage_nonce") is not None and not isinstance(
            record["storage_nonce"], str):
        raise UnifierError("view storage_nonce must be a string")
    if not isinstance(record.get("created_at"), str) or not isinstance(record.get("updated_at"), str):
        raise UnifierError("view timestamps must be strings")
    if not isinstance(record.get("uid_map"), dict):
        raise UnifierError("view uid_map must be an object")
    for key in ("fuse_access_uid", "fuse_access_gid"):
        if key in record and (isinstance(record[key], bool) or not isinstance(record[key], int)):
            raise UnifierError(f"view {key} must be an integer")
    if record.get("namespace_caps") not in (None, "not-assumed"):
        raise UnifierError("view namespace_caps must be 'not-assumed'")
    return record


class ViewRegistry:
    def __init__(self, root: Path):
        self.collection = JsonRecordCollection(
            Path(root), "views", schema_version=SCHEMA_VERSION,
            validator=validate_view_record, unique_keys=("view_uuid",))

    def list(self) -> list[dict]:
        return self.collection.list()

    def add(self, record: dict) -> dict:
        validate_view_record(record)
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            if any(item["view_uuid"] == record["view_uuid"] for item in records):
                raise UnifierError("view UUID already registered")
            records.append(record)
            self.collection.write_unlocked(records)
        return record

    def update(self, record: dict) -> dict:
        validate_view_record(record)
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            for index, item in enumerate(records):
                if item["view_uuid"] == record["view_uuid"]:
                    records[index] = record
                    self.collection.write_unlocked(records)
                    return record
        raise UnifierError(f"view {record['view_uuid']} is not registered")

    def remove(self, view_uuid: str) -> dict:
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            found = next((item for item in records if item["view_uuid"] == view_uuid), None)
            if found is None:
                raise UnifierError(f"view {view_uuid} is not registered")
            self.collection.write_unlocked(
                [item for item in records if item["view_uuid"] != view_uuid])
            return found

    def find(self, identifier: str) -> list[dict]:
        if not isinstance(identifier, str) or not identifier:
            raise UnifierError("view identifier must be a non-empty string")
        return [item for item in self.list()
                if item["view_uuid"] == identifier or item["environment_id"] == identifier]

    def one(self, identifier: str) -> dict:
        found = self.find(identifier)
        if not found:
            raise UnifierError(f"view {identifier} is not registered")
        if len(found) > 1:
            raise UnifierError(
                f"view identifier {identifier} is ambiguous; use the view UUID")
        return found[0]


def is_mounted(path: str) -> bool:
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as stream:
            for line in stream:
                fields = line.split()
                if len(fields) < 5:
                    continue
                mountpoint = fields[4].replace("\\040", " ").replace("\\011", "\t")
                if mountpoint == path:
                    return True
    except OSError:
        return False
    return False


def _alive(pid, stamp) -> bool:
    return pid is not None and stamp is not None and process_stamp(pid) == stamp


def _journal_matches(journal, *, cid: str, view_uuid: str, nonce: str) -> bool:
    return (isinstance(journal, dict)
            and journal.get("cid") == cid
            and journal.get("view_uuid") == view_uuid
            and journal.get("nonce") == nonce)


def _journal_state(record: dict):
    journal = read_journal(record.get("storage_journal"))
    return journal.get("state") if isinstance(journal, dict) else None


def _storage_may_be_held(record: dict) -> bool:
    if record.get("storage_ref_state") in {
            "mounting", "mounted", "releasing", "release-failed",
            "needs-reconciliation"}:
        return True
    journal = read_journal(record.get("storage_journal"))
    return (isinstance(journal, dict)
            and journal.get("state") in {
                "mounting", "mounted", "releasing", "release-failed"})


def view_active(record: dict) -> bool:
    state = record.get("state")
    if state in {"revoking", "needs-reconciliation"}:
        return True
    if (record.get("broker_pid") is not None
            and _alive(record.get("broker_pid"), record.get("broker_stamp"))):
        return True
    if (record.get("worker_pid") is not None
            and _alive(record.get("worker_pid"), record.get("worker_stamp"))):
        return True
    if is_mounted(record.get("mountpoint", "")):
        return True
    if record.get("storage_ref_state") in {
            "mounting", "mounted", "releasing", "release-failed", "needs-reconciliation"}:
        return True
    if _journal_state(record) in {
            "mounting", "mounted", "releasing", "release-failed"}:
        return True
    if state == "starting":
        return _alive(record.get("owner_pid"), record.get("owner_stamp"))
    return False


def assert_no_active_views(root: Path, env_id: str) -> None:
    registry = ViewRegistry(root)
    for record in registry.list():
        if record["environment_id"] != env_id:
            continue
        if view_active(record):
            raise UnifierError(
                f"environment {env_id} has active read-only view "
                f"{record['view_uuid']} at {record['mountpoint']}; "
                "revoke it before start/remove")


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _secure_dir(path: Path, *, create: bool) -> None:
    if create:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        _fsync_dir(path.parent)
    observed = os.lstat(path)
    import stat as stat_module
    if not stat_module.S_ISDIR(observed.st_mode):
        raise UnifierError(f"view path is not a directory: {path}")
    if observed.st_uid != os.getuid() or (observed.st_mode & 0o077):
        raise UnifierError(
            f"view directory must be owned by this user with mode 0700: {path}")


def _unlink(path: str | None) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _rmdir(path: Path) -> None:
    try:
        os.rmdir(path)
    except FileNotFoundError:
        pass


def _terminate(pid, stamp, *, timeout=10.0) -> bool:
    if not _alive(pid, stamp):
        return True
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_stamp(pid) != stamp:
            return True
        time.sleep(0.05)
    return process_stamp(pid) != stamp


def _runtime_dir_for(view_uuid: str) -> Path:
    """Choose a short private AF_UNIX directory, never a long project path."""
    uid = os.getuid()
    candidates = []
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidates.append(Path(xdg))
    candidates.append(Path(f"/run/user/{uid}"))
    for base in candidates:
        try:
            observed = os.lstat(base)
            import stat as stat_module
            if (stat_module.S_ISDIR(observed.st_mode)
                    and observed.st_uid == uid and not (observed.st_mode & 0o077)):
                candidate = base / "unifier-views" / view_uuid
                if len(str(candidate / "s").encode()) < 100:
                    return candidate
        except OSError:
            continue
    return Path(tempfile.gettempdir()) / f"unifier-view-{uid}-{view_uuid}"


def _cleanup_list(view: dict) -> list[str]:
    """Known program-owned files; never recursively delete a directory."""
    names = []
    for key in ("control_socket", "storage_socket", "spec_path", "log_path",
                "storage_journal", "ready_path"):
        value = view.get(key)
        if value:
            names.append(value)
    view_dir = Path(view["view_dir"])
    for name in ("spec.json", "view.log", "log", "control.sock", "storage.sock",
                 "ready.json"):
        names.append(str(view_dir / name))
    # Atomic journal/readiness writes use this fixed program-owned prefix. A
    # crash can leave an incomplete temporary file; remove only regular files.
    try:
        for child in view_dir.iterdir():
            if child.name.startswith(".storage-journal-") and child.is_file() \
                    and not child.is_symlink():
                names.append(str(child))
    except OSError:
        pass
    seen = set()
    return [item for item in names if not (item in seen or seen.add(item))]


def receive_scm_fd(connection: socket.socket) -> tuple[dict, int]:
    """Receive JSON metadata plus exactly one SCM_RIGHTS fd.

    The second recvmsg result is a list of (level, type, bytes) ancillary
    records, never an integer fd list. Truncated/multiple descriptors are
    closed and refused rather than returning a tuple as an fd.
    """
    message, ancillary, flags, _address = connection.recvmsg(
        65536, socket.CMSG_SPACE(4 * 16))
    received = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            import array
            values = array.array("i")
            usable = len(data) - (len(data) % values.itemsize)
            values.frombytes(data[:usable])
            received.extend(values.tolist())
    if flags & getattr(socket, "MSG_CTRUNC", 0) or len(received) != 1:
        for descriptor in received:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        detail = "truncated" if flags & getattr(socket, "MSG_CTRUNC", 0) else str(
            len(received))
        raise UnifierError(
            f"BACKEND_UNAVAILABLE: expected one SCM_RIGHTS fd, got {detail}")
    try:
        payload = json.loads(message.decode().split("\n", 1)[0] or "{}")
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        os.close(received[0])
        raise UnifierError(f"BACKEND_UNAVAILABLE: broker metadata is invalid: {exc}") from exc
    if not isinstance(payload, dict):
        os.close(received[0])
        raise UnifierError("BACKEND_UNAVAILABLE: broker metadata is not an object")
    return payload, received[0]


def _ppid(pid: int):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
        return int(fields[1])
    except (OSError, IndexError, ValueError):
        return None


def _descendant_or_same(pid: int, ancestor: int) -> bool:
    current = pid
    for _ in range(64):
        if current == ancestor:
            return True
        parent = _ppid(current)
        if parent is None or parent <= 1:
            return False
        current = parent
    return False


def _descendants(pid: int) -> list[int]:
    """Live descendants of one exact process, used only for our launcher."""
    result = []
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return result
    for entry in entries:
        if not entry.name.isdigit():
            continue
        child = int(entry.name)
        if child != pid and _descendant_or_same(child, pid):
            result.append(child)
    return result


def _signal_descendants(pid: int, signum) -> None:
    for child in _descendants(pid):
        with contextlib.suppress(OSError):
            os.kill(child, signum)


def _session_id(pid: int):
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8").rsplit(")", 1)[1].split()
        return int(fields[3])
    except (OSError, IndexError, ValueError):
        return None


def _peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    """Return the kernel SO_PEERCRED (pid, uid, gid) for a UNIX socket."""
    raw = connection.getsockopt(
        socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    if len(raw) != struct.calcsize("3i"):
        raise UnifierError("BACKEND_UNAVAILABLE: SO_PEERCRED is unavailable")
    return struct.unpack("3i", raw)


def _verify_broker_ready(connection: socket.socket, payload: dict, *,
                         nonce: str, launcher_pid: int) -> tuple[int, int]:
    """Verify the broker's own host PID, start stamp, nonce and process origin.

    The Popen process is the outer ``podman unshare`` launcher. The Python
    broker inside it is the SO_PEERCRED peer; it must name itself, match its
    own live start stamp, carry this view's nonce, and descend from the launcher
    (or be the launcher after exec). The outer PID is never accepted as the
    broker identity.
    """
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise UnifierError(
            f"BACKEND_UNAVAILABLE: storage broker failed: "
            f"{payload.get('error') if isinstance(payload, dict) else payload}")
    broker_pid = payload.get("broker_pid")
    broker_stamp = payload.get("broker_stamp")
    if (isinstance(broker_pid, bool) or not isinstance(broker_pid, int)
            or broker_pid <= 0):
        raise UnifierError("BACKEND_UNAVAILABLE: broker did not report a host PID")
    if (isinstance(broker_stamp, bool) or not isinstance(broker_stamp, int)):
        raise UnifierError("BACKEND_UNAVAILABLE: broker did not report a start stamp")
    if payload.get("nonce") != nonce:
        raise UnifierError("BACKEND_UNAVAILABLE: broker nonce does not match this view")
    peer_pid, peer_uid, _peer_gid = _peer_credentials(connection)
    if peer_pid != broker_pid:
        raise UnifierError(
            "BACKEND_UNAVAILABLE: broker PID does not match the SO_PEERCRED peer")
    if peer_uid != os.getuid():
        raise UnifierError(
            "BACKEND_UNAVAILABLE: broker peer UID is not the view owner")
    current_stamp = process_stamp(broker_pid)
    if current_stamp != broker_stamp:
        raise UnifierError(
            "BACKEND_UNAVAILABLE: broker start stamp does not identify a live process")
    # The outer podman process normally remains the parent. start_new_session
    # also pins the whole launch to the launcher's session, which still proves
    # origin if podman exits after exec/reparenting.
    launcher_session = _session_id(launcher_pid)
    same_session = (broker_pid == launcher_pid
                    or _session_id(broker_pid) == launcher_pid
                    or (launcher_session is not None
                        and _session_id(broker_pid) == launcher_session))
    if not (same_session
            or _descendant_or_same(broker_pid, launcher_pid)
            or _descendant_or_same(broker_pid, os.getpid())):
        raise UnifierError(
            "BACKEND_UNAVAILABLE: broker process does not descend from the launcher")
    return broker_pid, broker_stamp


def _control_request(socket_path: str, request: dict, *, timeout=5.0) -> dict:
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    try:
        client.connect(socket_path)
        client.sendall((json.dumps(request) + "\n").encode())
        response = json.loads(client.recv(65536).decode().split("\n", 1)[0])
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise UnifierError(f"view control request failed: {exc}") from exc
    finally:
        client.close()
    if not isinstance(response, dict):
        raise UnifierError("view control response is not an object")
    return response


def _unmount_fuse(mountpoint: str) -> tuple[bool, str]:
    """Normal (never lazy) unmount of the host FUSE mount."""
    try:
        result = subprocess.run(
            ["fusermount3", "-u", mountpoint], stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, check=False)
    except OSError as exc:
        return False, str(exc)
    detail = (result.stderr or "").strip()
    if result.returncode != 0:
        return False, detail or f"fusermount3 exited {result.returncode}"
    return True, ""


def _wait_pid_gone(pid, stamp, timeout=10.0) -> bool:
    if pid is None or stamp is None:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_stamp(pid) != stamp:
            return True
        time.sleep(0.05)
    return process_stamp(pid) != stamp


class ViewManager:
    def __init__(self, root: Path, service):
        self.root = Path(root)
        self.service = service
        self.registry = ViewRegistry(self.root)
        # Exact Popen handles for outer `podman unshare` launchers started by
        # this manager instance, used only for reliable wait/reap.
        self._broker_launchers = {}
        self._worker_processes = {}

    def open(self, env_id: str, *, mountpoint: str | None = None) -> dict:
        from unifier.service import validate_env_id
        env_id = validate_env_id(env_id)
        view_uuid = str(uuid.uuid4())
        base = self.root / ".lab/state/views"
        view_dir = base / view_uuid
        if mountpoint:
            mount_dir = Path(_absolute(mountpoint, "mountpoint"))
            if mount_dir.exists() or mount_dir.is_symlink():
                raise UnifierError(f"view mountpoint already exists: {mount_dir}")
        else:
            mount_dir = view_dir / "mount"
        runtime_dir = _runtime_dir_for(view_uuid)
        with self.service._lifecycle_lock(env_id):
            record = self.service.store.load(env_id)
            assert_no_active_views(self.root, env_id)
            info = self.service._require_owned(record)
            if info.running:
                raise UnifierError(
                    f"environment {env_id} is running; stop it before opening a view")
            binding = binding_for(record)
            validate_binding(binding)
            now = self.service.clock()
            view = {
                "schema_version": SCHEMA_VERSION,
                "view_uuid": view_uuid,
                "environment_id": env_id,
                "environment_binding": binding,
                "state": "starting",
                "mountpoint": str(mount_dir),
                "view_dir": str(view_dir),
                "runtime_dir": str(runtime_dir),
                "runtime_dev": None,
                "runtime_ino": None,
                "owner_pid": os.getpid(),
                "owner_stamp": process_stamp(os.getpid()),
                "worker_pid": None, "worker_stamp": None,
                "broker_pid": None, "broker_stamp": None,
                "broker_launcher_pid": None, "broker_launcher_stamp": None,
                "storage_journal": str(view_dir / "storage.json"),
                "storage_nonce": uuid.uuid4().hex,
                "storage_ref_state": "none",
                "control_socket": str(runtime_dir / "c"),
                "storage_socket": str(runtime_dir / "s"),
                "spec_path": str(view_dir / "spec.json"),
                "log_path": str(view_dir / "view.log"),
                "ready_path": str(view_dir / "ready.json"),
                "last_error": None,
                "cleanup_errors": [],
                "readonly": True,
                "backend": "podman-overlay",
                "raw_link_semantics": "projected-link",
                "uid_map": build_id_map(record, os.getuid(), os.getgid()).as_dict(),
                "fuse_access_uid": os.getuid(),
                "fuse_access_gid": os.getgid(),
                "namespace_caps": "not-assumed",
                "created_at": now,
                "updated_at": now,
            }
            # Record every planned resource before creating it. Once this row
            # exists, start/remove are blocked even if setup fails.
            self.registry.add(view)
            try:
                _secure_dir(base, create=True)
                os.mkdir(view_dir, 0o700)
                _fsync_dir(base)
                _secure_dir(view_dir, create=False)
                if mountpoint and not mount_dir.parent.is_dir():
                    raise UnifierError(
                        f"view mountpoint parent does not exist: {mount_dir.parent}")
                if mount_dir.exists() or mount_dir.is_symlink():
                    raise UnifierError(f"view mountpoint already exists: {mount_dir}")
                mount_dir.mkdir(mode=0o700)
                _secure_dir(mount_dir, create=False)
                if runtime_dir.parent.name == "unifier-views":
                    _secure_dir(runtime_dir.parent, create=True)
                os.mkdir(runtime_dir, 0o700)
                _secure_dir(runtime_dir, create=False)
                observed = os.lstat(runtime_dir)
                view.update(runtime_dev=observed.st_dev, runtime_ino=observed.st_ino,
                            updated_at=self.service.clock())
                self.registry.update(view)
            except BaseException as exc:
                errors = self._cleanup_resources(view)
                if errors:
                    self._record_failed(view, exc, errors)
                else:
                    with contextlib.suppress(Exception):
                        self.registry.remove(view_uuid)
                raise
        try:
            self._start_view(view)
            return self.registry.one(view_uuid)
        except BaseException as exc:
            errors = self._cleanup_resources(view)
            if errors:
                self._record_failed(view, exc, errors)
            else:
                with contextlib.suppress(Exception):
                    self.registry.remove(view_uuid)
            raise

    def _start_view(self, view: dict) -> None:
        env_id = view["environment_id"]
        record = self.service.store.load(env_id)
        binding = binding_for(record)
        if binding != view["environment_binding"]:
            raise UnifierError("environment binding changed while opening the view")
        mounts, extra_capabilities = self._collect_mounts(record)
        storage_socket = view["storage_socket"]
        control_socket = view["control_socket"]
        log_path = view["log_path"]
        merged_fd, broker_pid, broker_stamp, launcher_pid, launcher_stamp, broker_meta = (
            self._start_broker(
                record["runtime"]["container_id"], storage_socket, log_path,
                view["storage_journal"], view["storage_nonce"], view["view_uuid"]))
        view.update(broker_pid=broker_pid, broker_stamp=broker_stamp,
                    broker_launcher_pid=launcher_pid,
                    broker_launcher_stamp=launcher_stamp,
                    storage_ref_state="mounted",
                    updated_at=self.service.clock())
        self.registry.update(view)
        try:
            id_mappings = self._inspect_id_mappings(record["runtime"]["container_id"])
            if (record.get("runtime", {}).get("user_namespace") == "keep-id"
                    and id_mappings is None):
                raise UnifierError(
                    "BACKEND_UNAVAILABLE: keep-id ID mappings are unavailable")
            id_map = build_id_map(
                record, os.getuid(), os.getgid(),
                storage_uid_map=broker_meta.get("storage_uid_map"),
                storage_gid_map=broker_meta.get("storage_gid_map"),
                id_mappings=id_mappings)
            spec = {
                "view_uuid": view["view_uuid"],
                "mounts": mounts,
                "capabilities": sorted(set(self._capabilities(record))
                                       | set(extra_capabilities)),
                "id_map": id_map.as_dict(),
            }
            spec_path = Path(view["spec_path"])
            spec_path.write_text(
                json.dumps(spec, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            os.chmod(spec_path, 0o600)
            view.update(uid_map=id_map.as_dict(), updated_at=self.service.clock())
            self.registry.update(view)
            worker_pid, worker_stamp = self._start_worker(
                view, merged_fd, spec_path, control_socket, log_path,
                view["ready_path"], view["storage_nonce"])
        except BaseException:
            # Do not release the storage reference ahead of a possibly mounted
            # FUSE worker. open() runs the ordered cleanup (unmount FUSE, then
            # worker, then broker/storage).
            raise
        finally:
            try:
                os.close(merged_fd)
            except OSError:
                pass
            _unlink(storage_socket)
        view.update(worker_pid=worker_pid, worker_stamp=worker_stamp,
                    state="ready", updated_at=self.service.clock())
        self.registry.update(view)

    def _inspect_id_mappings(self, cid: str):
        result = subprocess.run(
            ["podman", "inspect", "--format", "{{json .HostConfig.IDMappings}}", cid],
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            return None
        try:
            value = json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            return None
        if value is None:
            return None
        if not isinstance(value, dict):
            raise UnifierError("BACKEND_UNAVAILABLE: Podman IDMappings is not an object")
        for key in ("UidMap", "GidMap"):
            if key in value and not isinstance(value[key], list):
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: Podman {key} is not a list")
        return value

    @staticmethod
    def _mount_options(item: dict) -> str:
        value = item.get("Options")
        if isinstance(value, list):
            tokens = [str(token) for token in value]
        elif isinstance(value, str):
            tokens = [token for token in value.split(",") if token]
        else:
            tokens = []
        if item.get("RW") is False and "ro" not in tokens:
            tokens.append("ro")
        return ",".join(tokens)

    def _collect_mounts(self, record: dict) -> tuple[list[dict], set[str]]:
        cid = record["runtime"]["container_id"]
        result = subprocess.run(
            ["podman", "inspect", "--format", "{{json .Mounts}}", cid],
            capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise UnifierError(
                f"BACKEND_UNAVAILABLE: podman inspect mounts failed: "
                f"{((result.stderr or '') + (result.stdout or ''))[-2000:]}")
        try:
            inspected = json.loads(result.stdout or "null") or []
        except json.JSONDecodeError as exc:
            raise UnifierError(f"BACKEND_UNAVAILABLE: invalid mount JSON: {exc}") from exc
        if not isinstance(inspected, list):
            raise UnifierError("BACKEND_UNAVAILABLE: Podman mounts are not a list")
        by_destination = {}
        for item in inspected:
            if not isinstance(item, dict):
                raise UnifierError("BACKEND_UNAVAILABLE: malformed Podman mount")
            destination = item.get("Destination")
            source = item.get("Source")
            if not isinstance(destination, str) or not destination.startswith("/"):
                raise UnifierError("BACKEND_UNAVAILABLE: mount destination is invalid")
            destination = os.path.normpath(destination)
            if destination in by_destination:
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: duplicate mount destination {destination}")
            by_destination[destination] = item
        data = []
        extra_capabilities = set()
        desktop_destinations = {
            os.path.normpath(item.get("container", ""))
            for item in ((record.get("desktop") or {}).get("mounts") or [])
            if isinstance(item, dict) and isinstance(item.get("container"), str)
        }
        frozen = record.get("shared_mounts") or []
        for item in frozen:
            if not isinstance(item, dict):
                raise UnifierError("BACKEND_UNAVAILABLE: malformed frozen share")
            host = item.get("host")
            destination = os.path.normpath(item.get("container", ""))
            mounted = by_destination.get(destination)
            if mounted is None:
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: frozen share {destination} is not in Podman mounts")
            source = mounted.get("Source")
            if not isinstance(host, str) or not isinstance(source, str) or os.path.realpath(source) != os.path.realpath(host):
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: frozen share source changed for {destination}")
            expected_options = {
                token.strip() for token in str(item.get("options", "")).split(",") if token.strip()}
            actual_options = {
                token.strip() for token in self._mount_options(mounted).split(",") if token.strip()}
            if expected_options and not expected_options.issubset(actual_options):
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: frozen share options changed for {destination}")
            data.append({"destination": destination, "source": source,
                         "type": mounted.get("Type", "bind"),
                         "options": self._mount_options(mounted)})
        # Explicit persistent-mount records are the preferred contract for
        # nested binds and named volumes.
        persistent = record.get("persistent_mounts") or []
        if not isinstance(persistent, list):
            raise UnifierError("BACKEND_UNAVAILABLE: persistent_mounts must be a list")
        for item in persistent:
            if not isinstance(item, dict):
                raise UnifierError("BACKEND_UNAVAILABLE: malformed persistent mount")
            host = item.get("host")
            destination = os.path.normpath(item.get("container", ""))
            mounted = by_destination.get(destination)
            if mounted is None or not isinstance(host, str) or not isinstance(
                    mounted.get("Source"), str) or os.path.realpath(mounted["Source"]) != os.path.realpath(host):
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: persistent mount changed for {destination}")
            expected_options = {
                token.strip() for token in str(item.get("options", "")).split(",") if token.strip()}
            actual_options = {
                token.strip() for token in self._mount_options(mounted).split(",") if token.strip()}
            if expected_options and not expected_options.issubset(actual_options):
                raise UnifierError(
                    f"BACKEND_UNAVAILABLE: persistent mount options changed for {destination}")
            if destination not in {entry["destination"] for entry in data}:
                data.append({"destination": destination, "source": mounted["Source"],
                             "type": mounted.get("Type", "bind"),
                             "options": self._mount_options(mounted)})
        # Named volumes are persistent data, not a generated capability node.
        for destination, item in by_destination.items():
            if destination in {entry["destination"] for entry in data}:
                continue
            if destination in desktop_destinations:
                extra_capabilities.add(destination)
                continue
            if item.get("Type") == "volume":
                source = item.get("Source")
                if not isinstance(source, str) or not os.path.exists(source):
                    raise UnifierError(
                        f"BACKEND_UNAVAILABLE: volume source is missing for {destination}")
                data.append({"destination": destination, "source": source,
                             "type": "volume", "options": self._mount_options(item)})
        # A bind nested below a persistent volume/shared destination is a real
        # overlay layer, not a runtime capability. Other unexpected binds/tmpfs
        # stay fail-closed capability nodes.
        for destination, item in by_destination.items():
            if destination in {entry["destination"] for entry in data}:
                continue
            if destination in desktop_destinations:
                extra_capabilities.add(destination)
                continue
            parent_data = any(
                destination.startswith(entry["destination"].rstrip("/") + "/")
                for entry in data)
            source = item.get("Source")
            if parent_data and isinstance(source, str) and os.path.exists(source):
                data.append({"destination": destination, "source": source,
                             "type": item.get("Type", "bind"),
                             "options": self._mount_options(item)})
            else:
                extra_capabilities.add(destination)
        return data, extra_capabilities

    def _capabilities(self, record: dict) -> list[str]:
        caps = {"/proc", "/sys", "/dev", "/run"}
        desktop = record.get("desktop") or {}
        for item in desktop.get("mounts") or []:
            container = item.get("container")
            if isinstance(container, str) and container.startswith("/"):
                caps.add(container)
        return sorted(caps)

    def _abort_launcher(self, process, *, broker_pid=None, broker_stamp=None) -> None:
        """Stop an unfinished broker launcher and reap its outer podman process.

        Before the broker identity is verified, the exact new session process
        group is used so the inner Python process cannot be orphaned. A verified
        broker is signalled by its own host PID+stamp first.
        """
        if broker_pid is not None and _alive(broker_pid, broker_stamp):
            _terminate(broker_pid, broker_stamp, timeout=5.0)
        if process is not None:
            if process.poll() is None:
                _signal_descendants(process.pid, signal.SIGTERM)
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=5.0)
                except (subprocess.TimeoutExpired, OSError):
                    _signal_descendants(process.pid, signal.SIGKILL)
                    with contextlib.suppress(OSError):
                        os.killpg(process.pid, signal.SIGKILL)
                    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                        process.wait(timeout=5.0)
            else:
                with contextlib.suppress(OSError):
                    process.wait(timeout=0)

    def _start_broker(self, cid: str, socket_path: str, log_path: str,
                      journal_path: str, nonce: str, view_uuid: str):
        _unlink(socket_path)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(socket_path)
        os.chmod(socket_path, 0o600)
        listener.listen(1)
        listener.settimeout(25.0)
        log = open(log_path, "ab", buffering=0)
        command = [
            "podman", "unshare", sys.executable,
            str(Path(__file__).resolve().parents[2] / "tools/unifier.py"),
            "view", "_broker", "--cid", cid, "--socket", socket_path,
            "--journal", journal_path, "--nonce", nonce,
            "--view-uuid", view_uuid,
        ]
        from unifier.install import worker_environment
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True,
                env=worker_environment(self.root))
        finally:
            log.close()
        connection = None
        descriptor = None
        broker_pid = broker_stamp = None
        try:
            try:
                connection, _address = listener.accept()
            except (socket.timeout, OSError) as exc:
                raise UnifierError(
                    "BACKEND_UNAVAILABLE: storage broker did not connect: "
                    f"{exc}") from exc
            payload, descriptor = receive_scm_fd(connection)
            broker_pid, broker_stamp = _verify_broker_ready(
                connection, payload, nonce=nonce, launcher_pid=process.pid)
            journal = read_journal(journal_path)
            if not _journal_matches(
                    journal, cid=cid, view_uuid=view_uuid, nonce=nonce):
                raise UnifierError(
                    "BACKEND_UNAVAILABLE: broker storage journal does not match "
                    "this view")
            if journal.get("state") != "mounted":
                raise UnifierError(
                    "BACKEND_UNAVAILABLE: broker storage reference is not durably "
                    f"mounted (journal state {journal.get('state')!r})")
            if payload.get("merged_path") != journal.get("merged_path"):
                raise UnifierError(
                    "BACKEND_UNAVAILABLE: broker merged root does not match the journal")
            self._broker_launchers[view_uuid] = process
            return (descriptor, broker_pid, broker_stamp,
                    process.pid, process_stamp(process.pid), payload)
        except BaseException:
            if descriptor is not None:
                with contextlib.suppress(OSError):
                    os.close(descriptor)
                descriptor = None
            self._abort_launcher(
                process, broker_pid=broker_pid, broker_stamp=broker_stamp)
            raise
        finally:
            if connection is not None:
                connection.close()
            listener.close()

    def _start_worker(self, view: dict, merged_fd: int, spec_path: Path,
                      control_socket: str, log_path: str, ready_path: str,
                      nonce: str):
        _unlink(ready_path)
        command = [
            sys.executable, str(Path(__file__).resolve().parents[2] / "tools/unifier.py"),
            "view", "_fuse", "--mountpoint", view["mountpoint"],
            "--merged-fd", str(merged_fd), "--spec", str(spec_path),
            "--control-socket", control_socket, "--view-uuid", view["view_uuid"],
            "--ready-file", ready_path, "--nonce", nonce,
        ]
        log = open(log_path, "ab", buffering=0)
        from unifier.install import worker_environment
        try:
            process = subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                pass_fds=(merged_fd,), start_new_session=True,
                env=worker_environment(self.root))
        finally:
            log.close()
        worker_stamp = process_stamp(process.pid)
        if worker_stamp is None:
            self._abort_launcher(process)
            raise UnifierError(f"FUSE worker exited immediately; see {log_path}")
        self._worker_processes[view["view_uuid"]] = process
        # Publish the exact worker identity before waiting, so a timeout can be
        # cleaned up in the required order (unmount FUSE before terminating it).
        view.update(worker_pid=process.pid, worker_stamp=worker_stamp,
                    updated_at=self.service.clock())
        self.registry.update(view)
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise UnifierError(
                    f"FUSE worker exited before becoming ready; see {log_path}")
            ready = read_journal(ready_path)
            if isinstance(ready, dict):
                if (ready.get("view_uuid") != view["view_uuid"]
                        or ready.get("nonce") != nonce
                        or ready.get("pid") != process.pid
                        or ready.get("stamp") != worker_stamp
                        or ready.get("init_major") != 7):
                    raise UnifierError(
                        f"FUSE worker readiness identity is invalid; see {log_path}")
                try:
                    status = _control_request(
                        control_socket, {"op": "status"}, timeout=1.0)
                except UnifierError:
                    status = None
                if (status and status.get("ok")
                        and status.get("initialized") is True
                        and status.get("worker_pid") == process.pid
                        and status.get("worker_stamp") == worker_stamp
                        and status.get("view_uuid") == view["view_uuid"]
                        and is_mounted(view["mountpoint"])):
                    return process.pid, worker_stamp
            time.sleep(0.05)
        raise UnifierError(
            f"FUSE worker did not complete INIT and answer control; see {log_path}")

    def _terminate_worker(self, view: dict) -> bool:
        """Signal the exact FUSE worker and reap it when this manager owns it."""
        pid = view.get("worker_pid")
        stamp = view.get("worker_stamp")
        ok = _terminate(pid, stamp, timeout=5.0)
        if not ok:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            ok = _wait_pid_gone(pid, stamp, timeout=5.0)
        process = self._worker_processes.pop(view["view_uuid"], None)
        if process is not None:
            if process.poll() is None:
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=5.0)
            else:
                with contextlib.suppress(OSError):
                    process.wait(timeout=0)
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.kill()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=5.0)
                ok = process.poll() is not None and ok
        return ok and not _alive(pid, stamp)

    def _terminate_broker(self, view: dict) -> bool:
        """Signal the real broker, wait/reap the outer launcher, never by name."""
        pid = view.get("broker_pid")
        stamp = view.get("broker_stamp")
        journal = read_journal(view.get("storage_journal"))
        if _journal_matches(
                journal, cid=view["environment_binding"]["container_id"],
                view_uuid=view["view_uuid"], nonce=view.get("storage_nonce")):
            if not _alive(pid, stamp):
                candidate = journal.get("broker_pid")
                candidate_stamp = journal.get("broker_stamp")
                if _alive(candidate, candidate_stamp):
                    pid, stamp = candidate, candidate_stamp
                    view.update(broker_pid=pid, broker_stamp=stamp)
        ok = _terminate(pid, stamp, timeout=10.0)
        if not ok:
            with contextlib.suppress(OSError):
                os.kill(pid, signal.SIGKILL)
            ok = _wait_pid_gone(pid, stamp, timeout=5.0)
        process = self._broker_launchers.pop(view["view_uuid"], None)
        if process is not None:
            if process.poll() is None:
                try:
                    process.wait(timeout=5.0)
                except (subprocess.TimeoutExpired, OSError):
                    with contextlib.suppress(OSError):
                        os.killpg(process.pid, signal.SIGTERM)
                    with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                        process.wait(timeout=5.0)
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    os.killpg(process.pid, signal.SIGKILL)
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=5.0)
            if process.poll() is None:
                ok = False
        else:
            launcher_pid = view.get("broker_launcher_pid")
            launcher_stamp = view.get("broker_launcher_stamp")
            if _alive(launcher_pid, launcher_stamp):
                _terminate(launcher_pid, launcher_stamp, timeout=5.0)
                if _alive(launcher_pid, launcher_stamp):
                    ok = False
        return ok

    def _storage_umount(self, cid: str) -> tuple[bool, str]:
        """Release exactly one storage reference owned by this view.

        The caller has already proved ownership from the journal. Podman mount
        references are counted, so this single umount never releases somebody
        else's pre-existing reference.
        """
        try:
            result = subprocess.run(
                ["podman", "unshare", "podman", "umount", cid],
                capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, str(exc)
        detail = ((result.stderr or "") + (result.stdout or "")).strip()
        return result.returncode == 0, detail

    def _mark_needs_reconciliation(self, view: dict, reason: str) -> None:
        view["state"] = "needs-reconciliation"
        view["storage_ref_state"] = "needs-reconciliation"
        view["last_error"] = reason
        view["updated_at"] = self.service.clock()

    def _reconcile_storage(self, view: dict) -> list[str]:
        """Resolve this view's own Podman storage reference, fail-closed.

        Only a journal state of ``mounted`` proves that exactly one acquired
        reference is still held and may be released. ``mounting``/``releasing``/
        ``release-failed`` are ambiguous crash windows: keep the lease and mark
        needs-reconciliation rather than executing a possibly wrong umount.
        """
        journal_path = view.get("storage_journal")
        journal = read_journal(journal_path)
        nonce = view.get("storage_nonce")
        cid = view["environment_binding"]["container_id"]
        state_before = view.get("storage_ref_state")
        if journal is None:
            if state_before in {
                    "mounting", "mounted", "releasing", "release-failed",
                    "needs-reconciliation"}:
                reason = (
                    "storage journal is missing although this view may hold a "
                    "Podman mount reference")
                self._mark_needs_reconciliation(view, reason)
                return [reason]
            if state_before not in {"released"}:
                view["storage_ref_state"] = "none"
            return []
        if not _journal_matches(
                journal, cid=cid, view_uuid=view["view_uuid"], nonce=nonce):
            reason = "storage journal identity does not match this view"
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        state = journal.get("state")
        if state in {"released", "mount-failed"}:
            view["storage_ref_state"] = "released" if state == "released" else "none"
            return []
        if state != "mounted":
            reason = f"storage reference is unresolved (journal state {state!r})"
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        # A live broker still owns the reference; the caller must stop it first.
        if _alive(journal.get("broker_pid"), journal.get("broker_stamp")):
            reason = "storage broker is still alive; refusing to umount underneath it"
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        payload = dict(journal)
        payload.update(
            state="releasing", updated_at=time.time(),
            reason="recovery", recovered_by_pid=os.getpid(),
            recovered_by_stamp=process_stamp(os.getpid()))
        try:
            write_journal(journal_path, payload)
        except OSError as exc:
            reason = f"could not durably record storage release: {exc}"
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        ok, detail = self._storage_umount(cid)
        if not ok:
            payload.update(state="release-failed", updated_at=time.time(),
                           error=detail[-2000:])
            with contextlib.suppress(OSError):
                write_journal(journal_path, payload)
            reason = f"could not release this view's storage reference: {detail}"
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        payload.update(state="released", updated_at=time.time(),
                       released_by="recovery")
        try:
            write_journal(journal_path, payload)
        except OSError as exc:
            reason = (
                "storage umount succeeded but the release journal could not be "
                f"made durable: {exc}")
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        view["storage_ref_state"] = "released"
        return []

    def _cleanup_resources(self, view: dict) -> list[str]:
        """Clean only this view's known resources, in dependency order.

        The caller preserves a live/uncertain record and the original exception
        when any resource remains. No recursive delete and no lazy unmount. A
        busy FUSE mount leaves the worker, broker, storage reference, sockets,
        logs, journal and lease untouched so the view remains readable.
        """
        errors = []
        mounted = is_mounted(view["mountpoint"])
        if mounted:
            ok, detail = _unmount_fuse(view["mountpoint"])
            if not ok:
                view["state"] = "ready"
                view["last_error"] = (
                    "view mountpoint is busy or could not be unmounted"
                    + (f": {detail}" if detail else ""))
                return [view["last_error"]]
            mounted = is_mounted(view["mountpoint"])
        if mounted:
            view["state"] = "ready"
            return ["view mountpoint is still mounted after a normal unmount"]
        try:
            if not self._terminate_worker(view):
                reason = "FUSE worker did not exit after the mount was removed"
                if _storage_may_be_held(view):
                    self._mark_needs_reconciliation(view, reason)
                else:
                    view["state"] = "failed"
                return [reason]
        except Exception as exc:
            reason = f"FUSE worker cleanup: {exc}"
            if _storage_may_be_held(view):
                self._mark_needs_reconciliation(view, reason)
            else:
                view["state"] = "failed"
            return [reason]
        try:
            if not self._terminate_broker(view):
                reason = "storage broker did not exit"
                self._mark_needs_reconciliation(view, reason)
                return [reason]
        except Exception as exc:
            reason = f"storage broker cleanup: {exc}"
            self._mark_needs_reconciliation(view, reason)
            return [reason]
        try:
            errors.extend(self._reconcile_storage(view))
        except Exception as exc:
            reason = f"storage reference reconciliation failed: {exc}"
            self._mark_needs_reconciliation(view, reason)
            errors.append(reason)
        if errors:
            return errors
        for path in _cleanup_list(view):
            try:
                observed = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue
            try:
                if os.path.isdir(path) and not os.path.islink(path):
                    errors.append(f"unexpected directory remains: {path}")
                else:
                    os.unlink(path)
            except OSError as exc:
                errors.append(f"{path}: {exc}")
        for directory in (view.get("runtime_dir"), view["mountpoint"], view["view_dir"]):
            if not directory:
                continue
            try:
                os.rmdir(directory)
            except FileNotFoundError:
                pass
            except OSError as exc:
                errors.append(f"{directory}: {exc}")
        return errors

    def _record_failed(self, view: dict, exc: BaseException, errors: list[str]) -> None:
        if view.get("state") not in {"ready", "needs-reconciliation"}:
            view["state"] = "failed"
        view["last_error"] = str(exc)
        view["cleanup_errors"] = list(errors)
        view["updated_at"] = self.service.clock()
        with contextlib.suppress(Exception):
            self.registry.update(view)

    def list(self) -> list[dict]:
        result = []
        for record in self.registry.list():
            item = dict(record)
            item["active"] = view_active(record)
            item["mounted"] = is_mounted(record["mountpoint"])
            result.append(item)
        return result

    def status(self, identifier: str | None = None) -> list[dict]:
        records = self.list() if identifier is None else [
            {**item, "active": view_active(item), "mounted": is_mounted(item["mountpoint"])}
            for item in self.registry.find(identifier)]
        if identifier is not None and not records:
            raise UnifierError(f"view {identifier} is not registered")
        return records

    def readlink_raw(self, identifier: str, path: str) -> dict:
        record = self.registry.one(identifier)
        socket_path = record.get("control_socket")
        if not socket_path:
            raise UnifierError("view has no control socket")
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(5.0)
        try:
            client.connect(socket_path)
            client.sendall((json.dumps({"op": "readlink_raw", "path": path}) + "\n").encode())
            response = json.loads(client.recv(65536).decode().split("\n", 1)[0])
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise UnifierError(f"view control request failed: {exc}") from exc
        finally:
            client.close()
        if not response.get("ok"):
            raise UnifierError(str(response.get("error")))
        return response

    def revoke(self, identifier: str) -> dict:
        record = self.registry.one(identifier)
        env_id = record["environment_id"]
        with self.service._lifecycle_lock(env_id):
            record = self.registry.one(record["view_uuid"])
            record["state"] = "revoking"
            record["updated_at"] = self.service.clock()
            self.registry.update(record)
            errors = self._cleanup_resources(record)
            if errors:
                self._record_failed(record, UnifierError("view revoke incomplete"), errors)
                raise UnifierError(
                    "view revoke incomplete: " + "; ".join(errors))
            return self.registry.remove(record["view_uuid"])

    def recover(self, identifier: str | None = None) -> list[dict]:
        targets = self.registry.list() if identifier is None else self.registry.find(identifier)
        if identifier is not None and not targets:
            raise UnifierError(f"view {identifier} is not registered")
        recovered = []
        failures = []
        for record in targets:
            try:
                recovered.append(self.revoke(record["view_uuid"]))
            except UnifierError as exc:
                failures.append(f"{record['view_uuid']}: {exc}")
        if failures:
            raise UnifierError(
                "view recovery incomplete: " + "; ".join(failures))
        return recovered
