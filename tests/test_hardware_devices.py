"""Explicit render nodes work without importing a desktop session."""
from pathlib import Path
import os
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'tests')]
from unifier.errors import UnifierError
from unifier.cuda_device import CudaDevice, require_cuda_identity, resolve_cuda_device
from unifier.runtime import creation_argv
from test_env import service_in


class HeadlessDevices(unittest.TestCase):
    cuda = CudaDevice('GPU-711a11e2-5b33-9a0c-2c9d-4dbfa0ebbca9',
                      '0000:03:00.0', 0, '/usr/lib/x86_64-linux-gnu/libcuda.so.580.178.04')

    def test_cuda_records_uuid_and_attaches_only_one_card(self):
        with tempfile.TemporaryDirectory() as directory:
            service, runtime = service_in(Path(directory))
            with patch('unifier.cuda_device.resolve_cuda_device', return_value=self.cuda):
                record = service.create('debian13', None, [], cuda_device=self.cuda.uuid)
            spec = next(event[1] for event in runtime.events if event[0] == 'create')
        argv = creation_argv(spec)
        actual = [value for index, value in enumerate(argv) if index and argv[index-1] == '--device']
        self.assertEqual(actual, [f'{path}:{path}:rw' for path in self.cuda.nodes])
        self.assertEqual(record['runtime']['cuda'], self.cuda.identity())
        self.assertIsNone(record['desktop'])
        self.assertIn(self.cuda.library+':/run/unifier-cuda/libcuda.so.1:ro,nosuid,nodev', argv)

    def test_cuda_rejects_aliases_changed_identity_and_combined_requests(self):
        for selection in ('0', 'all', 'nvidia.com/gpu=all', 'MIG-711a11e2'):
            with self.subTest(selection=selection), self.assertRaisesRegex(UnifierError, 'full NVIDIA GPU UUID'):
                resolve_cuda_device(selection)
        with patch('unifier.cuda_device.resolve_cuda_device', return_value=self.cuda):
            with self.assertRaisesRegex(UnifierError, 'changed'):
                require_cuda_identity({**self.cuda.identity(), 'minor': 1})
        for extra in ({'desktop': True}, {'devices': ['/dev/dri/renderD128']},
                      {'ipc': 'host'}, {'user_namespace': 'keep-id'}):
            with self.subTest(extra=extra), tempfile.TemporaryDirectory() as directory:
                service, runtime = service_in(Path(directory))
                with self.assertRaisesRegex(UnifierError, 'CUDA requires a headless'):
                    service.create('debian13', None, [], cuda_device=self.cuda.uuid, **extra)
                self.assertFalse(any(event[0] == 'create' for event in runtime.events))

    def test_explicit_render_node_does_not_import_desktop_or_change_ipc(self):
        device = '/dev/dri/renderD999'
        real_stat = os.stat

        def device_stat(path, *args, **kwargs):
            if str(path) == device:
                return SimpleNamespace(st_mode=stat.S_IFCHR | 0o660)
            return real_stat(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as directory:
            service, runtime = service_in(Path(directory))
            with patch('unifier.service.os.stat', side_effect=device_stat), \
                 patch('unifier.service.discover_session', side_effect=AssertionError('desktop discovered')):
                record = service.create('debian13', None, [], devices=[device])
            spec = next(event[1] for event in runtime.events if event[0] == 'create')
            argv = creation_argv(spec)
            self.assertEqual(spec.devices, (device,))
            self.assertEqual(spec.session_mounts, ())
            self.assertEqual(spec.ipc, 'private')
            self.assertEqual(spec.selinux_label, 'default')
            self.assertIsNone(record['desktop'])
            self.assertEqual(record['runtime']['devices'], [device])
            self.assertIn(device + ':' + device + ':rw', argv)
            self.assertNotIn('--security-opt', argv)
            self.assertNotIn('--ipc=host', argv)
            plan = service.change_plan('debian13', {'devices': []})
            self.assertEqual(plan['action'], 'replace')
            self.assertIn('devices', plan['changes'])

    def test_camera_card_bulk_gpu_and_portals_still_require_desktop(self):
        for options in ({'devices': ['/dev/video999']},
                        {'devices': ['/dev/dri/card999']},
                        {'gpu': True}, {'portals': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                service, runtime = service_in(Path(directory))
                with self.assertRaisesRegex(UnifierError, '--desktop'):
                    service.create('debian13', None, [], **options)
                self.assertFalse(any(e[0] == 'create' for e in runtime.events))

    def test_missing_render_node_and_non_device_are_rejected_before_runtime(self):
        for device in ('/dev/dri/renderD999999999', '/dev/null',
                       '/dev/dri/renderD128/../card0', 'nvidia.com/gpu=all'):
            with self.subTest(device=device), tempfile.TemporaryDirectory() as directory:
                service, runtime = service_in(Path(directory))
                with self.assertRaises(UnifierError):
                    service.create('debian13', None, [], devices=[device])
                self.assertFalse(any(e[0] == 'create' for e in runtime.events))


if __name__ == '__main__':
    unittest.main()
