"""Restore only absent private session aliases; never overwrite auth data."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from multilinux.desktop import restore_xauthority_mount
from multilinux.errors import MultilinuxError
from test_env import service_in


class SessionMount(unittest.TestCase):
    def fixture(self, root):
        root.chmod(0o700)
        source = root / 'xauth_Current'
        source.write_bytes(b'fixture-cookie')
        source.chmod(0o600)
        old = root / 'xauth_Previous'
        record = {'desktop': {'mounts': [{'host': str(old), 'container': '/tmp/ml-xauthority'}]}}
        options = {'uid': os.getuid(), 'environ': {'XAUTHORITY': str(source)}, 'runtime_directory': root}
        return record, source, old, options

    def test_missing_legacy_path_is_restored_without_copying_or_modifying_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            record, source, old, options = self.fixture(Path(temporary))
            baseline = copy.deepcopy(record)
            self.assertEqual(restore_xauthority_mount(record, **options)['status'], 'restored-session-alias')
            self.assertTrue(old.is_symlink())
            self.assertEqual(os.readlink(old), source.name)
            self.assertEqual(old.read_bytes(), b'fixture-cookie')
            self.assertEqual(source.read_bytes(), b'fixture-cookie')
            self.assertEqual(record, baseline)
            self.assertEqual(restore_xauthority_mount(record, **options)['status'], 'current-session-alias')

    def test_existing_regular_file_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            record, source, old, options = self.fixture(Path(temporary))
            old.write_bytes(b'original')
            old.chmod(0o600)
            restore_xauthority_mount(record, **options)
            self.assertEqual(old.read_bytes(), b'original')
            self.assertFalse(old.is_symlink())

    def test_foreign_symlink_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            record, source, old, options = self.fixture(Path(temporary))
            old.symlink_to('/etc/passwd')
            with self.assertRaisesRegex(MultilinuxError, 'refusing to overwrite'):
                restore_xauthority_mount(record, **options)
            self.assertEqual(os.readlink(old), '/etc/passwd')

    def test_bad_current_file_or_runtime_permissions_are_refused(self):
        for scenario in ['missing', 'symlink', 'public-file', 'public-directory', 'outside-directory']:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                record, source, old, options = self.fixture(root)
                if scenario == 'missing':
                    source.unlink()
                elif scenario == 'symlink':
                    source.unlink(); source.symlink_to('/etc/passwd')
                elif scenario == 'public-file':
                    source.chmod(0o644)
                elif scenario == 'public-directory':
                    root.chmod(0o755)
                else:
                    options['environ']['XAUTHORITY'] = '/other/xauth_Current'
                with self.assertRaises(MultilinuxError):
                    restore_xauthority_mount(record, **options)
                self.assertFalse(old.exists() or old.is_symlink())

    def test_headless_and_other_mounts_create_no_alias(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(restore_xauthority_mount({}, uid=os.getuid()))
            record = {'desktop': {'mounts': [{'host': str(root / 'other'), 'container': '/tmp/ml-xauthority'}]}}
            self.assertIsNone(restore_xauthority_mount(record, uid=os.getuid(), runtime_directory=root))
            self.assertEqual(list(root.iterdir()), [])

    def test_refresh_runs_under_start_gate_before_runtime_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.create('debian13', None, [])
            before = list(runtime.events)
            with patch('multilinux.service.restore_xauthority_mount', side_effect=MultilinuxError('session unavailable')):
                with self.assertRaisesRegex(MultilinuxError, 'session unavailable'):
                    service.start('debian13')
            self.assertEqual(runtime.events, before)


if __name__ == '__main__':
    unittest.main()
