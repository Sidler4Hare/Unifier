"""Per-environment application leases and an optional filtered desktop bridge.

No watcher or container is started at login. The launch process owns its lease;
the final managed launch stops only the environment that this group started.
A short-lived launcher is not a reliable lease owner: so supervised_open()
moves the lease into one detached worker that survives the launcher, including
a SIGKILL, and releases it only when the application really exits.

Lease records are pid+start-stamp pairs. A recycled pid cannot renew an old
lease because process_stamp() changes when the pid is reused.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
import uuid

from multilinux.errors import MultilinuxError
from multilinux.runtime import ExecResult

# Set for the detached worker so a second supervision layer can never recurse.
WORKER_MARKER = 'MULTILINUX_APP_WORKER'
# Bounded waits keep a broken proxy or a stuck podman stop from hanging a launch.
BRIDGE_READY_SECONDS = 5.0
BRIDGE_STOP_SECONDS = 2.0


def process_stamp(pid):
    """Identity of one live process, or None when it is gone or a zombie."""
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else int(fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, TypeError, ValueError):
        return None


def live_leases(items):
    live = {}
    for key, item in items.items():
        if not isinstance(item, dict):
            continue
        stamp = item.get('stamp')
        pid = item.get('pid')
        if stamp is None or pid is None:
            continue
        if process_stamp(pid) == stamp:
            live[key] = item
    return live


def exit_status(code):
    """Translate wait()'s negative signal code to a shell-style exit status."""
    return code if code >= 0 else 128 - code


def _terminate(pid, stamp, socket=None):
    """Stop one bridge process, escalating only if SIGTERM is ignored."""
    if pid is not None and stamp is not None and process_stamp(pid) == stamp:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
        deadline = time.monotonic() + BRIDGE_STOP_SECONDS
        while time.monotonic() < deadline and process_stamp(pid) == stamp:
            time.sleep(.02)
        if process_stamp(pid) == stamp:
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
    if socket:
        try:
            Path(socket).unlink(missing_ok=True)
        except OSError:
            pass


def stop_bridge(bridge):
    if not bridge:
        return
    _terminate(bridge.get('pid'), bridge.get('stamp'), bridge.get('socket'))


def _bridge_alive(bridge):
    """A usable bridge needs both a live process and its bound socket."""
    if not bridge:
        return False
    return (bridge.get('stamp') is not None
            and process_stamp(bridge.get('pid')) == bridge.get('stamp')
            and bool(bridge.get('socket'))
            and Path(bridge['socket']).is_socket())


