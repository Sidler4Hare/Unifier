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
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_application_supervisor import DAEMON, SCRIPT
from test_on_demand import ready
import test_runtime_control as handshake
from unifier.application_supervisor import observe, signal_exact
from unifier.application_leases import cancel_application
from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup, live_leases, managed_open, managed_exec
from unifier.runtime import GuestProcess, parse_guest_ready


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
        mode = 'run-stop-children' if kwargs.get('exit_policy') == 'stop-children' else 'run'
        try:
            child = subprocess.Popen([sys.executable, str(SCRIPT), mode, token,
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

    def test_foreground_policy_reaps_helpers_then_releases_only_its_lease(self):
        launch = self.service.show_app('notes')
        launch['exit_policy'] = 'stop-children'
        self.service.store.save_launch(launch)
        # A separate project lease represents the concurrent CLI owner.
        peer = self.group.acquire_project('peer-cli', expected_binding=launch['environment_binding'])
        code = '''import os,sys,time
from pathlib import Path
marker=Path(sys.argv[1])
if os.fork():
 while not marker.exists(): time.sleep(.01)
 os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
marker.touch()
while True: time.sleep(.05)
'''
        result = managed_exec(self.service, 'notes', [sys.executable, '-c', code, str(self.root/'helper')])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(set(self.group.read()['leases']), {peer})
        proofs = [json.loads(p.read_text()) for p in self.receipts.glob('*.json')]
        self.assertEqual(len(proofs), 1)
        self.assertTrue(proofs[0]['all_children_reaped'])
        self.assertEqual(proofs[0]['cleanup_reason'], 'main-exit')
        self.assertFalse(proofs[0]['cleanup_escalated'])
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'running')
        self.group.release(peer)
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
        self.dead_owner(token)
        result = cancel_application(self.group, token, seconds=5)
        self.assertEqual(result['state'], 'exited')
        self.children[-1].wait(timeout=5)
        self.assertEqual(set(self.group.read()['leases']), {project})
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'running')
        self.group.release(project)
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'stopped')

    def test_live_worker_accepts_terminal_proof_after_concurrent_cancel(self):
        gate = threading.Event()
        def delayed_poll(*args, **kwargs):
            execution = self.start(*args, **kwargs)
            original = execution.poll
            execution.poll = lambda: original() if gate.is_set() else None
            return execution
        self.runtime.start_application_exec = delayed_poll
        self.set_argv(self.daemon_argv('daemon'))
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(managed_open, self.service, 'notes', [])
            try:
                self.wait_file(self.root/'daemon')
                token = next(iter(self.group.read()['leases']))
                result = cancel_application(self.group, token, seconds=5)
                self.assertEqual(result['state'], 'exited')
                self.assertEqual(result['lease'], 'awaiting-worker')
                self.assertEqual(self.group.read()['leases'][token]['execution']['state'], 'exited')
            finally:
                gate.set()
            self.assertEqual(future.result(timeout=5).returncode, 0)
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'stopped')

    def test_short_activation_keeps_existing_application_lease_and_bridge(self):
        self.set_argv(self.daemon_argv('daemon'))
        with patch('unifier.on_demand.start_bridge', return_value={'fixture': 'bridge'}), \
             patch('unifier.on_demand.stop_bridge') as stop, ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(managed_open, self.service, 'notes', [])
            try:
                self.wait_file(self.root/'daemon')
                original_tokens = set(self.group.read()['leases'])
                result = managed_exec(self.service, 'notes', [sys.executable, '-c', 'pass'])
                self.assertEqual(result.returncode, 0)
                self.assertEqual(set(self.group.read()['leases']), original_tokens)
                self.assertFalse(future.done())
                self.assertEqual(self.group.read()['bridge'], {'fixture': 'bridge'})
                stop.assert_not_called()
            finally:
                (self.root/'end-daemon').touch()
            self.assertEqual(future.result(timeout=5).returncode, 0)
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

    def test_stopped_container_releases_lease_without_fabricating_exit_status(self):
        def stopped_during_execution(name, argv, *, token, project, env_id, **kwargs):
            container_name = self.service.store.load(env_id)['runtime']['container_name']
            self.runtime.stop(container_name, project=project, env_id=env_id)
            guest = GuestProcess(20, 30, 20, 20, token, 'subreaper-v1')
            return SimpleNamespace(guest=guest, poll=lambda: 137)

        self.runtime.start_application_exec = stopped_during_execution
        with self.assertRaisesRegex(UnifierError, 'without a native exit status'):
            managed_open(self.service, 'notes', [])
        self.assertEqual(self.group.read()['leases'], {})
        self.assertEqual(self.service.inspect('debian13')['runtime_state'], 'stopped')

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
    def test_foreground_policy_reaches_the_guest_through_real_fd3(self):
        fixture = handshake.RealHandshake()
        fixture.setUpClass(); self.addCleanup(fixture.tearDownClass)
        fixture.setUp(); self.addCleanup(fixture.tearDown)
        fixture.runtime.application_state_root = str(fixture.root/'receipts')
        execution = fixture.runtime.start_application_exec(handshake.CONTAINER_ID,
            [sys.executable, '-c', 'raise SystemExit(0)'], exit_policy='stop-children',
            token='e'*32, project=handshake.PROJECT, env_id=handshake.ENV_ID,
            workdir=None, env={}, interactive=False, tty=False)
        _out, err = execution.process.communicate(timeout=5)
        self.assertEqual(execution.process.returncode, 0, err)
        value = observe(fixture.runtime.application_state_root, 'e'*32)
        self.assertEqual(value['exit_policy'], 'stop-children')
        self.assertTrue(value['all_children_reaped'])

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
