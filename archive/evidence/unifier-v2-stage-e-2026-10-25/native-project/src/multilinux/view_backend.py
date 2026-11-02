"""Safe read-only backend for a stopped-environment FUSE view.

Paths are container paths. The backend selects the most specific persistent
mount by destination, or falls back to the storage-namespace merged root FD.
Every component is opened relative to an anchored directory FD with O_NOFOLLOW.
No code writes to the rootfs, bind sources or volumes.
"""

from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat as stat_module


class ViewError(Exception):
    def __init__(self, err, message: str):
        super().__init__(message)
        self.errno = err
        self.message = message


def unavailable(message: str) -> ViewError:
    return ViewError(errno.EIO, message)


def _mnt_id(descriptor: int) -> int | None:
    try:
        text = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
    return None


def normalize_container_path(path: str) -> str:
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path:
        raise ViewError(errno.EINVAL, "a container view path must be absolute")
    if ".." in path.split("/"):
        raise ViewError(errno.EINVAL, "container view path must not contain '..'")
    normalized = posixpath.normpath(path)
    if normalized != "/" and not normalized.startswith("/"):
        raise ViewError(errno.EINVAL, "container view path escaped its root")
    return normalized


def _relative(path: str, root: str) -> str:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    if len(path_parts) < len(root_parts) or path_parts[:len(root_parts)] != root_parts:
        raise ViewError(errno.ENOENT, "path is outside the selected mount")
    remainder = path_parts[len(root_parts):]
    return "/".join(remainder)


@dataclass(frozen=True)
class IdMap:
    host_uid: int
    host_gid: int
    container_uid: int
    container_gid: int
    subuid_start: int
    subuid_count: int
    subgid_start: int
    subgid_count: int
    user_namespace: str
    uid_ranges: tuple = ()
    gid_ranges: tuple = ()

    def _map(self, host: int, ranges) -> int:
        for host_start, guest_start, count in ranges:
            if host_start <= host < host_start + count:
                return guest_start + host - host_start
        return host

    def uid(self, host_uid: int) -> int:
        if self.uid_ranges:
            return self._map(host_uid, self.uid_ranges)
        if host_uid == self.host_uid:
            return self.container_uid
        return host_uid

    def gid(self, host_gid: int) -> int:
        if self.gid_ranges:
            return self._map(host_gid, self.gid_ranges)
        if host_gid == self.host_gid:
            return self.container_gid
        return host_gid

    def as_dict(self):
        return {
            "host_uid": self.host_uid, "host_gid": self.host_gid,
            "container_uid": self.container_uid, "container_gid": self.container_gid,
            "subuid_start": self.subuid_start, "subuid_count": self.subuid_count,
            "subgid_start": self.subgid_start, "subgid_count": self.subgid_count,
            "user_namespace": self.user_namespace,
            "uid_ranges": list(self.uid_ranges), "gid_ranges": list(self.gid_ranges),
        }


def _username(uid: int) -> str | None:
    try:
        import pwd
        return pwd.getpwuid(uid).pw_name
    except (KeyError, OSError):
        return None


def read_subid(uid: int, gid: int) -> tuple[int, int, int, int]:
    names = {str(uid)}
    username = _username(uid)
    if username:
        names.add(username)

    def lookup(path, names, ident):
        candidate = {str(ident)}
        if username:
            candidate.add(username)
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                fields = line.split(":")
                if len(fields) >= 3 and fields[0] in candidate:
                    return int(fields[1]), int(fields[2])
        except (OSError, ValueError):
            pass
        return 0, 0
    ustart, ucount = lookup("/etc/subuid", names, uid)
    gstart, gcount = lookup("/etc/subgid", names, gid)
    return ustart, ucount, gstart, gcount


def _parse_map(value):
    if value is None:
        return []
    if isinstance(value, dict):
        value = value.get("UidMap") or value.get("GidMap") or []
    result = []
    for item in value:
        if isinstance(item, str):
            fields = item.replace(":", " ").split()
        elif isinstance(item, (list, tuple)):
            fields = list(item)
        else:
            continue
        if len(fields) != 3:
            continue
        try:
            inside, outside, length = (int(fields[0]), int(fields[1]), int(fields[2]))
        except (TypeError, ValueError):
            continue
        if length > 0:
            result.append((inside, outside, length))
    return result