def start_bridge(record, bridge=None):
    settings = (record.get('desktop') or {}).get('portal_proxy')
    if not settings:
        return None
    directory = Path(settings['directory'])
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    auth = os.environ.get('XAUTHORITY')
    if auth:
        source = Path(auth)
        if not source.is_file():
            raise MultilinuxError(f'current XAUTHORITY is unavailable: {auth}')
        shutil.copyfile(source, directory / 'Xauthority')
        (directory / 'Xauthority').chmod(0o600)
    if _bridge_alive(bridge):
        return bridge
    if bridge:
        # Half of a bridge is never reused: the proxy died or its socket is gone.
        stop_bridge(bridge)
    executable = shutil.which('xdg-dbus-proxy')
    if not executable:
        raise MultilinuxError('desktop portals require host xdg-dbus-proxy')
    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    if not address:
        raise MultilinuxError('current host session has no DBUS_SESSION_BUS_ADDRESS')
    socket = directory / 'bus'
    socket.unlink(missing_ok=True)
    # bind(2) limits AF_UNIX names to 107 pathname bytes.  Project paths may
    # contain long experiment names or multibyte characters; bind relative to
    # the private mounted directory so the address remains just "bus".
    command = [executable, address, socket.name, '--filter',
               '--talk=org.freedesktop.portal.Desktop',
               '--talk=org.freedesktop.portal.Documents',
               '--talk=org.kde.StatusNotifierWatcher',
               '--talk=org.freedesktop.Notifications',
               '--own=com.tencent.wemeet.*']
    process = subprocess.Popen(command, cwd=directory,
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + BRIDGE_READY_SECONDS
    while not socket.is_socket() and time.monotonic() < deadline and process.poll() is None:
        time.sleep(.02)
    if not socket.is_socket() or process.poll() is not None:
        if process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                try:
                    process.kill()
                    process.wait()
                except OSError:
                    pass
        socket.unlink(missing_ok=True)
        raise MultilinuxError('desktop portal proxy did not become ready')
    return {'pid': process.pid, 'stamp': process_stamp(process.pid), 'socket': str(socket)}


class LaunchGroup:
    def __init__(self, service, env_id):
        self.service = service
        self.env_id = env_id
        directory = service.root / '.lab/leases'
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = directory / (env_id + '.json')
        self.lock_path = directory / (env_id + '.lock')

    @contextmanager
    def locked(self):
        fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _empty():
        return {'leases': {}, 'auto_stop': False, 'bridge': None}

    def read(self):
        if not self.path.exists():
            return self._empty()
        try:
            state = json.loads(self.path.read_text())
        except json.JSONDecodeError:
            return self._empty()
        if not isinstance(state, dict):
            return self._empty()
        state.setdefault('leases', {})
        state.setdefault('auto_stop', False)
        state.setdefault('bridge', None)
        if not isinstance(state['leases'], dict):
            state['leases'] = {}
        state['leases'] = live_leases(state['leases'])
        return state

    def save(self, state):
        self.service.store._write_json(self.path, state)

    @staticmethod
    def _inherits_auto_stop(state, info):
        """Whether this running container still belongs to the previous group.

        The last group's lease can disappear without a clean release (for
        example its process was killed). Ownership is inherited only when the
        record still names the same container instance that this project
        started; a manual stop/start changes started_at and must be preserved.
        Missing fields from an older record are treated conservatively: never
        stop an environment whose ownership cannot be proven.
        """
        if not state.get('auto_stop'):
            return False
        if not info.running:
            return True
        container_id = state.get('container_id')
        started_at = state.get('container_started_at')
        if not container_id or not started_at:
            return False
        if not info.container_id or not info.started_at:
            return False
        return (container_id == info.container_id
                and started_at == info.started_at)

    def acquire(self, app_id):
        token = uuid.uuid4().hex
        with self.locked():
            state = self.read()
            record = self.service.store.load(self.env_id)
            info = self.service._require_owned(record)
            if not state['leases']:
                state['auto_stop'] = self._inherits_auto_stop(state, info) or not info.running
                state['container_id'] = info.container_id
            bridge = start_bridge(record, state.get('bridge'))
            try:
                # The lease lock serializes launch groups; service.start waits
                # for a finishing podman stop and never double-starts an env.
                self.service.start(self.env_id)
            except BaseException:
                if not state['leases']:
                    stop_bridge(bridge)
                raise
            info = self.service._require_owned(self.service.store.load(self.env_id))
            state['bridge'] = bridge
            state['container_id'] = info.container_id
            state['container_started_at'] = info.started_at
            state['leases'][token] = {'pid': os.getpid(),
                                      'stamp': process_stamp(os.getpid()),
                                      'app_id': app_id}
            self.save(state)
        return token

    def release(self, token):
        with self.locked():
            state = self.read()
            state['leases'].pop(token, None)
            try:
                if not state['leases']:
                    self._close_group(state)
            finally:
                # The lease is removed even when stopping the environment fails;
                # auto_stop stays set so the next managed launch retries cleanup.
                self.save(state)

    def _close_group(self, state):
        try:
            record = self.service.store.load(self.env_id)
            info = self.service._require_owned(record)
        except MultilinuxError:
            info = None
        try:
            if (info is not None and state.get('auto_stop')
                    and info.container_id == state.get('container_id')):
                if info.running and state.get('container_started_at') != info.started_at:
                    # The user stop/started the container while this lease was
                    # held. Like a manual pre-start, the running instance is now
                    # theirs and must not be stopped when the window closes.
                    state['auto_stop'] = False
                else:
                    self.service.stop(self.env_id)
                    state['auto_stop'] = False
        finally:
            # The last lease is gone: no application can still be using the
            # filtered host portal proxy, so its bridge is always removed.
            stop_bridge(state.get('bridge'))
            state['bridge'] = None


def managed_open(service, app_id, extra):
    launch = service.show_app(app_id)
    origin = launch.get('package_origin')
    if origin and origin['target'] != service.store.load(launch['environment_id'])['target']:
        raise MultilinuxError('recorded package target differs from the application environment')
    group = LaunchGroup(service, launch['environment_id'])
    lease = group.acquire(app_id)
    try:
        return service._open_app_unmanaged(app_id, extra)
    finally:
        group.release(lease)


def worker_command(service, app_id, extra):
    """The detached worker runs this checkout's own CLI entry point."""
    entry = Path(__file__).resolve().parents[2] / 'tools/ml.py'
    return [sys.executable, str(entry), 'app', '_worker', app_id, '--', *extra]


def supervised_open(service, app_id, extra):
    """Hold the lease in a detached worker, not in the short-lived launcher.

    The launcher only waits. If it is killed the worker keeps the lease and the
    environment still stops when the application window closes. Standard input
    and output are deliberately inherited so the application's own logging
    (for example WAYLAND_DEBUG on stderr) still reaches the caller.
    """
    if os.environ.get(WORKER_MARKER) == '1':
        # The worker must hold the lease itself; never spawn a nested supervisor.
        return managed_open(service, app_id, extra)
    command = worker_command(service, app_id, extra)
    environment = dict(os.environ)
    environment[WORKER_MARKER] = '1'
    try:
        worker = subprocess.Popen(command, start_new_session=True, env=environment)
    except OSError as exc:
        raise MultilinuxError(f'cannot start the supervised launcher: {exc}') from exc
    return ExecResult(exit_status(worker.wait()), '', '')
