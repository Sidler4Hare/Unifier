"""Speak the smallest Wayland connection from inside an environment.

The compositor must answer. A successful socket connect by itself is not
enough: this asks wl_display for the registry and waits for bytes back.
"""

import os
import socket
import struct
import sys


def main() -> int:
    runtime = os.environ.get("XDG_RUNTIME_DIR", "")
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if not runtime or not display:
        print("wayland environment is incomplete", file=sys.stderr)
        return 1
    path = f"{runtime}/{display}"
    client = socket.socket(socket.AF_UNIX)
    client.settimeout(3)
    try:
        client.connect(path)
        payload = struct.pack("<I", 2)
        header = struct.pack("<IHH", 1, 1, 8 + len(payload))
        client.sendall(header + payload)
        reply = client.recv(4096)
    except OSError as exc:
        print(f"wayland handshake failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    if len(reply) < 8:
        print(f"wayland reply was {len(reply)} bytes", file=sys.stderr)
        return 1
    print(f"wayland-reply-bytes={len(reply)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
