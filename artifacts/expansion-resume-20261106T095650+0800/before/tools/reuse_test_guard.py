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
import stat
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from tools.reuse_guard_policy import GuardPolicy, same_execution

MIB = 1024**2
CG = Path('/sys/fs/cgroup')
SLICE = 'unifier-tests.slice'


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


def unit(name):
    result = subprocess.run(['systemctl', '--user', 'show', name, '-p', 'Id',
        '-p', 'MainPID', '-p', 'InvocationID', '-p', 'ControlGroup', '-p', 'ActiveState',
        '-p', 'MemoryMax', '-p', 'MemorySwapMax', '-p', 'TasksMax',
        '-p', 'CPUQuotaPerSecUSec'], capture_output=True, text=True, timeout=5)
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
    return {key: (CG / group.lstrip('/') / key).read_text().strip() for key in
            ('memory.max', 'memory.swap.max', 'pids.max', 'cpu.max')}


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
    path = CG / group.lstrip('/')
    def fields(name):
        return {key: int(value) for key, value in
                (line.split() for line in (path / name).read_text().splitlines())}
    return {'memory': int((path / 'memory.current').read_text()),
            'tasks': int((path / 'pids.current').read_text()),
            'events': fields('memory.events'), 'pids_events': fields('pids.events'),
            'cpu': fields('cpu.stat')}


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


def info(config):
    value, = json.loads(call(['podman', 'inspect', '--format', 'json', config['cid']]))
    labels = value['Config'].get('Labels', {})
    if (value['Id'] != config['cid'] or labels.get('multilinux.project') != config['project']
            or labels.get('multilinux.env') != config['env_id']
            or labels.get('multilinux.role') != 'environment'):
        raise RuntimeError('fixed instance ownership changed')
    record = get(Path(config['project']) / '.lab/state/environments' / (config['env_id'] + '.json'))
    if (record.get('environment_uuid') != config['environment_uuid']
            or record.get('generation') != config['generation']
            or record['runtime']['container_id'] != config['cid']):
        raise RuntimeError('fixed environment generation changed')
    hc = value['HostConfig']
    if (hc.get('CgroupParent') != SLICE or hc.get('Memory') != 768*MIB
            or hc.get('MemorySwap') != 896*MIB or hc.get('PidsLimit') != 192
            or hc.get('NanoCpus') != 1000000000 or hc.get('IpcMode') != 'private'
            or hc.get('PidMode') != 'private' or hc.get('Privileged')
            or hc.get('Devices') or hc.get('RestartPolicy', {}).get('Name') != 'no'):
        raise RuntimeError('fixed container limits/isolation are not admitted')
    expected = config['shared']
    if any(m['Source'] != expected or m['Destination'] != expected for m in value['Mounts']):
        raise RuntimeError('unapproved mount')
    if any(key in {'DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XAUTHORITY'}
           for key in (item.split('=', 1)[0] for item in value['Config'].get('Env', []))):
        raise RuntimeError('host desktop environment present')
    return value


def capture_binding(config, out, *, new_start=False):
    value = info(config)
    if not value['State']['Running']:
        raise RuntimeError('cannot bind a stopped execution')
    parent = get(out/'ready.json')['aggregate']
    init = process(value['State']['Pid'])
    prior = out/'live-binding.json'
    if prior.exists() and not new_start and get(prior)['started_at'] != value['State']['StartedAt']:
        raise RuntimeError('unregistered container restart')
    scope_name = 'libpod-'+config['cid']+'.scope'
    scope = unit(scope_name)
    binding = {'cid': config['cid'], 'started_at': value['State']['StartedAt'],
               'init': init, 'scope': scope, 'coverage_verified': False}
    put(out/'live-binding.json', binding)  # Register emergency stop ownership first.
    conmon = conmon_process(value, config)
    binding['conmon'] = conmon
    if not under(init['cgroup'], parent) or not under(conmon['cgroup'], parent):
        raise RuntimeError('container init/conmon escaped the aggregate budget')
    if not scope.get('ControlGroup') or not under(init['cgroup'], scope['ControlGroup']):
        raise RuntimeError('guest scope identity is unknown')
    actual = limits(scope['ControlGroup'])
    if (actual['memory.max'] != str(768*MIB) or actual['pids.max'] != '192'
            or actual['memory.swap.max'] != str(128*MIB)
            or actual['cpu.max'].split()[0] == 'max'):
        raise RuntimeError('actual guest cgroup limits differ')
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
    put(out/'live-binding.json', binding)
    return binding


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


