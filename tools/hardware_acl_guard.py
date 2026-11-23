"""One-shot AMD render-node ACL lease for the approved v0.0.8 DRM phase."""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time


ROOT = Path('/run/unifier-v008-drm-20261115a')
HELPER = ROOT / 'helper.py'
NODE = Path('/dev/dri/renderD128')
PCI = '0000:01:00.0'
PCI_ID = '1002:6778'
TARGET_UID = 1001
MAX_FILE = 8192
DEADLINE_US = 300_000_000
MIN_GRANT_REMAINING_US = 30_000_000
PYTHON = ('/usr/bin/python3', '-I', '-S', '-B', str(HELPER))
PHASES = ('PREPARED', 'ARMED', 'GRANTING', 'APPLIED', 'RESTORED', 'CONFLICT')
LIMITS = {'memory.max': '16777216', 'memory.high': '12582912',
          'memory.swap.max': '0', 'pids.max': '4'}
UNIT_PROPERTIES = {
    'LoadState': 'loaded', 'ActiveState': 'active', 'SubState': 'running', 'Type': 'exec',
    'Transient': 'yes', 'User': 'root', 'Restart': 'no', 'KillMode': 'control-group',
    'UMask': '0077', 'LimitCORE': '0', 'MemoryMax': LIMITS['memory.max'],
    'MemoryHigh': LIMITS['memory.high'], 'MemorySwapMax': '0', 'TasksMax': '4',
}


class Conflict(RuntimeError):
    pass


def token_value(token):
    if not re.fullmatch(r'[a-z0-9-]{1,40}', token):
        raise ValueError('invalid ACL lease token')
    return token


def unit_name(token):
    return f'unifier-v008-amd-acl-{token_value(token)}.service'


def read_small(path, limit=MAX_FILE):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'bounded read exceeded: {path.name}')
    return data.decode('ascii').strip()


def safe_directory(path, *, private=False):
    info = path.lstat()
    mode = stat.S_IMODE(info.st_mode)
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or mode & 0o022 or (private and mode != 0o700)):
        raise ValueError(f'untrusted directory: {path}')


def verify_root_paths():
    if os.geteuid() != 0 or Path(__file__).absolute() != HELPER:
        raise ValueError('use the installed, fixed root helper')
    for path in reversed(ROOT.parents):
        safe_directory(path)
    safe_directory(ROOT, private=True)
    info = HELPER.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
            or not 0 < info.st_size <= 65_536):
        raise ValueError('untrusted installed helper')


def state_file(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or info.st_size > MAX_FILE):
        os.close(fd)
        raise ValueError(f'untrusted state file: {path.name}')
    return fd


def state_read(path):
    with os.fdopen(state_file(path, os.O_RDONLY), 'rb') as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ValueError(f'state exceeds read bound: {path.name}')
    return data


