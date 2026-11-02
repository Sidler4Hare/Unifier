"""Shared isolated fixtures for projection tests; never creates a FUSE mount."""

import contextlib
import errno
import os
from pathlib import Path
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from multilinux import fuse_view
from multilinux.view_backend import IdMap, ViewBackend, ViewError


class CaptureServer(fuse_view.FuseServer):
    def __init__(self, backend, **kwargs):
        super().__init__(
            -1, backend, mountpoint=backend.mountpoint, control_socket="",
            view_uuid="view-test", **kwargs)
        self.sent = []
        self.notifications = []
        self.events = []

    def _send(self, unique, payload=b"", error=0):
        self.sent.append((unique, payload, error))
        self.events.append(("reply", unique, error, payload))

    def _notify(self, code, payload):
        self.notifications.append((0, code, payload))
        self.events.append(("notify", 0, code, payload))
        return True

    def call(self, opcode, node, body=b""):
        self.sent.clear()
        self.notifications.clear()
        self.events.clear()
        try:
            self._handle(opcode, node, 1, body)
        except (ViewError, OSError) as exc:
            self.sent = [(1, b"", exc.errno)]
        return self.sent[-1] if self.sent else None

    def lookup(self, name, parent=fuse_view.FUSE_ROOT_ID, *, unique=1):
        reply = self.call(
            fuse_view.FUSE_LOOKUP, parent, name.encode() + b"\x00")
        if reply is None or reply[2] != 0:
            raise AssertionError(f"lookup {name!r} failed: {reply!r}")
        return struct.unpack_from("<Q", reply[1], 0)[0]

    def open_file(self, node, flags=os.O_RDONLY):
        reply = self.call(
            fuse_view.FUSE_OPEN, node, struct.pack("<II", flags, 0))
        if reply is None or reply[2] != 0:
            raise AssertionError(f"open failed: {reply!r}")
        return struct.unpack_from("<Q", reply[1], 0)[0]

    def opendir(self, node=fuse_view.FUSE_ROOT_ID):
        reply = self.call(
            fuse_view.FUSE_OPENDIR, node, struct.pack("<I", os.O_RDONLY))
        if reply is None or reply[2] != 0:
            raise AssertionError(f"opendir failed: {reply!r}")
        return struct.unpack_from("<Q", reply[1], 0)[0]

    def read(self, fh, offset=0, size=4096, node=0):
        return self.call(
            fuse_view.FUSE_READ, node,
            struct.pack("<QQI", fh, offset, size))

    def getattr(self, node, fh=0, getattr_flags=None):
        if getattr_flags is None:
            getattr_flags = (
                fuse_view.FUSE_GETATTR_FH if fh else 0)
        body = (
            struct.pack("<IIQ", getattr_flags, 0, fh)
            if fh or getattr_flags else b"")
        return self.call(fuse_view.FUSE_GETATTR, node, body)

    def readdir(self, fh, cookie=0, size=4096):
        return self.call(
            fuse_view.FUSE_READDIR, 1,
            struct.pack("<QQI", fh, cookie, size))

    def release(self, fh):
        return self.call(
            fuse_view.FUSE_RELEASE, 0, struct.pack("<Q", fh))

    def releasedir(self, fh):
        return self.call(
            fuse_view.FUSE_RELEASEDIR, 0, struct.pack("<Q", fh))

    def forget(self, node, count=1):
        return self.call(
            fuse_view.FUSE_FORGET, node, struct.pack("<Q", count))

    def batch_forget(self, pairs):
        body = struct.pack("<II", len(pairs), 0)
        for node, count in pairs:
            body += struct.pack("<QQ", node, count)
        return self.call(fuse_view.FUSE_BATCH_FORGET, 0, body)


def backend_for(root: Path, *, mounts=(), capabilities=("/proc",), **kwargs):
    merged = os.open(root, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        backend = ViewBackend(
            mountpoint=str(root / "view"), merged_fd=merged,
            merged_stat=os.fstat(merged), mounts=list(mounts),
            capabilities=list(capabilities),
            id_map=IdMap(
                os.getuid(), os.getgid(), 0, 0, 0, 0, 0, 0,
                "rootless-default"),
            view_uuid="view-test", **kwargs)
    except BaseException:
        os.close(merged)
        raise
    return backend, merged


@contextlib.contextmanager
def fixture(root: Path | None = None, *, mounts=(), capabilities=("/proc",),
            **kwargs):
    if root is None:
        import tempfile
        manager = tempfile.TemporaryDirectory()
        directory = manager.__enter__()
    else:
        manager = None
        directory = str(root)
    backend, merged = backend_for(
        Path(directory), mounts=mounts, capabilities=capabilities, **kwargs)
    server = CaptureServer(backend)
    try:
        yield Path(directory), backend, server
    finally:
        backend.close()
        os.close(merged)
        if manager is not None:
            manager.__exit__(None, None, None)


def parse_dirents(blob):
    result = []
    cursor = 0
    while cursor < len(blob):
        if cursor + fuse_view._DIRENT.size > len(blob):
            break
        ino, cookie, length, kind = fuse_view._DIRENT.unpack_from(blob, cursor)
        name = blob[
            cursor + fuse_view._DIRENT.size:
            cursor + fuse_view._DIRENT.size + length].decode(
                "utf-8", "surrogateescape")
        result.append(
            {"name": name, "ino": ino, "cookie": cookie, "kind": kind})
        cursor += (fuse_view._DIRENT.size + length + 7) // 8 * 8
    return result


def attr_from_payload(payload, *, entry=False):
    offset = fuse_view._ENTRY.size if entry else fuse_view._ATTR_OUT.size
    values = fuse_view._ATTR.unpack_from(payload, offset)
    keys = (
        "ino", "size", "blocks", "atime", "mtime", "ctime", "atimensec",
        "mtimensec", "ctimensec", "mode", "nlink", "uid", "gid", "rdev",
        "blksize", "padding")
    return dict(zip(keys, values))


def open_fd_count():
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def errno_of(reply):
    return None if reply is None else reply[2]
