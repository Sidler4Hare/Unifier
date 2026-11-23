"""Safe read-only backend for a stopped-environment FUSE view.

Paths are container paths. The backend selects a provider from one shared
MountGraph, and resolves every new name relative to the pinned parent object
with O_NOFOLLOW. Open files and directory streams use independent real
descriptors; a path is never re-walked for READ, GETATTR(fh) or READLINK.

This object-anchored implementation serves ``unifier view _fuse``
and the historical view backend import.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import errno
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat as stat_module

from unifier.projection import (
    DEFAULT_MAX_DIR_BYTES, DEFAULT_MAX_DIR_ENTRIES, DEFAULT_MAX_HANDLES,
    DEFAULT_MAX_NODES, FdBudget, MountGraph, ProjectionObjects, Provider, Selection,
    ViewError, _mnt_id, kind_for_mode, normalize_container_path, path_depth,
    reopen_readonly, unavailable,
)


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


def _relative(path: str, root: str) -> str:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    if len(path_parts) < len(root_parts) or path_parts[:len(root_parts)] != root_parts:
        raise ViewError(errno.ENOENT, "path is outside the selected mount")
    remainder = path_parts[len(root_parts):]
    return "/".join(remainder)


class ViewBackend:
    def __init__(self, *, mountpoint: str, merged_fd: int, merged_stat,
                 mounts: list[dict], capabilities: list[str], id_map: IdMap,
                 view_uuid: str, max_nodes: int = DEFAULT_MAX_NODES,
                 max_handles: int = DEFAULT_MAX_HANDLES,
                 max_dir_entries: int = DEFAULT_MAX_DIR_ENTRIES,
                 max_dir_bytes: int = DEFAULT_MAX_DIR_BYTES):
        self.mountpoint = mountpoint.rstrip("/") or "/"
        self.merged_fd = merged_fd
        self.merged_stat = merged_stat
        self.fd_budget = FdBudget()
        self.fd_budget.require()
        self.merged_mnt_id = _mnt_id(merged_fd)
        if self.merged_mnt_id is None:
            raise unavailable("cannot read mnt_id for the merged storage root")
        self.id_map = id_map
        self.view_uuid = view_uuid
        self.capabilities = sorted(
            {normalize_container_path(item) for item in capabilities
             if isinstance(item, str)},
            key=path_depth, reverse=True)
        providers: list[Provider] = []
        try:
            for index, item in enumerate(mounts):
                self.fd_budget.require()
                destination = normalize_container_path(item["destination"])
                source = os.path.realpath(item["source"])
                try:
                    descriptor = os.open(
                        source, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
                except OSError as exc:
                    if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOMEM}:
                        raise ViewError(exc.errno, str(exc)) from exc
                    raise unavailable(
                        f"persistent mount source is unavailable: {source}: {exc}") from exc
                try:
                    observed = os.fstat(descriptor)
                    is_dir = stat_module.S_ISDIR(observed.st_mode)
                    if not (is_dir or stat_module.S_ISREG(observed.st_mode)):
                        raise unavailable(
                            "persistent mount source is not a file or directory: "
                            f"{source}")
                    mount_mnt = _mnt_id(descriptor)
                    if mount_mnt is None:
                        raise unavailable(
                            "cannot read mnt_id for persistent mount source")
                except BaseException:
                    os.close(descriptor)
                    raise
                providers.append(Provider(
                    provider_id=f"mount:{index}", destination=destination,
                    root_fd=descriptor, is_dir=is_dir,
                    dev=observed.st_dev, ino=observed.st_ino,
                    mnt_id=mount_mnt, source=source,
                    type=item.get("type", "bind"),
                    options=item.get("options", ""), owns_root=True))
            merged_provider = Provider(
                provider_id="merged", destination=None, root_fd=merged_fd,
                is_dir=True, dev=merged_stat.st_dev, ino=merged_stat.st_ino,
                mnt_id=self.merged_mnt_id, source=None,
                type="merged-root", owns_root=False)
            self.graph = MountGraph(
                merged_provider, providers, self.capabilities)
            self.mounts = providers
            self.merged = merged_provider
            self.objects = ProjectionObjects(
                self.graph, view_uuid=self.view_uuid,
                max_nodes=max_nodes, max_handles=max_handles,
                max_dir_entries=max_dir_entries, max_dir_bytes=max_dir_bytes,
                fd_budget=self.fd_budget)
        except BaseException:
            for provider in providers:
                try:
                    os.close(provider.root_fd)
                except OSError:
                    pass
            raise
        self._closed = False

    # ----- path-level compatibility API -----------------------------------
    def resolve_path(self, path: str):
        # Administration callers have no lookup reference. Collect zero-ref
        # nodes left by an earlier one-shot resolution before creating the next
        # temporary node; path-level operations below do this automatically.
        self.objects.collect_unreferenced()
        try:
            return self.objects.resolve_path(path)
        except BaseException:
            self.objects.collect_unreferenced()
            raise

    @contextmanager
    def _hold_path(self, path: str):
        """Keep a path-resolved node alive for one compatibility operation.

        Path-level callers have no kernel nlookup reference, so they use the
        node table's internal reference instead of assuming the FUSE thread
        will not deliver FORGET concurrently.
        """
        node = None
        try:
            node = self.objects.resolve_path_retained(path)
            yield node
        finally:
            if node is not None:
                self.objects.release_internal(node.nodeid)
            # Path-level callers have no kernel lookup reference. Reclaim the
            # temporary target plus any zero-reference parent nodes created by
            # this resolution; live FUSE nlookup/open/internal refs stay.
            self.objects.collect_unreferenced()

    def _stat_view(self, observed, *, ino=None, mode=None) -> dict:
        return {
            "dev": observed.st_dev,
            "ino": observed.st_ino if ino is None else ino,
            "mode": observed.st_mode if mode is None else mode,
            "nlink": observed.st_nlink,
            "uid": self.id_map.uid(observed.st_uid),
            "gid": self.id_map.gid(observed.st_gid),
            "size": observed.st_size, "blocks": observed.st_blocks,
            "atime": int(observed.st_atime), "mtime": int(observed.st_mtime),
            "ctime": int(observed.st_ctime),
            "atimensec": observed.st_atime_ns % 1_000_000_000,
            "mtimensec": observed.st_mtime_ns % 1_000_000_000,
            "ctimensec": observed.st_ctime_ns % 1_000_000_000,
            "rdev": observed.st_rdev, "blksize": observed.st_blksize,
        }

    def _synthetic_view(self, node) -> dict:
        uid = self.id_map.uid(self.id_map.host_uid)
        gid = self.id_map.gid(self.id_map.host_gid)
        return {
            "dev": 0, "ino": node.inode, "mode": node.mode, "nlink": 2,
            "uid": uid, "gid": gid, "size": 0, "blocks": 0,
            "atime": 0, "mtime": 0, "ctime": 0,
            "atimensec": 0, "mtimensec": 0, "ctimensec": 0,
            "rdev": 0, "blksize": 4096,
        }

    def node_attr(self, node, fh: int = 0) -> dict:
        observed = self.objects.node_stat(node, fh)
        if observed is None:
            return self._synthetic_view(node)
        # Use the live fstat mode, not the lookup-time copy: chmod/ACL-visible
        # bits must reflect the pinned object, while node.mode only decides
        # object type and forced synthetic directories.
        return self._stat_view(observed, ino=node.inode)

    def getattr(self, path: str) -> dict:
        with self._hold_path(path) as node:
            return self.node_attr(node)

    def read(self, path: str, offset: int, size: int) -> bytes:
        with self._hold_path(path) as node:
            if not stat_module.S_ISREG(node.mode):
                raise ViewError(errno.EISDIR, "read requires a regular file")
            if node.anchor_fd is None:
                raise ViewError(errno.EIO, "file has no pinned object")
            fh = self.objects.open_file(node.nodeid, os.O_RDONLY)
            try:
                return self.objects.read_file(fh, offset, size)
            finally:
                self.objects.release(fh)

    def readdir(self, path: str) -> list[dict]:
        with self._hold_path(path) as node:
            entries = self.objects.snapshot_entries(node, include_stat=True)
            result = []
            for entry in entries:
                observed = entry.get("observed")
                if observed is None or entry.get("synthetic"):
                    stat_view = {
                        "dev": 0, "ino": entry["ino"], "mode": entry["mode"],
                        "nlink": 2,
                        "uid": self.id_map.uid(self.id_map.host_uid),
                        "gid": self.id_map.gid(self.id_map.host_gid),
                        "size": 0, "blocks": 0, "atime": 0, "mtime": 0,
                        "ctime": 0, "atimensec": 0, "mtimensec": 0,
                        "ctimensec": 0, "rdev": 0, "blksize": 4096,
                    }
                else:
                    stat_view = self._stat_view(
                        observed, ino=entry["ino"], mode=entry["mode"])
                result.append({
                    "name": entry["name"], "kind": entry["kind"],
                    "stat": stat_view})
            return result

    def readlink_raw(self, path: str) -> str:
        with self._hold_path(path) as node:
            return self.objects.readlink(node.nodeid)

    MAX_SYMLINK_FOLLOWS = 40

    @staticmethod
    def _link_components(text: str) -> list[str]:
        """Split a link target without collapsing ``.``/``..`` components."""
        parts = [part for part in text.split("/") if part]
        # A trailing slash requires the final component to be a directory.
        # Preserve that requirement as an explicit "." component, where the
        # resolver will either follow a final symlink or report ENOTDIR.
        if text.endswith("/") and text != "/":
            parts.append(".")
        return parts

    def _link_component_kind(self, path: str):
        """Return ``(kind, raw_target)`` for one component in the view.

        This is a lstat-style question about the projection namespace: the
        node is pinned by an O_PATH anchor and readlink uses that anchor.
        """
        try:
            with self._hold_path(path) as node:
                if stat_module.S_ISLNK(node.mode):
                    return "link", self.objects.readlink(node.nodeid)
                if stat_module.S_ISDIR(node.mode):
                    return "dir", None
                return "other", None
        except ViewError as exc:
            if exc.errno in {
                    errno.ENOENT, errno.ENOTDIR, errno.ENOTSUP,
                    errno.EACCES}:
                return "unavailable", exc
            raise

    def projected_link(self, path: str, raw: str) -> str:
        """Project a link target while resolving every intermediate symlink.

        Lexical ``normpath`` is wrong for ``jump/../target``: POSIX follows
        ``jump`` first and only then applies ``..``.  Resolve in component
        order inside the view's mount graph, clamp ``..`` at the view root,
        and keep plain dangling suffixes textual. An unresolved suffix with
        ``..`` fails explicitly rather than escaping or inventing a target.
        """
        normalized = normalize_container_path(path)
        if normalized == "/":
            raise ViewError(errno.EINVAL, "the view root is not a symlink")
        parent = posixpath.dirname(normalized) or "/"
        if raw.startswith("/"):
            stack: list[str] = []
        else:
            stack = [part for part in parent.split("/") if part]
        pending = self._link_components(raw)
        followed = 0
        while pending:
            component = pending.pop(0)
            if component == ".":
                continue
            if component == "..":
                if stack:
                    stack.pop()
                # At the projection root, ".." stays at the root. It must
                # never climb into the host namespace that contains the mount.
                continue
            if not pending:
                # The final component is returned textually, even when it is
                # a symlink or dangling, so READLINK keeps normal display.
                stack.append(component)
                break
            candidate = "/" + "/".join(stack + [component])
            kind, target = self._link_component_kind(candidate)
            if kind == "link":
                followed += 1
                if followed > self.MAX_SYMLINK_FOLLOWS:
                    raise ViewError(
                        errno.ELOOP, "too many symbolic links in link target")
                if target.startswith("/"):
                    stack = []
                pending = self._link_components(target) + pending
                continue
            if kind == "dir":
                stack.append(component)
                continue
            # Never export unresolved "..": a missing prefix may appear
            # before this target is followed and then permit a host escape.
            # Dropping it would instead fabricate a different target. Refuse
            # this ambiguous suffix while keeping plain dangling text useful.
            if ".." in pending:
                if kind == "unavailable":
                    raise target
                raise ViewError(errno.ENOTDIR, "non-directory in link target")
            stack.append(component)
            stack.extend(pending)
            pending = []
            break
        target = "/" + "/".join(stack) if stack else "/"
        return self.mountpoint if target == "/" else self.mountpoint + target

    def statfs(self) -> dict:
        observed = os.fstatvfs(self.merged_fd)
        return {
            "blocks": observed.f_blocks, "bfree": observed.f_bfree,
            "bavail": observed.f_bavail, "files": observed.f_files,
            "ffree": observed.f_ffree, "bsize": observed.f_bsize,
            "namelen": observed.f_namemax, "frsize": observed.f_frsize,
        }

    # ----- object/handle API used by the FUSE adapter ----------------------
    def lookup_node(self, parentid: int, name: str):
        return self.objects.lookup(parentid, name)

    def get_node(self, nodeid: int):
        return self.objects.get_node(nodeid)

    def forget_node(self, nodeid: int, count: int) -> None:
        self.objects.forget(nodeid, count)

    def retain_node_internal(self, nodeid: int):
        return self.objects.retain_internal(nodeid)

    def release_node_internal(self, nodeid: int) -> None:
        self.objects.release_internal(nodeid)

    def batch_forget(self, pairs) -> None:
        self.objects.batch_forget(pairs)

    def open_node(self, nodeid: int, flags: int) -> int:
        return self.objects.open_file(nodeid, flags)

    def open_dir(self, nodeid: int) -> int:
        return self.objects.opendir(nodeid)

    def read_handle(self, fh: int, offset: int, size: int) -> bytes:
        return self.objects.read_file(fh, offset, size)

    def release_handle(self, fh: int) -> str:
        return self.objects.release(fh)

    def readdir_handle(self, fh: int, cookie: int):
        return self.objects.readdir(fh, cookie)

    def readlink_node(self, nodeid: int) -> str:
        return self.objects.readlink(nodeid)

    def node_attr_by_id(self, nodeid: int, fh: int = 0) -> dict:
        return self.node_attr(self.objects.get_node(nodeid), fh)

    def handle_nodeid(self, fh: int) -> int:
        return self.objects.handle_nodeid(fh)

    def handle_fd(self, fh: int) -> int:
        return self.objects.handle_fd(fh)

    def stats(self) -> dict:
        return self.objects.stats()

    def consistency(self) -> dict:
        return {
            "mode": "live-readonly",
            "readonly": True,
            "live": True,
            "snapshot": False,
            "watcher": False,
            "cross_volume_consistent": False,
            "entry_ttl": 0,
            "attr_ttl": 0,
            "direct_io": False,
            "mmap": "page-cache",
            "data_invalidation": "regular-file-on-every-revalidation",
            "xattr": "unsupported",
            "acl": "unknown",
            "permissions": "not-default_permissions",
        }

    def close(self) -> None:
        if getattr(self, "_closed", True):
            return
        self._closed = True
        self.objects.close()
        for provider in self.mounts:
            if provider.owns_root:
                try:
                    os.close(provider.root_fd)
                except OSError:
                    pass