def sync_directory(path):
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def state_write(path, data):
    if len(data) > MAX_FILE or os.path.lexists(path):
        raise ValueError(f'cannot create bounded, fresh state file: {path.name}')
    temporary = path.with_name(f'.{path.name}.tmp-{os.getpid()}')
    with os.fdopen(state_file(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL), 'wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.rename(temporary, path)  # All writers hold the same root-only lock.
    sync_directory(path.parent)


@contextmanager
def locked():
    with os.fdopen(state_file(ROOT / 'acl.lock', os.O_RDWR | os.O_CREAT), 'r+b') as stream:
        deadline = time.monotonic() + 3
        for _ in range(61):
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError('ACL lease lock is busy')
                time.sleep(.05)
        else:
            raise RuntimeError('ACL lease lock is busy')
        yield


def marker(directory, phase, **details):
    state_write(directory / f'{phase}.json', json.dumps(
        {'phase': phase, 'monotonic_us': monotonic_us(), **details}, separators=(',', ':')).encode('ascii'))


def read_marker(directory, phase):
    value = json.loads(state_read(directory / f'{phase}.json'))
    if not isinstance(value, dict) or value.get('phase') != phase:
        raise ValueError(f'invalid phase marker: {phase}')
    return value


def latest(directory):
    for phase in reversed(PHASES):
        path = directory / f'{phase}.json'
        if os.path.lexists(path):
            return read_marker(directory, phase)
    raise ValueError('lease preparation is incomplete')


def monotonic_us():
    return time.monotonic_ns() // 1000


def command(argv, *, pass_fds=(), data=None):
    result = subprocess.run(argv, input=data, capture_output=True, check=False,
                            pass_fds=pass_fds, timeout=4,
                            env={'LC_ALL': 'C', 'LANG': 'C', 'PATH': '/usr/bin:/bin'})
    if result.returncode != 0 or len(result.stdout) > MAX_FILE or len(result.stderr) > MAX_FILE:
        detail = result.stderr[-400:].decode('ascii', errors='replace').strip()
        raise RuntimeError(f'{Path(argv[0]).name} result {result.returncode}: {detail}')
    return result.stdout


def node_identity(fd):
    info = os.fstat(fd)
    current = NODE.lstat()
    if (not stat.S_ISCHR(info.st_mode) or not stat.S_ISCHR(current.st_mode)
            or (current.st_dev, current.st_ino, current.st_rdev) != (info.st_dev, info.st_ino, info.st_rdev)
            or (os.major(info.st_rdev), os.minor(info.st_rdev)) != (226, 128)
            or (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode)) != (0, 993, 0o660)
            or (current.st_uid, current.st_gid, stat.S_IMODE(current.st_mode)) != (0, 993, 0o660)):
        raise Conflict('render node type, device number, inode binding or owner/mode differs')
    render = Path('/sys/dev/char/226:128').resolve(strict=True)
    device = (render / 'device').resolve(strict=True)
    expected = Path('/sys/bus/pci/devices') / PCI
    vendor = read_small(device / 'vendor', 64)
    pci_device = read_small(device / 'device', 64)
    driver = (device / 'driver').resolve(strict=True)
    if (render != Path('/sys/class/drm/renderD128').resolve(strict=True)
            or device.name != PCI or device != expected.resolve(strict=True)
            or f'{vendor.removeprefix("0x")}:{pci_device.removeprefix("0x")}' != PCI_ID
            or driver.name != 'radeon'):
        raise Conflict('render node sysfs PCI identity or driver differs')
    return {'boot': read_small(Path('/proc/sys/kernel/random/boot_id'), 64),
            'dev': info.st_dev, 'inode': info.st_ino, 'rdev': info.st_rdev, 'uid': info.st_uid,
            'gid': info.st_gid, 'mode': stat.S_IMODE(info.st_mode), 'pci': PCI, 'pci_id': PCI_ID,
            'render_sysfs': str(render), 'pci_sysfs': str(device), 'driver': str(driver)}


def verify_node(expected, observed):
    if expected != observed:
        raise Conflict('render node no longer matches the prepared identity')


