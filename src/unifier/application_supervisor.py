"""Guest-side application ownership; source is sent to the guest's Python3.

Only stdlib and Linux prctl are used. A subreaper waits for all descendants,
including double-fork/setsid daemons. Its disappearance is never an exit proof:
only the token-bound receipt written after ECHILD can establish completion.
This module also runs directly for bounded, host-local protocol fixtures.
"""
import ctypes
import json
import os
from pathlib import Path
import re
import signal
import stat
import sys
import time

READY_SENT = False


def process(pid):
    try:
        fields = Path('/proc/%d/stat' % pid).read_text().rsplit(')', 1)[1].split()
        return {'pid': int(pid), 'start': int(fields[19]), 'pgrp': int(fields[2]),
                'sid': int(fields[3]), 'ppid': int(fields[1]), 'zombie': fields[0] == 'Z'}
    except (OSError, ValueError, IndexError):
        return None


def directory(root, token, *, create=False):
    if not re.fullmatch(r'[0-9a-f]{32}', token):
        raise ValueError('invalid application token')
    base = Path(root) if root else Path('/tmp/.unifier-app-%d' % os.geteuid())
    if create:
        base.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(base, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    observed = os.fstat(fd)
    if observed.st_uid != os.geteuid() or observed.st_mode & 0o077:
        os.close(fd)
        raise ValueError('application receipt directory is not private to this user')
    return fd, token + '.json'


def write_receipt(fd, name, payload):
    temporary = name + '.new'
    child = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=fd)
    try:
        data = json.dumps(payload, sort_keys=True).encode() + b'\n'
        with os.fdopen(child, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=fd, dst_dir_fd=fd)
        os.fsync(fd)
    except BaseException:
        # A failed write is not an ended execution; keep the previous receipt.
        raise


def read_receipt(root, token):
    fd, name = directory(root, token)
    try:
        child = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        try:
            observed = os.fstat(child)
            if (not stat.S_ISREG(observed.st_mode) or observed.st_size > 16384
                    or observed.st_uid != os.geteuid()):
                raise ValueError('invalid application receipt')
            value = json.loads(os.read(child, 16385).decode())
        finally:
            os.close(child)
        if value.get('token') != token or value.get('ownership') != 'subreaper-v1':
            raise ValueError('application receipt binding changed')
        return value
    finally:
        os.close(fd)


def observe(root, token):
    try:
        value = read_receipt(root, token)
        if value.get('state') == 'exited' and value.get('all_children_reaped') is True:
            return value
        current = process(value['pid'])
        if current and not current['zombie'] and current['start'] == value['start']:
            return {**value, 'state': 'running'}
        return {**value, 'state': 'unknown', 'reason': 'supervisor gone without terminal proof'}
    except (OSError, ValueError, KeyError, TypeError):
        return {'state': 'unknown', 'token': token, 'ownership': 'subreaper-v1',
                'reason': 'receipt unavailable or invalid'}


def signal_exact(pid, start, number):
    # pidfd prevents reuse between validation and delivery.
    fd = os.pidfd_open(pid)
    try:
        current = process(pid)
        if not current or current['start'] != start or current['zombie']:
            return False
        signal.pidfd_send_signal(fd, number)
        return True
    finally:
        os.close(fd)


def descendants(root_pid):
    entries = {}
    with os.scandir('/proc') as iterator:
        for entry in iterator:
            if entry.name.isdecimal():
                value = process(int(entry.name))
                if value:
                    entries[value['pid']] = value
                if len(entries) > 4096:
                    raise RuntimeError('application signal observation exceeds process budget')
    owned = {root_pid}
    while True:
        new = {pid for pid, item in entries.items() if item['ppid'] in owned} - owned
        if not new:
            break
        owned.update(new)
    return [entries[pid] for pid in owned - {root_pid} if not entries[pid]['zombie']]


