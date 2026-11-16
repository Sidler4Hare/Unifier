"""Bounded, change-detected read of a stopped instance's pacman local database.

Pacman has no shared read lock, so ``db.lck`` is only probed (never created,
removed or held) and the caller must already hold the lifecycle lock with the
instance confirmed stopped. The evidence is therefore labelled
``stopped-database-with-change-detection``: per-directory/per-file stat
fingerprints plus a bounded re-listing detect ordinary replacement or edits
during this observation. It is not an atomic running-state snapshot and it does
not defend against external writers that deliberately bypass ``db.lck``.
"""
import hashlib
import os
import re
import stat
import time

from unifier.errors import UnifierError
from unifier.package_inventory import normalize_inventory


MAX_DATABASE_BYTES = 32 * 1024**2
MAX_INVENTORY_BYTES = 4 * 1024**2
MAX_PACKAGES = 50000
MAX_ENTRY_BYTES = 1024**2
MAX_LIST_ENTRIES = MAX_PACKAGES + 1024
READ_CHUNK = 65536
MAX_NAME_LENGTH = 255
MAX_VERSION_LENGTH = 1024

NAME_PATTERN = r'[A-Za-z0-9@._+-]+'
FIELD_PATTERN = r'[A-Z][A-Z0-9_]*'


def parse_pacman_desc(text: str, entry: str = '<desc>') -> tuple[str, str]:
    """Return (name, version) from one pacman ``desc`` file or refuse.

    Sections are ``%FIELD%`` headers terminated by a blank line. NAME and
    VERSION must each appear exactly once with exactly one non-empty value;
    every other field may hold any number of values.
    """
    if '\0' in text:
        raise UnifierError(f'pacman desc {entry} contains NUL')
    headers: dict[str, int] = {}
    values: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if not line.strip():
            current = None
            continue
        if line.startswith('%'):
            key = line[1:-1] if line.endswith('%') else ''
            if not re.fullmatch(FIELD_PATTERN, key):
                raise UnifierError(f'pacman desc {entry} has a malformed field header')
            headers[key] = headers.get(key, 0) + 1
            values.setdefault(key, [])
            current = key
            continue
        if current is None:
            raise UnifierError(f'pacman desc {entry} has a value outside a field')
        values[current].append(line)
    for key in ('NAME', 'VERSION'):
        if headers.get(key) != 1 or len(values.get(key, [])) != 1:
            raise UnifierError(f'pacman desc {entry} {key} must appear exactly once')
    name = values['NAME'][0].strip()
    version = values['VERSION'][0].strip()
    if not re.fullmatch(NAME_PATTERN, name) or len(name) > MAX_NAME_LENGTH:
        raise UnifierError(f'pacman desc {entry} package name is empty or unsupported')
    if (not version or len(version) > MAX_VERSION_LENGTH or '\0' in version
            or any(character.isspace() for character in version)):
        raise UnifierError(f'pacman desc {entry} package version is empty or unsupported')
    return name, version


