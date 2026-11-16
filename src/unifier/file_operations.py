"""Durable managed regular-file copy and restricted move.

This is the first D-stage product path: one regular file from a registered
source data root into a registered read-write target data root.  Paths are
never taken as bare host strings; both sides are selected by the registered
``DataRootRegistry`` identity and walked from an anchored root FD with
``O_NOFOLLOW`` on every component.

The journal is deliberately per operation.  A long copy never holds the global
operation registry lock.  Publication uses an O_EXCL staging file in the target
directory and a same-filesystem ``linkat`` (``os.link``) without overwrite.
By default a move retains the source at ``SOURCE_CLEANUP_PENDING``.  Explicit
``cleanup_source`` authorization on a controlled root enables pinned source
capture and deletion, with durable recovery at every cleanup stage.

Journal checks, target publication and source cleanup live in
``file_operation_journal``, ``file_operation_publish`` and
``file_operation_cleanup``. State names, lock order and failure windows are
unchanged. This module does not claim a cross-system transaction between the
filesystem and the operation registry, a content-addressed CAS, full metadata
fidelity, or an atomic cross-filesystem move.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import uuid

from unifier.errors import UnifierError
from unifier.file_operation_cleanup import _SourceCleanup
from unifier.file_operation_journal import (
    CAPABILITIES,
    CLEANUP_STATES,
    REGISTRY_SCHEMA_VERSION,
    SCHEMA_VERSION,
    TERMINAL_STATES,
    UNSUPPORTED,
    _Conflict,
    _DIR_FLAGS,
    _MediaUnavailable,
    _READ_FLAGS,
    _Reconcile,
    _atomic_write_json,
    _canonical_uuid,
    _close_fds,
    _mount_id,
    _now,
    _read_json,
    _root_matches,
    _stat_version,
    _validate_journal,
    _validate_registry_record,
    _validate_relative,
)
from unifier.file_operation_publish import _TargetPublish
from unifier.json_records import JsonRecordCollection
from unifier.registry import DataRootRegistry
from unifier.file_access import require_registered_permission
from unifier.file_access_store import AccessStore


class FileOperations(_TargetPublish, _SourceCleanup):
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
            raise UnifierError(
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
            raise UnifierError(f"file operation {operation_uuid} is not recorded")
        record = _validate_journal(_read_json(path, "file operation journal"))
        if record["operation_uuid"] != operation_uuid:
            raise UnifierError(
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
        except UnifierError as exc:
            if recovery:
                raise _Reconcile(
                    f"{label} data root registration is unavailable: {exc}") from exc
            raise
        if record["root_uuid"] != root_uuid:
            raise UnifierError(f"{label} root identity changed")
        return record

    def _check_nested(self, record: dict, absolute: str, label: str) -> None:
        purpose = 'copy-target' if label == 'target' else 'copy-source'
        require_registered_permission(record, purpose)
        AccessStore(self.root).policy().require(absolute, purpose)
        matched, _relative = self.registry.match(absolute)
        if matched is not None and matched["root_uuid"] != record["root_uuid"]:
            raise UnifierError(
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
                    raise UnifierError(
                        f"{label} component {part!r} is not a directory "
                        "(symlinks are refused)")
                if _mount_id(child) != root_mount_id:
                    raise UnifierError(
                        f"{label} path crosses a mount boundary inside the data root")
                if observed.st_dev != root_stat.st_dev:
                    raise UnifierError(
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
        from unifier.views import ViewRegistry, is_mounted

        if not isinstance(source_view, dict):
            raise UnifierError("source view registration must be an object")
        try:
            view = ViewRegistry(self.root).one(source_view.get("view_uuid"))
        except UnifierError as exc:
            raise _MediaUnavailable(
                f"source read-only view is not registered: {exc}") from exc
        if view.get("readonly") is not True:
            raise UnifierError("a source view must be read-only")
        if view.get("backend") != "podman-overlay":
            raise UnifierError("source view backend is not supported")
        if view.get("environment_binding") != source_view.get("environment_binding"):
            raise UnifierError(
                "source view environment binding no longer matches the registration")
        if view.get("mountpoint") != record["locator"]:
            raise UnifierError("source view mountpoint does not match the data root")
        if view.get("state") not in {"ready", "mounted"} or not is_mounted(record["locator"]):
            raise _MediaUnavailable(
                "source read-only view is not mounted; an empty mount directory is "
                "not a data root")

    def _inspect_source(self, descriptor: int, relative_path: str) -> dict:
        observed = os.fstat(descriptor)
        if not stat.S_ISREG(observed.st_mode):
            raise UnifierError(
                f"source {relative_path!r} is not a regular file; only regular files "
                "are supported")
        if observed.st_nlink > 1:
            raise UnifierError(
                "hard-linked source files are refused until alias ownership can be "
                "verified")
        if stat.S_ISREG(observed.st_mode) and observed.st_blocks * 512 < observed.st_size:
            raise UnifierError(
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
            raise UnifierError(
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
            raise UnifierError(
                f"target data root {target_record['root_uuid']} is "
                f"{target_record['access_policy']}; managed file operations "
                "require a read-write target")
        if (source_record["root_uuid"] == target_record["root_uuid"]
                and source_relative == target_relative):
            raise UnifierError("source and target identify the same registered path")
        source_absolute = os.path.join(source_record["locator"], source_relative)
        target_absolute = os.path.join(target_record["locator"], target_relative)
        self._check_nested(source_record, source_absolute, "source")
        self._check_nested(target_record, target_absolute, "target")
        self._source_view_check(source_record)
        return source_record, target_record, source_parts, target_parts

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
        except UnifierError as exc:
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
        except UnifierError as exc:
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
                _close_fds(source_fd, source_parent_fd, source_root_fd)
                source_fd = source_parent_fd = source_root_fd = None
                self._finish_committed(record)
            except _Conflict as exc:
                if record is None:
                    raise UnifierError(str(exc)) from exc
                self._record_problem(record, "CONFLICT", exc.reason)
            except _Reconcile as exc:
                if record is None:
                    raise UnifierError(str(exc)) from exc
                self._record_problem(record, "NEEDS_RECONCILIATION", exc.reason)
            except _MediaUnavailable as exc:
                if record is None:
                    raise UnifierError(str(exc)) from exc
                self._record_problem(record, "CONFLICT", exc.reason)
            except OSError as exc:
                if record is None:
                    raise UnifierError(str(exc)) from exc
                self._record_problem(
                    record, "NEEDS_RECONCILIATION",
                    f"filesystem error after the operation became durable: {exc}")
            finally:
                _close_fds(
                    stage_fd, source_fd, source_parent_fd, source_root_fd,
                    target_parent_fd, target_root_fd)
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
                    raise UnifierError(
                        "source file crosses a mount boundary inside the data root")
                if observed.st_dev != root_stat.st_dev:
                    raise UnifierError(
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
