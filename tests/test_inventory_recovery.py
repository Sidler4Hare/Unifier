"""No Podman invocation: exact orphan-reference recovery contracts."""
import os
from pathlib import Path
import unittest
from test_package_inventory import AdapterContracts
from unifier.inventory_probe import recover
from unifier.identity import binding_for
from unifier.on_demand import process_stamp
from unifier.view_journal import read_journal, write_journal


class InventoryRecovery(unittest.TestCase):
    setUp = AdapterContracts.setUp
    runner = AdapterContracts.runner

    def journal_value(self, **changes):
        value = {'schema_version': 1, 'state': 'mounted', 'cid': 'a' * 64,
                 'token': 'owned-token', 'environment_binding': binding_for(self.record),
                 'owner': {'pid': 99999999, 'stamp': 1,
                           'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}}
        value.update(changes)
        write_journal(str(self.journal), value)
        return value

    def test_owned_dead_reference_released_exactly_once(self):
        self.journal_value()
        self.assertEqual(recover(self.record, self.journal, run=self.runner)['status'], 'recovered')
        self.assertEqual(recover(self.record, self.journal, run=self.runner)['status'], 'already-released')
        self.assertEqual([c for c in self.calls if c[1] == 'umount'], [['podman', 'umount', 'a'*64]])
        self.assertFalse(any(c[1] in {'mount', 'start', 'exec', 'stop'} for c in self.calls))

    def test_ambiguous_crash_windows_never_guess_umount(self):
        for state in ('mounting', 'releasing', 'release-failed'):
            self.journal_value(state=state)
            self.assertEqual(recover(self.record, self.journal, run=self.runner)['status'], 'unresolved')
            self.assertEqual(read_journal(str(self.journal))['state'], state)
        self.assertEqual(self.calls, [])

    def test_live_missing_identity_wrong_binding_and_reboot_are_kept(self):
        boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
        for changes in ({'owner': {}}, {'environment_binding': {}},
                        {'owner': {'pid': os.getpid(), 'stamp': process_stamp(os.getpid()), 'boot_id': boot}},
                        {'owner': {'pid': 99999999, 'stamp': 1, 'boot_id': 'old-boot'}}):
            self.journal_value(**changes)
            self.assertEqual(recover(self.record, self.journal, run=self.runner)['status'], 'unresolved')
        self.assertEqual(self.calls, [])

    def test_timeout_is_preserved_and_not_retried(self):
        self.journal_value()
        def failed(argv, **kwargs):
            if argv[1] == 'umount':
                self.calls.append(argv)
                raise TimeoutError('unmount unknown')
            return self.runner(argv, **kwargs)
        self.assertEqual(recover(self.record, self.journal, run=failed)['status'], 'unresolved')
        self.assertEqual(recover(self.record, self.journal, run=failed)['status'], 'unresolved')
        self.assertEqual(sum(c[1]=='umount' for c in self.calls), 1)
