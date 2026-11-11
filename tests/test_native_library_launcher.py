"""Bounded metadata, private alias cache and exec contracts; no real GTK."""

import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from unifier import native_library_launcher as launcher


def elf_fixture():
    data = bytearray(512)
    data[:7] = b'\x7fELF\x02\x01\x01'
    struct.pack_into('<HHI', data, 16, 3, 62, 1)
    struct.pack_into('<Q', data, 32, 64)
    struct.pack_into('<HHH', data, 52, 64, 56, 2)
    struct.pack_into('<IIQQQQQQ', data, 64, 1, 4, 0, 0, 0, 512, 512, 4096)
    struct.pack_into('<IIQQQQQQ', data, 120, 2, 4, 256, 256, 256, 64, 64, 8)
    value = b'\0libgtk-3.so.0\0'
    struct.pack_into('<qQqQqQqQ', data, 256, 5, 384, 10, len(value), 14, 1, 0, 0)
    data[384:384+len(value)] = value
    return data


class NativeLibraryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.target = self.root / 'libgtk-3.so.0'
        self.target.write_bytes(elf_fixture())
        self.cache = self.root / 'cache'
        self.cache.mkdir()

    def environment(self, env=None):
        return launcher.alias_environment('gtk3-native', env or {},
                                          resolver=lambda _: self.target, cache_root=self.cache)

    def test_elf_soname_is_read_without_loading(self):
        with patch('ctypes.CDLL', side_effect=AssertionError('no load')):
            self.assertEqual(launcher.elf_soname(self.target), 'libgtk-3.so.0')

    def test_malformed_elf_is_refused(self):
        mutations = [
            lambda d: d[:30],
            lambda d: d[:16] + struct.pack('<HH', 3, 183) + d[20:],
            lambda d: d[:64],
        ]
        for transform in mutations:
            with self.subTest(transform=transform):
                self.target.write_bytes(transform(elf_fixture()))
                with self.assertRaises(ValueError):
                    launcher.elf_soname(self.target)
        for offset, value in [(264, 9999), (280, 17*1024*1024), (296, 9999)]:
            with self.subTest(offset=offset):
                data = elf_fixture()
                struct.pack_into('<Q', data, offset, value)
                self.target.write_bytes(data)
                with self.assertRaises(ValueError):
                    launcher.elf_soname(self.target)

    def test_alias_is_private_reused_and_preserves_environment(self):
        source = {'LD_LIBRARY_PATH': '/existing/lib', 'HOME': '/guest/home', 'GTK_USE_PORTAL': '1'}
        result = self.environment(source)
        self.assertEqual(source['LD_LIBRARY_PATH'], '/existing/lib')
        directory, rest = result['LD_LIBRARY_PATH'].split(':', 1)
        self.assertEqual(rest, source['LD_LIBRARY_PATH'])
        self.assertEqual(result['HOME'], '/guest/home')
        alias = Path(directory) / 'libgtk-3.so'
        self.assertEqual(os.readlink(alias), str(self.target))
        self.assertEqual(Path(directory).stat().st_mode & 0o777, 0o700)
        before = alias.lstat()
        self.assertEqual(self.environment(source), result)
        self.assertEqual(alias.lstat().st_ino, before.st_ino)
        self.assertEqual(len(list(Path(directory).parent.iterdir())), 1)

    def test_unknown_rule_and_loader_failure_create_no_alias(self):
        with self.assertRaisesRegex(ValueError, 'unknown'):
            launcher.alias_environment('arbitrary-library', {}, cache_root=self.cache)
        def fail(_):
            raise OSError('missing required GTK symbols')
        with self.assertRaises(OSError):
            launcher.alias_environment('gtk3-native', {}, resolver=fail, cache_root=self.cache)
        self.assertEqual(list(self.cache.iterdir()), [])

    def test_private_root_symlink_does_not_write_outside(self):
        outside = self.root / 'outside'
        outside.mkdir()
        (self.cache / f'unifier-native-{os.getuid()}').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(OSError):
            self.environment()
        self.assertEqual(list(outside.iterdir()), [])

    def test_wrong_permissions_and_alias_collision_are_refused(self):
        env = self.environment()
        directory = Path(env['LD_LIBRARY_PATH'])
        alias = directory / 'libgtk-3.so'
        original = self.target.read_bytes()
        alias.unlink()
        alias.symlink_to('/unrelated/library')
        with self.assertRaisesRegex(ValueError, 'does not match'):
            self.environment()
        self.assertEqual(os.readlink(alias), '/unrelated/library')
        self.assertEqual(self.target.read_bytes(), original)
        alias.unlink()
        alias.symlink_to(self.target)
        directory.chmod(0o755)
        with self.assertRaisesRegex(ValueError, '0700'):
            self.environment()

    def test_library_identity_change_uses_new_cache_without_deleting_old(self):
        first = Path(self.environment()['LD_LIBRARY_PATH'])
        self.target.write_bytes(elf_fixture() + b'new-version')
        second = Path(self.environment()['LD_LIBRARY_PATH'])
        self.assertNotEqual(first, second)
        self.assertTrue((first / 'libgtk-3.so').is_symlink())
        self.assertTrue((second / 'libgtk-3.so').is_symlink())

    def test_cache_path_cannot_inject_loader_search_entries(self):
        bad = self.root / 'cache:other'
        bad.mkdir()
        with self.assertRaisesRegex(ValueError, 'unsafe'):
            launcher.alias_environment('gtk3-native', {}, resolver=lambda _: self.target, cache_root=bad)
        self.assertFalse(list(bad.iterdir()))

    def test_main_preserves_argv_and_exec_environment(self):
        args = ['gtk3-native', '--', '/opt/app/start', '中文 空格', ';$(false)']
        env = {'GTK_USE_PORTAL': '1', 'HOME': '/guest'}
        prepared = {**env, 'LD_LIBRARY_PATH': '/private'}
        with (patch.dict(os.environ, env, clear=True),
              patch.object(launcher, 'alias_environment', return_value=prepared) as prepare,
              patch('os.execvpe') as execute):
            launcher.main(args)
            self.assertEqual(dict(prepare.call_args.args[1]), env)
            execute.assert_called_once_with('/opt/app/start', args[2:], prepared)

    def test_main_refuses_errors_without_executing_application(self):
        with (patch.dict(os.environ, {}, clear=True), patch('os.execvpe') as execute,
              patch('sys.stderr')):
            self.assertEqual(launcher.main(['gtk3-native', '--', 'app']), 126)
            execute.assert_not_called()
        with (patch.dict(os.environ, {'GTK_USE_PORTAL': '1'}, clear=True),
              patch.object(launcher, 'alias_environment', side_effect=ValueError('bad ABI')),
              patch('os.execvpe') as execute, patch('sys.stderr')):
            self.assertEqual(launcher.main(['gtk3-native', '--', 'app']), 126)
            execute.assert_not_called()
