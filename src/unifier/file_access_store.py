"""Host-private policy and durable, lease-bound selected-file grants."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import time

from unifier.atomic_io import atomic_replace
from unifier.file_access import AccessError, AccessPolicy, canonical_path
from unifier.identity import validate_binding

ACTIVE = frozenset({'preparing', 'ready', 'revoking', 'needs-recovery'})
STATES = ACTIVE | {'released', 'failed'}


def _read(path, *, limit=2 * 1024**2):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit or info.st_uid != os.getuid():
            raise AccessError('ACCESS_STATE_INVALID', 'invalid host access state file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise AccessError('ACCESS_STATE_INVALID', 'host access state exceeds its budget')
        return json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise AccessError('ACCESS_STATE_INVALID', 'host access state is unreadable') from exc
    finally:
        os.close(descriptor)


def _private(directory):
    if directory.resolve() != directory.absolute():
        raise AccessError('ACCESS_STATE_INVALID', 'access state directory contains a symbolic link')
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise AccessError('ACCESS_STATE_INVALID', 'access state directory must be private to its host owner')


@contextmanager
def _lock(directory, name):
    _private(directory)
    fd = os.open(directory / name, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
            raise AccessError('ACCESS_STATE_INVALID', 'invalid access state lock')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write(path, value, *, limit=2 * 1024**2):
    payload = (json.dumps(value, ensure_ascii=False, sort_keys=True) + '\n').encode()
    if len(payload) > limit:
        raise AccessError('ACCESS_STATE_LIMIT', 'access record exceeds its serialized byte budget')
    atomic_replace(path, payload, prefix='.access-')


def reclaim_directory(parent_fd, directory, name, expected, *, missing_ok=True):
    """Remove only the recorded directory, with every traversal anchored to its FD."""
    try:
        selected_fd = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
    except FileNotFoundError:
        if not missing_ok:
            raise AccessError('ACCESS_OBJECT_MISSING', 'snapshot disappeared without confirmed cleanup')
        return
    except NotADirectoryError as exc:
        raise AccessError('ACCESS_OBJECT_CHANGED', f'snapshot directory identity changed: {directory / name}') from exc
    try:
        info = os.fstat(selected_fd)
        if [info.st_dev, info.st_ino] != expected:
            raise AccessError('ACCESS_OBJECT_CHANGED', f'snapshot directory identity changed: {directory / name}')
        with os.scandir(selected_fd) as children:
            for child in children:
                if child.is_dir(follow_symlinks=False):
                    shutil.rmtree(child.name, dir_fd=selected_fd)
                else:
                    os.unlink(child.name, dir_fd=selected_fd)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if [current.st_dev, current.st_ino] != expected:
            raise AccessError('ACCESS_OBJECT_CHANGED', 'snapshot directory moved during cleanup')
        os.rmdir(name, dir_fd=parent_fd)
    finally:
        os.close(selected_fd)


class AccessStore:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root / '.lab/state/file-access'
        self.policy_path = self.directory / 'policy.json'

    def policy(self):
        if self.directory.is_symlink() or self.directory.exists():
            _private(self.directory)
        try:
            value = _read(self.policy_path, limit=65536)
        except FileNotFoundError:
            return AccessPolicy()
        return AccessPolicy.from_dict(value)

    def set_policy(self, value, *, environments=()):
        policy = AccessPolicy.from_dict(value)
        # A software deny cannot hide a path already exposed by a static mount.
        for record in environments:
            self.check_mounts(record.get('shared_mounts', []), policy=policy)
        with _lock(self.directory, 'policy.lock'):
            current = self.policy()
            if policy.revision != current.revision:
                raise AccessError('ACCESS_POLICY_STALE', 'policy revision changed; read the current policy first')
            updated = {**policy.as_dict(), 'revision': current.revision + 1}
            _write(self.policy_path, updated, limit=65536)
            return updated

    def check_mounts(self, mounts, *, policy=None):
        policy = policy or self.policy()
        for mount in mounts:
            path = canonical_path(mount['host'])
            if self.directory.is_relative_to(path):
                raise AccessError('ACCESS_STATE_EXPOSED', 'a guest share would expose host file access authority')
            policy.require(path, 'browse', directory=True)
            if 'ro' not in mount.get('options', '').split(','):
                policy.require(path, 'edit', directory=True)

    def ledgers(self):
        directory = self.directory / 'leases'
        if not directory.exists():
            return []
        results = []
        total = 0
        for path in sorted(directory.glob('*.json')):
            if len(results) >= 4096:
                raise AccessError('ACCESS_STATE_LIMIT', 'too many access lease records')
            total += path.lstat().st_size
            if total > 16 * 1024**2:
                raise AccessError('ACCESS_STATE_LIMIT', 'lease listing exceeds its byte budget; select one lease')
            value = validate_ledger(_read(path))
            results.append(value)
        return results

    def ledger(self, lease_id):
        if not isinstance(lease_id, str) or not re.fullmatch(r'[0-9a-f]{32}', lease_id):
            raise AccessError('ACCESS_STATE_INVALID', 'invalid access lease id')
        directory = self.directory / 'leases'
        value = validate_ledger(_read(directory / (lease_id + '.json')))
        if value['lease_id'] != lease_id:
            raise AccessError('ACCESS_STATE_IDENTITY', 'access lease filename disagrees with its record')
        return GrantLedger(directory, lease_id, value['environment_binding'],
                           value['selection_directory'], value['selection_identity'])


def validate_ledger(value):
    if not isinstance(value, dict) or value.get('schema_version') != 1 or type(value['schema_version']) is not int:
        raise AccessError('ACCESS_STATE_INVALID', 'unsupported grant ledger schema')
    if not isinstance(value.get('lease_id'), str) or not re.fullmatch(r'[0-9a-f]{32}', value['lease_id']):
        raise AccessError('ACCESS_STATE_INVALID', 'invalid grant lease identity')
    if value.get('environment_binding') is not None:
        validate_binding(value['environment_binding'])
    canonical_path(value.get('selection_directory'))
    ident = value.get('selection_identity')
    if not isinstance(ident, list) or len(ident) != 2 or any(type(x) is not int or x < 0 for x in ident):
        raise AccessError('ACCESS_STATE_INVALID', 'invalid selection directory identity')
    grants = value.get('grants')
    if not isinstance(grants, dict) or len(grants) > 4096:
        raise AccessError('ACCESS_STATE_INVALID', 'invalid grant collection')
    for key, grant in grants.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[0-9a-f]{32}', key) or not isinstance(grant, dict)
                or grant.get('state') not in STATES or type(grant.get('bytes')) is not int
                or grant['bytes'] < 0 or type(grant.get('objects')) is not int or grant['objects'] < 0):
            raise AccessError('ACCESS_STATE_INVALID', 'invalid grant record')
    return value


class GrantLedger:
    def __init__(self, directory, lease_id, binding, selection_directory, selection_identity):
        if not re.fullmatch(r'[0-9a-f]{32}', lease_id):
            raise AccessError('ACCESS_STATE_INVALID', 'invalid grant lease identity')
        if binding is not None:
            validate_binding(binding)
        self.directory = Path(directory)
        self.path = self.directory / (lease_id + '.json')
        self.header = {'schema_version': 1, 'lease_id': lease_id, 'environment_binding': binding,
                       'selection_directory': str(selection_directory),
                       'selection_identity': list(selection_identity)}
        with self.locked():
            try:
                self.read()
            except FileNotFoundError:
                fields = Path(f'/proc/{os.getpid()}/stat').read_text().rsplit(')', 1)[1].split()
                _write(self.path, {**self.header, 'grants': {}, 'owner': {
                    'pid': os.getpid(), 'starttime': int(fields[19]),
                    'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}})

    def locked(self):
        return _lock(self.directory, self.path.stem + '.lock')

    def read(self):
        value = validate_ledger(_read(self.path))
        if any(value.get(k) != v for k, v in self.header.items()):
            raise AccessError('ACCESS_STATE_IDENTITY', 'grant ledger belongs to a different environment or directory')
        return value

    def usage(self):
        with self.locked():
            grants = self.read()['grants'].values()
            return (sum(g['bytes'] for g in grants if g['state'] in ACTIVE),
                    sum(g['objects'] for g in grants if g['state'] in ACTIVE))

    def reserve(self, grant_id, *, size, objects, byte_limit, object_limit, decisions):
        if not re.fullmatch(r'[0-9a-f]{32}', grant_id) or any(type(x) is not int or x < 0 for x in (size, objects)):
            raise AccessError('ACCESS_STATE_INVALID', 'invalid grant reservation')
        with self.locked():
            value = self.read()
            grants = value['grants']
            if grant_id in grants:
                raise AccessError('ACCESS_GRANT_EXISTS', 'grant identity is already reserved')
            if any(g['state'] in ('preparing', 'revoking', 'needs-recovery') for g in grants.values()):
                raise AccessError('ACCESS_RECOVERY_REQUIRED', 'an earlier delivery needs reconciliation')
            used = sum(g['bytes'] for g in grants.values() if g['state'] in ACTIVE)
            count = sum(g['objects'] for g in grants.values() if g['state'] in ACTIVE)
            if used + size > byte_limit or count + objects > object_limit:
                raise AccessError('ACCESS_QUOTA', 'selection lease disk or object budget exhausted')
            if len(grants) >= 4096:
                raise AccessError('ACCESS_QUOTA', 'selection lease grant record budget exhausted')
            grants[grant_id] = {'state': 'preparing', 'bytes': size, 'objects': objects,
                                'decisions': decisions, 'created_at': time.time()}
            # Leave headroom for materialized paths, fingerprints and terminal states.
            _write(self.path, value, limit=1024**2)

    def transition(self, grant_id, state, **extra):
        allowed = {'preparing': {'ready', 'revoking', 'failed', 'needs-recovery'},
                   'ready': {'revoking', 'needs-recovery'},
                   'revoking': {'released', 'needs-recovery'},
                   'needs-recovery': {'revoking'}, 'released': set(), 'failed': set()}
        with self.locked():
            value = self.read()
            grant = value['grants'].get(grant_id)
            if grant is None or state != grant['state'] and state not in allowed[grant['state']]:
                raise AccessError('ACCESS_STATE_TRANSITION', 'invalid grant state transition')
            if set(extra) & {'state', 'bytes', 'objects'}:
                raise AccessError('ACCESS_STATE_TRANSITION', 'cannot rewrite reservation accounting')
            grant.update(extra, state=state, updated_at=time.time())
            _write(self.path, value)

    def release_all(self):
        """Called only after the owner proved the complete selection tree was removed."""
        with self.locked():
            value = self.read()
            for grant in value['grants'].values():
                if grant['state'] in ACTIVE:
                    grant.update(state='released', updated_at=time.time(), cleanup='lease-directory-removed')
            _write(self.path, value)
