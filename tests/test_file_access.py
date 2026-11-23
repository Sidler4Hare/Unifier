"""Shared file policy, static authority and durable delivery failure contracts."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.file_access import AccessError, AccessPolicy, decide_selection, mapped_path
from unifier.file_access_store import AccessStore, GrantLedger
from unifier.portal_delivery import SelectionStore
from unifier.kdialog_portal import guest_paths
from unifier.kdialog_args import parse_args


class FileAccessContracts(unittest.TestCase):
    def test_selection_intersects_policy_for_all_backends(self):
        policy = AccessPolicy(read_roots=('/data',), write_roots=('/data/write',), denied_roots=('/data/secret',))
        for shares in ([], [('/data', '/guest', True)]):
            with self.subTest(shares=shares):
                with self.assertRaisesRegex(AccessError, 'ACCESS_DENIED'):
                    decide_selection(policy, '/data/secret/a', shares)
                with self.assertRaisesRegex(AccessError, 'ACCESS_OUTSIDE_SCOPE'):
                    decide_selection(policy, '/other/a', shares)
                with self.assertRaises(AccessError):
                    decide_selection(policy, '/data/readme', shares, writable=True)
        self.assertEqual(decide_selection(policy, '/data/write/a', [('/data','/guest',True)],
                                         writable=True).backend, 'static')

    def test_directory_cannot_grant_a_denied_descendant(self):
        with self.assertRaisesRegex(AccessError, 'ACCESS_DENIED'):
            AccessPolicy(denied_roots=('/data/secret',)).require('/data', 'browse', directory=True)

    def test_static_mapping_uses_guest_mount_authority(self):
        shares = [('/data','/guest',True), ('/else','/guest/private',False)]
        with self.assertRaisesRegex(AccessError, 'SHADOWED'):
            mapped_path('/data/private/a', shares)
        with self.assertRaisesRegex(AccessError, 'READ_ONLY'):
            mapped_path('/else/a', shares, writable=True)
        self.assertEqual(str(mapped_path('/data/a', shares)), '/guest/a')

    def test_policy_schema_fails_closed_and_revision_is_compared(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccessStore(tmp)
            first = store.policy().as_dict()
            result = store.set_policy(first)
            self.assertEqual(result['revision'], 2)
            with self.assertRaisesRegex(AccessError, 'STALE'):
                store.set_policy(first)
            store.policy_path.write_text('{broken')
            with self.assertRaisesRegex(AccessError, 'STATE_INVALID'):
                store.policy()

    def test_policy_cannot_claim_to_hide_existing_mount(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AccessStore(tmp)
            policy = AccessPolicy(denied_roots=('/data/secret',)).as_dict()
            with self.assertRaisesRegex(AccessError, 'DENIED'):
                store.set_policy(policy, environments=[{'shared_mounts':[{'host':'/data','container':'/data'}]}])
            self.assertFalse(store.policy_path.exists())


class DurableAccessContracts(unittest.TestCase):
    def setUp(self):
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.tmp)
        (self.root/'bridge').mkdir()
        self.source = self.root/'source.txt'
        self.source.write_text('contents')
        self.store = SelectionStore(self.root/'bridge', str(self.root/'bridge'), access_root=self.root)
        self.addCleanup(self.store.close)
        self.enterContext(patch('unifier.portal_delivery.shutil.disk_usage',
                               return_value=type('Disk',(),{'free':90*1024**3,'total':100*1024**3})()))

    def test_native_and_command_picker_consume_the_same_delivered_snapshot(self):
        uris = self.store.deliver([self.source.as_uri()])
        request = parse_args(['--getopenfilename', '.'])
        paths = guest_paths(uris, [], request)
        self.assertEqual(Path(paths[0]).read_bytes(), self.source.read_bytes())
        value = self.store.ledger.read()
        grant = next(iter(value['grants'].values()))
        self.assertEqual(grant['state'], 'ready')
        self.assertEqual(grant['decisions'][0]['enforcement'], 'snapshot-copy')
        self.assertFalse(self.store.ledger.path.is_relative_to(self.root/'bridge'))

    def test_cleanup_failure_retains_capacity_and_requires_recovery(self):
        uris = self.store.deliver([self.source.as_uri()])
        with patch('unifier.portal_delivery.shutil.rmtree', side_effect=PermissionError('busy')):
            with self.assertRaises(PermissionError):
                self.store.revoke(uris)
        self.assertEqual(self.store.ledger.usage()[0], len('contents'))
        self.assertEqual(next(iter(self.store.ledger.read()['grants'].values()))['state'], 'needs-recovery')
        with self.assertRaisesRegex(AccessError, 'RECOVERY_REQUIRED'):
            self.store.deliver([self.source.as_uri()])

    def test_static_delivery_does_not_use_snapshot_quota(self):
        self.store.shares = [(str(self.root),str(self.root),True)]
        self.store.max_session_bytes = 0
        for _ in range(130):
            self.store.deliver([self.source.as_uri()])
        self.assertEqual(self.store.ledger.usage(), (0,0))

    def test_policy_is_reloaded_before_each_request(self):
        state = AccessStore(self.root)
        state.set_policy(AccessPolicy(denied_roots=(str(self.source),)).as_dict())
        with self.assertRaisesRegex(AccessError, 'DENIED'):
            self.store.deliver([self.source.as_uri()])
        self.assertEqual(self.store.ledger.read()['grants'], {})

    def test_lease_close_records_release_and_is_idempotent(self):
        self.store.deliver([self.source.as_uri()])
        self.store.close()
        self.store.close()
        self.assertEqual(self.store.ledger.usage(), (0,0))
        self.assertFalse(self.store.directory.exists())
