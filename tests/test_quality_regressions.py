"""Failure/retry contracts using isolated state and fake runtimes only."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]

from unifier.project_sessions import ProjectSessions
from unifier.on_demand import LaunchGroup
from unifier.service import EnvironmentService
from test_cli_shortcuts import CONFIG, LOCK, SERVICE_KWARGS, ShortcutRuntime, context, run_ctx
from test_packages import StaticHost, package_service, make_env
from test_project_sessions import ready


class RecoveryFailures(unittest.TestCase):
    def test_package_observation_failure_preserves_running_target_and_success(self):
        for phase in ('host', 'other'):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as temporary:
                service, runtime = package_service(Path(temporary))
                record = make_env(service, runtime, 'debian13', 'deb')
                service.start('deb')
                before = list(runtime.events)
                target, name = ((service.host_observer, 'snapshot') if phase == 'host'
                                else (service, '_other_fingerprints'))
                with patch.object(target, name, side_effect=OSError('preflight failed')):
                    txn = service.install_packages('deb', ['hello'])
                self.assertEqual(txn['phase'], 'completed')
                self.assertTrue(txn['native'])
                self.assertEqual(txn['isolation_observation']['status'], 'unavailable')
                self.assertTrue(runtime.inspect(record['runtime']['container_name']).running)
                self.assertFalse(any(event[0] in ('start', 'stop') for event in runtime.events[len(before):]))

    def test_lease_release_failure_keeps_token_for_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, _routes, project, _root, document = ready(root)
            runtime.probe_status = 'gone'
            sessions = ProjectSessions(root, service)
            record = sessions.create_session(project['project_uuid'], argv=['/bin/true'],
                                             cwd=str(document.parent))
            group = LaunchGroup(service, 'arch')
            token = group.acquire_project(project['project_uuid'],
                                          expected_binding=project['environment_binding'])
            sid = record['session_uuid']
            sessions.store.update(sid, state='running', lease_token=token,
                                  worker_pid=999999, worker_stamp=1,
                                  guest_pid=123456, guest_start=99, guest_pgrp=123456,
                                  guest_sid=123456, guest_token='b' * 32, guest_state='alive')
            with patch.object(LaunchGroup, 'release', side_effect=OSError('journal write failed')):
                result = sessions.recover(sid)[0]
            self.assertEqual(result['action'], 'unresolved-lease-release')
            self.assertEqual(sessions.store.read(sid)['lease_token'], token)
            self.assertIn(token, group.read()['leases'])
            self.assertEqual(sessions.recover(sid)[0]['action'], 'recovered')
            self.assertIsNone(sessions.store.read(sid)['lease_token'])
            self.assertNotIn(token, group.read()['leases'])
            self.assertEqual(sessions.recover(sid)[0]['action'], 'already-settled')

    def test_adopted_failure_retry_preserves_original_environment(self):
        for command in ('init', 'configure'):
            with self.subTest(command=command), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime = ShortcutRuntime()
                runtime.identities['legacy'] = dict(runtime.identities['debian13'])
                legacy = root / 'legacy-root'
                service = EnvironmentService(legacy, runtime, CONFIG, LOCK,
                                             host_observer=StaticHost(), **SERVICE_KWARGS)
                old = service.create('debian13', 'legacy', [], 'pasta')
                old_bytes = service.store.path('legacy').read_bytes()
                ctx = context(root / 'new-root', runtime)
                result = run_ctx(ctx, ['adopt', '--target', 'debian13', '--root', str(legacy),
                                      '--environment', 'legacy', '--json'])
                self.assertEqual(result[0], 0, result)
                identity = ctx.state.load()['targets']['debian13']['identity']
                name = old['runtime']['container_name']
                container = runtime.containers.pop(name)
                argv = [command, '--target', 'debian13', '--yes', '--json']
                self.assertEqual(run_ctx(ctx, argv)[0], 1)
                self.assertEqual(ctx.state.load()['targets']['debian13']['state'], 'failed')
                runtime.containers[name] = container
                before = list(runtime.events)
                result = run_ctx(ctx, argv)
                self.assertEqual(result[0], 0, result)
                item = ctx.state.load()['targets']['debian13']
                self.assertEqual(item['source'], 'adopted')
                self.assertEqual(item['state'], 'adopted')
                self.assertIsNone(item['failure'])
                self.assertEqual(item['identity'], identity)
                self.assertEqual(item['source_root'], str(legacy))
                self.assertEqual(runtime.events, before)
                self.assertEqual(service.store.path('legacy').read_bytes(), old_bytes)
                self.assertFalse((ctx.root / '.lab/state/environments/legacy.json').exists())
