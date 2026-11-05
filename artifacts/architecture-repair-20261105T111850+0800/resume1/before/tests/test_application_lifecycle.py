"""Lease acceptance: real micro process trees in a fake container boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from test_application_supervisor import DAEMON, SCRIPT
from test_on_demand import ready
import test_runtime_control as handshake
from unifier.application_supervisor import observe, signal_exact
from unifier.application_leases import cancel_application
from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup, live_leases, managed_open
from unifier.runtime import parse_guest_ready


class ApplicationLifecycle(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service, self.runtime = ready(self.root)
        self.group = LaunchGroup(self.service, 'debian13')
        self.receipts = self.root / 'receipts'; self.receipts.mkdir(mode=0o700)
        self.children = []
        self.addCleanup(self.cleanup)
        self.runtime.start_application_exec = self.start
        self.runtime.observe_application = lambda name, token, **kwargs: observe(str(self.receipts), token)
        self.runtime.signal_application_guest = self.send

    def cleanup(self):
        for child in self.children:
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=15)

    def start(self, name, argv, *, token, **kwargs):
        read_fd, write_fd = os.pipe()
        try:
            child = subprocess.Popen([sys.executable, str(SCRIPT), 'run', token,
                str(self.receipts), str(write_fd), *argv], pass_fds=(write_fd,),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True)
            self.children.append(child)
        finally:
            os.close(write_fd)
        try:
            if not select.select([read_fd], [], [], 5)[0]:
                raise AssertionError('application fixture failed to report readiness')
            line = os.read(read_fd, 4096).decode()
        finally:
            os.close(read_fd)
        guest = replace(parse_guest_ready(line, token), ownership='subreaper-v1')
        class Execution:
            def __init__(inner):
                inner.guest = guest
            def poll(inner):
                return child.poll()
            def signal(inner, number):
                return 'signalled' if signal_exact(guest.pid, guest.start, number) else 'unknown'
        return Execution()

    def send(self, name, guest, number, **kwargs):
        value = observe(str(self.receipts), guest.token)
        if value.get('pid') != guest.pid or value.get('start') != guest.start:
            return 'unknown'
        return 'signalled' if signal_exact(guest.pid, guest.start, number) else 'unknown'

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while (not path.exists() or not path.stat().st_size) and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(path.exists() and path.stat().st_size)

    def daemon_argv(self, key):
        return [sys.executable, '-c', DAEMON, str(self.root/key), str(self.root/('end-'+key))]

    def set_argv(self, argv):
        launch = self.service.show_app('notes'); launch['argv'] = argv
        self.service.store.save_launch(launch)

    def dead_owner(self, token):
        state = self.group.read()
        state['leases'][token].update(pid=999999999, stamp=1)
        self.group.save(state)

    def test_launcher_return_does_not_release_daemon_lease_or_bridge(self):
        self.set_argv(self.daemon_argv('daemon'))
        with patch('unifier.on_demand.start_bridge', return_value={'fixture': 'bridge'}), \
             patch('unifier.on_demand.stop_bridge') as stop, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(managed_open, self.service, 'notes', [])
            try:
                self.wait_file(self.root/'daemon')
                self.assertFalse(future.done())
                self.assertTrue(self.group.read()['leases'])
                self.assertEqual(self.group.read()['bridge'], {'fixture': 'bridge'})
                self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'running')
                stop.assert_not_called()
            finally:
                (self.root/'end-daemon').touch()
            self.assertEqual(future.result(timeout=5).returncode, 0)
            self.assertEqual(self.group.read()['leases'], {})
            stop.assert_called_once()
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'stopped')

    def test_worker_recovery_reconnects_by_token_even_before_pid_was_saved(self):
        token = self.group.acquire('notes', owned_execution=True)
        self.group.update_execution(token, state='starting')
        execution = self.start('cid-debian13', self.daemon_argv('daemon'), token=token)
        self.wait_file(self.root/'daemon'); self.dead_owner(token)
        self.assertIsNone(self.group.read()['leases'][token]['execution']['guest'])
        result = self.group.recover_applications(token)
        self.assertEqual(result[0]['action'], 'kept-running')
        self.assertIn(token, self.group.read()['leases'])
        (self.root/'end-daemon').touch(); self.children[-1].wait(timeout=5)
        result = self.group.recover_applications(token)
        self.assertEqual(result[0]['action'], 'released')
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'stopped')

    def test_explicit_cancel_targets_the_daemon_and_releases_only_its_lease(self):
        token = self.group.acquire('notes', owned_execution=True)
        project = self.group.acquire_project('project-fixture',
            expected_binding=self.service.show_app('notes')['environment_binding'])
        self.group.update_execution(token, state='starting')
        execution = self.start('cid-debian13', self.daemon_argv('daemon'), token=token)
        self.wait_file(self.root/'daemon')
        self.group.update_execution(token, state='running', guest={
            'pid': execution.guest.pid, 'start': execution.guest.start})
        result = cancel_application(self.group, token, seconds=5)
        self.assertEqual(result['state'], 'exited')
        self.children[-1].wait(timeout=5)
        self.assertEqual(set(self.group.read()['leases']), {project})
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'running')
        self.group.release(project)
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'stopped')

    def test_unknown_spawn_and_probe_failure_keep_lease_and_bridge(self):
        def fail(*args, **kwargs):
            raise UnifierError('handshake timeout after possible spawn')
        self.runtime.start_application_exec = fail
        with patch('unifier.on_demand.start_bridge', return_value={'fixture': 'bridge'}), \
             patch('unifier.on_demand.stop_bridge') as stop:
            with self.assertRaisesRegex(UnifierError, 'timeout'):
                managed_open(self.service, 'notes', [])
            token = next(iter(self.group.read()['leases']))
            self.assertEqual(self.group.read()['leases'][token]['execution']['state'], 'recovery-required')
            self.dead_owner(token)
            self.runtime.observe_application = lambda *args, **kwargs: {'state': 'unknown'}
            self.assertEqual(self.group.recover_applications(token)[0]['action'], 'kept-unknown')
            self.assertIn(token, self.group.read()['leases'])
            self.assertEqual(self.group.read()['bridge'], {'fixture': 'bridge'})
            stop.assert_not_called()

    def test_dead_legacy_app_lease_is_retained_and_corrupt_state_refuses(self):
        legacy = {'app_id': 'notes', 'pid': 999999999, 'stamp': 1}
        self.assertEqual(live_leases({'legacy': legacy}), {'legacy': legacy})
        self.group.save({'leases': {'legacy': legacy}, 'auto_stop': True, 'bridge': {'old': True}})
        self.assertEqual(self.group.recover_applications()[0]['action'], 'kept-unknown')
        self.assertIn('legacy', self.group.read()['leases'])
        self.group.path.write_text('{')
        with self.assertRaisesRegex(UnifierError, 'corrupt'):
            self.group.read()

    def test_generation_move_and_manual_restart_never_release_or_stop_new_owner(self):
        token = self.group.acquire('notes', owned_execution=True)
        self.group.update_execution(token, state='starting')
        self.dead_owner(token)
        record = self.service.store.load('debian13'); record['generation'] += 1
        self.service.store.save(record, None)
        self.assertEqual(self.group.recover_applications(token)[0]['action'], 'kept-unknown')
        self.assertIn(token, self.group.read()['leases'])
        # A separate fresh group demonstrates that joining after a manual
        # restart cannot renew old auto-stop ownership.
        record['generation'] -= 1; self.service.store.save(record, None)
        self.group.update_execution(token, state='exited'); self.group.release(token)
        first = self.group.acquire('notes')
        name = record['runtime']['container_name']
        current = self.runtime.inspect(name)
        self.runtime.containers[name] = replace(current, started_at='manual-restart')
        second = self.group.acquire('notes')
        self.group.release(first); self.group.release(second)
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'running')


class ApplicationControlChannel(unittest.TestCase):
    def test_real_fd3_handshake_and_terminal_receipt_through_the_runtime(self):
        fixture = handshake.RealHandshake()
        fixture.setUpClass(); self.addCleanup(fixture.tearDownClass)
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture.runtime.application_state_root = str(fixture.root/'receipts')
        execution = fixture.runtime.start_application_exec(handshake.CONTAINER_ID,
            [sys.executable, '-c', 'print("app-payload");raise SystemExit(4)'],
            token='d'*32, project=handshake.PROJECT, env_id=handshake.ENV_ID,
            workdir=None, env={}, interactive=False, tty=False)
        out, err = execution.process.communicate(timeout=5)
        self.assertEqual(execution.guest.ownership, 'subreaper-v1')
        self.assertEqual(out, b'app-payload\n', err)
        self.assertEqual(execution.process.returncode, 4)
        self.assertEqual(execution.probe(), 'gone')
        self.assertEqual(handshake.preserved_flags(fixture.last_command), [('--preserve-fds', '1')])


if __name__ == '__main__':
    unittest.main()
