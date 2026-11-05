"""Static dependency evidence cannot stand in for actual loader outcomes."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import uuid

from test_dependencies import PluginBoxes, elf_record
from test_packages import make_env, package_service
from unifier.cli import _print_transaction
from unifier.dependencies import CACHE, SCAN, parse_scan
from unifier.errors import UnifierError
from unifier.loader_verification import fixture_argv, loading_result, verify_fixture_loading
from unifier.runtime import ExecResult


class BundledBoxes(PluginBoxes):
    runpath = ''
    def _answer(self, info, argv):
        if argv[:2] == ['dpkg-query', '-L']:
            return ExecResult(0, '/opt/vendor/main\n/opt/vendor/private/libufix.so.1\n', '')
        if argv[:3] == ['/bin/sh', '-c', SCAN]:
            output = elf_record('/opt/vendor/main', ['libufix.so.1'])
            if self.runpath:
                output += ' (RUNPATH) Library runpath: [%s]\n' % self.runpath
            output += elf_record('/opt/vendor/private/libufix.so.1', sonames=['libufix.so.1'])
            return ExecResult(0, output + 'UNIFIER_SCAN_DONE\n', '')
        if argv[:3] == ['/bin/sh', '-c', CACHE]:
            return ExecResult(0, '', '')
        return super()._answer(info, argv)


class DependencyEvidence(unittest.TestCase):
    def transaction(self, runtime):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        service, runtime = package_service(Path(temporary.name), runtime)
        make_env(service, runtime, 'debian13', 'deps')
        return service.install_packages('deps', ['vendor'], repair_dependencies=True)

    def test_library_exists_outside_search_path_is_only_an_unverified_candidate(self):
        txn = self.transaction(BundledBoxes())
        report = txn['dependency_repair']
        self.assertEqual(txn['phase'], 'completed')  # Native package transaction.
        self.assertEqual(report['status'], 'bundled-candidates-unverified')
        self.assertEqual(report['runtime_loading'], 'unverified')
        self.assertEqual(report['workflow'], 'unverified')
        edge = report['dependency_edges'][0]
        self.assertEqual(edge['bundled_candidates'], ['/opt/vendor/private/libufix.so.1'])
        self.assertEqual(edge['runpath'], [])
        self.assertEqual(edge['loader_resolution'], 'unverified')
        out, err = StringIO(), StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            self.assertEqual(_print_transaction(txn), 0)
        self.assertIn('runtime loading', err.getvalue())
        self.assertIn('unverified', err.getvalue())

    def test_origin_runpath_is_recorded_without_claiming_dynamic_resolution(self):
        runtime = BundledBoxes(); runtime.runpath = '$ORIGIN/private'
        report = self.transaction(runtime)['dependency_repair']
        obj = next(obj for obj in report['elf_objects'] if obj['path'] == '/opt/vendor/main')
        self.assertEqual((obj['runpath'], obj['origin'], obj['architecture']),
                         (['$ORIGIN/private'], '/opt/vendor', 'x86_64'))
        self.assertEqual(report['status'], 'bundled-candidates-unverified')
        self.assertEqual(report['evidence_scope']['symbol_versions'], 'unverified')
        self.assertEqual(report['evidence_scope']['runpath_transitivity'], 'unverified')

    def test_wrong_architecture_and_ambiguous_dynamic_tags_are_rejected(self):
        text = elf_record('/opt/main', ['libufix.so.1'])
        for output in (text.replace('ELF64', 'ELF32'),
                       text + ' (RPATH) Library rpath: [/a]\n (RPATH) Library rpath: [/b]\n'):
            with self.assertRaises(UnifierError):
                parse_scan(output + 'UNIFIER_SCAN_DONE\n')

    def test_real_adapter_loader_probe_refuses_root_or_unbound_fixture_before_exec(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        service, runtime = package_service(Path(temporary.name))
        record = make_env(service, runtime, 'debian13', 'deps')
        root = '/tmp/unifier-fixture-' + 'f'*32
        fixture = {'root': root, 'path': root+'/main', 'loader': '/lib64/ld-linux-x86-64.so.2',
                   'files': {root+'/main': 'a'*64}, 'environment_binding': {}}
        runtime.events.clear()
        with self.assertRaisesRegex(UnifierError, 'non-root'):
            verify_fixture_loading(service, 'deps', fixture, user='0')
        with self.assertRaisesRegex(UnifierError, 'binding changed'):
            verify_fixture_loading(service, 'deps', fixture, user='1000')
        self.assertEqual(runtime.events, [])

    def test_zero_exit_with_unknown_diagnostics_is_never_a_loader_pass(self):
        root = '/tmp/unifier-fixture-' + 'f'*32
        fixture = {'path': root+'/main', 'loader': '/lib64/ld-linux-x86-64.so.2',
                   'files': {root+'/main': 'a'*64}}
        native = ExecResult(0, '\nUNIFIER_LOADER_EXIT 0\n', 'unexpected diagnostic\n')
        result = loading_result(fixture, native)
        self.assertEqual(result['status'], 'unverified')
        self.assertEqual(result['native_exit_code'], 0)
        self.assertEqual(result['reason'], 'loader diagnostics require review')


class NativeLoaderFixtures(unittest.TestCase):
    """Trusted C fixtures compiled and resolved by the host's own loader.

    This proves loader-versus-name behavior only. It is T1 local mechanism
    evidence, not a Debian/Arch container or installed-application acceptance.
    """
    @classmethod
    def setUpClass(cls):
        if not shutil.which('gcc') or not Path('/lib64/ld-linux-x86-64.so.2').exists():
            raise unittest.SkipTest('native x86_64 fixture toolchain unavailable')
        if os.geteuid() == 0:
            raise unittest.SkipTest('fixture requires an ordinary user')

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)/('unifier-fixture-'+uuid.uuid4().hex)
        self.root.mkdir(mode=0o700); (self.root/'lib').mkdir()
        self.library = self.root/'lib/libufix.so.1'
        self.program = self.root/'main'
        (self.root/'lib.c').write_text('int fixture(void){return 42;}\n')
        (self.root/'main.c').write_text('extern int fixture(void);int main(void){return fixture()!=42;}\n')
        self.gcc('-shared', '-fPIC', '-Wl,-soname,libufix.so.1', '-o', self.library, self.root/'lib.c')

    def gcc(self, *args):
        result = subprocess.run(['gcc', *map(str, args)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)

    def build_main(self, *, runpath=None):
        flags = ['-Wl,-rpath,'+runpath] if runpath is not None else []
        self.gcc('-o', self.program, self.root/'main.c', '-L'+str(self.root/'lib'),
                 '-l:libufix.so.1', *flags)

    def fixture(self, files=None):
        paths = files or [self.program, self.library]
        return {'root': str(self.root), 'path': str(self.program),
                'loader': '/lib64/ld-linux-x86-64.so.2',
                'files': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}}

    def load(self, fixture=None):
        fixture = fixture or self.fixture()
        native = subprocess.run(fixture_argv(fixture, str(os.geteuid())),
            capture_output=True, text=True, timeout=10,
            env={**os.environ, 'LD_LIBRARY_PATH': str(self.root/'lib')})
        return loading_result(fixture, native)

    def test_present_but_unsearchable_library_fails_and_origin_runpath_passes(self):
        self.build_main()
        self.assertEqual(self.load()['status'], 'verified-fail')
        self.build_main(runpath='$ORIGIN/lib')
        result = self.load()
        self.assertEqual(result['status'], 'verified-pass', result)
        self.assertEqual(result['workflow'], 'unverified')
        self.assertFalse(result['program_executed'])

    def test_wrong_architecture_same_soname_is_a_real_loader_failure(self):
        self.build_main(runpath='$ORIGIN/lib')
        content = bytearray(self.library.read_bytes()); content[4] = 1  # ELFCLASS32 fixture.
        self.library.write_bytes(content)
        result = self.load()
        self.assertEqual(result['status'], 'verified-fail', result)
        self.assertIn('ELFCLASS32', result['stderr_tail'])

    def test_manifest_change_refuses_before_loader_and_is_not_a_verified_failure(self):
        self.build_main(runpath='$ORIGIN/lib'); fixture = self.fixture()
        self.library.write_bytes(self.library.read_bytes() + b'changed')
        result = self.load(fixture)
        self.assertEqual(result['status'], 'unverified')
        self.assertIn('fixture safety', result['reason'])

    def test_missing_symbol_version_is_distinct_from_a_matching_soname(self):
        version = self.root/'versions.map'
        version.write_text('UFIX_1 { global: fixture; };\n')
        self.gcc('-shared', '-fPIC', '-Wl,-soname,libufix.so.1',
                 '-Wl,--version-script='+str(version), '-o', self.library, self.root/'lib.c')
        self.build_main(runpath='$ORIGIN/lib')
        version.write_text('UFIX_2 { global: fixture; };\n')
        self.gcc('-shared', '-fPIC', '-Wl,-soname,libufix.so.1',
                 '-Wl,--version-script='+str(version), '-o', self.library, self.root/'lib.c')
        result = self.load()
        self.assertEqual(result['status'], 'verified-fail', result)
        self.assertIn('UFIX_1', result['stderr_tail'])
        self.assertEqual(result['reason'], 'required symbol version unavailable')

    def test_main_runpath_does_not_prove_transitive_dependency_resolution(self):
        nested = self.root/'lib/nested'; nested.mkdir()
        (self.root/'b.c').write_text('int nested(void){return 42;}\n')
        (self.root/'lib.c').write_text('extern int nested(void);int fixture(void){return nested();}\n')
        library_b = nested/'libunested.so.1'
        self.gcc('-shared', '-fPIC', '-Wl,-soname,libunested.so.1', '-o', library_b, self.root/'b.c')
        self.gcc('-shared', '-fPIC', '-Wl,-soname,libufix.so.1', '-o', self.library,
                 self.root/'lib.c', '-L'+str(nested), '-l:libunested.so.1')
        self.gcc('-o', self.program, self.root/'main.c', '-L'+str(self.root/'lib'),
                 '-l:libufix.so.1', '-Wl,-rpath-link,'+str(nested),
                 '-Wl,-rpath,$ORIGIN/lib:$ORIGIN/lib/nested')
        result = self.load(self.fixture([self.program, self.library, library_b]))
        self.assertEqual(result['status'], 'verified-fail', result)
        self.assertIn('libunested.so.1', result['stdout_tail'] + result['stderr_tail'])

    def test_initial_loader_pass_does_not_validate_dlopen_plugin(self):
        (self.root/'plugin.c').write_text('#include <dlfcn.h>\nint main(void){return dlopen("libufix_missing.so.1",RTLD_NOW)?0:3;}\n')
        self.gcc('-o', self.program, self.root/'plugin.c', '-ldl')
        result = self.load(self.fixture([self.program]))
        self.assertEqual(result['status'], 'verified-pass', result)
        self.assertEqual(result['dlopen'], 'unverified')
        native = subprocess.run([str(self.program)], capture_output=True, timeout=5)
        self.assertEqual(native.returncode, 3)


if __name__ == '__main__':
    unittest.main()