def run(root, token, control_fd, argv, *, exit_policy='wait'):
    global READY_SENT
    if exit_policy not in ('wait', 'stop-children'):
        raise ValueError('invalid application exit policy')
    if not argv:
        raise ValueError('application argv is empty')
    if not hasattr(os, 'pidfd_open') or not hasattr(signal, 'pidfd_send_signal'):
        raise RuntimeError('pidfd application control is unavailable')
    libc = ctypes.CDLL(None, use_errno=True)
    enabled = ctypes.c_int()
    if (libc.prctl(36, 1, 0, 0, 0) != 0
            or libc.prctl(37, ctypes.byref(enabled), 0, 0, 0) != 0 or enabled.value != 1):
        raise RuntimeError('child subreaper is unavailable')
    # The supervisor's group/session is dedicated even if podman exec initially
    # made it a group leader. The parent fork waits for the real supervisor.
    if os.getsid(0) != os.getpid():
        try:
            os.setsid()
        except PermissionError:
            pid = os.fork()
            if pid:
                os.close(control_fd)
                return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])
            os.setsid()
            # PR_SET_CHILD_SUBREAPER is not inherited by fork.
            if libc.prctl(36, 1, 0, 0, 0) != 0:
                raise RuntimeError('child supervisor could not enable subreaper')
    identity = process(os.getpid())
    if identity is None:
        raise RuntimeError('supervisor identity unavailable')
    fd, name = directory(root, token, create=True)
    pending = {'signal': None, 'at': None}
    def requested(number, _frame):
        if pending['signal'] is None:
            pending.update(signal=number, at=time.monotonic())
    for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(number, requested)
    payload = {**{k: identity[k] for k in ('pid', 'start', 'pgrp', 'sid')},
               'token': token, 'ownership': 'subreaper-v1', 'state': 'starting',
               'all_children_reaped': False, 'exit_policy': exit_policy}
    try:
        # Never reuse a token or overwrite another execution's terminal proof.
        try:
            old = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        except FileNotFoundError:
            pass
        else:
            os.close(old)
            raise RuntimeError('application token already exists')
        write_receipt(fd, name, payload)
        READY_SENT = True
        os.write(control_fd, ('READY pid=%d start=%d pgrp=%d sid=%d token=%s\n' %
                 (identity['pid'], identity['start'], identity['pgrp'], identity['sid'], token)).encode())
        os.close(control_fd)
        child = os.fork()
        if child == 0:
            os.close(fd)
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(number, signal.SIG_DFL)
            try:
                os.execvp(argv[0], argv)
            except OSError as exc:
                sys.stderr.write('application exec failed: %s\n' % exc)
                os._exit(127)
        payload['state'] = 'running'
        write_receipt(fd, name, payload)
        main_code = None
        cleanup_at = None
        terminated = set()
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                # The only normal terminal proof: no children remain after
                # every adopted descendant has also been reaped.
                code = main_code if main_code is not None else 125
                if code == 0 and payload.get('cleanup_escalated'):
                    code = 125
                payload.update(state='exited', all_children_reaped=True,
                               exit_code=code,
                               delivered_signal=pending['signal'])
                write_receipt(fd, name, payload)
                return payload['exit_code']
            if pid:
                if pid == child:
                    main_code = os.waitstatus_to_exitcode(status)
                    if main_code < 0:
                        main_code = 128 - main_code
                    payload.update(main_exited=True, main_exit_code=main_code)
                    write_receipt(fd, name, payload)
                continue
            if pending['signal'] is not None:
                number = (signal.SIGKILL if time.monotonic() - pending['at'] >= 10
                          else pending['signal'])
                for item in descendants(os.getpid()):
                    try:
                        signal_exact(item['pid'], item['start'], number)
                    except ProcessLookupError:
                        pass
            elif exit_policy == 'stop-children' and main_code is not None:
                # Explicit foreground contract only. Default launchers may
                # intentionally exit before their UI and must keep waiting.
                owned = descendants(os.getpid())
                if owned and cleanup_at is None:
                    cleanup_at = time.monotonic()
                    payload.update(cleanup_reason='main-exit', cleanup_signal=signal.SIGTERM,
                                   cleanup_escalated=False)
                    write_receipt(fd, name, payload)
                for item in owned:
                    number = (signal.SIGKILL if time.monotonic() - cleanup_at >= 10
                              else signal.SIGTERM)
                    key = (item['pid'], item['start'])
                    if number == signal.SIGTERM and key in terminated:
                        continue
                    try:
                        if signal_exact(item['pid'], item['start'], number):
                            terminated.add(key)
                            if number == signal.SIGKILL and not payload['cleanup_escalated']:
                                payload['cleanup_escalated'] = True
                                write_receipt(fd, name, payload)
                    except ProcessLookupError:
                        pass
            time.sleep(.05)
    finally:
        os.close(fd)


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    mode, token, root = args[:3]
    if mode == 'probe':
        print(json.dumps(observe(root, token)))
        return 0
    if mode == 'signal':
        value = observe(root, token)
        expected_pid, expected_start, number = map(int, args[3:6])
        if value.get('pid') != expected_pid or value.get('start') != expected_start:
            value = {'state': 'unknown', 'reason': 'supervisor identity changed'}
        elif value['state'] == 'running':
            try:
                if signal_exact(expected_pid, expected_start, number):
                    value = {'state': 'signalled'}
            except (OSError, AttributeError):
                value = {'state': 'unknown', 'reason': 'supervisor signal unverified'}
        print(json.dumps(value))
        return 0
    if mode not in ('run', 'run-stop-children'):
        raise ValueError('unknown application supervisor mode')
    control_fd = int(args[3])
    try:
        return run(root, token, control_fd, args[4:],
                   exit_policy='stop-children' if mode == 'run-stop-children' else 'wait')
    except BaseException as exc:
        # ERROR is a pre-execution proof only while the control FD remains open.
        # After READY the parent treats failures as unknown without terminal proof.
        if not READY_SENT:
            try:
                os.write(control_fd, ('ERROR application-supervisor: %s\n' % exc).encode())
            except OSError:
                pass
        sys.stderr.write('application supervisor failed: %s\n' % exc)
        return 125


if __name__ == '__main__':
    sys.exit(main())