@contextmanager
def open_node():
    for parent in reversed(NODE.parents):
        safe_directory(parent)
    fd = os.open(NODE, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        yield fd
    finally:
        os.close(fd)


def acl_for_fd(acl, old, new):
    header = f'# file: {old}\n'.encode('ascii')
    if not acl.startswith(header):
        raise ValueError('unexpected ACL object header')
    return f'# file: {new}\n'.encode('ascii') + acl[len(header):]


def current_acl(fd):
    alias = f'/proc/self/fd/{fd}'
    raw = command(('/usr/bin/getfacl', '-pn', '--', alias), pass_fds=(fd,))
    return acl_for_fd(raw, alias, NODE)


def expected_acl(original):
    header = f'# file: {NODE}\n# owner: 0\n# group: 993\n'.encode('ascii')
    baseline = header + b'user::rw-\ngroup::rw-\nother::---\n'
    if original not in (baseline, baseline + b'\n'):
        raise Conflict('original ACL does not match the approved base permissions')
    return original.replace(b'group::rw-\n', f'user:{TARGET_UID}:rw-\ngroup::rw-\nmask::rw-\n'.encode('ascii'))


def grant_acl(fd):
    alias = f'/proc/self/fd/{fd}'
    command(('/usr/bin/setfacl', '-m', f'u:{TARGET_UID}:rw', '--', alias), pass_fds=(fd,))


def restore_acl(fd, original):
    alias = f'/proc/self/fd/{fd}'
    command(('/usr/bin/setfacl', '--restore=-'), pass_fds=(fd,), data=acl_for_fd(original, NODE, alias))


def setup(directory, token):
    for count, candidate in enumerate(ROOT.iterdir(), 1):
        if count > 128:
            raise ValueError('ACL state directory exceeds its bounded lease history')
        if re.fullmatch(r'[a-z0-9-]{1,40}', candidate.name):
            safe_directory(candidate, private=True)
            if latest(candidate)['phase'] != 'RESTORED':
                raise Conflict('an earlier AMD ACL lease needs reconciliation')
    with open_node() as fd:
        identity = node_identity(fd)
        original = current_acl(fd)
        applied = expected_acl(original)
        verify_node(identity, node_identity(fd))
    directory.mkdir(mode=0o700)
    sync_directory(ROOT)
    state_write(directory / 'original.acl', original)
    state_write(directory / 'expected.acl', applied)
    marker(directory, 'PREPARED', node=identity, token=token, unit=unit_name(token),
           original_sha256=hashlib.sha256(original).hexdigest(), expected_sha256=hashlib.sha256(applied).hexdigest())


def load_prepared(directory, token):
    prepared = read_marker(directory, 'PREPARED')
    original = state_read(directory / 'original.acl')
    applied = state_read(directory / 'expected.acl')
    if (prepared['token'] != token or prepared['unit'] != unit_name(token)
            or hashlib.sha256(original).hexdigest() != prepared['original_sha256']
            or hashlib.sha256(applied).hexdigest() != prepared['expected_sha256']
            or applied != expected_acl(original)):
        raise Conflict('prepared ACL lease identity or backup differs')
    return prepared['node'], original, applied


def verify_exec(properties, field, token, verb):
    argv = ' '.join((*PYTHON, verb, token))
    expected = '{ path=/usr/bin/python3 ; argv[]=' + argv + ' ; ignore_errors=no ; '
    value = properties.get(field, '')
    if not value.startswith(expected) or value.count('argv[]=') != 1:
        raise ValueError(f'{field} does not name the exact root helper')


def verify_unit(token, *, self_is_main=False):
    name = unit_name(token)
    fields = (*UNIT_PROPERTIES, 'Id', 'InvocationID', 'MainPID', 'ControlGroup', 'RuntimeMaxUSec', 'TimeoutStopUSec',
              'ExecMainStartTimestampMonotonic', 'CPUQuotaPerSecUSec', 'ExecStart', 'ExecStopPost')
    raw = command(('/usr/bin/systemctl', '--system', '--no-pager', 'show',
                   *[f'--property={field}' for field in fields], name))
    values = dict(line.split('=', 1) for line in raw.decode('ascii').splitlines() if '=' in line)
    if (any(values.get(key) != value for key, value in UNIT_PROPERTIES.items())
            or values.get('Id') != name or values.get('ControlGroup') != f'/system.slice/{name}'
            or values.get('RuntimeMaxUSec') not in ('5min', '300s', '300000000')
            or values.get('TimeoutStopUSec') not in ('15s', '15000000')
            or values.get('CPUQuotaPerSecUSec') not in ('1s', '1000000')
            or not re.fullmatch(r'[a-f0-9]{32}', values.get('InvocationID', ''))):
        raise ValueError('watchdog unit identity, lifetime or configured budget differs')
    verify_exec(values, 'ExecStart', token, 'hold')
    verify_exec(values, 'ExecStopPost', token, 'restore')
    pid = int(values['MainPID'])
    if pid <= 1 or (self_is_main and (pid != os.getpid() or os.environ.get('INVOCATION_ID') != values['InvocationID'])):
        raise ValueError('watchdog is not the root unit main process')
    proc = Path('/proc') / str(pid)
    process_group = read_small(proc / 'cgroup')
    status = read_small(proc / 'status')
    process_stat = read_small(proc / 'stat')
    start_ticks = int(process_stat.rsplit(') ', 1)[1].split()[19])
    expected_group = f'0::{values["ControlGroup"]}'
    if expected_group not in process_group.splitlines() or not re.search(r'^Uid:\s+0\s+0\s+0\s+0$', status, re.M):
        raise ValueError('watchdog process lacks root ownership or cgroup coverage')
    group = Path('/sys/fs/cgroup') / values['ControlGroup'].lstrip('/')
    actual_limits = {field: read_small(group / field, 64) for field in LIMITS}
    if actual_limits != LIMITS:
        raise ValueError('effective watchdog cgroup hard limits differ')
    quota, period = (int(value) for value in read_small(group / 'cpu.max', 64).split())
    if not 0 < quota == period:
        raise ValueError('effective watchdog CPU limit is not one CPU')
    started = int(values['ExecMainStartTimestampMonotonic'])
    now = monotonic_us()
    if started <= 0 or not started <= now < started + DEADLINE_US:
        raise ValueError('watchdog has no remaining systemd lifetime')
    return {'unit': name, 'invocation_id': values['InvocationID'], 'pid': pid, 'start_ticks': start_ticks,
            'cgroup': values['ControlGroup'], 'limits': {**actual_limits, 'cpu.max': f'{quota} {period}'},
            'started_us': started, 'deadline_us': started + DEADLINE_US, 'stop_timeout_us': 15_000_000}


def allow_grant(phase, armed, active, now):
    if phase != 'ARMED' or active != armed or now >= armed['deadline_us']:
        raise Conflict('grant requires the same active, unexpired ARMED watchdog')


def request_grant(directory, token):
    armed = read_marker(directory, 'ARMED')['watchdog']
    allow_grant(latest(directory)['phase'], armed, verify_unit(token), monotonic_us())
    state_write(directory / 'grant.request', json.dumps(armed, separators=(',', ':')).encode('ascii'))


def apply_grant(directory, token, armed):
    active = verify_unit(token, self_is_main=True)
    allow_grant(latest(directory)['phase'], armed, active, monotonic_us())
    if json.loads(state_read(directory / 'grant.request')) != armed:
        raise Conflict('grant request is not bound to the armed watchdog')
    identity, original, applied = load_prepared(directory, token)
    with open_node() as fd:
        verify_node(identity, node_identity(fd))
        if current_acl(fd) != original:
            raise Conflict('ACL changed between preparation and grant')
        marker(directory, 'GRANTING', watchdog=armed)
        if armed['deadline_us'] - monotonic_us() < MIN_GRANT_REMAINING_US:
            raise Conflict('less than 30 seconds remain on the watchdog before granting ACL')
        grant_acl(fd)
        verify_node(identity, node_identity(fd))
        if current_acl(fd) != applied:
            raise Conflict('applied ACL differs from its precomputed permissions')
        marker(directory, 'APPLIED', watchdog=armed)


def hold(directory, token):
    with locked():
        if latest(directory)['phase'] != 'PREPARED':
            raise Conflict('watchdog may only arm the fresh prepared lease')
        armed = verify_unit(token, self_is_main=True)
        marker(directory, 'ARMED', watchdog=armed)
    while monotonic_us() < armed['deadline_us']:
        with locked():
            phase = latest(directory)['phase']
            if phase == 'ARMED' and os.path.lexists(directory / 'grant.request'):
                apply_grant(directory, token, armed)
            elif phase not in ('ARMED', 'APPLIED'):
                return phase
        time.sleep(.25)
    return 'DEADLINE'


def restore_needed(phase, current, original, applied):
    if phase == 'CONFLICT':
        raise Conflict('lease has a latched recovery conflict')
    if current == original:
        return False
    if phase in ('GRANTING', 'APPLIED') and current == applied:
        return True
    raise Conflict('current ACL differs from the ACL states owned by the lease')


def restore(directory, token):
    phase = latest(directory)['phase']
    try:
        identity, original, applied = load_prepared(directory, token)
        with open_node() as fd:
            verify_node(identity, node_identity(fd))
            needs_write = restore_needed(phase, current_acl(fd), original, applied)
            if needs_write:
                restore_acl(fd, original)
                verify_node(identity, node_identity(fd))
                if current_acl(fd) != original:
                    raise Conflict('restored ACL does not match the prepared original bytes')
        if phase != 'RESTORED':
            marker(directory, 'RESTORED', prior_phase=phase, acl_written=needs_write)
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError) as exc:
        if phase != 'CONFLICT':
            marker(directory, 'CONFLICT', prior_phase=phase, error=str(exc)[:400])
        raise Conflict(str(exc)) from exc


