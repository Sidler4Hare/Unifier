"""Window changes reach the detached worker and the owned Podman proxy."""
import os
import signal
import subprocess
import sys
import threading
import unittest
from unittest import mock
from unifier.project_sessions import ProjectSessions
from unifier.runtime import SessionExecution


@unittest.skipUnless(hasattr(signal, 'SIGWINCH'), 'requires Unix TTY signals')
class ProjectResize(unittest.TestCase):
    def child(self):
        program = """
import signal, sys, time
signal.signal(signal.SIGWINCH, lambda *_: sys.exit(0))
print('ready', flush=True)
time.sleep(5)
sys.exit(7)
"""
        process = subprocess.Popen([sys.executable, '-c', program],
                                   start_new_session=True,
                                   stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.settle, process)
        self.assertEqual(process.stdout.readline().strip(), 'ready')
        return process

    @staticmethod
    def settle(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=3)
        process.stdout.close()

    def test_launcher_relays_winch_to_detached_worker_and_restores_handler(self):
        process = self.child()
        waiting = threading.Event()
        original = signal.getsignal(signal.SIGWINCH)

        class Worker:
            pid = process.pid

            def poll(self):
                return process.poll()

            def wait(self):
                waiting.set()
                return process.wait(timeout=3)

        def resize():
            if waiting.wait(timeout=3):
                os.kill(os.getpid(), signal.SIGWINCH)

        sender = threading.Thread(target=resize)
        sender.start()
        try:
            # This check exercises signal routing; its stdin is the test
            # runner's pipe, while the real PTY dimensions are checked by the
            # native acceptance probe.
            with mock.patch('unifier.project_sessions.sys.stdin') as terminal:
                terminal.fileno.side_effect = ValueError('no terminal in test runner')
                result = ProjectSessions._wait_forwarding(None, Worker(), tty=True)
        finally:
            sender.join(timeout=3)
        self.assertEqual(result, 0)
        self.assertEqual(signal.getsignal(signal.SIGWINCH), original)

    def test_supervisor_resizes_proxy_without_cancelling_guest(self):
        process = self.child()
        execution = SessionExecution(object(), name='owned', project='test',
                                     env_id='test', process=process,
                                     guest=None, user='1000:1000')
        pending = {'signal': None, 'resize': True}
        result = ProjectSessions._supervise(None, execution,
                                           {'signals': [], 'tty': True}, pending)
        self.assertEqual(result, (0, None, []))
        self.assertFalse(pending['resize'])
