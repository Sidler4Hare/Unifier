"""Per-environment application leases and an optional filtered desktop bridge.

No watcher or container is started at login. The launch process owns its lease;
the final managed launch stops only the environment that this group started.
A short-lived launcher is not a reliable lease owner: so supervised_open()
moves the lease into one detached worker that survives the launcher, including
a SIGKILL, and releases it only when the application really exits.

Application leases bind the environment generation, CID and guest execution
token to a subreaper receipt. A dead host worker alone cannot release them;
the guest must prove its complete descendant tree ended. Legacy application
leases without that proof remain available for explicit recovery.
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import select
import signal
import stat
import subprocess
import sys
import time
import uuid

from unifier import __version__
from unifier.desktop_vendor import SHARED_BRIDGE_OWN_NAMES
from unifier.errors import UnifierError
from unifier.file_chooser import MODE_KEY, PORTAL_MODES, preflight_picker, verified_shares
from unifier.identity import binding_for, identity, validate_binding
from unifier.process_identity import process_stamp
from unifier.runtime import ExecResult
from unifier.file_access import PROTOCOL_VERSION
from unifier.file_access_store import AccessStore

# Set for the detached worker so a second supervision layer can never recurse.
WORKER_MARKER = 'MULTILINUX_APP_WORKER'
# Bounded waits keep a broken proxy or a stuck podman stop from hanging a launch.
BRIDGE_READY_SECONDS = 5.0
BRIDGE_STOP_SECONDS = 2.0


def live_leases(items):
    live = {}
    for key, item in items.items():
        if not isinstance(item, dict):
            raise UnifierError('invalid lease entry; refusing to discard ownership')
        # A project session lease is evidence for a guest execution. A dead
        # host proxy is not proof that the guest ended, so retain the lease
        # until ProjectSessions.recover() verifies the guest identity.
        if item.get('kind') in {'project-session', 'application'} or item.get('app_id'):
            live[key] = item
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


def _bridge_directory(bridge, record):
    if not isinstance(bridge, dict) or not isinstance(bridge.get('socket'), str):
        raise UnifierError('invalid Portal bridge ownership record')
    if record is None:
        directory = Path(bridge['socket']).parent
    else:
        settings = (record.get('desktop') or {}).get('portal_proxy') or {}
        if not settings.get('directory'):
            raise UnifierError('Portal bridge has no registered directory')
        directory = Path(settings['directory'])
        owner = identity(record)
        if 'environment_identity' in bridge and bridge['environment_identity'] != (
                list(owner) if owner is not None else None):
            raise UnifierError('Portal bridge belongs to a different environment generation')
    if (not directory.is_absolute() or directory.resolve() != directory
            or Path(bridge['socket']) != directory / 'bus'):
        raise UnifierError('Portal bus socket is outside its registered directory')
    return directory


def _stop_bridge_process(entry):
    pid, stamp = entry.get('pid'), entry.get('stamp')
    if type(pid) is not int or pid <= 0 or type(stamp) is not int or stamp <= 0:
        raise UnifierError('Portal bridge process identity is missing or invalid')
    _terminate(pid, stamp)
    deadline = time.monotonic() + BRIDGE_STOP_SECONDS
    while process_stamp(pid) == stamp:
        if time.monotonic() >= deadline:
            raise UnifierError(f'Portal bridge process {pid} did not exit')
        time.sleep(.02)


def _reclaim_snapshot(parent_fd, directory, name, expected):
    from unifier.file_access_store import reclaim_directory
    return reclaim_directory(parent_fd, directory, name, expected)


def _remove_bridge_files(directory, socket_name, selection=None):
    try:
        parent_fd = os.open(directory, os.O_DIRECTORY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    try:
        if selection is not None:
            _reclaim_snapshot(parent_fd, directory, *selection)
        try:
            socket_info = os.stat(socket_name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISSOCK(socket_info.st_mode):
            raise UnifierError(f'Portal socket path is not a socket: {directory / socket_name}')
        os.unlink(socket_name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


def _stop_file_delivery(delivery, directory):
    socket = directory / 'files-bus'
    if not isinstance(delivery, dict) or delivery.get('socket') != str(socket):
        raise UnifierError('Portal delivery socket is outside its registered directory')
    _stop_bridge_process(delivery)
    selected = delivery.get('selection_directory')
    expected = delivery.get('selection_identity')
    if not isinstance(selected, str) or not isinstance(expected, list) or (
            len(expected) != 2 or any(type(value) is not int or value < 0 for value in expected)):
        raise UnifierError('Portal snapshot ownership record is incomplete')
    selected = Path(selected)
    if selected.parent != directory or not selected.name.startswith('selected-'):
        raise UnifierError(f'Portal snapshot directory is outside its registered directory: {selected}')
    ledger = None
    if delivery.get('grant_lease'):
        from unifier.file_access_store import ACTIVE
        root = directory.parent.parent.parent
        ledger = AccessStore(root).ledger(delivery['grant_lease'])
        if (ledger.header['environment_binding'] != delivery['environment_binding']
                or ledger.header['selection_directory'] != str(selected)
                or ledger.header['selection_identity'] != expected):
            raise UnifierError('Portal access ledger identity changed')
        if (not selected.exists() and any(grant['state'] in ACTIVE
                                         for grant in ledger.read()['grants'].values())):
            raise UnifierError('Portal active snapshot disappeared; preserve its ledger for reconciliation')
    try:
        _remove_bridge_files(directory, socket.name, (selected.name, expected))
        if ledger is not None:
            ledger.release_all()
    except OSError as exc:
        raise UnifierError(f'cannot clean up Portal delivery at {selected}: {exc}') from exc


def stop_bridge(bridge, *, record=None):
    if bridge is None:
        return
    directory = _bridge_directory(bridge, record)
    error = None
    try:
        delivery = bridge.get('file_delivery')
        if delivery is not None:
            _stop_file_delivery(delivery, directory)
    except UnifierError as exc:
        error = exc
    try:
        _stop_bridge_process(bridge)
        _remove_bridge_files(directory, 'bus')
    except (OSError, UnifierError) as exc:
        message = f'cannot clean up Portal bus at {directory}: {exc}'
        if error is not None:
            message = f'{error}; {message}'
        raise UnifierError(message) from exc
    if error is not None:
        raise error


def _bridge_alive(bridge):
    """A usable bridge needs both a live process and its bound socket."""
    if not isinstance(bridge, dict):
        return False
    pid, stamp, socket = bridge.get('pid'), bridge.get('stamp'), bridge.get('socket')
    if type(pid) is not int or pid <= 0 or type(stamp) is not int or stamp <= 0:
        return False
    if not isinstance(socket, str) or process_stamp(pid) != stamp:
        return False
    try:
        return stat.S_ISSOCK(Path(socket).lstat().st_mode)
    except FileNotFoundError:
        return False


def _start_bus_bridge(record, bridge=None):
    settings = (record.get('desktop') or {}).get('portal_proxy')
    if bridge is not None:
        _bridge_directory(bridge, record)
    if not settings:
        return None
    directory = Path(settings['directory'])
    owner = identity(record)
    if not directory.is_absolute() or directory.resolve() != directory:
        raise UnifierError('Portal bridge directory is not a canonical host path')
    if settings.get('managed_mount') is not True:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        auth = os.environ.get('XAUTHORITY')
        if auth:
            source = Path(auth)
            if not source.is_file():
                raise UnifierError(f'current XAUTHORITY is unavailable: {auth}')
            shutil.copyfile(source, directory / 'Xauthority')
            (directory / 'Xauthority').chmod(0o600)
    if _bridge_alive(bridge):
        return bridge
    if bridge:
        # Half of a bridge is never reused: the proxy died or its socket is gone.
        stop_bridge(bridge, record=record)
    executable = shutil.which('xdg-dbus-proxy')
    if not executable:
        raise UnifierError('desktop portals require host xdg-dbus-proxy')
    address = os.environ.get('DBUS_SESSION_BUS_ADDRESS')
    if not address:
        raise UnifierError('current host session has no DBUS_SESSION_BUS_ADDRESS')
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
               *(f'--own={name}' for name in SHARED_BRIDGE_OWN_NAMES)]
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
        raise UnifierError('desktop portal proxy did not become ready')
    return {'pid': process.pid, 'stamp': process_stamp(process.pid), 'socket': str(socket),
            'environment_identity': list(owner) if owner is not None else None}


def start_bridge(record, bridge=None, *, file_delivery=False, mounts=()):
    if file_delivery:
        binding_for(record)  # Validate before starting a proxy we could otherwise orphan.
    current = _start_bus_bridge(record, bridge)
    if not file_delivery or current is None:
        return current
    try:
        return _start_file_delivery(record, current, mounts)
    except BaseException:
        if current is not bridge:
            stop_bridge(current, record=record)
        raise


def _start_file_delivery(record, current, mounts):
    settings = record['desktop']['portal_proxy']
    directory = Path(settings['directory'])
    shares = verified_shares(record, mounts)
    existing = current.get('file_delivery')
    if (isinstance(existing, dict) and existing.get('socket') == str(directory / 'files-bus')
            and _bridge_alive(existing)):
        if existing.get('protocol_version') != PROTOCOL_VERSION:
            raise UnifierError('active file access bridge uses an older protocol; finish its application leases before relaunching')
        if existing.get('shares') != [list(item) for item in shares]:
            raise UnifierError('Portal delivery shares changed during an active lease')
        return current
    if existing is not None:
        _stop_file_delivery(existing, directory)
    root = directory.parent.parent.parent
    binding = binding_for(record)
    state_directory = AccessStore(root).directory
    for source, _target, _rw, kind in mounts:
        if kind == 'bind' and state_directory.is_relative_to(Path(source)):
            raise UnifierError('file access policy and grant ledger must not be exposed by a guest mount')
    grant_lease = uuid.uuid4().hex
    socket = directory / 'files-bus'
    socket.unlink(missing_ok=True)
    audit_dir = directory.parent.parent / 'portal-delivery'
    audit_dir.mkdir(mode=0o700, exist_ok=True)
    audit = audit_dir / (record['id'] + '-' + uuid.uuid4().hex + '.jsonl')
    # Isolated interpreter imports only this release, never guest-writable cwd.
    command = ['/usr/bin/python3', '-I', '-c',
               'import sys; sys.path.insert(0, sys.argv.pop(1)); from unifier.portal_file_bridge import main; main()',
               str(Path(__file__).resolve().parent.parent), '--guest', settings['container'],
               '--shares', json.dumps(shares), '--parent', str(current['pid']),
               '--stamp', str(current['stamp']), '--access-root', str(root),
               '--environment-binding', json.dumps(binding), '--lease-id', grant_lease]
    read_fd, write_fd = os.pipe2(os.O_CLOEXEC | os.O_NONBLOCK)
    command.extend(['--ready-fd', str(write_fd)])
    try:
        with audit.open('xb') as log:
            os.chmod(audit, 0o600)
            process = subprocess.Popen(command, cwd=directory, stdin=subprocess.DEVNULL,
                                       stdout=log, stderr=log, start_new_session=True, pass_fds=(write_fd,))
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    entry = {'pid': process.pid, 'stamp': process_stamp(process.pid), 'socket': str(socket),
             'audit': str(audit), 'shares': [list(item) for item in shares],
             'protocol_version': PROTOCOL_VERSION, 'grant_lease': grant_lease,
             'environment_binding': binding}
    deadline = time.monotonic() + BRIDGE_READY_SECONDS
    ready_record = None
    pending = b''
    try:
        while time.monotonic() < deadline and process.poll() is None:
            if not select.select([read_fd], [], [], .02)[0]:
                continue
            block = os.read(read_fd, 4097 - len(pending))
            if not block:
                break
            pending += block
            if len(pending) > 4096:
                break
            if b'\n' in pending:
                value = json.loads(pending)
                selected = Path(value.get('selection_directory', ''))
                ident = value.get('selection_identity')
                if (value.get('protocol_version') == PROTOCOL_VERSION
                        and value.get('pid') == entry['pid'] and value.get('stamp') == entry['stamp']
                        and value.get('lease_id') == grant_lease and selected.parent == directory
                        and selected.name.startswith('selected-') and isinstance(ident, list)
                        and len(ident) == 2 and socket.is_socket()):
                    info = selected.lstat()
                    if stat.S_ISDIR(info.st_mode) and ident == [info.st_dev, info.st_ino]:
                        ready_record = value
                break
    except (OSError, ValueError, TypeError):
        pass
    finally:
        os.close(read_fd)
    if ready_record is None:
        _terminate(entry['pid'], entry['stamp'], entry['socket'])
        raise UnifierError(f'Portal selected-file delivery did not become ready; see {audit}')
    worker_version = ready_record.get('worker_version')
    entry.update(selection_directory=ready_record['selection_directory'],
                 selection_identity=ready_record['selection_identity'],
                 worker_version=(worker_version if isinstance(worker_version, str)
                                 and 0 < len(worker_version) <= 128 else None))
    owner = identity(record)
    return {**current, 'file_delivery': entry,
            'environment_identity': list(owner) if owner is not None else None}


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
        return {'schema_version': 3, 'leases': {}, 'auto_stop': False, 'bridge': None}

    def read(self):
        if not self.path.exists():
            return self._empty()
        try:
            state = json.loads(self.path.read_text())
        except json.JSONDecodeError as exc:
            raise UnifierError('lease state is corrupt; ownership recovery is required') from exc
        if not isinstance(state, dict):
            raise UnifierError('lease state must be an object; ownership recovery is required')
        if state.get('schema_version', 1) not in {1, 2, 3}:
            raise UnifierError('unsupported lease schema; refusing to discard ownership')
        state.setdefault('leases', {})
        state.setdefault('auto_stop', False)
        state.setdefault('bridge', None)
        if not isinstance(state['leases'], dict):
            raise UnifierError('invalid lease set; ownership recovery is required')
        state['leases'] = live_leases(state['leases'])
        return state

    def save(self, state):
        state['schema_version'] = 3
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

    def _acquire(self, *, app_id=None, expected_app_instance_uuid=None,
                 expected_execution_fingerprint=None, expected_binding=None,
                 owner=None, validate=None, owned_execution=False):
        """Take one lease in this environment's existing shared group.

        Application and project-session leases live in the same file, so they
        share one container instance and the existing auto-stop ownership.
        The optional ``validate`` callback rechecks caller-specific pins under
        the group lock and before ``service.start``.
        """
        token = uuid.uuid4().hex
        with self.locked():
            state = self.read()
            had_leases = bool(state['leases'])
            record = self.service.store.load(self.env_id)
            expected_app = None
            if app_id is not None:
                # Check the application pin before the bridge or any start call.
                # The supervisor must not start a same-name replacement environment
                # and only discover the stale binding later in exec_app.
                launch = self.service.show_app(app_id)
                self.service._require_app_execution(
                    launch, app_instance_uuid=expected_app_instance_uuid,
                    execution_fingerprint=expected_execution_fingerprint)
                binding = self.service._validate_launch_binding(launch, record=record)
                preflight_picker(record, launch)
                expected_app = {
                    "app_id": app_id,
                    "app_instance_uuid": expected_app_instance_uuid,
                    "execution_fingerprint": expected_execution_fingerprint,
                }
            else:
                validate_binding(expected_binding)
                self.service._require_record_binding(record, expected_binding)
                binding = expected_binding
            if validate is not None:
                validate(record)
            info = self.service._require_owned(record)
            if not state['leases']:
                state['auto_stop'] = self._inherits_auto_stop(state, info) or not info.running
                state['container_id'] = info.container_id
            settings = (record.get('desktop') or {}).get('portal_proxy') or {}
            managed = settings.get('managed_mount') is True
            portal_picker = app_id is not None and launch['env'].get(MODE_KEY) in PORTAL_MODES
            bridge = state.get('bridge')
            if portal_picker:
                if managed:
                    try:
                        self.service._resolve_managed_portal_mount(record)
                    except (OSError, ValueError) as exc:
                        raise UnifierError(f'cannot prepare the Portal bridge for {self.env_id}: {exc}') from exc
                bridge = start_bridge(record, bridge, file_delivery=True, mounts=info.mounts)
            elif not managed:
                bridge = start_bridge(record, bridge)
            entry = {'pid': os.getpid(), 'stamp': process_stamp(os.getpid()),
                     'worker_version': __version__}
            if app_id is not None:
                entry.update(app_id=app_id, kind='application',
                             environment_binding=binding_for(record), user=launch.get('user'))
                if owned_execution:
                    entry['execution'] = {'token': token, 'ownership': 'subreaper-v1',
                                          'state': 'not-started', 'guest': None}
            else:
                entry.update(owner or {})
                entry['kind'] = 'project-session'
            state['leases'][token] = entry
            state['bridge'] = bridge
            # Persist ownership before start/wait. A dead worker must not make
            # a possibly running guest disappear from the shared lease set.
            self.save(state)
            try:
                # The lease lock serializes launch groups; service.start waits
                # for a finishing podman stop and never double-starts an env.
                # expected_binding is rechecked under the lifecycle lock itself.
                self.service.start(
                    self.env_id, expected_binding=binding,
                    expected_app=expected_app)
            except BaseException:
                state['leases'].pop(token, None)  # No app exec has been attempted.
                try:
                    if not state['leases']:
                        stop_bridge(bridge, record=record)
                        state['bridge'] = None
                finally:
                    self.save(state)
                raise
            info = self.service._require_owned(self.service.store.load(self.env_id))
            if (had_leases and state.get('auto_stop')
                    and state.get('container_started_at') != info.started_at):
                state['auto_stop'] = False
            state['bridge'] = bridge
            state['container_id'] = info.container_id
            state['container_started_at'] = info.started_at
            self.save(state)
        return token

    def acquire(self, app_id, *, expected_app_instance_uuid=None,
                expected_execution_fingerprint=None, owned_execution=False):
        return self._acquire(
            app_id=app_id,
            expected_app_instance_uuid=expected_app_instance_uuid,
            expected_execution_fingerprint=expected_execution_fingerprint,
            owned_execution=owned_execution)

    def update_execution(self, token, **updates):
        with self.locked():
            state = self.read()
            entry = state['leases'].get(token)
            if entry is None or not isinstance(entry.get('execution'), dict):
                raise UnifierError('application execution lease is missing')
            self.service._require_record_binding(self.service.store.load(self.env_id),
                                                 entry['environment_binding'])
            entry['execution'].update(updates)
            self.save(state)

    def recover_applications(self, token=None):
        from unifier.application_leases import recover_applications
        return recover_applications(self, token)

    def acquire_project(self, project_uuid, *, expected_binding, validate=None):
        """Take a project-session lease in the same group as application leases."""
        if not isinstance(project_uuid, str) or not project_uuid:
            raise UnifierError('project UUID must be a non-empty string')
        return self._acquire(
            expected_binding=expected_binding,
            owner={'project_uuid': project_uuid},
            validate=validate)

    def release(self, token):
        with self.locked():
            state = self.read()
            if token not in state['leases']:
                return  # A concurrent terminal collector already released it.
            entry = state['leases'].get(token) or {}
            execution = entry.get('execution')
            if execution and execution.get('state') != 'exited':
                raise UnifierError('application termination is unverified; recover this lease')
            if execution:
                record = self.service.store.load(self.env_id)
                self.service._require_record_binding(record, entry['environment_binding'])
                info = self.service._require_owned(record)
                if info.container_id != entry['environment_binding']['container_id']:
                    raise UnifierError('application lease container changed; recovery required')
            state['leases'].pop(token, None)
            try:
                if not state['leases']:
                    self._close_group(state)
            finally:
                # The lease is removed even when stopping the environment fails;
                # auto_stop stays set so the next managed launch retries cleanup.
                self.save(state)

    def _close_group(self, state):
        record = None
        try:
            record = self.service.store.load(self.env_id)
            info = self.service._require_owned(record)
        except UnifierError:
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
            bridge = state.get('bridge')
            if bridge is not None and record is None:
                raise UnifierError('cannot verify the registered Portal bridge during cleanup')
            stop_bridge(bridge, record=record)
            state['bridge'] = None


def managed_open(service, app_id, extra, *, expected_app_instance_uuid=None,
                 expected_execution_fingerprint=None, integration_uuid=None,
                 project_uuid=None):
    launch = service.show_app(app_id)
    # Read-only preflight before the lease can start the environment. The
    # integration journal is checked here, inside the detached worker, not only
    # in the short-lived launcher.
    if integration_uuid is not None:
        service.validate_integration_launch(
            integration_uuid, app_id=app_id, project_uuid=project_uuid,
            app_instance_uuid=expected_app_instance_uuid,
            execution_fingerprint=expected_execution_fingerprint)
    service._validate_launch_binding(launch)
    origin = launch.get('package_origin')
    if origin and origin['target'] != service.store.load(launch['environment_id'])['target']:
        raise UnifierError('recorded package target differs from the application environment')
    pins = {}
    if expected_app_instance_uuid is not None:
        pins["expected_app_instance_uuid"] = expected_app_instance_uuid
    if expected_execution_fingerprint is not None:
        pins["expected_execution_fingerprint"] = expected_execution_fingerprint
    group = LaunchGroup(service, launch['environment_id'])
    argv = list(launch['argv']) + [item for item in extra or [] if item != '']
    from unifier.application_leases import execute_application
    return execute_application(group, app_id, argv, pins=pins)


def managed_exec(service, app_id, argv, *, workdir=None,
                 expected_app_instance_uuid=None,
                 expected_execution_fingerprint=None):
    """Run one explicit argv in an on-demand app's environment under its lease.

    This is the B-stage foreground variant of managed_open: the lease is held
    by the calling process, the application runs with a caller-provided argv
    and cwd, and the environment stops after the last managed execution if the
    group owns it. A detached-worker variant for file open is not implemented.
    """
    launch = service.show_app(app_id)
    service._require_app_execution(
        launch, app_instance_uuid=expected_app_instance_uuid,
        execution_fingerprint=expected_execution_fingerprint)
    service._validate_launch_binding(launch)
    group = LaunchGroup(service, launch["environment_id"])
    from unifier.application_leases import execute_application
    return execute_application(group, app_id, argv, workdir=workdir, pins={
        'expected_app_instance_uuid': expected_app_instance_uuid,
        'expected_execution_fingerprint': expected_execution_fingerprint})


def worker_command(service, app_id, extra, *, expected_app_instance_uuid=None,
                   expected_execution_fingerprint=None, integration_uuid=None,
                   project_uuid=None):
    """The detached worker runs this checkout's own CLI entry point."""
    entry = Path(__file__).resolve().parents[2] / 'tools/unifier.py'
    command = [sys.executable, str(entry), 'app', '_worker', app_id]
    if integration_uuid is not None:
        command.extend(['--integration', integration_uuid])
    if expected_app_instance_uuid is not None:
        command.extend(['--app-instance', expected_app_instance_uuid])
    if expected_execution_fingerprint is not None:
        command.extend(['--execution-fingerprint',
                        expected_execution_fingerprint])
    if project_uuid is not None:
        command.extend(['--project', project_uuid])
    command.extend(['--', *extra])
    return command


