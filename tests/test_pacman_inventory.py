"""Stopped ALPM DB contracts, including native lock and real layout metadata."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.errors import UnifierError
from unifier import pacman_inventory as inventory


def desc(name='base', version='2:1.4-3'):
    return f'%NAME%\n{name}\n\n%VERSION%\n{version}\n\n%DESC%\nfixture\n'


class PacmanInventory(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = self.root/'var/lib/pacman'
        self.local = self.db/'local'
        (self.local/'base-1').mkdir(parents=True)
        (self.local/'base-1/desc').write_text(desc())

    def read(self, **options):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return inventory.read_pacman_database(fd, **options)
        finally:
            os.close(fd)

    def test_real_local_database_version_file_is_supported(self):
        (self.local/'ALPM_DB_VERSION').write_text('9\n')
        text, proof = self.read()
        self.assertEqual(text, 'base 2:1.4-3\n')
        self.assertEqual(proof['database'], 'pacman')

    def test_native_db_lck_refuses_even_when_it_is_a_dangling_symlink(self):
        lock = self.db/'db.lck'
        lock.touch()
        with self.assertRaisesRegex(UnifierError, 'lock'):
            self.read()
        lock.unlink()
        lock.symlink_to(self.root/'absent')
        with self.assertRaisesRegex(UnifierError, 'lock'):
            self.read()

    def test_late_in_place_change_of_already_read_desc_refuses(self):
        (self.local/'z-1').mkdir()
        (self.local/'z-1/desc').write_text(desc('z'))
        original = inventory.parse_pacman_desc
        def changing(text, entry):
            if entry == 'z-1':
                (self.local/'base-1/desc').write_text(desc(version='new-longer-version'))
            return original(text, entry)
        with patch.object(inventory, 'parse_pacman_desc', changing):
            with self.assertRaisesRegex(UnifierError, 'changed'):
                self.read()

    def test_lock_appearing_mid_read_and_desc_link_refuse(self):
        original = inventory.parse_pacman_desc
        def locking(text, entry):
            (self.db/'db.lck').touch()
            return original(text, entry)
        with patch.object(inventory, 'parse_pacman_desc', locking):
            with self.assertRaisesRegex(UnifierError, 'lock'):
                self.read()
        (self.db/'db.lck').unlink()
        (self.root/'outside').write_text(desc())
        (self.local/'base-1/desc').unlink()
        (self.local/'base-1/desc').symlink_to(self.root/'outside')
        with self.assertRaises(UnifierError):
            self.read()

    def test_corruption_and_limits_fail_closed(self):
        for text in ('', desc()+'\n%NAME%\nagain\n', desc().replace('2:1.4-3', 'bad version')):
            with self.subTest(text=text), self.assertRaises(UnifierError):
                inventory.parse_pacman_desc(text)
        with self.assertRaisesRegex(UnifierError, 'deadline'):
            self.read(seconds=0)
        with patch.object(inventory, 'MAX_ENTRY_BYTES', 8):
            with self.assertRaisesRegex(UnifierError, 'byte budget'):
                self.read()

    def test_multiline_other_fields_and_multiple_versions_are_serialized_natively(self):
        (self.local/'base-1/desc').write_text(desc()+'\n%LICENSE%\nMIT\nApache-2.0\n')
        (self.local/'z-1').mkdir()
        (self.local/'z-1/desc').write_text(desc('z', '1.0~beta+2-1'))
        self.assertEqual(self.read()[0], 'base 2:1.4-3\nz 1.0~beta+2-1\n')
