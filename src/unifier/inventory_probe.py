"""One stopped Debian inventory read in Podman's storage user namespace.

This acquires a storage mount reference, not a running container or guest exec.
It is a T2 operation, with a durable journal for ambiguous acquisition/release;
it must not be treated as a side-effect-free capability probe.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unifier.errors import UnifierError
from unifier.package_inventory import observation, read_dpkg_database
from unifier.view_journal import read_journal, write_journal


def inspect_stopped(cid, project, env_id, *, run=subprocess.run):
    result = run(['podman', 'inspect', '--format', 'json', cid],
                 capture_output=True, text=True, timeout=10, check=False)
    if result.returncode:
        raise UnifierError('read-only inventory inspect failed')
    payload = json.loads(result.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise UnifierError('read-only inventory inspect is ambiguous')
    data = payload[0]
    labels = (data.get('Config') or {}).get('Labels') or {}
    state = data.get('State') or {}
    if (data.get('Id') != cid or labels.get('multilinux.project') != project
            or labels.get('multilinux.env') != env_id
            or labels.get('multilinux.role') != 'environment'):
        raise UnifierError('read-only inventory container binding changed')
    if state.get('Running') or state.get('Status') not in {'exited', 'created', 'configured', 'stopped'}:
        raise UnifierError('running/transitioning peer has no consistent read-only snapshot')
    for mount in data.get('Mounts') or []:
        destination = mount.get('Destination', '').rstrip('/') or '/'
        if (destination == '/' or destination == '/var/lib/dpkg'
                or '/var/lib/dpkg'.startswith(destination + '/')
                or destination.startswith('/var/lib/dpkg/')):
            raise UnifierError('mounted package database is unsupported')
    return {'started_at': state.get('StartedAt'), 'finished_at': state.get('FinishedAt'),
            'status': state.get('Status'), 'mounts': data.get('Mounts') or []}


def probe(record, journal, token, *, run=subprocess.run):
    cid = record['runtime']['container_id']
    project = record['runtime']['project']
    env_id = record['id']
    owned = False
    result = None
    error = None
    def stamp(state, **extra):
        write_journal(str(journal), {'schema_version': 1, 'state': state, 'cid': cid,
                      'token': token, 'owner_pid': os.getpid(), 'updated_at': time.time(), **extra})
    previous = read_journal(str(journal))
    if journal.exists() and (previous is None or previous.get('state') != 'released'):
        return observation(record, status='unavailable', reason='inventory storage journal needs reconciliation')
    try:
        before = inspect_stopped(cid, project, env_id, run=run)
        stamp('mounting')
        mounted = run(['podman', 'mount', cid], capture_output=True, text=True,
                      timeout=10, check=False)
        if mounted.returncode:
            # Acquisition failure can be ambiguous. Never guess at umount.
            raise UnifierError('inventory storage mount failed; reconciliation required')
        owned = True
        stamp('mounted')
        lines = (mounted.stdout or '').strip().splitlines()
        if len(lines) != 1 or not lines[0].startswith('/'):
            raise UnifierError('inventory storage returned an invalid root path')
        descriptor = os.open(lines[0], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            if inspect_stopped(cid, project, env_id, run=run) != before:
                raise UnifierError('peer state changed before database read')
            text, consistency = read_dpkg_database(descriptor)
            if inspect_stopped(cid, project, env_id, run=run) != before:
                raise UnifierError('peer state changed during database read')
            result = observation(record, inventory=text, consistency={**consistency,
                                 'storage_reference_token': token, 'container_state': before})
        finally:
            os.close(descriptor)
    except Exception as exc:
        error = str(exc)[-500:]
    finally:
        if owned:
            # Exactly this successfully acquired reference, once, without force.
            try:
                stamp('releasing')
                released = run(['podman', 'umount', cid], capture_output=True, text=True,
                               timeout=10, check=False)
                if released.returncode:
                    stamp('release-failed')
                    raise UnifierError('inventory storage release failed; reconciliation required')
                stamp('released')
            except Exception as exc:
                error = str(exc)[-500:]
        # A pre-mount error has no reference to release. An ambiguous mounting
        # or releasing journal intentionally remains as a persistent blocker.
    return (observation(record, status='unavailable', reason=error) if error
            else result)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--record', required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--token', required=True)
    args = parser.parse_args(argv)
    def interrupted(_number, _frame):
        raise UnifierError('inventory observer interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    value = probe(json.loads(args.record), Path(args.journal), args.token)
    from dataclasses import asdict
    print(json.dumps(asdict(value), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
