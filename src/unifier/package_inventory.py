"""Independent inventory contract and bounded, locked Debian DB observation.

The wire inventory stays identical to dpkg-query's ${Package}\t${Version}.
Architecture and status are validated as database fields, not appended to the
historical inventory serialization. No target binaries are executed here.
"""
from dataclasses import asdict, dataclass
import datetime as dt
import fcntl
import hashlib
import os
import re
import stat
import time

from unifier.errors import UnifierError
from unifier.identity import binding_for


MAX_DATABASE_BYTES = 32 * 1024**2
MAX_INVENTORY_BYTES = 4 * 1024**2
MAX_PACKAGES = 50000


def normalize_inventory(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise UnifierError("package inventory is empty")
    return "\n".join(sorted(lines)) + "\n"


@dataclass(frozen=True)
class InventoryObservation:
    status: str
    reason: str | None
    binding: dict
    target: str
    database: str
    method: str
    observed_at: str
    consistency: dict
    inventory: str | None = None

    def evidence(self) -> dict:
        value = asdict(self)
        text = value.pop('inventory')
        value['sha256'] = hashlib.sha256(text.encode()).hexdigest() if text is not None else None
        return value


def observation(record: dict, *, inventory=None, status='supported', reason=None,
                database='dpkg', method='locked-stopped-database', consistency=None):
    text = normalize_inventory(inventory) if inventory is not None else None
    if text is not None and len(text.encode()) > MAX_INVENTORY_BYTES:
        raise UnifierError('read-only inventory exceeds output budget')
    return InventoryObservation(
        status, reason, binding_for(record), record['target'], database, method,
        dt.datetime.now(dt.timezone.utc).isoformat(), consistency or {}, text)


def parse_dpkg_status(text: str) -> str:
    """Reject unsupported/corrupt stanzas rather than inventing package values."""
    rows = []
    identities = set()
    for stanza in re.split(r'\n[ \t]*\n', text):
        if not stanza.strip():
            continue
        fields = {}
        previous = None
        for line in stanza.splitlines():
            if line.startswith((' ', '\t')):
                if previous is None:
                    raise UnifierError('invalid dpkg field continuation')
                if previous in {'package', 'version', 'architecture', 'status'}:
                    raise UnifierError('multiline dpkg identity field is unsupported')
                continue
            name, separator, value = line.partition(':')
            key = name.lower()
            if not separator or key in fields:
                raise UnifierError('invalid or duplicate dpkg field')
            fields[key] = value.strip()
            previous = key
        name = fields.get('package', '')
        version = fields.get('version', '')
        architecture = fields.get('architecture', '')
        state = fields.get('status', '').split()
        if (not re.fullmatch(r'[a-z0-9][a-z0-9+.-]*', name)
                or len(name) > 256 or not re.fullmatch(r'[a-z0-9][a-z0-9-]*', architecture)
                or len(state) != 3
                or state[0] not in {'unknown', 'install', 'hold', 'deinstall', 'purge'}
                or state[1] not in {'ok', 'reinstreq'}
                or state[2] not in {'installed', 'config-files', 'half-installed', 'unpacked',
                                    'half-configured', 'triggers-awaited', 'triggers-pending'}
                or not version or len(version) > 256
                or not re.fullmatch(r'[0-9A-Za-z.+:~\-]+', version)):
            raise UnifierError('dpkg identity/status unsupported or corrupt')
        key = (name, architecture)
        if key in identities:
            raise UnifierError('duplicate dpkg package architecture')
        identities.add(key)
        rows.append(f'{name}\t{version}')
        if len(rows) > MAX_PACKAGES:
            raise UnifierError('dpkg package count exceeds budget')
    return normalize_inventory('\n'.join(rows))


def read_dpkg_database(root_fd: int, *, seconds: float = 5.0) -> tuple[str, dict]:
    """Hold dpkg's POSIX read lock; refuse journals and all path indirections.

    Native dpkg writers use the corresponding exclusive POSIX lock. External
    writers ignoring that protocol are not controlled; inode/stat checks detect
    ordinary replacement during this observation, not arbitrary hostile writes.
    """
    deadline = time.monotonic() + seconds
    opened = []
    directories = []
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
    def open_at(parent, name, *, directory=False):
        fd = os.open(name, flags | (os.O_DIRECTORY if directory else 0), dir_fd=parent)
        opened.append(fd)
        mode = os.fstat(fd).st_mode
        if not (stat.S_ISDIR(mode) if directory else stat.S_ISREG(mode)):
            raise UnifierError('database path is not a regular file/directory')
        return fd
    def fingerprint(value):
        return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)
    try:
        parent = root_fd
        for part in ('var', 'lib', 'dpkg'):
            child = open_at(parent, part, directory=True)
            directories.append((parent, part, child))
            parent = child
        db = parent
        lock_fd = open_at(db, 'lock')
        try:
            fcntl.lockf(lock_fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as exc:
            raise UnifierError('dpkg database busy or read lock unavailable') from exc
        updates = open_at(db, 'updates', directory=True)
        with os.scandir(updates) as entries:
            if next(entries, None) is not None:
                raise UnifierError('dpkg database has pending updates; recover it first')
        status_fd = open_at(db, 'status')
        before = os.fstat(status_fd)
        if before.st_size > MAX_DATABASE_BYTES:
            raise UnifierError('dpkg database exceeds byte budget')
        data = bytearray()
        while True:
            if time.monotonic() >= deadline:
                raise UnifierError('dpkg database read deadline exceeded')
            chunk = os.read(status_fd, min(65536, MAX_DATABASE_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_DATABASE_BYTES:
                raise UnifierError('dpkg database exceeds byte budget')
        inventory = parse_dpkg_status(data.decode('utf-8', errors='strict'))
        after = os.stat('status', dir_fd=db, follow_symlinks=False)
        current_lock = os.stat('lock', dir_fd=db, follow_symlinks=False)
        if (fingerprint(before) != fingerprint(os.fstat(status_fd))
                or fingerprint(before) != fingerprint(after)
                or (current_lock.st_dev, current_lock.st_ino) !=
                   (os.fstat(lock_fd).st_dev, os.fstat(lock_fd).st_ino)):
            raise UnifierError('dpkg database changed during observation')
        with os.scandir(updates) as entries:
            if next(entries, None) is not None:
                raise UnifierError('dpkg database updates changed during observation')
        for parent, name, child in directories + [(db, 'updates', updates)]:
            current = os.stat(name, dir_fd=parent, follow_symlinks=False)
            pinned = os.fstat(child)
            if (current.st_dev, current.st_ino) != (pinned.st_dev, pinned.st_ino):
                raise UnifierError('dpkg database directory changed during observation')
        if time.monotonic() >= deadline:
            raise UnifierError('dpkg database read deadline exceeded')
        return inventory, {'native_lock': 'dpkg-posix-shared', 'pending_updates': False,
                           'database_bytes': len(data), 'status_sha256': hashlib.sha256(data).hexdigest(),
                           'scope': 'stopped container; cooperative native writers only'}
    finally:
        for fd in reversed(opened):
            os.close(fd)