def supervised_open(service, app_id, extra, *, expected_app_instance_uuid=None,
                    expected_execution_fingerprint=None, integration_uuid=None,
                    project_uuid=None):
    """Hold the lease in a detached worker, not in the short-lived launcher.

    The launcher only waits. If it is killed the worker keeps the lease and the
    environment still stops when the application window closes. Standard input
    and output are deliberately inherited so the application's own logging
    (for example WAYLAND_DEBUG on stderr) still reaches the caller.
    """
    if os.environ.get(WORKER_MARKER) == '1':
        # The worker must hold the lease itself; never spawn a nested supervisor.
        pins = {}
        if expected_app_instance_uuid is not None:
            pins["expected_app_instance_uuid"] = expected_app_instance_uuid
        if expected_execution_fingerprint is not None:
            pins["expected_execution_fingerprint"] = expected_execution_fingerprint
        if integration_uuid is not None:
            pins["integration_uuid"] = integration_uuid
        if project_uuid is not None:
            pins["project_uuid"] = project_uuid
        return managed_open(service, app_id, extra, **pins)
    if service is not None:
        # Fail before spawning a detached worker when the binding is already
        # stale. The worker still rechecks it under the lease lock.
        service.validate_app_binding(app_id)
        if integration_uuid is not None:
            service.validate_integration_launch(
                integration_uuid, app_id=app_id, project_uuid=project_uuid,
                app_instance_uuid=expected_app_instance_uuid,
                execution_fingerprint=expected_execution_fingerprint)
    command = worker_command(
        service, app_id, extra,
        expected_app_instance_uuid=expected_app_instance_uuid,
        expected_execution_fingerprint=expected_execution_fingerprint,
        integration_uuid=integration_uuid, project_uuid=project_uuid)
    if service is not None:
        from unifier.install import worker_environment
        environment = worker_environment(service.root, os.environ)
    else:
        environment = dict(os.environ)
    environment[WORKER_MARKER] = '1'
    try:
        worker = subprocess.Popen(command, start_new_session=True, env=environment)
    except OSError as exc:
        raise UnifierError(f'cannot start the supervised launcher: {exc}') from exc
    return ExecResult(exit_status(worker.wait()), '', '')
