"""Independent data-root registry.

Data roots are durable host data identities. They live in their own file and
are never deleted by EnvironmentStore.remove(): removing a container or an
environment cannot remove the user's project data registration.

The first backend is a local host directory. Unknown backends, management
domains and access policies are refused rather than guessed. Nested roots are
allowed and matching is by path components with the longest explicit root
winning; matching only selects a root, it never authorizes a path by itself.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import stat
import uuid
from zoneinfo import ZoneInfo

from unifier.atomic_io import atomic_replace
from unifier.errors import UnifierError
from unifier.root_identity import observe_root, resolve_identity, validate_identity


SCHEMA_VERSION = 1
BACKENDS = {"local-directory"}
MANAGEMENT_DOMAINS = {"host-project"}
ACCESS_POLICIES = {"read-only", "read-write"}
# A data root only ever permits deletion of a move source when it is explicitly
# registered for the bounded controlled-move protocol.  Records written before
# this field existed have no cleanup rights and are read as "retain".
CLEANUP_POLICIES = {"retain", "controlled"}
REQUIRED_FIELDS = (
    "schema_version",
    "root_uuid",
    "management_domain",
    "backend",
    "locator",
    "access_policy",
    "root_dev",
    "root_ino",
    "created_at",
)


def _now() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _choice(value, allowed: set[str], label: str) -> str:
    if not isinstance(value, str):
        raise UnifierError(f"data root {label} must be a string")
    if value not in allowed:
        raise UnifierError(f"unknown data root {label}: {value!r}")
    return value


def _canonical_root_uuid(value) -> str:
    if not isinstance(value, str):
        raise UnifierError("data root root_uuid must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UnifierError(f"data root root_uuid is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise UnifierError("data root root_uuid must be the canonical lowercase UUID form")
    return value


def _validate_record(record) -> dict:
    if not isinstance(record, dict):
        raise UnifierError("data root record must be a JSON object")
    missing = [key for key in REQUIRED_FIELDS if record.get(key) in (None, "")]
    if missing:
        raise UnifierError("data root record is missing fields: " + ", ".join(missing))
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise UnifierError("data root schema_version must be an integer")
    if version != SCHEMA_VERSION:
        raise UnifierError(
            f"data root schema_version {version} is not supported "
            f"(this build understands {SCHEMA_VERSION})")
    _canonical_root_uuid(record["root_uuid"])
    _choice(record["management_domain"], MANAGEMENT_DOMAINS, "management domain")
    _choice(record["backend"], BACKENDS, "backend")
    _choice(record["access_policy"], ACCESS_POLICIES, "access policy")
    cleanup_policy = record.get("cleanup_policy", "retain")
    _choice(cleanup_policy, CLEANUP_POLICIES, "cleanup policy")
    if (cleanup_policy == "controlled"
            and record["access_policy"] != "read-write"):
        raise UnifierError(
            "a controlled source-cleanup policy requires a read-write data root")
    locator = record["locator"]
    if (not isinstance(locator, str) or not locator.startswith("/")
            or "\x00" in locator or "\n" in locator):
        raise UnifierError("data root locator must be an absolute host path")
    if os.path.normpath(locator) != locator:
        raise UnifierError("data root locator must be a normalized absolute path")
    for key in ("root_dev", "root_ino"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            raise UnifierError(f"data root {key} must be an integer")
    if "root_identity" in record:
        validate_identity(record["root_identity"])
    if not isinstance(record.get("created_at"), str):
        raise UnifierError("data root created_at must be a string")
    source_view = record.get("source_view")
    if source_view is not None:
        if not isinstance(source_view, dict):
            raise UnifierError("data root source_view must be an object")
        if record["backend"] != "local-directory":
            raise UnifierError(
                "a read-only FUSE source view requires the local-directory backend")
        if record["access_policy"] != "read-only":
            raise UnifierError(
                "a read-only FUSE source view must be registered read-only")
        if cleanup_policy != "retain":
            raise UnifierError(
                "a read-only FUSE source view can never carry source-cleanup rights")
        _canonical_root_uuid(source_view.get("view_uuid"))
        from unifier.identity import validate_binding
        validate_binding(source_view.get("environment_binding"))
        mountpoint = source_view.get("mountpoint")
        if (not isinstance(mountpoint, str) or not mountpoint.startswith("/")
                or "\x00" in mountpoint or "\n" in mountpoint):
            raise UnifierError(
                "data root source_view mountpoint must be an absolute path")
        if mountpoint != locator:
            raise UnifierError(
                "data root source_view mountpoint must equal the registered locator")
    return record


class DataRootRegistry:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / ".lab/state/data-roots"
        self.path = self.directory / "registry.json"
        self.lock_path = self.directory / "registry.lock"

    @contextmanager
    def _locked(self, exclusive: bool):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    def _read_unlocked(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnifierError(f"data root registry is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise UnifierError("data root registry must be a JSON object")
        version = payload.get("schema_version")
        if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
            raise UnifierError(
                f"data root registry schema_version {version!r} is not supported")
        roots = payload.get("roots")
        if not isinstance(roots, list):
            raise UnifierError("data root registry roots must be a list")
        seen_uuid = set()
        seen_inode = set()
        seen_locator = set()
        validated = []
        for record in roots:
            _validate_record(record)
            if record["root_uuid"] in seen_uuid:
                raise UnifierError("data root registry contains a duplicate root_uuid")
            if record["locator"] in seen_locator:
                raise UnifierError("data root registry contains a duplicate locator")
            identity = record.get("root_identity")
            inode = (("btrfs", identity["filesystem_uuid"], identity["subvolume_id"],
                      identity["subvolume_uuid"], record["root_ino"]) if identity else
                     ("device", record["root_dev"], record["root_ino"]))
            if inode in seen_inode:
                raise UnifierError("data root registry contains a duplicate root inode")
            seen_uuid.add(record["root_uuid"])
            seen_locator.add(record["locator"])
            seen_inode.add(inode)
            validated.append(record)
        validated.sort(key=lambda item: item["created_at"])
        return validated

    def _write_unlocked(self, roots: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {"schema_version": SCHEMA_VERSION, "roots": roots},
            ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
        atomic_replace(self.path, payload, prefix=".ml-root-")

    def list(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with self._locked(False):
            return self._read_unlocked()

    def find(self, identifier: str) -> dict:
        if not isinstance(identifier, str) or not identifier:
            raise UnifierError("data root identifier must be a non-empty string")
        for record in self.list():
            if record["root_uuid"] == identifier or record["locator"] == identifier:
                return resolve_identity(record)
        if identifier.startswith("/"):
            canonical = os.path.realpath(identifier)
            for record in self.list():
                if record["locator"] == canonical:
                    return resolve_identity(record)
        raise UnifierError(f"data root {identifier} is not registered")

    def show(self, identifier: str) -> dict:
        return dict(self.find(identifier))

    def add(self, path: str, *, backend: str = "local-directory",
            management_domain: str = "host-project",
            access_policy: str = "read-write",
            cleanup_policy: str = "retain",
            source_view: dict | None = None) -> dict:
        _choice(backend, BACKENDS, "backend")
        _choice(management_domain, MANAGEMENT_DOMAINS, "management domain")
        _choice(access_policy, ACCESS_POLICIES, "access policy")
        _choice(cleanup_policy, CLEANUP_POLICIES, "cleanup policy")
        if cleanup_policy == "controlled" and access_policy != "read-write":
            raise UnifierError(
                "a controlled source-cleanup policy requires a read-write data root")
        if source_view is not None and cleanup_policy != "retain":
            raise UnifierError(
                "a read-only source view cannot carry source-cleanup rights")
        if (not isinstance(path, str) or not path.startswith("/")
                or "\x00" in path or "\n" in path):
            raise UnifierError("data root path must be an absolute host path")
        canonical = os.path.realpath(path)
        try:
            observed = os.stat(canonical, follow_symlinks=False)
        except OSError as exc:
            raise UnifierError(f"data root path is unavailable: {canonical}: {exc}") from exc
        if not stat.S_ISDIR(observed.st_mode):
            raise UnifierError(f"data root must be a directory: {canonical}")
        anchored, identity = observe_root(canonical)
        if (anchored.st_dev, anchored.st_ino) != (observed.st_dev, observed.st_ino):
            raise UnifierError("data root path was replaced during registration")
        record = {
            "schema_version": SCHEMA_VERSION,
            "root_uuid": str(uuid.uuid4()),
            "management_domain": management_domain,
            "backend": backend,
            "locator": canonical,
            "access_policy": access_policy,
            "cleanup_policy": cleanup_policy,
            "root_dev": observed.st_dev,
            "root_ino": observed.st_ino,
            "created_at": _now(),
        }
        if source_view is not None:
            record["source_view"] = source_view
        if identity is not None:
            record["root_identity"] = identity
        _validate_record(record)
        with self._locked(True):
            roots = self._read_unlocked()
            for existing in roots:
                if existing["locator"] == canonical:
                    raise UnifierError(
                        f"data root {canonical} is already registered as "
                        f"{existing['root_uuid']}")
                existing_identity = existing.get("root_identity")
                if existing_identity is not None:
                    same_root = (identity == existing_identity
                                 and existing["root_ino"] == observed.st_ino)
                else:
                    same_root = (existing["root_dev"], existing["root_ino"]) == (
                        observed.st_dev, observed.st_ino)
                if same_root:
                    raise UnifierError(
                        f"data root {canonical} is an alias of the same inode as "
                        f"{existing['root_uuid']}; refusing a conflicting root alias")
            roots.append(record)
            self._write_unlocked(roots)
        return record

    def pin_identity(self, identifier: str) -> dict:
        """Explicitly upgrade a legacy Btrfs root while dev/inode still match.

        A stale legacy device number is not proof of continuity. It must be
        independently recovered before this migration is allowed.
        """
        with self._locked(True):
            roots = self._read_unlocked()
            for record in roots:
                if identifier not in (record["root_uuid"], record["locator"]):
                    continue
                if "root_identity" in record:
                    return resolve_identity(record)
                observed, identity = observe_root(record["locator"])
                if (observed.st_dev, observed.st_ino) != (record["root_dev"], record["root_ino"]):
                    raise UnifierError("legacy data root path was replaced; refusing identity migration")
                if identity is None:
                    raise UnifierError("persistent root identity migration requires Btrfs")
                record["root_identity"] = identity
                _validate_record(record)
                self._write_unlocked(roots)
                return dict(record)
            raise UnifierError(f"data root {identifier} is not registered")

    def remove(self, identifier: str) -> dict:
        """Remove only the registration. This never unlinks or moves a directory."""
        with self._locked(True):
            roots = self._read_unlocked()
            found = None
            canonical = os.path.realpath(identifier) if (
                isinstance(identifier, str) and identifier.startswith("/")) else None
            for record in roots:
                if (record["root_uuid"] == identifier
                        or record["locator"] == identifier
                        or (canonical is not None and record["locator"] == canonical)):
                    found = record
                    break
            if found is None:
                raise UnifierError(f"data root {identifier} is not registered")
            self._write_unlocked(
                [item for item in roots if item["root_uuid"] != found["root_uuid"]])
            return found

    def match(self, absolute_path: str):
        """Return ``(root_record, relative_path)`` by longest component match.

        This is selection only. File resolution still opens the selected root
        with O_NOFOLLOW and walks each component from an anchored FD.
        """
        if (not isinstance(absolute_path, str) or not absolute_path.startswith("/")
                or "\x00" in absolute_path or "\n" in absolute_path):
            raise UnifierError("path must be an absolute host path")
        parts = PurePosixPath(absolute_path).parts
        if ".." in parts:
            raise UnifierError("path must not contain '..' components")
        selected = None
        selected_parts = None
        for record in self.list():
            root_parts = PurePosixPath(record["locator"]).parts
            if len(parts) >= len(root_parts) and parts[:len(root_parts)] == root_parts:
                if selected is None or len(root_parts) > len(selected_parts):
                    selected = record
                    selected_parts = root_parts
        if selected is None:
            return None, None
        relative = "/".join(parts[len(selected_parts):])
        return resolve_identity(selected), relative
