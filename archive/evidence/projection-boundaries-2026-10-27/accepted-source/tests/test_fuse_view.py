"""FUSE protocol policy checks that do not require a real kernel mount."""

import array
import errno
import json
import os
from pathlib import Path
import socket
import struct
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from multilinux import fuse_view
from multilinux.on_demand import process_stamp
from multilinux.views import _control_request
from test_projection_support import (
    CaptureServer, attr_from_payload, backend_for)


def _init_body(offered_flags=0):
    return struct.pack("<IIII", 7, 45, 128 * 1024, offered_flags) + b"\x00" * 48


class FusePolicy(unittest.TestCase):
    def test_mount_options_are_readonly_noexec_and_not_allow_other(self):
        options = set(fuse_view.MOUNT_OPTIONS.split(","))
        self.assertIn("ro", options)
        self.assertIn("noexec", options)
        self.assertIn("nosuid", options)
        self.assertIn("nodev", options)
        self.assertNotIn("allow_other", options)
        # Presentation UID/GID is not host permission authority.
        self.assertNotIn("default_permissions", options)

    def test_mutations_and_write_open_return_erofs_after_real_lookup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("x", encoding="utf-8")
            backend, merged = backend_for(root)
            server = CaptureServer(backend)
            try:
                node = server.lookup("file")
                for opcode in (fuse_view.FUSE_WRITE, fuse_view.FUSE_CREATE,
                               fuse_view.FUSE_UNLINK, fuse_view.FUSE_MKDIR,
                               fuse_view.FUSE_RENAME, fuse_view.FUSE_SETATTR):
                    self.assertEqual(
                        server.call(opcode, node, b"")[2], errno.EROFS)
                reply = server.call(
                    fuse_view.FUSE_OPEN, node,
                    struct.pack("<II", os.O_RDWR, 0))
                self.assertEqual(reply[2], errno.EROFS)
                reply = server.call(
                    fuse_view.FUSE_OPEN, node,
                    struct.pack("<II", os.O_RDONLY, 0))
                self.assertEqual(reply[2], 0)
                fh = struct.unpack_from("<Q", reply[1], 0)[0]
                server.release(fh)
            finally:
                backend.close()
                os.close(merged)

    def test_readlink_callback_returns_projected_absolute_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "etc").mkdir()
            (root / "etc/os-release").write_text("x", encoding="utf-8")
            os.symlink("/etc/os-release", root / "etc/abs")
            backend, merged = backend_for(root)
            server = CaptureServer(backend)
            try:
                node = server.lookup("etc")
                link = server.lookup("abs", node)
                reply = server.call(fuse_view.FUSE_READLINK, link, b"")
                self.assertEqual(reply[2], 0)
                self.assertEqual(
                    reply[1].decode(), str(root / "view/etc/os-release"))
                self.assertFalse(reply[1].decode().startswith("/etc/"))
            finally:
                backend.close()
                os.close(merged)

    def test_init_reply_has_supported_protocol_shape_and_auto_inval(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend, merged = backend_for(root)
            server = CaptureServer(backend)
            try:
                server.call(
                    fuse_view.FUSE_INIT, 0,
                    _init_body(fuse_view.FUSE_AUTO_INVAL_DATA | 1))
                unique, payload, error = server.sent[-1]
                self.assertEqual((unique, error, len(payload)), (1, 0, 64))
                self.assertEqual(
                    struct.unpack_from("<II", payload, 0), (7, 31))
                reply_flags = struct.unpack_from("<I", payload, 12)[0]
                self.assertTrue(
                    reply_flags & fuse_view.FUSE_AUTO_INVAL_DATA)

                server.call(fuse_view.FUSE_INIT, 0, _init_body(0))
                payload = server.sent[-1][1]
                self.assertEqual(
                    struct.unpack_from("<I", payload, 12)[0], 0)
            finally:
                backend.close()
                os.close(merged)

    def test_entry_and_attr_ttl_are_zero_and_open_keeps_cached_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("data", encoding="utf-8")
            backend, merged = backend_for(root)
            server = CaptureServer(backend)
            try:
                lookup = server.call(
                    fuse_view.FUSE_LOOKUP, 1, b"file\x00")
                node, generation, entry_valid, attr_valid, entry_nsec, attr_nsec = \
                    fuse_view._ENTRY.unpack_from(lookup[1], 0)
                self.assertEqual(generation, 1)
                self.assertEqual(
                    (entry_valid, attr_valid, entry_nsec, attr_nsec),
                    (0, 0, 0, 0))
                opened = server.call(
                    fuse_view.FUSE_OPEN, node,
                    struct.pack("<II", os.O_RDONLY, 0))
                fh, open_flags, padding = fuse_view._OPEN_OUT.unpack_from(
                    opened[1], 0)
                self.assertTrue(fh)
                self.assertEqual((open_flags, padding), (0, 0))
                getattr = server.getattr(node)
                attr_valid = fuse_view._ATTR_OUT.unpack_from(
                    getattr[1], 0)[0]
                self.assertEqual(attr_valid, 0)
            finally:
                backend.close()
                os.close(merged)


class RevalidationInvalidation(unittest.TestCase):
    def test_notify_header_uses_unique_zero_and_positive_notify_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            backend, merged = backend_for(root)
            read_fd, write_fd = os.pipe()
            server = fuse_view.FuseServer(
                write_fd, backend, mountpoint=str(root / "view"),
                control_socket="", view_uuid="view-notify")
            try:
                payload = fuse_view._NOTIFY_INVAL_INODE.pack(7, 0, 0)
                self.assertTrue(server._notify(
                    fuse_view.FUSE_NOTIFY_INVAL_INODE, payload))
                blob = os.read(read_fd, 4096)
                length, code, unique = fuse_view._OUT_HEADER.unpack_from(
                    blob, 0)
                self.assertEqual((code, unique),
                                 (fuse_view.FUSE_NOTIFY_INVAL_INODE, 0))
                self.assertEqual(length, len(blob))
                self.assertEqual(
                    fuse_view._NOTIFY_INVAL_INODE.unpack_from(
                        blob, fuse_view._OUT_HEADER.size),
                    (7, 0, 0))
            finally:
                os.close(write_fd)
                os.close(read_fd)
                backend.close()
                os.close(merged)

    def test_ctime_only_change_invalidates_cached_inode_before_attr_reply(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "file"
            path.write_bytes(b"O" * 4096)
            backend, merged = backend_for(root)
            server = CaptureServer(backend)
            try:
                node = server.lookup("file")
                fh = server.open_file(node)
                self.assertEqual(server.read(fh, 0, 4096)[1], b"O" * 4096)

                # First real revalidation establishes the baseline only.
                self.assertEqual(server.getattr(node, fh)[2], 0)
                self.assertEqual(server.notifications, [])

                before = path.stat()
                time.sleep(0.02)
                with path.open("r+b") as stream:
                    stream.write(b"N" * 4096)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.utime(path, ns=(
                    before.st_atime_ns, before.st_mtime_ns))
                after = path.stat()
                self.assertEqual(after.st_size, before.st_size)
                self.assertEqual(after.st_mtime_ns, before.st_mtime_ns)
                self.assertNotEqual(after.st_ctime_ns, before.st_ctime_ns)

                reply = server.getattr(node, fh)
                self.assertEqual(reply[2], 0)
                self.assertEqual(len(server.notifications), 1)
                unique, code, payload = server.notifications[0]
                self.assertEqual((unique, code),
                                 (0, fuse_view.FUSE_NOTIFY_INVAL_INODE))
                self.assertEqual(
                    fuse_view._NOTIFY_INVAL_INODE.unpack(payload),
                    (node, 0, 0))
                self.assertEqual(
                    [event[0] for event in server.events],
                    ["notify", "reply"])

                attr = attr_from_payload(reply[1])
                self.assertEqual(attr["size"], 4096)
                self.assertEqual(
                    attr["mtime"], before.st_mtime_ns // 1_000_000_000)
                self.assertEqual(
                    attr["mtimensec"], before.st_mtime_ns % 1_000_000_000)
                self.assertEqual(
                    (attr["ctime"], attr["ctimensec"]),
                    divmod(after.st_ctime_ns, 1_000_000_000))
                self.assertEqual(server.read(fh, 0, 4096)[1], b"N" * 4096)

                # A second unchanged revalidation must not spam notifications.
                self.assertEqual(server.getattr(node, fh)[2], 0)
                self.assertEqual(server.notifications, [])
                self.assertEqual(server.inval_inode_notifications, 1)
            finally:
                backend.close()
                os.close(merged)


class WorkerReadiness(unittest.TestCase):
    def test_ready_requires_real_init_and_control_reports_consistency(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "file").write_text("x", encoding="utf-8")
            backend, merged = backend_for(root)
            control = str(root / "control.sock")
            ready = root / "ready.json"
            server_socket, peer = socket.socketpair(
                socket.AF_UNIX, socket.SOCK_STREAM)
            server = fuse_view.FuseServer(
                server_socket.fileno(), backend,
                mountpoint=str(root / "view"),
                control_socket=control, view_uuid="view-ready",
                ready_file=str(ready), nonce="nonce-ready")
            thread = threading.Thread(target=server.serve, daemon=True)
            thread.start()
            try:
                # A listening control socket alone is not readiness.
                time.sleep(0.1)
                self.assertFalse(ready.exists())
                status = _control_request(control, {"op": "status"})
                self.assertTrue(status["ok"])
                self.assertFalse(status["initialized"])
                consistency = status["consistency"]
                self.assertTrue(consistency["readonly"])
                self.assertTrue(consistency["live"])
                self.assertFalse(consistency["snapshot"])
                self.assertFalse(consistency["watcher"])
                self.assertFalse(consistency["auto_inval_data"])
                self.assertEqual(consistency["xattr"], "unsupported")
                self.assertEqual(consistency["acl"], "unknown")
                self.assertEqual(consistency["entry_ttl"], 0)
                self.assertEqual(consistency["attr_ttl"], 0)
                self.assertFalse(consistency["direct_io"])
                counters = status["counters"]
                self.assertGreaterEqual(counters["nodes"], 1)
                self.assertEqual(counters["handles"], 0)

                body = _init_body(fuse_view.FUSE_AUTO_INVAL_DATA)
                header = fuse_view._IN_HEADER.pack(
                    fuse_view._IN_HEADER.size + len(body),
                    fuse_view.FUSE_INIT, 1, 0, os.getuid(), os.getgid(),
                    os.getpid(), 0)
                peer.sendall(header + body)
                deadline = time.monotonic() + 5.0
                while not ready.exists() and time.monotonic() < deadline:
                    time.sleep(0.02)
                self.assertTrue(ready.exists())
                marker = json.loads(ready.read_text(encoding="utf-8"))
                self.assertTrue(marker["ready"])
                self.assertEqual(marker["pid"], os.getpid())
                self.assertEqual(marker["stamp"], process_stamp(os.getpid()))
                self.assertEqual(marker["view_uuid"], "view-ready")
                self.assertEqual(marker["nonce"], "nonce-ready")
                self.assertEqual(marker["init_major"], 7)

                def status_reader():
                    responses.append(_control_request(
                        control, {"op": "status"}))
                responses = []
                readers = [
                    threading.Thread(target=status_reader)
                    for _ in range(8)]
                for reader in readers:
                    reader.start()
                for reader in readers:
                    reader.join(5)
                self.assertEqual(len(responses), 8)
                self.assertTrue(all(item["ok"] for item in responses))
                status = _control_request(control, {"op": "status"})
                self.assertTrue(status["initialized"])
                self.assertEqual(status["worker_pid"], os.getpid())
                self.assertEqual(status["view_uuid"], "view-ready")
                self.assertTrue(
                    status["consistency"]["auto_inval_data"])
            finally:
                server.stop.set()
                thread.join(5)
                server.close()
                server_socket.close()
                peer.close()
                backend.close()
                os.close(merged)


class FuseMountFd(unittest.TestCase):
    def test_one_real_socketpair_descriptor_is_returned_as_an_fd(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "payload"
            path.write_text("fuse", encoding="utf-8")
            descriptor = os.open(path, os.O_RDONLY)
            try:
                sender.sendmsg(
                    [b"x"],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array.array("i", [descriptor]))])
                received = fuse_view.receive_fusermount_fd(receiver)
                self.assertIsInstance(received, int)
                self.assertEqual(os.read(received, 10), b"fuse")
                os.close(received)
            finally:
                os.close(descriptor)
                sender.close()
                receiver.close()

    def test_multiple_descriptors_are_refused_not_returned_as_a_tuple(self):
        sender, receiver = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        with tempfile.TemporaryDirectory() as temporary:
            first = os.open(Path(temporary) / "a", os.O_CREAT | os.O_RDWR, 0o600)
            second = os.open(Path(temporary) / "b", os.O_CREAT | os.O_RDWR, 0o600)
            try:
                sender.sendmsg(
                    [b"x"],
                    [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                      array.array("i", [first, second]))])
                with self.assertRaisesRegex(RuntimeError, "SCM_RIGHTS"):
                    fuse_view.receive_fusermount_fd(receiver)
            finally:
                os.close(first)
                os.close(second)
                sender.close()
                receiver.close()


if __name__ == "__main__":
    unittest.main()
