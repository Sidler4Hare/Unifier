"""Object, mount-graph and handle state for the read-only view projection.

The old view backend treated a container path as the identity of a file. This
module separates the three things that must not be conflated:

* the logical mount graph (which provider owns a path, including injected
  bind-mount destinations and the synthetic ancestors needed to reach them);
* a view-local node identified by provider instance/mount identity/dev/ino and
  pinned by an O_PATH anchor, so a replaced name never redirects an old node;
* independent open file/directory handles, each with a real O_RDONLY fd.

Nothing here mounts anything. The FUSE protocol adapter owns replies; this
module owns anchors, handles, directory snapshots, budgets and reference
counts. See ``设计/投影对象与目录修复-2026-10-26.md`` for the live read-only
contract.
"""

from dataclasses import dataclass, field
import errno
import hashlib
import os
from pathlib import Path, PurePosixPath
import posixpath
import stat as stat_module
import threading


class ViewError(Exception):
    def __init__(self, err, message: str):
        super().__init__(message)
        self.errno = err
        self.message = message


def unavailable(message: str) -> ViewError:
    return ViewError(errno.EIO, message)


DEFAULT_MAX_NODES = 65536
DEFAULT_MAX_HANDLES = 1024
DEFAULT_MAX_DIR_ENTRIES = 65536
DEFAULT_MAX_DIR_BYTES = 8 * 1024 * 1024

# External inode numbers are a pure function of view salt plus object identity,
# so they never depend on node/identity cache history. Only live nodes are kept
# in the collision table; it is not a permanent identity catalogue. A 64-bit
# BLAKE2 digest can collide (birthday bound), so simultaneous distinct keys are
# rejected with EOVERFLOW rather than aliased. The same key may reuse the same
# external number after its node is collected, matching native inode reuse.
_INODE_PERSON = b"ml-view-inode-v1"


def _external_inode(view_uuid: str, key: tuple) -> int:
    digest = hashlib.blake2b(digest_size=8, person=_INODE_PERSON)
    view = str(view_uuid).encode("utf-8", "surrogateescape")
    domain = b"S" if key and key[0] == "synthetic" else b"P"
    digest.update(domain)
    digest.update(len(view).to_bytes(8, "big"))
    digest.update(view)
    for item in key:
        if isinstance(item, int):
            tag, data = b"i", str(item).encode("ascii")
        elif isinstance(item, str):
            tag = b"s"
            data = item.encode("utf-8", "surrogateescape")
        else:
            tag, data = b"r", repr(item).encode("utf-8", "surrogateescape")
        digest.update(tag)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    value = int.from_bytes(digest.digest(), "big")
    # 1 is the stable root; never let a non-root identity claim it (or zero).
    return value if value > 1 else value + 2


def _mnt_id(descriptor: int) -> int | None:
    try:
        text = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="utf-8")
    except OSError as exc:
        if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOMEM}:
            raise ViewError(
                exc.errno, f"cannot read mnt_id for fd {descriptor}: {exc}") from exc
        return None
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
    return None


def normalize_container_path(path: str) -> str:
    if not isinstance(path, str):
        raise ViewError(errno.EINVAL, "a container view path must be a string")
    if not path.startswith("/") or "\x00" in path:
        raise ViewError(errno.EINVAL, "a container view path must be absolute")
    if ".." in path.split("/"):
        raise ViewError(errno.EINVAL, "container view path must not contain '..'")
    normalized = posixpath.normpath(path)
    if normalized != "/" and not normalized.startswith("/"):
        raise ViewError(errno.EINVAL, "container view path escaped its root")
    return normalized


def path_depth(path: str) -> int:
    return 0 if path == "/" else len(PurePosixPath(path).parts)


def is_under(path: str, root: str) -> bool:
    if root == "/":
        return True
    return path == root or path.startswith(root.rstrip("/") + "/")


def relative_to(path: str, root: str) -> str:
    path_parts = PurePosixPath(path).parts
    root_parts = PurePosixPath(root).parts
    if len(path_parts) < len(root_parts) or path_parts[:len(root_parts)] != root_parts:
        raise ViewError(errno.ENOENT, "path is outside the selected mount")
    return "/".join(path_parts[len(root_parts):])


def join_path(parent: str, name: str) -> str:
    if not isinstance(name, str) or not name or name in {".", ".."}:
        raise ViewError(errno.EINVAL, "invalid lookup name")
    if "/" in name or "\x00" in name:
        raise ViewError(errno.EINVAL, "lookup name must be one component")
    return normalize_container_path(posixpath.join(parent, name))


