"""Durable managed regular-file copy and restricted move.

This is the first D-stage product path: one regular file from a registered
source data root into a registered read-write target data root.  Paths are
never taken as bare host strings; both sides are selected by the registered
``DataRootRegistry`` identity and walked from an anchored root FD with
``O_NOFOLLOW`` on every component.

The journal is deliberately per operation.  A long copy never holds the global
operation registry lock.  Publication uses an O_EXCL staging file in the target
directory and a same-filesystem ``linkat`` (``os.link``) without overwrite.
The source is never deleted.  A move therefore stops at
``SOURCE_CLEANUP_PENDING`` and says that the destination is copied while the
source is retained.

This module does not claim a cross-system transaction between the filesystem
and the operation registry, a content-addressed CAS, full metadata fidelity, or
an atomic cross-filesystem move.
"""

from contextlib import contextmanager
import datetime as dt
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile
import uuid
from zoneinfo import ZoneInfo

from multilinux.errors import MultilinuxError
from multilinux.json_records import JsonRecordCollection, fsync_directory
from multilinux.lease import (
    LeaseBroken,
    LeaseBusy,
    LeaseError,
    LeaseUnsupported,
    WriteLease,
    rename_noreplace,
)
from multilinux.registry import DataRootRegistry


SCHEMA_VERSION = 1
REGISTRY_SCHEMA_VERSION = 1
OPERATIONS = {"copy", "move"}
# Cleanup states are deliberately non-terminal: a process exit at any boundary
# is resumed from the durable journal instead of being guessed from a missing
# path.
CLEANUP_STATES = {
    "SOURCE_CLEANUP_INTENT",
    "SOURCE_QUARANTINE_INTENT",
    "SOURCE_QUARANTINE_READY",
    "SOURCE_CAPTURE_INTENT",
    "SOURCE_CAPTURED",
    "SOURCE_ISOLATED",
    "SOURCE_DELETION_INTENT",
    "SOURCE_REMOVED",
}
STATES = {
    "INTENT_DURABLE",
    "STAGING",
    "TARGET_PUBLISHED",
    "REGISTRY_COMMITTED",
    "SOURCE_CLEANUP_PENDING",
    "DONE",
    "CONFLICT",
    "NEEDS_RECONCILIATION",
} | CLEANUP_STATES
TERMINAL_STATES = {
    "DONE", "SOURCE_CLEANUP_PENDING", "CONFLICT", "NEEDS_RECONCILIATION",
}
PENDING_STATES = {
    "INTENT_DURABLE", "STAGING", "TARGET_PUBLISHED", "REGISTRY_COMMITTED",
} | CLEANUP_STATES
CAPABILITIES = {
    "regular_file_copy": True,
    "move_copies_then_retains_source": True,
    "controlled_move_source_delete": True,
    "source_delete": False,
    "atomic_cross_filesystem_move": False,
    "universal_atomic_rename": False,
    "directory_recursive": False,
    "acl_preservation": False,
    "sparse_file": False,
    "device_or_special_file": False,
    "symlink_follow": False,
    "hardlink_source": False,
    "xattr_or_selinux_preservation": False,
    "full_metadata_preservation": False,
    "overwrite_existing_target": False,
    "hash_and_rename_is_cas": False,
    "fs_and_registry_cross_system_atomic": False,
    "lease_blocks_rename_or_unlink": False,
    "hostile_same_uid_immunity": False,
}
UNSUPPORTED = [
    "directories and recursive trees",
    "ACL-bearing files",
    "sparse or compressed files",
    "device, fifo and socket nodes",
    "symbolic links and hard-linked regular sources",
    "copying xattrs, SELinux labels, ownership, ACLs or timestamps as a complete set",
    "automatic source deletion outside an explicitly authorized controlled move",
    "atomic cross-filesystem rename",
    "overwriting an existing target",
    "immunity from malicious same-UID tampering with private state or journals",
]

_SOURCE_VERSION_FIELDS = (
    "dev", "ino", "mode", "nlink", "uid", "gid", "size", "mtime_ns", "ctime_ns",
)
_READ_FLAGS = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW


