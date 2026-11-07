"""Bounded interpretation of caller-selected logs; log text is never code."""
import hashlib
import os
import re
import stat
import unicodedata

from unifier.dependencies import validate_library
from unifier.errors import UnifierError


MAX_BYTES = 1024 * 1024
MAX_LIBRARIES = 32
SGR = re.compile(r'\x1b\[[0-9;]{0,32}m')
MISSING = re.compile(
    r'(?<![\w/.$+:-])([A-Za-z0-9][A-Za-z0-9_.+-]{0,160}\.so'
    r'(?:\.[A-Za-z0-9_.+-]+)?): cannot open shared object file: '
    r'No such file or directory(?=$|[\s)\'\"])')
ABI = re.compile(
    r'ELFCLASS\d+|wrong ELF class|invalid ELF header|undefined symbol\s*:'
    r'|version\s+[\'\"`][^\'\"`\r\n]{1,128}[\'\"`]\s+not found', re.I)
COMMAND = re.compile(r'(?:^|:\s*)[A-Za-z0-9][A-Za-z0-9_.+-]*: '
                     r'(?:command not found|not found)(?:$|\s)', re.I)
PLUGIN = re.compile(r'could not (?:find|load) (?:the )?Qt platform plugin'
                    r'|cannot load library|failed to load (?:platform )?plugin', re.I)


def parse_diagnostics(text: str) -> dict:
    if not isinstance(text, str):
        raise UnifierError('diagnostic input must be UTF-8 text')
    try:
        raw = text.encode('utf-8')
    except UnicodeError as exc:
        raise UnifierError('diagnostic input must be UTF-8 text') from exc
    if len(raw) > MAX_BYTES:
        raise UnifierError('diagnostic log exceeds the 1 MiB input budget')
    clean = SGR.sub('', text)
    if any(unicodedata.category(char) == 'Cc' and char not in '\t\r\n'
           for char in clean):
        raise UnifierError('diagnostic log contains binary or unsupported control characters')
    libraries = set()
    issues = set()
    for line in clean.splitlines():
        missing = {validate_library(match.group(1)) for match in MISSING.finditer(line)}
        libraries.update(missing)
        if len(libraries) > MAX_LIBRARIES:
            raise UnifierError('diagnostic log exceeds the 32-library budget')
        issues.update(('missing-library', name) for name in missing)
        if ABI.search(line):
            issues.add(('abi-mismatch', ''))
        if 'permission denied' in line.lower():
            issues.add(('permission-denied', ''))
        if COMMAND.search(line):
            issues.add(('missing-command', ''))
        if PLUGIN.search(line) and not missing:
            issues.add(('plugin-unresolved', ''))
    return {'schema_version': 1, 'libraries': sorted(libraries),
            'issues': [dict(kind=kind, **({'library': name} if name else {}))
                       for kind, name in sorted(issues)],
            'source': {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}}


def read_diagnostics(path) -> dict:
    descriptor = None
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise UnifierError('diagnostic log must be a regular file')
        if before.st_size > MAX_BYTES:
            raise UnifierError('diagnostic log exceeds the 1 MiB input budget')
        chunks = []
        total = 0
        while total <= MAX_BYTES:
            chunk = os.read(descriptor, min(65536, MAX_BYTES + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            chunks.append(chunk)
        after = os.fstat(descriptor)
        fingerprint = lambda value: (value.st_dev, value.st_ino, value.st_size,
                                     value.st_mtime_ns, value.st_ctime_ns)
        if fingerprint(before) != fingerprint(after) or total != after.st_size:
            raise UnifierError('diagnostic log changed while reading')
        if total > MAX_BYTES:
            raise UnifierError('diagnostic log exceeds the 1 MiB input budget')
        return parse_diagnostics(b''.join(chunks).decode('utf-8'))
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        # Do not include the source path or any log/credential text in errors.
        raise UnifierError('cannot read a stable, regular UTF-8 diagnostic log') from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
