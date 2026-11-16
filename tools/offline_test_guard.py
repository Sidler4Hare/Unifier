"""Run one T1 command with bounded budgets, private paths and durable receipts.

This is a test supervisor, not a sandbox or permission to run T2/T3 workloads.
"""
import argparse
import errno
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.reuse_guard_policy import GuardPolicy, ADMISSION_FULL, ADMISSION_SOME, ADMISSION_SECONDS
from tools.test_runtime import cgroup_diagnostics, offline_environment, result_exit_code, short_temporary_directory

SPEC = C = OUT = None
CG = Path('/sys/fs/cgroup')
MIB = 1024**2
DEFAULT_MEMORY_MIB = 512
MAX_OFFLINE_MEMORY_MIB = 1024
HOST_FUSE_COMMAND = [
    '/usr/bin/unshare', '--user', '--map-root-user', '--mount',
    '--propagation', 'private', '--', '/usr/bin/python3',
]


def write(name, value):
    path = OUT / name
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(path)


def call(argv):
    return subprocess.check_output(argv, timeout=12, text=True).strip()


def fields(unit):
    result = subprocess.run(['systemctl', '--user', 'show', unit, '-p', 'InvocationID',
                '-p', 'ControlGroup', '-p', 'ActiveState', '-p', 'MainPID',
                '-p', 'ExecMainCode', '-p', 'ExecMainStatus', '-p', 'Result', '-p', 'LoadState'],
                capture_output=True, text=True, timeout=12)
    if result.returncode and 'not found' not in result.stderr:
        raise RuntimeError(result.stderr[:300])
    return dict(line.split('=', 1) for line in result.stdout.splitlines() if '=' in line)


def proc(pid):
    raw = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    group = Path(f'/proc/{pid}/cgroup').read_text().strip().split('::', 1)[1]
    return {'pid': pid, 'starttime': raw[19], 'cgroup': group,
            'boot_id': Path('/proc/sys/kernel/random/boot_id').read_text().strip()}


def limits(group):
    root = CG / group.lstrip('/')
    return {name: (root / name).read_text().strip() for name in
            ('memory.max', 'memory.high', 'memory.swap.max', 'pids.max', 'cpu.max')}


def metrics(group):
    root = CG / group.lstrip('/')
    def events(name):
        return dict((k, int(v)) for k, v in
                    (line.split() for line in (root / name).read_text().splitlines()))
    processes = []
    for pid in (root / 'cgroup.procs').read_text().split():
        try:
            processes.append(proc(int(pid)))
        except (FileNotFoundError, ProcessLookupError):
            pass  # An exited task can disappear between the two procfs reads.
    return {'memory': int((root / 'memory.current').read_text()),
            'tasks': int((root / 'pids.current').read_text()),
            'memory_peak': int((root / 'memory.peak').read_text()),
            'tasks_peak': int((root / 'pids.peak').read_text()),
            'memory_events': events('memory.events'),
            'pids_events': events('pids.events'), 'cpu_stat': events('cpu.stat'),
            'processes': processes, **cgroup_diagnostics(root)}


def host():
    memory = {k: int(v.split()[0]) * 1024 for k, v in
              (line.split(':', 1) for line in Path('/proc/meminfo').read_text().splitlines())}
    pressure = {}
    for line in Path('/proc/pressure/memory').read_text().splitlines():
        words = line.split()
        pressure[words[0]] = dict((k, float(v)) for k, v in
                                  (part.split('=') for part in words[1:]))
    return {'at': time.time(), 'available': memory['MemAvailable'],
            'swap_free': memory['SwapFree'], 'psi': pressure}


