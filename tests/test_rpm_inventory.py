"""Private SQLite fixtures and native RPM header/query equivalence; no installs."""
from contextlib import closing
import fcntl
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from unifier.errors import UnifierError
from unifier import rpm_inventory as inventory
try:
    import rpm
except ImportError:
    rpm = None


def create_rpm_database(root):
    db = root/'usr/lib/sysimage/rpm'
    db.mkdir(parents=True)
    (db/'.rpm.lock').touch()
    with closing(sqlite3.connect(db/'rpmdb.sqlite')) as connection, connection:
        connection.execute('CREATE TABLE Packages (hnum INTEGER PRIMARY KEY AUTOINCREMENT, blob BLOB NOT NULL)')
        # RPM's documented primary/header and Name-index schema. Build it
        # directly: host rpm --initdb may be forbidden by host MAC policy.
        # These are RPM's dbiTags (lib/rpmdb.cc); queries open all indexes.
        for name in ('Name', 'Basenames', 'Group', 'Requirename', 'Providename',
                     'Conflictname', 'Obsoletename', 'Triggername', 'Dirnames',
                     'Installtid', 'Sigmd5', 'Sha1header', 'Filetriggername',
                     'Transfiletriggername', 'Recommendname', 'Suggestname',
                     'Supplementname', 'Enhancename'):
            key_type = 'BLOB' if name in ('Installtid', 'Sigmd5') else 'TEXT'
            connection.execute(f'CREATE TABLE "{name}" (key {key_type} NOT NULL, hnum INTEGER NOT NULL, idx INTEGER NOT NULL)')
        for name, version, release, arch in [('base', '2.0', '3.fc44', 'x86_64'), ('base', '1.9', '1.fc44', 'i686')]:
            header = rpm.hdr()
            for key, value in zip(('name', 'version', 'release', 'arch'), (name, version, release, arch)):
                header[key] = value
            header['epoch'] = 7  # The established rpm query format excludes epoch.
            cursor = connection.execute('INSERT INTO Packages(blob) VALUES (?)', (header.unload(),))
            connection.execute('INSERT INTO Name VALUES (?, ?, 0)', (name, cursor.lastrowid))
    return db


