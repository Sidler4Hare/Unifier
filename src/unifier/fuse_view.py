"""Object-anchored read-only FUSE3 server for a stopped environment view.

This module speaks the Linux /dev/fuse protocol directly through the
fusermount3 helper; no Python FUSE package is installed or imported. It mounts
with ro,nosuid,nodev,noexec and never allow_other. ``default_permissions`` is
deliberately *not* enabled: guest UID/GID values are presentation metadata and
the host worker's credentials decide real readability. All mutation opcodes
return EROFS.

The projection is live, not a snapshot. Attributes and entries are returned
with TTL 0, cached page I/O remains enabled for mmap, and FUSE_AUTO_INVAL_DATA
is negotiated when the kernel offers it. Regular file pages are invalidated
on every attribute revalidation after the initial lookup, even when timestamps
collide. This trades cache reuse for correctness on coarse timestamp backends.
There is no watcher or cross-volume consistency guarantee.
The historical ``unifier.fuse_view_legacy`` entry runs this worker.
"""

import argparse
import contextlib
import errno
import json
import os
from pathlib import Path
import resource
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from unifier.process_identity import process_stamp
from unifier.view_backend import IdMap, ViewBackend, ViewError
from unifier.view_journal import write_journal


FUSE_ROOT_ID = 1
FUSE_LOOKUP = 1
FUSE_FORGET = 2
FUSE_GETATTR = 3
FUSE_SETATTR = 4
FUSE_READLINK = 5
FUSE_SYMLINK = 6
FUSE_MKNOD = 8
FUSE_MKDIR = 9
FUSE_UNLINK = 10
FUSE_RMDIR = 11
FUSE_RENAME = 12
FUSE_LINK = 13
FUSE_OPEN = 14
FUSE_READ = 15
FUSE_WRITE = 16
FUSE_STATFS = 17
FUSE_RELEASE = 18
FUSE_FSYNC = 20
FUSE_SETXATTR = 21
FUSE_GETXATTR = 22
FUSE_LISTXATTR = 23
FUSE_REMOVEXATTR = 24
FUSE_FLUSH = 25
FUSE_INIT = 26
FUSE_OPENDIR = 27
FUSE_READDIR = 28
FUSE_RELEASEDIR = 29
FUSE_FSYNCDIR = 30
FUSE_ACCESS = 34
FUSE_CREATE = 35
FUSE_INTERRUPT = 36
FUSE_DESTROY = 38
FUSE_BATCH_FORGET = 42
FUSE_FALLOCATE = 43
FUSE_READDIRPLUS = 44
FUSE_RENAME2 = 45
FUSE_COPY_FILE_RANGE = 47
FUSE_SYNCFS = 50
FUSE_TMPFILE = 51

FUSE_AUTO_INVAL_DATA = 1 << 12
FUSE_GETATTR_FH = 1 << 0
FUSE_NOTIFY_INVAL_INODE = 2

_IN_HEADER = struct.Struct("<IIQQIIII")
_OUT_HEADER = struct.Struct("<IiQ")
_ATTR = struct.Struct("<QQQQQQIIIIIIIIII")
_ENTRY = struct.Struct("<QQQQII")
_ATTR_OUT = struct.Struct("<QII")
_OPEN_OUT = struct.Struct("<QII")
_READ_IN = struct.Struct("<QQIIQII")
_DIRENT = struct.Struct("<QQII")
_INIT_OUT = struct.Struct("<IIIIHHIIHHIIH" + "H" * 11)
_STATFS = struct.Struct("<QQQQQIIII" + "I" * 6)
_NOTIFY_INVAL_INODE = struct.Struct("<Qqq")

_EROFS_OPS = {
    FUSE_SETATTR, FUSE_SYMLINK, FUSE_MKNOD, FUSE_MKDIR, FUSE_UNLINK,
    FUSE_RMDIR, FUSE_RENAME, FUSE_LINK, FUSE_WRITE, FUSE_SETXATTR,
    FUSE_REMOVEXATTR, FUSE_CREATE, FUSE_FALLOCATE, FUSE_RENAME2,
    FUSE_COPY_FILE_RANGE, FUSE_SYNCFS, FUSE_TMPFILE,
}
_NO_REPLY = {FUSE_FORGET, FUSE_INTERRUPT, FUSE_BATCH_FORGET}

MOUNT_OPTIONS = "ro,nosuid,nodev,noexec"


