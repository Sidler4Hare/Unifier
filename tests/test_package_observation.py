"""PRE02: package observation must not wake a stopped peer."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from test_packages import make_env, package_service


class PackageObservation(unittest.TestCase):
    def test_stopped_peer_is_not_started_or_executed_for_package_observation(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = package_service(Path(temporary))
            make_env(service, runtime, 'debian13', 'target')
            peer = make_env(service, runtime, 'fedora44', 'peer')
            runtime.read_inventory_readonly = None
            runtime.events.clear()
            txn = service.install_packages('target', ['hello'])
            forbidden = [event for event in runtime.events
                         if event[0] in ('start', 'stop', 'exec')
                         and event[1] == peer['runtime']['container_name']]
            self.assertEqual(forbidden, [], 'package observation woke a stopped peer')
            self.assertEqual(service.inspect('peer')['runtime_state'], 'stopped')
            # Without an independent fresh read, refusal must precede native writes.
            self.assertEqual(txn['phase'], 'failed')
            self.assertEqual(txn['native'], [])
            self.assertFalse(txn['verification']['ok'])
            self.assertIn('peer', str(txn['other_environments']))

    def fixture(self, running=False):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service, runtime = package_service(Path(temporary.name))
        make_env(service, runtime, 'debian13', 'target')
        peer = make_env(service, runtime, 'fedora44', 'peer')
        if running:
            service.start('peer')
        runtime.events.clear()
        return service, runtime, peer

    def assert_no_peer_execution(self, runtime, peer):
        self.assertEqual([event for event in runtime.events
                          if event[0] in ('start', 'stop', 'exec')
                          and event[1] == peer['runtime']['container_name']], [])

    def assert_refused_before_start(self, service, runtime, txn):
        self.assertEqual(txn['phase'], 'failed')
        self.assertEqual(txn['failure_phase'], 'isolation-preflight')
        self.assertEqual(txn['native'], [])
        self.assertFalse(txn['verification']['ok'])
        self.assertEqual([event for event in runtime.events
                          if event[0] in ('start', 'stop', 'exec')], [])
        self.assertEqual(service.inspect('target')['runtime_state'], 'stopped')
        self.assertTrue(service.store.transaction_path(txn['id']).is_file())

    def test_independent_snapshot_allows_install_without_peer_execution(self):
        service, runtime, peer = self.fixture()
        txn = service.install_packages('target', ['hello'])
        self.assertEqual(txn['phase'], 'completed')
        self.assertTrue(txn['other_environments']['peer']['unchanged'])
        self.assert_no_peer_execution(runtime, peer)
        self.assertEqual(service.inspect('peer')['runtime_state'], 'stopped')

    def test_snapshot_reader_failure_is_not_replaced_by_registered_hash(self):
        service, runtime, peer = self.fixture()
        runtime.read_inventory_readonly = Mock(side_effect=OSError('database unavailable'))
        txn = service.install_packages('target', ['hello'])
        self.assert_refused_before_start(service, runtime, txn)
        self.assertIn('database unavailable', txn['native_error'])
        self.assertIsNone(txn['other_environments']['peer']['probed_before'])
        self.assert_no_peer_execution(runtime, peer)

    def test_snapshot_without_the_observation_contract_is_rejected(self):
        service, runtime, _peer = self.fixture()
        runtime.read_inventory_readonly = Mock(return_value=b'base-peer\t1\n')
        txn = service.install_packages('target', ['hello'])
        self.assert_refused_before_start(service, runtime, txn)
        self.assertIn('observation contract', txn['native_error'])

    def test_mismatching_fresh_snapshot_refuses_before_native_writes(self):
        service, runtime, _peer = self.fixture()
        runtime.installed['peer'] = {'externally-added'}
        txn = service.install_packages('target', ['hello'])
        self.assert_refused_before_start(service, runtime, txn)
        self.assertIn('differs', txn['native_error'])

    def test_running_peer_without_reader_is_preserved_without_exec(self):
        service, runtime, peer = self.fixture(running=True)
        runtime.read_inventory_readonly = None
        txn = service.install_packages('target', ['hello'])
        self.assert_refused_before_start(service, runtime, txn)
        self.assert_no_peer_execution(runtime, peer)
        self.assertEqual(service.inspect('peer')['runtime_state'], 'running')

    def test_foreign_identity_is_rejected_before_reading_inventory(self):
        service, runtime, peer = self.fixture()
        name = peer['runtime']['container_name']
        runtime.containers[name] = replace(runtime.containers[name], project='/foreign')
        reader = runtime.read_inventory_readonly = Mock()
        txn = service.install_packages('target', ['hello'])
        self.assert_refused_before_start(service, runtime, txn)
        reader.assert_not_called()

    def test_peer_change_after_native_write_is_reported_by_final_verification(self):
        service, runtime, peer = self.fixture()
        reader = runtime.read_inventory_readonly
        calls = []
        def snapshot(record):
            calls.append(record['id'])
            if len(calls) == 2:
                runtime.installed['peer'] = {'changed-during-transaction'}
            return reader(record)
        runtime.read_inventory_readonly = snapshot
        txn = service.install_packages('target', ['hello'])
        self.assertEqual(txn['phase'], 'failed')
        self.assertEqual(txn['failure_phase'], 'verification')
        self.assertFalse(txn['other_environments']['peer']['unchanged'])
        self.assertTrue(txn['native'])
        self.assert_no_peer_execution(runtime, peer)


if __name__ == '__main__':
    unittest.main()
