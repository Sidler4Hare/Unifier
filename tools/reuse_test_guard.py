"""Opt-in fixed-instance supervisor. Never creates or reconfigures containers.

The existing test slice contains the controller AND the existing container.
An independent systemd service supervises it; ExecStopPost runs rescue even
if the supervisor dies. All permission is tied to one spec and exact binding.
"""
from __future__ import annotations
import fcntl
import json
import math
import os
from pathlib import Path
import select
import signal
import sqlite3
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from tools.reuse_guard_policy import (GuardPolicy, approved_mounts, same_execution,
                                     ADMISSION_FULL, ADMISSION_SOME, ADMISSION_SECONDS)
from tools.test_runtime import SESSION_VARIABLES, cgroup_diagnostics, result_exit_code, short_temporary_directory

MIB = 1024**2
CG = Path('/sys/fs/cgroup')
SLICE = 'unifier-tests.slice'
# Absent spec key stays on the original profile. The second profile is the
# one transient-controller change authorized on 2026-11-06; other sizes refuse.
DEFAULT_CONTROLLER = {'memory_max': 192 * MIB, 'memory_high': 144 * MIB, 'tasks_max': 64}
AUTHORIZED_CONTROLLER = {'memory_max': 384 * MIB, 'memory_high': 288 * MIB, 'tasks_max': 128}
ADMITTED_CONTROLLERS = (DEFAULT_CONTROLLER, AUTHORIZED_CONTROLLER)
SH10_TASK_BINDING = {
    'task_profile': 'sh10-20261112-close-fixed',
    'unit': 'unifier-wps-close-1112-fixed',
    'cid': 'e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45',
    'project': str(ROOT / '.lab/guarded-expansion-20261103/environment'),
    'env_id': 'debian13',
    'environment_uuid': 'ae147c3e-3e65-41ac-ac8d-9257c9202696',
    'generation': 1,
}

REMOTE_PACKAGE_BINDINGS = {
    'fedora44': ('31324dbcefe5262532f4e8ce8d4bf7220a2a31ed8d5cc00f9e747b413d194156',
                 '808394a7-e158-47ef-a1d8-9cb24070de5b'),
    'arch': ('aa38e5466b9665765aefe9931f5550eec78631bfa820121469391845cef7e430',
             '5b2111b6-f07e-4a95-9791-8a208343ba13'),
}


def fixture_resources(config):
    """Use existing smaller remote fixtures; never raise or change their limits."""
    if 'fixture_profile' not in config:
        return {'slice': SLICE, 'memory': 768*MIB, 'memory_swap': 896*MIB}
    expected = REMOTE_PACKAGE_BINDINGS.get(config.get('env_id'))
    if (config.get('fixture_profile') != 'remote-package-20261112' or expected is None
            or (config.get('cid'), config.get('environment_uuid')) != expected
            or type(config.get('generation')) is not int or config['generation'] != 1
            or config.get('project') != '/home/agent/expansion-continuation-20261103/bridge-environments'
            or config.get('container_name') != 'ml-2b804374-'+config['env_id']
            or 'task_profile' in config
            or type(config.get('seconds')) is not int or not 1 <= config['seconds'] <= 300):
        raise RuntimeError('remote fixture profile is not bound to an admitted instance')
    return {'slice': 'unifier-bridge-tests.slice', 'memory': 384*MIB, 'memory_swap': 512*MIB}


def task_budget(config):
    """The 2026-11-12 approval applies to one named attempt, not defaults."""
    if 'fixture_profile' in config:
        fixture_resources(config)
        return {'aggregate': 256, 'guest': 128}
    if 'task_profile' not in config:
        return {'aggregate': 256, 'guest': 192}
    if (any(type(config.get(key)) is not type(value) or config[key] != value
            for key, value in SH10_TASK_BINDING.items())
            or type(config.get('seconds')) is not int or not 1 <= config['seconds'] <= 600):
        raise RuntimeError('task profile is not authorized for this fixed attempt')
    return {'aggregate': 384, 'guest': 256}


def put(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + '.tmp-' + str(os.getpid()))
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    os.replace(temporary, path)


def get(path):
    return json.loads(Path(path).read_text())


def call(argv, timeout=15):
    result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError('command failed: ' + ' '.join(argv[:3]) + ': ' + result.stderr[-500:])
    return result.stdout


def unit(name, *, timeout=5):
    result = subprocess.run(['systemctl', '--user', 'show', name, '-p', 'Id',
        '-p', 'MainPID', '-p', 'InvocationID', '-p', 'ControlGroup', '-p', 'ActiveState',
        '-p', 'MemoryMax', '-p', 'MemorySwapMax', '-p', 'TasksMax',
        '-p', 'CPUQuotaPerSecUSec'], capture_output=True, text=True, timeout=timeout)
    if result.returncode and 'not found' not in result.stderr:
        raise RuntimeError(result.stderr[-300:])
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def process(pid):
    raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    return {'pid': int(pid), 'starttime': raw[19],
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'cgroup': Path(f'/proc/{pid}/cgroup').read_text().strip().split('::', 1)[1]}


def under(child, parent):
    return child == parent or child.startswith(parent + '/')


