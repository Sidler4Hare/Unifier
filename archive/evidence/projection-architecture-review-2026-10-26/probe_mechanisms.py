"""Isolated mechanism probes: real backend and protocol handlers, no FUSE mount.

Run from anywhere with Python 3. These expose current behavior; they are not
kernel integration acceptance or throughput measurements. Only temporary
fixtures are mutated. JSON goes to stdout.
"""
import collections
import contextlib
import errno
import hashlib
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from multilinux import fuse_view as f
from multilinux.view_backend import IdMap, ViewBackend, ViewError


class Capture(f.FuseServer):
    def __init__(self, backend):
        super().__init__(-1, backend, mountpoint=backend.mountpoint,
                         control_socket="", view_uuid="mechanism-probe")
        self.reply = None

    def _send(self, unique, payload=b"", error=0):
        self.reply = {"payload": payload, "errno": error}

    def call(self, opcode, node, body=b""):
        self.reply = None
        try:
            self._handle(opcode, node, 1, body)
        except (ViewError, OSError) as exc:
            self.reply = {"payload": b"", "errno": exc.errno}
        return self.reply

    def lookup(self, name, parent=1):
        reply = self.call(f.FUSE_LOOKUP, parent, name.encode() + b"\0")
        assert reply["errno"] == 0, reply
        return struct.unpack_from("<Q", reply["payload"])[0]


@contextlib.contextmanager
def fixture(mounts=()):
    with tempfile.TemporaryDirectory(prefix="unifier-projection-review-") as tmp:
        root = Path(tmp)
        fd = os.open(root, os.O_PATH | os.O_DIRECTORY)
        backend = ViewBackend(
            mountpoint=str(root / "view"), merged_fd=fd, merged_stat=os.fstat(fd),
            mounts=list(mounts), capabilities=["/proc"],
            id_map=IdMap(os.getuid(), os.getgid(), 0, 0, 0, 0, 0, 0, "rootless-default"),
            view_uuid="mechanism-probe")
        try:
            yield root, backend, Capture(backend)
        finally:
            backend.close()
            os.close(fd)


def entries(blob):
    result = []
    cursor = 0
    while cursor < len(blob):
        ino, offset, length, kind = f._DIRENT.unpack_from(blob, cursor)
        name = blob[cursor + 24:cursor + 24 + length].decode()
        result.append({"name": name, "cookie": offset, "ino": ino, "kind": kind})
        cursor += (24 + length + 7) // 8 * 8
    return result


def run():
    result = {"scope": "backend and FUSE callback mechanism only; no kernel mount"}
    with fixture() as (root, backend, server):
        path = root / "file"
        path.write_bytes(b"OLD")
        native = os.open(path, os.O_RDONLY)
        try:
            node = server.lookup("file")
            opened = server.call(f.FUSE_OPEN, node, struct.pack("<II", os.O_RDONLY, 0))
            fh = struct.unpack_from("<Q", opened["payload"])[0]
            replacement = root / "replacement"
            replacement.write_bytes(b"NEW-CONTENT")
            os.replace(replacement, path)
            read = server.call(f.FUSE_READ, node, struct.pack("<QQI", fh, 0, 100))
            result["open_replace"] = {"native": os.pread(native, 100, 0).decode(),
                "projected_callback": read["payload"].decode(),
                "node_before": node, "node_after": server.lookup("file"), "fh": fh}
            path.unlink()
            read = server.call(f.FUSE_READ, node, struct.pack("<QQI", fh, 0, 100))
            result["open_unlink"] = {"native": os.pread(native, 100, 0).decode(),
                "projected_callback_errno": read["errno"], "ENOENT": errno.ENOENT}
        finally:
            os.close(native)

    with fixture() as (root, backend, server):
        (root / "a").write_text("same")
        os.link(root / "a", root / "b")
        result["hardlinks"] = {"node_a": server.lookup("a"), "node_b": server.lookup("b"),
            "backing_inode_a": backend.getattr("/a")["ino"],
            "backing_inode_b": backend.getattr("/b")["ino"]}
        for i in range(1000):
            name = "churn-" + str(i)
            (root / name).touch()
            node = server.lookup(name)
            server.call(f.FUSE_FORGET, node, struct.pack("<Q", 1))
            (root / name).unlink()
        result["forget"] = {"unique_deleted_paths": 1000,
            "retained_nodes_after_forget": len(server.nodes), "baseline_nodes": 3}

    with tempfile.TemporaryDirectory(prefix="unifier-mount-source-") as tmp:
        source = Path(tmp)
        (source / "file").write_text("mounted")
        with fixture([{"destination": "/data/child", "source": str(source)}]) as (root, backend, server):
            (root / "data").mkdir()
            names = [entry["name"] for entry in backend.readdir("/data")]
            result["mount_enumeration"] = {"listed": names,
                "direct_child_read": backend.read("/data/child/file", 0, 100).decode()}
            (root / "data/child").write_text("underlying file placeholder")
            listed = next(entry for entry in backend.readdir("/data") if entry["name"] == "child")
            result["mount_entry_type"] = {"listed_kind": listed["kind"],
                "lookup_mode": oct(backend.getattr("/data/child")["mode"])}

    with fixture() as (root, backend, server):
        for name in ("a", "b", "c", "d"):
            (root / name).touch()
        first = server.call(f.FUSE_READDIR, 1, struct.pack("<QQI", 1, 0, 96))
        page1 = entries(first["payload"])
        (root / "a").unlink()
        second = server.call(f.FUSE_READDIR, 1, struct.pack("<QQI", 1, page1[-1]["cookie"], 4096))
        page2 = entries(second["payload"])
        result["directory_cookie"] = {"page1": [x["name"] for x in page1],
            "page2_after_removing_a": [x["name"] for x in page2],
            "b_still_exists": (root / "b").exists()}

    with fixture() as (root, backend, server):
        (root / "large").write_bytes(b"x" * (128 * 1024))
        node = server.lookup("large")
        fh = struct.unpack_from("<Q", server.call(f.FUSE_OPEN, node,
            struct.pack("<II", os.O_RDONLY, 0))["payload"])[0]
        counts = collections.Counter()
        originals = {name: getattr(os, name) for name in ("open", "close", "fstat", "pread")}
        def counted(name):
            def wrapped(*args, **kwargs):
                counts[name] += 1
                return originals[name](*args, **kwargs)
            return wrapped
        with contextlib.ExitStack() as stack:
            for name in originals:
                stack.enter_context(patch.object(os, name, counted(name)))
            for i in range(128):
                server.call(f.FUSE_READ, node, struct.pack("<QQI", fh, 0, 128 * 1024))
        result["repeated_read_calls"] = {"callback_count": 128,
            "note": "Python os calls counted; excludes Path.read_text fdinfo opens; not syscall trace or timing",
            "counts": dict(counts)}
    result["source_sha256"] = {str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in [ROOT / "src/multilinux/fuse_view.py", ROOT / "src/multilinux/view_backend.py",
                     ROOT / "src/multilinux/views.py", ROOT / "src/multilinux/view_broker.py"]}
    return result


if __name__ == "__main__":
    print(json.dumps(run(), indent=2, ensure_ascii=False))
