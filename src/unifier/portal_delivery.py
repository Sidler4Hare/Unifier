"""Deliver only selections authorized by a successful host Portal response.

The bridge owns authorization; this module handles verified static mappings
and bounded read-only snapshots. It never grants a parent-directory view.
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import uuid

from unifier.host_share_paths import open_host_share_path
from unifier.host_share_policy import HostSharePolicy
from unifier.file_access import AccessPolicy, AccessError, decide_selection
from unifier.file_access import local_path, mapped_path
from unifier.file_access_store import AccessStore, GrantLedger, reclaim_directory


DeliveryError = AccessError


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class DeliveredPaths(list):
    """Private broker result; the transport serializes only its list of URIs."""
    def __init__(self, paths, grant_id):
        super().__init__(paths)
        self.grant_id = grant_id


class SelectionStore:
    """Lease-scoped snapshots; every batch is all-or-nothing and bounded."""
    def __init__(self, directory, guest_directory, shares=(), *, emit=None,
                 max_file_bytes=512*1024**2, max_session_bytes=1024**3,
                 access_root=None, environment_binding=None, lease_id=None):
        self.directory = Path(tempfile.mkdtemp(prefix='selected-', dir=directory))
        self.guest = Path(guest_directory) / self.directory.name
        self.root_fd = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.root_identity = os.fstat(self.root_fd)
        self.shares = list(shares)
        self.emit = emit or (lambda **values: None)
        self.max_file_bytes = max_file_bytes
        self.max_session_bytes = max_session_bytes
        self.used = 0
        self.batches = 0
        self.grants = {}
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.closed = False
        self.access_store = AccessStore(access_root) if access_root is not None else None
        # Standalone offline callers own their enclosing temporary directory.
        # Production always supplies the management root outside guest mounts.
        ledger_directory = (self.access_store.directory / 'leases' if self.access_store else
                            Path(directory).parent / '.unifier-access-fixture')
        try:
            self.ledger = GrantLedger(ledger_directory, lease_id or uuid.uuid4().hex,
                                     environment_binding, self.directory,
                                     (self.root_identity.st_dev, self.root_identity.st_ino))
        except BaseException:
            os.close(self.root_fd)
            self.directory.rmdir()
            raise

    def _check_root(self):
        observed = self.directory.lstat()
        if (observed.st_dev, observed.st_ino) != (self.root_identity.st_dev, self.root_identity.st_ino):
            raise DeliveryError('selection directory identity changed')

    def deliver(self, uris, *, method='OpenFile', multiple=False, directory=False, writable=False, cancelled=None):
        stopped = lambda: self.cancelled.is_set() or (cancelled is not None and cancelled.is_set())
        if (not isinstance(uris, (list, tuple)) or not 1 <= len(uris) <= 256
                or (not multiple and len(uris) != 1)):
            raise DeliveryError('ACCESS_REQUEST_INVALID', 'invalid selection count')
        if method not in ('OpenFile', 'SaveFile', 'SaveFiles'):
            raise DeliveryError('ACCESS_PURPOSE_INVALID', 'unsupported file chooser method')
        with self.lock, ExitStack() as stack:
            if stopped():
                raise DeliveryError('ACCESS_CANCELLED', 'selection lease closed')
            self._check_root()
            policy = self.access_store.policy() if self.access_store else AccessPolicy()
            selected = []
            decisions = []
            required = 0
            for uri in uris:
                source = local_path(uri)
                decision = decide_selection(policy, source, self.shares, method=method,
                                            directory=directory, writable=writable)
                decisions.append(decision.as_dict())
                writing = writable or method != 'OpenFile'
                mapped = None if decision.guest_path is None else Path(decision.guest_path)
                if method != 'OpenFile' and not source.exists():
                    # Only static writable mappings may return new save paths.
                    stack.enter_context(open_host_share_path(
                        HostSharePolicy((str(source.parent),), writable=True), str(source.parent), write=True))
                    selected.append((source, mapped, None, None))
                    continue
                fd = stack.enter_context(open_host_share_path(
                    HostSharePolicy((str(source.parent),), writable=writing), str(source), write=writing))
                info = os.fstat(fd)
                decisions[-1]['source_identity'] = list(identity(info))
                if directory != stat.S_ISDIR(info.st_mode):
                    raise DeliveryError('selection has the wrong file type')
                if mapped is None:
                    if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_file_bytes:
                        raise DeliveryError('selection exceeds the regular-file delivery limit')
                    required += info.st_size
                selected.append((source, mapped, fd, info))
            if self.used + required > self.max_session_bytes:
                raise DeliveryError('selection lease disk budget exhausted')
            disk = shutil.disk_usage(self.directory)
            if required and disk.free - required < max(5*1024**3, disk.total//10):
                raise DeliveryError('insufficient disk reserve for selected files')
            token = uuid.uuid4().hex
            self.ledger.reserve(token, size=required, objects=sum(d['backend'] == 'snapshot' for d in decisions),
                                byte_limit=self.max_session_bytes, object_limit=128, decisions=decisions)
            try:
                os.mkdir(token, 0o700, dir_fd=self.root_fd)
            except BaseException:
                self.ledger.transition(token, 'failed')
                raise
            batch = self.directory / token
            batch_fd = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root_fd)
            batch_info = os.fstat(batch_fd)
            batch_identity = [batch_info.st_dev, batch_info.st_ino]
            result, entries = [], []
            try:
                self.ledger.transition(token, 'preparing', batch_identity=batch_identity)
                for index, (source, mapped, anchor, info) in enumerate(selected):
                    if stopped():
                        raise DeliveryError('selection lease closed')
                    if mapped is not None:
                        result.append(mapped.as_uri())
                        entries.append({'kind': 'static', 'guest_path': str(mapped)})
                        continue
                    name = str(index)
                    os.mkdir(name, 0o700, dir_fd=batch_fd)
                    folder = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=batch_fd)
                    try:
                        target = os.open(source.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         0o400, dir_fd=folder)
                        reader = -1
                        try:
                            reader = os.open(f'/proc/self/fd/{anchor}', os.O_RDONLY | os.O_CLOEXEC)
                            if identity(os.fstat(reader)) != identity(info):
                                raise DeliveryError('selected source changed before copy')
                            total = 0
                            digest = hashlib.sha256()
                            while True:
                                if stopped():
                                    raise DeliveryError('selection lease closed')
                                block = os.read(reader, min(1024*1024, info.st_size-total+1))
                                if not block:
                                    break
                                total += len(block)
                                if total > info.st_size:
                                    raise DeliveryError('selected source grew during copy')
                                digest.update(block)
                                pending = memoryview(block)
                                while pending:
                                    pending = pending[os.write(target, pending):]
                            if total != info.st_size or identity(os.fstat(reader)) != identity(info):
                                raise DeliveryError('selected source changed during copy')
                            os.fsync(target)
                        finally:
                            if reader >= 0:
                                os.close(reader)
                            os.close(target)
                    finally:
                        os.close(folder)
                    guest = self.guest / token / name / source.name
                    result.append(guest.as_uri())
                    entries.append({'kind': 'snapshot', 'guest_path': str(guest),
                                    'host_snapshot': str(batch/name/source.name),
                                    'bytes': total, 'sha256': digest.hexdigest()})
                self._check_root()
                if stopped():
                    raise DeliveryError('selection request cancelled')
                if self.access_store and self.access_store.policy().revision != policy.revision:
                    raise DeliveryError('ACCESS_POLICY_STALE', 'policy changed during delivery; select again')
                self.emit(event='selection-prepared', token=token, files=entries, bytes=required)
                # Persist before handing a URI to the transport. A send failure
                # will revoke this exact grant through the same ledger.
                self.ledger.transition(token, 'ready', entries=entries, uris=result)
                self.grants[token] = required
                self.used += required
                self.batches += 1
                return DeliveredPaths(result, token)
            except BaseException:
                self.ledger.transition(token, 'revoking')
                try:
                    reclaim_directory(self.root_fd, self.directory, token, batch_identity)
                except (OSError, AccessError):
                    self.ledger.transition(token, 'needs-recovery')
                    raise
                self.ledger.transition(token, 'released')
                raise
            finally:
                os.close(batch_fd)

    def ready(self, uris):
        token = getattr(uris, 'grant_id', None)
        grant = self.ledger.read()['grants'].get(token)
        return bool(grant and grant['state'] == 'ready' and grant.get('uris') == list(uris))

    def _remove_grant(self, token, grant):
        self._check_root()
        expected = grant.get('batch_identity')
        if not isinstance(expected, list) or len(expected) != 2:
            raise DeliveryError('ACCESS_RECOVERY_REQUIRED', 'batch identity was not recorded')
        reclaim_directory(self.root_fd, self.directory, token, expected, missing_ok=False)
        self.ledger.transition(token, 'released')
        self.grants.pop(token, None)

    def revoke(self, uris):
        with self.lock:
            token = getattr(uris, 'grant_id', None)
            grant = self.ledger.read()['grants'].get(token)
            if not grant or grant['state'] in ('released', 'failed'):
                return
            self.ledger.transition(token, 'revoking')
            try:
                self._remove_grant(token, grant)
            except (OSError, AccessError):
                self.ledger.transition(token, 'needs-recovery')
                raise
            self.used, _ = self.ledger.usage()

    def reconcile_revocations(self):
        """The existing bridge services revocation without another daemon."""
        if not self.lock.acquire(blocking=False):
            return False
        try:
            self._check_root()
            for token, grant in self.ledger.read()['grants'].items():
                if grant['state'] != 'revoking':
                    continue
                try:
                    self._remove_grant(token, grant)
                except (OSError, AccessError):
                    self.ledger.transition(token, 'needs-recovery')
            self.used, _ = self.ledger.usage()
            return True
        finally:
            self.lock.release()

    def close(self):
        self.cancelled.set()
        with self.lock:
            if self.closed:
                return
            self._check_root()
            parent_fd = os.open(self.directory.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                reclaim_directory(parent_fd, self.directory.parent, self.directory.name,
                                  [self.root_identity.st_dev, self.root_identity.st_ino])
            finally:
                os.close(parent_fd)
            self.ledger.release_all()
            os.close(self.root_fd)
            self.closed = True
