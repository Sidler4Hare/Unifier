"""Application execution and recovery using shared, identity-bound leases."""
from dataclasses import asdict
import signal
import time

from unifier.errors import UnifierError
from unifier.process_identity import process_stamp
from unifier.runtime import ExecResult, GuestProcess


def observe_lease(group, token, entry):
    binding = entry.get('environment_binding')
    if not binding:
        return {'state': 'unknown', 'reason': 'legacy lease has no environment binding'}
    try:
        record = group.service.store.load(group.env_id)
        group.service._require_record_binding(record, binding)
        info = group.service._require_owned(record)
        if info.container_id != binding['container_id']:
            raise UnifierError('application lease container changed')
        if not info.running and info.status in {'exited', 'stopped', 'created', 'configured'}:
            return {'state': 'exited', 'all_children_reaped': True,
                    'proof': 'exact-container-stopped', 'exit_code': None}
        execution = entry.get('execution') or {}
        if execution.get('state') == 'not-started':
            return {'state': 'exited', 'all_children_reaped': True,
                    'proof': 'execution-never-submitted', 'exit_code': None}
        if execution.get('ownership') != 'subreaper-v1' or execution.get('token') != token:
            return {'state': 'unknown', 'reason': 'legacy lease has no guest execution ownership'}
        value = group.service.runtime.observe_application(
            binding['container_id'], token, project=record['runtime']['project'],
            env_id=group.env_id, user=entry.get('user'))
        if value.get('token') != token or value.get('ownership') != 'subreaper-v1':
            return {'state': 'unknown', 'reason': 'application observation binding changed'}
        guest = execution.get('guest')
        if guest and (guest.get('pid') != value.get('pid') or guest.get('start') != value.get('start')):
            return {'state': 'unknown', 'reason': 'application supervisor identity changed'}
        if value.get('state') == 'exited' and value.get('all_children_reaped') is not True:
            return {'state': 'unknown', 'reason': 'application terminal proof incomplete'}
        # Do not release across a record/runtime replacement during the query.
        group.service._require_record_binding(group.service.store.load(group.env_id), binding)
        after = group.service._require_owned(record)
        if after.container_id != binding['container_id'] or after.started_at != info.started_at:
            return {'state': 'unknown', 'reason': 'container changed during application observation'}
        return value
    except Exception as exc:
        return {'state': 'unknown', 'reason': str(exc)[-500:]}


def execute_application(group, app_id, argv, *, pins, workdir=None):
    service = group.service
    for name in ('start_application_exec', 'observe_application', 'signal_application_guest'):
        if not callable(getattr(service.runtime, name, None)):
            raise UnifierError('runtime cannot prove application process-tree ownership; launch refused')
    token = group.acquire(app_id, owned_execution=True, **pins)
    execution = None
    submitted = confirmed = False
    pending = {'signal': None}
    handlers = {}
    def requested(number, _frame):
        pending['signal'] = number
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        try:
            handlers[number] = signal.signal(number, requested)
        except ValueError:  # Offline callers may run in a thread.
            pass
    try:
        group.update_execution(token, state='starting')
        submitted = True
        execution = service.start_app_execution(app_id, argv, token=token,
                                                workdir=workdir, **pins)
        guest = execution.guest
        if guest.ownership != 'subreaper-v1' or guest.token != token:
            raise UnifierError('runtime returned an unbound application supervisor')
        group.update_execution(token, state='running', guest=asdict(guest))
        while True:
            if pending['signal'] is not None:
                number, pending['signal'] = pending['signal'], None
                if execution.signal(number) not in {'signalled', 'gone'}:
                    raise UnifierError('application cancellation is unverified')
            if execution.poll() is not None:
                entry = group.read()['leases'].get(token)
                if entry is None:
                    raise UnifierError('application execution lease disappeared')
                value = observe_lease(group, token, entry)
                if value.get('state') == 'exited':
                    group.update_execution(token, state='exited', proof=value)
                    confirmed = True
                    break
                if value.get('state') != 'running':
                    raise UnifierError(value.get('reason') or 'application termination is unknown')
                # The host proxy ended while the guest supervisor still owns
                # descendants. Keep the lease and reconnect through its token.
                time.sleep(.25)
            else:
                time.sleep(.05)
    except BaseException as exc:
        # Only an explicit pre-READY error proves that no application ran.
        safe = not submitted or getattr(exc, 'execution_started', None) is False
        try:
            group.update_execution(token, state='exited' if safe else 'recovery-required',
                                   error=str(exc)[-500:], proof='not-started' if safe else None)
            confirmed = safe
        except Exception:
            confirmed = False
        raise
    finally:
        for number, handler in handlers.items():
            signal.signal(number, handler)
        if confirmed:
            group.release(token)

    # A stopped container proves termination even when the guest could not
    # record its exit status. Report that separately from lease recovery.
    code = value.get('exit_code')
    if type(code) is not int:
        raise UnifierError('application ended without a native exit status')
    return ExecResult(code, '', '')


