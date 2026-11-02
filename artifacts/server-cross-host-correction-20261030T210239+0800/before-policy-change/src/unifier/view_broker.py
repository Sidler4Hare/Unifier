"""Storage-namespace broker for a stopped environment view.

Run through `podman unshare`. It mounts exactly one container by ID, records a
durable storage-reference journal, passes the merged O_PATH FD to the parent,
and keeps the Podman mount reference until a signal releases it exactly once.
The broker reports its own host PID/start-stamp and nonce; the outer podman
process PID is not the broker identity.
"""

import argparse
import array
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time

from unifier.view_journal import read_journal, write_journal


def process_stamp(pid):
    try:
        fields = open(f"/proc/{pid}/stat", encoding="utf-8").read().rsplit(")", 1)[1].split()
        return None if fields[0] == "Z" else int(fields[19])
    except (OSError, IndexError, ValueError):
        return None


def _journal(path, state, *, cid, view_uuid, nonce, pid, stamp, **extra):
    write_journal(path, {
        "schema_version": 1,
        "state": state,
        "cid": cid,
        "view_uuid": view_uuid,
        "nonce": nonce,
        "broker_pid": pid,
        "broker_stamp": stamp,
        "updated_at": time.time(),
        **extra,
    })


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="unifier view _broker")
    parser.add_argument("--cid", required=True)
    parser.add_argument("--socket", required=True)
    parser.add_argument("--journal", required=True)
    parser.add_argument("--nonce", required=True)
    parser.add_argument("--view-uuid", required=True)
    args = parser.parse_args(argv)
    stop = threading.Event()

    def on_signal(_signum, _frame):
        stop.set()

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    pid = os.getpid()
    stamp = process_stamp(pid)
    if stamp is None:
        return 1
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    mounted = False
    released = False
    release_started = False

    def send_error(message):
        try:
            client.sendall((json.dumps({"ok": False, "error": message})
                            + "\n").encode())
        except OSError:
            pass

    def release(reason):
        """Release this broker's one mount reference, journaling every step."""
        nonlocal released, release_started
        release_started = True
        _journal(args.journal, "releasing", cid=args.cid,
                 view_uuid=args.view_uuid, nonce=args.nonce, pid=pid,
                 stamp=stamp, reason=reason)
        result = subprocess.run(
            ["podman", "umount", args.cid], capture_output=True, text=True,
            timeout=30, check=False)
        detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
        if result.returncode != 0:
            _journal(args.journal, "release-failed", cid=args.cid,
                     view_uuid=args.view_uuid, nonce=args.nonce, pid=pid,
                     stamp=stamp, error=detail, reason=reason)
            print(detail, file=sys.stderr)
            return False
        released = True
        _journal(args.journal, "released", cid=args.cid,
                 view_uuid=args.view_uuid, nonce=args.nonce, pid=pid,
                 stamp=stamp, reason=reason)
        return True

    try:
        client.connect(args.socket)
        _journal(args.journal, "mounting", cid=args.cid,
                 view_uuid=args.view_uuid, nonce=args.nonce, pid=pid, stamp=stamp)
        result = subprocess.run(
            ["podman", "mount", args.cid], capture_output=True, text=True,
            timeout=30, check=False)
        if result.returncode != 0:
            detail = ((result.stderr or "") + (result.stdout or ""))[-2000:]
            _journal(args.journal, "mount-failed", cid=args.cid,
                     view_uuid=args.view_uuid, nonce=args.nonce, pid=pid,
                     stamp=stamp, error=detail)
            send_error(detail)
            return 1
        # From here on one Podman mount reference is owned by this process.
        mounted = True
        lines = [line.strip() for line in (result.stdout or "").splitlines()
                 if line.strip()]
        if not lines:
            send_error("podman mount returned no path")
            return 1
        merged = lines[-1]
        try:
            descriptor = os.open(
                merged, os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as exc:
            send_error(f"could not open merged root: {exc}")
            return 1
        try:
            observed = os.fstat(descriptor)
            mnt_id = _mnt_id(descriptor)
            if mnt_id is None:
                send_error("merged root has no mnt_id")
                return 1
            _journal(args.journal, "mounted", cid=args.cid,
                     view_uuid=args.view_uuid, nonce=args.nonce, pid=pid,
                     stamp=stamp, merged_path=merged, dev=observed.st_dev,
                     ino=observed.st_ino, mnt_id=mnt_id)

            def map_lines(path):
                try:
                    return [line.strip() for line in open(path, encoding="utf-8")
                            if line.strip()]
                except OSError:
                    return []
            metadata = {
                "ok": True, "merged_path": merged, "dev": observed.st_dev,
                "ino": observed.st_ino, "mnt_id": mnt_id,
                "broker_pid": pid, "broker_stamp": stamp, "nonce": args.nonce,
                "storage_uid_map": map_lines("/proc/self/uid_map"),
                "storage_gid_map": map_lines("/proc/self/gid_map"),
            }
            client.sendmsg(
                [(json.dumps(metadata).encode() + b"\n")],
                [(socket.SOL_SOCKET, socket.SCM_RIGHTS,
                  array.array("i", [descriptor]))])
        finally:
            os.close(descriptor)
        client.close()
        client = None
        while not stop.wait(0.5):
            pass
        return 0 if release("broker-stop") else 1
    finally:
        if client is not None:
            try:
                client.close()
            except OSError:
                pass
        # A normal exception after mount but before/after the handoff must not
        # leave an untracked reference. SIGKILL bypasses this and leaves the
        # journal for recovery. A failed/ambiguous release is never retried.
        if mounted and not released and not release_started:
            try:
                release("broker-exception")
            except Exception:
                pass


def _mnt_id(descriptor: int):
    try:
        text = open(f"/proc/self/fdinfo/{descriptor}", encoding="utf-8").read()
    except OSError:
        return None
    for line in text.splitlines():
        if line.startswith("mnt_id:"):
            value = line.split(":", 1)[1].strip()
            if value.isdigit():
                return int(value)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
