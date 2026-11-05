"""Explicit trusted-fixture loader verification, separate from package repair.

Never called automatically on vendor/package ELF objects. A caller must supply
an exact fixture manifest, an existing running bound CID, and a non-root guest
UID. --list resolves that fixture's initial loader graph, not dlopen/workflow.
"""
import hashlib
from pathlib import PurePosixPath
import re

from unifier.errors import UnifierError
from unifier.identity import binding_for


FIXTURE_PROBE = r'''
set -u
PATH=/usr/bin:/bin
export PATH LC_ALL=C
unset LD_LIBRARY_PATH LD_PRELOAD LD_AUDIT GLIBC_TUNABLES
program=$1
loader=$2
root=$3
shift 3
blocked() { printf 'UNIFIER_LOADER_BLOCKED %s\n' "$1"; exit 125; }
uid=$(id -u) || blocked uid-unavailable
[ "$uid" != 0 ] || blocked root-user
[ "$(stat -c '%a:%u' -- "$root")" = "700:$uid" ] || blocked fixture-directory-not-private
[ "$(readlink -f -- "$root")" = "$root" ] || blocked fixture-directory-indirect
bytes=0
while [ "$#" -gt 0 ]; do
    file=$1
    want=$2
    shift 2
    [ -f "$file" ] && [ ! -L "$file" ] || blocked fixture-file-not-regular
    [ ! -u "$file" ] && [ ! -g "$file" ] || blocked fixture-file-privileged
    [ "$(readlink -f -- "$file")" = "$file" ] || blocked fixture-file-indirect
    size=$(stat -c '%s' -- "$file") || blocked fixture-size-unavailable
    bytes=$((bytes + size))
    [ "$bytes" -le 4194304 ] || blocked fixture-byte-budget
    got=$(sha256sum -- "$file") || blocked fixture-hash-unavailable
    [ "${got%% *}" = "$want" ] || blocked fixture-hash-changed
done
/usr/bin/env -i LC_ALL=C PATH=/usr/bin:/bin HOME="$root" TMPDIR="$root" "$loader" --list "$program"
code=$?
printf '\nUNIFIER_LOADER_EXIT %s\n' "$code"
exit "$code"
'''


def fixture_argv(fixture, user):
    if (not isinstance(user, str) or not re.fullmatch(r'[0-9]+(?::[0-9]+)?', user)
            or int(user.split(':')[0]) == 0):
        raise UnifierError('loader fixture requires an explicit non-root guest UID')
    root = fixture.get('root')
    path = fixture.get('path')
    loader = fixture.get('loader')
    if (not isinstance(root, str) or not root.startswith('/')
            or not re.fullmatch(r'unifier-fixture-[0-9a-f]{32}', PurePosixPath(root).name)
            or '..' in PurePosixPath(root).parts):
        raise UnifierError('loader fixture requires a private uniquely named root')
    if loader not in {'/lib64/ld-linux-x86-64.so.2', '/lib/x86_64-linux-gnu/ld-linux-x86-64.so.2'}:
        raise UnifierError('loader fixture has no supported x86_64 target loader')
    files = fixture.get('files')
    if not isinstance(files, dict) or not 1 <= len(files) <= 16 or path not in files:
        raise UnifierError('loader fixture requires a bounded complete manifest')
    args = []
    for name, digest in sorted(files.items()):
        if (not isinstance(name, str) or not name.startswith(root + '/')
                or '..' in PurePosixPath(name).parts or any(ord(c) < 32 for c in name)
                or not isinstance(digest, str) or not re.fullmatch(r'[0-9a-f]{64}', digest)):
            raise UnifierError('invalid loader fixture manifest')
        args.extend([name, digest])
    return ['/bin/sh', '-c', FIXTURE_PROBE, 'unifier-loader-fixture', path, loader, root, *args]


def loading_result(fixture, native):
    output = native.stdout or ''
    diagnostics = native.stderr or ''
    result = {'status': 'unverified', 'scope': 'trusted-fixture-initial-loader-graph',
              'fixture_sha256': fixture['files'][fixture['path']], 'loader': fixture['loader'],
              'native_exit_code': native.returncode,
              'stdout_tail': output[-4000:], 'stderr_tail': (native.stderr or '')[-4000:],
              'stdout_sha256': hashlib.sha256(output.encode()).hexdigest(),
              'stderr_sha256': hashlib.sha256(diagnostics.encode()).hexdigest(),
              'dlopen': 'unverified', 'workflow': 'unverified', 'program_executed': False}
    if len(output) > 65536 or len(diagnostics) > 65536:
        result['reason'] = 'loader output exceeded budget'
    elif 'UNIFIER_LOADER_BLOCKED ' in output:
        result['reason'] = 'fixture safety precondition refused'
    elif re.search(r'\nUNIFIER_LOADER_EXIT %d\n\Z' % native.returncode, output):
        # glibc --list can report an unsatisfied symbol version on stderr and
        # still exit zero. Preserve the native code, but never turn that trace
        # into a passing graph. Unknown diagnostics are insufficient evidence.
        if native.returncode != 0:
            result.update(status='verified-fail', reason='loader exited unsuccessfully')
        elif re.search(r": version [`'][^\n]+['`] not found \(required by [^\n]+\)", diagnostics):
            result.update(status='verified-fail', reason='required symbol version unavailable')
        elif diagnostics.strip():
            result['reason'] = 'loader diagnostics require review'
        elif re.search(r'=>\s+not found(?:\n|$)', output):
            result.update(status='verified-fail', reason='required library unavailable')
        else:
            result['status'] = 'verified-pass'
    else:
        result['reason'] = 'loader completion was not verified'
    return result


def verify_fixture_loading(service, env_id, fixture, *, user):
    """T2-only explicit diagnostic; does not start or mutate an environment."""
    argv = fixture_argv(fixture, user)
    record = service.store.load(env_id)
    binding = binding_for(record)
    expected = fixture.get('environment_binding')
    if expected != binding:
        raise UnifierError('loader fixture environment binding changed')
    before = service._require_owned(record)
    if not before.running or before.container_id != binding['container_id']:
        raise UnifierError('loader fixture needs its exact already-running container')
    native = service.runtime.exec(binding['container_id'], argv,
        project=record['runtime']['project'], env_id=env_id, user=user,
        workdir=fixture['root'], env={'LC_ALL': 'C', 'PATH': '/usr/bin:/bin'},
        capture=True, timeout=10)
    result = loading_result(fixture, native)
    service._require_record_binding(service.store.load(env_id), binding)
    after = service._require_owned(record)
    if (after.container_id != binding['container_id'] or not after.running
            or after.started_at != before.started_at):
        raise UnifierError('loader fixture container changed during verification')
    return {**result, 'environment_binding': binding, 'guest_user': user}
