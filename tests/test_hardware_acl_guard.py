"""Offline tests for the AMD ACL lease. No device or ACL subprocess is used."""

from contextlib import nullcontext
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from tools import hardware_acl_guard as guard


TOKEN = 'amd-test-17'
DIRECTORY = Path('/unused-root-fixture') / TOKEN
ORIGINAL = (b'# file: /dev/dri/renderD128\n# owner: 0\n# group: 993\n'
            b'user::rw-\ngroup::rw-\nother::---\n\n')
APPLIED = (b'# file: /dev/dri/renderD128\n# owner: 0\n# group: 993\n'
           b'user::rw-\nuser:1001:rw-\ngroup::rw-\nmask::rw-\nother::---\n\n')


class AclLeaseTests(unittest.TestCase):
    def setUp(self):
        self.identity = {'boot': 'boot-A', 'dev': 5, 'inode': 788, 'rdev': 57984,
                         'uid': 0, 'gid': 993, 'mode': 0o660}
        self.armed = {'invocation_id': 'test-invocation', 'deadline_us': 300_000_000}
        self.current_acl = Mock()
        self.restore_acl = Mock()
        self.grant_acl = Mock()
        self.marker = Mock()
        self.latest = Mock()
        self.node_identity = Mock(return_value=self.identity)
        self.command = Mock(side_effect=AssertionError('unexpected real command'))
        self.enterContext(patch.multiple(
            guard, command=self.command, current_acl=self.current_acl, restore_acl=self.restore_acl,
            grant_acl=self.grant_acl, marker=self.marker, latest=self.latest, node_identity=self.node_identity,
            load_prepared=Mock(return_value=(self.identity, ORIGINAL, APPLIED)), open_node=lambda: nullcontext(9),
        ))

    def test_refuses_unapproved_owner_mode_device_or_boot(self):
        self.latest.return_value = {'phase': 'APPLIED'}
        for field, wrong in (('boot', 'boot-B'), ('inode', 789), ('dev', 6), ('rdev', 57985),
                             ('uid', 1001), ('gid', 0), ('mode', 0o666)):
            with self.subTest(field=field):
                self.node_identity.return_value = {**self.identity, field: wrong}
                with self.assertRaisesRegex(guard.Conflict, 'prepared identity'):
                    guard.restore(DIRECTORY, TOKEN)
        self.current_acl.assert_not_called()
        self.restore_acl.assert_not_called()

    def test_changed_acl_is_preserved_and_conflict_recorded(self):
        self.latest.return_value = {'phase': 'APPLIED'}
        self.current_acl.return_value = APPLIED.replace(b'rw-\nmask', b'r--\nmask')
        with self.assertRaises(guard.Conflict):
            guard.restore(DIRECTORY, TOKEN)
        self.restore_acl.assert_not_called()
        self.assertEqual(self.marker.call_args.args, (DIRECTORY, 'CONFLICT'))

    def test_granting_recovers_after_write_before_applied_record(self):
        self.latest.return_value = {'phase': 'GRANTING'}
        self.current_acl.side_effect = [APPLIED, ORIGINAL]
        guard.restore(DIRECTORY, TOKEN)
        self.restore_acl.assert_called_once_with(9, ORIGINAL)
        self.marker.assert_called_once_with(DIRECTORY, 'RESTORED', prior_phase='GRANTING', acl_written=True)

    def test_restoring_original_is_idempotent(self):
        self.latest.return_value = {'phase': 'GRANTING'}
        self.current_acl.return_value = ORIGINAL
        guard.restore(DIRECTORY, TOKEN)
        self.latest.return_value = {'phase': 'RESTORED'}
        guard.restore(DIRECTORY, TOKEN)
        self.restore_acl.assert_not_called()
        self.marker.assert_called_once_with(DIRECTORY, 'RESTORED', prior_phase='GRANTING', acl_written=False)

    def test_grant_requires_armed_matching_unexpired_unit(self):
        cases = [('PREPARED', self.armed, 1), ('ARMED', {**self.armed, 'invocation_id': 'different'}, 1),
                 ('ARMED', self.armed, self.armed['deadline_us'])]
        for phase, active, now in cases:
            with self.subTest(phase=phase, active=active, now=now):
                self.latest.return_value = {'phase': phase}
                with (patch.object(guard, 'verify_unit', return_value=active),
                      patch.object(guard, 'monotonic_us', return_value=now)):
                    with self.assertRaisesRegex(guard.Conflict, 'ARMED'):
                        guard.apply_grant(DIRECTORY, TOKEN, self.armed)
        self.grant_acl.assert_not_called()
        self.marker.assert_not_called()

    def test_consumed_request_is_bound_to_unit_and_write_ahead_marker(self):
        self.latest.return_value = {'phase': 'ARMED'}
        self.current_acl.side_effect = [ORIGINAL, APPLIED]
        order = Mock()
        order.attach_mock(self.marker, 'marker')
        order.attach_mock(self.grant_acl, 'grant')
        with (patch.object(guard, 'verify_unit', return_value=self.armed),
              patch.object(guard, 'state_read', return_value=json.dumps(self.armed).encode('ascii')),
              patch.object(guard, 'monotonic_us', return_value=1)):
            guard.apply_grant(DIRECTORY, TOKEN, self.armed)
        self.assertEqual([entry[0] for entry in order.mock_calls], ['marker', 'grant', 'marker'])
        self.assertEqual(self.marker.call_args_list[0].args, (DIRECTORY, 'GRANTING'))
        self.assertEqual(self.marker.call_args_list[1].args, (DIRECTORY, 'APPLIED'))

    def test_grant_rechecks_remaining_time_after_write_ahead_marker(self):
        self.latest.return_value = {'phase': 'ARMED'}
        self.current_acl.return_value = ORIGINAL
        late = self.armed['deadline_us'] - guard.MIN_GRANT_REMAINING_US + 1
        with (patch.object(guard, 'verify_unit', return_value=self.armed),
              patch.object(guard, 'state_read', return_value=json.dumps(self.armed).encode('ascii')),
              patch.object(guard, 'monotonic_us', side_effect=[1, late])):
            with self.assertRaisesRegex(guard.Conflict, 'less than 30 seconds'):
                guard.apply_grant(DIRECTORY, TOKEN, self.armed)
        self.grant_acl.assert_not_called()
        self.marker.assert_called_once_with(DIRECTORY, 'GRANTING', watchdog=self.armed)

    def test_status_rejects_stale_applied_marker(self):
        self.latest.return_value = {'phase': 'APPLIED', 'watchdog': self.armed}
        replacement = {**self.armed, 'invocation_id': 'replacement'}
        with patch.object(guard, 'verify_unit', return_value=replacement):
            with self.assertRaisesRegex(guard.Conflict, 'no longer matches'):
                guard.status(DIRECTORY, TOKEN)
        self.current_acl.assert_not_called()
        self.marker.assert_not_called()

    def test_applied_status_requires_matching_acl_and_reports_remaining_time(self):
        self.latest.return_value = {'phase': 'APPLIED', 'watchdog': self.armed}
        self.current_acl.side_effect = [ORIGINAL, APPLIED]
        with (patch.object(guard, 'verify_unit', return_value=self.armed),
              patch.object(guard, 'monotonic_us', return_value=100)):
            with self.assertRaisesRegex(guard.Conflict, 'recorded APPLIED'):
                guard.status(DIRECTORY, TOKEN)
            result = guard.status(DIRECTORY, TOKEN)
        self.assertTrue(result['live_verified'])
        self.assertEqual(result['remaining_us'], self.armed['deadline_us'] - 100)
        self.marker.assert_not_called()

    def test_restored_status_rechecks_original_without_a_live_unit(self):
        self.latest.return_value = {'phase': 'RESTORED'}
        self.current_acl.return_value = ORIGINAL
        with patch.object(guard, 'verify_unit', side_effect=AssertionError('unexpected unit lookup')):
            result = guard.status(DIRECTORY, TOKEN)
        self.assertTrue(result['live_verified'])
        self.assertIsNone(result['remaining_us'])

    def test_acl_prediction_refuses_a_preexisting_named_acl_or_wider_base(self):
        self.assertEqual(guard.expected_acl(ORIGINAL), APPLIED)
        for original in (APPLIED, ORIGINAL.replace(b'other::---', b'other::r--')):
            with self.assertRaises(guard.Conflict):
                guard.expected_acl(original)

    def test_token_stays_within_the_fixed_service_namespace(self):
        self.assertEqual(guard.unit_name(TOKEN), 'unifier-v008-amd-acl-amd-test-17.service')
        for token in ('', '..', '../target', 'upperCase', 'x' * 41, 'x.service', 'x\n'):
            with self.subTest(token=token), self.assertRaises(ValueError):
                guard.unit_name(token)


class AclCommandTests(unittest.TestCase):
    def test_restore_passes_pinned_inode_and_rewrites_only_the_header(self):
        with patch.object(guard, 'command', return_value=b'') as command:
            guard.restore_acl(9, ORIGINAL)
        body = ORIGINAL.replace(b'/dev/dri/renderD128', b'/proc/self/fd/9', 1)
        command.assert_called_once_with(('/usr/bin/setfacl', '--restore=-'), pass_fds=(9,), data=body)


if __name__ == '__main__':
    unittest.main()
