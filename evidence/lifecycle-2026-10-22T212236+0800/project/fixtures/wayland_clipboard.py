#!/usr/bin/env python3
"""Small ext-data-control client for deterministic clipboard acceptance.

Uses the compositor's advertised protocol, preserving arbitrary MIME payloads
in memory. It is also used to restore the user's clipboard without persisting
its contents in experiment evidence. No X11 fallback is implemented.
"""

import argparse
import array
import base64
from collections import deque
import json
import os
from pathlib import Path
import select
import socket
import struct
import sys
import threading
import time


def uint(value):
    return struct.pack("=I", value)


def string(value):
    data = value.encode() + b"\0"
    return uint(len(data)) + data + bytes((-len(data)) % 4)


def read_string(payload, offset=0):
    size = struct.unpack_from("=I", payload, offset)[0]
    return payload[offset + 4:offset + 4 + size - 1].decode(), offset + 4 + ((size + 3) & ~3)


class Clipboard:
    def __init__(self):
        display = os.environ.get("WAYLAND_DISPLAY", "wayland-0")
        path = display if display.startswith("/") else str(Path(os.environ["XDG_RUNTIME_DIR"]) / display)
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(path)
        self.buffer = b""
        self.fds = deque()
        self.next_id = 2
        self.kinds = {1: "display"}
        self.globals = {}
        self.offers = {}
        self.selection = None
        self.payloads = {}
        self.cancelled = False
        self.synced = set()
        self.registry = self.new("registry")
        self.send(1, 1, uint(self.registry))
        self.roundtrip()
        if "ext_data_control_manager_v1" not in self.globals:
            raise RuntimeError("compositor does not advertise ext_data_control_manager_v1")
        self.manager = self.bind("ext_data_control_manager_v1", "manager")
        self.seat = self.bind("wl_seat", "seat")
        self.device = self.new("device")
        self.send(self.manager, 1, uint(self.device) + uint(self.seat))
        self.roundtrip()

    def new(self, kind):
        value = self.next_id
        self.next_id += 1
        self.kinds[value] = kind
        return value

    def send(self, object_id, opcode, payload=b"", fd=None):
        data = uint(object_id) + uint(((len(payload) + 8) << 16) | opcode) + payload
        ancillary = [] if fd is None else [(socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", [fd]))]
        sent = self.sock.sendmsg([data], ancillary)
        if sent < len(data):
            self.sock.sendall(data[sent:])

    def bind(self, interface, kind):
        name, _ = self.globals[interface]
        value = self.new(kind)
        self.send(self.registry, 0, uint(name) + string(interface) + uint(1) + uint(value))
        return value

    def dispatch(self, timeout=5):
        if not select.select([self.sock], [], [], timeout)[0]:
            return False
        data, ancillary, _, _ = self.sock.recvmsg(65536, socket.CMSG_SPACE(64 * 4))
        if not data:
            raise RuntimeError("Wayland compositor closed the connection")
        for level, kind, raw in ancillary:
            if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                fds = array.array("i")
                fds.frombytes(raw[:len(raw) - len(raw) % fds.itemsize])
                self.fds.extend(fds)
        self.buffer += data
        while len(self.buffer) >= 8:
            object_id, word = struct.unpack_from("=II", self.buffer)
            size, opcode = word >> 16, word & 65535
            if size < 8:
                raise RuntimeError("invalid Wayland event length")
            if len(self.buffer) < size:
                break
            payload = self.buffer[8:size]
            self.buffer = self.buffer[size:]
            kind = self.kinds.get(object_id)
            if kind == "registry" and opcode == 0:
                name = struct.unpack_from("=I", payload)[0]
                interface, offset = read_string(payload, 4)
                self.globals[interface] = (name, struct.unpack_from("=I", payload, offset)[0])
            elif kind == "display" and opcode == 0:
                message, _ = read_string(payload, 8)
                raise RuntimeError("Wayland protocol error: " + message)
            elif kind == "callback":
                self.synced.add(object_id)
            elif kind == "device" and opcode == 0:
                offer = struct.unpack_from("=I", payload)[0]
                self.kinds[offer] = "offer"
                self.offers[offer] = []
            elif kind == "device" and opcode == 1:
                self.selection = struct.unpack_from("=I", payload)[0] or None
            elif kind == "device" and opcode == 2:
                raise RuntimeError("compositor finished the data-control device")
            elif kind == "offer" and opcode == 0:
                mime, _ = read_string(payload)
                self.offers[object_id].append(mime)
            elif kind == "source" and opcode == 0:
                mime, _ = read_string(payload)
                fd = self.fds.popleft()
                body = self.payloads.get(mime, b"")
                def write_data(destination=fd, content=body):
                    try:
                        with os.fdopen(destination, "wb") as stream:
                            stream.write(content)
                    except BrokenPipeError:
                        pass
                threading.Thread(target=write_data, daemon=True).start()
            elif kind == "source" and opcode == 1:
                self.cancelled = True
        return True

    def roundtrip(self):
        callback = self.new("callback")
        self.send(1, 0, uint(callback))
        deadline = time.monotonic() + 5
        while callback not in self.synced:
            if time.monotonic() > deadline:
                raise TimeoutError("Wayland roundtrip timed out")
            self.dispatch(0.5)

    def types(self):
        self.roundtrip()
        return list(self.offers.get(self.selection, []))

    def read(self, mime):
        if mime not in self.types():
            raise RuntimeError("clipboard does not offer requested MIME type")
        read_fd, write_fd = os.pipe()
        try:
            self.send(self.selection, 0, string(mime), write_fd)
        finally:
            os.close(write_fd)
        chunks = []
        deadline = time.monotonic() + 8
        try:
            while time.monotonic() < deadline:
                ready, _, _ = select.select([read_fd, self.sock], [], [], 0.5)
                if self.sock in ready:
                    self.dispatch(0)
                if read_fd in ready:
                    part = os.read(read_fd, 65536)
                    if not part:
                        return b"".join(chunks)
                    chunks.append(part)
                    if sum(map(len, chunks)) > 64 * 1024 * 1024:
                        raise RuntimeError("clipboard payload exceeds bounded capture size")
            raise TimeoutError("clipboard source did not finish the transfer")
        finally:
            os.close(read_fd)

    def own(self, payloads):
        self.payloads = payloads
        if not payloads:
            self.send(self.device, 0, uint(0))
        else:
            source = self.new("source")
            self.send(self.manager, 0, uint(source))
            for mime in payloads:
                self.send(source, 0, string(mime))
            self.send(self.device, 0, uint(source))
        self.roundtrip()

    def close(self):
        self.sock.close()
        while self.fds:
            os.close(self.fds.popleft())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("types", "paste", "copy", "restore"))
    parser.add_argument("--mime", default="text/plain;charset=utf-8")
    parser.add_argument("--file", type=Path)
    args = parser.parse_args()
    clipboard = Clipboard()
    if args.operation == "types":
        print(json.dumps(clipboard.types()))
    elif args.operation == "paste":
        sys.stdout.buffer.write(clipboard.read(args.mime))
    else:
        if args.operation == "restore":
            data = json.load(sys.stdin)
            payloads = {mime: base64.b64decode(body) for mime, body in data.items()}
        else:
            body = args.file.read_bytes() if args.file else sys.stdin.buffer.read()
            payloads = {"text/plain;charset=utf-8": body, "text/plain": body}
        clipboard.own(payloads)
        print("clipboard-owner-ready", flush=True)
        while payloads and not clipboard.cancelled:
            clipboard.dispatch(2)
    clipboard.close()


if __name__ == "__main__":
    main()
