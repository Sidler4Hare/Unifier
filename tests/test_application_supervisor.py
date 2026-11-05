"""Real Linux process-tree fixtures inside the bounded offline test cgroup."""
import ctypes
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from unifier.application_supervisor import observe, process, signal_exact
from unifier.runtime import parse_guest_ready

SCRIPT = Path(__file__).resolve().parents[1] / 'src/unifier/application_supervisor.py'
DAEMON = '''import json,os,sys,time
from pathlib import Path
if os.fork(): os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
Path(sys.argv[1]).write_text(str(os.getpid()))
while not Path(sys.argv[2]).exists(): time.sleep(.02)
'''


class ApplicationSupervisor(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.processes = []
        self.addCleanup(self.cleanup)

    def cleanup(self):
        for child in self.processes:
            if child.poll() is None:
                child.terminate()
            child.communicate(timeout=15)

    def launch(self, argv):
        token = uuid.uuid4().hex
        read_fd, write_fd = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, str(SCRIPT), 'run', token,
                str(self.root), str(write_fd), *argv], pass_fds=(write_fd,),
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                start_new_session=True)
            self.processes.append(child)
        finally:
            os.close(write_fd)
        try:
            with os.fdopen(read_fd, 'rb', buffering=0) as stream:
                # A broken prerequisite exits and closes the pipe; the outside
                # cgroup deadline is an independent bound on any protocol bug.
                line = stream.readline(4096).decode()
            guest = parse_guest_ready(line, token)
        except BaseException:
            child.terminate()
            raise
        return token, guest, child

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists(), str(path))

    def test_foreground_exit_has_a_token_bound_terminal_receipt(self):
        token, guest, child = self.launch([sys.executable, '-c', 'print("payload");raise SystemExit(7)'])
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 7, err)
        self.assertEqual(out, b'payload\n')
        value = observe(str(self.root), token)
        self.assertEqual((value['state'], value['exit_code'], value['pid'], value['start']),
                         ('exited', 7, guest.pid, guest.start))
        self.assertTrue(value['all_children_reaped'])

    def test_double_fork_setsid_child_outlives_launcher_and_delays_completion(self):
        marker, release = self.root / 'daemon', self.root / 'release'
        token, guest, child = self.launch([sys.executable, '-c', DAEMON, str(marker), str(release)])
        self.wait_file(marker)
        self.assertEqual(observe(str(self.root), token)['state'], 'running')
        with self.assertRaises(subprocess.TimeoutExpired):
            child.wait(timeout=.1)
        release.touch()
        out, err = child.communicate(timeout=5)
        self.assertEqual(child.returncode, 0, err)
        self.assertEqual(observe(str(self.root), token)['state'], 'exited')

    def test_two_application_trees_do_not_share_termination(self):
        first = self.launch([sys.executable, '-c', DAEMON, str(self.root/'a'), str(self.root/'end-a')])
        second = self.launch([sys.executable, '-c', DAEMON, str(self.root/'b'), str(self.root/'end-b')])
        self.wait_file(self.root/'a'); self.wait_file(self.root/'b')
        (self.root/'end-a').touch()
        first[2].communicate(timeout=5)
        self.assertEqual(observe(str(self.root), first[0])['state'], 'exited')
        self.assertEqual(observe(str(self.root), second[0])['state'], 'running')
        self.assertIsNone(second[2].poll())
        (self.root/'end-b').touch()
        second[2].communicate(timeout=5)

    def test_cancellation_reaches_a_daemon_in_a_different_session(self):
        marker = self.root / 'daemon'
        token, guest, child = self.launch([sys.executable, '-c', DAEMON, str(marker), str(self.root/'unused')])
        self.wait_file(marker)
        self.assertTrue(signal_exact(guest.pid, guest.start, signal.SIGTERM))
        child.communicate(timeout=5)
        value = observe(str(self.root), token)
        self.assertEqual(value['state'], 'exited')
        self.assertTrue(value['all_children_reaped'])
        self.assertEqual(value['delivered_signal'], signal.SIGTERM)

    def test_supervisor_killed_is_unknown_and_does_not_fabricate_termination(self):
        # Adopt only this fixture's orphan so it can be explicitly reaped here.
        libc = ctypes.CDLL(None)
        enabled = ctypes.c_int()
        self.assertEqual(libc.prctl(37, ctypes.byref(enabled), 0, 0, 0), 0)
        self.assertEqual(libc.prctl(36, 1, 0, 0, 0), 0)
        self.addCleanup(lambda: libc.prctl(36, enabled.value, 0, 0, 0))
        marker = self.root / 'orphan'
        token, guest, child = self.launch([sys.executable, '-c', DAEMON, str(marker), str(self.root/'unused')])
        self.wait_file(marker)
        pid = int(marker.read_text()); identity = process(pid)
        try:
            self.assertTrue(signal_exact(guest.pid, guest.start, signal.SIGKILL))
            child.wait(timeout=5)
            self.assertEqual(observe(str(self.root), token)['state'], 'unknown')
            self.assertFalse(process(pid)['zombie'])
        finally:
            if identity and process(pid) and process(pid)['start'] == identity['start']:
                signal_exact(pid, identity['start'], signal.SIGTERM)
                os.waitpid(pid, 0)
        child.communicate(timeout=5)

    def test_missing_corrupt_and_reused_pid_receipts_are_unknown(self):
        token = uuid.uuid4().hex
        self.assertEqual(observe(str(self.root), token)['state'], 'unknown')
        path = self.root / (token + '.json')
        path.write_text('{')
        self.assertEqual(observe(str(self.root), token)['state'], 'unknown')
        path.write_text(json.dumps({'token': token, 'ownership': 'subreaper-v1',
            'state': 'running', 'pid': os.getpid(), 'start': 1}))
        self.assertEqual(observe(str(self.root), token)['state'], 'unknown')


if __name__ == '__main__':
    unittest.main()
