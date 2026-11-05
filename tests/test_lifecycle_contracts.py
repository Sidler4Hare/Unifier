"""Offline contracts for PID 1, leases, stopping races and supervised launchers.

The runtime is always FakeRuntime or a subclass: no podman command and no real
container is started by these tests.
"""
from dataclasses import replace
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]

from unifier import cli, on_demand
from unifier import service as service_module
from unifier.errors import UnifierError
from unifier.on_demand import (
    LaunchGroup,
    live_leases,
    managed_open,
    process_stamp,
    start_bridge,
    stop_bridge,
    supervised_open,
)
from unifier.runtime import CreateSpec, ExecResult, container_name, creation_argv
from test_desktop import SESSION
from test_env import FakeRuntime, service_in


# Host-only bookkeeping may expire by PID. APP ownership must not.
DEAD_HOST_LEASE = {'pid': 999999, 'stamp': 1}


def ready(root, runtime=None, *, portals=False):
    runtime = runtime if runtime is not None else FakeRuntime()
    service, runtime = service_in(root, runtime=runtime)
    if portals:
        service.create('debian13', None, [], desktop=True, session=SESSION, portals=True)
    else:
        service.create('debian13', None, [], desktop=True, session=SESSION)
    service.add_app('debian13', 'notes', '/usr/bin/true', ['/usr/bin/true'], on_demand=True)
    return service, runtime


def env_container_name(root):
    return container_name(root, 'debian13')


def write_stale_lease(service, **fields):
    group = LaunchGroup(service, 'debian13')
    state = group.read()
    state.update(fields)
    group.save(state)
    return group


class TickingRuntime(FakeRuntime):
    """Every start gets a distinct StartedAt, like a real podman restart."""

    def __init__(self):
        super().__init__()
        self.starts = 0

    def start(self, name, *, project, env_id):
        super().start(name, project=project, env_id=env_id)
        self.starts += 1
        info = self.containers[name]
        self.containers[name] = replace(
            info, started_at='2026-10-19T12:00:%02d+08:00' % self.starts)


class SettlingRuntime(FakeRuntime):
    """Reports 'stopping' for settle_after inspections, then exits."""

    def __init__(self, settle_after=1, never_settles=False):
        super().__init__()
        self.settle_after = settle_after
        self.never_settles = never_settles
        self.stopping_left = 0
        self.start_calls = 0
        self.stop_calls = 0

    def mark_stopping(self, name):
        info = self.containers[name]
        self.containers[name] = replace(info, status='stopping', running=False)
        self.stopping_left = self.settle_after

    def inspect(self, name):
        info = self.containers.get(name)
        if info is None:
            return None
        if info.status != 'stopping' or self.never_settles:
            return info
        if self.stopping_left > 0:
            self.stopping_left -= 1
            return info
        settled = replace(info, status='exited', running=False,
                          finished_at='2026-10-19T12:00:09+08:00')
        self.containers[name] = settled
        return settled

    def start(self, name, *, project, env_id):
        info = self._owned(name, project, env_id)
        if info.status == 'stopping':
            raise UnifierError('podman start refuses a stopping container')
        self.start_calls += 1
        super().start(name, project=project, env_id=env_id)

    def stop(self, name, *, project, env_id):
        info = self._owned(name, project, env_id)
        if info.status == 'stopping':
            raise UnifierError('podman stop refuses a stopping container')
        self.stop_calls += 1
        super().stop(name, project=project, env_id=env_id)


class SlowStartRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.start_calls = 0

    def start(self, name, *, project, env_id):
        self._owned(name, project, env_id)
        self.start_calls += 1
        time.sleep(.1)
        super().start(name, project=project, env_id=env_id)


