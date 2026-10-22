"""Per-environment application leases and an optional filtered desktop bridge.

No watcher or container is started at login. The launch process owns its lease;
the final managed launch stops only the environment that this group started.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import time
import uuid

from multilinux.errors import MultilinuxError


def process_stamp(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else int(fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError):
        return None


def live_leases(items):
    return {key: item for key, item in items.items()
            if process_stamp(item['pid']) == item['stamp'] and item['stamp'] is not None}


def stop_bridge(bridge):
    if bridge and bridge.get('stamp') is not None and process_stamp(bridge['pid']) == bridge['stamp']:
        try:
            os.kill(bridge['pid'], signal.SIGTERM)
        except ProcessLookupError:
            pass


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
    if bridge and process_stamp(bridge['pid']) == bridge.get('stamp') and bridge.get('stamp') is not None:
        return bridge
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
    deadline = time.monotonic() + 5
    while not socket.exists() and time.monotonic() < deadline and process.poll() is None:
        time.sleep(.02)
    if not socket.exists() or process.poll() is not None:
        if process.poll() is None:
            process.terminate(); process.wait(timeout=5)
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

    def read(self):
        if not self.path.exists():
            return {'leases': {}, 'auto_stop': False, 'bridge': None}
        state = json.loads(self.path.read_text())
        state['leases'] = live_leases(state.get('leases', {}))
        return state

    def save(self, state):
        self.service.store._write_json(self.path, state)

    def acquire(self, app_id):
        token = uuid.uuid4().hex
        with self.locked():
            state = self.read()
            record = self.service.store.load(self.env_id)
            info = self.service._require_owned(record)
            if not state['leases']:
                state['auto_stop'] = not info.running
                state['container_id'] = info.container_id
            bridge = start_bridge(record, state.get('bridge'))
            try:
                self.service.start(self.env_id)
            except BaseException:
                if not state['leases']:
                    stop_bridge(bridge)
                raise
            state['bridge'] = bridge
            state['leases'][token] = {'pid': os.getpid(), 'stamp': process_stamp(os.getpid()), 'app_id': app_id}
            self.save(state)
        return token

    def release(self, token):
        with self.locked():
            state = self.read()
            state['leases'].pop(token, None)
            if not state['leases']:
                record = self.service.store.load(self.env_id)
                info = self.service._require_owned(record)
                if state.get('auto_stop') and info.container_id == state.get('container_id'):
                    try:
                        self.service.stop(self.env_id)
                    finally:
                        stop_bridge(state.get('bridge'))
                        state['bridge'] = None
                elif not info.running:
                    stop_bridge(state.get('bridge'))
                    state['bridge'] = None
            self.save(state)


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