@unittest.skipIf(rpm is None, 'native host Python rpm decoder unavailable')
class RpmInventory(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = create_rpm_database(self.root)

    def read(self, **options):
        fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
        try:
            return inventory.read_rpm_database(fd, **options)
        finally:
            os.close(fd)

    def test_native_header_inventory_and_source_bytes_unchanged(self):
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.db.iterdir() if p.is_file()}
        text, proof = self.read()
        self.assertEqual(text, 'base\t1.9-1.fc44.i686\nbase\t2.0-3.fc44.x86_64\n')
        self.assertEqual(proof['native_lock'], 'rpm-posix-shared')
        self.assertEqual(before, {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.db.iterdir() if p.is_file()})
        if shutil.which('rpm'):
            # Query a different private copy; never direct the native tool at
            # the source fixture or any host/container package database.
            native = self.root/'native-copy'
            shutil.copytree(self.db, native)
            result = subprocess.run(['rpm', '--noplugins', '--dbpath', str(native),
                                     '-qa', '--qf', '%{NAME}\t%{VERSION}-%{RELEASE}.%{ARCH}\n'],
                                    text=True, capture_output=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(inventory.normalize_inventory(result.stdout), text)

    def test_pending_wal_and_missing_lock_refuse_without_source_changes(self):
        wal = self.db/'rpmdb.sqlite-wal'
        wal.write_bytes(b'pending')
        with self.assertRaisesRegex(UnifierError, 'WAL'):
            self.read()
        self.assertEqual(wal.read_bytes(), b'pending')
        wal.unlink()
        (self.db/'.rpm.lock').unlink()
        with self.assertRaisesRegex(UnifierError, 'lock'):
            self.read()
        self.assertFalse((self.db/'.rpm.lock').exists())

    def test_valid_large_native_header_is_not_misclassified_as_corrupt(self):
        with closing(sqlite3.connect(self.db/'rpmdb.sqlite')) as connection, connection:
            hnum, blob = connection.execute('SELECT hnum, blob FROM Packages ORDER BY hnum LIMIT 1').fetchone()
            header = rpm.hdr(blob)
            header['description'] = 'x' * (1536 * 1024)
            payload = header.unload()
            self.assertGreater(len(payload), 1024**2)
            connection.execute('UPDATE Packages SET blob=? WHERE hnum=?', (payload, hnum))
        text, evidence = self.read()
        self.assertEqual(evidence['package_count'], 2)
        self.assertIn('base\t2.0-3.fc44.x86_64\n', text)

    def test_legacy_public_key_without_arch_matches_native_query(self):
        header = rpm.hdr()
        for key, value in [('name','gpg-pubkey'),('version','a'*40),('release','6786af3b')]:
            header[key] = value
        with closing(sqlite3.connect(self.db/'rpmdb.sqlite')) as connection, connection:
            cursor = connection.execute('INSERT INTO Packages(blob) VALUES (?)', (header.unload(),))
            hnum = cursor.lastrowid
            connection.execute('INSERT INTO Name VALUES (?, ?, 0)', ('gpg-pubkey', hnum))
        text, _ = self.read()
        self.assertIn('gpg-pubkey\t'+'a'*40+'-6786af3b.(none)\n', text)
        if shutil.which('rpm'):
            native = self.root/'native-key-copy'
            shutil.copytree(self.db, native)
            result = subprocess.run(['rpm','--noplugins','--dbpath',str(native),'-qa',
                                     '--qf','%{NAME}\t%{VERSION}-%{RELEASE}.%{ARCH}\n'],
                                    text=True,capture_output=True,timeout=5)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(inventory.normalize_inventory(result.stdout),text)
        header = rpm.hdr()
        for key, value in [('name','broken-ordinary-package'),('version','1'),('release','1')]:
            header[key] = value
        with closing(sqlite3.connect(self.db/'rpmdb.sqlite')) as connection, connection:
            connection.execute('UPDATE Packages SET blob=? WHERE hnum=?',(header.unload(),hnum))
        with self.assertRaisesRegex(UnifierError,'identity'):
            self.read()

    def test_native_writer_lock_in_another_process_refuses(self):
        with (self.db/'.rpm.lock').open('r+') as lock:
            fcntl.lockf(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = subprocess.run([sys.executable, '-c', '''
import os, sys
from unifier.rpm_inventory import read_rpm_database
from unifier.errors import UnifierError
fd = os.open(sys.argv[1], os.O_RDONLY|os.O_DIRECTORY)
try:
    read_rpm_database(fd)
except UnifierError as exc:
    assert 'lock' in str(exc)
else:
    raise AssertionError('native RPM writer lock ignored')
finally:
    os.close(fd)
''', str(self.root)], text=True, capture_output=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_symlinks_unknown_schema_bad_header_and_budget_refuse(self):
        source = self.db/'rpmdb.sqlite'
        original = source.read_bytes()
        source.unlink()
        (self.root/'outside').write_bytes(original)
        source.symlink_to(self.root/'outside')
        with self.assertRaises(OSError):
            self.read()
        source.unlink()
        source.write_bytes(original)
        with patch.object(inventory, 'MAX_DATABASE_BYTES', 8):
            with self.assertRaisesRegex(UnifierError, 'byte budget'):
                self.read()
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute("UPDATE Packages SET blob=X'00'")
        with self.assertRaisesRegex(UnifierError, 'header decoding'):
            self.read()
        source.write_bytes(original)
        with closing(sqlite3.connect(source)) as connection, connection:
            connection.execute('DROP TABLE Packages')
            connection.execute('CREATE TABLE Packages(other TEXT)')
        with self.assertRaisesRegex(UnifierError, 'schema'):
            self.read()

    def test_source_replacement_or_new_journal_during_decode_refuses(self):
        native = inventory.rpm_decoder()
        def replacing(blob):
            (self.db/'rpmdb.sqlite-wal').write_bytes(b'changed')
            return native(blob)
        with patch.object(inventory, 'rpm_decoder', return_value=replacing):
            with self.assertRaisesRegex(UnifierError, 'WAL'):
                self.read()
        (self.db/'rpmdb.sqlite-wal').unlink()
        replaced = False
        def swapping(blob):
            nonlocal replaced
            if not replaced:
                source = self.db/'rpmdb.sqlite'
                replacement = self.db/'replacement'
                replacement.write_bytes(source.read_bytes())
                replacement.replace(source)
                replaced = True
            return native(blob)
        with patch.object(inventory, 'rpm_decoder', return_value=swapping):
            with self.assertRaisesRegex(UnifierError, 'changed'):
                self.read()

    def test_missing_native_index_is_not_reported_as_supported(self):
        with closing(sqlite3.connect(self.db/'rpmdb.sqlite')) as connection, connection:
            connection.execute('DROP TABLE Name')
        with self.assertRaisesRegex(UnifierError, 'index schema'):
            self.read()

    def test_legacy_real_directory_supported_but_directory_links_refuse(self):
        legacy = self.root/'var/lib/rpm'
        legacy.parent.mkdir(parents=True)
        self.db.rename(legacy)
        self.assertEqual(self.read()[1]['database_path'], '/var/lib/rpm')
        self.db.symlink_to(legacy)
        with self.assertRaises(OSError):
            self.read()

    def test_deadline_and_header_output_limits_refuse(self):
        with self.assertRaisesRegex(UnifierError, 'deadline'):
            self.read(seconds=0)
        with patch.object(inventory, 'MAX_HEADER_BYTES', 1):
            with self.assertRaisesRegex(UnifierError, 'header exceeds'):
                self.read()
        with patch.object(inventory, 'MAX_INVENTORY_BYTES', 1):
            with self.assertRaisesRegex(UnifierError, 'output budget'):
                self.read()