def read_pacman_database(root_fd: int, *, seconds: float = 5.0) -> tuple[str, dict]:
    """Read ``var/lib/pacman/local/*/desc`` under a stopped instance.

    Only path components below the caller-provided root are opened, each layer
    with ``O_NOFOLLOW``/``O_NONBLOCK``/``O_CLOEXEC`` and a type check, so
    symlinks, FIFOs and other indirections are refused rather than followed.
    Byte, package, listing and output budgets are enforced and every fd opened
    here is closed. Raises UnifierError (callers may also handle OSError).
    """
    deadline = time.monotonic() + seconds
    opened = []
    directories = []
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK

    def check():
        if time.monotonic() >= deadline:
            raise UnifierError('pacman database read deadline exceeded')

    def fingerprint(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)

    def open_at(parent, name, *, directory=False, tracked=True):
        try:
            fd = os.open(name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
        except OSError as exc:
            raise UnifierError(
                f'pacman database path {name} refused (missing path or symlink indirection)'
            ) from exc
        if tracked:
            opened.append(fd)
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            if not tracked:
                os.close(fd)
            raise UnifierError(f'pacman database path {name} is not a regular file/directory')
        return fd

    def listing(directory_fd):
        names = []
        with os.scandir(directory_fd) as entries:
            for entry in entries:
                names.append((entry.name, entry.is_dir(follow_symlinks=False)))
                check()
                if len(names) > MAX_LIST_ENTRIES:
                    raise UnifierError('pacman local directory listing exceeds budget')
        return sorted(names)

    def lock_present(relative):
        try:
            os.stat(relative, dir_fd=db, follow_symlinks=False)
        except (FileNotFoundError, NotADirectoryError):
            return False
        except OSError as exc:
            raise UnifierError('pacman database lock probe failed') from exc
        return True

    try:
        parent = root_fd
        for part in ('var', 'lib', 'pacman'):
            child = open_at(parent, part, directory=True)
            directories.append((parent, part, child))
            parent = child
            check()
        db = parent
        if lock_present('db.lck'):
            raise UnifierError('pacman database lock db.lck is present; refuse to read')
        check()
        local = open_at(db, 'local', directory=True)
        first = listing(local)
        if len(first) > MAX_PACKAGES + 1:
            raise UnifierError('pacman package count exceeds budget')
        total = 0
        digest = hashlib.sha256()
        rows = []
        names = set()
        pinned_directories = []
        metadata = None
        for entry_name, is_directory in first:
            check()
            if entry_name == 'ALPM_DB_VERSION':
                version_fd = open_at(local, entry_name)
                before = fingerprint(os.fstat(version_fd))
                version = os.read(version_fd, 33)
                if version.strip() != b'9' or len(version) > 32:
                    raise UnifierError('pacman database version is unsupported')
                metadata = (version_fd, before)
                digest.update(version)
                total += len(version)
                continue
            if not is_directory:
                raise UnifierError(f'pacman local contains a non-directory entry: {entry_name}')
            handle = []
            try:
                entry_fd = open_at(local, entry_name, directory=True, tracked=False)
                handle.append(entry_fd)
                dir_fingerprint = fingerprint(os.fstat(entry_fd))
                desc_fd = open_at(entry_fd, 'desc', tracked=False)
                handle.append(desc_fd)
                before = os.fstat(desc_fd)
                if before.st_size > MAX_ENTRY_BYTES:
                    raise UnifierError('pacman desc file exceeds byte budget')
                data = bytearray()
                while True:
                    check()
                    room = MAX_ENTRY_BYTES + 1 - len(data)
                    chunk = os.read(desc_fd, min(READ_CHUNK, max(room, 0)))
                    if not chunk:
                        break
                    data.extend(chunk)
                    if len(data) > MAX_ENTRY_BYTES:
                        raise UnifierError('pacman desc file exceeds byte budget')
                after = os.fstat(desc_fd)
                relinked = os.stat('desc', dir_fd=entry_fd, follow_symlinks=False)
                if (fingerprint(before) != fingerprint(after)
                        or (relinked.st_dev, relinked.st_ino) != (after.st_dev, after.st_ino)):
                    raise UnifierError('pacman desc changed during read')
                total += len(data)
                if total > MAX_DATABASE_BYTES:
                    raise UnifierError('pacman database exceeds byte budget')
                digest.update(bytes(data))
                name, version = parse_pacman_desc(data.decode('utf-8'), entry_name)
                if name in names:
                    raise UnifierError(f'duplicate pacman package: {name}')
                names.add(name)
                rows.append(f'{name} {version}')
                pinned_directories.append((entry_name, dir_fingerprint, fingerprint(after)))
            finally:
                for fd in reversed(handle):
                    os.close(fd)
            check()
        if len(rows) > MAX_PACKAGES:
            raise UnifierError('pacman package count exceeds budget')
        inventory = normalize_inventory('\n'.join(rows))
        if len(inventory.encode()) > MAX_INVENTORY_BYTES:
            raise UnifierError('read-only inventory exceeds output budget')
        check()
        if lock_present('db.lck'):
            raise UnifierError('pacman database lock db.lck appeared during observation')
        if listing(local) != first:
            raise UnifierError('pacman local listing changed during observation')
        for entry_name, dir_fingerprint, desc_fingerprint in pinned_directories:
            check()
            current = os.stat(entry_name, dir_fd=local, follow_symlinks=False)
            if fingerprint(current) != dir_fingerprint:
                raise UnifierError('pacman package directory changed during observation')
            entry_fd = open_at(local, entry_name, directory=True, tracked=False)
            try:
                current = os.stat('desc', dir_fd=entry_fd, follow_symlinks=False)
                if fingerprint(current) != desc_fingerprint:
                    raise UnifierError('pacman desc changed during observation')
            finally:
                os.close(entry_fd)
        if metadata is not None:
            version_fd, before = metadata
            if (fingerprint(os.fstat(version_fd)) != before or
                    fingerprint(os.stat('ALPM_DB_VERSION', dir_fd=local, follow_symlinks=False)) != before):
                raise UnifierError('pacman database version changed during observation')
        for parent_fd, name, child in directories + [(db, 'local', local)]:
            current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            pinned = os.fstat(child)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise UnifierError('pacman database directory changed during observation')
        check()
        return inventory, {
            'method': 'stopped-database-with-change-detection',
            'database': 'pacman',
            'scope': 'stopped instance while the caller holds the lifecycle lock',
            'native_lock': 'db.lck probed only; never created, held or removed',
            'atomicity': ('not an atomic running-state snapshot; stat fingerprints and a bounded '
                          're-listing detect ordinary replacement or edits during observation'),
            'uncontrolled_writers': 'external writers that deliberately bypass db.lck are not defended',
            'database_bytes': total,
            'package_count': len(rows),
            'directory_entries': len(first),
            'database_sha256': digest.hexdigest(),
            'inventory_sha256': hashlib.sha256(inventory.encode()).hexdigest(),
        }
    finally:
        for fd in reversed(opened):
            os.close(fd)