def limits(group):
    return {key: (cgroup_dir(group) / key).read_text().strip() for key in
            ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')}


def cgroup_dir(group):
    if not isinstance(group, str) or not group.startswith('/') or '..' in Path(group).parts:
        raise RuntimeError('cgroup path is outside the admitted aggregate')
    path = CG / group.lstrip('/')
    if not path.is_relative_to(CG):
        raise RuntimeError('cgroup path is outside the admitted aggregate')
    return path


def controller_budget(config):
    """Opt in with an exact admitted profile. Omission cannot raise the budget."""
    if 'controller' not in config:
        return dict(DEFAULT_CONTROLLER)
    requested = config['controller']
    if type(requested) is not dict or set(requested) != set(DEFAULT_CONTROLLER):
        raise RuntimeError('controller budget is not an admitted profile')
    if any(type(requested[key]) is not int or requested[key] <= 0 for key in requested):
        raise RuntimeError('controller budget is not an admitted profile')
    for profile in ADMITTED_CONTROLLERS:
        if requested == profile:
            return dict(profile)
    raise RuntimeError('controller budget is not an admitted profile')


def controller_properties(budget):
    """Byte values, so systemd cannot reinterpret an M suffix."""
    return ['MemoryMax=' + str(budget['memory_max']),
            'MemoryHigh=' + str(budget['memory_high']),
            'MemorySwapMax=0', 'TasksMax=' + str(budget['tasks_max'])]


def read_controller_limits(group):
    path = cgroup_dir(group)
    found = {}
    for name, key in (('memory.max', 'memory_max'), ('memory.high', 'memory_high'),
                      ('memory.swap.max', 'memory_swap_max'), ('pids.max', 'tasks_max')):
        text = (path / name).read_text().strip()
        if not text.isdecimal():
            raise RuntimeError('controller limit is not a byte bound: ' + name)
        found[key] = int(text)
    return found


def assert_controller_limits(group, budget):
    actual = read_controller_limits(group)
    if (actual['memory_max'] != budget['memory_max']
            or actual['memory_high'] != budget['memory_high']
            or actual['memory_swap_max'] != 0
            or actual['tasks_max'] != budget['tasks_max']):
        raise RuntimeError('controller unit limits differ: ' + json.dumps(actual))
    return actual


def memory_classes(group):
    found = {}
    for line in (cgroup_dir(group) / 'memory.stat').read_text().splitlines():
        key, value = line.split()
        if key in ('anon', 'file'):
            if not value.isdecimal():
                raise RuntimeError('memory.stat value is invalid')
            found[key] = int(value)
    if set(found) != {'anon', 'file'}:
        raise RuntimeError('memory.stat is missing anon or file')
    return found


def subgroup_metrics(group, limit=16):
    """One level of child cgroups. A vanished child is skipped; a bad file is not."""
    children = sorted((path for path in cgroup_dir(group).iterdir() if path.is_dir()),
                      key=lambda path: path.name)
    if len(children) > limit:
        raise RuntimeError('subgroup sample exceeds budget')
    rows = []
    for child in children:
        relative = '/' + child.relative_to(CG).as_posix()
        try:
            classes = memory_classes(relative)
            memory = int((child / 'memory.current').read_text())
            tasks = int((child / 'pids.current').read_text())
        except FileNotFoundError:
            continue
        rows.append({'cgroup': relative, 'memory': memory, 'tasks': tasks, **classes})
    return rows


def effective_limits(group, ancestor):
    """Read the entire admitted cgroup chain; child 'max' does not erase a parent cap."""
    group, ancestor = Path(group), Path(ancestor)
    if (not group.is_absolute() or not ancestor.is_absolute()
            or '..' in group.parts or '..' in ancestor.parts
            or not group.is_relative_to(ancestor)):
        raise RuntimeError('cgroup chain is outside the admitted aggregate')
    chain = []
    effective = {key: None for key in ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')}
    while True:
        if len(chain) >= 16:
            raise RuntimeError('cgroup chain exceeds the audit budget')
        raw = limits(group.as_posix())
        chain.append({'cgroup': group.as_posix(), 'limits': raw})
        try:
            for key in ('memory.max', 'memory.swap.max', 'pids.max'):
                text = raw[key]
                if text == 'max':
                    continue
                if not isinstance(text, str) or not text.isdecimal():
                    raise ValueError('invalid cgroup bound')
                value = int(text)
                if effective[key] is None or value < effective[key]:
                    effective[key] = value
            quota, period = raw['cpu.max'].split()
            if not period.isdecimal() or int(period) <= 0:
                raise ValueError('invalid CPU period')
            if quota != 'max':
                if not quota.isdecimal() or int(quota) <= 0:
                    raise ValueError('invalid CPU quota')
                candidate = {'quota': int(quota), 'period': int(period)}
                prior = effective['cpu.max']
                if prior is None or candidate['quota']*prior['period'] < prior['quota']*candidate['period']:
                    effective['cpu.max'] = candidate
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError('cgroup limit metrics are incomplete or invalid') from exc
        if group == ancestor:
            break
        group = group.parent
    return {**effective, 'chain': chain}


def aggregate_processes(group):
    """Bounded cgroup walk, including conmon, pasta and detached APP workers."""
    values = []; visited = 0
    for directory, children, files in os.walk(CG/group.lstrip('/')):
        visited += 1
        if visited > 32:
            raise RuntimeError('unexpected aggregate cgroup expansion')
        if 'cgroup.procs' not in files:
            continue
        for text in (Path(directory)/'cgroup.procs').read_text().split():
            if len(values) >= 256:
                raise RuntimeError('aggregate process inventory exceeds budget')
            try:
                value = process(int(text))
                value['comm'] = Path('/proc')/text/'comm'
                value['comm'] = value['comm'].read_text().strip()
                if not under(value['cgroup'], group):
                    raise RuntimeError('process escaped during coverage audit')
                values.append(value)
            except FileNotFoundError:
                pass
    return values


def read_pidfile(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        value = os.fstat(descriptor)
        if not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid() or value.st_size > 32:
            raise RuntimeError('untrusted conmon pidfile')
        data = os.read(descriptor, 33).strip()
        if not data.isdigit() or int(data) <= 0:
            raise RuntimeError('invalid conmon pidfile')
        return int(data)
    finally:
        os.close(descriptor)


def conmon_process(value, config):
    pid = value.get('ConmonPid')
    if not pid:
        path = Path(value.get('ConmonPidFile') or '')
        if not path.is_absolute() or config['cid'] not in path.parts:
            raise RuntimeError('conmon pidfile is not bound to the full CID')
        pid = read_pidfile(path)
    identity = process(pid)
    argv = Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
    if config['cid'].encode() not in argv or 'conmon' not in Path(f'/proc/{pid}/comm').read_text():
        raise RuntimeError('conmon process does not match the fixed CID')
    return identity


def load_metrics(group):
    path = cgroup_dir(group)
    def fields(name):
        return {key: int(value) for key, value in
                (line.split() for line in (path / name).read_text().splitlines())}
    return {'memory': int((path / 'memory.current').read_text()),
            'tasks': int((path / 'pids.current').read_text()),
            'events': fields('memory.events'), 'pids_events': fields('pids.events'),
            'cpu': fields('cpu.stat'), **memory_classes(group), **cgroup_diagnostics(path)}


def host():
    values = {}
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            values['available'] = int(line.split()[1]) * 1024
    for line in Path('/proc/pressure/memory').read_text().splitlines():
        words = line.split()
        values[words[0]] = float(dict(part.split('=') for part in words[1:])['avg10'])
    if set(values) != {'available', 'some', 'full'}:
        raise RuntimeError('host metrics unavailable')
    return values


def configured_resources(value, config, *, profile=None, validate_oci=None):
    """Read the saved stopped-container spec; inspect can expose the last run's OCI file.

    Only the exact CID row is read, using SQLite read-only/query-only mode. No
    native database or OCI config is ever modified by this supervisor.
    """
    profile = profile or fixture_resources(config)
    directory = Path(value.get('StaticDir') or '')
    if (not directory.is_absolute() or '..' in directory.parts
            or directory.name != 'userdata' or directory.parent.name != config['cid']
            or directory.parent.parent.name != 'overlay-containers'):
        raise RuntimeError('saved container configuration path is unbound')
    database = directory.parents[2] / 'db.sql'
    st = database.lstat()
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_size > 128*MIB:
        raise RuntimeError('saved container database is untrusted or too large')
    connection = sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)
    try:
        connection.execute('PRAGMA query_only=ON')
        row = connection.execute(
            'SELECT JSON FROM ContainerConfig WHERE ID=? AND length(JSON)<=?',
            (config['cid'], 2*MIB)).fetchone()
        if row is None:
            raise RuntimeError('saved container configuration unavailable')
        saved = json.loads(row[0])
    finally:
        connection.close()
    if (saved.get('id') != config['cid'] or saved.get('name') != config['container_name']
            or saved.get('staticDir') != str(directory) or saved.get('cgroupParent') != profile['slice']):
        raise RuntimeError('saved container configuration identity differs')
    if validate_oci:
        validate_oci(saved['spec'])
    resources = saved['spec']['linux']['resources']
    if (resources.get('memory') != {'limit':profile['memory'], 'swap':profile['memory_swap']}
            or resources.get('cpu') != {'quota':100000, 'period':100000}
            or type(resources.get('pids', {}).get('limit')) is not int):
        raise RuntimeError('saved container resource limits differ')
    return resources


def info(config, *, hardware=None, timeout=None):
    tasks = hardware.tasks if hardware else task_budget(config)
    profile = hardware.resources if hardware else fixture_resources(config)
    argv = ['podman', 'inspect', '--format', 'json', config['cid']]
    value, = json.loads(call(argv) if timeout is None else call(argv, timeout=timeout))
    labels = value['Config'].get('Labels', {})
    if (value['Id'] != config['cid'] or labels.get('multilinux.project') != config['project']
            or labels.get('multilinux.env') != config['env_id']
            or labels.get('multilinux.role') != 'environment'):
        raise RuntimeError('fixed instance ownership changed')
    if hardware:
        hardware.validate_container(config, value)
    else:
        record = get(Path(config['project']) / '.lab/state/environments' / (config['env_id'] + '.json'))
        if (record.get('environment_uuid') != config['environment_uuid']
                or record.get('generation') != config['generation']
                or record['runtime']['container_id'] != config['cid']):
            raise RuntimeError('fixed environment generation changed')
    hc = value['HostConfig']
    guest_tasks = hc.get('PidsLimit')
    saved_resources = None
    if hardware:
        saved_resources = configured_resources(value, config, profile=profile,
                                               validate_oci=hardware.validate_oci)
    if value.get('State', {}).get('Running') is False:
        cached_limits = (128,) if hardware or 'fixture_profile' in config else (192, 256)
        if value['State'].get('Pid') != 0 or guest_tasks not in cached_limits:
            raise RuntimeError('stopped container state or cached task limit differs')
        if saved_resources is None:
            saved_resources = configured_resources(value, config)
        guest_tasks = saved_resources['pids']['limit']
    if (hc.get('CgroupParent') != profile['slice'] or hc.get('Memory') != profile['memory']
            or hc.get('MemorySwap') != profile['memory_swap'] or guest_tasks != tasks['guest']
            or hc.get('NanoCpus') != 1000000000 or hc.get('IpcMode') != 'private'
            or hc.get('PidMode') != 'private' or hc.get('Privileged')
            or (not hardware and hc.get('Devices'))
            or hc.get('RestartPolicy', {}).get('Name') != 'no'):
        raise RuntimeError('fixed container limits/isolation are not admitted')
    try:
        expected = approved_mounts(config)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    actual = [(mount.get('Source'), mount.get('Destination')) for mount in value['Mounts']]
    if sorted(actual) != sorted(expected):
        raise RuntimeError('unapproved mount')
    if any(key in {'DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XAUTHORITY'}
           for key in (item.split('=', 1)[0] for item in value['Config'].get('Env', []))):
        raise RuntimeError('host desktop environment present')
    return value


def register_live_binding(config, out, value, *, new_start=False, timeout=None):
    if not value['State']['Running']:
        raise RuntimeError('cannot bind a stopped execution')
    init = process(value['State']['Pid'])
    prior = out/'live-binding.json'
    if prior.exists() and not new_start and get(prior)['started_at'] != value['State']['StartedAt']:
        raise RuntimeError('unregistered container restart')
    scope_name = 'libpod-'+config['cid']+'.scope'
    scope = unit(scope_name) if timeout is None else unit(scope_name, timeout=timeout)
    binding = {'cid': config['cid'], 'started_at': value['State']['StartedAt'],
               'init': init, 'scope': scope, 'coverage_verified': False}
    put(out/'live-binding.json', binding)  # Register emergency stop ownership first.
    return binding


def capture_binding(config, out, *, new_start=False, hardware=None):
    value = info(config, hardware=hardware) if hardware else info(config)
    if not value['State']['Running']:
        raise RuntimeError('cannot bind a stopped execution')
    parent = get(out/'ready.json')['aggregate']
    binding = register_live_binding(config, out, value, new_start=new_start)
    init, scope = binding['init'], binding['scope']
    conmon = conmon_process(value, config)
    binding['conmon'] = conmon
    if not under(init['cgroup'], parent) or not under(conmon['cgroup'], parent):
        raise RuntimeError('container init/conmon escaped the aggregate budget')
    if not scope.get('ControlGroup') or not under(init['cgroup'], scope['ControlGroup']):
        raise RuntimeError('guest scope identity is unknown')
    actual = effective_limits(init['cgroup'], parent)
    binding['guest_limits'] = actual
    put(out/'live-binding.json', binding)
    cpu = actual['cpu.max']
    profile = hardware.resources if hardware else fixture_resources(config)
    tasks = hardware.tasks if hardware else task_budget(config)
    expected_memory = profile['memory']
    if hardware and config.get('guest_profile'):
        expected_memory = hardware.expected_live_memory(binding, new_start=new_start)
    if (actual['memory.max'] != expected_memory or actual['pids.max'] != tasks['guest']
            or actual['memory.swap.max'] != 128*MIB or cpu is None
            or cpu['quota'] > cpu['period']):
        raise RuntimeError('effective guest cgroup limits differ: '+json.dumps(actual))
    binding.update(guest_limits=actual, coverage_verified=True)
    binding['aggregate_processes'] = aggregate_processes(parent)
    network = value.get('NetworkSettings', {}).get('SandboxKey')
    network_helpers = []
    for index, entry in enumerate(Path('/proc').iterdir()):
        if index > 8192:
            raise RuntimeError('host process inventory exceeds coverage budget')
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry/'comm').read_text().strip()
            if not comm.startswith(('pasta', 'slirp4netns')):
                continue
            argv = (entry/'cmdline').read_bytes().split(b'\0')
            if network and network.encode() in argv:
                helper = process(int(entry.name))
                if not under(helper['cgroup'], parent):
                    raise RuntimeError('network helper escaped aggregate')
                network_helpers.append(helper)
        except (FileNotFoundError, PermissionError):
            pass
    binding['network_helpers'] = network_helpers
    binding['network_namespace_path'] = network
    if value['HostConfig'].get('NetworkMode') == 'pasta' and not (
            network_helpers or any(p['comm'].startswith('pasta') for p in binding['aggregate_processes'])):
        raise RuntimeError('pasta process coverage could not be established')
    if not hardware:
        apply_guest_memory_high(config, binding, out, new_start=new_start)
    put(out/'live-binding.json', binding)
    return binding


def apply_guest_memory_high(config, binding, out, *, new_start=False):
    """Optional tighter reclaim line inside an already verified test cgroup.

    No HostConfig, hard limit, ancestor or host setting changes. The guest
    cgroup vanishes at stop. Never raise an existing, tighter watermark.
    """
    if 'guest_memory_high' not in config:
        return
    requested = config['guest_memory_high']
    if type(requested) is not int or requested != 512*MIB:
        raise RuntimeError('guest reclaim watermark is not an admitted tighter profile')
    if not binding.get('coverage_verified') or binding['cid'] != config['cid']:
        raise RuntimeError('guest reclaim requires verified execution coverage')
    path = cgroup_dir(binding['init']['cgroup'])/'memory.high'
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        value = os.read(descriptor, 64).decode().strip()
        if value != 'max' and (not value.isdecimal() or int(value) < requested):
            raise RuntimeError('existing guest watermark is tighter or unreadable; refusing to raise it')
        current = info(config)
        if (not current['State']['Running'] or current['State']['StartedAt'] != binding['started_at']
                or not same_execution(binding['init'], process(current['State']['Pid']))):
            raise RuntimeError('guest execution changed before reclaim setup')
        if new_start:
            os.lseek(descriptor, 0, os.SEEK_SET)
            os.write(descriptor, str(requested).encode())
        elif value != str(requested):
            raise RuntimeError('guest reclaim watermark changed after admission')
    finally:
        os.close(descriptor)
    if path.read_text().strip() != str(requested):
        raise RuntimeError('guest reclaim watermark did not take effect')
    binding['guest_memory_high'] = requested
    if new_start:
        put(out/'guest-watermark.json', {'cid':config['cid'], 'init':binding['init'],
            'started_at':binding['started_at'], 'previous':value, 'memory_high':requested,
            'hard_limits_unchanged':True})


def verify_permission(config, out, *, enqueue=True):
    if (out/'abort.json').exists() or (enqueue and (out/'queue-closed').exists()):
        raise RuntimeError('test attempt is latched or queue closed')
    ready = get(out/'ready.json')
    if not same_execution(ready['monitor'], process(ready['monitor']['pid'])):
        raise RuntimeError('supervisor identity changed')
    if time.monotonic() - get(out/'monitor-heartbeat.json')['at'] > 2:
        raise RuntimeError('independent supervisor heartbeat lost')
    if not under(process(os.getpid())['cgroup'], ready['worker']['cgroup']):
        raise RuntimeError('product worker escaped its registered test cgroup')
    latch = Path(config['project']).parent/'.guard-abort.json'
    if latch.exists():
        raise RuntimeError('old fixture abort latch requires explicit reconciliation')


def make_service(spec):
    from unifier.runtime import PodmanRuntime, _run_podman
    from unifier.project import load_project
    from unifier.service import EnvironmentService
    config = get(spec); out = Path(spec).parent
    class FixedRuntime(PodmanRuntime):
        def __init__(self):
            super().__init__(runner=self.checked_run, popen=self.checked_popen)
        def checked_run(self, argv, **kwargs):
            verb = argv[1] if len(argv) > 1 else ''
            allowed = {'inspect', 'info', 'start', 'stop', 'exec', 'cp', 'unshare',
                       'mount', 'unmount', 'ps', 'events', '--version', 'version'}
            if verb not in allowed:
                raise RuntimeError('test runtime forbids creating/reconfiguring/removing resources')
            verify_permission(config, out, enqueue=verb != 'stop')
            if verb in {'start', 'stop', 'restart', 'exec', 'cp', 'mount', 'unmount'}:
                if not any(arg == config['cid'] or arg == config['container_name']
                           or arg.startswith(config['cid']+':') for arg in argv[2:]):
                    raise RuntimeError('runtime request is not pinned to the fixed instance')
            return _run_podman(argv, **kwargs)
        def checked_popen(self, argv, **kwargs):
            verify_permission(config, out)
            capture_binding(config, out)
            return subprocess.Popen(argv, **kwargs)
        def create(self, *_args, **_kwargs):
            raise RuntimeError('zero new containers')
        def remove(self, *_args, **_kwargs):
            raise RuntimeError('historical test containers are retained')
        def start(self, name, *, project, env_id):
            verify_permission(config, out)
            value = info(config)
            put(out/'start-intent.json', {'at': time.time(), 'before': value['State']})
            super().start(name, project=project, env_id=env_id)
            capture_binding(config, out, new_start=True)
        def exec(self, *args, **kwargs):
            capture_binding(config, out)
            return super().exec(*args, **kwargs)
    targets, _, _ = load_project()
    lock = get(config['image_lock'])
    return EnvironmentService(Path(config['project']), FixedRuntime(), targets, lock)


def cleanup_guest(config, out, *, hardware=None):
    result = {'status': 'NO_START'}
    binding_path = out/'live-binding.json'
    if hardware:
        config = hardware.cleanup_config()

    def inspect_target():
        if hardware is None:
            return info(config)
        return info(config, hardware=hardware, timeout=hardware.cleanup_timeout(5, guest=True))

    if binding_path.exists():
        expected = get(binding_path); current = inspect_target()
        if hardware is not None and expected.get('cid') != config['cid']:
            raise RuntimeError('CLEANUP_UNKNOWN: registered cleanup CID differs')
        if current['State']['Running']:
            if (current['State']['StartedAt'] != expected['started_at']
                    or not same_execution(expected['init'], process(current['State']['Pid']))):
                raise RuntimeError('CLEANUP_UNKNOWN: current CID execution differs')
            argv = ['podman', 'stop', '--time', '8', config['cid']]
            if hardware is None:
                call(argv, timeout=25)
            else:
                try:
                    call(argv, timeout=hardware.cleanup_timeout(12, guest=True))
                except (RuntimeError, subprocess.TimeoutExpired) as exc:
                    put(out/'podman-stop-error.json', {'cid': config['cid'], 'reason': str(exc)})
        try:
            alive = same_execution(expected['init'], process(expected['init']['pid']))
        except FileNotFoundError:
            alive = False
        if alive:
            # A dead conmon/Podman proxy is not proof that the guest init died.
            scope_name = expected['scope']['Id']
            scope = (unit(scope_name) if hardware is None else
                     unit(scope_name, timeout=hardware.cleanup_timeout(5, guest=True)))
            if (scope.get('InvocationID') != expected['scope'].get('InvocationID')
                    or scope.get('ControlGroup') != expected['scope'].get('ControlGroup')
                    or not under(expected['init']['cgroup'], scope['ControlGroup'])):
                raise RuntimeError('CLEANUP_UNKNOWN: exact guest scope changed')
            if hardware is not None:
                if (scope['Id'] != 'libpod-'+config['cid']+'.scope'
                        or not scope.get('InvocationID')):
                    raise RuntimeError('CLEANUP_UNKNOWN: exact guest scope identity missing')
                # Recheck after the systemd query and immediately before stop.
                try:
                    observed = process(expected['init']['pid'])
                except FileNotFoundError:
                    still_alive = False
                else:
                    still_alive = True
                    if not same_execution(expected['init'], observed):
                        raise RuntimeError('CLEANUP_UNKNOWN: verified guest PID changed before scope stop')
            if hardware is None or still_alive:
                timeout = 15 if hardware is None else hardware.cleanup_timeout(10, guest=True)
                call(['systemctl', '--user', 'stop', scope['Id']], timeout=timeout)
            try:
                if same_execution(expected['init'], process(expected['init']['pid'])):
                    raise RuntimeError('CLEANUP_UNKNOWN: original guest init remains alive')
            except FileNotFoundError:
                pass
        current = inspect_target()
        if current['State']['Running']:
            raise RuntimeError('CLEANUP_UNKNOWN: fixed instance remains running')
        result = {'status': 'STOPPED', 'cid': config['cid'], 'state': current['State']}
    elif (out/'start-intent.json').exists():
        current = inspect_target()
        if current['State']['Running']:
            raise RuntimeError('CLEANUP_UNKNOWN: start interrupted before execution binding')
    return result


def cleanup_worker(out, result, *, hardware=None):
    worker_path = out/'worker-unit.json'
    if worker_path.exists():
        expected = get(worker_path)

        def inspect_worker():
            return (unit(expected['Id']) if hardware is None else
                    unit(expected['Id'], timeout=hardware.cleanup_timeout(5)))

        current = inspect_worker()
        if current.get('ControlGroup'):
            if (current.get('InvocationID') != expected['InvocationID']
                    or current['ControlGroup'] != expected['ControlGroup']):
                raise RuntimeError('CLEANUP_UNKNOWN: controller unit changed')
            timeout = 15 if hardware is None else hardware.cleanup_timeout(12)
            call(['systemctl', '--user', 'stop', expected['Id']], timeout=timeout)
        result['worker_final'] = inspect_worker()
        if result['worker_final'].get('MainPID') not in ('0', None) or result['worker_final'].get('ControlGroup'):
            raise RuntimeError('CLEANUP_UNKNOWN: controller still active')


def cleanup(config, out, *, hardware=None):
    """Used by normal finally AND systemd ExecStopPost after monitor death."""
    if hardware is None:
        result = cleanup_guest(config, out)
        cleanup_worker(out, result)
    else:
        problems = []
        result = {'status': 'CLEANUP_UNKNOWN'}
        try:
            result = cleanup_guest(config, out, hardware=hardware)
        except Exception as exc:
            problems.append(str(exc))
        try:
            cleanup_worker(out, result, hardware=hardware)
        except Exception as exc:
            problems.append(str(exc))
        if problems:
            result = {**result, 'status': 'CLEANUP_UNKNOWN', 'problems': problems}
            put(out/'cleanup.json', result)
            raise RuntimeError('CLEANUP_UNKNOWN: '+' / '.join(problems))
    put(out/'cleanup.json', result)
    return result


def worker(config, out, *, hardware=None):
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with short_temporary_directory() as temporary:
        put(out/'temporary.json', {'path':str(temporary), 'cleanup':'worker-owned'})
        if hardware is not None:
            return run_worker(config, out, temporary, hardware=hardware)
        return run_worker(config, out, temporary)


def run_worker(config, out, temporary, *, hardware=None):
    (out/'command.log').touch()
    (out/'events.jsonl').touch()
    put(out/'worker-heartbeat.json', {'at':time.monotonic()})
    since = get(out/'event-since.json')['since']
    # Go event helpers belong to the existing aggregate, not the small monitor.
    event_log = (out/'events.jsonl').open('ab')
    event_error = (out/'events.stderr').open('ab')
    event_process = subprocess.Popen(['podman','events','--since',since,
        '--filter','type=container','--format','json'], stdout=event_log, stderr=event_error,
        env={**os.environ,'GOMAXPROCS':'1'})
    put(out/'event-reader.json', process(event_process.pid))
    put(out/'worker-ready.json', process(os.getpid()))
    end = time.monotonic() + config['seconds']
    while not (out/'go').exists():
        if time.monotonic() > end:
            raise RuntimeError('no independent admission')
        put(out/'worker-heartbeat.json', {'at':time.monotonic()})
        time.sleep(.05)
    private = Path(config['private_root']); private.mkdir(mode=0o700, exist_ok=True)
    # Podman needs its original host HOME/XDG runtime to find the fixed rootless
    # storage. Guest HOME/configuration is separately passed by the product.
    env = {key:value for key,value in os.environ.items() if key not in SESSION_VARIABLES}
    if hardware:
        env = hardware.worker_environment(env)
    locale = 'C.UTF-8' if hardware else 'C'
    env.update(PYTHONPATH=str(ROOT/'src')+':'+str(ROOT), UNIFIER_HOME=config['project'],
               TMPDIR=str(temporary), PYTHONDONTWRITEBYTECODE='1', GOMAXPROCS='1',
               LC_ALL=locale, LANG=locale, MULTILINUX_INTEGRATION='0', MULTILINUX_EVIDENCE='0',
               UNIFIER_TEST_SPEC=str(out/'spec.json'))
    with (out/'command.log').open('wb') as log:
        child = subprocess.Popen(config['command'], cwd=ROOT, env=env,
                                 stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        put(out/'command-identity.json', process(child.pid))
        while child.poll() is None:
            put(out/'worker-heartbeat.json', {'at': time.monotonic()})
            if (time.monotonic() > end
                    or time.monotonic()-get(out/'monitor-heartbeat.json')['at'] > 2):
                raise RuntimeError('monitor lost or controller deadline')
            time.sleep(.1)
        put(out/'command-result.json', {'exit_code': child.returncode, 'at': time.time()})
        # Keep conmon/network/event helpers budgeted until the supervisor has
        # stopped the guest and this exact controller unit. Never orphan them.
        while time.monotonic() < end:
            put(out/'worker-heartbeat.json', {'at':time.monotonic()})
            if time.monotonic()-get(out/'monitor-heartbeat.json')['at'] > 2:
                raise RuntimeError('monitor lost after command completion')
            time.sleep(.1)
        raise RuntimeError('controller cleanup deadline')


def guard(config, out, *, job_lock=None, hardware=None):
    reason = None; events = None; event_output = None; log = None
    minimum = None; maximum = tasks_peak = 0; cleanup_result = None
    owns_job_lock = job_lock is None
    budget = controller_budget(config)
    tasks = hardware.tasks if hardware else task_budget(config)
    profile = hardware.resources if hardware else fixture_resources(config)
    start = time.monotonic(); deadline = start+config['seconds']+65
    monitor = process(os.getpid()); worker_unit = config['unit']+'-work.service'
    if not hardware:
        before = info(config)
        if before['State']['Running']:
            raise RuntimeError('refuse to take over a running test instance')
        leases = get(Path(config['project'])/'.lab/leases'/ (config['env_id']+'.json'))
        if leases.get('leases') or leases.get('bridge'):
            raise RuntimeError('existing leases/bridge require prior recovery')
    aggregate = unit(profile['slice'])['ControlGroup']; actual = limits(aggregate)
    if (actual['memory.max'] != str(1024*MIB) or actual['memory.swap.max'] != str(128*MIB)
            or actual['pids.max'] != str(tasks['aggregate']) or actual['cpu.max'].split()[0] == 'max'
            or load_metrics(aggregate)['tasks'] != 0):
        raise RuntimeError('aggregate absent, busy or outside its budget')
    lockpath = hardware.lockpath if hardware else Path(config['project'])/'.lab/state/operations/expansion-test.lock'
    lock = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    initial = load_metrics(aggregate)
    try:
        if owns_job_lock:
            jobpath = ROOT/'.lab/agents/test-job.lock'
            jobpath.parent.mkdir(parents=True, exist_ok=True)
            job_lock = os.open(jobpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            fcntl.flock(job_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if (out/'abort.json').exists():
            raise RuntimeError('persistent attempt abort exists')
        monitor_max = hardware.monitor_budget['memory_max'] if hardware else 64*MIB
        if int(limits(monitor['cgroup'])['memory.max']) != monitor_max:
            raise RuntimeError('independent supervisor budget differs')
        if hardware:
            hardware.preflight(monitor, aggregate, actual)
        since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        put(out/'event-since.json', {'since': since})
        monitor_bytes = hardware.admission_monitor_bytes if hardware else 64*MIB
        threshold = config['reserve_bytes']+1024*MIB+monitor_bytes+128*MIB
        stable = None
        with (out/'admission.jsonl').open('w') as admission:
            while True:
                sample = host(); now = time.monotonic()
                admission.write(json.dumps({'at':now, **sample})+'\n'); admission.flush()
                if (sample['available'] < threshold or sample['full'] >= ADMISSION_FULL
                        or sample['some'] >= ADMISSION_SOME):
                    stable = None
                elif stable is None:
                    stable = now
                elif now-stable >= ADMISSION_SECONDS:
                    break
                if now-start > 60:
                    raise RuntimeError('admission failed')
                time.sleep(.25)
        put(out/'monitor-heartbeat.json', {'at':time.monotonic()})
        properties = ['Slice='+profile['slice'], *controller_properties(budget),
            'CPUQuota=100%', 'KillMode=control-group',
            'RuntimeMaxSec='+str(config['seconds']), 'TimeoutStopSec=10s', 'Restart=no',
            'BindsTo='+config['unit']+'-guard.service', 'After='+config['unit']+'-guard.service',
            'LimitCORE=0', 'UMask=0077']
        argv = ['systemd-run', '--user', '--quiet', '--unit='+worker_unit]
        if hardware:
            argv += ['--setenv=LC_ALL=C.UTF-8', '--setenv=LANG=C.UTF-8']
        for value in properties:
            argv += ['--property='+value]
        entry = hardware.entry if hardware else Path(__file__).resolve()
        call(argv+['/usr/bin/python3', str(entry), 'worker', str(out/'spec.json')])
        identity = unit(worker_unit); put(out/'worker-unit.json', identity)
        ready_deadline = time.monotonic()+5
        while not (out/'worker-ready.json').exists():
            if time.monotonic() > ready_deadline:
                raise RuntimeError('controller did not register')
            time.sleep(.05)
        owner = get(out/'worker-ready.json')
        event_owner = get(out/'event-reader.json')
        if owner['cgroup'] != identity['ControlGroup'] or not under(owner['cgroup'], aggregate):
            raise RuntimeError('controller escaped aggregate')
        if hardware and (event_owner['cgroup'] != owner['cgroup']
                         or not same_execution(event_owner, process(event_owner['pid']))):
            raise RuntimeError('independent event reader did not register inside the controller budget')
        observed = assert_controller_limits(owner['cgroup'], budget)
        if hardware:
            deadline = time.monotonic()+config['seconds']
            controller_initial = load_metrics(owner['cgroup'])
        put(out/'ready.json', {'monitor':monitor, 'worker':owner, 'aggregate':aggregate,
                               'aggregate_limits':actual, 'cid':config['cid'],
                               'controller_budget':budget, 'controller_limits':observed,
                               **({'phase_deadline':deadline} if hardware else {})})
        (out/'go').touch()
        policy = GuardPolicy(reserve=config['reserve_bytes'],
                             task_limit=tasks['aggregate'], deadline=deadline)
        controller_policy = GuardPolicy(reserve=config['reserve_bytes'],
            memory_limit=budget['memory_max'], task_limit=budget['tasks_max'], deadline=deadline)
        if not hardware:
            controller_initial = load_metrics(owner['cgroup'])
        offset = 0; pending = b''; log = (out/'samples.jsonl').open('w')
        while True:
            now = time.monotonic(); h = host(); load = load_metrics(aggregate)
            put(out/'monitor-heartbeat.json', {'at':now})
            minimum = h['available'] if minimum is None else min(minimum, h['available'])
            maximum = max(maximum, load['memory']); tasks_peak = max(tasks_peak, load['tasks'])
            sample = {**h, 'memory':load['memory'], 'tasks':load['tasks'],
                'oom_events':load['events'].get('oom_kill',0)-initial['events'].get('oom_kill',0)
                    +load['events'].get('oom',0)-initial['events'].get('oom',0),
                'max_events':load['events'].get('max',0)-initial['events'].get('max',0),
                'pids_events':load['pids_events'].get('max',0)-initial['pids_events'].get('max',0)}
            controller_load = load_metrics(owner['cgroup'])
            controller_sample = {**h, 'memory':controller_load['memory'],
                'tasks':controller_load['tasks'],
                'oom_events':sum(controller_load['events'].get(k,0)
                    -controller_initial['events'].get(k,0) for k in ('oom','oom_kill')),
                'max_events':controller_load['events'].get('max',0)
                    -controller_initial['events'].get('max',0),
                'pids_events':controller_load['pids_events'].get('max',0)
                    -controller_initial['pids_events'].get('max',0),
                'high_events':controller_load['events'].get('high',0),
                'anon':controller_load['anon'], 'file':controller_load['file']}
            with (out/'events.jsonl').open('rb') as source:
                source.seek(offset); chunk = source.read(65536); offset += len(chunk)
            pending += chunk
            while b'\n' in pending:
                line, pending = pending.split(b'\n',1)
                event = json.loads(line)
                cid = event.get('ID') or event.get('id') or event.get('Actor',{}).get('ID')
                status = event.get('Status') or event.get('status') or event.get('Action')
                if not cid or not status:
                    raise RuntimeError('unrecognized native container event')
                if hardware:
                    hardware.event(event, cid, status)
                elif cid != config['cid'] and status in {'start','restart','exec','exec_create','exec_start','create'}:
                    raise RuntimeError('non-target container action: '+cid+':'+status)
            if len(pending) > 65536 or offset > 20*MIB or (out/'command.log').stat().st_size > 20*MIB:
                raise RuntimeError('event/command log budget exceeded')
            if limits(aggregate) != actual:
                raise RuntimeError('aggregate hard budget changed')
            if not same_execution(event_owner, process(event_owner['pid'])):
                raise RuntimeError('event supervision lost')
            completed = (out/'command-result.json').exists()
            if hardware:
                hardware.sample(now)
            alive = True
            try:
                alive = same_execution(owner, process(owner['pid']))
            except FileNotFoundError:
                alive = False
            heartbeat_age = 0 if completed else now-get(out/'worker-heartbeat.json')['at']
            reason = policy.check(sample, now=now, controller_alive=alive or completed,
                                  heartbeat_age=heartbeat_age)
            controller_reason = controller_policy.check(controller_sample, now=now,
                controller_alive=alive or completed, heartbeat_age=heartbeat_age)
            reason = reason or (('CONTROLLER_'+controller_reason) if controller_reason else None)
            log.write(json.dumps({'at':now, 'host':h, 'load':load, 'sample':sample,
                'controller':controller_load,'controller_sample':controller_sample,
                'subgroups':subgroup_metrics(aggregate)})+'\n'); log.flush()
            if reason:
                raise RuntimeError(reason)
            if not policy.may_enqueue(sample) or not controller_policy.may_enqueue(controller_sample):
                (out/'queue-closed').touch()
            if completed and (not hardware or hardware.finished(pending)):
                break
            time.sleep(max(0, .25-(time.monotonic()-now)) if hardware else .25)
    except Exception as exc:
        reason = str(exc)
        put(out/'abort.json', {'reason':reason, 'at':time.time()})
    finally:
        if events is not None and events.poll() is None:
            events.terminate()
            try:
                events.wait(timeout=2)
            except subprocess.TimeoutExpired:
                events.kill(); events.wait(timeout=2)
        if event_output is not None:
            event_output.close()
        if log is not None:
            log.close()
        try:
            cleanup_result = (hardware.cleanup() if hardware else cleanup(config, out))
        except Exception as exc:
            reason = (reason or '')+' '+str(exc)
            if hardware:
                put(out/'abort.json', {'reason':reason, 'at':time.time()})
        os.close(lock)
        if owns_job_lock and job_lock is not None:
            os.close(job_lock)
        put(out/'receipt.json', {'reason':reason, 'monitor':monitor,
            'controller_budget':budget,
            'task_budget':tasks,
            'min_available_bytes':minimum, 'peak_memory_bytes':maximum,
            'peak_tasks':tasks_peak, 'cleanup':cleanup_result,
            'command_result':get(out/'command-result.json') if (out/'command-result.json').exists() else None})
    return result_exit_code(reason, get(out/'command-result.json')
                            if (out/'command-result.json').exists() else None)


def launch(config, out, *, hardware=None):
    """Prepare the idle dedicated aggregate under the same job lock as guard."""
    tasks = hardware.tasks if hardware else task_budget(config)
    aggregate_slice = (hardware.resources if hardware else fixture_resources(config))['slice']
    controller_budget(config)
    if any((out/name).exists() for name in ('abort.json', 'receipt.json', 'go')):
        raise RuntimeError('attempt already exists; never retry or overwrite it')
    lockpath = ROOT/'.lab/agents/test-job.lock'
    descriptor = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    prepared = False
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not hardware:
            value = info(config)
            leases = get(Path(config['project'])/'.lab/leases'/(config['env_id']+'.json'))
            if value['State']['Running'] or leases.get('leases') or leases.get('bridge'):
                raise RuntimeError('fixed instance is running or needs formal lease recovery')
        before = unit(aggregate_slice)
        if before.get('ControlGroup') and load_metrics(before['ControlGroup'])['tasks']:
            raise RuntimeError('dedicated test aggregate is busy')
        put(out/'test-slice-before.json', before)
        call(['systemctl', '--user', 'set-property', '--runtime', aggregate_slice,
              'MemoryMax=1073741824', 'MemorySwapMax=134217728',
              'TasksMax='+str(tasks['aggregate']), 'CPUQuota=100%'])
        prepared = True
        call(['systemctl', '--user', 'start', aggregate_slice])
        put(out/'test-slice-ready.json', unit(aggregate_slice))
        if hardware is not None:
            return guard(config, out, job_lock=descriptor, hardware=hardware)
        return guard(config, out, job_lock=descriptor)
    finally:
        try:
            if prepared:
                observed = unit(aggregate_slice)
                if observed.get('ControlGroup') and not load_metrics(observed['ControlGroup'])['tasks']:
                    call(['systemctl', '--user', 'stop', aggregate_slice])
        finally:
            os.close(descriptor)


def main():
    mode, filename = sys.argv[1:]
    path = Path(filename).resolve(); config = get(path); out = path.parent
    if mode == 'worker':
        return worker(config, out)
    if mode == 'launch':
        try:
            return launch(config, out)
        except Exception as exc:
            if not (out/'receipt.json').exists() and not (out/'abort.json').exists():
                value = {'reason':str(exc), 'at':time.time(), 'phase':'launch'}
                put(out/'abort.json', value)
                put(out/'receipt.json', value)
            return 2
    if mode == 'guard':
        try:
            return guard(config, out)
        except Exception as exc:
            value = {'reason':str(exc), 'at':time.time(), 'phase':'before-workload'}
            put(out/'abort.json', value)
            put(out/'receipt.json', value)
            return 2
    if mode == 'rescue':
        try:
            result = cleanup(config, out)
            put(out/'rescue.json', result)
            return 0
        except Exception as exc:
            put(out/'rescue.json', {'status':'CLEANUP_UNKNOWN', 'error':str(exc)})
            return 2
    raise ValueError('unknown mode')


if __name__ == '__main__':
    raise SystemExit(main())
