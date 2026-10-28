"""Read-only file-reference control over registered local data roots.

The first file API observes and resolves only. It does not execute files, pick
an interpreter or environment, copy, move, write, mount, or relabel anything.
Resolution walks an anchored root directory FD one component at a time with
O_NOFOLLOW. Every symlink is refused in this first version. The final object is
fstat'ed on the opened FD, and the FileRef is built from that observation.
"""

import os
from pathlib import Path
import re
import stat

from multilinux.errors import MultilinuxError
from multilinux.file_operations import FileOperations
from multilinux.registry import DataRootRegistry


SCHEMA_VERSION = 1
ROLE = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}\Z")

# Only observation exists in this round. Access policy is recorded, but it
# does not turn an unimplemented write operation into a capability.
CAPABILITIES = {
    "observe": True,
    "read": False,
    "write": False,
    "read_content": False,
    "write_content": False,
    "create": False,
    "delete": False,
    "copy": False,
    "move": False,
    "execute": False,
}

_PATH_FLAGS = getattr(os, "O_PATH", None)
if _PATH_FLAGS is None:  # pragma: no cover - Linux build invariant
    raise RuntimeError("Unifier file resolution requires Linux O_PATH")
_PATH_FLAGS |= os.O_CLOEXEC | os.O_NOFOLLOW


def _validate_relative(relative_path: str) -> list[str]:
    if not isinstance(relative_path, str) or not relative_path:
        raise MultilinuxError("relative_path must be a non-empty string")
    if relative_path.startswith("/"):
        raise MultilinuxError("relative_path must not be absolute")
    if "\x00" in relative_path or "\n" in relative_path:
        raise MultilinuxError("relative_path contains an invalid character")
    parts = relative_path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise MultilinuxError(
            "relative_path must not contain empty, '.' or '..' components")
    return parts


def _mount_id(descriptor: int) -> int:
    """Read the mount ID of an open FD, or refuse the capability.

    st_dev cannot distinguish a same-device bind mount. Linux exposes the real
    mount instance through fdinfo; if that evidence is unavailable we refuse
    resolution instead of silently falling back to the weaker device check.
    """
    path = Path(f"/proc/self/fdinfo/{descriptor}")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MultilinuxError(
            "cannot verify the mount boundary: /proc/self/fdinfo is unavailable "
            f"({exc}); refusing file resolution") from exc
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
            break
    raise MultilinuxError(
        "cannot verify the mount boundary: mnt_id is missing from fdinfo; "
        "refusing file resolution")


def _validate_role(role: str) -> str:
    """A role is a bounded label, never an interpreter or environment selector."""
    if not isinstance(role, str) or not ROLE.fullmatch(role):
        raise MultilinuxError(
            "file role must be 1-64 characters, start with a letter, and contain "
            "only letters, digits, '.', '_' or '-'")
    return role


class FileControl:
    def __init__(self, registry: DataRootRegistry):
        self.registry = registry

    def resolve(self, root_uuid: str, relative_path: str, role: str = "document") -> dict:
        record = self.registry.show(root_uuid)
        return self.resolve_record(record, relative_path, role)

    def resolve_path(self, absolute_path: str, role: str = "document") -> dict:
        if isinstance(absolute_path, str) and absolute_path.endswith("/"):
            raise MultilinuxError("file resolution requires a file path, not a directory")
        record, relative = self.registry.match(absolute_path)
        if record is None:
            raise MultilinuxError(f"no registered data root contains {absolute_path}")
        if not relative:
            raise MultilinuxError("file resolution requires a path inside a data root")
        return self.resolve_record(record, relative, role)

    def resolve_record(self, record: dict, relative_path: str, role: str = "document") -> dict:
        role = _validate_role(role)
        parts = _validate_relative(relative_path)
        locator = record["locator"]
        # A path explicitly addressed through a parent root must not bypass a
        # more specific child root (which may have a stricter policy). Refuse
        # and require the caller to resolve against the child root explicitly.
        absolute = os.path.join(locator, relative_path)
        matched, _relative = self.registry.match(absolute)
        if matched is not None and matched["root_uuid"] != record["root_uuid"]:
            raise MultilinuxError(
                f"path enters nested data root {matched['root_uuid']} "
                f"({matched['locator']}); resolve it against that root explicitly")
        descriptors: list[int] = []
        try:
            root_fd = os.open(locator, _PATH_FLAGS | os.O_DIRECTORY)
            descriptors.append(root_fd)
            root_stat = os.fstat(root_fd)
            self._check_root(record, root_stat)
            root_mount_id = _mount_id(root_fd)
            current = root_fd
            for part in parts[:-1]:
                child = os.open(part, _PATH_FLAGS | os.O_DIRECTORY, dir_fd=current)
                descriptors.append(child)
                observed = os.fstat(child)
                if not stat.S_ISDIR(observed.st_mode):
                    raise MultilinuxError(
                        f"path component {part!r} is not a directory (symlinks are refused)")
                if _mount_id(child) != root_mount_id:
                    raise MultilinuxError(
                        "path crosses a mount boundary inside the data root; refusing it")
                if observed.st_dev != root_stat.st_dev:
                    raise MultilinuxError(
                        "path crosses a device boundary inside the data root; refusing it")
                current = child
            final_fd = os.open(parts[-1], _PATH_FLAGS, dir_fd=current)
            descriptors.append(final_fd)
            observed = os.fstat(final_fd)
            if stat.S_ISLNK(observed.st_mode):
                raise MultilinuxError("symbolic links are not resolved by this first file API")
            if not stat.S_ISREG(observed.st_mode):
                raise MultilinuxError("file resolve requires a regular file")
            if _mount_id(final_fd) != root_mount_id:
                raise MultilinuxError(
                    "file crosses a mount boundary inside the data root; refusing it")
            if observed.st_dev != root_stat.st_dev:
                raise MultilinuxError(
                    "file crosses a device boundary inside the data root; refusing it")
            if observed.st_nlink > 1:
                raise MultilinuxError(
                    "hard-linked regular files are refused until cross-root alias "
                    "ownership can be verified")
            # If the registered root path itself was replaced after we opened
            # it, the anchored FD would still point at the old inode. Refuse
            # anyway, so a replacement is visible rather than silently stale.
            try:
                current_root = os.stat(locator, follow_symlinks=False)
            except OSError as exc:
                raise MultilinuxError(
                    f"data root path changed during resolution: {exc}") from exc
            self._check_root(record, current_root)
            return {
                "schema_version": SCHEMA_VERSION,
                "root_uuid": record["root_uuid"],
                "management_domain": record["management_domain"],
                "backend": record["backend"],
                "access_policy": record["access_policy"],
                "relative_path": relative_path,
                "role": role,
                "observed_version": {
                    "dev": observed.st_dev,
                    "ino": observed.st_ino,
                    "mode": observed.st_mode,
                    "nlink": observed.st_nlink,
                    "uid": observed.st_uid,
                    "gid": observed.st_gid,
                    "size": observed.st_size,
                    "mtime": observed.st_mtime,
                    "ctime": observed.st_ctime,
                    "mtime_ns": observed.st_mtime_ns,
                    "ctime_ns": observed.st_ctime_ns,
                    "file_type": "regular",
                },
                "capabilities": dict(CAPABILITIES),
                "implemented_operations": ["observe"],
                "execution": {
                    "interpreter": None,
                    "environment": None,
                    "note": "role and management domain do not select executable context",
                },
            }
        except MultilinuxError:
            raise
        except OSError as exc:
            raise MultilinuxError(f"cannot resolve {relative_path!r}: {exc}") from exc
        finally:
            for descriptor in reversed(descriptors):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    @staticmethod
    def _check_root(record: dict, observed: os.stat_result) -> None:
        if not stat.S_ISDIR(observed.st_mode):
            raise MultilinuxError("data root locator is not a directory; it may have been replaced")
        if observed.st_dev != record["root_dev"] or observed.st_ino != record["root_ino"]:
            raise MultilinuxError(
                "data root path was replaced (device/inode no longer match the registration)")