def kind_for_mode(mode: int) -> str:
    if stat_module.S_ISDIR(mode):
        return "dir"
    if stat_module.S_ISLNK(mode):
        return "link"
    if stat_module.S_ISREG(mode):
        return "file"
    return "other"


def _dup_path_fd(descriptor: int) -> int:
    """Duplicate a descriptor as O_PATH using the kernel's magic link.

    This never resolves a user-visible path. For an unlinked object the magic
    link still names the already-open object, which is exactly the anchor
    behaviour required for replacement/unlink tests.
    """
    try:
        return os.open(
            f"/proc/self/fd/{descriptor}", os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise ViewError(exc.errno or errno.EIO, str(exc)) from exc


def reopen_readonly(anchor_fd: int, flags: int) -> int:
    """Open a fresh read-only handle from a pinned O_PATH anchor."""
    try:
        return os.open(f"/proc/self/fd/{anchor_fd}", flags | os.O_CLOEXEC)
    except OSError as exc:
        raise ViewError(exc.errno or errno.EIO, str(exc)) from exc


@dataclass
class Provider:
    provider_id: str
    destination: str | None
    root_fd: int
    is_dir: bool
    dev: int
    ino: int
    mnt_id: int
    source: str | None = None
    type: str = "bind"
    options: str = ""
    owns_root: bool = True

    def verify(self) -> None:
        """Check the provider's source identity, but never a child path.

        A failed check makes new lookups under this provider fail closed. It
        does not rewrite or fall back to another provider. Already pinned nodes
        and handles remain usable by their own object identity.
        """
        if self.source is None:
            try:
                observed = os.fstat(self.root_fd)
                current_mnt = _mnt_id(self.root_fd)
            except OSError as exc:
                if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOMEM}:
                    raise ViewError(exc.errno, str(exc)) from exc
                raise unavailable(f"merged root FD is unavailable: {exc}") from exc
            if ((observed.st_dev, observed.st_ino) != (self.dev, self.ino)
                    or current_mnt != self.mnt_id):
                raise unavailable("merged storage root was replaced")
            return
        try:
            descriptor = os.open(
                self.source, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW)
        except OSError as exc:
            if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOMEM}:
                raise ViewError(exc.errno, str(exc)) from exc
            raise unavailable(
                f"persistent mount source is unavailable: {self.source}: {exc}") from exc
        try:
            current = os.fstat(descriptor)
            current_mnt = _mnt_id(descriptor)
        finally:
            os.close(descriptor)
        if ((current.st_dev, current.st_ino) != (self.dev, self.ino)
                or current_mnt != self.mnt_id):
            raise unavailable(
                f"persistent mount source changed or was replaced: {self.source}")


@dataclass(frozen=True)
class Selection:
    kind: str                 # "mount", "merged" or "unavailable"
    provider: Provider | None
    relative: str
    destination: str | None = None


