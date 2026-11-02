"""Routing policy for the three supported native package formats.

Routing identifies the container target. Its native package manager remains
responsible for metadata, signature, dependency and architecture validation.
"""
import hashlib
from pathlib import Path

from multilinux.errors import MultilinuxError


def route_package(raw):
    path = Path(raw).expanduser().resolve()
    if not path.is_file():
        raise MultilinuxError(f'local package is not a file: {path}')
    with path.open('rb') as source:
        magic = source.read(8)
        source.seek(0)
        digest = hashlib.file_digest(source, 'sha256').hexdigest()
    if path.name.endswith('.deb') and magic == b'!<arch>\n':
        format_name, target = 'deb', 'debian13'
    elif path.name.endswith('.rpm') and magic.startswith(b'\xed\xab\xee\xdb'):
        format_name, target = 'rpm', 'fedora44'
    elif ((path.name.endswith('.pkg.tar.zst') and magic.startswith(b'\x28\xb5\x2f\xfd'))
          or (path.name.endswith('.pkg.tar.xz') and magic.startswith(b'\xfd7zXZ\x00'))
          or (path.name.endswith('.pkg.tar.gz') and magic.startswith(b'\x1f\x8b'))):
        format_name, target = 'arch', 'arch'
    else:
        raise MultilinuxError('unsupported package format or extension/header mismatch; expected .deb, .rpm or .pkg.tar.*')
    return {'path': str(path), 'sha256': digest, 'format': format_name, 'target': target,
            'size_bytes': path.stat().st_size, 'validation': 'native-package-manager-required'}
