"""Deliver only selections authorized by a successful host Portal response.

The bridge owns authorization; this module handles verified static mappings
and bounded read-only snapshots. It never grants a parent-directory view.
"""
from contextlib import ExitStack
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile
import threading
from urllib.parse import unquote_to_bytes, urlsplit
import uuid

from unifier.host_share_paths import open_host_share_path
from unifier.host_share_policy import HostSharePolicy


class DeliveryError(ValueError):
    pass


def local_path(uri):
    if not isinstance(uri, str) or len(uri) > 32768 or re.search(r'%(?![0-9a-fA-F]{2})', uri):
        raise DeliveryError('invalid local file URI')
    parsed = urlsplit(uri)
    if parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost') or parsed.query or parsed.fragment:
        raise DeliveryError('only local file selections can be delivered')
    text = unquote_to_bytes(parsed.path).decode('utf-8', 'strict')
    if (not text.startswith('/') or text.startswith('//') or
            any(c in text for c in '\0\n\r') or any(p in ('', '.', '..') for p in text.split('/')[1:])):
        raise DeliveryError('file path must be canonical and absolute')
    return Path(text)


def mapped_path(path, shares, *, writable=False):
    matches = [(Path(guest) / path.relative_to(host), rw)
               for host, guest, rw in shares if path.is_relative_to(host)]
    if len(matches) > 1:
        raise DeliveryError('ambiguous selected-file mapping')
    if not matches:
        return None
    if writable and not matches[0][1]:
        raise DeliveryError('selection needs a writable shared directory')
    return matches[0][0]


def identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


class SelectionStore:
    """Lease-scoped snapshots; every batch is all-or-nothing and bounded."""
    def __init__(self, directory, guest_directory, shares=(), *, emit=None,
                 max_file_bytes=512*1024**2, max_session_bytes=1024**3):
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

    def _check_root(self):
        observed = self.directory.lstat()
        if (observed.st_dev, observed.st_ino) != (self.root_identity.st_dev, self.root_identity.st_ino):
            raise DeliveryError('selection directory identity changed')

    def deliver(self, uris, *, method='OpenFile', multiple=False, directory=False, writable=False, cancelled=None):
        stopped = lambda: self.cancelled.is_set() or (cancelled is not None and cancelled.is_set())
        if (not isinstance(uris, (list, tuple)) or not 1 <= len(uris) <= 256
                or (not multiple and len(uris) != 1)):
            raise DeliveryError('invalid selection count')
        if method not in ('OpenFile', 'SaveFile', 'SaveFiles'):
            raise DeliveryError('unsupported file chooser method')
        with self.lock, ExitStack() as stack:
            if stopped() or self.batches >= 128:
                raise DeliveryError('selection lease closed or request budget exhausted')
            self._check_root()
            selected = []
            required = 0
            for uri in uris:
                source = local_path(uri)
                writing = writable or method != 'OpenFile'
                mapped = mapped_path(source, self.shares, writable=writing)
                if mapped is None and (writing or directory):
                    raise DeliveryError('write, save and directory selections require an existing shared directory')
                if method != 'OpenFile' and not source.exists():
                    # Only static writable mappings may return new save paths.
                    stack.enter_context(open_host_share_path(
                        HostSharePolicy((str(source.parent),), writable=True), str(source.parent), write=True))
                    selected.append((source, mapped, None, None))
                    continue
                fd = stack.enter_context(open_host_share_path(
                    HostSharePolicy((str(source.parent),), writable=writing), str(source), write=writing))
                info = os.fstat(fd)
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
            os.mkdir(token, 0o700, dir_fd=self.root_fd)
            batch = self.directory / token
            batch_fd = os.open(token, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.root_fd)
            result, entries = [], []
            try:
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
                self.emit(event='selection-prepared', token=token, files=entries, bytes=required)
                self.grants[tuple(result)] = (token, required)
                self.used += required
                self.batches += 1
                return result
            except BaseException:
                shutil.rmtree(batch)
                raise
            finally:
                os.close(batch_fd)

    def revoke(self, uris):
        with self.lock:
            grant = self.grants.pop(tuple(uris), None)
            if grant:
                token, size = grant
                shutil.rmtree(self.directory / token, ignore_errors=True)
                self.used -= size

    def close(self):
        self.cancelled.set()
        with self.lock:
            self._check_root()
            os.close(self.root_fd)
            shutil.rmtree(self.directory)