class MountGraph:
    """One topology shared by lookup and readdir.

    Mount destinations always win over the merged storage namespace. Required
    ancestors are directories even when the merged namespace has a file or
    nothing at that name. Runtime capability paths are unavailable unless a
    deeper mount overrides them. A file mount has no children.
    """

    def __init__(self, merged: Provider, mounts: list[Provider],
                 capabilities: list[str]):
        self.merged = merged
        self.mounts = sorted(
            mounts, key=lambda item: path_depth(item.destination or "/"),
            reverse=True)
        self.by_destination: dict[str, Provider] = {}
        for mount in self.mounts:
            # First declaration wins for duplicate destinations, matching the
            # old longest-prefix selector.
            self.by_destination.setdefault(mount.destination, mount)
        self.capabilities = sorted(
            {normalize_container_path(item) for item in capabilities
             if isinstance(item, str)},
            key=path_depth, reverse=True)
        required = set()
        for destination in self.by_destination:
            parts = [part for part in destination.split("/") if part]
            for index in range(1, len(parts)):
                required.add("/" + "/".join(parts[:index]))
        required.discard("/")
        self.required = required

    def root_mount(self) -> Provider | None:
        return self.by_destination.get("/")

    def _deepest_mount(self, path: str) -> Provider | None:
        for mount in self.mounts:
            destination = mount.destination
            if destination and is_under(path, destination):
                return mount
        return None

    def _deepest_capability(self, path: str) -> str | None:
        for capability in self.capabilities:
            if is_under(path, capability):
                return capability
        return None

    def _reject_file_mount_child(self, path: str) -> None:
        for mount in self.mounts:
            if not mount.is_dir and mount.destination != path \
                    and is_under(path, mount.destination or "/"):
                raise ViewError(errno.ENOTDIR, "a file mount has no children")

    def select(self, path: str) -> Selection:
        path = normalize_container_path(path)
        self._reject_file_mount_child(path)
        mount = self._deepest_mount(path)
        capability = self._deepest_capability(path)
        mount_depth = path_depth(mount.destination) if mount is not None else -1
        capability_depth = path_depth(capability) if capability is not None else -1
        if mount is not None and mount_depth >= capability_depth:
            return Selection(
                "mount", mount,
                relative_to(path, mount.destination) if path != mount.destination else "",
                mount.destination)
        if capability is not None:
            return Selection("unavailable", None, "", capability)
        return Selection("merged", self.merged, path.lstrip("/"), None)

    def lookup_context(self, parent_path: str, name: str):
        child = join_path(parent_path, name)
        self._reject_file_mount_child(child)
        mount = self.by_destination.get(child)
        if mount is not None:
            return "mount", mount, child
        if child in self.required:
            return "required", None, child
        selection = self.select(child)
        if selection.kind == "unavailable":
            raise ViewError(
                errno.ENOTSUP,
                f"runtime capability path {selection.destination!r} "
                "is unavailable in a stopped view")
        return "regular", selection, child

    def injected_names(self, directory: str) -> set[str]:
        directory = normalize_container_path(directory)
        if directory != "/" and any(
                not mount.is_dir and mount.destination == directory
                for mount in self.mounts):
            return set()
        names = set()
        for destination in self.by_destination:
            if destination != directory and is_under(destination, directory):
                remainder = relative_to(destination, directory)
                first = remainder.split("/", 1)[0]
                if first:
                    names.add(first)
        for required in self.required:
            if required != directory and is_under(required, directory):
                remainder = relative_to(required, directory)
                first = remainder.split("/", 1)[0]
                if first:
                    names.add(first)
        return names

    def required_type(self, path: str) -> str:
        return "dir" if path in self.required else ""


@dataclass
class Node:
    nodeid: int
    inode: int
    key: tuple
    path: str
    mode: int
    provider: Provider | None = None
    anchor_fd: int | None = None
    synthetic: bool = False
    virtual: bool = False
    nlookup: int = 0
    opens: int = 0
    internal: int = 0
    attr_signature: tuple | None = None


@dataclass
class OpenHandle:
    fh: int
    nodeid: int
    kind: str                 # "file" or "dir"
    fd: int | None
    entries: list = field(default_factory=list)
    bytes_used: int = 0