class InitProcess(unittest.TestCase):
    def test_creation_passes_init_so_pid_one_forwards_signals_and_reaps(self):
        spec = CreateSpec('name', 'id', '/project', 'repo@sha256:' + 'a' * 64, ())
        argv = creation_argv(spec)
        self.assertEqual(argv.count('--init'), 1)
        self.assertLess(argv.index('--init'), argv.index(spec.image))
        entrypoint = argv.index('--entrypoint')
        self.assertLess(entrypoint, argv.index(spec.image))
        self.assertEqual(argv[entrypoint + 1], '/usr/bin/sleep')

    def test_created_environment_record_uses_the_init_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = ready(Path(temporary))
            spec = runtime.events[0][1]
            self.assertIn('--init', creation_argv(spec))


class StoppingRace(unittest.TestCase):
    def test_start_waits_for_a_stopping_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root, runtime=SettlingRuntime())
            service.create('debian13', None, [])
            runtime.start_calls = 0
            runtime.mark_stopping(env_container_name(root))
            view = service.start('debian13')
            self.assertEqual(view['runtime_state'], 'running')
            self.assertEqual(runtime.start_calls, 1)

    def test_stop_waits_instead_of_treating_stopping_as_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root, runtime=SettlingRuntime())
            service.create('debian13', None, [])
            service.start('debian13')
            runtime.stop_calls = 0
            runtime.mark_stopping(env_container_name(root))
            view = service.stop('debian13')
            self.assertEqual(view['runtime_state'], 'stopped')
            self.assertEqual(runtime.stop_calls, 0)
            self.assertEqual(runtime.containers[env_container_name(root)].status, 'exited')

    def test_start_refuses_a_container_that_never_settles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = SettlingRuntime(never_settles=True)
            service, runtime = service_in(root, runtime=runtime)
            service.create('debian13', None, [])
            runtime.start_calls = 0
            runtime.mark_stopping(env_container_name(root))
            with patch.object(service_module, 'LIFECYCLE_SETTLE_SECONDS', .1), \
                 patch.object(service_module, 'LIFECYCLE_SETTLE_POLL_SECONDS', .01):
                with self.assertRaisesRegex(UnifierError, 'still stopping'):
                    service.start('debian13')
            self.assertEqual(runtime.start_calls, 0)

    def test_concurrent_start_calls_enter_the_runtime_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = SlowStartRuntime()
            service, runtime = service_in(root, runtime=runtime)
            service.create('debian13', None, [])
            runtime.start_calls = 0
            errors = []

            def call():
                try:
                    service.start('debian13')
                except BaseException as exc:  # pragma: no cover - failure detail
                    errors.append(exc)

            threads = [threading.Thread(target=call) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)
            self.assertEqual(errors, [])
            self.assertEqual(runtime.start_calls, 1)


