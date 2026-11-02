"""Pinned source isolation and deletion for an authorized controlled move.

Quarantine directory names, journal state names and the failure windows stay
as they are. The operation lock and the source-root lock are still taken by
the caller.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import os
import stat
import uuid

from unifier.errors import UnifierError
from unifier.lease import (
    LeaseBroken,
    LeaseError,
    WriteLease,
    rename_noreplace,
)
from unifier.file_operation_journal import (
    _CleanupRefused,
    _DIR_FLAGS,
    _MediaUnavailable,
    _READ_FLAGS,
    _Reconcile,
    _close_fds,
    _hash_fd,
    _mount_id,
    _root_matches,
    _same_version,
    _version_without_ctime_matches,
)


@dataclass
class _CleanupResources:
    """FDs and leases owned by one controlled-cleanup attempt.

    The coordinator releases target then source leases before closing any FD,
    including when a stage exits early or fails while writing its journal.
    """

    root_fd: int | None = None
    parent_fd: int | None = None
    qfd: int | None = None
    entry_fd: int | None = None
    source_fd: int | None = None
    target_root_fd: int | None = None
    target_parent_fd: int | None = None
    target_fd: int | None = None
    root_stat: os.stat_result | None = None
    qstat: os.stat_result | None = None
    entry_stat: os.stat_result | None = None
    lease: WriteLease | None = None
    target_lease: WriteLease | None = None

    def close(self) -> None:
        if self.target_lease is not None:
            self.target_lease.release()
        if self.lease is not None:
            self.lease.release()
        _close_fds(
            self.entry_fd, self.source_fd, self.qfd, self.parent_fd, self.root_fd,
            self.target_fd, self.target_parent_fd, self.target_root_fd)


class _SourceCleanup:
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
            raise UnifierError(
                "source cleanup was requested but the source data root is not "
                "registered with --cleanup-policy controlled; refusing before any "
                "copy or journal side effect")
        if source_record.get("access_policy") != "read-write":
            raise UnifierError(
                "source cleanup is only supported from a read-write data root; "
                "read-only roots and read-only C views retain their source")
        if source_record.get("source_view") is not None:
            raise UnifierError(
                "a C read-only view can be copied from but never cleaned up")
        if source_record.get("backend") != "local-directory":
            raise UnifierError(
                "controlled source cleanup only supports the local-directory backend")


    # ----- bounded controlled source cleanup ------------------------------
    @staticmethod
    def _source_entry_name(record: dict) -> str:
        return record["source"]["relative_path"].split("/")[-1]

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
            _close_fds(target_fd, parent_fd, root_fd)
            raise

    def _verify_cleanup_target_once(self, record: dict) -> None:
        root_fd, parent_fd, target_fd, _name, _stat_result = \
            self._open_cleanup_target_held(record)
        _close_fds(target_fd, parent_fd, root_fd)
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

    def _acquire_cleanup_lease(self, record: dict, lease: WriteLease,
                               problem_state: str) -> bool:
        try:
            lease.acquire()
        except LeaseError as exc:
            self._cleanup_problem(record, problem_state, str(exc))
            return False
        return True

    def _mark_source_isolated(self, record: dict, parent_fd: int,
                              entry_name: str) -> None:
        block = record["source_cleanup"]
        block["original_path_repopulated"] = self._check_original_after_capture(
            record, parent_fd, entry_name)
        block["state"] = "isolated"
        record["state"] = "SOURCE_ISOLATED"
        self._write_journal(record)
        self._fault("SOURCE_ISOLATED")

    def _verify_cleanup_bindings(self, record: dict, handles: _CleanupResources,
                                target_name: str) -> dict:
        source_record, target_record = self._load_records(record)
        self._verify_cleanup_authorization(record, source_record)
        self._verify_source_anchor_paths(
            record, source_record, handles.root_fd, handles.parent_fd,
            handles.root_stat)
        self._verify_target_anchor_paths(
            record, target_record, handles.target_root_fd, handles.target_parent_fd,
            handles.target_fd, target_name)
        self._verify_registry_once(record)
        return source_record

    def _resume_removed_cleanup(self, record: dict, handles: _CleanupResources) -> None:
        block = record["source_cleanup"]
        # Journal written after the private unlink.  A missing empty
        # quarantine is expected if the process died after rmdir.
        source_record = self._root_record(
            record["source"]["root_uuid"], "source", recovery=True)
        handles.root_fd, handles.root_stat, _ = self._open_root_fd(
            source_record, "source")
        name = block.get("quarantine", {}).get("dir_name")
        observed = self._lstat_at(handles.root_fd, name) if name else None
        if observed is not None:
            if not stat.S_ISDIR(observed.st_mode):
                raise _Reconcile(
                    "the quarantine path is no longer a directory")
            handles.qfd = os.open(name, _DIR_FLAGS, dir_fd=handles.root_fd)
            handles.qstat = os.fstat(handles.qfd)
            if (handles.qstat.st_dev, handles.qstat.st_ino) != (
                    block["quarantine"].get("dir_dev"),
                    block["quarantine"].get("dir_ino")):
                raise _Reconcile(
                    "the quarantine path was replaced after source removal")
            self._remove_quarantine_dir(
                record, handles.root_fd, handles.qfd, handles.qstat)
        self._cleanup_done(record)

    def _isolate_cleanup_source(self, record: dict, handles: _CleanupResources) -> bool:
        state = record.get("state")
        (_, handles.root_fd, handles.parent_fd, handles.root_stat,
         root_mount) = self._open_source_anchor(record)
        create = state not in {
            "SOURCE_CAPTURED", "SOURCE_ISOLATED", "SOURCE_DELETION_INTENT"}
        handles.qfd, handles.qstat = self._ensure_quarantine(
            record, handles.root_fd, handles.root_stat, root_mount, create=create)
        handles.entry_fd, handles.entry_stat = self._open_quarantine_entry(handles.qfd)

        if handles.entry_fd is not None:
            source_identity = (
                record["source"]["observed_version"].get("dev"),
                record["source"]["observed_version"].get("ino"))
            if (handles.entry_stat.st_dev, handles.entry_stat.st_ino) != source_identity:
                self._handle_foreign_capture(
                    record, None, handles.parent_fd, self._source_entry_name(record),
                    handles.qfd, handles.root_fd)
                return False
            handles.lease = WriteLease(handles.entry_fd, "captured source")
            if not self._acquire_cleanup_lease(
                    record, handles.lease, "NEEDS_RECONCILIATION"):
                return False
            record["source_cleanup"]["lease"] = {
                "type": "write", "acquired": True, "broken": False,
                "target": "captured-source",
            }
            self._verify_captured_source(record, handles.entry_fd, handles.entry_stat)
            if state in {"SOURCE_CAPTURED", "SOURCE_CAPTURE_INTENT"}:
                self._mark_source_isolated(
                    record, handles.parent_fd, self._source_entry_name(record))
            elif state == "SOURCE_DELETION_INTENT":
                repopulated = self._check_original_after_capture(
                    record, handles.parent_fd, self._source_entry_name(record))
                record["source_cleanup"]["original_path_repopulated"] = repopulated
        else:
            if state in {"SOURCE_CAPTURED", "SOURCE_ISOLATED"}:
                raise _Reconcile(
                    "the captured inode is missing from the pinned quarantine; "
                    "refusing to infer deletion from a missing path")
            if state == "SOURCE_DELETION_INTENT":
                # Durable unlink intent plus a pinned, private, empty
                # quarantine is the proof domain for an unrecorded unlink.
                if os.listdir(handles.qfd):
                    raise _Reconcile(
                        "the quarantine is not empty and the captured entry is "
                        "missing; refusing to infer deletion")
                self._finish_after_removal(
                    record, handles.root_fd, handles.qfd, handles.qstat)
                return False
            # Before capture: the original name must still hold the exact
            # opened source object.
            entry_name = self._source_entry_name(record)
            handles.source_fd = os.open(
                entry_name, _READ_FLAGS, dir_fd=handles.parent_fd)
            source_stat = os.fstat(handles.source_fd)
            if (source_stat.st_dev, source_stat.st_ino) != (
                    record["source"]["observed_version"].get("dev"),
                    record["source"]["observed_version"].get("ino")):
                raise _Reconcile(
                    "the original source name now names a different inode; "
                    "refusing to capture or delete it")
            self._verify_source_original(record, handles.source_fd, source_stat)
            handles.lease = WriteLease(handles.source_fd, "source")
            if not self._acquire_cleanup_lease(
                    record, handles.lease, "SOURCE_CLEANUP_PENDING"):
                return False
            record["source_cleanup"]["lease"] = {
                "type": "write", "acquired": True, "broken": False,
                "target": "source",
            }
            self._capture_source(
                record, handles.parent_fd, entry_name, handles.qfd,
                handles.source_fd, source_stat, handles.lease)
            captured_stat = self._lstat_at(handles.qfd, "source")
            if captured_stat is None:
                raise _Reconcile(
                    "the captured entry vanished immediately after capture")
            source_identity = (
                record["source"]["observed_version"].get("dev"),
                record["source"]["observed_version"].get("ino"))
            if (captured_stat.st_dev, captured_stat.st_ino) != source_identity:
                self._handle_foreign_capture(
                    record, handles.source_fd, handles.parent_fd, entry_name,
                    handles.qfd, handles.root_fd)
                return False
            # Reuse the already-leased open file description.  Opening the
            # captured pathname again would be a second open description
            # and would self-break the kernel write lease.
            handles.entry_fd = handles.source_fd
            handles.entry_stat = os.fstat(handles.entry_fd)
            handles.source_fd = None
            if (handles.entry_stat.st_dev, handles.entry_stat.st_ino) != source_identity:
                raise _Reconcile(
                    "the captured inode does not match the held source FD")
            self._verify_captured_source(record, handles.entry_fd, handles.entry_stat)
            self._mark_source_isolated(record, handles.parent_fd, entry_name)
        return True

    def _delete_cleanup_source(self, record: dict, handles: _CleanupResources) -> None:
        block = record["source_cleanup"]
        # Target and authorization are rechecked with the target inode
        # held open.  Acquire an exclusive write lease on the target too:
        # this is a content-writer barrier, not a rename barrier, so the
        # pathname/root/parent checks below are still required.
        (handles.target_root_fd, handles.target_parent_fd, handles.target_fd,
         target_name, target_stat) = self._open_cleanup_target_held(record)
        handles.target_lease = WriteLease(handles.target_fd, "published target")
        if not self._acquire_cleanup_lease(
                record, handles.target_lease, "NEEDS_RECONCILIATION"):
            return
        block["lease"]["target_acquired"] = True
        fresh_source = self._verify_cleanup_bindings(record, handles, target_name)
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
        self._verify_cleanup_bindings(record, handles, target_name)
        self._verify_target_held(record, handles.target_fd, os.fstat(handles.target_fd))
        observed_dir = self._lstat_at(
            handles.root_fd, block["quarantine"]["dir_name"])
        if observed_dir is None or (observed_dir.st_dev, observed_dir.st_ino) != (
                handles.qstat.st_dev, handles.qstat.st_ino):
            raise _Reconcile(
                "the quarantine directory path was replaced before deletion")
        current_entry = self._lstat_at(handles.qfd, "source")
        if current_entry is None or (current_entry.st_dev, current_entry.st_ino) != (
                handles.entry_stat.st_dev, handles.entry_stat.st_ino):
            raise _Reconcile(
                "the captured quarantine entry changed before deletion")
        # A hard link can be added anywhere on the same filesystem without
        # changing the quarantine name; revalidate the captured inode (and
        # in particular nlink == 1 plus its content hash) at this last
        # deletion boundary, not only immediately after capture.
        self._verify_captured_source(record, handles.entry_fd, current_entry)
        self._check_original_after_capture(
            record, handles.parent_fd, self._source_entry_name(record))
        if handles.lease is not None:
            handles.lease.assert_held()
        if handles.target_lease is not None:
            handles.target_lease.assert_held()
        os.unlink("source", dir_fd=handles.qfd)
        os.fsync(handles.qfd)
        os.fsync(handles.root_fd)
        self._finish_after_removal(
            record, handles.root_fd, handles.qfd, handles.qstat, just_removed=True)

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
        handles = _CleanupResources()
        try:
            source_record, _ = self._load_records(record)
            self._verify_cleanup_authorization(record, source_record)
            self._verify_cleanup_target_once(record)
            if record.get("state") == "SOURCE_REMOVED":
                self._resume_removed_cleanup(record, handles)
            elif self._isolate_cleanup_source(record, handles):
                self._delete_cleanup_source(record, handles)
        except _CleanupRefused as exc:
            self._cleanup_problem(record, "SOURCE_CLEANUP_PENDING", exc.reason)
        except (_Reconcile, _MediaUnavailable) as exc:
            self._cleanup_problem(record, "NEEDS_RECONCILIATION", exc.reason)
        except LeaseBroken as exc:
            self._cleanup_problem(record, "NEEDS_RECONCILIATION", str(exc))
        except LeaseError as exc:
            self._cleanup_problem(record, "SOURCE_CLEANUP_PENDING", str(exc))
        except OSError as exc:
            self._cleanup_problem(
                record, "NEEDS_RECONCILIATION",
                f"filesystem error during controlled cleanup: {exc}")
        finally:
            handles.close()

    def _finish_after_removal(self, record: dict, root_fd: int, qfd: int,
                              qstat: os.stat_result, *, just_removed: bool = False) -> None:
        block = record["source_cleanup"]
        block["performed"] = True
        block["removed_at"] = (self.clock() if just_removed else
                               block.get("removed_at") or self.clock())
        block["state"] = "source_removed"
        record["source_retained"] = False
        record["state"] = "SOURCE_REMOVED"
        self._write_journal(record)
        self._fault("SOURCE_REMOVED")
        self._remove_quarantine_dir(record, root_fd, qfd, qstat)
        self._cleanup_done(record)
