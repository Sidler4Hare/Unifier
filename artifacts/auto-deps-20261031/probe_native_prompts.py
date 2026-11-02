"""Run one real package CLI in a PTY; answer only observed native prompts."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import termios
import time

parser = argparse.ArgumentParser()
parser.add_argument("--output", required=True)
parser.add_argument("--answer", default="n")
parser.add_argument("command", nargs=argparse.REMAINDER)
args = parser.parse_args()
command = args.command[1:] if args.command[:1] == ["--"] else args.command
output = Path(args.output)
master, slave = pty.openpty()
fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave, start_new_session=True)
os.close(slave)
data = bytearray()
events = []
deadline = time.monotonic() + 900
prompt_seen = 0
try:
    while time.monotonic() < deadline:
        if select.select([master], [], [], 0.2)[0]:
            try:
                chunk = os.read(master, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            data.extend(chunk)
        tail = bytes(data[prompt_seen:])
        prompt = next((p for p in [b"[Y/n]", b"[y/N]", b"NATIVE_FIXTURE_CHOICE:"] if p in tail), None)
        if prompt:
            prompt_seen = len(data)
            fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 38, 120, 0, 0))
            process.send_signal(signal.SIGWINCH)
            time.sleep(0.1)
            answer = b"\x03" if args.answer == "CTRL_C" else (args.answer + "\n").encode()
            os.write(master, answer)
            events.append({"prompt": prompt.decode(), "answer": args.answer, "resize": [38, 120]})
    code = process.wait(timeout=15)
finally:
    if process.poll() is None:
        process.terminate()
        process.wait(timeout=15)
    os.close(master)
    output.with_suffix(".terminal.log").write_bytes(data)
result = {"command": command, "exit": code, "events": events,
          "native_prompt_observed": bool(events), "bytes": len(data)}
output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n")
print(json.dumps(result, ensure_ascii=False))
