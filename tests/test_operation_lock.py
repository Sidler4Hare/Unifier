"""Offline process boundaries for management-root package coordination."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from unifier.errors import UnifierError
from unifier.operation_lock import management_lock


class ManagementLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def child(self, code):
        child = subprocess.Popen([sys.executable, "-c", code, str(self.root)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        def cleanup():
            if child.poll() is None:
                child.terminate()  # Exact Popen child still owned and unreaped.
            child.communicate(timeout=5)
        self.addCleanup(cleanup)
        return child

    def test_two_independent_processes_do_not_enter_at_the_same_time(self):
        with management_lock(self.root):
            child = self.child("""from pathlib import Path
import sys
from unifier.operation_lock import management_lock
from unifier.errors import UnifierError
try:
    with management_lock(Path(sys.argv[1]), timeout=.1):
        raise AssertionError('overlapping writers')
except UnifierError as exc:
    assert 'TIMEOUT' in str(exc)
""")
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, (out, err))
        with management_lock(self.root, timeout=0):
            pass

    def test_crashed_holder_releases_without_deleting_lock_or_transaction(self):
        child = self.child("""import os, sys
from pathlib import Path
from unifier.operation_lock import management_lock
root=Path(sys.argv[1])
with management_lock(root):
    (root/'pending.json').write_text('needs-recovery')
    os._exit(17)
""")
        child.communicate(timeout=5)
        self.assertEqual(child.returncode, 17)
        path = self.root / '.lab/state/operations/management.lock'
        inode = path.stat().st_ino
        with management_lock(self.root, timeout=0):
            self.assertEqual((self.root / 'pending.json').read_text(), 'needs-recovery')
        self.assertEqual(path.stat().st_ino, inode)

    def test_two_process_writers_both_complete_without_overlapping_writes(self):
        code = """import sys, time
from pathlib import Path
from unifier.operation_lock import management_lock
root=Path(sys.argv[1])
with management_lock(root, timeout=3):
    marker=root/'writer-active'
    assert not marker.exists(), 'overlapping native simulation'
    marker.touch()
    with (root/'writes').open('a') as stream:
        stream.write('enter\\n'); stream.flush()
        time.sleep(.05)
        stream.write('exit\\n'); stream.flush()
    marker.unlink()
"""
        first, second = self.child(code), self.child(code)
        for child in (first, second):
            out, err = child.communicate(timeout=5)
            self.assertEqual(child.returncode, 0, (out, err))
        self.assertEqual((self.root / 'writes').read_text().splitlines(),
                         ['enter', 'exit', 'enter', 'exit'])

    def test_cancellation_and_busy_never_admit(self):
        event = threading.Event()
        with management_lock(self.root):
            with self.assertRaisesRegex(UnifierError, 'BUSY'):
                with management_lock(self.root, blocking=False):
                    self.fail('busy writer admitted')
            event.set()
            with self.assertRaisesRegex(UnifierError, 'CANCELLED'):
                with management_lock(self.root, cancel=event):
                    self.fail('cancelled writer admitted')

    def test_cancellation_while_waiting_releases_waiter_descriptor(self):
        event = threading.Event()
        ready = threading.Event()
        errors = []
        def wait():
            ready.set()
            try:
                with management_lock(self.root, cancel=event, timeout=2):
                    errors.append('unexpected admission')
            except UnifierError as exc:
                errors.append(str(exc))
        with management_lock(self.root):
            thread = threading.Thread(target=wait)
            thread.start()
            try:
                self.assertTrue(ready.wait(1))
                event.set()
            finally:
                event.set()
                thread.join(3)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertIn('CANCELLED', errors[0])
        with management_lock(self.root, timeout=0):
            pass

    def test_symbolic_alias_uses_same_inode_but_different_root_is_independent(self):
        actual = self.root / 'actual'
        actual.mkdir()
        alias = self.root / 'alias'
        alias.symlink_to(actual, target_is_directory=True)
        with management_lock(actual):
            with self.assertRaisesRegex(UnifierError, 'BUSY'):
                with management_lock(alias, blocking=False):
                    self.fail('alias bypassed writer')
            with management_lock(self.root / 'other', blocking=False):
                pass

    def test_exception_closes_lock_and_invalid_timeouts_are_rejected(self):
        with self.assertRaisesRegex(RuntimeError, 'probe'):
            with management_lock(self.root):
                raise RuntimeError('probe')
        with management_lock(self.root, timeout=0):
            pass
        for value in (-1, True, float('nan'), float('inf'), '1'):
            with self.subTest(value=value), self.assertRaises(UnifierError):
                with management_lock(self.root, timeout=value):
                    self.fail('invalid timeout')


if __name__ == '__main__':
    unittest.main()
