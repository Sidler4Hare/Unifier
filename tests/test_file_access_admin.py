"""Host policy administration and durable recovery; no runtime or desktop."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.cli import main
from unifier.control import Control
from unifier.file_access import AccessError, AccessPolicy
from unifier.file_access_cli import AccessControl
from unifier.file_access_store import AccessStore, _write
from unifier.identity import binding_for
from unifier.portal_delivery import SelectionStore
from unifier.store import EnvironmentStore


class AccessAdministration(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.directory = self.root / '.lab/desktop/fixture'
        self.directory.mkdir(parents=True)
        self.record = {'id': 'fixture', 'environment_uuid': 'f74ef424-7093-458b-a7de-ff32fa82563c',
                       'generation': 1, 'runtime': {'container_id': 'a' * 64},
                       'desktop': {'portal_proxy': {'directory': str(self.directory)}}, 'shared_mounts': []}
        EnvironmentStore(self.root).save(self.record, None)
        self.store = SelectionStore(self.directory, str(self.directory), access_root=self.root,
                                    environment_binding=binding_for(self.record))
        self.addCleanup(self.close)
        self.source = self.root / 'source.txt'
        self.source.write_text('selected bytes')
        self.enterContext(patch('unifier.portal_delivery.shutil.disk_usage',
                               return_value=type('Disk', (), {'free': 90*1024**3, 'total': 100*1024**3})()))
        self.control = AccessControl(self.root)
        self.lease = self.store.ledger.header['lease_id']

    def close(self):
        if self.store.directory.exists():
            self.store.close()
        elif not self.store.closed:
            import os
            os.close(self.store.root_fd)
            self.store.closed = True

    def mark_dead(self):
        value = self.store.ledger.read()
        value['owner']['boot_id'] = 'previous-boot'
        _write(self.store.ledger.path, value)

    def test_revoke_persists_then_owner_removes_exact_snapshot(self):
        paths = self.store.deliver([self.source.as_uri()])
        self.assertEqual(self.control.revoke(self.lease, paths.grant_id)['state'], 'revoking')
        self.assertFalse(self.store.ready(paths))
        self.store.reconcile_revocations()
        self.assertFalse(Path(paths[0].removeprefix('file://')).exists())
        self.assertEqual(self.store.ledger.usage(), (0, 0))
        self.assertEqual(self.control.revoke(self.lease, paths.grant_id)['state'], 'released')

    def test_same_static_uri_has_distinct_request_grants(self):
        self.store.shares = [(str(self.root), str(self.root), True)]
        first = self.store.deliver([self.source.as_uri()])
        second = self.store.deliver([self.source.as_uri()])
        self.store.revoke(first)
        self.assertFalse(self.store.ready(first))
        self.assertTrue(self.store.ready(second))
        self.assertTrue(self.source.exists())

    def test_live_owner_cannot_be_recovered(self):
        with self.assertRaisesRegex(AccessError, 'LEASE_ACTIVE'):
            self.control.recover(self.lease)
        self.assertTrue(self.store.directory.exists())

    def test_dead_recovery_is_idempotent_and_preserves_other_lease_and_socket(self):
        self.store.deliver([self.source.as_uri()])
        unrelated = self.directory / 'selected-unrelated'
        unrelated.mkdir()
        socket = self.directory / 'files-bus'
        socket.write_text('belongs to a newer bridge')
        self.mark_dead()
        self.assertEqual(self.control.recover(self.lease)['state'], 'released')
        self.assertEqual(self.control.recover(self.lease)['state'], 'released')
        self.assertTrue(unrelated.is_dir())
        self.assertEqual(socket.read_text(), 'belongs to a newer bridge')

    def test_replaced_selection_inode_is_preserved(self):
        old = self.store.directory.rename(self.directory / 'selected-moved')
        self.store.directory.mkdir()
        self.mark_dead()
        try:
            with self.assertRaisesRegex(AccessError, 'OBJECT_CHANGED'):
                self.control.recover(self.lease)
            self.assertTrue(self.store.directory.exists())
        finally:
            self.store.directory.rmdir()
            old.rename(self.store.directory)

    def test_changed_environment_blocks_recovery(self):
        self.record['generation'] += 1
        EnvironmentStore(self.root).save(self.record, None)
        self.mark_dead()
        with self.assertRaisesRegex(AccessError, 'BINDING_CHANGED'):
            self.control.recover(self.lease)

    def test_missing_batch_retains_quota_instead_of_claiming_cleanup(self):
        paths = self.store.deliver([self.source.as_uri()])
        batch = self.store.directory / paths.grant_id
        batch.rename(self.store.directory / 'moved-by-guest')
        with self.assertRaisesRegex(AccessError, 'OBJECT_MISSING'):
            self.store.revoke(paths)
        self.assertEqual(self.store.ledger.usage(), (len('selected bytes'), 1))
        self.assertEqual(self.store.ledger.read()['grants'][paths.grant_id]['state'], 'needs-recovery')

    def test_missing_active_lease_directory_is_not_reported_as_recovered(self):
        self.store.deliver([self.source.as_uri()])
        moved = self.store.directory.rename(self.directory / 'selected-moved')
        self.mark_dead()
        try:
            with self.assertRaisesRegex(AccessError, 'OBJECT_MISSING'):
                self.control.recover(self.lease)
            self.assertEqual(self.store.ledger.usage(), (len('selected bytes'), 1))
        finally:
            moved.rename(self.store.directory)

    def test_oversized_policy_is_rejected_before_replacing_valid_policy(self):
        original = self.control.set_policy(AccessPolicy().as_dict())
        candidate = {**original, 'denied_roots': ['/' + ('a' * 32760), '/' + ('b' * 32760)]}
        with self.assertRaisesRegex(AccessError, 'STATE_LIMIT'):
            self.control.set_policy(candidate)
        self.assertEqual(self.control.store.policy().as_dict(), original)

    def test_policy_revision_changed_during_copy_prevents_publication(self):
        import unifier.portal_delivery as delivery
        original = delivery.os.read
        updated = False
        def read(fd, count):
            nonlocal updated
            if not updated:
                updated = True
                self.control.set_policy(AccessPolicy(denied_roots=(str(self.source),)).as_dict())
            return original(fd, count)
        with patch.object(delivery.os, 'read', side_effect=read):
            with self.assertRaisesRegex(AccessError, 'POLICY_STALE'):
                self.store.deliver([self.source.as_uri()])
        self.assertEqual(self.store.ledger.usage(), (0, 0))

    def test_cli_policy_set_list_and_revoke(self):
        policy_file = self.root / 'policy-input.json'
        policy_file.write_text(json.dumps(AccessPolicy().as_dict()))
        paths = self.store.deliver([self.source.as_uri()])
        for argv, expected in ((['policy', '--set', str(policy_file)], 'revision'),
                               (['list'], self.lease),
                               (['revoke', self.lease, paths.grant_id], 'revoking')):
            output = StringIO()
            with patch('unifier.cli._state_root', return_value=self.root), redirect_stdout(output):
                self.assertEqual(main(['file', 'access', *argv]), 0)
            self.assertIn(expected, output.getvalue())

    def test_host_authority_cannot_be_shared(self):
        with self.assertRaisesRegex(AccessError, 'STATE_EXPOSED'):
            self.control.store.check_mounts([{'host': str(self.root), 'container': '/data'}])


class RegisteredAccessPolicy(unittest.TestCase):
    def test_registered_file_route_uses_global_policy_before_execution(self):
        from test_file_routes import ready
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, runtime, _, _, _, document = ready(root)
            before = list(runtime.events)
            policy = AccessPolicy(denied_roots=(str(document),))
            with patch.object(AccessStore, 'policy', return_value=policy):
                with self.assertRaisesRegex(AccessError, 'DENIED'):
                    routes.plan(str(document), 'edit')
            self.assertEqual(runtime.events, before)

    def test_project_session_cannot_browse_a_denied_descendant(self):
        from test_project_sessions import ready
        from unifier.project_sessions import ProjectSessions
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, _, project, _, document = ready(root)
            before = list(runtime.events)
            with patch.object(AccessStore, 'policy', return_value=AccessPolicy(denied_roots=(str(document),))):
                with self.assertRaisesRegex(AccessError, 'DENIED'):
                    ProjectSessions(root, service).prepare(project['project_uuid'], argv=['true'])
            self.assertEqual(runtime.events, before)

    def test_view_policy_refusal_precedes_any_runtime_or_mount_action(self):
        from unittest.mock import Mock
        from unifier.views import ViewManager
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            mount = root / 'view'
            service = Mock()
            AccessControl(root).set_policy(AccessPolicy(denied_roots=(str(mount),)).as_dict())
            with self.assertRaisesRegex(AccessError, 'DENIED'):
                ViewManager(root, service).open('arch', mountpoint=str(mount))
            self.assertFalse(mount.exists())
            self.assertEqual(service.mock_calls, [])

    def test_resolve_and_copy_share_the_same_global_denial(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, target = root / 'source', root / 'target'
            source.mkdir(); target.mkdir()
            (source / 'a').write_text('keep')
            control = Control(root)
            src = control.add_root(str(source))
            dst = control.add_root(str(target))
            AccessControl(root).set_policy(AccessPolicy(denied_roots=(str(source / 'a'),)).as_dict())
            with self.assertRaisesRegex(AccessError, 'DENIED'):
                control.resolve_path(str(source / 'a'))
            with self.assertRaisesRegex(AccessError, 'DENIED'):
                control.copy_file(src['root_uuid'], 'a', dst['root_uuid'], 'b')
            self.assertFalse((target / 'b').exists())
            self.assertEqual((source / 'a').read_text(), 'keep')

    def test_creation_checks_policy_before_runtime_create(self):
        from test_env import service_in
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            share = root / 'data'
            share.mkdir()
            service, runtime = service_in(root)
            AccessControl(root).set_policy(AccessPolicy(denied_roots=(str(share),)).as_dict())
            before = list(runtime.events)
            with self.assertRaisesRegex(AccessError, 'DENIED'):
                service.create('arch', None, [str(share)])
            self.assertEqual(runtime.events, before)
