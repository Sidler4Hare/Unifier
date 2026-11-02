"""Stage, publish and register one regular-file operation.

These methods keep the existing journal state names and the order of the
durable steps. They do not introduce a generic transaction type.
"""

from __future__ import annotations

import hashlib
import os
import stat
import uuid

from unifier.file_operation_journal import (
    CLEANUP_STATES,
    REGISTRY_SCHEMA_VERSION,
    _Conflict,
    _READ_FLAGS,
    _Reconcile,
    _close_fds,
    _hash_fd,
    _same_version,
    _write_all,
)


class _TargetPublish:
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
            _close_fds(parent_fd, root_fd)

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
                    _close_fds(source_fd, source_parent_fd, source_root_fd)
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
            _close_fds(source_fd, source_parent_fd, source_root_fd)
            source_fd = source_parent_fd = source_root_fd = None
            self._finish_committed(record)
        finally:
            if stage_writable is not None and stage_writable >= 0:
                try:
                    os.close(stage_writable)
                except OSError:
                    pass
            _close_fds(
                source_fd, source_parent_fd, source_root_fd, target_parent_fd, target_root_fd)