def worker():
    # A signal must unwind our private temporary directory too.
    def interrupted(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    with short_temporary_directory() as temporary:
        path = Path(temporary)
        st = path.stat()
        write('temporary.json', {'path': str(path), 'device': st.st_dev, 'inode': st.st_ino})
        return run_worker(path)


def run_worker(temporary):
    identity = proc(os.getpid())
    write('worker-ready.json', {'identity': identity, 'limits': limits(identity['cgroup'])})
    deadline = time.monotonic() + C['seconds']
    # systemd BindsTo handles supervisor exit; heartbeat handles a live but stuck supervisor.
    while not (OUT / 'go').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('no verified launch permission')
        time.sleep(.05)
    private = Path(C.get('private_root', str(OUT / 'private')))
    env = offline_environment(Path(C['root']), private, temporary, C.get('environment'))
    with (OUT / 'command.log').open('wb') as log:
        stdin = open(C['stdin'], 'rb') if C.get('stdin') else subprocess.DEVNULL
        child = subprocess.Popen(C['command'], cwd=C['root'], env=env,
                                 stdin=stdin, stdout=log, stderr=subprocess.STDOUT)
        try:
            identity_record = proc(child.pid)
        except FileNotFoundError:
            if child.poll() is None:
                raise
            identity_record = None  # A fast command may exit before registration.
        write('command-identity.json', {'identity': identity_record, 'argv': C['command']})
        while child.poll() is None:
            heartbeat = json.loads((OUT / 'heartbeat.json').read_text())['monotonic']
            if time.monotonic() - heartbeat > 2:
                raise RuntimeError('independent supervisor heartbeat stale')
            if time.monotonic() > deadline:
                raise RuntimeError('command deadline')
            time.sleep(.1)
        write('worker-final-cgroup.json', metrics(identity['cgroup']))
        write('command-result.json', {'exit_code': child.returncode, 'finished': time.time()})
        return child.returncode


def guardian():
    reason = None
    owned = None
    minimum = 2**63
    peak_memory = peak_tasks = 0
    job_lock = None
    start = time.monotonic()
    unit = C['unit'] + '-work.service'
    ownunit = C['unit'] + '-guard.service'
    memory_mib = C['memory_mib']
    memory_high_mib = memory_mib * 7 // 8
    scope = ('T2 host FUSE only; private namespace, no containers or GUI'
             if C.get('scope') == 'host-fuse'
             else 'T1 offline only; containers and GUI not admitted')
    receipt = {'reserve_bytes': C['reserve_bytes'], 'command': C['command'],
               'scope': scope,
               'supervisor': proc(os.getpid())}
    receipt['supervisor_limits'] = limits(receipt['supervisor']['cgroup'])
    try:
        if (OUT / 'abort.json').exists():
            raise RuntimeError('persistent abort record exists')
        lockpath = ROOT / '.lab/agents/test-job.lock'
        lockpath.parent.mkdir(parents=True, exist_ok=True)
        job_lock = os.open(lockpath, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(job_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if shutil.disk_usage(ROOT).free < max(5 * 1024**3, shutil.disk_usage(ROOT).total // 10):
            raise RuntimeError('BLOCKED_DISK')
        supervisor_limits = receipt['supervisor_limits']
        quota, period = supervisor_limits['cpu.max'].split()
        if (supervisor_limits['memory.max'] != str(64*MIB)
                or supervisor_limits['memory.high'] != str(48*MIB)
                or supervisor_limits['memory.swap.max'] != '0' or supervisor_limits['pids.max'] != '16'
                or quota == 'max' or not 0 < int(quota) <= int(period)):
            raise RuntimeError('supervisor hard budget mismatch')
        # Fresh admission, including the independent monitor and margin.
        threshold = C['reserve_bytes'] + (memory_mib + 64 + 128) * MIB
        with (OUT / 'admission.jsonl').open('w') as log:
            stable_since = None
            admission_deadline = time.monotonic() + 60
            while True:
                sample = host()
                log.write(json.dumps(sample) + '\n')
                log.flush()
                if (sample['available'] < threshold or sample['psi']['full']['avg10'] >= ADMISSION_FULL
                        or sample['psi']['some']['avg10'] >= ADMISSION_SOME):
                    stable_since = None
                elif stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= ADMISSION_SECONDS:
                    break
                if time.monotonic() >= admission_deadline:
                    raise RuntimeError(f'no continuous {ADMISSION_SECONDS}s admission window within 60s')
                time.sleep(.25)
        write('heartbeat.json', {'monotonic': time.monotonic()})
        properties = ['MemoryMax=' + str(memory_mib) + 'M',
                      'MemoryHigh=' + str(memory_high_mib) + 'M',
                      'MemorySwapMax=0', 'TasksMax=96', 'CPUQuota=100%',
                      'RuntimeMaxSec=' + str(C['seconds']), 'TimeoutStopSec=10s',
                      'KillMode=control-group', 'Restart=no', 'LimitCORE=0',
                      'UMask=0077', 'BindsTo=' + ownunit, 'After=' + ownunit]
        args = ['systemd-run', '--user', '--quiet', '--unit=' + unit,
                '--working-directory=' + C['root']]
        for value in properties:
            args += ['--property=' + value]
        call(args + ['/usr/bin/python3', str(Path(__file__).resolve()), 'worker', str(SPEC)])
        owned = fields(unit)
        receipt['unit_identity'] = owned
        ready_deadline = time.monotonic() + 5
        while not (OUT / 'worker-ready.json').exists():
            write('heartbeat.json', {'monotonic': time.monotonic()})
            if time.monotonic() > ready_deadline:
                raise RuntimeError('worker did not register')
            time.sleep(.1)
        ready = json.loads((OUT / 'worker-ready.json').read_text())
        group = ready['identity']['cgroup']
        actual = ready['limits']
        quota, period = actual['cpu.max'].split()
        if (owned['ControlGroup'] != group or int(actual['memory.max']) != memory_mib * MIB
                or actual['memory.high'] != str(memory_high_mib * MIB)
                or actual['memory.swap.max'] != '0' or actual['pids.max'] != '96'
                or quota == 'max' or not 0 < int(quota) <= int(period)):
            raise RuntimeError('actual worker cgroup budget mismatch')
        receipt['worker'] = ready
        (OUT / 'go').touch()
        policy = GuardPolicy(reserve=C['reserve_bytes'], memory_limit=memory_mib * MIB,
                             task_limit=96, deadline=start+C['seconds']+65)
        with (OUT / 'samples.jsonl').open('w') as log:
            while True:
                sample = host()
                minimum = min(minimum, sample['available'])
                write('heartbeat.json', {'monotonic': time.monotonic()})
                try:
                    load = metrics(group)
                except OSError as exc:
                    # An empty cgroup may disappear between metric reads.
                    # Accept teardown only with both durable worker receipts;
                    # final event counters are still checked below.
                    if (exc.errno in {errno.ENOENT, errno.ENODEV}
                            and (OUT / 'command-result.json').exists()
                            and (OUT / 'worker-final-cgroup.json').exists()):
                        break
                    raise
                receipt['last_cgroup_metrics'] = load
                peak_memory = max(peak_memory, load['memory'])
                peak_tasks = max(peak_tasks, load['tasks'])
                decision = policy.check({
                    'available': sample['available'], 'full': sample['psi']['full']['avg10'],
                    'some': sample['psi']['some']['avg10'], 'memory': load['memory'],
                    'tasks': load['tasks'], 'oom_events': sum(load['memory_events'].get(k, 0)
                    for k in ('oom', 'oom_kill')), 'max_events': load['memory_events'].get('max', 0),
                    'pids_events': load['pids_events'].get('max', 0)}, now=time.monotonic())
                log.write(json.dumps({'host': sample, 'load': load}) + '\n')
                log.flush()
                if decision:
                    raise RuntimeError(decision)
                if not policy.may_enqueue({'tasks': load['tasks']}):
                    (OUT / 'queue-closed').touch()
                if (OUT / 'command.log').exists() and (OUT / 'command.log').stat().st_size > 20 * MIB:
                    raise RuntimeError('log budget exceeded')
                if time.monotonic() - start > C['seconds'] + 65:
                    raise RuntimeError('supervisor total deadline')
                if (OUT / 'command-result.json').exists():
                    break
                if proc(ready['identity']['pid']) != ready['identity']:
                    raise RuntimeError('worker lost without receipt')
                time.sleep(.25)
        receipt['command_result'] = json.loads((OUT / 'command-result.json').read_text())
        receipt['worker_final_cgroup'] = json.loads((OUT / 'worker-final-cgroup.json').read_text())
        final = receipt['worker_final_cgroup']
        if (any(final['memory_events'].get(k, 0) for k in ('max', 'oom', 'oom_kill'))
                or final['pids_events'].get('max', 0)):
            raise RuntimeError('final hard resource event: invalid sample')
    except Exception as exc:
        reason = str(exc)
        write('abort.json', {'reason': reason, 'at': time.time()})
    finally:
        try:
            finish_owned_unit(owned, receipt, unit)
            clean_temporary(receipt)
        except Exception as exc:
            reason = (reason or '') + ' CLEANUP_UNKNOWN: ' + str(exc)
        if job_lock is not None:
            os.close(job_lock)
        receipt.update(reason=reason, min_available_bytes=minimum if minimum != 2**63 else None,
                       peak_memory_bytes=peak_memory, peak_tasks=peak_tasks,
                       elapsed_seconds=time.monotonic() - start)
        receipt['state'] = 'INVALID' if reason else ('PASS' if receipt.get('command_result', {}).get('exit_code') == 0 else 'FAIL')
        write('receipt.json', receipt)
    return result_exit_code(reason, receipt.get('command_result'))


def finish_owned_unit(owned, receipt, unit):
    if owned:
        current = fields(unit)
        group = CG / owned['ControlGroup'].lstrip('/')
        gone = not group.exists()
        if gone and not current.get('InvocationID'):
            receipt['already_exited'] = True
            receipt['cgroup_removed'] = True
            receipt['final_unit'] = current
        elif current.get('InvocationID') != owned['InvocationID'] or (current.get('ControlGroup') and current['ControlGroup'] != owned['ControlGroup']):
            raise RuntimeError('unit identity changed')
        else:
            try:
                call(['systemctl', '--user', 'stop', unit])
            except subprocess.CalledProcessError as exc:
                if exc.returncode != 5:
                    raise
                # A finished transient service can unload between show and
                # stop. Only treat that race as clean if the exact scope is
                # gone and the name was not reused.
                final = fields(unit)
                if (group.exists() or final.get('LoadState') != 'not-found'
                        or final.get('InvocationID') or final.get('ControlGroup')
                        or final.get('MainPID') != '0' or final.get('ActiveState') != 'inactive'):
                    raise
                receipt['already_exited'] = True
            else:
                final = fields(unit)
            receipt['final_unit'] = final
            receipt['cgroup_removed'] = not group.exists()
            if group.exists() and int((group/'pids.current').read_text()) != 0:
                raise RuntimeError('owned workload still has tasks after stop')


def clean_temporary(receipt):
    marker = OUT / 'temporary.json'
    if not marker.exists():
        return
    value = json.loads(marker.read_text())
    path = Path(value['path'])
    if path.parent != Path('/tmp') or not path.name.startswith('ut-') or path.is_symlink():
        raise RuntimeError('temporary path identity invalid')
    if path.exists():
        st = path.stat()
        if (st.st_dev, st.st_ino) != (value['device'], value['inode']):
            raise RuntimeError('temporary path was replaced')
        shutil.rmtree(path)
    receipt['temporary_removed'] = True


def validate_spec(config):
    scope = config.get('scope', 'offline')
    if scope not in {'offline', 'host-fuse'}:
        raise ValueError('invalid guarded job scope')
    is_host_fuse = scope == 'host-fuse'
    prefix = 'unifier-t2-fuse-' if is_host_fuse else 'unifier-t1-'
    max_seconds = 120 if is_host_fuse else 300
    max_memory_mib = DEFAULT_MEMORY_MIB if is_host_fuse else MAX_OFFLINE_MEMORY_MIB
    memory_mib = config.get('memory_mib')
    if (type(memory_mib) is not int or not DEFAULT_MEMORY_MIB <= memory_mib <= max_memory_mib
            or config.get('reserve_bytes') != 1024**3
            or type(config.get('seconds')) is not int or not 1 <= config['seconds'] <= max_seconds
            or config.get('root') != str(ROOT)
            or not isinstance(config.get('command'), list) or not config['command']
            or any(not isinstance(x, str) or '\0' in x for x in config['command'])
            or not config['command'][0]
            or not isinstance(config.get('unit'), str)
            or not config['unit'].startswith(prefix)
            or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in config['unit'])):
        raise ValueError('invalid T1 spec or changed safety budget')
    if is_host_fuse:
        command = config['command']
        probe = Path(command[-1])
        if (command[:-1] != HOST_FUSE_COMMAND or not probe.is_absolute()
                or probe != probe.resolve() or not probe.is_relative_to(ROOT / 'artifacts')
                or probe.suffix != '.py' or not probe.is_file()):
            raise ValueError('host FUSE probe requires a Python file under artifacts and a private mount namespace')


def main(argv=None):
    global SPEC, C, OUT
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='mode', required=True)
    launch = sub.add_parser('run', help='run one authorized offline command; no automatic retries')
    launch.add_argument('--out', required=True, type=Path, help='new evidence directory')
    launch.add_argument('--seconds', type=int, default=300)
    launch.add_argument('--memory-mib', type=int, default=DEFAULT_MEMORY_MIB,
                        help='T1 memory budget in MiB (512–1024, default: 512)')
    launch.add_argument('--locale', default='C')
    launch.add_argument('command', nargs=argparse.REMAINDER)
    host_fuse = sub.add_parser('run-host-fuse', help='run one isolated T2 host FUSE probe')
    host_fuse.add_argument('--out', required=True, type=Path)
    host_fuse.add_argument('--seconds', type=int, default=90)
    host_fuse.add_argument('--probe', required=True, type=Path)
    for mode in ('guard', 'worker'):
        sub.add_parser(mode).add_argument('spec', type=Path)
    args = parser.parse_args(argv)
    if args.mode in {'run', 'run-host-fuse'}:
        is_host_fuse = args.mode == 'run-host-fuse'
        if is_host_fuse:
            command = [*HOST_FUSE_COMMAND, str(args.probe.resolve())]
        else:
            command = args.command[1:] if args.command[:1] == ['--'] else args.command
        prefix = 'unifier-t2-fuse-' if is_host_fuse else 'unifier-t1-'
        locale = 'C' if is_host_fuse else args.locale
        memory_mib = DEFAULT_MEMORY_MIB if is_host_fuse else args.memory_mib
        config = {'unit': prefix+uuid.uuid4().hex[:12], 'seconds': args.seconds,
                  'memory_mib': memory_mib, 'reserve_bytes': 1024**3, 'root': str(ROOT),
                  'command': command, 'environment': {'LC_ALL': locale, 'LANG': locale}}
        if is_host_fuse:
            config['scope'] = 'host-fuse'
        validate_spec(config)
        out = args.out.resolve()
        out.mkdir(mode=0o700)  # Existing attempts, especially aborts, are immutable.
        spec = out / 'spec.json'
        spec.write_text(json.dumps(config, ensure_ascii=False, indent=2))
        return subprocess.call(['systemd-run', '--user', '--wait', '--collect',
            '--unit='+config['unit']+'-guard', '--working-directory='+str(ROOT),
            '--property=MemoryMax=64M', '--property=MemoryHigh=48M',
            '--property=MemorySwapMax=0', '--property=TasksMax=16', '--property=CPUQuota=100%',
            '--property=KillMode=control-group', '--property=Restart=no',
            '--property=RuntimeMaxSec='+str(args.seconds+90),
            sys.executable, str(Path(__file__).resolve()), 'guard', str(spec)])
    SPEC = args.spec.resolve()
    C = json.loads(SPEC.read_text())
    OUT = SPEC.parent
    validate_spec(C)
    return guardian() if args.mode == 'guard' else worker()


if __name__ == '__main__':
    sys.exit(main())
