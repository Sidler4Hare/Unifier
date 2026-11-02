"""Exercise real PTYs: prompts, answer bytes, resize, interruption and restore."""
import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest

from unifier.native_io import NativeCommandError, run_native
from unifier.runtime import GUEST_PACKAGE_CANCEL


ROOT = Path(__file__).resolve().parents[1]


class NativeIO(unittest.TestCase):
    def test_timeout_cleans_descendants_after_group_leader_exits(self):
        for leader_exits, ignore_term in ((True, False), (True, True), (False, True)):
            with self.subTest(leader_exits=leader_exits, ignore_term=ignore_term), \
                    tempfile.TemporaryDirectory() as temporary:
                marker = Path(temporary) / "child"
                child = """
import os, signal, time
from pathlib import Path
if %r:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(%r).write_text(str(os.getpid()))
print('descendant ready', flush=True)
time.sleep(30)
""" % (ignore_term, str(marker))
                parent = """
import subprocess, sys, time
from pathlib import Path
subprocess.Popen([sys.executable, '-c', %r])
while not Path(%r).exists():
    time.sleep(.01)
if not %r:
    time.sleep(30)
""" % (child, str(marker), leader_exits)
                def spawn(*args, **kwargs):
                    process = subprocess.Popen(*args, **kwargs)
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    if not marker.exists():
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait(timeout=2)
                        self.fail("descendant did not become ready")
                    return process

                try:
                    with self.assertRaises(NativeCommandError) as caught:
                        run_native([sys.executable, "-c", parent], interactive=False,
                                   stream=False, timeout=.2, spawn=spawn)
                    self.assertTrue(marker.exists())
                    self.assertEqual(caught.exception.cleanup, "gone")
                    self.assertIn("descendant ready", caught.exception.stdout)
                    stat = Path(f"/proc/{int(marker.read_text())}/stat")
                    self.assertTrue(not stat.exists() or
                                    stat.read_text().rsplit(") ", 1)[1].split()[0] == "Z")
                finally:
                    if marker.exists():
                        try:
                            os.kill(int(marker.read_text()), signal.SIGKILL)
                        except ProcessLookupError:
                            pass

    def test_package_cancellation_includes_descendant_in_another_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "child"
            child = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(30)" % str(marker)
            parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',%r],start_new_session=True); time.sleep(30)" % child
            process = subprocess.Popen([sys.executable, "-c", parent], start_new_session=True)
            try:
                deadline = time.monotonic() + 5
                while not marker.exists() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(marker.exists())
                pid = int(marker.read_text())
                start = Path(f"/proc/{process.pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
                result = subprocess.run(["/bin/sh", "-c", GUEST_PACKAGE_CANCEL, "fixture",
                                         str(process.pid), start], capture_output=True, text=True, timeout=10)
                self.assertEqual(result.stdout.strip(), "GONE", result.stderr)
                process.wait(timeout=2)
                stat = Path(f"/proc/{pid}/stat")
                self.assertTrue(not stat.exists() or stat.read_text().rsplit(") ", 1)[1].split()[0] == "Z")
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()

    def test_timeout_retains_output_and_cleanup_outcome(self):
        for outcome in ("gone", "unknown"):
            with self.assertRaises(NativeCommandError) as caught:
                run_native([sys.executable, "-c", "import time; print('written',flush=True); time.sleep(30)"],
                           interactive=False, stream=False, timeout=.2, abort=lambda: outcome)
            self.assertIn("written", caught.exception.stdout)
            self.assertEqual(caught.exception.cleanup, outcome)

    def test_termination_signals_clean_child_and_restore_handlers(self):
        with tempfile.TemporaryDirectory() as temporary:
            for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
                marker = Path(temporary) / str(number)
                program = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); print('started',flush=True); time.sleep(30)" % str(marker)
                wrapper = """
import signal,sys,json
from unifier.native_io import NativeCommandError, run_native
before={n: signal.getsignal(n) for n in (signal.SIGTERM,signal.SIGHUP,signal.SIGINT)}
try:
    run_native([sys.executable,'-c',%r],interactive=False,stream=False,timeout=10)
except NativeCommandError as exc:
    assert all(signal.getsignal(n)==h for n,h in before.items())
    print(json.dumps({'signum':exc.signum,'cleanup':exc.cleanup,'stdout':exc.stdout}))
""" % program
                process = subprocess.Popen([sys.executable, "-c", wrapper], stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE, env={**os.environ, "PYTHONPATH": str(ROOT / "src")})
                try:
                    deadline = time.monotonic() + 5
                    while not marker.exists() and time.monotonic() < deadline:
                        time.sleep(.01)
                    self.assertTrue(marker.exists())
                    child_pid = int(marker.read_text())
                    process.send_signal(number)
                    output, errors = process.communicate(timeout=8)
                    self.assertEqual(process.returncode, 0, errors)
                    result = json.loads(output)
                    self.assertEqual(result["signum"], number)
                    self.assertEqual(result["cleanup"], "gone")
                    self.assertFalse(Path(f"/proc/{child_pid}").exists())
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.communicate()

    def test_pipe_capture_preserves_streams_and_failure(self):
        result = run_native([sys.executable, "-c", "import sys; print('out'); print('err',file=sys.stderr); sys.exit(7)"],
                            interactive=False, stream=False, timeout=5)
        self.assertEqual((result.returncode, result.stdout, result.stderr), (7, "out\n", "err\n"))

    def test_native_prompt_input_resize_and_terminal_restoration(self):
        child = """
import json, os, sys, termios
from unifier.native_io import run_native
before = termios.tcgetattr(0)
program = 'import os; print("PROMPT [Y/n]", flush=True); answer=input(); print("ANSWER="+answer, flush=True); print("SIZE="+str(os.get_terminal_size(0)), flush=True)'
r=run_native([sys.executable, '-c', program], interactive=True, stream=True, timeout=8)
assert termios.tcgetattr(0) == before
print('RESULT='+str(r.returncode), flush=True)
"""
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        process = subprocess.Popen([sys.executable, "-c", child], stdin=slave, stdout=slave, stderr=slave,
                                   env={**os.environ, "PYTHONPATH": str(ROOT / "src")}, start_new_session=True)
        os.close(slave)
        data = b""
        answered = False
        try:
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        data += os.read(master, 65536)
                    except OSError:
                        break
                if b"PROMPT" in data and not answered:
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 38, 120, 0, 0))
                    process.send_signal(signal.SIGWINCH)
                    time.sleep(0.1)
                    os.write(master, b"n\n")
                    answered = True
                if process.poll() is not None and b"RESULT=" in data:
                    break
            self.assertEqual(process.wait(timeout=2), 0, data)
            self.assertIn(b"ANSWER=n", data)
            self.assertIn(b"columns=120, lines=38", data)
            self.assertIn(b"RESULT=0", data)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            os.close(master)


if __name__ == "__main__":
    unittest.main()
