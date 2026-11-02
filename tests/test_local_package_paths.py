"""Package filenames stay literal; package identifiers retain their own rules."""
from pathlib import Path
import tempfile
import unittest
from unifier.errors import UnifierError
from unifier.packages import driver_for, local_package_path, validate_package_name


class LocalPackagePaths(unittest.TestCase):
    def test_native_paths_keep_unicode_spaces_and_shell_characters(self):
        with tempfile.TemporaryDirectory() as temporary:
            for target, suffix in [('debian13', '.deb'), ('fedora44', '.rpm'),
                                   ('arch', '.pkg.tar.zst')]:
                with self.subTest(target=target):
                    path = Path(temporary) / ('中文 空格 $; jq' + suffix)
                    path.write_bytes(b'path-contract-fixture')
                    accepted = local_package_path(target, str(path))
                    self.assertEqual(accepted, path.resolve())
                    # Every native adapter receives an absolute path as one
                    # argument, never a shell command or split filename.
                    native = driver_for(target).local(str(accepted))
                    self.assertEqual(native[-1][0][-1], str(accepted))

    def test_control_characters_and_container_separator_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            for name in ['line\nbreak.deb', 'tab\tname.deb', 'host:guest.deb']:
                with self.subTest(name=name):
                    path = Path(temporary) / name
                    path.write_bytes(b'path-contract-fixture')
                    with self.assertRaisesRegex(UnifierError, 'file name is not accepted'):
                        local_package_path('debian13', str(path))

    def test_filename_permission_does_not_relax_package_name_or_format(self):
        with self.assertRaises(UnifierError):
            validate_package_name('jq; echo injected')
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / '中文 package.deb'
            path.write_bytes(b'path-contract-fixture')
            with self.assertRaisesRegex(UnifierError, 'not a arch package'):
                local_package_path('arch', str(path))