def status(directory, token):
    recorded = latest(directory)
    phase = recorded['phase']
    watchdog = recorded.get('watchdog')
    active = phase in ('ARMED', 'APPLIED')
    result = {'phase': phase, 'unit': unit_name(token), 'watchdog': watchdog,
              'error': recorded.get('error'), 'live_verified': False, 'remaining_us': None}
    if active and verify_unit(token) != watchdog:
        raise Conflict('watchdog no longer matches the recorded ACL lease')
    if phase in ('APPLIED', 'RESTORED'):
        identity, original, applied = load_prepared(directory, token)
        with open_node() as fd:
            verify_node(identity, node_identity(fd))
            acl = current_acl(fd)
            verify_node(identity, node_identity(fd))
        if acl != (applied if phase == 'APPLIED' else original):
            raise Conflict(f'current ACL does not match the recorded {phase} state')
        result['acl_sha256'] = hashlib.sha256(acl).hexdigest()
    if active:
        remaining = watchdog['deadline_us'] - monotonic_us()
        if remaining <= 0:
            raise Conflict('watchdog expired during ACL status verification')
        result['remaining_us'] = remaining
    result['live_verified'] = active or phase == 'RESTORED'
    return result


def main(argv):
    if len(argv) not in (2, 3) or (len(argv) == 3 and (argv[0] != 'request' or argv[2] != 'grant')):
        raise ValueError('expected setup/hold/status/restore TOKEN or request TOKEN grant')
    verb, token = argv[:2]
    if verb not in ('setup', 'hold', 'status', 'restore', 'request') or (verb == 'request' and len(argv) != 3):
        raise ValueError('invalid ACL lease action')
    directory = ROOT / token_value(token)
    os.umask(0o077)
    verify_root_paths()
    if verb == 'hold':
        safe_directory(directory, private=True)
        return {'phase': hold(directory, token), 'unit': unit_name(token)}
    with locked():
        if verb == 'setup':
            setup(directory, token)
        else:
            safe_directory(directory, private=True)
            if verb == 'status':
                return status(directory, token)
            if verb == 'request':
                request_grant(directory, token)
            elif verb == 'restore':
                restore(directory, token)
        result = latest(directory)
        return {'phase': result['phase'], 'unit': unit_name(token), 'watchdog': result.get('watchdog'),
                'error': result.get('error')}


if __name__ == '__main__':
    try:
        result = main(sys.argv[1:])
        print(json.dumps(result, separators=(',', ':')), flush=True)
        raise SystemExit(1 if result['phase'] == 'CONFLICT' else 0)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({'phase': 'CONFLICT' if isinstance(exc, Conflict) else 'ERROR',
                          'error': str(exc)[:500]}, separators=(',', ':')), flush=True)
        raise SystemExit(1)
