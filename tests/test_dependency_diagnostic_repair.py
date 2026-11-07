"""Saved runtime errors enter the existing native transaction, never a shell."""
from contextlib import redirect_stderr, redirect_stdout
import hashlib
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_dependencies import PluginBoxes
from test_packages import make_env, package_service
from unifier import cli
from unifier.errors import UnifierError


MISSING = ('vendor: error while loading shared libraries: libasound.so.2: '
           'cannot open shared object file: No such file or directory\n')


class DiagnosticRepair(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service, self.runtime = package_service(self.root, PluginBoxes())
        make_env(self.service, self.runtime, 'debian13', 'deps')
        self.runtime.events.clear()
        self.log = self.root / 'runtime.log'
        self.log.write_text(MISSING)

    def test_log_supplies_the_provider_hint_and_preserves_only_structured_evidence(self):
        self.log.write_text('credential=not-for-the-transaction\n' + MISSING)
        transaction = self.service.repair_dependencies('deps', [], diagnostics_path=self.log)
        self.assertEqual(transaction['phase'], 'completed', transaction)
        repair = transaction['dependency_repair']
        self.assertEqual(repair['installed'], ['libasound2t64'])
        self.assertEqual(repair['unresolved'], [])
        self.assertEqual(repair['runtime_loading'], 'unverified')
        saved = self.service.show_transaction(transaction['id'])
        self.assertEqual(saved['dependency_input']['libraries'], ['libasound.so.2'])
        self.assertEqual(saved['dependency_input']['source']['sha256'],
                         hashlib.sha256(self.log.read_bytes()).hexdigest())
        self.assertNotIn('not-for-the-transaction', json.dumps(saved))
        self.assertNotIn(str(self.log), json.dumps(saved))

    def test_persistent_provenance_exists_before_the_first_native_write(self):
        original = self.service._run_native
        seen = []

        def checked(record, transaction, steps):
            saved = self.service.show_transaction(transaction['id'])
            seen.append(saved['dependency_input']['libraries'])
            return original(record, transaction, steps)

        with patch.object(self.service, '_run_native', side_effect=checked):
            transaction = self.service.repair_dependencies(
                'deps', [], libraries=['libasound.so.2'], diagnostics_path=self.log)
        self.assertEqual(transaction['phase'], 'completed', transaction)
        self.assertTrue(seen)
        self.assertTrue(all(libraries == ['libasound.so.2'] for libraries in seen))

    def test_abi_mixed_or_unrecognized_log_is_rejected_before_runtime_access(self):
        for text in ("vendor: undefined symbol: unsupported_symbol\n",
                     MISSING + "vendor: version `GLIBC_9.99' not found\n",
                     'nothing to diagnose\n'):
            with self.subTest(text=text):
                self.log.write_text(text)
                before = self.service.transactions_for('deps')
                with self.assertRaises(UnifierError):
                    self.service.repair_dependencies('deps', [], diagnostics_path=self.log)
                self.assertEqual(self.runtime.events, [])
                self.assertEqual(self.service.transactions_for('deps'), before)

    def test_ambiguous_repository_and_ineffective_install_still_fail(self):
        for providers, effective in (('first-provider\nsecond-provider\n', True),
                                     ('libasound2t64\n', False)):
            with self.subTest(providers=providers, effective=effective):
                self.runtime.providers = providers
                self.runtime.repair_effective = effective
                transaction = self.service.repair_dependencies('deps', [], diagnostics_path=self.log)
                self.assertEqual(transaction['failure_phase'], 'dependency-repair', transaction)
                self.assertEqual(transaction['dependency_repair']['status'], 'unresolved')

    def test_diagnosis_only_has_no_service_or_environment_side_effects(self):
        output, errors = StringIO(), StringIO()
        with patch.object(cli, 'service_factory', side_effect=AssertionError('runtime accessed')):
            with redirect_stdout(output), redirect_stderr(errors):
                result = cli.main(['pkg', 'repair-deps', 'unregistered', '--from-log',
                                   str(self.log), '--diagnose-only', '--json'])
        self.assertEqual(result, 0, errors.getvalue())
        report = json.loads(output.getvalue())
        self.assertEqual(report['mode'], 'diagnostic-only')
        self.assertTrue(report['library_repair_candidate'])
        self.assertEqual(report['provider_resolution'], 'not-run')
        self.assertEqual(report['runtime_loading'], 'unverified')
        self.assertEqual(self.runtime.events, [])

    def test_diagnosis_only_reports_unhandled_failure_without_claiming_repair(self):
        self.log.write_text(MISSING + 'vendor: undefined symbol: missing_symbol\n')
        output, errors = StringIO(), StringIO()
        with patch.object(cli, 'service_factory', side_effect=AssertionError('runtime accessed')):
            with redirect_stdout(output), redirect_stderr(errors):
                result = cli.main(['pkg', 'repair-deps', 'unregistered', '--from-log',
                                   str(self.log), '--diagnose-only'])
        self.assertEqual(result, 0, errors.getvalue())
        report = json.loads(output.getvalue())
        self.assertFalse(report['library_repair_candidate'])
        self.assertEqual(report['provider_resolution'], 'not-run')
        self.assertIn({'kind': 'abi-mismatch'}, report['issues'])

    def test_cli_repair_routes_to_the_real_transaction_api(self):
        output, errors = StringIO(), StringIO()
        with patch.object(cli, 'service_factory', return_value=self.service):
            with redirect_stdout(output), redirect_stderr(errors):
                result = cli.main(['pkg', 'repair-deps', 'deps', '--from-log',
                                   str(self.log), '--json'])
        self.assertEqual(result, 0, errors.getvalue())
        transaction = json.loads(output.getvalue())
        self.assertEqual(transaction['phase'], 'completed')
        self.assertEqual(transaction['dependency_input']['libraries'], ['libasound.so.2'])


if __name__ == '__main__':
    unittest.main()
