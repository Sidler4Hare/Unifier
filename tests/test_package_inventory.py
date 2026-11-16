"""Bounded offline DB and real-adapter contracts; never run Podman here."""
from dataclasses import asdict, replace
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from test_packages import make_env, package_service
from unifier.errors import UnifierError
from unifier.inventory_probe import probe
from unifier.package_inventory import (
    MAX_DATABASE_BYTES, observation, parse_dpkg_status, read_dpkg_database,
)
from unifier.runtime import PodmanRuntime
from unifier.view_journal import read_journal, write_journal


def stanza(name='base', version='1:2.0-3~deb13', architecture='amd64', state='install ok installed'):
    return (f'Package: {name}\nStatus: {state}\nArchitecture: {architecture}\n'
            f'Version: {version}\nDescription: fixture\n continuation\n')


class DatabaseContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.db = self.root / 'var/lib/dpkg'
        self.db.mkdir(parents=True)
        (self.db / 'updates').mkdir()
        (self.db / 'lock').touch()
        (self.db / 'status').write_text(stanza())

    def read(self, **kwargs):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return read_dpkg_database(fd, **kwargs)
        finally:
            os.close(fd)

    def test_exact_historical_inventory_preserves_multiarch_rows_and_versions(self):
        text = (stanza('z') + '\n' + stanza('base', architecture='i386') + '\n'
                + stanza('base', architecture='amd64', state='deinstall ok config-files'))
        inventory = parse_dpkg_status(text)
        self.assertEqual(inventory, 'base\t1:2.0-3~deb13\nbase\t1:2.0-3~deb13\nz\t1:2.0-3~deb13\n')
        (self.db / 'status').write_text(text)
        observed, evidence = self.read()
        self.assertEqual(observed, inventory)
        self.assertEqual(evidence['native_lock'], 'dpkg-posix-shared')
        self.assertFalse(evidence['pending_updates'])

    def test_corruption_unknown_states_and_duplicate_identity_are_rejected(self):
        for text in ('', stanza() + '\n' + stanza(), stanza(state='install ok unknown'),
                     stanza().replace('Version:', 'Package:'),
                     stanza().replace('Package: base', 'Package: base\n injected'),
                     stanza().replace('Architecture: amd64\n', '')):
            with self.subTest(text=text), self.assertRaises(UnifierError):
                parse_dpkg_status(text)

    def test_failed_preinst_leaves_a_selection_outside_the_inventory(self):
        selection = 'Package: fixture\nStatus: install ok not-installed\nArchitecture: all\n'
        (self.db / 'status').write_text(stanza() + '\n' + selection)
        inventory, _ = self.read()
        self.assertEqual(inventory, 'base\t1:2.0-3~deb13\n')

    def test_pending_updates_and_missing_lock_refuse_without_creating_files(self):
        (self.db / 'updates/0001').write_text('unfinished')
        with self.assertRaisesRegex(UnifierError, 'pending updates'):
            self.read()
        (self.db / 'updates/0001').unlink()
        (self.db / 'lock').unlink()
        with self.assertRaises(OSError):
            self.read()
        self.assertFalse((self.db / 'lock').exists())

    def test_symlink_file_and_parent_escapes_are_refused(self):
        (self.root / 'outside').write_text(stanza())
        (self.db / 'status').unlink()
        (self.db / 'status').symlink_to(self.root / 'outside')
        with self.assertRaises(OSError):
            self.read()
        (self.db / 'status').unlink()
        (self.db / 'status').write_text(stanza())
        self.db.rename(self.root / 'outside-db')
        self.db.symlink_to(self.root / 'outside-db', target_is_directory=True)
        with self.assertRaises(OSError):
            self.read()

    def test_sparse_oversize_deadline_and_path_replacement_refuse(self):
        with (self.db / 'status').open('wb') as stream:
            stream.truncate(MAX_DATABASE_BYTES + 1)
        with self.assertRaisesRegex(UnifierError, 'byte budget'):
            self.read()
        (self.db / 'status').write_text(stanza())
        with self.assertRaisesRegex(UnifierError, 'deadline'):
            self.read(seconds=0)
        parser = parse_dpkg_status
        def replace_status(text):
            replacement = self.db / 'new'
            replacement.write_text(stanza())
            replacement.replace(self.db / 'status')
            return parser(text)
        with patch('unifier.package_inventory.parse_dpkg_status', replace_status):
            with self.assertRaisesRegex(UnifierError, 'changed'):
                self.read()

    def test_native_posix_writer_lock_in_an_independent_process_is_respected(self):
        # Acquire in this process and ask a different process to read: POSIX
        # locks are process-associated, so a thread would not test this boundary.
        with (self.db / 'lock').open('r+') as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code = '''import os, sys
from unifier.package_inventory import read_dpkg_database
from unifier.errors import UnifierError
fd=os.open(sys.argv[1], os.O_RDONLY|os.O_DIRECTORY)
try:
    read_dpkg_database(fd)
except UnifierError as exc:
    assert 'busy' in str(exc)
else:
    raise AssertionError('native writer lock ignored')
finally:
    os.close(fd)
'''
            result = subprocess.run([sys.executable, '-c', code, str(self.root)],
                                    capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.read()[0])

    def test_database_directory_replacement_does_not_read_a_stale_pinned_tree(self):
        parser = parse_dpkg_status
        def replace_directory(text):
            self.db.rename(self.root / 'old-db')
            self.db.mkdir()
            return parser(text)
        with patch('unifier.package_inventory.parse_dpkg_status', replace_directory):
            with self.assertRaisesRegex(UnifierError, 'directory changed'):
                self.read()


class AdapterContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service, self.fake = package_service(self.root)
        self.record = make_env(self.service, self.fake, 'debian13', 'peer')
        self.record['runtime']['container_id'] = 'a' * 64
        self.journal = self.root / 'journal.json'
        self.calls = []
        db = self.root / 'merged/var/lib/dpkg'
        db.mkdir(parents=True)
        (db / 'updates').mkdir()
        (db / 'lock').touch()
        (db / 'status').write_text(stanza())

    def runner(self, argv, **kwargs):
        self.calls.append(argv)
        if argv[1] == 'inspect':
            return subprocess.CompletedProcess(argv, 0, json.dumps([{
                'Id': 'a' * 64, 'Config': {'Labels': {
                    'multilinux.project': str(self.root), 'multilinux.env': 'peer',
                    'multilinux.role': 'environment'}},
                'State': {'Running': False, 'Status': 'exited'}, 'Mounts': [],
            }]), '')
        if argv[1] == 'mount':
            return subprocess.CompletedProcess(argv, 0, str(self.root / 'merged') + '\n', '')
        if argv[1] == 'umount':
            return subprocess.CompletedProcess(argv, 0, '', '')
        raise AssertionError('forbidden runtime command: ' + str(argv))

    def test_helper_acquires_and_releases_only_one_exact_storage_reference(self):
        value = probe(self.record, self.journal, 'token', run=self.runner)
        self.assertEqual(value.status, 'supported', value.reason)
        self.assertEqual(value.inventory, 'base\t1:2.0-3~deb13\n')
        self.assertEqual(read_journal(str(self.journal))['state'], 'released')
        self.assertEqual([c[1] for c in self.calls], ['inspect', 'mount', 'inspect', 'inspect', 'umount'])
        self.assertTrue(all(c[-1] == 'a' * 64 for c in self.calls))

    def test_failed_read_releases_but_failed_release_is_persistent_and_not_retried(self):
        (self.root / 'merged/var/lib/dpkg/updates/0001').touch()
        result = probe(self.record, self.journal, 'one', run=self.runner)
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(read_journal(str(self.journal))['state'], 'released')
        def failed_release(argv, **kwargs):
            if argv[1] == 'umount':
                self.calls.append(argv)
                return subprocess.CompletedProcess(argv, 1, '', 'busy')
            return self.runner(argv, **kwargs)
        result = probe(self.record, self.journal, 'two', run=failed_release)
        self.assertEqual(result.status, 'unavailable')
        self.assertEqual(read_journal(str(self.journal))['state'], 'release-failed')
        count = len(self.calls)
        result = probe(self.record, self.journal, 'three', run=self.runner)
        self.assertIn('reconciliation', result.reason)
        self.assertEqual(len(self.calls), count)

    def test_running_foreign_or_shadowed_database_refuses_before_mount(self):
        for kind in ('running', 'foreign', 'mount'):
            def altered(argv, **kwargs):
                response = self.runner(argv, **kwargs)
                data = json.loads(response.stdout)[0]
                if kind == 'running':
                    data['State']['Running'] = True
                elif kind == 'foreign':
                    data['Id'] = 'b' * 64
                else:
                    data['Mounts'] = [{'Destination': '/var/lib'}]
                response.stdout = json.dumps([data])
                return response
            self.calls.clear()
            result = probe(self.record, self.journal, kind, run=altered)
            self.assertEqual(result.status, 'unavailable')
            self.assertEqual([c[1] for c in self.calls], ['inspect'])

    def test_actual_podman_runtime_uses_helper_contract_and_checks_release_evidence(self):
        info = self.fake.inspect(self.record['runtime']['container_name'])
        info = replace(info, container_id='a' * 64, status='exited')
        def runner(argv, **kwargs):
            self.assertEqual(argv[:2], ['podman', 'unshare'])
            journal = Path(argv[argv.index('--journal') + 1])
            token = argv[argv.index('--token') + 1]
            write_journal(str(journal), {'state': 'released', 'cid': 'a' * 64, 'token': token})
            return subprocess.CompletedProcess(argv, 0, json.dumps(asdict(
                observation(self.record, inventory='base\t1\n'))), '')
        runtime = PodmanRuntime(runner=runner, check_rootless=False)
        with patch.object(runtime, '_require_owned', return_value=info):
            value = runtime.read_inventory_readonly(self.record)
        self.assertEqual(value.status, 'supported', value.reason)
        self.assertEqual(value.binding['container_id'], 'a' * 64)
        # Missing native decoding remains a capability refusal before Podman.
        record = make_env(self.service, self.fake, 'fedora44', 'p-fedora')
        from unifier.package_inventory import InventoryUnsupported
        with patch('unifier.inventory_backends.rpm_decoder',
                   side_effect=InventoryUnsupported('native decoder missing')):
            self.assertEqual(runtime.read_inventory_readonly(record).status, 'unsupported')

    def test_peer_observation_generation_change_does_not_authorize_or_block_target(self):
        make_env(self.service, self.fake, 'fedora44', 'target')
        original = self.fake.read_inventory_readonly
        def changed(record):
            value = original(record)
            current = self.service.store.load('peer')
            current['generation'] += 1
            self.service.store.save(current, None)
            return value
        self.fake.read_inventory_readonly = changed
        self.fake.events.clear()
        txn = self.service.install_packages('target', ['hello'])
        self.assertIsNone(txn['failure_phase'])
        self.assertTrue(txn['native'])
        self.assertEqual(txn['isolation_observation']['status'], 'unavailable')
        self.assertFalse(any(e[0] in {'start', 'exec'} and e[1] == self.record['runtime']['container_name']
                             for e in self.fake.events))


if __name__ == '__main__':
    unittest.main()