def _compose_ranges(storage_map, container_map):
    """Return host->guest ranges from namespace and Podman ID mappings.

    storage_map entries are (namespace_id, host_id, length) from the storage
    namespace /proc/self/uid_map. container_map entries are Podman
    (container_id, storage_id, length); absent map means identity.
    """
    storage_map = _parse_map(storage_map)
    container_map = _parse_map(container_map)
    if not storage_map:
        return []
    if not container_map:
        return [(outside, inside, length) for inside, outside, length in storage_map]
    ranges = []
    for container_start, storage_start, container_length in container_map:
        for namespace_id, host_id, storage_length in storage_map:
            lo = max(storage_start, namespace_id)
            hi = min(storage_start + container_length, namespace_id + storage_length)
            if hi > lo:
                ranges.append((host_id + (lo - namespace_id),
                               container_start + (lo - storage_start), hi - lo))
    return sorted(ranges)


def build_id_map(record: dict, uid: int, gid: int, *, storage_uid_map=None,
                 storage_gid_map=None, id_mappings=None) -> IdMap:
    namespace = record.get("runtime", {}).get("user_namespace", "rootless-default")
    ustart, ucount, gstart, gcount = read_subid(uid, gid)
    container_uid = uid if namespace == "keep-id" else 0
    container_gid = gid if namespace == "keep-id" else 0
    id_mappings = id_mappings or {}
    composed_u = _compose_ranges(
        storage_uid_map, id_mappings.get("UidMap") if isinstance(id_mappings, dict) else None)
    composed_g = _compose_ranges(
        storage_gid_map, id_mappings.get("GidMap") if isinstance(id_mappings, dict) else None)
    if composed_u:
        uid_ranges = tuple(composed_u)
    elif namespace == "keep-id":
        uid_ranges = tuple([(uid, uid, 1)]
                           + ([(ustart, 0, ucount)] if ucount else []))
    else:
        uid_ranges = tuple([(uid, 0, 1)] + ([(ustart, 1, ucount)] if ucount else []))
    if composed_g:
        gid_ranges = tuple(composed_g)
    elif namespace == "keep-id":
        gid_ranges = tuple([(gid, gid, 1)]
                           + ([(gstart, 0, gcount)] if gcount else []))
    else:
        gid_ranges = tuple([(gid, 0, 1)] + ([(gstart, 1, gcount)] if gcount else []))
    return IdMap(uid, gid, container_uid, container_gid,
                 ustart, ucount, gstart, gcount, namespace,
                 uid_ranges=uid_ranges, gid_ranges=gid_ranges)


