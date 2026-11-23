"""Common authority decisions for Unifier's file access backends.

Decisions describe the enforcement actually provided by the chosen backend.
They are not transferable capabilities: adapters must revalidate their trusted
authority, object descriptors and environment binding at the execution boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from urllib.parse import unquote_to_bytes, urlsplit

from unifier.errors import UnifierError

SCHEMA_VERSION = 1
PROTOCOL_VERSION = 2
PURPOSES = frozenset({'read', 'edit', 'save', 'browse', 'copy-source', 'copy-target', 'delete'})
WRITE_PURPOSES = frozenset({'edit', 'save', 'copy-target', 'delete'})


class AccessError(UnifierError, ValueError):
    def __init__(self, code, message=None):
        if message is None:
            code, message = 'ACCESS_DELIVERY_FAILED', code
        self.code = code
        super().__init__(f'{code}: {message}')


def canonical_path(value) -> PurePosixPath:
    value = str(value) if isinstance(value, PurePosixPath) else value
    if (not isinstance(value, str) or len(value) > 32768 or not value.startswith('/')
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
            or value != '/' and any(p in ('', '.', '..') for p in value.split('/')[1:])):
        raise AccessError('ACCESS_PATH_INVALID', 'file path must be canonical and absolute')
    return PurePosixPath(value)


def local_path(uri) -> Path:
    if not isinstance(uri, str) or len(uri) > 32768 or re.search(r'%(?![0-9a-fA-F]{2})', uri):
        raise AccessError('ACCESS_URI_INVALID', 'invalid local file URI')
    parsed = urlsplit(uri)
    if (parsed.scheme != 'file' or parsed.netloc not in ('', 'localhost')
            or parsed.query or parsed.fragment):
        raise AccessError('ACCESS_URI_INVALID', 'only local file selections can be delivered')
    try:
        return Path(canonical_path(unquote_to_bytes(parsed.path).decode('utf-8', 'strict')))
    except UnicodeError as exc:
        raise AccessError('ACCESS_URI_INVALID', 'file URI is not UTF-8') from exc


def within(path, root):
    return canonical_path(path).is_relative_to(canonical_path(root))


def _paths(values, label, *, optional=False):
    if values is None and optional:
        return None
    if not isinstance(values, (tuple, list)) or len(values) > 256:
        raise AccessError('ACCESS_POLICY_INVALID', f'{label} must contain at most 256 paths')
    return tuple(dict.fromkeys(str(canonical_path(v)) for v in values))


@dataclass(frozen=True)
class AccessPolicy:
    """Restrictions intersect backend authority; None inherits its existing scope."""
    read_roots: tuple[str, ...] | None = None
    write_roots: tuple[str, ...] | None = None
    denied_roots: tuple[str, ...] = ()
    selected_read: bool = True
    revision: int = 1

    def __post_init__(self):
        for key in ('read_roots', 'write_roots', 'denied_roots'):
            object.__setattr__(self, key, _paths(getattr(self, key), key, optional=key != 'denied_roots'))
        if type(self.selected_read) is not bool or type(self.revision) is not int or self.revision < 1:
            raise AccessError('ACCESS_POLICY_INVALID', 'invalid revision or selected_read')

    @classmethod
    def from_dict(cls, value):
        fields = {'schema_version', 'revision', 'read_roots', 'write_roots', 'denied_roots', 'selected_read'}
        if (not isinstance(value, dict) or set(value) != fields
                or type(value.get('schema_version')) is not int or value['schema_version'] != SCHEMA_VERSION):
            raise AccessError('ACCESS_POLICY_INVALID', 'unknown file access policy schema or fields')
        return cls(**{k: v for k, v in value.items() if k != 'schema_version'})

    def as_dict(self):
        return {'schema_version': SCHEMA_VERSION, 'revision': self.revision,
                'read_roots': None if self.read_roots is None else list(self.read_roots),
                'write_roots': None if self.write_roots is None else list(self.write_roots),
                'denied_roots': list(self.denied_roots), 'selected_read': self.selected_read}

    def require(self, path, purpose, *, directory=False, selected=False):
        path = canonical_path(path)
        if purpose not in PURPOSES:
            raise AccessError('ACCESS_PURPOSE_INVALID', 'unsupported file access purpose')
        if type(directory) is not bool or type(selected) is not bool:
            raise AccessError('ACCESS_REQUEST_INVALID', 'request flags must be boolean')
        if selected and not self.selected_read and purpose not in WRITE_PURPOSES:
            raise AccessError('ACCESS_SELECTION_DISABLED', 'selected-file access is disabled')
        for denied in self.denied_roots:
            if path.is_relative_to(denied) or directory and PurePosixPath(denied).is_relative_to(path):
                raise AccessError('ACCESS_DENIED', 'file access policy denies this path or a directory descendant')
        for roots in (self.read_roots, self.write_roots if purpose in WRITE_PURPOSES else None):
            if roots is not None and not any(path.is_relative_to(root) for root in roots):
                raise AccessError('ACCESS_OUTSIDE_SCOPE', 'path is outside the permitted file access roots')
        return path


def require_registered_permission(record, purpose):
    """Shared root/project restriction, in addition to each adapter's identity proof."""
    if purpose not in PURPOSES:
        raise AccessError('ACCESS_PURPOSE_INVALID', 'unsupported file access purpose')
    mode = record.get('access_policy')
    if mode not in ('read-only', 'read-write'):
        raise AccessError('ACCESS_POLICY_INVALID', 'unknown registered access policy')
    if purpose in WRITE_PURPOSES and mode != 'read-write':
        raise AccessError('ACCESS_READ_ONLY', 'registered scope is read-only')
    if purpose == 'delete' and (record.get('cleanup_policy') != 'controlled' or record.get('source_view')):
        raise AccessError('ACCESS_DELETE_REFUSED', 'source removal requires a controlled writable data root')


