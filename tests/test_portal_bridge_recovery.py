"""Recovery preserves the old record until its exact snapshot is reclaimed."""
import copy
import json
import os
from pathlib import Path
import signal
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from unifier.errors import UnifierError
from unifier.on_demand import start_bridge, stop_bridge


class DeliveryBridgeRecovery(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(temporary) / '.lab/desktop/fixture'
        self.directory.mkdir(parents=True)
        self.record = {'id': 'fixture', 'desktop': {'portal_proxy': {
            'directory': str(self.directory), 'container': '/run/multilinux-portal'}}}
        self.selected = self.directory / 'selected-old'
        self.selected.mkdir()
        self.snapshot = self.selected / 'token' / 'snapshot'
        self.snapshot.parent.mkdir()
        self.snapshot.write_text('old contents')
        info = self.selected.stat()
        self.delivery = {'pid': 22, 'stamp': 122, 'socket': str(self.directory / 'files-bus'),
                         'selection_directory': str(self.selected),
                         'selection_identity': [info.st_dev, info.st_ino]}
        self.bridge = {'pid': 11, 'stamp': 111, 'socket': str(self.directory / 'bus'),
                       'file_delivery': self.delivery}
        self.stamps = {11: 111, 33: 133}
        self.enterContext(patch('unifier.on_demand.process_stamp', side_effect=self.stamps.get))
        self.kill = self.enterContext(patch('unifier.on_demand.os.kill'))
        self.spawn = self.enterContext(patch('unifier.on_demand.subprocess.Popen'))
        self.enterContext(patch.dict(os.environ, {'XAUTHORITY': ''}))
        real_lstat = Path.lstat

        def lstat(path):
            if path == self.directory / 'bus':
                return SimpleNamespace(st_mode=stat.S_IFSOCK)
            return real_lstat(path)

        self.enterContext(patch.object(Path, 'lstat', lstat))

    def start(self):
        return start_bridge(self.record, self.bridge, file_delivery=True)

    def assert_cleanup_rejected(self, message):
        before = copy.deepcopy(self.bridge)
        with self.assertRaisesRegex(UnifierError, message):
            self.start()
        self.assertEqual(self.bridge, before)
        self.spawn.assert_not_called()
        self.kill.assert_not_called()

    def spawn_delivery(self, command, *, cwd, stdout, **kwargs):
        self.assertFalse(self.selected.exists())
        self.assertEqual(cwd, self.directory)
        new_directory = cwd / 'selected-new'
        new_directory.mkdir()
        info = new_directory.stat()
        stdout.write((json.dumps({'event': 'ready', 'selection_directory': str(new_directory),
                                  'selection_identity': [info.st_dev, info.st_ino]}) + '\n').encode())
        stdout.flush()
        return SimpleNamespace(pid=33, poll=lambda: None)

    def test_reclaims_old_snapshot_before_spawning_replacement(self):
        sibling = self.directory / 'selected-unrelated'
        sibling.mkdir()
        self.stamps[22] = 222  # The old PID now belongs to someone else.
        self.spawn.side_effect = self.spawn_delivery
        with patch.object(Path, 'is_socket', return_value=True):
            current = self.start()
        self.assertEqual(current['file_delivery']['pid'], 33)
        self.assertTrue(sibling.is_dir())
        self.assertIs(self.bridge['file_delivery'], self.delivery)
        self.kill.assert_not_called()

    def test_absent_snapshot_allows_recovery_and_repeated_stop(self):
        self.snapshot.unlink()
        self.snapshot.parent.rmdir()
        self.selected.rmdir()
        self.spawn.side_effect = self.spawn_delivery
        with patch.object(Path, 'is_socket', return_value=True):
            current = self.start()
        self.stamps.clear()
        stop_bridge(current, record=self.record)
        stop_bridge(current, record=self.record)
        self.assertFalse(Path(current['file_delivery']['selection_directory']).exists())

    def test_inode_mismatch_keeps_record_and_snapshot(self):
        self.delivery['selection_identity'][1] += 1
        self.assert_cleanup_rejected('snapshot directory identity changed')
        self.assertTrue(self.selected.is_dir())

    def test_symlink_is_not_followed(self):
        moved = self.selected.rename(self.directory / 'selected-unrelated')
        self.selected.symlink_to(moved, target_is_directory=True)
        self.assert_cleanup_rejected('snapshot directory identity changed')
        self.assertTrue((moved / 'token' / 'snapshot').is_file())

    def test_outside_parent_is_rejected_even_when_inode_matches(self):
        moved = self.selected.rename(self.directory.parent / self.selected.name)
        self.delivery['selection_directory'] = str(moved)
        self.assert_cleanup_rejected('snapshot directory is outside')
        self.assertTrue((moved / 'token' / 'snapshot').is_file())

    def test_missing_legacy_selection_identity_retains_ownership(self):
        del self.delivery['selection_identity']
        self.assert_cleanup_rejected('snapshot ownership record is incomplete')

    def test_cleanup_error_prevents_replacement(self):
        with patch('unifier.on_demand.shutil.rmtree', side_effect=PermissionError('denied')):
            self.assert_cleanup_rejected('cannot clean up Portal delivery.*denied')
        self.assertTrue(self.snapshot.is_file())

    def test_dead_bus_cleanup_failure_prevents_replacement(self):
        self.stamps.clear()
        with patch('unifier.on_demand.shutil.rmtree', side_effect=PermissionError('denied')):
            self.assert_cleanup_rejected('cannot clean up Portal delivery.*denied')

    def test_stop_closes_owned_bus_after_snapshot_cleanup_fails(self):
        before = copy.deepcopy(self.bridge)

        def stop_process(pid, _signal):
            del self.stamps[pid]

        self.kill.side_effect = stop_process
        with patch('unifier.on_demand.shutil.rmtree', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(UnifierError, 'cannot clean up Portal delivery.*denied'):
                stop_bridge(self.bridge, record=self.record)

        self.kill.assert_called_once_with(self.bridge['pid'], signal.SIGTERM)
        self.assertNotIn(self.bridge['pid'], self.stamps)
        self.assertEqual(self.bridge, before)
        self.assertTrue(self.snapshot.is_file())
        self.spawn.assert_not_called()

    def test_generation_mismatch_blocks_cleanup_and_reuse(self):
        self.bridge['environment_identity'] = ['f74ef424-7093-458b-a7de-ff32fa82563c', 1]
        self.record.update(environment_uuid=self.bridge['environment_identity'][0], generation=2)
        self.assert_cleanup_rejected('different environment generation')