class ProjectionObjects:
    """Reference-counted nodes, independent handles and bounded directories."""

    def __init__(self, graph: MountGraph, *, view_uuid: str = "",
                 max_nodes=DEFAULT_MAX_NODES,
                 max_handles=DEFAULT_MAX_HANDLES,
                 max_dir_entries=DEFAULT_MAX_DIR_ENTRIES,
                 max_dir_bytes=DEFAULT_MAX_DIR_BYTES):
        self.graph = graph
        self.view_uuid = str(view_uuid)
        self.max_nodes = int(max_nodes)
        self.max_handles = int(max_handles)
        self.max_dir_entries = int(max_dir_entries)
        self.max_dir_bytes = int(max_dir_bytes)
        self.lock = threading.RLock()
        self.nodes: dict[int, Node] = {}
        self.by_key: dict[tuple, Node] = {}
        self.handles: dict[int, OpenHandle] = {}
        self.next_nodeid = 1
        self.next_fh = 1
        self._inode_owners: dict[int, tuple] = {}
        self.inode_collisions = 0
        self.dir_entry_count = 0
        self.dir_bytes_used = 0
        self._closed = False
        self.root: Node | None = None
        self._create_root()

    # ----- provider / node creation ---------------------------------------
    def _check_open(self) -> None:
        if self._closed:
            raise ViewError(errno.EIO, "projection is closed")

    # ----- stable external inode identity ---------------------------------
    def _inode_for(self, key: tuple, *, root: bool = False) -> int:
        if root:
            return 1
        candidate = _external_inode(self.view_uuid, key)
        owner = self._inode_owners.get(candidate)
        if owner is not None and owner != key:
            self.inode_collisions += 1
            raise ViewError(
                errno.EOVERFLOW,
                "external inode hash collision between distinct live objects")
        return candidate

    def _allocate_node(self, key, anchor_fd, mode, path, provider,
                       *, synthetic=False, virtual=False, count_ref=False,
                       root=False):
        existing = self.by_key.get(key)
        if existing is not None:
            if anchor_fd is not None:
                try:
                    os.close(anchor_fd)
                except OSError:
                    pass
            if count_ref:
                existing.nlookup += 1
            return existing
        if len(self.nodes) >= self.max_nodes:
            if anchor_fd is not None:
                try:
                    os.close(anchor_fd)
                except OSError:
                    pass
            raise ViewError(
                errno.ENOSPC,
                "projection node budget exhausted; forget unused nodes first")
        inode = None
        try:
            inode = self._inode_for(key, root=root)
        except BaseException:
            if anchor_fd is not None:
                try:
                    os.close(anchor_fd)
                except OSError:
                    pass
            raise
        node = Node(
            nodeid=self.next_nodeid, inode=inode, key=key, path=path, mode=mode,
            provider=provider, anchor_fd=anchor_fd, synthetic=synthetic,
            virtual=virtual, nlookup=1 if count_ref else 0)
        self.next_nodeid += 1
        self.nodes[node.nodeid] = node
        self.by_key[key] = node
        self._inode_owners[inode] = key
        return node

    def _create_root(self) -> None:
        provider = self.graph.root_mount() or self.graph.merged
        provider.verify()
        anchor = _dup_path_fd(provider.root_fd)
        try:
            observed = os.fstat(anchor)
        except OSError as exc:
            os.close(anchor)
            raise unavailable(f"view root is unavailable: {exc}") from exc
        if not stat_module.S_ISDIR(observed.st_mode):
            os.close(anchor)
            raise unavailable("view root must be a directory")
        key = (provider.provider_id, provider.mnt_id,
               observed.st_dev, observed.st_ino)
        self.root = self._allocate_node(
            key, anchor, observed.st_mode, "/", provider,
            virtual=True, count_ref=False, root=True)

    # ----- lookup and object resolution -----------------------------------
    def get_node(self, nodeid: int) -> Node:
        node = self.nodes.get(nodeid)
        if node is None:
            raise ViewError(errno.ENOENT, "unknown FUSE node id")
        return node

    def lookup(self, parentid: int, name: str) -> Node:
        return self._lookup(parentid, name, count_ref=True)

    def resolve_path_retained(self, path: str) -> Node:
        """Resolve a path and atomically take an internal anchor reference."""
        with self.lock:
            node = self.resolve_path(path)
            node.internal += 1
            return node

    def resolve_path(self, path: str, *, count_ref=False) -> Node:
        with self.lock:
            self._check_open()
            path = normalize_container_path(path)
            node = self.root
            if node is None:
                raise ViewError(errno.EIO, "projection root is unavailable")
            if path == "/":
                if count_ref:
                    node.nlookup += 1
                return node
            for part in [item for item in path.split("/") if item]:
                node = self._lookup(node.nodeid, part, count_ref=count_ref)
            return node

    def _lookup(self, parentid: int, name: str, *, count_ref: bool) -> Node:
        with self.lock:
            self._check_open()
            parent = self.get_node(parentid)
            if not stat_module.S_ISDIR(parent.mode):
                if (parent.provider is not None
                        and parent.provider.destination is not None
                        and not parent.provider.is_dir):
                    raise ViewError(errno.ENOTDIR, "a file mount has no children")
                raise ViewError(errno.ENOTDIR, "lookup parent is not a directory")
            kind, value, child_path = self.graph.lookup_context(parent.path, name)
            if kind == "mount":
                return self._mount_node(value, child_path, count_ref=count_ref)
            if kind == "required":
                return self._required_node(
                    parent, name, child_path, count_ref=count_ref)
            selection = value
            provider = parent.provider
            if provider is None or parent.anchor_fd is None:
                raise ViewError(errno.ENOENT, "virtual directory has no such child")
            if selection.kind == "mount" and selection.provider is not provider:
                # A deeper mount can only be entered by its exact destination,
                # which lookup_context already handled. Treat anything else as
                # a topology change rather than silently crossing providers.
                raise unavailable("mount topology changed during lookup")
            provider.verify()
            try:
                anchor = os.open(
                    name, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent.anchor_fd)
            except OSError as exc:
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
            try:
                observed = os.fstat(anchor)
            except OSError as exc:
                os.close(anchor)
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
            key = (provider.provider_id, provider.mnt_id,
                   observed.st_dev, observed.st_ino)
            return self._allocate_node(
                key, anchor, observed.st_mode, child_path, provider,
                count_ref=count_ref)

    def _mount_node(self, mount: Provider, child_path: str, *, count_ref):
        mount.verify()
        anchor = _dup_path_fd(mount.root_fd)
        try:
            observed = os.fstat(anchor)
        except OSError as exc:
            os.close(anchor)
            raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
        if not (stat_module.S_ISDIR(observed.st_mode)
                or stat_module.S_ISREG(observed.st_mode)):
            os.close(anchor)
            raise unavailable("mounted source is not a file or directory")
        key = (mount.provider_id, mount.mnt_id,
               observed.st_dev, observed.st_ino)
        return self._allocate_node(
            key, anchor, observed.st_mode, child_path, mount,
            count_ref=count_ref)

    def _required_node(self, parent: Node, name: str, child_path: str,
                       *, count_ref):
        anchor = None
        observed = None
        provider = parent.provider
        tried_pinned_parent = (
            provider is not None and parent.anchor_fd is not None
            and stat_module.S_ISDIR(parent.mode))
        if tried_pinned_parent:
            provider.verify()
            candidate = None
            try:
                candidate = os.open(
                    name, os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW,
                    dir_fd=parent.anchor_fd)
                candidate_stat = os.fstat(candidate)
            except OSError as exc:
                if candidate is not None:
                    try:
                        os.close(candidate)
                    except OSError:
                        pass
                if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
                    candidate = None
                    candidate_stat = None
                else:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
            if candidate is not None and stat_module.S_ISDIR(candidate_stat.st_mode):
                anchor, observed = candidate, candidate_stat
            elif candidate is not None:
                os.close(candidate)
        if anchor is None and not tried_pinned_parent:
            # No pinned parent object exists. Resolve the required ancestor in
            # the provider selected by the shared graph, never by falling back
            # to a different provider. In particular a missing ancestor under
            # a mount must not read through to the merged namespace.
            try:
                selection = self.graph.select(child_path)
            except ViewError:
                selection = None
            provider = None
            relative = ""
            if selection is not None and selection.kind in {"mount", "merged"}:
                provider = selection.provider
                relative = selection.relative
            if provider is not None:
                provider.verify()
                try:
                    candidate, candidate_stat = self._open_provider_path(
                        provider, relative)
                except ViewError as exc:
                    if exc.errno not in {errno.ENOENT, errno.ENOTDIR}:
                        raise
                    candidate = None
                    candidate_stat = None
                if (candidate is not None
                        and stat_module.S_ISDIR(candidate_stat.st_mode)):
                    anchor, observed = candidate, candidate_stat
                elif candidate is not None:
                    os.close(candidate)
        if anchor is not None:
            key = (provider.provider_id, provider.mnt_id,
                   observed.st_dev, observed.st_ino)
            node = self._allocate_node(
                key, anchor, observed.st_mode, child_path, provider,
                virtual=True, count_ref=count_ref)
            node.virtual = True
            return node
        key = ("synthetic", child_path)
        return self._allocate_node(
            key, None, stat_module.S_IFDIR | 0o555, child_path, None,
            synthetic=True, virtual=True, count_ref=count_ref)

    def _open_provider_path(self, provider: Provider, relative: str):
        """Open a provider-relative path for a synthetic required ancestor.

        Ordinary lookup of a child always uses the pinned parent anchor, never
        a re-walked path. This helper exists only when no pinned parent object
        exists (a synthetic ancestor chain), and it is always rooted at the
        provider selected by MountGraph.
        """
        provider.verify()
        parts = [part for part in relative.split("/") if part]
        if not parts:
            anchor = _dup_path_fd(provider.root_fd)
            return anchor, os.fstat(anchor)
        current = provider.root_fd
        owned = []
        try:
            for index, part in enumerate(parts):
                flags = os.O_PATH | os.O_CLOEXEC | os.O_NOFOLLOW
                if index != len(parts) - 1:
                    flags |= os.O_DIRECTORY
                try:
                    child = os.open(part, flags, dir_fd=current)
                except OSError as exc:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
                if index == len(parts) - 1:
                    try:
                        observed = os.fstat(child)
                    except OSError:
                        os.close(child)
                        raise
                    return child, observed
                owned.append(child)
                current = child
        finally:
            for descriptor in reversed(owned):
                try:
                    os.close(descriptor)
                except OSError:
                    pass
        raise ViewError(errno.ENOENT, "empty path")

    # ----- attributes ------------------------------------------------------
    def node_stat(self, node: Node, fh: int = 0):
        with self.lock:
            if fh:
                handle = self.handles.get(fh)
                if handle is None:
                    raise ViewError(errno.EBADF, "unknown FUSE file handle")
                if handle.nodeid != node.nodeid:
                    raise ViewError(errno.EBADF, "file handle does not match node")
                try:
                    return os.fstat(handle.fd)
                except OSError as exc:
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
            if node.anchor_fd is None:
                return None
            try:
                return os.fstat(node.anchor_fd)
            except OSError as exc:
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc

    # ----- file handles ----------------------------------------------------
    def open_file(self, nodeid: int, flags: int) -> int:
        with self.lock:
            self._check_open()
            node = self.get_node(nodeid)
            if not stat_module.S_ISREG(node.mode):
                if stat_module.S_ISDIR(node.mode):
                    raise ViewError(errno.EISDIR, "opening a directory as a file")
                raise ViewError(errno.EINVAL, "only regular files can be opened")
            if (flags & os.O_ACCMODE) != os.O_RDONLY or flags & (
                    os.O_TRUNC | os.O_CREAT | os.O_EXCL):
                raise ViewError(errno.EROFS, "the projection is read-only")
            if len(self.handles) >= self.max_handles:
                raise ViewError(errno.EMFILE, "projection handle budget exhausted")
            if node.anchor_fd is None:
                raise ViewError(errno.EIO, "node has no pinned object")
            descriptor = reopen_readonly(
                node.anchor_fd, os.O_RDONLY | os.O_NOCTTY)
            try:
                observed = os.fstat(descriptor)
                anchor_stat = os.fstat(node.anchor_fd)
                if ((observed.st_dev, observed.st_ino)
                        != (anchor_stat.st_dev, anchor_stat.st_ino)):
                    raise unavailable("pinned file changed while opening")
                fh = self.next_fh
                self.next_fh += 1
                self.handles[fh] = OpenHandle(
                    fh=fh, nodeid=nodeid, kind="file", fd=descriptor)
                node.opens += 1
                return fh
            except BaseException:
                os.close(descriptor)
                raise

    def read_file(self, fh: int, offset: int, size: int) -> bytes:
        with self.lock:
            handle = self.handles.get(fh)
            if handle is None or handle.kind != "file":
                raise ViewError(errno.EBADF, "unknown FUSE file handle")
            if offset < 0 or size < 0:
                raise ViewError(errno.EINVAL, "negative read range")
            try:
                return os.pread(handle.fd, size, offset)
            except OSError as exc:
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc

    def release(self, fh: int) -> str:
        with self.lock:
            handle = self.handles.pop(fh, None)
            if handle is None:
                raise ViewError(errno.EBADF, "unknown FUSE handle")
            if handle.kind == "dir":
                self.dir_entry_count -= len(handle.entries)
                self.dir_bytes_used -= handle.bytes_used
                self._release_snapshot_refs(handle.entries)
            try:
                if handle.fd is not None:
                    os.close(handle.fd)
            except OSError:
                pass
            node = self.nodes.get(handle.nodeid)
            if node is not None:
                node.opens = max(0, node.opens - 1)
                self._collect(node)
            return handle.kind

    # ----- directory streams ----------------------------------------------
    def _snapshot(self, node: Node, directory_fd: int | None,
                  *, include_stat=False, enforce_budget=False):
        graph = self.graph
        injected = graph.injected_names(node.path)
        names = []
        scan_cost = len(".") + len("..") + 2 * 64
        available_entries = None
        available_bytes = None
        if enforce_budget:
            available_entries = self.max_dir_entries - self.dir_entry_count
            available_bytes = self.max_dir_bytes - self.dir_bytes_used
            if 2 > available_entries or scan_cost > available_bytes:
                raise ViewError(
                    errno.ENOSPC, "directory snapshot budget exhausted")
        if directory_fd is not None:
            # Stop the scan as soon as the *aggregate* remaining budget is
            # exhausted. The declared bytes budget is accounting, not an RSS
            # or kernel-memory promise.
            try:
                with os.scandir(directory_fd) as iterator:
                    for item in iterator:
                        name = item.name
                        names.append(name)
                        scan_cost += len(
                            name.encode("utf-8", "surrogateescape")) + 64
                        if enforce_budget and (
                                len(names) + 2 > available_entries
                                or scan_cost > available_bytes):
                            raise ViewError(
                                errno.ENOSPC,
                                "directory snapshot budget exhausted")
            except ViewError:
                raise
            except OSError as exc:
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
            names.sort()
        union = sorted(set(names) | injected)
        total_cost = len(".") + len("..") + 2 * 64 + sum(
            len(name.encode("utf-8", "surrogateescape")) + 64
            for name in union)
        if enforce_budget and (
                len(union) + 2 > available_entries
                or total_cost > available_bytes):
            raise ViewError(
                errno.ENOSPC, "directory snapshot budget exhausted")

        parent_mode = node.mode
        entries = [
            {"name": ".", "kind": "dir", "ino": node.inode,
             "mode": parent_mode, "synthetic": True, "held": False},
            {"name": "..", "kind": "dir", "ino": node.inode,
             "mode": parent_mode, "synthetic": True, "held": False},
        ]
        held_nodeids = []
        try:
            for name in union:
                try:
                    child = self._lookup(
                        node.nodeid, name, count_ref=False)
                except ViewError as exc:
                    if exc.errno == errno.ENOTSUP:
                        # Capability names stay unavailable and hidden.
                        continue
                    if exc.errno in {errno.ENOENT, errno.EACCES}:
                        # A name may vanish or be unreadable between scandir
                        # and lookup; it must not become a fabricated node.
                        continue
                    raise
                except OSError as exc:
                    if exc.errno in {errno.ENOENT, errno.EACCES}:
                        continue
                    raise ViewError(exc.errno or errno.EIO, str(exc)) from exc
                child.internal += 1
                held_nodeids.append(child.nodeid)
                observed = None
                mode = child.mode
                if include_stat:
                    observed = self.node_stat(child)
                    if observed is not None and not child.synthetic:
                        mode = observed.st_mode
                entry = {
                    "name": name, "kind": kind_for_mode(child.mode),
                    "ino": child.inode, "mode": mode,
                    "synthetic": child.synthetic, "held": True,
                    "nodeid": child.nodeid,
                }
                if include_stat:
                    entry["observed"] = observed
                    if observed is not None and not child.synthetic:
                        entry["stat"] = observed
                entries.append(entry)
            return entries
        except BaseException:
            for nodeid in reversed(held_nodeids):
                self.release_internal(nodeid)
            raise

    def snapshot_entries(self, node: Node, *, include_stat=False):
        """Build a one-shot merged listing for the path-level compatibility API.

        Entry nodes are retained while the snapshot is assembled, then the
        temporary internal references are released before returning. This keeps
        zero-lookup path calls from growing the node/anchor table while still
        allowing a concurrent FUSE lookup to share the same projected inode.
        """
        with self.lock:
            if not stat_module.S_ISDIR(node.mode):
                if (node.provider is not None
                        and node.provider.destination is not None
                        and not node.provider.is_dir):
                    raise ViewError(
                        errno.ENOTDIR, "a file mount has no children")
                raise ViewError(errno.ENOTDIR, "not a directory")
            descriptor = None
            if node.anchor_fd is not None:
                descriptor = reopen_readonly(
                    node.anchor_fd, os.O_RDONLY | os.O_DIRECTORY)
            try:
                entries = self._snapshot(
                    node, descriptor, include_stat=include_stat)
            finally:
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
            try:
                return entries
            finally:
                self._release_snapshot_refs(entries)

    def _release_snapshot_refs(self, entries) -> None:
        for entry in reversed(entries):
            if entry.get("held"):
                self.release_internal(entry["nodeid"])

    def collect_unreferenced(self) -> None:
        with self.lock:
            for node in list(self.nodes.values()):
                if node is self.root:
                    continue
                if not node.nlookup and not node.opens and not node.internal:
                    self._collect(node)

    def _snapshot_cost(self, entries) -> int:
        return sum(len(item["name"].encode("utf-8", "surrogateescape")) + 64
                   for item in entries)

    def opendir(self, nodeid: int) -> int:
        with self.lock:
            self._check_open()
            node = self.get_node(nodeid)
            if not stat_module.S_ISDIR(node.mode):
                raise ViewError(errno.ENOTDIR, "not a directory")
            if len(self.handles) >= self.max_handles:
                raise ViewError(errno.EMFILE, "projection handle budget exhausted")
            descriptor = None
            if node.anchor_fd is not None:
                descriptor = reopen_readonly(
                    node.anchor_fd, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    observed = os.fstat(descriptor)
                    anchor_stat = os.fstat(node.anchor_fd)
                    if ((observed.st_dev, observed.st_ino)
                            != (anchor_stat.st_dev, anchor_stat.st_ino)):
                        raise unavailable("pinned directory changed while opening")
                except BaseException:
                    os.close(descriptor)
                    raise
            entries = None
            try:
                entries = self._snapshot(
                    node, descriptor, include_stat=False,
                    enforce_budget=True)
                cost = self._snapshot_cost(entries)
                fh = self.next_fh
                self.next_fh += 1
                self.handles[fh] = OpenHandle(
                    fh=fh, nodeid=nodeid, kind="dir", fd=descriptor,
                    entries=entries, bytes_used=cost)
                node.opens += 1
                self.dir_entry_count += len(entries)
                self.dir_bytes_used += cost
                return fh
            except BaseException:
                if entries is not None:
                    self._release_snapshot_refs(entries)
                if descriptor is not None:
                    try:
                        os.close(descriptor)
                    except OSError:
                        pass
                raise

    def readdir(self, fh: int, cookie: int):
        with self.lock:
            handle = self.handles.get(fh)
            if handle is None or handle.kind != "dir":
                raise ViewError(errno.EBADF, "unknown FUSE directory handle")
            if cookie < 0:
                raise ViewError(errno.EINVAL, "negative directory cookie")
            if cookie > len(handle.entries):
                raise ViewError(errno.EINVAL, "directory cookie is out of range")
            return cookie, handle.entries[cookie:]

    # ----- links and lifecycle --------------------------------------------
    def readlink(self, nodeid: int) -> str:
        with self.lock:
            node = self.get_node(nodeid)
            if not stat_module.S_ISLNK(node.mode):
                raise ViewError(errno.EINVAL, "not a symbolic link")
            if node.anchor_fd is None:
                raise ViewError(errno.EIO, "link has no pinned object")
            try:
                return os.readlink("", dir_fd=node.anchor_fd)
            except OSError as exc:
                raise ViewError(exc.errno or errno.EIO, str(exc)) from exc

    def retain_internal(self, nodeid: int) -> Node:
        with self.lock:
            node = self.get_node(nodeid)
            node.internal += 1
            return node

    def release_internal(self, nodeid: int) -> None:
        with self.lock:
            node = self.nodes.get(nodeid)
            if node is None:
                return
            node.internal = max(0, node.internal - 1)
            self._collect(node)

    def forget(self, nodeid: int, count: int) -> None:
        with self.lock:
            node = self.nodes.get(nodeid)
            if node is None or node is self.root:
                return
            node.nlookup = max(0, node.nlookup - max(0, int(count)))
            self._collect(node)

    def batch_forget(self, pairs) -> None:
        with self.lock:
            for nodeid, count in pairs:
                node = self.nodes.get(nodeid)
                if node is None or node is self.root:
                    continue
                node.nlookup = max(0, node.nlookup - max(0, int(count)))
                self._collect(node)

    def _collect(self, node: Node) -> None:
        if node is self.root:
            return
        if node.nlookup or node.opens or node.internal:
            return
        current = self.by_key.get(node.key)
        if current is node:
            del self.by_key[node.key]
        self.nodes.pop(node.nodeid, None)
        if self._inode_owners.get(node.inode) == node.key:
            del self._inode_owners[node.inode]
        if node.anchor_fd is not None:
            try:
                os.close(node.anchor_fd)
            except OSError:
                pass
            node.anchor_fd = None

    def handle_nodeid(self, fh: int) -> int:
        handle = self.handles.get(fh)
        if handle is None:
            raise ViewError(errno.EBADF, "unknown FUSE handle")
        return handle.nodeid

    def handle_fd(self, fh: int) -> int:
        handle = self.handles.get(fh)
        if handle is None or handle.fd is None:
            raise ViewError(errno.EBADF, "FUSE handle has no file descriptor")
        return handle.fd

    def stats(self) -> dict:
        with self.lock:
            file_handles = sum(
                1 for handle in self.handles.values() if handle.kind == "file")
            dir_handles = len(self.handles) - file_handles
            return {
                "nodes": len(self.nodes),
                "node_budget": self.max_nodes,
                "handles": len(self.handles),
                "file_handles": file_handles,
                "dir_handles": dir_handles,
                "handle_budget": self.max_handles,
                "dir_entries": self.dir_entry_count,
                "dir_entry_budget": self.max_dir_entries,
                "dir_bytes": self.dir_bytes_used,
                "dir_bytes_budget": self.max_dir_bytes,
                "next_nodeid": self.next_nodeid,
                "next_handle": self.next_fh,
                "inode_ids": len(self._inode_owners),
                "attr_versions": sum(
                    node.attr_signature is not None
                    for node in self.nodes.values()),
                "inode_collisions": self.inode_collisions,
            }

    def close(self) -> None:
        with self.lock:
            if self._closed:
                return
            self._closed = True
            for handle in list(self.handles.values()):
                try:
                    if handle.fd is not None:
                        os.close(handle.fd)
                except OSError:
                    pass
            self.handles.clear()
            self.dir_entry_count = 0
            self.dir_bytes_used = 0
            for node in list(self.nodes.values()):
                if node.anchor_fd is not None:
                    try:
                        os.close(node.anchor_fd)
                    except OSError:
                        pass
                    node.anchor_fd = None
            self.nodes.clear()
            self.by_key.clear()
            self._inode_owners.clear()
            self.root = None