def _now() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _canonical_uuid(value, label: str) -> str:
    if not isinstance(value, str):
        raise MultilinuxError(f"{label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MultilinuxError(f"{label} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise MultilinuxError(f"{label} must be the canonical lowercase UUID form")
    return value


def _validate_relative(relative_path: str, label: str) -> list[str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise MultilinuxError(f"{label} must be a non-empty string")
    if relative_path.startswith("/"):
        raise MultilinuxError(f"{label} must not be absolute")
    if "\x00" in relative_path or "\n" in relative_path:
        raise MultilinuxError(f"{label} contains an invalid character")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MultilinuxError(
            f"{label} must not contain empty, '.' or '..' components")
    return parts


def _mount_id(descriptor: int) -> int:
    path = Path(f"/proc/self/fdinfo/{descriptor}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MultilinuxError(
            "cannot verify the mount boundary: /proc/self/fdinfo is unavailable "
            f"({exc}); refusing the operation") from exc
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
            break
    raise MultilinuxError(
        "cannot verify the mount boundary: mnt_id is missing from fdinfo; "
        "refusing the operation")


def _stat_version(observed: os.stat_result) -> dict:
    return {
        "dev": observed.st_dev,
        "ino": observed.st_ino,
        "mode": observed.st_mode,
        "nlink": observed.st_nlink,
        "uid": observed.st_uid,
        "gid": observed.st_gid,
        "size": observed.st_size,
        "mtime_ns": observed.st_mtime_ns,
        "ctime_ns": observed.st_ctime_ns,
        "file_type": "regular",
    }


def _same_version(stored: dict, observed: os.stat_result) -> bool:
    if not isinstance(stored, dict):
        return False
    current = _stat_version(observed)
    return all(stored.get(field) == current[field] for field in _SOURCE_VERSION_FIELDS)


def _root_matches(record: dict, observed: os.stat_result) -> bool:
    return (stat.S_ISDIR(observed.st_mode)
            and observed.st_dev == record.get("root_dev")
            and observed.st_ino == record.get("root_ino"))


def _hash_fd(descriptor: int, *, reset: bool = True) -> tuple[str, int]:
    if reset:
        os.lseek(descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = os.read(descriptor, 1024 * 1024)
        if not chunk:
            break
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


def _write_all(descriptor: int, data: bytes) -> None:
    offset = 0
    while offset < len(data):
        written = os.write(descriptor, data[offset:])
        if written <= 0:
            raise OSError("short write to staging file")
        offset += written


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(
                payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _read_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MultilinuxError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise MultilinuxError(f"{label} must be a JSON object")
    return payload


def _require(record: dict, key: str, label: str):
    value = record.get(key)
    if value is None or value == "":
        raise MultilinuxError(f"{label} is missing field {key}")
    return value


def _validate_plan_side(side, label: str) -> dict:
    if not isinstance(side, dict):
        raise MultilinuxError(f"{label} must be an object")
    for key in ("root_uuid", "management_domain", "access_policy", "locator",
                "relative_path"):
        _require(side, key, label)
    _canonical_uuid(side["root_uuid"], f"{label} root_uuid")
    _validate_relative(side["relative_path"], f"{label} relative_path")
    if (not isinstance(side["locator"], str) or not side["locator"].startswith("/")
            or "\x00" in side["locator"] or "\n" in side["locator"]):
        raise MultilinuxError(f"{label} locator must be an absolute path")
    if side["access_policy"] not in {"read-only", "read-write"}:
        raise MultilinuxError(f"{label} has an unknown access policy")
    for key in ("root_dev", "root_ino"):
        if isinstance(side.get(key), bool) or not isinstance(side.get(key), int):
            raise MultilinuxError(f"{label} {key} must be an integer")
    parent_parts = side.get("parent_parts")
    if parent_parts is None:
        # Early D journals/registry rows did not store the parent list.  It is
        # derivable from the already validated relative path without resolving
        # or following anything.
        parent_parts = side["relative_path"].split("/")[:-1]
        side["parent_parts"] = parent_parts
    if not isinstance(parent_parts, list) or any(
            not isinstance(part, str) or part in {"", ".", ".."} or "/" in part
            or "\x00" in part or "\n" in part for part in parent_parts):
        raise MultilinuxError(f"{label} parent_parts is invalid")
    return side


def _validate_cleanup_block(block) -> dict:
    if not isinstance(block, dict):
        raise MultilinuxError("journal source_cleanup must be an object")
    for key in ("requested", "authorized", "performed"):
        if key in block and not isinstance(block[key], bool):
            raise MultilinuxError(f"journal source_cleanup.{key} must be a boolean")
    mode = block.get("mode")
    if mode is not None and mode not in {"retain", "controlled"}:
        raise MultilinuxError("journal source_cleanup.mode is invalid")
    policy = block.get("policy_snapshot")
    if policy is not None and policy not in {"retain", "controlled"}:
        raise MultilinuxError("journal source_cleanup.policy_snapshot is invalid")
    quarantine = block.get("quarantine")
    if quarantine is not None:
        if not isinstance(quarantine, dict):
            raise MultilinuxError("journal source_cleanup.quarantine must be an object")
        name = quarantine.get("dir_name")
        if name is not None and (
                not isinstance(name, str) or not name or "/" in name
                or "\x00" in name or name in {".", ".."}):
            raise MultilinuxError("journal quarantine directory name is invalid")
        entry = quarantine.get("entry_name")
        if entry is not None and entry not in {"source"}:
            raise MultilinuxError("journal quarantine entry name is invalid")
        for key in ("dir_dev", "dir_ino", "captured_dev", "captured_ino",
                    "captured_size"):
            value = quarantine.get(key)
            if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)):
                raise MultilinuxError(
                    f"journal quarantine {key} must be an integer or null")
    captured = block.get("captured")
    if captured is not None and not isinstance(captured, dict):
        raise MultilinuxError("journal source_cleanup.captured must be an object")
    return block


def _validate_journal(record: dict) -> dict:
    if not isinstance(record, dict):
        raise MultilinuxError("file operation journal must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise MultilinuxError("file operation journal schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise MultilinuxError(
            f"file operation journal schema_version {version!r} is not supported "
            f"(this build understands {SCHEMA_VERSION})")
    _canonical_uuid(_require(record, "operation_uuid", "journal"), "operation_uuid")
    if record.get("operation") not in OPERATIONS:
        raise MultilinuxError(f"unknown file operation: {record.get('operation')!r}")
    if record.get("state") not in STATES:
        raise MultilinuxError(f"unknown file operation state: {record.get('state')!r}")
    _validate_plan_side(record.get("source"), "journal source")
    _validate_plan_side(record.get("target"), "journal target")
    stage = record.get("stage")
    if not isinstance(stage, dict):
        raise MultilinuxError("journal stage must be an object")
    if stage.get("name") is not None:
        name = stage["name"]
        if (not isinstance(name, str) or not name or "/" in name
                or "\x00" in name or name in {".", ".."}):
            raise MultilinuxError("journal stage name is invalid")
    if stage.get("nonce") is not None and not isinstance(stage["nonce"], str):
        raise MultilinuxError("journal stage nonce must be a string")
    for key in ("dev", "ino", "size"):
        value = stage.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise MultilinuxError(f"journal stage {key} must be an integer or null")
    if not isinstance(record.get("published"), dict):
        raise MultilinuxError("journal published must be an object")
    if not isinstance(record.get("registry"), dict):
        raise MultilinuxError("journal registry must be an object")
    if "execution" in record and not isinstance(record["execution"], dict):
        raise MultilinuxError("journal execution must be an object")
    if not isinstance(record.get("capabilities"), dict):
        raise MultilinuxError("journal capabilities must be an object")
    if "source_cleanup" in record:
        _validate_cleanup_block(record["source_cleanup"])
    if not isinstance(record.get("unsupported"), list):
        raise MultilinuxError("journal unsupported must be a list")
    if record.get("state") in CLEANUP_STATES:
        block = record.get("source_cleanup")
        if not isinstance(block, dict) or block.get("authorized") is not True:
            raise MultilinuxError(
                "a cleanup-state journal has no pinned source-cleanup authorization")
    if not isinstance(record.get("created_at"), str) or not isinstance(
            record.get("updated_at"), str):
        raise MultilinuxError("journal timestamps must be strings")
    return record


def _validate_registry_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise MultilinuxError("file operation registry record must be an object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise MultilinuxError("file operation registry schema_version must be an integer")
    if version != REGISTRY_SCHEMA_VERSION:
        raise MultilinuxError(
            f"file operation registry schema_version {version!r} is not supported")
    _canonical_uuid(_require(record, "operation_uuid", "registry record"),
                    "operation_uuid")
    if record.get("operation") not in OPERATIONS:
        raise MultilinuxError("registry record has an unknown operation")
    _validate_plan_side(record.get("source"), "registry source")
    _validate_plan_side(record.get("target"), "registry target")
    published = record.get("published")
    if not isinstance(published, dict):
        raise MultilinuxError("registry published must be an object")
    for key in ("dev", "ino", "size"):
        if isinstance(published.get(key), bool) or not isinstance(published.get(key), int):
            raise MultilinuxError(f"registry published {key} must be an integer")
    sha = published.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise MultilinuxError("registry published sha256 must be 64 hex digits")
    for key in ("registered_at", "management_domain"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise MultilinuxError(f"registry record {key} must be a non-empty string")
    if not isinstance(record.get("provenance"), dict):
        raise MultilinuxError("registry provenance must be an object")
    return record


class _Conflict(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _Reconcile(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _MediaUnavailable(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _CleanupRefused(Exception):
    """A clean refusal before any irreversible deletion."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class _LeaseLost(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _version_without_ctime_matches(stored: dict, observed: os.stat_result) -> bool:
    if not isinstance(stored, dict):
        return False
    current = _stat_version(observed)
    for field in _SOURCE_VERSION_FIELDS:
        if field == "ctime_ns":
            continue
        if stored.get(field) != current[field]:
            return False
    # Rename updates ctime.  It must move forward, never backward, and the
    # expected ctime change is recorded separately from the identity/content
    # checks rather than weakening them.
    old_ctime = stored.get("ctime_ns")
    return isinstance(old_ctime, int) and observed.st_ctime_ns >= old_ctime


class FileOperations:
    """Per-operation durable journal plus a short global registration record."""

    def __init__(self, root: Path, registry: DataRootRegistry | None = None,
                 clock=None, fault_hook=None):
        self.root = Path(root)
        self.registry = registry or DataRootRegistry(self.root)
        self.clock = clock or _now
        self.fault_hook = fault_hook
        self.directory = self.root / ".lab/state/file-operations"
        self.operations = self.directory / "operations"
        self.locks = self.directory / "locks"
        self.registry_collection = JsonRecordCollection(
            self.root, "file-operations-registry",
            schema_version=REGISTRY_SCHEMA_VERSION,
            validator=_validate_registry_record,
            unique_keys=("operation_uuid",))

    # ----- small durable primitives ---------------------------------------
    def _ensure_directories(self) -> None:
        for path in (self.directory, self.operations, self.locks):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _journal_path(self, operation_uuid: str) -> Path:
        return self.operations / f"{operation_uuid}.json"

    def _lock_path(self, operation_uuid: str) -> Path:
        return self.locks / f"{operation_uuid}.lock"

    @contextmanager
    def _operation_lock(self, operation_uuid: str, *, shared: bool = False):
        self._ensure_directories()
        descriptor = os.open(
            self._lock_path(operation_uuid), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _fault(self, point: str) -> None:
        if callable(self.fault_hook):
            self.fault_hook(point)

    @contextmanager
    def _root_lock(self, root_uuid: str, *, exclusive: bool):
        """Per-source-root lock; never the global registry lock across I/O.

        The lock is re-entrant for this instance so a cleanup step reached from
        ``_finish_committed`` does not deadlock against the outer operation.
        Copy/move before cleanup take it shared; an explicitly authorized
        controlled move takes it exclusively so a copy cannot race the source
        capture/deletion in this process or another.
        """
        held = getattr(self, "_held_root_locks", None)
        if held is None:
            held = self._held_root_locks = {}
        entry = held.get(root_uuid)
        if entry is not None:
            if entry["exclusive"] or not exclusive:
                entry["depth"] += 1
                try:
                    yield
                finally:
                    entry["depth"] -= 1
                return
            raise MultilinuxError(
                "cannot upgrade a shared source-root lock to exclusive")
        self._ensure_directories()
        path = self.locks / f"root-{root_uuid}.lock"
        descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            held[root_uuid] = {
                "descriptor": descriptor, "exclusive": exclusive, "depth": 1,
            }
            try:
                yield
            finally:
                held.pop(root_uuid, None)
        finally:
            os.close(descriptor)

    def _read_journal(self, operation_uuid: str) -> dict:
        path = self._journal_path(operation_uuid)
        if not path.is_file():
            raise MultilinuxError(f"file operation {operation_uuid} is not recorded")
        record = _validate_journal(_read_json(path, "file operation journal"))
        if record["operation_uuid"] != operation_uuid:
            raise MultilinuxError(
                "file operation journal UUID does not match its filename")
        return record

    def _write_journal(self, record: dict) -> None:
        record["updated_at"] = self.clock()
        if not record.get("created_at"):
            record["created_at"] = record["updated_at"]
        _validate_journal(record)
        _atomic_write_json(self._journal_path(record["operation_uuid"]), record)

    def _transition(self, record: dict, state: str) -> None:
        record["state"] = state
        record.setdefault("transitions", []).append(
            {"state": state, "at": self.clock()})
        self._write_journal(record)
        self._fault(state)

    def _record_problem(self, record: dict, state: str, reason: str) -> dict:
        record["state"] = state
        record["last_error"] = reason
        block = record.get("source_cleanup")
        proven_removed = (isinstance(block, dict)
                          and block.get("performed") is True)
        record["source_retained"] = not proven_removed
        record["transitions"] = record.get("transitions", [])
        record["transitions"].append(
            {"state": state, "at": self.clock(), "reason": reason})
        self._write_journal(record)
        self._fault(state)
        return record

    # ----- registration / root and path checks ----------------------------
    def _root_record(self, root_uuid: str, label: str, *,
                     recovery: bool = False) -> dict:
        try:
            record = self.registry.show(root_uuid)
        except MultilinuxError as exc:
            if recovery:
                raise _Reconcile(
                    f"{label} data root registration is unavailable: {exc}") from exc
            raise
        if record["root_uuid"] != root_uuid:
            raise MultilinuxError(f"{label} root identity changed")
        return record

    def _check_nested(self, record: dict, absolute: str, label: str) -> None:
        matched, _relative = self.registry.match(absolute)
        if matched is not None and matched["root_uuid"] != record["root_uuid"]:
            raise MultilinuxError(
                f"{label} path enters nested data root {matched['root_uuid']} "
                f"({matched['locator']}); use that root explicitly")

    def _open_root_fd(self, record: dict, label: str):
        locator = record["locator"]
        try:
            path_stat = os.stat(locator, follow_symlinks=False)
        except OSError as exc:
            raise _MediaUnavailable(f"{label} data root is unavailable: {locator}: {exc}") from exc
        if not _root_matches(record, path_stat):
            raise _MediaUnavailable(
                f"{label} data root path was replaced (device/inode no longer match "
                f"the registration): {locator}")
        try:
            descriptor = os.open(locator, _DIR_FLAGS)
        except OSError as exc:
            raise _MediaUnavailable(f"{label} data root cannot be opened: {exc}") from exc
        try:
            observed = os.fstat(descriptor)
            if not _root_matches(record, observed):
                raise _MediaUnavailable(
                    f"{label} data root changed while opening: {locator}")
            return descriptor, observed, _mount_id(descriptor)
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _verify_root_path(record: dict, label: str) -> None:
        try:
            observed = os.stat(record["locator"], follow_symlinks=False)
        except OSError as exc:
            raise _MediaUnavailable(
                f"{label} data root path became unavailable: {exc}") from exc
        if not _root_matches(record, observed):
            raise _MediaUnavailable(f"{label} data root path was replaced")

    def _walk_parent(self, root_fd: int, root_stat: os.stat_result,
                     root_mount_id: int, parts: list[str], label: str) -> int:
        current = root_fd
        owned: list[int] = []
        try:
            for part in parts[:-1]:
                child = os.open(part, _DIR_FLAGS, dir_fd=current)
                owned.append(child)
                observed = os.fstat(child)
                if not stat.S_ISDIR(observed.st_mode):
                    raise MultilinuxError(
                        f"{label} component {part!r} is not a directory "
                        "(symlinks are refused)")
                if _mount_id(child) != root_mount_id:
                    raise MultilinuxError(
                        f"{label} path crosses a mount boundary inside the data root")
                if observed.st_dev != root_stat.st_dev:
                    raise MultilinuxError(
                        f"{label} path crosses a device boundary inside the data root")
                current = child
            # The caller owns the returned descriptor. Duplicate it so the
            # intermediate descriptors can be closed here.
            parent = os.dup(current)
            return parent
        finally:
            for descriptor in reversed(owned):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _source_view_check(self, record: dict) -> None:
        source_view = record.get("source_view")
        if source_view is None:
            return
        from multilinux.views import ViewRegistry, is_mounted

        if not isinstance(source_view, dict):
            raise MultilinuxError("source view registration must be an object")
        try:
            view = ViewRegistry(self.root).one(source_view.get("view_uuid"))
        except MultilinuxError as exc:
            raise _MediaUnavailable(
                f"source read-only view is not registered: {exc}") from exc
        if view.get("readonly") is not True:
            raise MultilinuxError("a source view must be read-only")
        if view.get("backend") != "podman-overlay":
            raise MultilinuxError("source view backend is not supported")
        if view.get("environment_binding") != source_view.get("environment_binding"):
            raise MultilinuxError(
                "source view environment binding no longer matches the registration")
        if view.get("mountpoint") != record["locator"]:
            raise MultilinuxError("source view mountpoint does not match the data root")
        if view.get("state") not in {"ready", "mounted"} or not is_mounted(record["locator"]):
            raise _MediaUnavailable(
                "source read-only view is not mounted; an empty mount directory is "
                "not a data root")

    def _inspect_source(self, descriptor: int, relative_path: str) -> dict:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise MultilinuxError(
                f"source {relative_path!r} is not a regular file; only regular files "
                "are supported")
        if observed.st_nlink > 1:
            raise MultilinuxError(
                "hard-linked source files are refused until alias ownership can be "
                "verified")
        if stat.S_ISREG(observed.st_mode) and observed.st_blocks * 512 < observed.st_size:
            raise MultilinuxError(
                "sparse or compressed source files are not supported by this first "
                "regular-file operation")
        try:
            names = os.listxattr(f"/proc/self/fd/{descriptor}")
        except OSError as exc:
            names = []
            acl_state = f"unavailable: {exc}"
        else:
            acl_state = "checked"
        if any("posix_acl" in name for name in names):
            raise MultilinuxError(
                "ACL-bearing source files are not supported by this first "
                "regular-file operation")
        version = _stat_version(observed)
        version["acl_check"] = acl_state
        version["xattrs_observed_but_not_copied"] = list(names)
        return version

    def _validate_request(self, source_root_uuid: str, source_relative: str,
                          target_root_uuid: str, target_relative: str):
        source_parts = _validate_relative(source_relative, "source relative_path")
        target_parts = _validate_relative(target_relative, "target relative_path")
        source_record = self._root_record(source_root_uuid, "source")
        target_record = self._root_record(target_root_uuid, "target")
        if target_record["access_policy"] != "read-write":
            raise MultilinuxError(
                f"target data root {target_record['root_uuid']} is "
                f"{target_record['access_policy']}; managed file operations "
                "require a read-write target")
        if (source_record["root_uuid"] == target_record["root_uuid"]
                and source_relative == target_relative):
            raise MultilinuxError("source and target identify the same registered path")
        source_absolute = os.path.join(source_record["locator"], source_relative)
        target_absolute = os.path.join(target_record["locator"], target_relative)
        self._check_nested(source_record, source_absolute, "source")
        self._check_nested(target_record, target_absolute, "target")
        self._source_view_check(source_record)
        return source_record, target_record, source_parts, target_parts

    @staticmethod
    def _cleanup_authorized_pinned(record: dict) -> bool:
        """Only a journal written with explicit per-operation authorization.

        Absence of the block (all pre-controlled-move journals) or a block
        without ``authorized: true`` can never gain delete rights later, even
        if the root policy is changed to ``controlled``.
        """
        block = record.get("source_cleanup")
        return (isinstance(block, dict)
                and block.get("requested") is True
                and block.get("authorized") is True
                and block.get("mode") == "controlled"
                and block.get("policy_snapshot") == "controlled")

    @staticmethod
    def _preflight_cleanup(source_record: dict) -> None:
        if source_record.get("cleanup_policy", "retain") != "controlled":
            raise MultilinuxError(
                "source cleanup was requested but the source data root is not "
                "registered with --cleanup-policy controlled; refusing before any "
                "copy or journal side effect")
        if source_record.get("access_policy") != "read-write":
            raise MultilinuxError(
                "source cleanup is only supported from a read-write data root; "
                "read-only roots and read-only C views retain their source")
        if source_record.get("source_view") is not None:
            raise MultilinuxError(
                "a C read-only view can be copied from but never cleaned up")
        if source_record.get("backend") != "local-directory":
            raise MultilinuxError(
                "controlled source cleanup only supports the local-directory backend")

    # ----- request construction -------------------------------------------
    @staticmethod
    def _side(record: dict, relative_path: str, parts: list[str]) -> dict:
        return {
            "root_uuid": record["root_uuid"],
            "management_domain": record["management_domain"],
            "access_policy": record["access_policy"],
            "locator": record["locator"],
            "relative_path": relative_path,
            "root_dev": record["root_dev"],
            "root_ino": record["root_ino"],
            "parent_parts": parts[:-1],
        }

    def _new_record(self, operation: str, source_record: dict,
                    source_relative: str, source_parts: list[str],
                    target_record: dict, target_relative: str,
                    target_parts: list[str], source_version: dict,
                    target_parent_stat: os.stat_result, *,
                    source_parent_stat: os.stat_result | None = None,
                    cleanup_requested: bool = False,
                    cleanup_authorized: bool = False) -> dict:
        now = self.clock()
        source = self._side(source_record, source_relative, source_parts)
        source["observed_version"] = source_version
        source["sha256"] = None
        source["source_view"] = source_record.get("source_view")
        source["cleanup_policy"] = source_record.get("cleanup_policy", "retain")
        if source_parent_stat is not None:
            source["parent_dev"] = source_parent_stat.st_dev
            source["parent_ino"] = source_parent_stat.st_ino
        target = self._side(target_record, target_relative, target_parts)
        target["parent_dev"] = target_parent_stat.st_dev
        target["parent_ino"] = target_parent_stat.st_ino
        target["overwrite"] = False
        return {
            "schema_version": SCHEMA_VERSION,
            "operation_uuid": str(uuid.uuid4()),
            "operation": operation,
            "state": "INTENT_DURABLE",
            "source": source,
            "target": target,
            "stage": {
                "name": None, "nonce": None, "parent_dev": None,
                "parent_ino": None, "dev": None, "ino": None, "size": None,
                "mode": None, "mtime_ns": None, "ctime_ns": None,
                "sha256": None, "ready": False, "created": False,
                "cleaned": False,
            },
            "published": {
                "dev": None, "ino": None, "size": None, "sha256": None,
                "nlink": None, "published_at": None,
            },
            "registry": {"committed": False, "committed_at": None},
            "provenance": {
                "source_root_uuid": source_record["root_uuid"],
                "source_relative_path": source_relative,
                "source_management_domain": source_record["management_domain"],
                "source_view_uuid": (source_record.get("source_view") or {}).get(
                    "view_uuid") if isinstance(source_record.get("source_view"), dict) else None,
                "note": "target uses the destination management domain; source is "
                        "independent provenance",
            },
            "management_domain": target_record["management_domain"],
            "execution": {
                "authorized": False,
                "interpreter": None,
                "environment": None,
                "note": "a copy does not inherit source execution authorization, "
                        "handler registration or project binding",
            },
            "source_cleanup": {
                "requested": bool(cleanup_requested),
                "authorized": bool(cleanup_authorized),
                "mode": "controlled" if cleanup_authorized else "retain",
                "policy_snapshot": source_record.get("cleanup_policy", "retain"),
                "root_uuid": source_record["root_uuid"],
                "root_dev": source_record["root_dev"],
                "root_ino": source_record["root_ino"],
                "locator": source_record["locator"],
                "access_policy": source_record["access_policy"],
                "backend": source_record["backend"],
                "source_view_present": bool(source_record.get("source_view")),
                "authorized_at": now if cleanup_authorized else None,
                "state": "pending" if cleanup_authorized else "not_requested",
                "performed": False,
                "quarantine": {
                    "dir_name": None, "dir_dev": None, "dir_ino": None,
                    "dir_nonce": None, "entry_name": "source",
                    "captured_dev": None, "captured_ino": None,
                    "captured_size": None, "captured_sha256": None,
                    "captured_ctime_ns": None,
                },
                "captured": None,
                "lease": {"type": "write", "acquired": False, "broken": False},
                "deletion_intent": None,
                "reason": None,
                "removed_at": None,
            },
            "capabilities": dict(CAPABILITIES),
            "unsupported": list(UNSUPPORTED),
            "source_retained": True,
            "source_retention_reason": None,
            "last_error": None,
            "transitions": [],
            "created_at": now,
            "updated_at": now,
        }

    def _plan_stage(self, record: dict) -> None:
        nonce = uuid.uuid4().hex
        record["stage"].update({
            "name": f".ml-stage-{record['operation_uuid']}-{nonce}",
            "nonce": nonce,
            "parent_dev": record["target"]["parent_dev"],
            "parent_ino": record["target"]["parent_ino"],
            "dev": None,
            "ino": None,
            "size": None,
            "mode": None,
            "mtime_ns": None,
            "ctime_ns": None,
            "sha256": None,
            "ready": False,
            "created": False,
            "cleaned": False,
        })

    # ----- open helpers ----------------------------------------------------
    def _open_source(self, record: dict, *, recovery: bool):
        source = record["source"]
        fresh = self._root_record(source["root_uuid"], "source",
                                 recovery=recovery)
        if (fresh["root_dev"], fresh["root_ino"]) != (
                source["root_dev"], source["root_ino"]):
            raise _Reconcile("source data root registration identity changed")
        if fresh["locator"] != source["locator"]:
            raise _Reconcile("source data root locator changed")
        absolute = os.path.join(fresh["locator"], source["relative_path"])
        try:
            self._check_nested(fresh, absolute, "source")
        except MultilinuxError as exc:
            raise _Reconcile(str(exc)) from exc
        self._source_view_check(fresh)
        root_fd, root_stat, root_mount = self._open_root_fd(fresh, "source")
        try:
            parts = list(source["parent_parts"]) + [source["relative_path"].split("/")[-1]]
            parent_fd = self._walk_parent(root_fd, root_stat, root_mount, parts, "source")
            source_fd = None
            try:
                source_fd = os.open(
                    parts[-1], _READ_FLAGS, dir_fd=parent_fd)
                observed = os.fstat(source_fd)
                if _mount_id(source_fd) != root_mount:
                    raise _MediaUnavailable(
                        "source file crosses a mount boundary inside the data root")
                if observed.st_dev != root_stat.st_dev:
                    raise _MediaUnavailable(
                        "source file crosses a device boundary inside the data root")
                return root_fd, parent_fd, source_fd, observed
            except BaseException:
                if source_fd is not None:
                    try:
                        os.close(source_fd)
                    except OSError:
                        pass
                os.close(parent_fd)
                raise
        except BaseException:
            os.close(root_fd)
            raise

    def _open_target(self, record: dict, *, recovery: bool):
        target = record["target"]
        fresh = self._root_record(target["root_uuid"], "target",
                                 recovery=recovery)
        if fresh["access_policy"] != "read-write":
            raise _Reconcile(
                "target data root is no longer read-write; publication refused")
        if (fresh["root_dev"], fresh["root_ino"]) != (
                target["root_dev"], target["root_ino"]):
            raise _Reconcile("target data root registration identity changed")
        if fresh["locator"] != target["locator"]:
            raise _Reconcile("target data root locator changed")
        absolute = os.path.join(fresh["locator"], target["relative_path"])
        try:
            self._check_nested(fresh, absolute, "target")
        except MultilinuxError as exc:
            raise _Reconcile(str(exc)) from exc
        root_fd, root_stat, root_mount = self._open_root_fd(fresh, "target")
        try:
            parts = list(target["parent_parts"]) + [target["relative_path"].split("/")[-1]]
            parent_fd = self._walk_parent(root_fd, root_stat, root_mount, parts, "target")
            parent_stat = os.fstat(parent_fd)
            if _mount_id(parent_fd) != root_mount:
                raise _Reconcile("target parent crosses a mount boundary")
            if (parent_stat.st_dev, parent_stat.st_ino) != (
                    target.get("parent_dev"), target.get("parent_ino")):
                raise _Reconcile(
                    "target parent directory was replaced; refusing to publish")
            return root_fd, parent_fd, parent_stat, parts[-1]
        except BaseException:
            os.close(root_fd)
            raise

    @staticmethod
    def _lstat_at(parent_fd: int, name: str):
        try:
            return os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise _MediaUnavailable(f"cannot observe {name!r}: {exc}") from exc

    @staticmethod
    def _unlink_at(parent_fd: int, name: str) -> None:
        os.unlink(name, dir_fd=parent_fd)
        os.fsync(parent_fd)

    def _create_stage(self, record: dict, parent_fd: int) -> int:
        stage = record["stage"]
        try:
            descriptor = os.open(
                stage["name"],
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                0o600, dir_fd=parent_fd)
        except FileExistsError as exc:
            raise _Reconcile(
                "the operation staging name already exists as an unknown object; "
                "refusing to claim or replace it") from exc
        observed = os.fstat(descriptor)
        stage.update({
            "dev": observed.st_dev,
            "ino": observed.st_ino,
            "size": observed.st_size,
            "mode": observed.st_mode,
            "mtime_ns": observed.st_mtime_ns,
            "ctime_ns": observed.st_ctime_ns,
            "created": True,
        })
        self._write_journal(record)
        os.fsync(parent_fd)
        self._fault("STAGING_CREATED")
        return descriptor

    def _stage_identity_matches(self, record: dict, observed: os.stat_result) -> bool:
        stage = record["stage"]
        return (stat.S_ISREG(observed.st_mode)
                and stage.get("dev") is not None
                and observed.st_dev == stage["dev"]
                and observed.st_ino == stage["ino"])

    def _copy_stream(self, source_fd: int, stage_fd: int) -> tuple[str, int]:
        os.lseek(source_fd, 0, os.SEEK_SET)
        digest = hashlib.sha256()
        total = 0
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            total += len(chunk)
            _write_all(stage_fd, chunk)
        return digest.hexdigest(), total

    def _source_after_copy(self, source_fd: int, record: dict) -> tuple[os.stat_result, str]:
        observed = os.fstat(source_fd)
        if not _same_version(record["source"]["observed_version"], observed):
            raise _Conflict(
                "source identity, size or timestamps changed during the copy; "
                "source and staging are retained")
        digest, size = _hash_fd(source_fd)
        record["source"]["sha256"] = digest
        record["source"]["verified_size"] = size
        if size != observed.st_size:
            raise _Conflict(
                "source size changed while hashing; source and staging are retained")
        return observed, digest

    def _copy_to_stage(self, record: dict, source_fd: int, stage_fd: int,
                       parent_fd: int) -> None:
        source_digest, copied = self._copy_stream(source_fd, stage_fd)
        source_mode = record["source"]["observed_version"]["mode"] & 0o777
        try:
            os.fchmod(stage_fd, source_mode)
        except OSError:
            pass
        os.fsync(stage_fd)
        os.close(stage_fd)
        stage_fd = -1
        os.fsync(parent_fd)
        read_fd = os.open(record["stage"]["name"], _READ_FLAGS, dir_fd=parent_fd)
        try:
            observed = os.fstat(read_fd)
            if not self._stage_identity_matches(record, observed):
                raise _Reconcile(
                    "staging inode changed while reading it back; refusing to publish")
            stage_digest, stage_size = _hash_fd(read_fd)
        finally:
            os.close(read_fd)
        _observed, source_digest_after = self._source_after_copy(source_fd, record)
        if copied != stage_size or stage_size != record["source"]["observed_version"]["size"]:
            raise _Conflict(
                "source size, copied size and staging size disagree; source and "
                "staging are retained")
        if not (source_digest == source_digest_after == stage_digest):
            raise _Conflict(
                "source and staging hashes do not agree; source and staging are "
                "retained")
        observed = os.stat(record["stage"]["name"], dir_fd=parent_fd,
                           follow_symlinks=False)
        if not self._stage_identity_matches(record, observed):
            raise _Reconcile("staging inode changed before it was recorded as ready")
        record["stage"].update({
            "size": observed.st_size,
            "mode": observed.st_mode,
            "mtime_ns": observed.st_mtime_ns,
            "ctime_ns": observed.st_ctime_ns,
            "sha256": stage_digest,
            "ready": True,
        })
        self._write_journal(record)
        self._fault("STAGING_READY")

    def _publish(self, record: dict, parent_fd: int, stage_name: str,
                 target_name: str) -> None:
        target_record = self._root_record(
            record["target"]["root_uuid"], "target", recovery=True)
        self._verify_root_path(target_record, "target")
        absolute_parent = os.path.join(
            record["target"]["locator"], *record["target"]["parent_parts"])
        try:
            path_parent = os.stat(absolute_parent, follow_symlinks=False)
        except OSError as exc:
            raise _Reconcile(
                f"target parent path disappeared before publication: {exc}") from exc
        opened_parent = os.fstat(parent_fd)
        if (path_parent.st_dev, path_parent.st_ino) != (
                opened_parent.st_dev, opened_parent.st_ino):
            raise _Reconcile(
                "target parent path no longer names the directory held by this "
                "operation; refusing to publish")
        if self._lstat_at(parent_fd, target_name) is not None:
            raise _Conflict(
                "target already exists; this operation does not overwrite it")
        try:
            os.link(stage_name, target_name, src_dir_fd=parent_fd,
                    dst_dir_fd=parent_fd, follow_symlinks=False)
        except FileExistsError as exc:
            raise _Conflict(
                "target appeared before publication; no overwrite was performed") from exc
        target_fd = os.open(target_name, _READ_FLAGS, dir_fd=parent_fd)
        try:
            observed = os.fstat(target_fd)
            stage = record["stage"]
            if (observed.st_dev, observed.st_ino) != (stage["dev"], stage["ino"]):
                raise _Reconcile(
                    "published target is not the exact staging inode; refusing to "
                    "claim it")
            os.fsync(target_fd)
            target_stat = observed
        finally:
            os.close(target_fd)
        os.fsync(parent_fd)
        record["published"] = {
            "dev": target_stat.st_dev,
            "ino": target_stat.st_ino,
            "size": target_stat.st_size,
            "sha256": record["stage"]["sha256"],
            "nlink": target_stat.st_nlink,
            "published_at": self.clock(),
        }
        self._transition(record, "TARGET_PUBLISHED")

    # ----- bounded controlled source cleanup ------------------------------
    @staticmethod
    def _source_entry_name(record: dict) -> str:
        return record["source"]["relative_path"].split("/")[-1]

    def _verify_registry_once(self, record: dict) -> None:
        candidate = self._registry_record(record)
        with self.registry_collection.locked(False):
            records = self.registry_collection.read_unlocked()
        matches = [item for item in records
                   if item.get("operation_uuid") == record["operation_uuid"]]
        if len(matches) != 1:
            raise _Reconcile(
                "the operation registration is missing or duplicated; source "
                "cleanup requires exactly one durable registration")
        if not self._same_registry(matches[0], candidate):
            raise _Reconcile(
                "the durable operation registration no longer matches this "
                "operation; refusing source cleanup")

    def _verify_target_held(self, record: dict, target_fd: int,
                            target_stat: os.stat_result) -> None:
        published = record["published"]
        if (target_stat.st_dev, target_stat.st_ino) != (
                published.get("dev"), published.get("ino")):
            raise _Reconcile(
                "the held target inode no longer matches the published identity")
        if target_stat.st_nlink != 1:
            raise _Reconcile(
                "the published target has extra hard links; refusing source cleanup")
        digest, size = _hash_fd(target_fd)
        if size != published.get("size") or digest != published.get("sha256"):
            raise _Reconcile(
                "the published target content changed before source cleanup")

    def _open_cleanup_target_held(self, record: dict):
        root_fd = parent_fd = target_fd = None
        try:
            root_fd, parent_fd, _parent_stat, target_name = self._open_target(
                record, recovery=True)
            target_stat = self._lstat_at(parent_fd, target_name)
            if target_stat is None:
                raise _Reconcile("the published target path is missing")
            if (target_stat.st_dev, target_stat.st_ino) != (
                    record["published"].get("dev"),
                    record["published"].get("ino")):
                raise _Reconcile(
                    "the target path no longer names the published inode")
            if target_stat.st_nlink != 1:
                raise _Reconcile(
                    "the target path has extra hard links; refusing source cleanup")
            stage_name = record["stage"].get("name")
            if stage_name and self._lstat_at(parent_fd, stage_name) is not None:
                raise _Reconcile(
                    "an internal staging path is still present; refusing source "
                    "cleanup while a stage leftover exists")
            target_fd = os.open(target_name, _READ_FLAGS, dir_fd=parent_fd)
            opened = os.fstat(target_fd)
            if (opened.st_dev, opened.st_ino) != (
                    target_stat.st_dev, target_stat.st_ino):
                raise _Reconcile("the target changed while it was being opened")
            self._verify_target_held(record, target_fd, opened)
            return root_fd, parent_fd, target_fd, target_name, opened
        except BaseException:
            for descriptor in (target_fd, parent_fd, root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            raise

    def _verify_cleanup_target_once(self, record: dict) -> None:
        root_fd, parent_fd, target_fd, _name, _stat_result = \
            self._open_cleanup_target_held(record)
        for descriptor in (target_fd, parent_fd, root_fd):
            try:
                os.close(descriptor)
            except OSError:
                pass
        self._verify_registry_once(record)

    def _verify_cleanup_authorization(self, record: dict,
                                      source_record: dict) -> None:
        block = record.get("source_cleanup")
        if not self._cleanup_authorized_pinned(record):
            raise _Reconcile(
                "this journal has no pinned per-operation controlled cleanup "
                "authorization; a later policy change cannot grant delete rights")
        if source_record.get("cleanup_policy", "retain") != "controlled":
            raise _CleanupRefused(
                "the source data root cleanup policy is no longer controlled")
        if block.get("policy_snapshot") != "controlled":
            raise _CleanupRefused(
                "the pinned cleanup policy snapshot is not controlled")
        if source_record.get("access_policy") != "read-write":
            raise _CleanupRefused(
                "the source data root is no longer read-write")
        if source_record.get("source_view") is not None:
            raise _CleanupRefused(
                "the source root is now a read-only view")
        if source_record.get("backend") != "local-directory":
            raise _CleanupRefused(
                "the source root backend is not local-directory")
        for key in ("root_uuid", "root_dev", "root_ino", "locator"):
            if block.get(key) != source_record.get(key):
                raise _CleanupRefused(
                    f"the source root {key} changed since cleanup was authorized")
        source = record["source"]
        for key in ("root_dev", "root_ino", "locator"):
            if source.get(key) != source_record.get(key):
                raise _CleanupRefused(
                    f"the source plan {key} no longer matches the registration")

    def _ensure_quarantine(self, record: dict, root_fd: int,
                           root_stat: os.stat_result, root_mount: int, *,
                           create: bool):
        block = record["source_cleanup"]
        quarantine = block.setdefault("quarantine", {})
        if not quarantine.get("dir_name"):
            if not create:
                raise _Reconcile(
                    "the private quarantine directory was never planned in this "
                    "journal; refusing to invent one")
            nonce = uuid.uuid4().hex
            quarantine["dir_nonce"] = nonce
            quarantine["dir_name"] = (
                f".ml-quarantine-{record['operation_uuid']}-{nonce}")
            quarantine["entry_name"] = "source"
            block["state"] = "quarantine_intent"
            record["state"] = "SOURCE_QUARANTINE_INTENT"
            self._write_journal(record)
            self._fault("SOURCE_QUARANTINE_INTENT")
        name = quarantine.get("dir_name")
        if (not isinstance(name, str) or not name.startswith(".ml-quarantine-")
                or "/" in name or name in {".", ".."}):
            raise _Reconcile("the quarantine directory name is invalid")
        pinned_identity = quarantine.get("dir_dev") is not None
        observed = self._lstat_at(root_fd, name)
        created_here = False
        if observed is None:
            if not create:
                raise _Reconcile(
                    "the pinned private quarantine directory is missing; refusing "
                    "to infer deletion or invent a replacement")
            if pinned_identity:
                raise _Reconcile(
                    "the journal pinned a quarantine dev/ino but the path is "
                    "missing; refusing to invent a replacement directory")
            try:
                os.mkdir(name, 0o700, dir_fd=root_fd)
            except FileExistsError as exc:
                raise _Reconcile(
                    "the quarantine directory appeared concurrently; refusing to "
                    "claim it") from exc
            os.fsync(root_fd)
            observed = self._lstat_at(root_fd, name)
            created_here = True
        elif not pinned_identity:
            # A same-named directory that exists before this journal recorded a
            # dev/ino is unknown state.  Never adopt, chmod or delete it.
            raise _Reconcile(
                "a pre-existing same-named quarantine directory has no pinned "
                "identity; refusing to adopt or modify it")
        if observed is None or not stat.S_ISDIR(observed.st_mode):
            raise _Reconcile(
                "the quarantine path is not a directory owned by this operation")
        if observed.st_uid != os.getuid():
            raise _Reconcile("the quarantine directory is not owned by this user")
        qfd = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
        try:
            qstat = os.fstat(qfd)
            if not stat.S_ISDIR(qstat.st_mode) or qstat.st_uid != os.getuid():
                raise _Reconcile(
                    "the quarantine directory identity is not private to this user")
            if (qstat.st_dev, qstat.st_ino) != (
                    observed.st_dev, observed.st_ino):
                raise _Reconcile(
                    "the quarantine directory changed while it was being opened")
            if qstat.st_dev != root_stat.st_dev or _mount_id(qfd) != root_mount:
                raise _Reconcile(
                    "the quarantine directory is not on the source filesystem")
            if pinned_identity and (quarantine["dir_dev"], quarantine["dir_ino"]) \
                    != (qstat.st_dev, qstat.st_ino):
                raise _Reconcile(
                    "the quarantine directory no longer matches its pinned dev/ino")
            if created_here:
                # Only a directory this call just created is safe to chmod.
                try:
                    os.fchmod(qfd, 0o700)
                except OSError as exc:
                    raise _Reconcile(
                        f"cannot enforce a private quarantine mode: {exc}") from exc
                qstat = os.fstat(qfd)
            elif (qstat.st_mode & 0o777) != 0o700:
                raise _Reconcile(
                    "the existing quarantine directory does not have private "
                    "0700 mode; refusing to adopt it")
            quarantine["dir_dev"] = qstat.st_dev
            quarantine["dir_ino"] = qstat.st_ino
            if (block.get("state") == "quarantine_intent"
                    or record.get("state") in {
                        "SOURCE_CLEANUP_INTENT", "SOURCE_QUARANTINE_INTENT"}):
                block["state"] = "quarantine_ready"
                record["state"] = "SOURCE_QUARANTINE_READY"
                self._write_journal(record)
                self._fault("SOURCE_QUARANTINE_READY")
            return qfd, qstat
        except BaseException:
            os.close(qfd)
            raise

    def _quarantine_entry_stat(self, qfd: int):
        return self._lstat_at(qfd, "source")

    def _open_quarantine_entry(self, qfd: int):
        observed = self._quarantine_entry_stat(qfd)
        if observed is None:
            return None, None
        if not stat.S_ISREG(observed.st_mode):
            raise _Reconcile(
                "the captured quarantine entry is not a regular file")
        descriptor = os.open("source", _READ_FLAGS, dir_fd=qfd)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (
                    observed.st_dev, observed.st_ino):
                raise _Reconcile(
                    "the quarantine entry changed while it was being opened")
            return descriptor, opened
        except BaseException:
            os.close(descriptor)
            raise

    def _verify_source_original(self, record: dict, descriptor: int,
                                observed: os.stat_result) -> None:
        stored = record["source"].get("observed_version") or {}
        expected_hash = record["source"].get("sha256")
        if not expected_hash:
            raise _Reconcile(
                "the journal has no source hash; refusing controlled cleanup")
        if not _same_version(stored, observed):
            raise _CleanupRefused(
                "the original source identity, size or timestamps changed before "
                "capture; source is retained")
        if observed.st_nlink != 1:
            raise _CleanupRefused(
                "the original source gained another hard link; source is retained")
        digest, size = _hash_fd(descriptor)
        if size != stored.get("size") or digest != expected_hash:
            raise _CleanupRefused(
                "the original source content no longer matches the recorded hash; "
                "source is retained")

    def _verify_captured_source(self, record: dict, descriptor: int,
                                observed: os.stat_result) -> None:
        stored = record["source"].get("observed_version") or {}
        expected_hash = record["source"].get("sha256")
        if not expected_hash:
            raise _Reconcile(
                "the journal has no source hash; refusing controlled cleanup")
        if not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1:
            raise _Reconcile(
                "the captured source is not one regular file with no extra links")
        if not _version_without_ctime_matches(stored, observed):
            raise _Reconcile(
                "the captured inode identity, mode, size or mtime changed beyond "
                "the expected rename ctime update")
        digest, size = _hash_fd(descriptor)
        if size != stored.get("size") or digest != expected_hash:
            raise _Reconcile(
                "the captured source content no longer matches the recorded hash")
        block = record["source_cleanup"]
        block["quarantine"].update({
            "captured_dev": observed.st_dev,
            "captured_ino": observed.st_ino,
            "captured_size": observed.st_size,
            "captured_sha256": digest,
            "captured_ctime_ns": observed.st_ctime_ns,
        })
        block["captured"] = {
            "dev": observed.st_dev,
            "ino": observed.st_ino,
            "size": observed.st_size,
            "sha256": digest,
            "ctime_ns": observed.st_ctime_ns,
            "ctime_change_from_capture": True,
        }

    def _check_original_after_capture(self, record: dict, parent_fd: int,
                                      entry_name: str) -> bool:
        source = record["source"]
        observed = self._lstat_at(parent_fd, entry_name)
        if observed is None:
            return False
        if (observed.st_dev, observed.st_ino) == (
                source["observed_version"].get("dev"),
                source["observed_version"].get("ino")):
            raise _Reconcile(
                "a new hard link to the captured source reappeared at the original "
                "name; refusing to delete any link")
        return True

    def _preserve_original_bytes(self, record: dict, source_fd: int, qfd: int):
        """Best-effort extra copy when a raced foreign name was captured."""
        name = "source-original"
        try:
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC
                | os.O_NOFOLLOW, 0o600, dir_fd=qfd)
        except FileExistsError:
            return None
        except OSError:
            return None
        try:
            digest, size = self._copy_stream(source_fd, descriptor)
            os.fsync(descriptor)
        except OSError:
            try:
                os.unlink(name, dir_fd=qfd)
            except OSError:
                pass
            return None
        finally:
            os.close(descriptor)
        os.fsync(qfd)
        return {"name": name, "size": size, "sha256": digest}

    def _restore_captured_entry(self, record: dict, parent_fd: int,
                                entry_name: str, qfd: int) -> bool:
        if self._lstat_at(parent_fd, entry_name) is not None:
            return False
        try:
            rename_noreplace(qfd, "source", parent_fd, entry_name)
        except FileExistsError:
            return False
        except OSError as exc:
            if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise _Reconcile(
                    "RENAME_NOREPLACE is unavailable for a safe restore") from exc
            raise
        os.fsync(parent_fd)
        os.fsync(qfd)
        return True

    def _remove_quarantine_dir(self, record: dict, root_fd: int, qfd: int,
                               qstat: os.stat_result) -> None:
        block = record["source_cleanup"]
        name = block["quarantine"]["dir_name"]
        observed = self._lstat_at(root_fd, name)
        if observed is None:
            return
        if not stat.S_ISDIR(observed.st_mode) or (
                observed.st_dev, observed.st_ino) != (
                qstat.st_dev, qstat.st_ino):
            raise _Reconcile(
                "the quarantine directory path no longer names the pinned inode; "
                "refusing to remove it")
        try:
            entries = os.listdir(qfd)
        except OSError as exc:
            raise _Reconcile(
                f"cannot inspect the quarantine directory: {exc}") from exc
        if entries:
            raise _Reconcile(
                "the quarantine directory still contains entries; refusing to "
                "remove it")
        try:
            os.rmdir(name, dir_fd=root_fd)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise _Reconcile(
                f"cannot remove the empty quarantine directory: {exc}") from exc
        os.fsync(root_fd)

    def _cleanup_done(self, record: dict) -> None:
        block = record["source_cleanup"]
        block["state"] = "done"
        block["performed"] = True
        block["reason"] = None
        block["lease"]["acquired"] = False
        record["source_retained"] = False
        record["source_retention_reason"] = None
        record["complete"] = True
        record["copy_complete"] = True
        record["finished_at"] = self.clock()
        self._transition(record, "DONE")

    def _cleanup_problem(self, record: dict, state: str, reason: str) -> None:
        block = record.get("source_cleanup")
        if isinstance(block, dict):
            block["state"] = "refused" if state == "SOURCE_CLEANUP_PENDING" else "needs_reconciliation"
            block["reason"] = reason
            lease = block.setdefault("lease", {})
            lease["acquired"] = False
            lease["broken"] = False
        self._record_problem(record, state, reason)

    def _open_source_anchor(self, record: dict):
        source_record = self._root_record(
            record["source"]["root_uuid"], "source", recovery=True)
        root_fd, root_stat, root_mount = self._open_root_fd(
            source_record, "source")
        parent_fd = None
        try:
            parts = list(record["source"]["parent_parts"]) + [
                self._source_entry_name(record)]
            parent_fd = self._walk_parent(
                root_fd, root_stat, root_mount, parts, "source")
            parent_stat = os.fstat(parent_fd)
            if (record["source"].get("parent_dev") is not None
                    and (parent_stat.st_dev, parent_stat.st_ino) != (
                        record["source"].get("parent_dev"),
                        record["source"].get("parent_ino"))):
                raise _Reconcile(
                    "the source parent directory was replaced; refusing cleanup")
            return source_record, root_fd, parent_fd, root_stat, root_mount
        except BaseException:
            if parent_fd is not None:
                os.close(parent_fd)
            os.close(root_fd)
            raise

    def _capture_source(self, record: dict, parent_fd: int, entry_name: str,
                        qfd: int, source_fd: int, source_stat: os.stat_result,
                        lease: WriteLease) -> None:
        block = record["source_cleanup"]
        source = record["source"]
        observed = self._lstat_at(parent_fd, entry_name)
        if observed is None or (observed.st_dev, observed.st_ino) != (
                source_stat.st_dev, source_stat.st_ino):
            preserved = self._preserve_original_bytes(record, source_fd, qfd)
            block["pre_capture_name_changed"] = {
                "observed_dev": observed.st_dev if observed is not None else None,
                "observed_ino": observed.st_ino if observed is not None else None,
                "preserved_original_copy": preserved,
            }
            self._write_journal(record)
            raise _Reconcile(
                "the original source name changed immediately before capture; "
                "refusing to rename an unverified object"
                + ("; the opened source bytes were preserved in the private "
                   "quarantine" if preserved is not None else ""))
        block["state"] = "capture_intent"
        block["capture_intent"] = {
            "source_dev": source_stat.st_dev,
            "source_ino": source_stat.st_ino,
            "quarantine_dir_dev": block["quarantine"]["dir_dev"],
            "quarantine_dir_ino": block["quarantine"]["dir_ino"],
            "at": self.clock(),
        }
        record["state"] = "SOURCE_CAPTURE_INTENT"
        self._write_journal(record)
        self._fault("SOURCE_CAPTURE_INTENT")
        lease.assert_held()
        try:
            rename_noreplace(parent_fd, entry_name, qfd, "source")
        except FileNotFoundError as exc:
            raise _Reconcile(
                "the original source name disappeared before capture; the bytes "
                "are not assumed deleted") from exc
        except FileExistsError as exc:
            raise _Reconcile(
                "the private quarantine entry already exists; refusing to replace "
                "it") from exc
        except OSError as exc:
            if exc.errno in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
                raise _CleanupRefused(
                    "RENAME_NOREPLACE is unsupported on the source filesystem; "
                    "source is retained") from exc
            raise
        os.fsync(parent_fd)
        os.fsync(qfd)
        captured = self._lstat_at(qfd, "source")
        if captured is None:
            raise _Reconcile(
                "the capture rename returned without a quarantine entry")
        block["state"] = "captured"
        block["captured"] = {
            "dev": captured.st_dev,
            "ino": captured.st_ino,
            "size": captured.st_size,
            "matches_source_fd": (captured.st_dev, captured.st_ino) == (
                source_stat.st_dev, source_stat.st_ino),
            "at": self.clock(),
        }
        record["state"] = "SOURCE_CAPTURED"
        self._write_journal(record)
        self._fault("SOURCE_CAPTURED")
        _ = source

    def _handle_foreign_capture(self, record: dict, source_fd, parent_fd: int,
                                entry_name: str, qfd: int, root_fd: int) -> None:
        block = record["source_cleanup"]
        if not isinstance(block.get("captured"), dict):
            block["captured"] = {}
        preserved = None
        if source_fd is not None:
            preserved = self._preserve_original_bytes(record, source_fd, qfd)
        block["captured"]["matches_source_fd"] = False
        block["captured"]["preserved_original_copy"] = preserved
        if self._lstat_at(parent_fd, entry_name) is None:
            restored = self._restore_captured_entry(record, parent_fd, entry_name, qfd)
            block["captured"]["foreign_restored_no_replace"] = restored
            if restored and preserved is None:
                qstat = os.fstat(qfd)
                self._remove_quarantine_dir(record, root_fd, qfd, qstat)
            reason = (
                "a raced foreign inode was captured; it was restored with "
                "RENAME_NOREPLACE and no source bytes were deleted")
            if preserved is not None:
                reason += (
                    "; the original source bytes were preserved in the private "
                    "quarantine for reconciliation")
                self._cleanup_problem(
                    record, "NEEDS_RECONCILIATION", reason)
            else:
                self._cleanup_problem(record, "SOURCE_CLEANUP_PENDING", reason)
            return
        block["captured"]["foreign_retained_in_quarantine"] = True
        self._cleanup_problem(
            record, "NEEDS_RECONCILIATION",
            "a raced foreign inode was captured while the original name was "
            "already repopulated; the foreign bytes are retained in the private "
            "quarantine and the original path was not overwritten")

    def _verify_source_anchor_paths(self, record: dict, source_record: dict,
                                    root_fd: int, parent_fd: int,
                                    root_stat: os.stat_result) -> None:
        self._verify_root_path(source_record, "source")
        opened_root = os.fstat(root_fd)
        if not _root_matches(source_record, opened_root):
            raise _Reconcile("the held source root FD no longer matches registration")
        if opened_root.st_dev != root_stat.st_dev or opened_root.st_ino != root_stat.st_ino:
            raise _Reconcile("the held source root changed during cleanup")
        absolute_parent = os.path.join(
            source_record["locator"], *record["source"]["parent_parts"])
        try:
            path_parent = os.stat(absolute_parent, follow_symlinks=False)
        except OSError as exc:
            raise _Reconcile(
                f"the source parent path disappeared before deletion: {exc}") from exc
        opened_parent = os.fstat(parent_fd)
        if (path_parent.st_dev, path_parent.st_ino) != (
                opened_parent.st_dev, opened_parent.st_ino):
            raise _Reconcile(
                "the source parent path no longer names the anchored directory")

    def _verify_target_anchor_paths(self, record: dict, target_record: dict,
                                    root_fd: int, parent_fd: int, target_fd: int,
                                    target_name: str) -> os.stat_result:
        self._verify_root_path(target_record, "target")
        opened_root = os.fstat(root_fd)
        if not _root_matches(target_record, opened_root):
            raise _Reconcile("the held target root FD no longer matches registration")
        absolute_parent = os.path.join(
            target_record["locator"], *record["target"]["parent_parts"])
        try:
            path_parent = os.stat(absolute_parent, follow_symlinks=False)
        except OSError as exc:
            raise _Reconcile(
                f"the target parent path disappeared before deletion: {exc}") from exc
        opened_parent = os.fstat(parent_fd)
        if (path_parent.st_dev, path_parent.st_ino) != (
                opened_parent.st_dev, opened_parent.st_ino):
            raise _Reconcile(
                "the target parent path no longer names the anchored directory")
        current = self._lstat_at(parent_fd, target_name)
        held = os.fstat(target_fd)
        if current is None or (current.st_dev, current.st_ino) != (
                held.st_dev, held.st_ino):
            raise _Reconcile(
                "the target pathname no longer names the held published inode")
        return held

    def _cleanup_run(self, record: dict) -> None:
        """Drive a pinned controlled cleanup to DONE or an explicit problem."""
        block = record.get("source_cleanup")
        if not isinstance(block, dict) or not self._cleanup_authorized_pinned(record):
            self._cleanup_problem(
                record, "NEEDS_RECONCILIATION",
                "controlled cleanup state has no pinned per-operation authorization")
            return
        if record.get("state") == "DONE" or block.get("state") == "done":
            return
        root_fd = parent_fd = qfd = entry_fd = target_root_fd = None
        target_parent_fd = target_fd = None
        lease = None
        target_lease = None
        source_fd = None
        try:
            source_record, target_record = self._load_records(record)
            self._verify_cleanup_authorization(record, source_record)
            self._verify_cleanup_target_once(record)
            state = record.get("state")

            if state == "SOURCE_REMOVED":
                # Journal written after the private unlink.  A missing empty
                # quarantine is expected if the process died after rmdir.
                source_record = self._root_record(
                    record["source"]["root_uuid"], "source", recovery=True)
                root_fd, root_stat, root_mount = self._open_root_fd(
                    source_record, "source")
                name = block.get("quarantine", {}).get("dir_name")
                observed = self._lstat_at(root_fd, name) if name else None
                if observed is not None:
                    if not stat.S_ISDIR(observed.st_mode):
                        raise _Reconcile(
                            "the quarantine path is no longer a directory")
                    qfd = os.open(name, _DIR_FLAGS, dir_fd=root_fd)
                    qstat = os.fstat(qfd)
                    if (qstat.st_dev, qstat.st_ino) != (
                            block["quarantine"].get("dir_dev"),
                            block["quarantine"].get("dir_ino")):
                        raise _Reconcile(
                            "the quarantine path was replaced after source removal")
                    self._remove_quarantine_dir(record, root_fd, qfd, qstat)
                self._cleanup_done(record)
                return

            source_record, root_fd, parent_fd, root_stat, root_mount = \
                self._open_source_anchor(record)
            create = state not in {
                "SOURCE_CAPTURED", "SOURCE_ISOLATED", "SOURCE_DELETION_INTENT"}
            qfd, qstat = self._ensure_quarantine(
                record, root_fd, root_stat, root_mount, create=create)
            entry_fd, entry_stat = self._open_quarantine_entry(qfd)

            if entry_fd is not None:
                source_identity = (
                    record["source"]["observed_version"].get("dev"),
                    record["source"]["observed_version"].get("ino"))
                if (entry_stat.st_dev, entry_stat.st_ino) != source_identity:
                    self._handle_foreign_capture(
                        record, None, parent_fd, self._source_entry_name(record),
                        qfd, root_fd)
                    return
                lease = WriteLease(entry_fd, "captured source")
                try:
                    lease.acquire()
                except LeaseBusy as exc:
                    self._cleanup_problem(
                        record, "NEEDS_RECONCILIATION", str(exc))
                    return
                except (LeaseUnsupported, LeaseError) as exc:
                    self._cleanup_problem(
                        record, "NEEDS_RECONCILIATION", str(exc))
                    return
                record["source_cleanup"]["lease"] = {
                    "type": "write", "acquired": True, "broken": False,
                    "target": "captured-source",
                }
                self._verify_captured_source(record, entry_fd, entry_stat)
                if state in {"SOURCE_CAPTURED", "SOURCE_CAPTURE_INTENT"}:
                    repopulated = self._check_original_after_capture(
                        record, parent_fd, self._source_entry_name(record))
                    record["source_cleanup"]["original_path_repopulated"] = repopulated
                    record["source_cleanup"]["state"] = "isolated"
                    record["state"] = "SOURCE_ISOLATED"
                    self._write_journal(record)
                    self._fault("SOURCE_ISOLATED")
                elif state == "SOURCE_DELETION_INTENT":
                    repopulated = self._check_original_after_capture(
                        record, parent_fd, self._source_entry_name(record))
                    record["source_cleanup"]["original_path_repopulated"] = repopulated
            else:
                if state in {"SOURCE_CAPTURED", "SOURCE_ISOLATED"}:
                    raise _Reconcile(
                        "the captured inode is missing from the pinned quarantine; "
                        "refusing to infer deletion from a missing path")
                if state == "SOURCE_DELETION_INTENT":
                    # Durable unlink intent plus a pinned, private, empty
                    # quarantine is the proof domain for an unrecorded unlink.
                    if os.listdir(qfd):
                        raise _Reconcile(
                            "the quarantine is not empty and the captured entry is "
                            "missing; refusing to infer deletion")
                    self._finish_after_removal(record, root_fd, qfd, qstat)
                    return
                # Before capture: the original name must still hold the exact
                # opened source object.
                entry_name = self._source_entry_name(record)
                source_fd = os.open(
                    entry_name, _READ_FLAGS, dir_fd=parent_fd)
                source_stat = os.fstat(source_fd)
                if (source_stat.st_dev, source_stat.st_ino) != (
                        record["source"]["observed_version"].get("dev"),
                        record["source"]["observed_version"].get("ino")):
                    raise _Reconcile(
                        "the original source name now names a different inode; "
                        "refusing to capture or delete it")
                self._verify_source_original(record, source_fd, source_stat)
                lease = WriteLease(source_fd, "source")
                try:
                    lease.acquire()
                except LeaseBusy as exc:
                    self._cleanup_problem(
                        record, "SOURCE_CLEANUP_PENDING", str(exc))
                    return
                except (LeaseUnsupported, LeaseError) as exc:
                    self._cleanup_problem(
                        record, "SOURCE_CLEANUP_PENDING", str(exc))
                    return
                record["source_cleanup"]["lease"] = {
                    "type": "write", "acquired": True, "broken": False,
                    "target": "source",
                }
                self._capture_source(
                    record, parent_fd, entry_name, qfd, source_fd, source_stat,
                    lease)
                captured_stat = self._lstat_at(qfd, "source")
                if captured_stat is None:
                    raise _Reconcile(
                        "the captured entry vanished immediately after capture")
                source_identity = (
                    record["source"]["observed_version"].get("dev"),
                    record["source"]["observed_version"].get("ino"))
                if (captured_stat.st_dev, captured_stat.st_ino) != source_identity:
                    self._handle_foreign_capture(
                        record, source_fd, parent_fd, entry_name, qfd, root_fd)
                    return
                # Reuse the already-leased open file description.  Opening the
                # captured pathname again would be a second open description
                # and would self-break the kernel write lease.
                entry_fd = source_fd
                entry_stat = os.fstat(entry_fd)
                source_fd = None
                if (entry_stat.st_dev, entry_stat.st_ino) != source_identity:
                    raise _Reconcile(
                        "the captured inode does not match the held source FD")
                self._verify_captured_source(record, entry_fd, entry_stat)
                repopulated = self._check_original_after_capture(
                    record, parent_fd, entry_name)
                record["source_cleanup"]["original_path_repopulated"] = repopulated
                record["source_cleanup"]["state"] = "isolated"
                record["state"] = "SOURCE_ISOLATED"
                self._write_journal(record)
                self._fault("SOURCE_ISOLATED")

            # Target and authorization are rechecked with the target inode
            # held open.  Acquire an exclusive write lease on the target too:
            # this is a content-writer barrier, not a rename barrier, so the
            # pathname/root/parent checks below are still required.
            target_root_fd, target_parent_fd, target_fd, target_name, target_stat = \
                self._open_cleanup_target_held(record)
            target_lease = WriteLease(target_fd, "published target")
            try:
                target_lease.acquire()
            except LeaseBusy as exc:
                self._cleanup_problem(record, "NEEDS_RECONCILIATION", str(exc))
                return
            except (LeaseUnsupported, LeaseError) as exc:
                self._cleanup_problem(record, "NEEDS_RECONCILIATION", str(exc))
                return
            block["lease"]["target_acquired"] = True
            fresh_source, fresh_target = self._load_records(record)
            self._verify_cleanup_authorization(record, fresh_source)
            self._verify_source_anchor_paths(
                record, fresh_source, root_fd, parent_fd, root_stat)
            self._verify_target_anchor_paths(
                record, fresh_target, target_root_fd, target_parent_fd,
                target_fd, target_name)
            self._verify_registry_once(record)
            block["deletion_intent"] = {
                "target_dev": target_stat.st_dev,
                "target_ino": target_stat.st_ino,
                "target_sha256": record["published"].get("sha256"),
                "quarantine_dir_dev": block["quarantine"].get("dir_dev"),
                "quarantine_dir_ino": block["quarantine"].get("dir_ino"),
                "captured_dev": block["quarantine"].get("captured_dev"),
                "captured_ino": block["quarantine"].get("captured_ino"),
                "source_policy": fresh_source.get("cleanup_policy", "retain"),
                "at": self.clock(),
            }
            block["state"] = "deletion_intent"
            record["state"] = "SOURCE_DELETION_INTENT"
            self._write_journal(record)
            self._fault("SOURCE_DELETION_INTENT")

            # Final pinned checks use fresh registrations and fresh fstat data,
            # never the cached plan object or an earlier target stat.  Then the
            # only unlink of the captured private entry is performed.
            fresh_source, fresh_target = self._load_records(record)
            self._verify_cleanup_authorization(record, fresh_source)
            self._verify_source_anchor_paths(
                record, fresh_source, root_fd, parent_fd, root_stat)
            self._verify_target_anchor_paths(
                record, fresh_target, target_root_fd, target_parent_fd,
                target_fd, target_name)
            self._verify_registry_once(record)
            self._verify_target_held(record, target_fd, os.fstat(target_fd))
            observed_dir = self._lstat_at(
                root_fd, block["quarantine"]["dir_name"])
            if observed_dir is None or (observed_dir.st_dev, observed_dir.st_ino) != (
                    qstat.st_dev, qstat.st_ino):
                raise _Reconcile(
                    "the quarantine directory path was replaced before deletion")
            current_entry = self._lstat_at(qfd, "source")
            if current_entry is None or (current_entry.st_dev, current_entry.st_ino) != (
                    entry_stat.st_dev, entry_stat.st_ino):
                raise _Reconcile(
                    "the captured quarantine entry changed before deletion")
            # A hard link can be added anywhere on the same filesystem without
            # changing the quarantine name; revalidate the captured inode (and
            # in particular nlink == 1 plus its content hash) at this last
            # deletion boundary, not only immediately after capture.
            self._verify_captured_source(record, entry_fd, current_entry)
            self._check_original_after_capture(
                record, parent_fd, self._source_entry_name(record))
            if lease is not None:
                lease.assert_held()
            if target_lease is not None:
                target_lease.assert_held()
            os.unlink("source", dir_fd=qfd)
            os.fsync(qfd)
            os.fsync(root_fd)
            block["performed"] = True
            block["removed_at"] = self.clock()
            block["state"] = "source_removed"
            record["source_retained"] = False
            record["state"] = "SOURCE_REMOVED"
            self._write_journal(record)
            self._fault("SOURCE_REMOVED")
            self._remove_quarantine_dir(record, root_fd, qfd, qstat)
            self._cleanup_done(record)
        except _CleanupRefused as exc:
            self._cleanup_problem(record, "SOURCE_CLEANUP_PENDING", exc.reason)
        except _Reconcile as exc:
            self._cleanup_problem(
                record, "NEEDS_RECONCILIATION", exc.reason)
        except LeaseBroken as exc:
            self._cleanup_problem(record, "NEEDS_RECONCILIATION", str(exc))
        except (LeaseUnsupported, LeaseBusy, LeaseError) as exc:
            self._cleanup_problem(record, "SOURCE_CLEANUP_PENDING", str(exc))
        except _MediaUnavailable as exc:
            self._cleanup_problem(record, "NEEDS_RECONCILIATION", exc.reason)
        except OSError as exc:
            self._cleanup_problem(
                record, "NEEDS_RECONCILIATION",
                f"filesystem error during controlled cleanup: {exc}")
        finally:
            if target_lease is not None:
                target_lease.release()
            if lease is not None:
                lease.release()
            for descriptor in (entry_fd, source_fd, qfd, parent_fd, root_fd,
                               target_fd, target_parent_fd, target_root_fd):
                if descriptor is not None and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _finish_after_removal(self, record: dict, root_fd: int, qfd: int,
                              qstat: os.stat_result) -> None:
        block = record["source_cleanup"]
        block["performed"] = True
        block["removed_at"] = block.get("removed_at") or self.clock()
        block["state"] = "source_removed"
        record["source_retained"] = False
        record["state"] = "SOURCE_REMOVED"
        self._write_journal(record)
        self._fault("SOURCE_REMOVED")
        self._remove_quarantine_dir(record, root_fd, qfd, qstat)
        self._cleanup_done(record)

    # ----- registry commit and terminal -----------------------------------
    def _registry_record(self, record: dict) -> dict:
        return {
            "schema_version": REGISTRY_SCHEMA_VERSION,
            "operation_uuid": record["operation_uuid"],
            "operation": record["operation"],
            "source": {
                key: record["source"].get(key)
                for key in ("root_uuid", "management_domain", "access_policy",
                            "locator", "relative_path", "root_dev", "root_ino",
                            "parent_parts")
            },
            "target": {
                key: record["target"].get(key)
                for key in ("root_uuid", "management_domain", "access_policy",
                            "locator", "relative_path", "root_dev", "root_ino",
                            "parent_parts")
            },
            "published": {
                key: record["published"].get(key)
                for key in ("dev", "ino", "size", "sha256")
            },
            "management_domain": record["target"]["management_domain"],
            "provenance": dict(record["provenance"]),
            "registered_at": self.clock(),
        }

    @staticmethod
    def _same_registry(existing: dict, candidate: dict) -> bool:
        for key in ("operation", "operation_uuid", "management_domain"):
            if existing.get(key) != candidate.get(key):
                return False
        for side in ("source", "target"):
            for key in ("root_uuid", "relative_path"):
                if existing.get(side, {}).get(key) != candidate.get(side, {}).get(key):
                    return False
        for key in ("dev", "ino", "size", "sha256"):
            if existing.get("published", {}).get(key) != candidate.get(
                    "published", {}).get(key):
                return False
        return True

    def _commit_registry(self, record: dict, *, require_existing: bool) -> None:
        candidate = self._registry_record(record)
        with self.registry_collection.locked(True):
            records = self.registry_collection.read_unlocked()
            existing = next(
                (item for item in records
                 if item["operation_uuid"] == record["operation_uuid"]), None)
            if existing is not None:
                if not self._same_registry(existing, candidate):
                    raise _Reconcile(
                        "operation UUID is already registered for a different target; "
                        "refusing duplicate or conflicting registration")
            elif require_existing:
                raise _Reconcile(
                    "journal says REGISTRY_COMMITTED but the operation registration is "
                    "missing; refusing to invent a new claim")
            else:
                records.append(candidate)
                self.registry_collection.write_unlocked(records)
        record["registry"] = {
            "committed": True,
            "committed_at": self.clock(),
        }
        if (record["state"] != "REGISTRY_COMMITTED"
                and record["state"] not in CLEANUP_STATES):
            self._transition(record, "REGISTRY_COMMITTED")

    def _terminal(self, record: dict) -> None:
        now = self.clock()
        record["finished_at"] = now
        record["stage"]["cleaned"] = True
        record["published"]["nlink"] = 1
        if record["operation"] == "copy":
            record["source_retained"] = True
            record["source_retention_reason"] = (
                "copy does not delete its source; source cleanup is only part of an "
                "explicitly authorized controlled move")
            record["complete"] = True
            self._transition(record, "DONE")
            return
        if self._cleanup_authorized_pinned(record):
            cleanup = record.setdefault("source_cleanup", {})
            cleanup["state"] = "intent"
            cleanup["reason"] = (
                "target published and registered; bounded controlled source cleanup "
                "is authorized and has not completed yet")
            record["complete"] = False
            record["copy_complete"] = True
            record["source_retained"] = True
            record["source_retention_reason"] = (
                "controlled cleanup is authorized; the source is retained until the "
                "captured inode passes every durable check")
            self._transition(record, "SOURCE_CLEANUP_INTENT")
            return
        record["complete"] = False
        record["copy_complete"] = True
        record["source_retained"] = True
        record["source_retention_reason"] = (
            "move without an explicit per-operation --cleanup-source authorization "
            "and a controlled source-root cleanup policy: the target was copied and "
            "registered, the source is retained, and SOURCE_CLEANUP_PENDING is "
            "intentional")
        cleanup = record.setdefault("source_cleanup", {})
        cleanup.update({
            "requested": bool(cleanup.get("requested")),
            "authorized": False,
            "mode": "retain",
            "state": "refused",
            "performed": False,
            "reason": "no pinned controlled cleanup authorization for this operation",
        })
        self._transition(record, "SOURCE_CLEANUP_PENDING")

    def _verify_published_ownership(self, record: dict, *,
                                    require_stage: bool,
                                    verify_content: bool = False) -> None:
        """Prove the target path is still the published and staged inode."""
        published = record["published"]
        if published.get("dev") is None or published.get("ino") is None:
            raise _Reconcile("journal has no published inode identity to verify")
        root_fd = parent_fd = None
        try:
            root_fd, parent_fd, _parent_stat, target_name = self._open_target(
                record, recovery=True)
            target_stat = self._lstat_at(parent_fd, target_name)
            if target_stat is None:
                raise _Reconcile(
                    "published target path is missing; refusing to claim a "
                    "replacement by name or hash")
            if (target_stat.st_dev, target_stat.st_ino) != (
                    published["dev"], published["ino"]):
                raise _Reconcile(
                    "published target path no longer names the recorded inode; "
                    "refusing to claim an unrelated replacement")
            if verify_content:
                target_fd = os.open(target_name, _READ_FLAGS, dir_fd=parent_fd)
                try:
                    opened = os.fstat(target_fd)
                    if (opened.st_dev, opened.st_ino) != (
                            target_stat.st_dev, target_stat.st_ino):
                        raise _Reconcile(
                            "published target changed while it was being verified")
                    digest, size = _hash_fd(target_fd)
                finally:
                    os.close(target_fd)
                if (size != published.get("size")
                        or digest != published.get("sha256")):
                    raise _Reconcile(
                        "published target content no longer matches the recorded "
                        "hash and size; refusing to register or claim it")
            stage_name = record["stage"].get("name")
            stage_stat = self._lstat_at(parent_fd, stage_name) if stage_name else None
            if stage_stat is not None:
                if not self._stage_identity_matches(record, stage_stat):
                    raise _Reconcile(
                        "staging path was replaced by an unrelated object; "
                        "refusing to claim or unlink it")
                if (stage_stat.st_dev, stage_stat.st_ino) != (
                        target_stat.st_dev, target_stat.st_ino):
                    raise _Reconcile(
                        "target and staging are no longer the same inode; refusing "
                        "to register the publication")
            elif require_stage:
                raise _Reconcile(
                    "staging inode evidence is missing before registration; refusing "
                    "to register a target that cannot be tied to this operation")
        finally:
            for descriptor in (parent_fd, root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _finish_committed(self, record: dict, *,
                          verify_content: bool = False) -> None:
        if verify_content:
            self._verify_published_ownership(
                record, require_stage=False, verify_content=True)
        self._commit_registry(record, require_existing=True)
        target = record["target"]
        parent_fd = None
        stage_name = record["stage"].get("name")
        try:
            root_fd, parent_fd, _parent_stat, target_name = self._open_target(
                record, recovery=True)
            try:
                target_stat = self._lstat_at(parent_fd, target_name)
                if target_stat is None:
                    raise _Reconcile(
                        "registered target is missing; bytes are not claimed by hash")
                published = record["published"]
                if (target_stat.st_dev, target_stat.st_ino) != (
                        published["dev"], published["ino"]):
                    raise _Reconcile(
                        "registered target path no longer names the published inode; "
                        "refusing to claim a replacement")
                if stage_name:
                    stage_stat = self._lstat_at(parent_fd, stage_name)
                    if stage_stat is not None:
                        if not self._stage_identity_matches(record, stage_stat):
                            raise _Reconcile(
                                "staging path was replaced by an unrelated object; "
                                "refusing to unlink it")
                        if (stage_stat.st_dev, stage_stat.st_ino) != (
                                target_stat.st_dev, target_stat.st_ino):
                            raise _Reconcile(
                                "staging path and published target are no longer the "
                                "same inode; refusing to unlink")
                        self._unlink_at(parent_fd, stage_name)
                current = self._lstat_at(parent_fd, target_name)
                if current is None or (current.st_dev, current.st_ino) != (
                        published["dev"], published["ino"]):
                    raise _Reconcile(
                        "published target changed during staging cleanup")
                if current.st_nlink != 1:
                    raise _Reconcile(
                        "published target still has extra hard links; refusing to "
                        "declare a clean terminal state")
            finally:
                os.close(parent_fd)
            os.close(root_fd)
        except BaseException:
            if parent_fd is not None:
                try:
                    os.close(parent_fd)
                except OSError:
                    pass
            raise
        self._fault("STAGE_CLEANED")
        self._terminal(record)
        if record.get("state") in CLEANUP_STATES:
            self._cleanup_run(record)

    # ----- initial operation ----------------------------------------------
    def copy_file(self, source_root_uuid: str, source_relative: str,
                  target_root_uuid: str, target_relative: str, *,
                  fault_hook=None) -> dict:
        return self._run("copy", source_root_uuid, source_relative,
                         target_root_uuid, target_relative,
                         fault_hook=fault_hook)

    def move_file(self, source_root_uuid: str, source_relative: str,
                  target_root_uuid: str, target_relative: str, *,
                  cleanup_source: bool = False, fault_hook=None) -> dict:
        return self._run("move", source_root_uuid, source_relative,
                         target_root_uuid, target_relative,
                         cleanup_source=cleanup_source,
                         fault_hook=fault_hook)

    def _run(self, operation: str, source_root_uuid: str, source_relative: str,
             target_root_uuid: str, target_relative: str, *,
             cleanup_source: bool = False, fault_hook=None) -> dict:
        previous_hook = self.fault_hook
        if fault_hook is not None:
            self.fault_hook = fault_hook
        try:
            return self._run_locked(
                operation, source_root_uuid, source_relative,
                target_root_uuid, target_relative,
                cleanup_source=cleanup_source)
        finally:
            self.fault_hook = previous_hook

    def _run_locked(self, operation: str, source_root_uuid: str,
                    source_relative: str, target_root_uuid: str,
                    target_relative: str, *, cleanup_source: bool = False) -> dict:
        source_record, target_record, source_parts, target_parts = \
            self._validate_request(
                source_root_uuid, source_relative, target_root_uuid, target_relative)
        cleanup_requested = bool(cleanup_source and operation == "move")
        if cleanup_requested:
            self._preflight_cleanup(source_record)
        operation_uuid = str(uuid.uuid4())
        record = None
        lock_exclusive = bool(cleanup_requested)
        with self._operation_lock(operation_uuid), self._root_lock(
                source_record["root_uuid"], exclusive=lock_exclusive):
            source_root_fd = source_parent_fd = source_fd = None
            target_root_fd = target_parent_fd = None
            stage_fd = -1
            try:
                source_root_fd, source_parent_fd, source_fd, source_stat = \
                    self._open_source_for_new(source_record, source_parts)
                source_view = self._inspect_source(source_fd, source_relative)
                target_root_fd, target_parent_fd, parent_stat, target_name = \
                    self._open_target_for_new(record=None, target_record=target_record,
                                              target_parts=target_parts)
                record = self._new_record(
                    operation, source_record, source_relative, source_parts,
                    target_record, target_relative, target_parts, source_view,
                    parent_stat, source_parent_stat=os.fstat(source_parent_fd),
                    cleanup_requested=cleanup_requested,
                    cleanup_authorized=cleanup_requested)
                self._transition(record, "INTENT_DURABLE")
                if self._lstat_at(target_parent_fd, target_name) is not None:
                    raise _Conflict(
                        "target already exists; this operation preserves it and does "
                        "not overwrite")
                self._plan_stage(record)
                self._transition(record, "STAGING")
                stage_fd = self._create_stage(record, target_parent_fd)
                self._copy_to_stage(record, source_fd, stage_fd, target_parent_fd)
                stage_fd = -1
                self._publish(record, target_parent_fd, record["stage"]["name"],
                              target_name)
                self._verify_published_ownership(
                    record, require_stage=True, verify_content=True)
                self._commit_registry(record, require_existing=False)
                # The controlled-cleanup lease fails while any second open
                # description of the source inode exists.  The copy is done, so
                # close our copy source FD before the cleanup phase.
                for descriptor in (source_fd, source_parent_fd, source_root_fd):
                    if descriptor is not None and descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
                source_fd = source_parent_fd = source_root_fd = None
                self._finish_committed(record)
            except _Conflict as exc:
                if record is None:
                    raise MultilinuxError(str(exc)) from exc
                self._record_problem(record, "CONFLICT", exc.reason)
            except _Reconcile as exc:
                if record is None:
                    raise MultilinuxError(str(exc)) from exc
                self._record_problem(record, "NEEDS_RECONCILIATION", exc.reason)
            except _MediaUnavailable as exc:
                if record is None:
                    raise MultilinuxError(str(exc)) from exc
                self._record_problem(record, "CONFLICT", exc.reason)
            except OSError as exc:
                if record is None:
                    raise MultilinuxError(str(exc)) from exc
                self._record_problem(
                    record, "NEEDS_RECONCILIATION",
                    f"filesystem error after the operation became durable: {exc}")
            finally:
                for descriptor in (stage_fd, source_fd, source_parent_fd,
                                   source_root_fd, target_parent_fd, target_root_fd):
                    if descriptor is not None and descriptor >= 0:
                        try:
                            os.close(descriptor)
                        except OSError:
                            pass
        return self._public(record) if record is not None else record

    def _open_source_for_new(self, source_record: dict, parts: list[str]):
        absolute = os.path.join(source_record["locator"], *parts)
        self._check_nested(source_record, absolute, "source")
        root_fd, root_stat, root_mount = self._open_root_fd(source_record, "source")
        try:
            parent_fd = self._walk_parent(
                root_fd, root_stat, root_mount, parts, "source")
            source_fd = None
            try:
                source_fd = os.open(parts[-1], _READ_FLAGS, dir_fd=parent_fd)
                observed = os.fstat(source_fd)
                if _mount_id(source_fd) != root_mount:
                    raise MultilinuxError(
                        "source file crosses a mount boundary inside the data root")
                if observed.st_dev != root_stat.st_dev:
                    raise MultilinuxError(
                        "source file crosses a device boundary inside the data root")
                return root_fd, parent_fd, source_fd, observed
            except BaseException:
                if source_fd is not None:
                    try:
                        os.close(source_fd)
                    except OSError:
                        pass
                os.close(parent_fd)
                raise
        except BaseException:
            os.close(root_fd)
            raise

    def _open_target_for_new(self, *, record, target_record: dict,
                             target_parts: list[str]):
        root_fd, root_stat, root_mount = self._open_root_fd(target_record, "target")
        try:
            parent_fd = self._walk_parent(
                root_fd, root_stat, root_mount, target_parts, "target")
            parent_stat = os.fstat(parent_fd)
            return root_fd, parent_fd, parent_stat, target_parts[-1]
        except BaseException:
            os.close(root_fd)
            raise

    # ----- recovery -------------------------------------------------------
    def _load_records(self, record: dict):
        source = record["source"]
        target = record["target"]
        fresh_source = self._root_record(
            source["root_uuid"], "source", recovery=True)
        fresh_target = self._root_record(
            target["root_uuid"], "target", recovery=True)
        if (fresh_source["root_dev"], fresh_source["root_ino"]) != (
                source["root_dev"], source["root_ino"]):
            raise _Reconcile("source data root registration identity changed")
        if fresh_source["locator"] != source["locator"]:
            raise _Reconcile("source data root locator changed")
        if target["access_policy"] == "read-only" or fresh_target["access_policy"] != "read-write":
            raise _Reconcile("target data root policy no longer permits publication")
        if (fresh_target["root_dev"], fresh_target["root_ino"]) != (
                target["root_dev"], target["root_ino"]):
            raise _Reconcile("target data root registration identity changed")
        if fresh_target["locator"] != target["locator"]:
            raise _Reconcile("target data root locator changed")
        self._source_view_check(fresh_source)
        return fresh_source, fresh_target

    def _resume_before_publish(self, record: dict) -> None:
        source_record, target_record = self._load_records(record)
        source = record["source"]
        target = record["target"]
        source_root_fd = source_parent_fd = source_fd = None
        target_root_fd = target_parent_fd = None
        stage_writable = -1
        try:
            source_root_fd, source_parent_fd, source_fd, source_stat = self._open_source(
                record, recovery=True)
            if not _same_version(source["observed_version"], source_stat):
                raise _Conflict(
                    "source identity, size or timestamps differ from INTENT_DURABLE; "
                    "source and staging are retained")
            self._inspect_source(source_fd, source["relative_path"])
            target_root_fd, target_parent_fd, parent_stat, target_name = \
                self._open_target(record, recovery=True)
            stage_name = record["stage"].get("name")
            target_stat = self._lstat_at(target_parent_fd, target_name)
            stage_stat = (self._lstat_at(target_parent_fd, stage_name)
                          if stage_name else None)
            if target_stat is not None:
                if (stage_name and stage_stat is not None
                        and self._stage_identity_matches(record, stage_stat)
                        and (target_stat.st_dev, target_stat.st_ino) == (
                            stage_stat.st_dev, stage_stat.st_ino)):
                    # The process exited after linkat but before the journal
                    # recorded TARGET_PUBLISHED.  The stage inode is the
                    # operation-specific ownership evidence.
                    record["published"] = {
                        "dev": target_stat.st_dev,
                        "ino": target_stat.st_ino,
                        "size": target_stat.st_size,
                        "sha256": record["stage"].get("sha256"),
                        "nlink": target_stat.st_nlink,
                        "published_at": self.clock(),
                    }
                    self._transition(record, "TARGET_PUBLISHED")
                    self._verify_published_ownership(
                        record, require_stage=True, verify_content=True)
                    self._commit_registry(record, require_existing=False)
                    for descriptor in (source_fd, source_parent_fd, source_root_fd):
                        if descriptor is not None and descriptor >= 0:
                            try:
                                os.close(descriptor)
                            except OSError:
                                pass
                    source_fd = source_parent_fd = source_root_fd = None
                    self._finish_committed(record)
                    return
                raise _Conflict(
                    "target already exists and is not this operation's exact staging "
                    "inode; target and source are retained")
            if stage_stat is not None:
                if not self._stage_identity_matches(record, stage_stat):
                    raise _Reconcile(
                        "staging path was replaced by an unrelated object; refusing "
                        "to claim or unlink it")
                if record["stage"].get("ready"):
                    stage_fd_read = os.open(stage_name, _READ_FLAGS,
                                            dir_fd=target_parent_fd)
                    try:
                        stage_observed = os.fstat(stage_fd_read)
                        if not self._stage_identity_matches(record, stage_observed):
                            raise _Reconcile("staging inode changed")
                        stage_digest, stage_size = _hash_fd(stage_fd_read)
                    finally:
                        os.close(stage_fd_read)
                    if (stage_size != record["stage"].get("size")
                            or stage_digest != record["stage"].get("sha256")):
                        raise _Reconcile(
                            "staging content no longer matches its persisted hash; "
                            "refusing to publish it")
                    source_digest, source_size = _hash_fd(source_fd)
                    if (source_digest != record["stage"].get("sha256")
                            or source_size != stage_size):
                        raise _Conflict(
                            "source content changed after staging; source and staging "
                            "are retained")
                    record["source"]["sha256"] = source_digest
                    record["source"]["verified_size"] = source_size
                else:
                    # The journaling order is STAGING before O_EXCL creation, so a
                    # partial stage with a recorded inode is provably this operation's
                    # own file.  With the target absent and the source unchanged it is
                    # safe to remove that exact inode and retry on a fresh nonce.
                    if stage_stat.st_size > source["observed_version"]["size"]:
                        raise _Reconcile(
                            "partial staging file is larger than the recorded source; "
                            "preserving it for reconciliation")
                    self._unlink_at(target_parent_fd, stage_name)
                    record["stage"]["removed_for_retry"] = True
                    self._plan_stage(record)
                    self._write_journal(record)
                    stage_stat = None
            elif record["stage"].get("dev") is not None:
                raise _Reconcile(
                    "the recorded staging inode is missing; refusing to claim a "
                    "same-named or same-content replacement")
            if record["state"] != "STAGING":
                self._plan_stage(record)
                self._transition(record, "STAGING")
            if record["stage"].get("dev") is None:
                stage_writable = self._create_stage(record, target_parent_fd)
            if not record["stage"].get("ready"):
                if stage_writable < 0:
                    stage_writable = os.open(
                        record["stage"]["name"],
                        os.O_WRONLY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=target_parent_fd)
                    observed = os.fstat(stage_writable)
                    if not self._stage_identity_matches(record, observed):
                        raise _Reconcile(
                            "staging inode changed before resuming copy")
                    os.ftruncate(stage_writable, 0)
                self._copy_to_stage(
                    record, source_fd, stage_writable, target_parent_fd)
                stage_writable = -1
            self._publish(record, target_parent_fd, record["stage"]["name"],
                          target_name)
            self._verify_published_ownership(
                record, require_stage=True, verify_content=True)
            self._commit_registry(record, require_existing=False)
            for descriptor in (source_fd, source_parent_fd, source_root_fd):
                if descriptor is not None and descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            source_fd = source_parent_fd = source_root_fd = None
            self._finish_committed(record)
        finally:
            if stage_writable is not None and stage_writable >= 0:
                try:
                    os.close(stage_writable)
                except OSError:
                    pass
            for descriptor in (source_fd, source_parent_fd, source_root_fd,
                               target_parent_fd, target_root_fd):
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass

    def _recover_locked(self, record: dict) -> tuple[dict, str]:
        state = record["state"]
        if state in TERMINAL_STATES:
            return record, "terminal-noop"
        if state == "INTENT_DURABLE" or state == "STAGING":
            self._resume_before_publish(record)
            return record, "resumed-before-publish"
        if state == "TARGET_PUBLISHED":
            self._load_records(record)
            self._verify_published_ownership(
                record, require_stage=True, verify_content=True)
            self._commit_registry(record, require_existing=False)
            self._finish_committed(record)
            return record, "committed-after-publish"
        if state == "REGISTRY_COMMITTED":
            self._load_records(record)
            self._finish_committed(record, verify_content=True)
            return record, "finished-after-commit"
        if state in CLEANUP_STATES:
            self._cleanup_run(record)
            return record, "controlled-cleanup"
        return record, "manual-review-noop"

    def recover(self, operation_uuid: str | None = None) -> list[dict]:
        if operation_uuid is not None:
            operation_uuid = _canonical_uuid(operation_uuid, "operation_uuid")
            identifiers = [operation_uuid]
        else:
            self._ensure_directories()
            identifiers = sorted(
                path.stem for path in self.operations.glob("*.json")
                if path.is_file() and len(path.stem) == 36)
        results = []
        for identifier in identifiers:
            with self._operation_lock(identifier):
                record = self._read_journal(identifier)
                if operation_uuid is None and record["state"] in TERMINAL_STATES:
                    # A bare `recover` means all still-pending operations.
                    # Terminal records stay immutable and are inspected with
                    # status/list or an explicit UUID.
                    continue
                state_before = record["state"]
                try:
                    needs_exclusive = (
                        state_before in CLEANUP_STATES
                        or (record.get("operation") == "move"
                            and self._cleanup_authorized_pinned(record)
                            and state_before not in TERMINAL_STATES))
                    with self._root_lock(
                            record["source"]["root_uuid"],
                            exclusive=needs_exclusive):
                        record, action = self._recover_locked(record)
                except _Conflict as exc:
                    record = self._record_problem(record, "CONFLICT", exc.reason)
                    action = "conflict-retained"
                except _Reconcile as exc:
                    record = self._record_problem(
                        record, "NEEDS_RECONCILIATION", exc.reason)
                    action = "needs-reconciliation"
                except _MediaUnavailable as exc:
                    record = self._record_problem(record, "CONFLICT", exc.reason)
                    action = "media-missing-retained"
                except OSError as exc:
                    record = self._record_problem(
                        record, "NEEDS_RECONCILIATION",
                        f"filesystem error during recovery: {exc}")
                    action = "filesystem-error"
                result = self._public(record)
                result["recovery"] = {
                    "action": action,
                    "state_before": state_before,
                    "state_after": record["state"],
                }
                results.append(result)
        return results

    # ----- read-only views -------------------------------------------------
    def _public(self, record: dict) -> dict:
        result = json.loads(json.dumps(record))
        result["complete"] = record.get("state") == "DONE"
        result["source_retained"] = bool(record.get("source_retained", True))
        result["real_paths"] = {
            "source": os.path.join(
                record["source"]["locator"], record["source"]["relative_path"]),
            "target": os.path.join(
                record["target"]["locator"], record["target"]["relative_path"]),
            "stage": (os.path.join(
                record["target"]["locator"], *record["target"]["parent_parts"],
                record["stage"]["name"])
                if record["stage"].get("name") else None),
        }
        result["management_domain"] = record["target"]["management_domain"]
        result.setdefault("execution", {
            "authorized": False,
            "interpreter": None,
            "environment": None,
            "note": "no source execution authorization is inherited by this copy",
        })
        result["provenance"] = dict(record["provenance"])
        result["no_overwrite"] = True
        result["overwrite_mechanism"] = (
            "O_EXCL staging in the target directory plus same-filesystem linkat")
        result["registry_transaction_note"] = (
            "filesystem publication and the operation registration are separate "
            "durable steps; recovery verifies the recorded inode instead of assuming "
            "one cross-system transaction")
        cleanup = record.get("source_cleanup")
        if isinstance(cleanup, dict):
            quarantine = cleanup.get("quarantine") or {}
            result["quarantine_path"] = (
                os.path.join(record["source"]["locator"],
                             quarantine["dir_name"])
                if quarantine.get("dir_name") else None)
            result["source_cleanup_state"] = cleanup.get("state")
        if record.get("state") == "SOURCE_CLEANUP_PENDING":
            result["target_copied"] = True
        return result

    def list(self) -> list[dict]:
        return self.status()

    def status(self, operation_uuid: str | None = None) -> list[dict]:
        if operation_uuid is not None:
            identifier = _canonical_uuid(operation_uuid, "operation_uuid")
            identifiers = [identifier]
        else:
            self._ensure_directories()
            identifiers = sorted(
                path.stem for path in self.operations.glob("*.json")
                if path.is_file() and len(path.stem) == 36)
        results = []
        for identifier in identifiers:
            # Journals are atomically replaced; a status read does not wait for
            # a long copy that still holds the operation lock.
            record = self._read_journal(identifier)
            results.append(self._public(record))
        return results

    def operation(self, operation_uuid: str) -> dict:
        identifier = _canonical_uuid(operation_uuid, "operation_uuid")
        record = self._read_journal(identifier)
        return self._public(record)
