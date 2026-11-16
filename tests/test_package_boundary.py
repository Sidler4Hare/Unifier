"""Target identity and native failure remain mandatory despite passive audit failures."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_packages import package_service, make_env
from unifier.runtime import PodmanRuntime


class PackageBoundary(unittest.TestCase):
    def fixture(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service, runtime = package_service(Path(tmp.name))
        target = make_env(service, runtime, 'debian13', 'target')
        peer = make_env(service, runtime, 'arch', 'peer')
        runtime.events.clear()
        return service, runtime, target, peer

    def test_real_runtime_running_snapshot_capability_does_not_block_target(self):
        service, runtime, target, peer = self.fixture()
        service.start('peer')
        original = runtime.inspect(peer['runtime']['container_name'])
        adapter = PodmanRuntime(check_rootless=False)
        runtime.events.clear()
        with patch.object(adapter, '_require_owned', return_value=original):
            runtime.read_inventory_readonly = adapter.read_inventory_readonly
            result = service.install_packages('target', ['hello'])
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(result['isolation_observation']['status'], 'unavailable')
        self.assertIn('consistent snapshot', str(result['other_environments']))
        self.assertFalse(any(e[0] in ('exec', 'start', 'stop') and e[1] == original.name for e in runtime.events))

    def test_target_generation_changed_during_observation_refuses_native_write(self):
        service, runtime, target, peer = self.fixture()
        reader = runtime.read_inventory_readonly
        def changed(record):
            value = service.store.load('target')
            value['generation'] += 1
            service.store.save(value, None)
            return reader(record)
        runtime.read_inventory_readonly = changed
        result = service.install_packages('target', ['hello'])
        self.assertNotEqual(result['phase'], 'completed')
        self.assertEqual(result['native'], [])
        self.assertFalse(any(e[0] in ('exec', 'start') for e in runtime.events))

    def test_throwing_audit_does_not_create_recovery_barrier(self):
        service, runtime, target, peer = self.fixture()
        with patch.object(service, '_other_fingerprints', side_effect=OSError('peer offline')), \
             patch.object(service.host_observer, 'snapshot', side_effect=OSError('host observer failed')):
            result = service.install_packages('target', ['hello'])
        self.assertEqual(result['phase'], 'completed')
        self.assertEqual(result['isolation_observation']['status'], 'unavailable')
        self.assertFalse(result['recovery_required'])