def resolve_static_mount(mounts, target):
    """Resolve a guest path through the most specific mount, retaining its authority."""
    target = canonical_path(target)
    matches = []
    for mount in mounts:
        host, guest = canonical_path(mount['host']), canonical_path(mount['container'])
        options = mount.get('options', '')
        if not isinstance(options, str):
            raise AccessError('ACCESS_MOUNT_INVALID', 'mount options must be text')
        if target.is_relative_to(guest):
            matches.append((len(guest.parts), mount, host / target.relative_to(guest)))
    if not matches:
        raise AccessError('ACCESS_MAPPING_MISSING', f'no static mount covers container path {str(target)!r}')
    longest = max(item[0] for item in matches)
    top = [item for item in matches if item[0] == longest]
    if len({(str(item[2]), tuple(sorted(item[1].get('options', '').split(',')))) for item in top}) != 1:
        raise AccessError('ACCESS_MAPPING_AMBIGUOUS', 'ambiguous static mounts cover the requested path')
    return top[0][1], str(top[0][2])


def mapped_path(path, shares, *, writable=False):
    path = canonical_path(path)
    mounts, candidates = [], []
    for host, guest, rw in shares:
        host, guest = canonical_path(host), canonical_path(guest)
        if type(rw) is not bool:
            raise AccessError('ACCESS_MOUNT_INVALID', 'runtime write permission must be boolean')
        mounts.append({'host': str(host), 'container': str(guest), 'options': 'rw' if rw else 'ro'})
        if path.is_relative_to(host):
            candidates.append(guest / path.relative_to(host))
    results = set()
    for candidate in candidates:
        actual, source = resolve_static_mount(mounts, candidate)
        if source != str(path):
            raise AccessError('ACCESS_MAPPING_SHADOWED', 'selected path is shadowed by another mount')
        results.add((str(candidate), actual['options'] == 'rw'))
    if not results:
        return None
    if len(results) != 1:
        raise AccessError('ACCESS_MAPPING_AMBIGUOUS', 'ambiguous selected-file mapping')
    guest, rw = next(iter(results))
    if writable and not rw:
        raise AccessError('ACCESS_READ_ONLY', 'selection needs a writable shared directory')
    return Path(guest)


@dataclass(frozen=True)
class AccessDecision:
    purpose: str
    backend: str
    source: str
    guest_path: str | None
    policy_revision: int
    enforcement: str
    scope: str = 'environment'

    def as_dict(self):
        from dataclasses import asdict
        return asdict(self)


def decide_selection(policy, path, shares, *, method='OpenFile', directory=False, writable=False):
    if method not in ('OpenFile', 'SaveFile', 'SaveFiles'):
        raise AccessError('ACCESS_PURPOSE_INVALID', 'unsupported file chooser method')
    purpose = 'save' if method != 'OpenFile' else 'edit' if writable else 'browse' if directory else 'read'
    source = policy.require(path, purpose, directory=directory, selected=True)
    guest = mapped_path(source, shares, writable=purpose in WRITE_PURPOSES)
    if guest is None and (purpose in WRITE_PURPOSES or directory):
        raise AccessError('ACCESS_BACKEND_UNSUPPORTED',
                          'write, save and directory selections require an existing shared directory')
    return AccessDecision(purpose, 'static' if guest is not None else 'snapshot', str(source),
                          None if guest is None else str(guest), policy.revision,
                          'environment-mount' if guest is not None else 'snapshot-copy')