class AutoStopOwnership(unittest.TestCase):
    def test_unknown_legacy_app_lease_keeps_its_started_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            service.start('debian13')
            info = service._require_owned(service.store.load('debian13'))
            legacy = dict(DEAD_HOST_LEASE, app_id='notes')
            group = write_stale_lease(service, auto_stop=True, container_id=info.container_id,
                container_started_at=info.started_at, leases={'legacy-app': legacy})
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
            self.assertEqual(group.read()['leases'], {'legacy-app': legacy})
            self.assertEqual(group.recover_applications()[0]['action'], 'kept-unknown')

    def test_stale_host_lease_restores_auto_stop_for_the_same_container_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            service.start('debian13')
            info = service._require_owned(service.store.load('debian13'))
            write_stale_lease(service, auto_stop=True, container_id=info.container_id,
                              container_started_at=info.started_at,
                              leases={'dead-launcher': DEAD_HOST_LEASE})
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')
            state = LaunchGroup(service, 'debian13').read()
            self.assertEqual(state['leases'], {})
            self.assertFalse(state['auto_stop'])

    def test_manual_restart_changes_started_at_and_keeps_the_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            service.start('debian13')
            info = service._require_owned(service.store.load('debian13'))
            service.stop('debian13')
            service.start('debian13')
            write_stale_lease(service, auto_stop=True, container_id=info.container_id,
                              container_started_at=info.started_at,
                              leases={'dead-launcher': DEAD_HOST_LEASE})
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
            state = LaunchGroup(service, 'debian13').read()
            self.assertEqual(state['leases'], {})
            self.assertFalse(state['auto_stop'])

    def test_missing_container_started_at_is_kept_running(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            service.start('debian13')
            info = service._require_owned(service.store.load('debian13'))
            write_stale_lease(service, auto_stop=True, container_id=info.container_id,
                              leases={'dead-launcher': DEAD_HOST_LEASE})
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')

    def test_foreign_container_is_never_stopped_by_an_old_flag(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            service.start('debian13')
            info = service._require_owned(service.store.load('debian13'))
            write_stale_lease(service, auto_stop=True, container_id='cid-from-another-run',
                              container_started_at=info.started_at,
                              leases={'dead-launcher': DEAD_HOST_LEASE})
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')

    def test_manually_started_environment_with_a_dead_lease_survives(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            service.start('debian13')
            write_stale_lease(service, auto_stop=False, leases={'dead-launcher': DEAD_HOST_LEASE})
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')


    def test_manual_restart_during_the_lease_is_not_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            group = LaunchGroup(service, 'debian13')
            lease = group.acquire('notes')
            # The user restarts the environment while the application is open.
            service.stop('debian13')
            service.start('debian13')
            group.release(lease)
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
            self.assertFalse(group.read()['auto_stop'])
            # A later managed open treats the restarted instance as manual too.
            managed_open(service, 'notes', [])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')

    def test_container_stopped_during_the_lease_is_cleaned_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, TickingRuntime())
            group = LaunchGroup(service, 'debian13')
            lease = group.acquire('notes')
            service.stop('debian13')
            group.release(lease)
            self.assertFalse(group.read()['auto_stop'])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')


class SharedLeases(unittest.TestCase):
    def test_three_leases_share_one_environment_until_the_last_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = ready(root)
            groups = [LaunchGroup(service, 'debian13') for _ in range(3)]
            runtime.events.clear()
            leases = [group.acquire('notes') for group in groups]
            self.assertEqual(sum(1 for event in runtime.events if event[0] == 'start'), 1)
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
            groups[0].release(leases[0])
            groups[1].release(leases[1])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
            groups[2].release(leases[2])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')
            self.assertEqual(sum(1 for event in runtime.events if event[0] == 'stop'), 1)
            self.assertEqual(LaunchGroup(service, 'debian13').read()['leases'], {})


class LeaseIdentity(unittest.TestCase):
    def test_recycled_pid_cannot_extend_an_old_lease(self):
        stamp = process_stamp(os.getpid())
        items = {'old': {'pid': os.getpid(), 'stamp': stamp + 1},
                 'current': {'pid': os.getpid(), 'stamp': stamp},
                 'dead': DEAD_HOST_LEASE}
        self.assertEqual(set(live_leases(items)), {'current'})


class LeaseRelease(unittest.TestCase):
    def test_failed_start_leaves_no_lease_and_removes_its_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root)
            group = LaunchGroup(service, 'debian13')
            stopped = []

            def fail(*_args, **_kwargs):
                raise UnifierError('start failed')

            with patch.object(on_demand, 'start_bridge',
                              return_value={'pid': 7, 'stamp': 8, 'socket': None}), \
                 patch.object(on_demand, 'stop_bridge', side_effect=stopped.append), \
                 patch.object(service, 'start', fail):
                with self.assertRaisesRegex(UnifierError, 'start failed'):
                    group.acquire('notes')
            self.assertEqual(stopped, [{'pid': 7, 'stamp': 8, 'socket': None}])
            self.assertEqual(group.read()['leases'], {})
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')

    def test_release_saves_the_removed_lease_when_stopping_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root)
            group = LaunchGroup(service, 'debian13')
            lease = group.acquire('notes')

            def fail(*_args, **_kwargs):
                raise UnifierError('stop failed')

            with patch.object(service, 'stop', fail):
                with self.assertRaisesRegex(UnifierError, 'stop failed'):
                    group.release(lease)
            state = group.read()
            self.assertEqual(state['leases'], {})
            self.assertIsNone(state['bridge'])
            self.assertTrue(state['auto_stop'])


