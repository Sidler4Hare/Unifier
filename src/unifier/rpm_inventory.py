"""Stopped, checkpointed RPM SQLite inventory without opening the source DB.

Only a bounded private copy is opened by SQLite; native rpm.hdr decodes header
blobs without a transaction set, guest executable, plugin or scriptlet. Native
POSIX locking and path/stat revalidation cover cooperative writers, not hostile
writers ignoring RPM's lock. A running instance is never accepted by the caller.
"""
import fcntl
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
import time

from unifier.errors import UnifierError
from unifier.package_inventory import (
    InventoryUnsupported, MAX_DATABASE_BYTES, MAX_INVENTORY_BYTES, MAX_PACKAGES,
    normalize_inventory,
)

DATABASE_PATHS = ('/usr/lib/sysimage/rpm', '/var/lib/rpm')
# A standard Fedora fixture contains a 1,402,996-byte native header. Keep a
# separate bounded header allowance within the unchanged 32 MiB DB budget.
MAX_HEADER_BYTES = 4 * 1024**2
INDEX_TABLES = ('Name', 'Basenames', 'Group', 'Requirename', 'Providename',
                'Conflictname', 'Obsoletename', 'Triggername', 'Dirnames',
                'Installtid', 'Sigmd5', 'Sha1header', 'Filetriggername',
                'Transfiletriggername', 'Recommendname', 'Suggestname',
                'Supplementname', 'Enhancename')


def rpm_decoder():
    try:
        import rpm
    except ImportError as exc:
        raise InventoryUnsupported('RPM inventory requires the host Python rpm module') from exc
    return rpm.hdr