def cleanup(config, out):
    """Used by normal finally AND systemd ExecStopPost after monitor death."""
    result = {'status': 'NO_START'}
    binding_path = out/'live-binding.json'
    if binding_path.exists():
        expected = get(binding_path); current = info(config)
        if current['State']['Running']:
            if (current['State']['StartedAt'] != expected['started_at']
                    or not same_execution(expected['init'], process(current['State']['Pid']))):
                raise RuntimeError('CLEANUP_UNKNOWN: current CID execution differs')
            call(['podman', 'stop', '--time', '8', config['cid']], timeout=25)
        try:
            alive = same_execution(expected['init'], process(expected['init']['pid']))
        except FileNotFoundError:
            alive = False
        if alive:
            # A dead conmon/Podman proxy is not proof that the guest init died.
            scope = unit(expected['scope']['Id'])
            if (scope.get('InvocationID') != expected['scope'].get('InvocationID')
                    or scope.get('ControlGroup') != expected['scope'].get('ControlGroup')
                    or not under(expected['init']['cgroup'], scope['ControlGroup'])):
                raise RuntimeError('CLEANUP_UNKNOWN: exact guest scope changed')
            call(['systemctl', '--user', 'stop', scope['Id']], timeout=15)
            try:
                if same_execution(expected['init'], process(expected['init']['pid'])):
                    raise RuntimeError('CLEANUP_UNKNOWN: original guest init remains alive')
            except FileNotFoundError:
                pass
        current = info(config)
        if current['State']['Running']:
            raise RuntimeError('CLEANUP_UNKNOWN: fixed instance remains running')
        result = {'status': 'STOPPED', 'cid': config['cid'], 'state': current['State']}
    elif (out/'start-intent.json').exists():
        current = info(config)
        if current['State']['Running']:
            raise RuntimeError('CLEANUP_UNKNOWN: start interrupted before execution binding')
    worker_path = out/'worker-unit.json'
    if worker_path.exists():
        expected = get(worker_path); current = unit(expected['Id'])
        if current.get('ControlGroup'):
            if (current.get('InvocationID') != expected['InvocationID']
                    or current['ControlGroup'] != expected['ControlGroup']):
                raise RuntimeError('CLEANUP_UNKNOWN: controller unit changed')
            call(['systemctl', '--user', 'stop', expected['Id']], timeout=15)
        result['worker_final'] = unit(expected['Id'])
        if result['worker_final'].get('MainPID') not in ('0', None) or result['worker_final'].get('ControlGroup'):
            raise RuntimeError('CLEANUP_UNKNOWN: controller still active')
    put(out/'cleanup.json', result)
    return result