def recover_applications(group, token=None):
    entries = group.read()['leases']
    if token is not None:
        if token not in entries:
            raise UnifierError('application lease token does not exist')
        entries = {token: entries[token]}
    results = []
    for key, entry in entries.items():
        if entry.get('kind') == 'project-session' or not entry.get('app_id'):
            continue
        if entry.get('stamp') is not None and process_stamp(entry.get('pid')) == entry['stamp']:
            results.append({'token': key, 'action': 'kept-live-worker'})
            continue
        value = observe_lease(group, key, entry)
        if value.get('state') != 'exited':
            results.append({'token': key, 'action': 'kept-' + value.get('state', 'unknown'),
                            'observation': value})
            continue
        try:
            if entry.get('execution') is not None:
                group.update_execution(key, state='exited', proof=value)
            group.release(key)
        except UnifierError as exc:
            results.append({'token': key, 'action': 'recovery-required', 'reason': str(exc)})
        else:
            results.append({'token': key, 'action': 'released', 'proof': value})
    return results


def cancel_application(group, token, *, seconds=15.0):
    entry = group.read()['leases'].get(token)
    if not entry or not entry.get('app_id'):
        raise UnifierError('application lease token does not exist')
    value = observe_lease(group, token, entry)
    if value.get('state') != 'running':
        raise UnifierError('application cancellation requires a live verified supervisor')
    guest = GuestProcess(value['pid'], value['start'], value['pgrp'], value['sid'],
                         token, 'subreaper-v1')
    record = group.service.store.load(group.env_id)
    answer = group.service.runtime.signal_application_guest(
        entry['environment_binding']['container_id'], guest, signal.SIGTERM,
        project=record['runtime']['project'], env_id=group.env_id, user=entry.get('user'))
    if answer not in {'signalled', 'gone'}:
        raise UnifierError('application cancellation could not be verified; lease retained')
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = observe_lease(group, token, entry)
        if value.get('state') == 'exited':
            with group.locked():
                state = group.read()
                current = state['leases'].get(token)
                if current is None:
                    return {'token': token, 'state': 'exited', 'proof': value,
                            'lease': 'released'}
                group.service._require_record_binding(
                    group.service.store.load(group.env_id), current['environment_binding'])
                current['execution'].update(state='exited', proof=value)
                group.save(state)
                worker_live = (current.get('stamp') is not None
                    and process_stamp(current.get('pid')) == current['stamp'])
            # A live worker still needs the lease to collect its native result.
            # Removing it here races with that collection and can stop the CID
            # before the worker reads the terminal guest receipt.
            if not worker_live:
                group.release(token)
            return {'token': token, 'state': 'exited', 'proof': value,
                    'lease': 'awaiting-worker' if worker_live else 'released'}
        if value.get('state') != 'running':
            break
        time.sleep(.1)
    raise UnifierError('application cancellation is unresolved; lease retained for recovery')
