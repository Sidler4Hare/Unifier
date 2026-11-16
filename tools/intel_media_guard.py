"""Lock the Intel VA-API dependency transaction to an authenticated APT closure."""

import hashlib
import os
import re
import stat


PACKAGES = ('ffmpeg', 'intel-media-va-driver', 'vainfo')
PAYLOAD_BYTES = 96 * 1024**2
DEBIAN_RELEASE = 'debian-13: /usr/share/keyrings/debian-archive-keyring.pgp'
SOLVER_ROW = re.compile(r'Inst ([a-z0-9+.-]+)(?::amd64)? \((\S+) .*\[(amd64|all)\]\)')
ARCHIVE = re.compile(r'[a-z0-9+.-]+_[0-9A-Za-z%+~.\-]+_(?:amd64|all)\.deb')


def archive_name(name, version, arch):
    return f"{name}_{version.replace(':', '%3a')}_{arch}.deb"


def expected_archives(query, prefetch, plan, cid, archive):
    if (query.get('cid') != cid or query.get('status') != 'PASS' or query.get('mode') != 'query'
            or query['plan']['package_plan'] != plan or plan['signed_repository'] != DEBIAN_RELEASE
            or prefetch.get('status') != 'VERIFIED' or prefetch.get('bytes') != plan['download_bytes']):
        raise RuntimeError('Intel media install lacks its reviewed APT plan or prefetch evidence')
    closure = query['plan']['closure']
    if not 0 < len(closure) <= 256:
        raise RuntimeError('Intel media closure size is out of bounds')
    prefetched = {entry['name']: entry for entry in prefetch['packages']}
    expected = {}
    for package in closure:
        name, version, arch = (package[key] for key in ('name', 'version', 'arch'))
        row = prefetched[name]
        filename = archive_name(name, version, arch)
        if (name in expected or not ARCHIVE.fullmatch(filename) or row['filename'] != filename
                or row['sha256'] != package['sha256'] or row['bytes'] != package['download_bytes']):
            raise RuntimeError('Intel media archive proof differs from the signed closure')
        expected[name] = (version, arch)
        path = archive / filename
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        with os.fdopen(fd, 'rb') as stream:
            metadata = os.fstat(stream.fileno())
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid()
                    or metadata.st_mode & 0o022 or metadata.st_size != row['bytes']
                    or hashlib.file_digest(stream, 'sha256').hexdigest() != row['sha256']):
                raise RuntimeError('Intel media archive changed after prefetch: '+filename)
    if len(prefetch['packages']) != len(expected):
        raise RuntimeError('Intel media prefetch has unexpected extra archives')
    return expected


def check_simulated_closure(output, expected):
    selected = {}
    for line in output.splitlines():
        if line.startswith(('Remv ', 'Purg ')):
            raise RuntimeError('Intel media transaction would remove a package')
        if line.startswith('Inst '):
            match = SOLVER_ROW.fullmatch(line)
            if match is None or match[1] in selected:
                raise RuntimeError('Intel media transaction would upgrade or has an ambiguous APT row')
            selected[match[1]] = (match[2], match[3])
    if selected != expected or not re.search(
            rf'^0 upgraded, {len(expected)} newly installed, 0 to remove\b', output, re.M):
        raise RuntimeError('Intel media APT closure changed after the signed refresh')