class FuseServer:
    def __init__(self, fuse_fd: int, backend: ViewBackend, *, mountpoint: str,
                 control_socket: str, view_uuid: str, ready_file: str = "",
                 nonce: str = ""):
        self.fuse_fd = fuse_fd
        self.backend = backend
        self.mountpoint = mountpoint
        self.control_socket = control_socket
        self.view_uuid = view_uuid
        self.ready_file = ready_file
        self.nonce = nonce
        self.stop = threading.Event()
        self.initialized = threading.Event()
        self.init_major = 0
        self.init_minor = 0
        self.negotiated_flags = 0
        self.kernel_negotiated_flags = 0
        self._control_server = None
        self._ready_written = False
        self._write_lock = threading.Lock()
        self.inval_inode_notifications = 0
        self.notification_failures = 0
        self.notification_absent_inodes = 0

    # ----- protocol I/O ---------------------------------------------------
    def _write_message(self, blob: bytes) -> None:
        # Serialize complete FUSE messages: a notification must never be
        # interleaved with a reply or split across another writer's bytes.
        with self._write_lock:
            while blob:
                try:
                    written = os.write(self.fuse_fd, blob)
                except InterruptedError:
                    continue
                if written <= 0:
                    raise OSError(errno.EIO, "short write to the FUSE device")
                blob = blob[written:]

    def _send(self, unique: int, payload: bytes = b"", error: int = 0) -> None:
        header = _OUT_HEADER.pack(_OUT_HEADER.size + len(payload), -abs(error), unique)
        self._write_message(header + payload)

    def _notify(self, code: int, payload: bytes) -> bool:
        """Send one userspace notification (unique=0, positive notify code)."""
        header = _OUT_HEADER.pack(_OUT_HEADER.size + len(payload), code, 0)
        try:
            self._write_message(header + payload)
            return True
        except OSError as exc:
            if exc.errno == errno.ENOENT:
                # LOOKUP may reuse a live userspace node after the kernel has
                # evicted its inode but before the pending FORGET is handled.
                # fuse_reverse_inval_inode returns ENOENT only when no kernel
                # inode exists, so there are no old cached pages to invalidate.
                self.notification_absent_inodes += 1
                return True
            self.notification_failures += 1
            # Required page invalidation failed. Do not acknowledge fresh
            # attributes while knowingly retaining stale cached content.
            raise

    def _attr_signature(self, view: dict) -> tuple:
        return (view["size"], view["mtime"], view["mtimensec"],
                view["ctime"], view["ctimensec"])

    def _revalidate_attr(self, node, view: dict) -> None:
        """Invalidate regular file pages at every real revalidation.

        This is deliberately *only* on revalidation: it is not a watcher and
        cannot deliver inotify events or invalidate an unopened path before the
        kernel asks for attributes. Size/mtime/ctime can all collide on a fast
        same-length write, so they cannot prove that cached bytes are current.
        Do not hash whole files or fabricate timestamps. Read-only files have
        no dirty pages to write back; non-file attributes remain change-based.
        """
        signature = self._attr_signature(view)
        previous = node.attr_signature
        if previous is None:
            node.attr_signature = signature
            return
        if previous == signature and not stat.S_ISREG(view["mode"]):
            return
        if node.anchor_fd is None:
            node.attr_signature = signature
            return
        payload = _NOTIFY_INVAL_INODE.pack(node.nodeid, 0, 0)
        if not self._notify(FUSE_NOTIFY_INVAL_INODE, payload):
            raise ViewError(errno.EIO, "required page invalidation failed")
        self.inval_inode_notifications += 1
        # Version ownership follows Node lifetime, including child collection
        # when a directory snapshot releases its last internal references.
        node.attr_signature = signature

    def _attr_pack(self, view: dict) -> bytes:
        return _ATTR.pack(
            view["ino"], view["size"], view["blocks"], view["atime"], view["mtime"],
            view["ctime"], view["atimensec"], view["mtimensec"], view["ctimensec"],
            view["mode"], view["nlink"], view["uid"], view["gid"], view["rdev"],
            view["blksize"], 0)

    def _attr_payload(self, node, fh: int = 0) -> bytes:
        view = self.backend.node_attr(node, fh)
        self._revalidate_attr(node, view)
        return _ATTR_OUT.pack(0, 0, 0) + self._attr_pack(view)

    def _entry_pack(self, node) -> bytes:
        # A new lookup always returns a fresh (TTL 0) entry. The node id is
        # view-local and never reused; generation stays 1 for this worker.
        view = self.backend.node_attr(node)
        self._revalidate_attr(node, view)
        return _ENTRY.pack(node.nodeid, 1, 0, 0, 0, 0) + self._attr_pack(view)

    def _do_forget(self, nodeid: int, body: bytes) -> None:
        if len(body) >= 8:
            count = struct.unpack_from("<Q", body, 0)[0]
        else:
            count = 0
        # FORGET has no reply. The nodeid is in the request header; the body
        # only carries the lookup count.
        self.backend.forget_node(nodeid, count)

    def _do_batch_forget(self, body: bytes) -> None:
        if len(body) < 8:
            return
        count, _dummy = struct.unpack_from("<II", body, 0)
        pairs = []
        offset = 8
        for _ in range(min(count, (len(body) - offset) // 16)):
            nodeid, lookups = struct.unpack_from("<QQ", body, offset)
            pairs.append((nodeid, lookups))
            offset += 16
        self.backend.batch_forget(pairs)

    def _handle(self, opcode: int, nodeid: int, unique: int, body: bytes):
        if opcode == FUSE_FORGET:
            self._do_forget(nodeid, body)
            return
        if opcode == FUSE_BATCH_FORGET:
            self._do_batch_forget(body)
            return
        if opcode in _NO_REPLY:
            return
        if opcode in _EROFS_OPS:
            self._send(unique, error=errno.EROFS)
            return
        if opcode == FUSE_INIT:
            major, minor = struct.unpack_from("<II", body, 0)
            if major != 7:
                self._send(unique, error=errno.EPROTO)
                return
            max_readahead = (
                struct.unpack_from("<I", body, 8)[0] if len(body) >= 12 else 0)
            offered = (
                struct.unpack_from("<I", body, 12)[0] if len(body) >= 16 else 0)
            flags2 = (
                struct.unpack_from("<I", body, 16)[0] if len(body) >= 20 else 0)
            self.kernel_negotiated_flags = offered | (flags2 << 32)
            self.negotiated_flags = offered & FUSE_AUTO_INVAL_DATA
            reply_minor = min(minor, 31)
            reply = _INIT_OUT.pack(
                7, reply_minor, min(max_readahead, 128 * 1024),
                self.negotiated_flags, 16, 12, 128 * 1024, 1, 0, 0, 0, 0, 0,
                *([0] * 11))
            self._send(unique, reply)
            self.init_major = 7
            self.init_minor = reply_minor
            self.initialized.set()
            self._write_ready(7, reply_minor)
            return
        if opcode == FUSE_DESTROY:
            self._send(unique)
            self.stop.set()
            return
        if opcode == FUSE_LOOKUP:
            name = body.split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
            node = self.backend.lookup_node(nodeid, name)
            try:
                payload = self._entry_pack(node)
            except BaseException:
                # An entry that never reached the kernel owns no lookup ref.
                self.backend.forget_node(node.nodeid, 1)
                raise
            self._send(unique, payload)
            return
        if opcode == FUSE_GETATTR:
            fh = 0
            if len(body) >= 16:
                getattr_flags, _dummy, candidate_fh = struct.unpack_from(
                    "<IIQ", body, 0)
                if getattr_flags & FUSE_GETATTR_FH:
                    fh = candidate_fh
            node = self.backend.get_node(nodeid)
            self._send(unique, self._attr_payload(node, fh))
            return
        if opcode == FUSE_READLINK:
            node = self.backend.get_node(nodeid)
            raw = self.backend.readlink_node(nodeid)
            if not raw.startswith("/") and self.backend.node_attr(node)["nlink"] > 1:
                # READLINK only carries an inode, not the alias used by the
                # caller. Absolute projection of a relative hardlinked target
                # would silently select the first alias's parent directory.
                raise ViewError(
                    errno.EOPNOTSUPP,
                    "relative hardlinked symlinks need alias-aware projection")
            # node.path is the last directory entry a confirmed lookup
            # recorded. The raw target remains the pinned symlink text.
            projected = self.backend.projected_link(node.path, raw)
            self._send(unique, projected.encode("utf-8", "surrogateescape"))
            return
        if opcode in {FUSE_OPEN, FUSE_OPENDIR}:
            flags = struct.unpack_from("<I", body, 0)[0] if len(body) >= 4 else 0
            if ((flags & os.O_ACCMODE) != os.O_RDONLY
                    or flags & (os.O_TRUNC | os.O_CREAT | os.O_EXCL)):
                self._send(unique, error=errno.EROFS)
                return
            if opcode == FUSE_OPEN:
                fh = self.backend.open_node(nodeid, flags)
            else:
                fh = self.backend.open_dir(nodeid)
            self._send(unique, _OPEN_OUT.pack(fh, 0, 0))
            return
        if opcode == FUSE_READ:
            fh, offset, size = struct.unpack_from("<QQI", body, 0)
            data = self.backend.read_handle(
                fh, offset, min(size, 1024 * 1024))
            self._send(unique, data)
            return
        if opcode == FUSE_READDIR:
            fh, cookie, size = struct.unpack_from("<QQI", body, 0)
            start, entries = self.backend.readdir_handle(fh, cookie)
            blob = bytearray()
            for index, entry in enumerate(entries):
                name = entry["name"].encode("utf-8", "surrogateescape")
                kind = {"dir": 4, "file": 8, "link": 10}.get(
                    entry["kind"], 0)
                record = _DIRENT.pack(
                    entry["ino"], start + index + 1, len(name), kind) + name
                record += b"\x00" * ((8 - len(record) % 8) % 8)
                if len(blob) + len(record) > size:
                    break
                blob.extend(record)
            self._send(unique, bytes(blob))
            return
        if opcode in {FUSE_RELEASE, FUSE_RELEASEDIR}:
            fh = struct.unpack_from("<Q", body, 0)[0]
            self.backend.release_handle(fh)
            self._send(unique)
            return
        if opcode in {FUSE_FLUSH, FUSE_FSYNC, FUSE_FSYNCDIR}:
            self._send(unique)
            return
        if opcode == FUSE_ACCESS:
            mask = struct.unpack_from("<I", body, 0)[0] if len(body) >= 4 else 0
            node = self.backend.get_node(nodeid)
            mode = self.backend.node_attr(node)["mode"]
            if mask & os.W_OK:
                self._send(unique, error=errno.EROFS)
            elif (mask & os.R_OK) and not (mode & 0o444):
                self._send(unique, error=errno.EACCES)
            elif (mask & os.X_OK) and not (mode & 0o111):
                self._send(unique, error=errno.EACCES)
            else:
                self._send(unique)
            return
        if opcode == FUSE_STATFS:
            info = self.backend.statfs()
            payload = _STATFS.pack(
                info["blocks"], info["bfree"], info["bavail"], info["files"],
                info["ffree"], info["bsize"], info["namelen"], info["frsize"], 0,
                *([0] * 6))
            self._send(unique, payload)
            return
        if opcode == FUSE_GETXATTR:
            size = struct.unpack_from("<I", body, 0)[0] if len(body) >= 4 else 0
            if size == 0:
                self._send(unique, struct.pack("<II", 0, 0))
            else:
                self._send(unique, error=errno.ENODATA)
            return
        if opcode == FUSE_LISTXATTR:
            self._send(unique, struct.pack("<II", 0, 0))
            return
        if opcode == FUSE_READDIRPLUS:
            self._send(unique, error=errno.ENOSYS)
            return
        self._send(unique, error=errno.ENOSYS)

    def serve(self) -> None:
        self._start_control()
        import select
        while not self.stop.is_set():
            try:
                ready, _, _ = select.select([self.fuse_fd], [], [], 0.5)
                if not ready:
                    continue
                data = os.read(self.fuse_fd, 1024 * 1024)
            except InterruptedError:
                continue
            except OSError as exc:
                if exc.errno in {errno.ENODEV, errno.EBADF}:
                    break
                raise
            if not data:
                break
            if len(data) < _IN_HEADER.size:
                continue
            length, opcode, unique, nodeid, _uid, _gid, _pid, _pad = _IN_HEADER.unpack_from(data, 0)
            body = data[_IN_HEADER.size:length]
            try:
                self._handle(opcode, nodeid, unique, body)
            except ViewError as exc:
                if opcode not in _NO_REPLY:
                    self._send(unique, error=exc.errno)
            except OSError as exc:
                if opcode not in _NO_REPLY:
                    self._send(unique, error=exc.errno or errno.EIO)
            except Exception:
                if opcode not in _NO_REPLY:
                    self._send(unique, error=errno.EIO)

    def _write_ready(self, init_major: int, init_minor: int) -> None:
        """Publish readiness only after a real FUSE INIT reply was sent."""
        if not self.ready_file or self._ready_written:
            return
        pid = os.getpid()
        stamp = process_stamp(pid)
        if stamp is None:
            self.stop.set()
            return
        try:
            write_journal(self.ready_file, {
                "schema_version": 1,
                "ready": True,
                "pid": pid,
                "stamp": stamp,
                "view_uuid": self.view_uuid,
                "nonce": self.nonce,
                "init_major": init_major,
                "init_minor": init_minor,
                "updated_at": time.time(),
            })
        except OSError as exc:
            print(f"could not write FUSE readiness: {exc}", file=sys.stderr)
            self.stop.set()
            return
        self._ready_written = True

    def _status(self) -> dict:
        consistency = self.backend.consistency()
        counters = self.backend.stats()
        consistency["auto_inval_data"] = bool(
            self.negotiated_flags & FUSE_AUTO_INVAL_DATA)
        consistency["kernel_offered_auto_inval_data"] = bool(
            self.kernel_negotiated_flags & FUSE_AUTO_INVAL_DATA)
        return {
            "ok": True,
            "view_uuid": self.view_uuid,
            "mountpoint": self.mountpoint,
            "worker_pid": os.getpid(),
            "worker_stamp": process_stamp(os.getpid()),
            "initialized": self.initialized.is_set(),
            "readonly": True,
            "allow_other": False,
            "init_major": self.init_major,
            "init_minor": self.init_minor,
            "consistency": consistency,
            "counters": counters,
            "notifications": {
                "inval_inode": self.inval_inode_notifications,
                "failures": self.notification_failures,
                "absent_inodes": self.notification_absent_inodes,
                "scope": "on-revalidation-only",
                "tracked_inodes": counters["attr_versions"],
            },
        }

    def _start_control(self) -> None:
        if not self.control_socket:
            return
        try:
            os.unlink(self.control_socket)
        except FileNotFoundError:
            pass
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(self.control_socket)
        os.chmod(self.control_socket, 0o600)
        server.listen(8)
        self._control_server = server

        def run():
            while not self.stop.is_set():
                try:
                    client, _ = server.accept()
                except OSError as exc:
                    if exc.errno in {errno.EMFILE, errno.ENFILE, errno.ENOMEM,
                                     errno.EINTR}:
                        # An external limit change/system file-table shortage
                        # can still beat admission. Resume accepting when it
                        # clears; never permanently lose the control service.
                        self.stop.wait(0.05)
                        continue
                    break
                try:
                    request = client.recv(65536).split(b"\n", 1)[0]
                    payload = json.loads(request.decode()) if request else {}
                    op = payload.get("op")
                    if op == "readlink_raw":
                        path = str(payload.get("path", ""))
                        raw = self.backend.readlink_raw(path)
                        response = {"ok": True, "raw": raw,
                                    "projected": self.backend.projected_link(path, raw),
                                    "semantics": "projected-link"}
                    elif op == "status":
                        response = self._status()
                    else:
                        response = {"ok": False, "error": "unknown control operation"}
                except (ViewError, OSError, ValueError, json.JSONDecodeError) as exc:
                    response = {"ok": False, "error": str(exc)}
                try:
                    client.sendall((json.dumps(response, ensure_ascii=False) + "\n").encode())
                finally:
                    client.close()
        threading.Thread(target=run, daemon=True).start()

    def close(self) -> None:
        self.stop.set()
        if self._control_server is not None:
            try:
                self._control_server.close()
            except OSError:
                pass
        try:
            os.unlink(self.control_socket)
        except (FileNotFoundError, TypeError):
            pass
        # Closing the worker/connection closes every pinned node, handle and
        # directory stream. run_fuse calls this before its own backend.close(),
        # which is idempotent.
        self.backend.close()


def receive_fusermount_fd(connection: socket.socket) -> int:
    """Parse exactly one SCM_RIGHTS descriptor from the fusermount3 socket.

    recvmsg's second result is a list of ``(level, type, bytes)`` ancillary
    entries, never a list of integer descriptors. Returning a tuple here would
    turn a valid FUSE fd into a non-fd object, so every ambiguous case closes
    what was received and raises.
    """
    _message, ancillary, flags, _address = connection.recvmsg(
        1, socket.CMSG_SPACE(64))
    import array
    descriptors = []
    for level, kind, data in ancillary:
        if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
            values = array.array("i")
            values.frombytes(data[:len(data) - len(data) % values.itemsize])
            descriptors.extend(values.tolist())
    if flags & getattr(socket, "MSG_CTRUNC", 0) or len(descriptors) > 1:
        for descriptor in descriptors:
            with contextlib.suppress(OSError):
                os.close(descriptor)
        raise RuntimeError("fusermount3 returned invalid or truncated SCM_RIGHTS")
    if not descriptors:
        raise RuntimeError("fusermount3 did not provide a FUSE fd")
    return descriptors[0]


def mount_with_fusermount(mountpoint: str, view_uuid: str) -> int:
    parent, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
    environment = dict(os.environ)
    environment["_FUSE_COMMFD"] = str(child.fileno())
    options = f"{MOUNT_OPTIONS},fsname=unifier-view-{view_uuid},subtype=unifier"
    process = None
    try:
        process = subprocess.Popen(
            ["fusermount3", "-o", options, mountpoint],
            env=environment, pass_fds=(child.fileno(),),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE)
        child.close()
        parent.settimeout(15.0)
        try:
            return receive_fusermount_fd(parent)
        except RuntimeError as exc:
            error = process.stderr.read().decode("utf-8", "replace") if process.stderr else ""
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            raise RuntimeError(f"{exc}: {error.strip()}") from exc
    finally:
        try:
            parent.close()
        except OSError:
            pass
        try:
            child.close()
        except OSError:
            pass
        if process is not None:
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=5)
            if process.poll() is None:
                with contextlib.suppress(OSError):
                    process.terminate()
                with contextlib.suppress(subprocess.TimeoutExpired, OSError):
                    process.wait(timeout=2)
            if process.stderr is not None:
                with contextlib.suppress(OSError):
                    process.stderr.close()


def _load_spec(path: str) -> dict:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("view spec must be an object")
    return payload


def raise_nofile_soft_limit():
    """Raise RLIMIT_NOFILE soft limit to the hard limit for the FUSE worker.

    Hosts commonly default to a soft limit of 1024, which a deep projection
    exhausts long before the hard limit. The hard limit is never changed.
    Returns the (soft, hard) pair in effect afterwards.
    """
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = hard
    if hard == resource.RLIM_INFINITY:
        try:
            target = int(Path("/proc/sys/fs/nr_open").read_text().strip())
        except (OSError, ValueError):
            target = 1048576
    if soft != resource.RLIM_INFINITY and soft < target:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
        except (OSError, ValueError):
            pass
    return resource.getrlimit(resource.RLIMIT_NOFILE)


def run_fuse(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="unifier view _fuse")
    parser.add_argument("--mountpoint", required=True)
    parser.add_argument("--merged-fd", type=int, required=True)
    parser.add_argument("--spec", required=True)
    parser.add_argument("--control-socket", default="")
    parser.add_argument("--view-uuid", required=True)
    parser.add_argument("--ready-file", default="")
    parser.add_argument("--nonce", default="")
    args = parser.parse_args(argv)
    raise_nofile_soft_limit()
    spec = _load_spec(args.spec)
    id_data = spec["id_map"]
    id_map = IdMap(**id_data)
    merged_stat = os.fstat(args.merged_fd)
    backend = ViewBackend(
        mountpoint=args.mountpoint, merged_fd=args.merged_fd,
        merged_stat=merged_stat, mounts=spec.get("mounts", []),
        capabilities=spec.get("capabilities", []), id_map=id_map,
        view_uuid=args.view_uuid)
    fuse_fd = mount_with_fusermount(args.mountpoint, args.view_uuid)
    server = FuseServer(fuse_fd, backend, mountpoint=args.mountpoint,
                        control_socket=args.control_socket,
                        view_uuid=args.view_uuid, ready_file=args.ready_file,
                        nonce=args.nonce)

    def stop(_signum, _frame):
        server.stop.set()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        server.serve()
    finally:
        server.close()
        backend.close()
        try:
            os.close(fuse_fd)
        except OSError:
            pass
        # Normal, non-lazy unmount. Failures are reported by the parent's
        # revoke path; never use fusermount -z here.
        subprocess.run(["fusermount3", "-u", args.mountpoint],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(run_fuse(sys.argv[1:]))