class Control:
    """Small facade used by the CLI and by future local clients."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.registry = DataRootRegistry(self.root)
        self.files = FileControl(self.registry)
        self.operations = FileOperations(self.root, self.registry)

    def add_root(self, path: str, *, source_view_uuid: str | None = None,
                 **options) -> dict:
        if source_view_uuid is not None:
            from multilinux.views import ViewRegistry, is_mounted
            view = ViewRegistry(self.root).one(source_view_uuid)
            if view.get("readonly") is not True:
                raise MultilinuxError(
                    "only a read-only view can be registered as a source root")
            if view.get("backend") != "podman-overlay":
                raise MultilinuxError("source view backend is not supported")
            if view.get("state") not in {"ready", "mounted"}:
                raise MultilinuxError(
                    f"source view {view['view_uuid']} is {view.get('state')!r}, "
                    "not a ready read-only mount")
            if not is_mounted(view["mountpoint"]):
                raise MultilinuxError(
                    "source view mountpoint is not a live FUSE mount; an empty "
                    "directory is not a data root")
            canonical = os.path.realpath(path)
            if canonical != view["mountpoint"]:
                raise MultilinuxError(
                    "registered source path must be the view mountpoint itself")
            if options.get("access_policy", "read-only") != "read-only":
                raise MultilinuxError(
                    "a read-only FUSE source root must use the read-only policy")
            options["access_policy"] = "read-only"
            # A read-only projection can be a copy source but never a cleanup
            # source, regardless of what a caller passed.
            options["cleanup_policy"] = "retain"
            options["source_view"] = {
                "view_uuid": view["view_uuid"],
                "environment_binding": dict(view["environment_binding"]),
                "mountpoint": view["mountpoint"],
            }
        return self.registry.add(path, **options)

    def list_roots(self) -> list[dict]:
        return self.registry.list()

    def show_root(self, identifier: str) -> dict:
        return self.registry.show(identifier)

    def remove_root(self, identifier: str) -> dict:
        return self.registry.remove(identifier)

    def match_root(self, absolute_path: str):
        return self.registry.match(absolute_path)

    def resolve_file(self, root_uuid: str, relative_path: str, role: str = "document") -> dict:
        return self.files.resolve(root_uuid, relative_path, role)

    def resolve_path(self, absolute_path: str, role: str = "document") -> dict:
        return self.files.resolve_path(absolute_path, role)

    # ----- D-stage managed operations -------------------------------------
    def copy_file(self, source_root_uuid: str, source_relative: str,
                  target_root_uuid: str, target_relative: str, *,
                  fault_hook=None) -> dict:
        return self.operations.copy_file(
            source_root_uuid, source_relative, target_root_uuid, target_relative,
            fault_hook=fault_hook)

    def move_file(self, source_root_uuid: str, source_relative: str,
                  target_root_uuid: str, target_relative: str, *,
                  cleanup_source: bool = False, fault_hook=None) -> dict:
        return self.operations.move_file(
            source_root_uuid, source_relative, target_root_uuid, target_relative,
            cleanup_source=cleanup_source, fault_hook=fault_hook)

    def file_operation(self, operation_uuid: str) -> dict:
        return self.operations.operation(operation_uuid)

    def list_file_operations(self) -> list[dict]:
        return self.operations.list()

    def recover_file_operations(self, operation_uuid: str | None = None) -> list[dict]:
        return self.operations.recover(operation_uuid)
