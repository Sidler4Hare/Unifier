"""Stopped-view records and liveness shared by lifecycle and file operations."""

from __future__ import annotations

from pathlib import Path

from unifier.errors import UnifierError
from unifier.record_ids import validate_uuid
from unifier.identity import validate_binding
from unifier.json_records import JsonRecordCollection
from unifier.process_identity import process_stamp
from unifier.view_journal import read_journal


SCHEMA_VERSION = 1
STATES = {"starting", "ready", "revoking", "failed", "stale", "needs-reconciliation"}
PID_FIELDS = ("owner_pid", "worker_pid", "broker_pid", "broker_launcher_pid")
STAMP_FIELDS = ("owner_stamp", "worker_stamp", "broker_stamp", "broker_launcher_stamp")


def _canonical_uuid(value, label):
    return validate_uuid(value, f"view {label}", form="canonical lowercase UUID")


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
