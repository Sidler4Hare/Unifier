"""Persisted journal schema and checks for one regular-file operation.

State names, schema versions and quarantine field rules stay here so a caller
can validate a record without running a copy or a cleanup. This is not a
generic transaction framework.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid
from zoneinfo import ZoneInfo

from unifier.atomic_io import atomic_replace
from unifier.errors import UnifierError


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


def _close_fds(*descriptors: int | None) -> None:
    for descriptor in descriptors:
        if descriptor is not None and descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _now() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _canonical_uuid(value, label: str) -> str:
    if not isinstance(value, str):
        raise UnifierError(f"{label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UnifierError(f"{label} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise UnifierError(f"{label} must be the canonical lowercase UUID form")
    return value


def _validate_relative(relative_path: str, label: str) -> list[str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise UnifierError(f"{label} must be a non-empty string")
    if relative_path.startswith("/"):
        raise UnifierError(f"{label} must not be absolute")
    if "\x00" in relative_path or "\n" in relative_path:
        raise UnifierError(f"{label} contains an invalid character")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise UnifierError(
            f"{label} must not contain empty, '.' or '..' components")
    return parts


def _mount_id(descriptor: int) -> int:
    path = Path(f"/proc/self/fdinfo/{descriptor}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UnifierError(
            "cannot verify the mount boundary: /proc/self/fdinfo is unavailable "
            f"({exc}); refusing the operation") from exc
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
            break
    raise UnifierError(
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
    data = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
    atomic_replace(path, data, prefix=f".{path.name}.tmp-")


def _read_json(path: Path, label: str) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnifierError(f"{label} is unreadable: {exc}") from exc
    if not isinstance(payload, dict):
        raise UnifierError(f"{label} must be a JSON object")
    return payload


def _require(record: dict, key: str, label: str):
    value = record.get(key)
    if value is None or value == "":
        raise UnifierError(f"{label} is missing field {key}")
    return value


def _validate_plan_side(side, label: str) -> dict:
    if not isinstance(side, dict):
        raise UnifierError(f"{label} must be an object")
    for key in ("root_uuid", "management_domain", "access_policy", "locator",
                "relative_path"):
        _require(side, key, label)
    _canonical_uuid(side["root_uuid"], f"{label} root_uuid")
    _validate_relative(side["relative_path"], f"{label} relative_path")
    if (not isinstance(side["locator"], str) or not side["locator"].startswith("/")
            or "\x00" in side["locator"] or "\n" in side["locator"]):
        raise UnifierError(f"{label} locator must be an absolute path")
    if side["access_policy"] not in {"read-only", "read-write"}:
        raise UnifierError(f"{label} has an unknown access policy")
    for key in ("root_dev", "root_ino"):
        if isinstance(side.get(key), bool) or not isinstance(side.get(key), int):
            raise UnifierError(f"{label} {key} must be an integer")
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
        raise UnifierError(f"{label} parent_parts is invalid")
    return side


def _validate_cleanup_block(block) -> dict:
    if not isinstance(block, dict):
        raise UnifierError("journal source_cleanup must be an object")
    for key in ("requested", "authorized", "performed"):
        if key in block and not isinstance(block[key], bool):
            raise UnifierError(f"journal source_cleanup.{key} must be a boolean")
    mode = block.get("mode")
    if mode is not None and mode not in {"retain", "controlled"}:
        raise UnifierError("journal source_cleanup.mode is invalid")
    policy = block.get("policy_snapshot")
    if policy is not None and policy not in {"retain", "controlled"}:
        raise UnifierError("journal source_cleanup.policy_snapshot is invalid")
    quarantine = block.get("quarantine")
    if quarantine is not None:
        if not isinstance(quarantine, dict):
            raise UnifierError("journal source_cleanup.quarantine must be an object")
        name = quarantine.get("dir_name")
        if name is not None and (
                not isinstance(name, str) or not name or "/" in name
                or "\x00" in name or name in {".", ".."}):
            raise UnifierError("journal quarantine directory name is invalid")
        entry = quarantine.get("entry_name")
        if entry is not None and entry not in {"source"}:
            raise UnifierError("journal quarantine entry name is invalid")
        for key in ("dir_dev", "dir_ino", "captured_dev", "captured_ino",
                    "captured_size"):
            value = quarantine.get(key)
            if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)):
                raise UnifierError(
                    f"journal quarantine {key} must be an integer or null")
    captured = block.get("captured")
    if captured is not None and not isinstance(captured, dict):
        raise UnifierError("journal source_cleanup.captured must be an object")
    return block


def _validate_journal(record: dict) -> dict:
    if not isinstance(record, dict):
        raise UnifierError("file operation journal must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnifierError("file operation journal schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise UnifierError(
            f"file operation journal schema_version {version!r} is not supported "
            f"(this build understands {SCHEMA_VERSION})")
    _canonical_uuid(_require(record, "operation_uuid", "journal"), "operation_uuid")
    if record.get("operation") not in OPERATIONS:
        raise UnifierError(f"unknown file operation: {record.get('operation')!r}")
    if record.get("state") not in STATES:
        raise UnifierError(f"unknown file operation state: {record.get('state')!r}")
    _validate_plan_side(record.get("source"), "journal source")
    _validate_plan_side(record.get("target"), "journal target")
    stage = record.get("stage")
    if not isinstance(stage, dict):
        raise UnifierError("journal stage must be an object")
    if stage.get("name") is not None:
        name = stage["name"]
        if (not isinstance(name, str) or not name or "/" in name
                or "\x00" in name or name in {".", ".."}):
            raise UnifierError("journal stage name is invalid")
    if stage.get("nonce") is not None and not isinstance(stage["nonce"], str):
        raise UnifierError("journal stage nonce must be a string")
    for key in ("dev", "ino", "size"):
        value = stage.get(key)
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)):
            raise UnifierError(f"journal stage {key} must be an integer or null")
    if not isinstance(record.get("published"), dict):
        raise UnifierError("journal published must be an object")
    if not isinstance(record.get("registry"), dict):
        raise UnifierError("journal registry must be an object")
    if "execution" in record and not isinstance(record["execution"], dict):
        raise UnifierError("journal execution must be an object")
    if not isinstance(record.get("capabilities"), dict):
        raise UnifierError("journal capabilities must be an object")
    if "source_cleanup" in record:
        _validate_cleanup_block(record["source_cleanup"])
    if not isinstance(record.get("unsupported"), list):
        raise UnifierError("journal unsupported must be a list")
    if record.get("state") in CLEANUP_STATES:
        block = record.get("source_cleanup")
        if not isinstance(block, dict) or block.get("authorized") is not True:
            raise UnifierError(
                "a cleanup-state journal has no pinned source-cleanup authorization")
    if not isinstance(record.get("created_at"), str) or not isinstance(
            record.get("updated_at"), str):
        raise UnifierError("journal timestamps must be strings")
    return record


def _validate_registry_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise UnifierError("file operation registry record must be an object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnifierError("file operation registry schema_version must be an integer")
    if version != REGISTRY_SCHEMA_VERSION:
        raise UnifierError(
            f"file operation registry schema_version {version!r} is not supported")
    _canonical_uuid(_require(record, "operation_uuid", "registry record"),
                    "operation_uuid")
    if record.get("operation") not in OPERATIONS:
        raise UnifierError("registry record has an unknown operation")
    _validate_plan_side(record.get("source"), "registry source")
    _validate_plan_side(record.get("target"), "registry target")
    published = record.get("published")
    if not isinstance(published, dict):
        raise UnifierError("registry published must be an object")
    for key in ("dev", "ino", "size"):
        if isinstance(published.get(key), bool) or not isinstance(published.get(key), int):
            raise UnifierError(f"registry published {key} must be an integer")
    sha = published.get("sha256")
    if not isinstance(sha, str) or len(sha) != 64:
        raise UnifierError("registry published sha256 must be 64 hex digits")
    for key in ("registered_at", "management_domain"):
        if not isinstance(record.get(key), str) or not record[key]:
            raise UnifierError(f"registry record {key} must be a non-empty string")
    if not isinstance(record.get("provenance"), dict):
        raise UnifierError("registry provenance must be an object")
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
