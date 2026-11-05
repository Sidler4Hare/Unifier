"""The writer queue covers peer observation through durable final receipts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from test_packages import PackageBoxes, make_env, package_service
from unifier.errors import UnifierError
from unifier.operation_lock import management_lock
from unifier.identity import binding_for
from unifier.on_demand import LaunchGroup
from unifier.runtime import ExecResult


class PackageCoordination(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service, runtime = package_service(Path(temporary.name), PackageBoxes())
        make_env(service, runtime, 'debian13', 'a')
        make_env(service, runtime, 'fedora44', 'b')
        runtime.events.clear()
        return service, runtime

    def test_two_environment_writers_wait_until_final_receipt_and_both_complete(self):
        service, runtime = self.fixture()
        first_window = threading.Event()
        release = threading.Event()
        second_submitted = threading.Event()
        second_native = threading.Event()
        first_persisted = threading.Event()
        original_save = service.store.save_transaction
        original_exec = runtime.exec
        errors = []
        def save(txn):
            if txn['environment_id'] == 'a' and txn['finished_at']:
                first_window.set()
                if not release.wait(5):
                    raise AssertionError('test barrier deadline')
                original_save(txn)
                first_persisted.set()
            else:
                original_save(txn)
        def execute(name, argv, **kwargs):
            if kwargs['env_id'] == 'b' and argv[0] == 'dnf':
                second_native.set()
                if not first_persisted.is_set():
                    errors.append('second writer entered before durable first receipt')
            return original_exec(name, argv, **kwargs)
        def second():
            second_submitted.set()
            return service.install_packages('b', ['probe-b'])
        service.store.save_transaction = save
        runtime.exec = execute
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(service.install_packages, 'a', ['probe-a'])
            try:
                self.assertTrue(first_window.wait(5))
                queued = pool.submit(second)
                self.assertTrue(second_submitted.wait(1))
                self.assertFalse(second_native.wait(.15))
            finally:
                release.set()
            transactions = [first.result(timeout=5), queued.result(timeout=5)]
        self.assertEqual(errors, [])
        self.assertTrue(second_native.is_set())
        self.assertEqual([t['phase'] for t in transactions], ['completed', 'completed'])
        self.assertTrue(all(all(p['unchanged'] for p in t['other_environments'].values())
                            for t in transactions))

    def test_queue_timeout_has_no_start_or_native_write_for_any_package_entry(self):
        service, runtime = self.fixture()
        original = service.package_coordination_lock
        service.package_coordination_lock = lambda **kwargs: original(timeout=.01)
        actions = [
            lambda: service.install_packages('a', ['hello']),
            lambda: service.update_packages('a'),
            lambda: service.remove_packages('a', ['hello']),
            lambda: service.install_local('a', '/absent/probe.deb'),
            lambda: service.repair_dependencies('a', ['hello']),
            lambda: service.recover_packages('a'),
            lambda: service.remove('b'),
            lambda: service.create('arch', 'c', [], 'pasta'),
        ]
        with management_lock(service.root):
            for action in actions:
                with self.assertRaisesRegex(UnifierError, 'COORDINATION_TIMEOUT'):
                    action()
        self.assertEqual(runtime.events, [])
        self.assertEqual(service.transactions_for('a'), [])
        self.assertTrue(service.store.exists('b'))

    def test_app_start_stop_does_not_wait_for_another_environment_window(self):
        service, runtime = self.fixture()
        with management_lock(service.root):
            service.start('a')
            service.stop('a')
        self.assertEqual([e[0] for e in runtime.events if e[0] in {'start', 'stop'}],
                         ['start', 'stop'])

    def test_stop_refuses_package_writer_even_without_management_lock(self):
        service, runtime = self.fixture()
        with service.package_operation_lock('a'):
            with self.assertRaisesRegex(UnifierError, 'transaction is in progress'):
                service.stop('a')
        self.assertEqual(runtime.events, [])

    def test_crash_released_queue_does_not_bypass_pending_recovery(self):
        service, runtime = self.fixture()
        record = service.store.load('a')
        record['package_recovery_required'] = True
        service.store.save(record, None)
        with self.assertRaisesRegex(UnifierError, 'recover'):
            service.install_packages('a', ['hello'])
        self.assertEqual(runtime.events, [])
        self.assertTrue(service.store.load('a')['package_recovery_required'])

    def test_external_peer_collection_or_identity_change_remains_a_failure(self):
        for change in ('remove', 'add', 'generation'):
            with self.subTest(change=change):
                service, runtime = self.fixture()
                original = service._finish_isolation
                def finish(txn, before, host, env_id):
                    if change == 'remove':
                        service.store.delete('b')
                    elif change == 'add':
                        record = service.store.load('b')
                        record['id'] = 'external'
                        service.store.save(record, None)
                    else:
                        record = service.store.load('b')
                        record['generation'] += 1
                        service.store.save(record, None)
                    original(txn, before, host, env_id)
                with patch.object(service, '_finish_isolation', finish):
                    txn = service.install_packages('a', ['hello'])
                self.assertEqual(txn['phase'], 'failed')
                self.assertEqual(txn['failure_phase'], 'verification')
                self.assertTrue(txn['verification']['isolation'])

    def test_install_repair_and_recovery_do_not_deadlock_app_and_project_lease_release(self):
        for operation in ('install', 'repair', 'recover'):
            with self.subTest(operation=operation):
                service, runtime = self.fixture()
                answer = runtime._answer
                runtime._answer = lambda info, argv: (
                    ExecResult(0, '', '') if argv == ['test', '-x', '/usr/bin/true']
                    else answer(info, argv))
                if operation == 'repair':
                    self.assertEqual(service.install_packages('a', ['hello'])['phase'], 'completed')
                service.add_app('a', 'notes', '/usr/bin/true', ['/usr/bin/true'],
                                desktop_required=False, on_demand=True)
                group = LaunchGroup(service, 'a')
                app = group.acquire('notes')
                project = group.acquire_project('probe-project',
                    expected_binding=binding_for(service.store.load('a')))
                entered, release = threading.Event(), threading.Event()
                if operation == 'recover':
                    record = service.store.load('a')
                    record['package_recovery_required'] = True
                    service.store.save(record, None)
                    method = '_audit_package_database'
                    action = lambda: service.recover_packages('a')
                else:
                    method = '_package_transaction_locked'
                    action = (lambda: service.install_packages('a', ['hello'])) if operation == 'install' else (
                        lambda: service.repair_dependencies('a', ['hello']))
                original = getattr(service, method)
                def paused(*args, **kwargs):
                    entered.set()
                    if not release.wait(5):
                        raise AssertionError('lease/writer barrier deadline')
                    return original(*args, **kwargs)
                with patch.object(service, method, paused), ThreadPoolExecutor(max_workers=1) as pool:
                    writer = pool.submit(action)
                    try:
                        self.assertTrue(entered.wait(5))
                        group.release(app)
                        # The last project lease must fail promptly rather than
                        # hold lease -> wait for package -> reverse into lease.
                        with self.assertRaisesRegex(UnifierError, 'transaction is in progress'):
                            group.release(project)
                        self.assertTrue(service._require_owned(service.store.load('a')).running)
                    finally:
                        release.set()
                    result = writer.result(timeout=5)
                self.assertEqual(result.get('status') if operation == 'recover' else result['phase'],
                                 'recovered' if operation == 'recover' else 'completed')
                # Auto-stop ownership was retained and can settle after the
                # writer releases its locks; no user-started instance is taken.
                token = group.acquire('notes')
                group.release(token)
                self.assertFalse(service._require_owned(service.store.load('a')).running)


if __name__ == '__main__':
    unittest.main()
