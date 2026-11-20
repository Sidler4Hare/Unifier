"""One stopped database inventory read in Podman's storage user namespace.

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
from unifier.identity import binding_for
from unifier.process_identity import process_stamp
from unifier.package_inventory import InventoryUnsupported, observation
from unifier.inventory_backends import backend_for
from unifier.view_journal import read_journal, write_journal


def inspect_stopped(cid, project, env_id, *, run=subprocess.run, database_paths=('/var/lib/dpkg',)):
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
        if any(destination == '/' or destination == path
               or path.startswith(destination + '/') or destination.startswith(path + '/')
               for path in database_paths):
            raise UnifierError('mounted package database is unsupported')
    return {'started_at': state.get('StartedAt'), 'finished_at': state.get('FinishedAt'),
            'status': state.get('Status'), 'mounts': data.get('Mounts') or []}


def probe(record, journal, token, *, run=subprocess.run):
    try:
        database, paths, reader = backend_for(record, require_available=True)
    except InventoryUnsupported as exc:
        return observation(record, status='unsupported', database='unknown', reason=str(exc))
    cid = record['runtime']['container_id']
    project = record['runtime']['project']
    env_id = record['id']
    owned = False
    result = None
    error = None
    failure_status = 'unavailable'
    owner = {'pid': os.getpid(), 'stamp': process_stamp(os.getpid()),
             'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
    def stamp(state, **extra):
        write_journal(str(journal), {'schema_version': 1, 'state': state, 'cid': cid,
                      'token': token, 'owner_pid': os.getpid(), 'owner': owner,
                      'environment_binding': binding_for(record),
                      'updated_at': time.time(), **extra})
    previous = read_journal(str(journal))
    if journal.exists() and (previous is None or previous.get('state') != 'released'):
        return observation(record, status='unavailable', database=database,
                           reason='inventory storage journal needs reconciliation')
    try:
        before = inspect_stopped(cid, project, env_id, run=run, database_paths=paths)
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
            if inspect_stopped(cid, project, env_id, run=run, database_paths=paths) != before:
                raise UnifierError('peer state changed before database read')
            text, consistency = reader(descriptor)
            if inspect_stopped(cid, project, env_id, run=run, database_paths=paths) != before:
                raise UnifierError('peer state changed during database read')
            result = observation(record, inventory=text, database=database,
                                 method=consistency.get('method', 'locked-stopped-database'), consistency={**consistency,
                                 'storage_reference_token': token, 'container_state': before})
        finally:
            os.close(descriptor)
    except InventoryUnsupported as exc:
        failure_status = 'unsupported'
        error = str(exc)[-500:]
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
                failure_status = 'unavailable'
                error = str(exc)[-500:]
        # A pre-mount error has no reference to release. An ambiguous mounting
        # or releasing journal intentionally remains as a persistent blocker.
    return (observation(record, status=failure_status, database=database, reason=error) if error
            else result)


def recover(record, journal, *, run=subprocess.run):
    """Release one proven orphaned reference. Ambiguous crash windows remain fenced."""
    value = read_journal(str(journal))
    if value is None:
        return {'status': 'unresolved' if journal.exists() else 'not-needed',
                'reason': 'journal missing or unreadable'}
    cid = record['runtime']['container_id']
    if (value.get('cid') != cid or value.get('environment_binding') != binding_for(record)
            or not isinstance(value.get('token'), str) or not value['token']):
        return {'status': 'unresolved', 'reason': 'journal ownership is incomplete or changed'}
    if value.get('state') == 'released':
        return {'status': 'already-released', 'token': value['token']}
    if value.get('state') != 'mounted':
        return {'status': 'unresolved', 'reason': 'ambiguous acquisition/release window',
                'journal_state': value.get('state')}
    owner = value.get('owner') or {}
    if (not isinstance(owner.get('pid'), int) or owner['pid'] <= 0
            or not isinstance(owner.get('stamp'), int) or not owner.get('boot_id')):
        return {'status': 'unresolved', 'reason': 'helper process identity is incomplete'}
    boot = Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    if owner['boot_id'] != boot:
        return {'status': 'unresolved', 'reason': 'storage reference crossed a host reboot'}
    stamp = process_stamp(owner['pid'])
    if stamp == owner['stamp'] or (stamp is None and Path(f"/proc/{owner['pid']}").exists()):
        return {'status': 'unresolved', 'reason': 'helper is live or its liveness is unknown'}
    _, paths, _ = backend_for(record)
    inspect_stopped(cid, record['runtime']['project'], record['id'], run=run, database_paths=paths)
    # The caller holds the observation lock across read, identity checks and release.
    value.update(state='releasing', recovery_started_at=time.time())
    write_journal(str(journal), value)
    try:
        result = run(['podman', 'umount', cid], capture_output=True, text=True,
                     timeout=10, check=False)
        if result.returncode:
            raise UnifierError('owned storage release failed')
    except Exception as exc:
        value.update(state='release-failed', recovery_error=str(exc)[-500:])
        write_journal(str(journal), value)
        return {'status': 'unresolved', 'reason': str(exc)[-500:]}
    value.update(state='released', recovered_at=time.time())
    write_journal(str(journal), value)
    return {'status': 'recovered', 'token': value['token'], 'container_id': cid}


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--record', required=True)
    parser.add_argument('--journal', required=True)
    parser.add_argument('--token')
    parser.add_argument('--recover', action='store_true')
    args = parser.parse_args(argv)
    def interrupted(_number, _frame):
        raise UnifierError('inventory observer interrupted')
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    if args.recover:
        value = recover(json.loads(args.record), Path(args.journal))
        print(json.dumps(value, ensure_ascii=False))
        return 0 if value['status'] in {'recovered', 'already-released', 'not-needed'} else 1
    if not args.token:
        parser.error('--token is required for observation')
    value = probe(json.loads(args.record), Path(args.journal), args.token)
    from dataclasses import asdict
    print(json.dumps(asdict(value), ensure_ascii=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