def read_rpm_database(root_fd: int, *, seconds: float = 5.0) -> tuple[str, dict]:
    decode = rpm_decoder()
    deadline = time.monotonic() + seconds
    opened, directories, tracked = [], [], []
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK

    def check():
        if time.monotonic() >= deadline:
            raise UnifierError('RPM database read deadline exceeded')

    def fingerprint(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)

    def open_at(parent, name, directory=False):
        check()
        fd = os.open(name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
        opened.append(fd)
        info = os.fstat(fd)
        if not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)):
            raise UnifierError('RPM database path is not a regular file/directory')
        (directories if directory else tracked).append((parent, name, fd, fingerprint(info)))
        return fd

    def sidecars(db):
        observed = {}
        for name in ('rpmdb.sqlite-wal', 'rpmdb.sqlite-journal', 'rpmdb.sqlite-shm'):
            try:
                info = os.stat(name, dir_fd=db, follow_symlinks=False)
            except FileNotFoundError:
                observed[name] = None
                continue
            if not stat.S_ISREG(info.st_mode):
                raise UnifierError('RPM database journal indirection refused')
            if name != 'rpmdb.sqlite-shm' and info.st_size:
                raise UnifierError('RPM database has pending WAL/journal; native recovery required')
            observed[name] = fingerprint(info)
        return observed

    try:
        db = None
        selected = None
        for path in DATABASE_PATHS:
            parent = root_fd
            try:
                for part in path.strip('/').split('/'):
                    parent = open_at(parent, part, directory=True)
            except FileNotFoundError:
                continue
            db, selected = parent, path
            break
        if db is None:
            raise InventoryUnsupported('RPM standard SQLite database directory is absent')
        try:
            lock = open_at(db, '.rpm.lock')
            fcntl.lockf(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            raise UnifierError('RPM native database lock missing, busy or unavailable') from exc
        before_sidecars = sidecars(db)
        try:
            source = open_at(db, 'rpmdb.sqlite')
        except FileNotFoundError as exc:
            raise InventoryUnsupported('RPM database backend is not supported SQLite') from exc
        size = os.fstat(source).st_size
        if size > MAX_DATABASE_BYTES:
            raise UnifierError('RPM database exceeds byte budget')
        total = 0
        digest = hashlib.sha256()
        with tempfile.TemporaryDirectory(prefix='rpm-inventory-') as temporary:
            copy = Path(temporary)/'rpmdb.sqlite'
            with copy.open('xb') as stream:
                while True:
                    check()
                    chunk = os.read(source, min(65536, MAX_DATABASE_BYTES + 1 - total))
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DATABASE_BYTES:
                        raise UnifierError('RPM database exceeds byte budget')
                    digest.update(chunk)
                    stream.write(chunk)
            if total != size:
                raise UnifierError('RPM database changed during copy')
            # Immutable is safe only for our private copy, never the live source.
            connection = sqlite3.connect(copy.as_uri()+'?mode=ro&immutable=1', uri=True, timeout=0)
            try:
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
                connection.execute('PRAGMA trusted_schema=OFF')
                kind = connection.execute("SELECT type FROM sqlite_schema WHERE name='Packages'").fetchone()
                columns = [(row[1], row[2].upper()) for row in connection.execute('PRAGMA table_info(Packages)')]
                if kind != ('table',) or columns != [('hnum', 'INTEGER'), ('blob', 'BLOB')]:
                    raise InventoryUnsupported('RPM SQLite Packages schema is unsupported')
                # Native RPM opens these indexes even for -qa. Do not claim
                # equivalent support for a truncated primary-table-only DB.
                tables = {row[0].lower() for row in connection.execute(
                    "SELECT name FROM sqlite_schema WHERE type='table'")}
                if any(name.lower() not in tables for name in INDEX_TABLES):
                    raise InventoryUnsupported('RPM SQLite index schema is incomplete')
                for name in INDEX_TABLES:
                    fields = [row[1] for row in connection.execute(f'PRAGMA table_info("{name}")')]
                    if fields != ['key', 'hnum', 'idx']:
                        raise InventoryUnsupported('RPM SQLite index schema is unsupported')
                if connection.execute('PRAGMA quick_check(1)').fetchone() != ('ok',):
                    raise UnifierError('RPM SQLite database integrity check failed')
                rows, output_bytes = [], 0
                for hnum, length, blob in connection.execute(
                        'SELECT hnum, length(blob), CASE WHEN length(blob) <= ? THEN blob END FROM Packages ORDER BY hnum',
                        (MAX_HEADER_BYTES,)):
                    check()
                    if len(rows) >= MAX_PACKAGES or type(hnum) is not int or hnum <= 0:
                        raise UnifierError('RPM package count or identity exceeds contract')
                    if not isinstance(blob, bytes) or not 0 < length <= MAX_HEADER_BYTES:
                        raise UnifierError('RPM header exceeds byte budget or is corrupt '
                                           f'(row={hnum}, bytes={length}, limit={MAX_HEADER_BYTES})')
                    try:
                        header = decode(blob)
                        fields = [header[key] for key in ('name', 'version', 'release', 'arch')]
                    except Exception as exc:
                        raise UnifierError('RPM native header decoding failed') from exc
                    # Legacy imported-key headers have no ARCH tag. Native
                    # rpm -qa prints "(none)" for these, and inventories must
                    # retain that row rather than misclassify the DB as corrupt.
                    if fields[0] == 'gpg-pubkey' and fields[3] is None:
                        fields[3] = '(none)'
                    if any(not isinstance(value, str) or not 0 < len(value) <= 1024
                           or any(ord(c) < 33 or ord(c) > 126 for c in value) for value in fields):
                        raise UnifierError('RPM header identity is unsupported or corrupt')
                    name, version, release, arch = fields
                    row = f'{name}\t{version}-{release}.{arch}'
                    output_bytes += len(row.encode()) + 1
                    if output_bytes > MAX_INVENTORY_BYTES:
                        raise UnifierError('RPM inventory exceeds output budget')
                    rows.append(row)
                inventory = normalize_inventory('\n'.join(rows))
            except sqlite3.Error as exc:
                raise UnifierError('RPM SQLite read failed or exceeded deadline') from exc
            finally:
                connection.close()
        if sidecars(db) != before_sidecars:
            raise UnifierError('RPM database journal changed during observation')
        for parent, name, fd, before in directories + tracked:
            after = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISDIR(os.fstat(fd).st_mode):
                if (after.st_dev, after.st_ino) != before[:2]:
                    raise UnifierError('RPM database directory changed during observation')
            elif fingerprint(after) != before or fingerprint(os.fstat(fd)) != before:
                raise UnifierError('RPM database file changed during observation')
        check()
        return inventory, {'database': 'rpm-sqlite', 'method': 'locked-stopped-private-copy',
                           'native_lock': 'rpm-posix-shared', 'database_path': selected,
                           'database_bytes': total, 'database_sha256': digest.hexdigest(),
                           'package_count': len(rows), 'pending_wal': False,
                           'scope': 'stopped instance; standard RPM layout and cooperative native writers only'}
    finally:
        for fd in reversed(opened):
            os.close(fd)