@dataclass
class BackendMount:
    destination: str
    source: str
    type: str
    options: str
    root_fd: int
    is_dir: bool
    dev: int
    ino: int
    mnt_id: int

    def verify(self) -> None:
        try:
            descriptor = os.open(
                self.source, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            raise unavailable(f"persistent mount source is unavailable: {self.source}: {exc}") from exc
        try:
            current = os.fstat(descriptor)
            current_mnt = _mnt_id(descriptor)
        finally:
            os.close(descriptor)
        if (current.st_dev, current.st_ino) != (self.dev, self.ino) or current_mnt != self.mnt_id:
            raise unavailable(
                f"persistent mount source changed or was replaced: {self.source}")


class ViewBackend:
    def __init__(self, *, mountpoint: str, merged_fd: int, merged_stat,
                 mounts: list[dict], capabilities: list[str], id_map: IdMap,
                 view_uuid: str):
        self.mountpoint = mountpoint.rstrip("/") or "/"
        self.merged_fd = merged_fd
        self.merged_stat = merged_stat
        self.merged_mnt_id = _mnt_id(merged_fd)
        if self.merged_mnt_id is None:
            raise unavailable("cannot read mnt_id for the merged storage root")
        self.id_map = id_map
        self.view_uuid = view_uuid
        self.capabilities = sorted(
            {normalize_container_path(item) for item in capabilities
             if isinstance(item, str)},
            key=lambda item: len(PurePosixPath(item).parts), reverse=True)
        self.mounts: list[BackendMount] = []
        for item in mounts:
            destination = normalize_container_path(item["destination"])
            source = os.path.realpath(item["source"])
            try:
                descriptor = os.open(
                    source, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
            except OSError as exc:
                raise unavailable(f"persistent mount source is unavailable: {source}: {exc}") from exc
            try:
                observed = os.fstat(descriptor)
                is_dir = stat_module.S_ISDIR(observed.st_mode)
                if not (is_dir or stat_module.S_ISREG(observed.st_mode)):
                    raise unavailable(f"persistent mount source is not a file or directory: {source}")
                mount_mnt = _mnt_id(descriptor)
            except BaseException:
                os.close(descriptor)
                raise
            if mount_mnt is None:
                os.close(descriptor)
                raise unavailable("cannot read mnt_id for persistent mount source")
            self.mounts.append(BackendMount(
                destination=destination, source=source, type=item.get("type", "bind"),
                options=item.get("options", ""), root_fd=descriptor, is_dir=is_dir,
                dev=observed.st_dev, ino=observed.st_ino, mnt_id=mount_mnt))
        self.mounts.sort(key=lambda item: len(PurePosixPath(item.destination).parts),
                         reverse=True)

    def close(self) -> None:
        for mount in self.mounts:
            try:
                os.close(mount.root_fd)
            except OSError:
                pass

    # ----- backend selection and anchored resolution ----------------------
    def _select(self, path: str):
        path = normalize_container_path(path)
        best = None
        best_len = -1
        for mount in self.mounts:
            if path == mount.destination or path.startswith(mount.destination.rstrip("/") + "/"):
                length = len(PurePosixPath(mount.destination).parts)
                if length > best_len:
                    best, best_len = ("mount", mount), length
        for capability in self.capabilities:
            if path == capability or path.startswith(capability.rstrip("/") + "/"):
                length = len(PurePosixPath(capability).parts)
                if length > best_len:
                    best, best_len = ("capability", capability), length
        if best is not None and best[0] == "capability":
            raise ViewError(
                errno.ENOTSUP,
                f"runtime capability path {best[1]!r} is unavailable in a stopped view")
        if best is not None:
            mount = best[1]
            if not mount.is_dir and path != mount.destination:
                raise ViewError(errno.ENOTDIR, "a file mount has no children")
            return mount, _relative(path, mount.destination) if path != mount.destination else ""
        return None, path.lstrip("/")

    def _check_merged(self) -> None:
        try:
            observed = os.fstat(self.merged_fd)
        except OSError as exc:
            raise unavailable(f"merged root FD is unavailable: {exc}") from exc
        if (observed.st_dev, observed.st_ino) != (self.merged_stat.st_dev, self.merged_stat.st_ino):
            raise unavailable("merged storage root was replaced")
        if _mnt_id(self.merged_fd) != self.merged_mnt_id:
            raise unavailable("merged storage mount was replaced")
        # Stale/aborted FUSE is outside this FD, but a vanished mount yields EIO
        # on the next openat instead of an empty directory.

    def _walk(self, backend, relative: str, *, final_open: int | None = None):
        if backend is None:
            self._check_merged()
            root_fd = self.merged_fd
            is_dir_root = True
        else:
            backend.verify()
            root_fd = backend.root_fd
            is_dir_root = backend.is_dir
        parts = [part for part in relative.split("/") if part]
        if any(part in {".", ".."} for part in parts):
            raise ViewError(errno.EINVAL, "path contains an unsafe component")
        if not is_dir_root:
            if parts:
                raise ViewError(errno.ENOTDIR, "a file mount has no children")
            if final_open is not None:
                if final_open & os.O_DIRECTORY:
                    raise ViewError(errno.ENOTDIR, "not a directory")
                try:
                    descriptor = os.open(
                        backend.source, final_open | os.O_CLOEXEC | os.O_NOFOLLOW)
                except OSError as exc:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
                observed = os.fstat(descriptor)
                if (observed.st_dev, observed.st_ino) != (backend.dev, backend.ino):
                    os.close(descriptor)
                    raise unavailable("mounted file source was replaced")
                return descriptor
            return os.fstat(root_fd)
        current = root_fd
        owned: list[int] = []
        try:
            for part in parts[:-1]:
                try:
                    child = os.open(
                        part, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=current)
                except OSError as exc:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
                owned.append(child)
                current = child
            if not parts:
                try:
                    if final_open is not None:
                        return os.open(".", final_open | os.O_CLOEXEC, dir_fd=current)
                    return os.stat(".", dir_fd=current, follow_symlinks=False)
                except OSError as exc:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
            name = parts[-1]
            try:
                if final_open is not None:
                    return os.open(name, final_open | os.O_CLOEXEC | os.O_NOFOLLOW,
                                   dir_fd=current)
                return os.stat(name, dir_fd=current, follow_symlinks=False)
            except OSError as exc:
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
        finally:
            for descriptor in reversed(owned):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def _stat_view(self, observed) -> dict:
        return {
            "dev": observed.st_dev, "ino": observed.st_ino, "mode": observed.st_mode,
            "nlink": observed.st_nlink,
            "uid": self.id_map.uid(observed.st_uid), "gid": self.id_map.gid(observed.st_gid),
            "size": observed.st_size, "blocks": observed.st_blocks,
            "atime": int(observed.st_atime), "mtime": int(observed.st_mtime),
            "ctime": int(observed.st_ctime),
            "atimensec": observed.st_atime_ns % 1_000_000_000,
            "mtimensec": observed.st_mtime_ns % 1_000_000_000,
            "ctimensec": observed.st_ctime_ns % 1_000_000_000,
            "rdev": observed.st_rdev, "blksize": observed.st_blksize,
        }

    def getattr(self, path: str) -> dict:
        backend, relative = self._select(path)
        return self._stat_view(self._walk(backend, relative))

    def read(self, path: str, offset: int, size: int) -> bytes:
        backend, relative = self._select(path)
        descriptor = self._walk(backend, relative, final_open=os.O_RDONLY)
        try:
            observed = os.fstat(descriptor)
            if not stat_module.S_ISREG(observed.st_mode):
                raise ViewError(errno.EISDIR, "read requires a regular file")
            return os.pread(descriptor, size, offset)
        finally:
            os.close(descriptor)

    def readdir(self, path: str) -> list[dict]:
        backend, relative = self._select(path)
        descriptor = self._walk(backend, relative, final_open=os.O_RDONLY | os.O_DIRECTORY)
        try:
            names = sorted(os.listdir(descriptor))
            entries = [{"name": ".", "kind": "dir"}, {"name": "..", "kind": "dir"}]
            for name in names:
                try:
                    observed = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                except OSError as exc:
                    if exc.errno in {errno.ENOENT, errno.EACCES}:
                        continue
                    raise
                entries.append({"name": name, "kind": _kind(observed.st_mode),
                                "stat": self._stat_view(observed)})
            return entries
        finally:
            os.close(descriptor)

    def readlink_raw(self, path: str) -> str:
        backend, relative = self._select(path)
        parent, name = posixpath.split(relative)
        parent_fd = self._walk_dir(backend, parent)
        try:
            return os.readlink(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)

    def _walk_dir(self, backend, relative: str) -> int:
        if backend is None:
            self._check_merged()
            root_fd = self.merged_fd
        else:
            backend.verify()
            root_fd = backend.root_fd
        current = root_fd
        owned = []
        try:
            for part in [item for item in relative.split("/") if item]:
                try:
                    child = os.open(
                        part, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW,
                        dir_fd=current)
                except OSError as exc:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
                owned.append(child)
                current = child
            return os.open(".", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=current)
        finally:
            for descriptor in reversed(owned):
                try:
                    os.close(descriptor)
                except OSError:
                    pass

    def projected_link(self, path: str, raw: str) -> str:
        parent = posixpath.dirname(normalize_container_path(path))
        if raw.startswith("/"):
            target = posixpath.normpath(raw)
        else:
            target = posixpath.normpath(posixpath.join(parent, raw))
        if not target.startswith("/"):
            target = "/"
        return self.mountpoint if target == "/" else self.mountpoint + target

    def statfs(self) -> dict:
        observed = os.fstatvfs(self.merged_fd)
        return {
            "blocks": observed.f_blocks, "bfree": observed.f_bfree,
            "bavail": observed.f_bavail, "files": observed.f_files,
            "ffree": observed.f_ffree, "bsize": observed.f_bsize,
            "namelen": observed.f_namemax, "frsize": observed.f_frsize,
        }


def _kind(mode: int) -> str:
    if stat_module.S_ISDIR(mode):
        return "dir"
    if stat_module.S_ISLNK(mode):
        return "link"
    if stat_module.S_ISREG(mode):
        return "file"
    return "other"