class FakeWorker:
    def __init__(self, code=0):
        self.code = code
        self.calls = []
        self.waited = False

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), dict(kwargs)))
        return self

    def wait(self):
        self.waited = True
        return self.code


class Supervision(unittest.TestCase):
    def test_launcher_waits_for_a_detached_worker_and_maps_its_status(self):
        for code, expected in [(0, 0), (3, 3), (-9, 137), (-15, 143)]:
            worker = FakeWorker(code)
            with patch.object(on_demand.subprocess, 'Popen', worker):
                result = supervised_open(None, 'notes', ['--file', 'x.txt'])
            self.assertEqual(result.returncode, expected)
            self.assertTrue(worker.waited)
        command, kwargs = worker.calls[0]
        self.assertEqual(command, [sys.executable, str(ROOT / 'tools/unifier.py'),
                                   'app', '_worker', 'notes', '--', '--file', 'x.txt'])
        self.assertTrue(kwargs['start_new_session'])
        self.assertEqual(kwargs['env'][on_demand.WORKER_MARKER], '1')
        self.assertNotIn('stdout', kwargs)
        self.assertNotIn('stderr', kwargs)

    def test_worker_never_spawns_a_second_supervisor(self):
        seen = []

        def fake_managed(service, app_id, extra):
            seen.append((service, app_id, extra))
            return ExecResult(4, '', '')

        with patch.dict(os.environ, {on_demand.WORKER_MARKER: '1'}), \
             patch.object(on_demand, 'managed_open', side_effect=fake_managed), \
             patch.object(on_demand.subprocess, 'Popen',
                          side_effect=AssertionError('nested supervisor')):
            result = supervised_open(None, 'notes', ['arg'])
        self.assertEqual(result.returncode, 4)
        self.assertEqual(seen, [(None, 'notes', ['arg'])])

    def test_spawn_failure_is_reported_as_a_product_error(self):
        with patch.object(on_demand.subprocess, 'Popen', side_effect=OSError('no python')):
            with self.assertRaisesRegex(UnifierError, 'supervised launcher'):
                supervised_open(None, 'notes', [])

    def test_open_app_routes_an_on_demand_record_to_the_supervisor(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            with patch.object(on_demand, 'supervised_open',
                              return_value=ExecResult(9, '', '')) as supervised:
                result = service.open_app('notes', ['payload'])
            self.assertEqual(result.returncode, 9)
            supervised.assert_called_once_with(service, 'notes', ['payload'])


class CommandLine(unittest.TestCase):
    def test_hidden_worker_parses_application_id_and_program_arguments(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            calls = []

            def fake_managed(service_arg, app_id, extra):
                calls.append((service_arg, app_id, extra))
                return ExecResult(3, '', '')

            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                with patch.object(on_demand, 'managed_open', side_effect=fake_managed):
                    code = cli.main(['app', '_worker', 'notes', '--', 'one', 'two'])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 3)
            self.assertEqual(calls, [(service, 'notes', ['one', 'two'])])

    def test_app_open_command_reaches_the_supervisor(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = ready(Path(temporary))
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                with patch.object(on_demand, 'supervised_open',
                                  return_value=ExecResult(0, '', '')) as supervised:
                    code = cli.main(['app', 'open', 'notes', '--', 'payload'])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 0)
            supervised.assert_called_once_with(service, 'notes', ['payload'])


def _real_stamp_factory(pool):
    real = process_stamp

    def stamp(pid):
        if pid in pool.alive:
            return 'stamp-' + str(pid)
        return real(pid)

    return stamp


class FakeProcess:
    def __init__(self, pid):
        self.pid = pid

    def poll(self):
        return None

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


class ProxyPool:
    """A fake xdg-dbus-proxy that really binds the AF_UNIX socket."""

    def __init__(self):
        self.commands = []
        self.alive = {}
        self.servers = []
        self.next_pid = 8100

    def __call__(self, command, **kwargs):
        self.commands.append((list(command), dict(kwargs)))
        self.next_pid += 1
        pid = self.next_pid
        server = socket.socket(socket.AF_UNIX)
        server.bind(str(Path(kwargs['cwd']) / command[2]))
        self.servers.append(server)
        self.alive[pid] = server
        return FakeProcess(pid)

    def kill(self, pid, _signal):
        self.alive.pop(pid, None)

    def close(self):
        for server in self.servers:
            server.close()


class BridgeLifecycle(unittest.TestCase):
    def test_live_proxy_is_reused_and_broken_one_is_rebuilt(self):
        with tempfile.TemporaryDirectory() as temporary:
            record = {'desktop': {'portal_proxy': {'directory': str(Path(temporary) / 'portal')}}}
            pool = ProxyPool()
            with patch.dict(os.environ, {
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/unused', 'XAUTHORITY': ''}), \
                 patch.object(on_demand.shutil, 'which', return_value='/fake/proxy'), \
                 patch.object(on_demand.subprocess, 'Popen', pool), \
                 patch.object(on_demand.os, 'kill', pool.kill), \
                 patch.object(on_demand, 'process_stamp', _real_stamp_factory(pool)):
                first = start_bridge(record)
                self.assertTrue(Path(first['socket']).is_socket())
                self.assertIs(start_bridge(record, first), first)
                self.assertEqual(len(pool.commands), 1)
                pool.alive.pop(first['pid'])
                second = start_bridge(record, first)
                self.assertNotEqual(second['pid'], first['pid'])
                self.assertEqual(len(pool.commands), 2)
                Path(second['socket']).unlink()
                third = start_bridge(record, second)
                self.assertNotEqual(third['pid'], second['pid'])
                self.assertEqual(len(pool.commands), 3)
                stop_bridge(third)
                self.assertFalse(Path(third['socket']).exists())
            pool.close()

    def test_last_release_stops_the_environment_and_removes_the_bridge(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, portals=True)
            group = LaunchGroup(service, 'debian13')
            pool = ProxyPool()
            with patch.dict(os.environ, {
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/unused', 'XAUTHORITY': ''}), \
                 patch.object(on_demand.shutil, 'which', return_value='/fake/proxy'), \
                 patch.object(on_demand.subprocess, 'Popen', pool), \
                 patch.object(on_demand.os, 'kill', pool.kill), \
                 patch.object(on_demand, 'process_stamp', _real_stamp_factory(pool)):
                lease = group.acquire('notes')
                state = group.read()
                self.assertIsNotNone(state['bridge'])
                socket_path = Path(state['bridge']['socket'])
                self.assertTrue(socket_path.is_socket())
                group.release(lease)
            state = group.read()
            self.assertIsNone(state['bridge'])
            self.assertFalse(socket_path.exists())
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')
            pool.close()

    def test_bridge_survives_while_another_lease_is_live(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = ready(root, portals=True)
            first = LaunchGroup(service, 'debian13')
            second = LaunchGroup(service, 'debian13')
            pool = ProxyPool()
            with patch.dict(os.environ, {
                    'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/unused', 'XAUTHORITY': ''}), \
                 patch.object(on_demand.shutil, 'which', return_value='/fake/proxy'), \
                 patch.object(on_demand.subprocess, 'Popen', pool), \
                 patch.object(on_demand.os, 'kill', pool.kill), \
                 patch.object(on_demand, 'process_stamp', _real_stamp_factory(pool)):
                lease_one = first.acquire('notes')
                lease_two = second.acquire('notes')
                self.assertEqual(len(pool.commands), 1)
                first.release(lease_one)
                self.assertIsNotNone(first.read()['bridge'])
                self.assertTrue(Path(first.read()['bridge']['socket']).is_socket())
                self.assertEqual(service.inspect('debian13')['runtime_state'], 'running')
                second.release(lease_two)
            self.assertIsNone(second.read()['bridge'])
            self.assertEqual(service.inspect('debian13')['runtime_state'], 'stopped')
            pool.close()


if __name__ == '__main__':
    unittest.main()