def worker(config, out):
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
    env = dict(os.environ)
    for key in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS', 'XAUTHORITY',
                'SESSION_MANAGER', 'SSH_AUTH_SOCK'):
        env.pop(key, None)
    env.update(PYTHONPATH=str(ROOT/'src')+':'+str(ROOT), UNIFIER_HOME=config['project'],
               TMPDIR=str(private), PYTHONDONTWRITEBYTECODE='1', GOMAXPROCS='1',
               LC_ALL='C', LANG='C', MULTILINUX_INTEGRATION='0', MULTILINUX_EVIDENCE='0',
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


def guard(config, out):
    reason = None; events = None; event_output = None; log = None
    minimum = None; maximum = tasks_peak = 0; cleanup_result = None
    start = time.monotonic(); deadline = start+config['seconds']+65
    monitor = process(os.getpid()); worker_unit = config['unit']+'-work.service'
    before = info(config)
    if before['State']['Running']:
        raise RuntimeError('refuse to take over a running test instance')
    leases = get(Path(config['project'])/'.lab/leases'/ (config['env_id']+'.json'))
    if leases.get('leases') or leases.get('bridge'):
        raise RuntimeError('existing leases/bridge require prior recovery')
    aggregate = unit(SLICE)['ControlGroup']; actual = limits(aggregate)
    if (actual['memory.max'] != str(1024*MIB) or actual['memory.swap.max'] != str(128*MIB)
            or actual['pids.max'] != '256' or actual['cpu.max'].split()[0] == 'max'
            or load_metrics(aggregate)['tasks'] != 0):
        raise RuntimeError('aggregate absent, busy or outside its budget')
    lockpath = Path(config['project'])/'.lab/state/operations/expansion-test.lock'
    lock = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    initial = load_metrics(aggregate)
    try:
        if (out/'abort.json').exists():
            raise RuntimeError('persistent attempt abort exists')
        if int(limits(monitor['cgroup'])['memory.max']) != 64*MIB:
            raise RuntimeError('independent supervisor budget differs')
        since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        put(out/'event-since.json', {'since': since})
        threshold = config['reserve_bytes']+1024*MIB+64*MIB+128*MIB
        stable = None
        with (out/'admission.jsonl').open('w') as admission:
            while True:
                sample = host(); now = time.monotonic()
                admission.write(json.dumps({'at':now, **sample})+'\n'); admission.flush()
                if sample['available'] < threshold or sample['full'] >= .5 or sample['some'] >= 2:
                    stable = None
                elif stable is None:
                    stable = now
                elif now-stable >= 30:
                    break
                if now-start > 60:
                    raise RuntimeError('admission failed')
                time.sleep(.25)
        put(out/'monitor-heartbeat.json', {'at':time.monotonic()})
        properties = ['Slice='+SLICE, 'MemoryMax=192M', 'MemoryHigh=144M',
            'MemorySwapMax=0', 'TasksMax=64', 'CPUQuota=100%', 'KillMode=control-group',
            'RuntimeMaxSec='+str(config['seconds']), 'TimeoutStopSec=10s', 'Restart=no',
            'BindsTo='+config['unit']+'-guard.service', 'After='+config['unit']+'-guard.service',
            'LimitCORE=0', 'UMask=0077']
        argv = ['systemd-run', '--user', '--quiet', '--unit='+worker_unit]
        for value in properties:
            argv += ['--property='+value]
        call(argv+['/usr/bin/python3', str(Path(__file__).resolve()), 'worker', str(out/'spec.json')])
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
        put(out/'ready.json', {'monitor':monitor, 'worker':owner, 'aggregate':aggregate,
                               'aggregate_limits':actual, 'cid':config['cid']})
        (out/'go').touch()
        policy = GuardPolicy(reserve=config['reserve_bytes'], deadline=deadline)
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
                if cid != config['cid'] and status in {'start','restart','exec','exec_create','exec_start','create'}:
                    raise RuntimeError('non-target container action: '+cid+':'+status)
            if len(pending) > 65536 or offset > 20*MIB or (out/'command.log').stat().st_size > 20*MIB:
                raise RuntimeError('event/command log budget exceeded')
            if limits(aggregate) != actual:
                raise RuntimeError('aggregate hard budget changed')
            if not same_execution(event_owner, process(event_owner['pid'])):
                raise RuntimeError('event supervision lost')
            completed = (out/'command-result.json').exists()
            alive = True
            try:
                alive = same_execution(owner, process(owner['pid']))
            except FileNotFoundError:
                alive = False
            heartbeat_age = 0 if completed else now-get(out/'worker-heartbeat.json')['at']
            reason = policy.check(sample, now=now, controller_alive=alive or completed,
                                  heartbeat_age=heartbeat_age)
            log.write(json.dumps({'at':now, 'host':h, 'load':load, 'sample':sample})+'\n'); log.flush()
            if reason:
                raise RuntimeError(reason)
            if not policy.may_enqueue(sample):
                (out/'queue-closed').touch()
            if completed:
                break
            time.sleep(.25)
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
            cleanup_result = cleanup(config, out)
        except Exception as exc:
            reason = (reason or '')+' '+str(exc)
        os.close(lock)
        put(out/'receipt.json', {'reason':reason, 'monitor':monitor,
            'min_available_bytes':minimum, 'peak_memory_bytes':maximum,
            'peak_tasks':tasks_peak, 'cleanup':cleanup_result,
            'command_result':get(out/'command-result.json') if (out/'command-result.json').exists() else None})
    return 2 if reason else 0


def main():
    mode, filename = sys.argv[1:]
    path = Path(filename).resolve(); config = get(path); out = path.parent
    if mode == 'worker':
        return worker(config, out)
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
