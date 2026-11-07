"""Loader errors supply bounded hints, unrelated failures never become installs."""
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.dependency_diagnostics import MAX_BYTES, parse_diagnostics, read_diagnostics
from unifier.errors import UnifierError


def missing(name='libasound.so.2'):
    return name + ': cannot open shared object file: No such file or directory'


class DiagnosticParsing(unittest.TestCase):
    def test_loader_dlopen_color_and_duplicates(self):
        text = ('vendor: error while loading shared libraries: ' + missing() + '\n'
                'Cannot load library /opt/plugin.so: (' + missing('libcodec.so.1') + ')\n'
                '\x1b[31m' + missing() + '\x1b[0m\n')
        report = parse_diagnostics(text)
        self.assertEqual(report['libraries'], ['libasound.so.2', 'libcodec.so.1'])
        self.assertEqual(len(report['issues']), 2)
        self.assertEqual(report['source']['sha256'], hashlib.sha256(text.encode()).hexdigest())

    def test_paths_shell_characters_and_unrelated_names_never_become_hints(self):
        for name in ('/tmp/libevil.so.1', '../libevil.so.1', 'dir/libevil.so.1',
                     '$libevil.so.1', 'libevil.so.1;touch /tmp/x', '-libevil.so.1'):
            with self.subTest(name=name):
                self.assertEqual(parse_diagnostics(missing(name))['libraries'], [])
        for text in ('loading libasound.so.2 succeeded',
                     'libasound.so.2: cannot open shared object file: Permission denied',
                     'libasound.so.2: cannot open shared object file: No such file or directoryWRONG'):
            with self.subTest(text=text):
                self.assertEqual(parse_diagnostics(text)['libraries'], [])

    def test_failure_categories_and_mixed_errors_remain_separate(self):
        examples = {"version `GLIBCXX_3.4.99' not found": 'abi-mismatch',
                    'vendor: undefined symbol: missing_symbol': 'abi-mismatch',
                    'vendor: wrong ELF class: ELFCLASS32': 'abi-mismatch',
                    'open: Permission denied': 'permission-denied',
                    '/bin/sh: 1: gio: not found': 'missing-command',
                    'gio: command not found': 'missing-command',
                    'Could not find the Qt platform plugin "xcb"': 'plugin-unresolved'}
        for text, kind in examples.items():
            with self.subTest(text=text):
                report = parse_diagnostics(text)
                self.assertEqual(report['libraries'], [])
                self.assertIn({'kind': kind}, report['issues'])
                self.assertIn({'kind': kind}, parse_diagnostics(missing()+'\n'+text)['issues'])

    def test_binary_controls_and_size_or_count_limits_reject(self):
        for text in ('\0', '\x1b[2J', '\x7f', '\x85', 'x'*(MAX_BYTES+1),
                     '\n'.join(missing('lib%d.so.1' % index) for index in range(33))):
            with self.subTest(length=len(text)), self.assertRaises(UnifierError):
                parse_diagnostics(text)
        self.assertEqual(len(parse_diagnostics('\n'.join(
            missing('lib%d.so.1' % index) for index in range(32)))['libraries']), 32)

    def test_only_structured_data_is_returned(self):
        report = parse_diagnostics('credential=secret-private-value\n' + missing())
        self.assertNotIn('secret-private-value', str(report))


class DiagnosticFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.log = self.root/'run.log'
        self.log.write_text(missing())

    def test_regular_utf8_file_matches_text_parser(self):
        self.assertEqual(read_diagnostics(self.log), parse_diagnostics(missing()))

    def test_final_symlink_fifo_directory_invalid_utf8_and_oversize_refuse(self):
        link = self.root/'link'; link.symlink_to(self.log)
        fifo = self.root/'fifo'; os.mkfifo(fifo)
        invalid = self.root/'invalid'; invalid.write_bytes(b'\xff')
        large = self.root/'large'
        with large.open('wb') as stream:
            stream.truncate(MAX_BYTES+1)
        for path in (link, fifo, self.root, invalid, large, self.root/'absent'):
            with self.subTest(path=path), self.assertRaises(UnifierError):
                read_diagnostics(path)

    def test_concurrent_file_change_is_rejected(self):
        original = os.read
        changed = False

        def change_after_read(descriptor, size):
            nonlocal changed
            result = original(descriptor, size)
            if not changed:
                changed = True
                self.log.write_text(missing('libchanged.so.1'))
            return result

        with patch('unifier.dependency_diagnostics.os.read', side_effect=change_after_read):
            with self.assertRaisesRegex(UnifierError, 'changed while reading'):
                read_diagnostics(self.log)


if __name__ == '__main__':
    unittest.main()
