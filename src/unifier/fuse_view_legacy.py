"""Minimal read-only FUSE3 file server for a stopped environment view.

This module speaks the Linux /dev/fuse protocol directly through the
fusermount3 helper; no Python FUSE package is installed or imported. It mounts
with ro,nosuid,nodev,noexec,default_permissions and never allow_other. All
mutation opcodes return EROFS. Path resolution is delegated to ViewBackend,
which uses anchored FDs and O_NOFOLLOW.
"""

import argparse
import contextlib
import errno
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import threading
import time

from unifier.on_demand import process_stamp
from unifier.view_backend_legacy import IdMap, ViewBackend, ViewError
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
FUSE_FALLOCATE = 43
FUSE_READDIRPLUS = 44
FUSE_RENAME2 = 45
FUSE_COPY_FILE_RANGE = 47
FUSE_SYNCFS = 50
FUSE_TMPFILE = 51

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

_EROFS_OPS = {
    FUSE_SETATTR, FUSE_SYMLINK, FUSE_MKNOD, FUSE_MKDIR, FUSE_UNLINK,
    FUSE_RMDIR, FUSE_RENAME, FUSE_LINK, FUSE_WRITE, FUSE_SETXATTR,
    FUSE_REMOVEXATTR, FUSE_CREATE, FUSE_FALLOCATE, FUSE_RENAME2,
    FUSE_COPY_FILE_RANGE, FUSE_SYNCFS, FUSE_TMPFILE,
}
_NO_REPLY = {FUSE_FORGET, FUSE_INTERRUPT, 42}  # BATCH_FORGET

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
        self.nodes = {"/": FUSE_ROOT_ID}
        self.paths = {FUSE_ROOT_ID: "/"}
        self.next_node = 2
        self._control_server = None
        self._ready_written = False

    # ----- protocol I/O ---------------------------------------------------
    def _node_for(self, path: str) -> int:
        path = _normalize(path)
        node = self.nodes.get(path)
        if node is None:
            node = self.next_node
            self.next_node += 1
            self.nodes[path] = node
            self.paths[node] = path
        return node

    def _path_for(self, nodeid: int) -> str:
        try:
            return self.paths[nodeid]
        except KeyError as exc:
            raise ViewError(errno.ENOENT, "unknown FUSE node id") from exc

    def _send(self, unique: int, payload: bytes = b"", error: int = 0) -> None:
        header = _OUT_HEADER.pack(_OUT_HEADER.size + len(payload), -abs(error), unique)
        blob = header + payload
        while blob:
            written = os.write(self.fuse_fd, blob)
            blob = blob[written:]

    def _attr_pack(self, view: dict) -> bytes:
        return _ATTR.pack(
            view["ino"], view["size"], view["blocks"], view["atime"], view["mtime"],
            view["ctime"], view["atimensec"], view["mtimensec"], view["ctimensec"],
            view["mode"], view["nlink"], view["uid"], view["gid"], view["rdev"],
            view["blksize"], 0)

    def _entry_pack(self, path: str) -> bytes:
        view = self.backend.getattr(path)
        node = self._node_for(path)
        return _ENTRY.pack(node, 1, 1, 1, 0, 0) + self._attr_pack(view)

    def _handle(self, opcode: int, nodeid: int, unique: int, body: bytes):
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
            reply = _INIT_OUT.pack(
                7, min(minor, 31), 0, 0, 16, 12, 128 * 1024, 1, 0, 0, 0, 0, 0,
                *([0] * 11))
            self._send(unique, reply)
            self.initialized.set()
            self._write_ready(major, min(minor, 31))
            return
        if opcode == FUSE_DESTROY:
            self._send(unique)
            self.stop.set()
            return
        if opcode == FUSE_LOOKUP:
            parent = self._path_for(nodeid)
            name = body.split(b"\x00", 1)[0].decode("utf-8", "surrogateescape")
            if name in {"", ".", ".."}:
                raise ViewError(errno.EINVAL, "invalid lookup name")
            target = _join(parent, name)
            self._send(unique, self._entry_pack(target))
            return
        if opcode == FUSE_GETATTR:
            path = self._path_for(nodeid)
            payload = _ATTR_OUT.pack(1, 0, 0) + self._attr_pack(self.backend.getattr(path))
            self._send(unique, payload)
            return
        if opcode == FUSE_READLINK:
            path = self._path_for(nodeid)
            raw = self.backend.readlink_raw(path)
            projected = self.backend.projected_link(path, raw)
            self._send(unique, projected.encode("utf-8", "surrogateescape"))
            return
        if opcode in {FUSE_OPEN, FUSE_OPENDIR}:
            flags = struct.unpack_from("<I", body, 0)[0] if len(body) >= 4 else 0
            if (flags & os.O_ACCMODE) != os.O_RDONLY:
                self._send(unique, error=errno.EROFS)
                return
            path = self._path_for(nodeid)
            view = self.backend.getattr(path)
            import stat as stat_module
            if opcode == FUSE_OPENDIR and not stat_module.S_ISDIR(view["mode"]):
                self._send(unique, error=errno.ENOTDIR)
                return
            if opcode == FUSE_OPEN and not stat_module.S_ISREG(view["mode"]):
                self._send(unique, error=errno.EISDIR)
                return
            self._send(unique, _OPEN_OUT.pack(nodeid, 0, 0))
            return
        if opcode == FUSE_READ:
            _fh, offset, size = struct.unpack_from("<QQI", body, 0)
            path = self._path_for(nodeid)
            data = self.backend.read(path, offset, min(size, 1024 * 1024))
            self._send(unique, data)
            return
        if opcode == FUSE_READDIR:
            _fh, offset, size = struct.unpack_from("<QQI", body, 0)
            path = self._path_for(nodeid)
            entries = self.backend.readdir(path)
            blob = bytearray()
            for index, entry in enumerate(entries):
                if index < offset:
                    continue
                name = entry["name"].encode("utf-8", "surrogateescape")
                if entry["name"] in {".", ".."}:
                    ino = self.backend.getattr(path)["ino"]
                    kind = 4
                else:
                    ino = entry["stat"]["ino"]
                    kind = {"dir": 4, "file": 8, "link": 10}.get(entry["kind"], 0)
                record = _DIRENT.pack(ino, index + 1, len(name), kind) + name
                record += b"\x00" * ((8 - len(record) % 8) % 8)
                if len(blob) + len(record) > size:
                    break
                blob.extend(record)
            self._send(unique, bytes(blob))
            return
        if opcode in {FUSE_RELEASE, FUSE_RELEASEDIR, FUSE_FLUSH, FUSE_FSYNC,
                      FUSE_FSYNCDIR, FUSE_ACCESS}:
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
        while not self.stop.is_set():
            try:
                import select
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
                except OSError:
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
                        response = {"ok": True, "view_uuid": self.view_uuid,
                                    "mountpoint": self.mountpoint,
                                    "worker_pid": os.getpid(),
                                    "worker_stamp": process_stamp(os.getpid()),
                                    "initialized": self.initialized.is_set(),
                                    "readonly": True, "allow_other": False}
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


def _normalize(path: str) -> str:
    import posixpath
    if not isinstance(path, str) or not path.startswith("/"):
        raise ViewError(errno.EINVAL, "FUSE path must be absolute")
    value = posixpath.normpath(path)
    if value != "/" and not value.startswith("/"):
        raise ViewError(errno.EINVAL, "FUSE path escaped root")
    return value


def _join(parent: str, name: str) -> str:
    import posixpath
    return posixpath.normpath(posixpath.join(parent, name))


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
