"""Single offline job, independent supervisor, exact transient units, no Podman."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time

SPEC = Path(sys.argv[2]).resolve()
C = json.loads(SPEC.read_text())
OUT = SPEC.parent
CG = Path('/sys/fs/cgroup')
MIB = 1024**2


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
    return {'pid': pid, 'starttime': raw[19], 'cgroup': group}


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
            'processes': processes}


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
    identity = proc(os.getpid())
    write('worker-ready.json', {'identity': identity, 'limits': limits(identity['cgroup'])})
    deadline = time.monotonic() + C['seconds']
    # systemd BindsTo handles supervisor exit; heartbeat handles a live but stuck supervisor.
    while not (OUT / 'go').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('no verified launch permission')
        time.sleep(.05)
    private = Path(C.get('private_root', str(OUT / 'private')))
    for name in ('home', 'tmp', 'config', 'cache', 'data', 'runtime'):
        (private / name).mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(os.environ)
    for name in ('DISPLAY', 'WAYLAND_DISPLAY', 'DBUS_SESSION_BUS_ADDRESS',
                 'SESSION_MANAGER', 'XAUTHORITY', 'SSH_AUTH_SOCK'):
        env.pop(name, None)
    env.update(HOME=str(private / 'home'), TMPDIR=str(private / 'tmp'),
               XDG_CONFIG_HOME=str(private / 'config'), XDG_CACHE_HOME=str(private / 'cache'),
               XDG_DATA_HOME=str(private / 'data'), XDG_RUNTIME_DIR=str(private / 'runtime'),
               UNIFIER_HOME=str(private / 'installation'), PYTHONDONTWRITEBYTECODE='1',
               MULTILINUX_INTEGRATION='0', MULTILINUX_EVIDENCE='0',
               LC_ALL='C', LANG='C', GOMAXPROCS='1',
               PYTHONPATH=C['root'] + '/src:' + C['root'] + '/tests:' + C['root'])
    env.update(C.get('environment', {}))
    with (OUT / 'command.log').open('wb') as log:
        stdin = open(C['stdin'], 'rb') if C.get('stdin') else subprocess.DEVNULL
        child = subprocess.Popen(C['command'], cwd=C['root'], env=env,
                                 stdin=stdin, stdout=log, stderr=subprocess.STDOUT)
        write('command-identity.json', {'identity': proc(child.pid), 'argv': C['command']})
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
    start = time.monotonic()
    unit = C['unit'] + '-work.service'
    ownunit = C['unit'] + '-guard.service'
    receipt = {'reserve_bytes': C['reserve_bytes'], 'command': C['command'],
               'scope': 'T1 offline only; containers and GUI not admitted',
               'supervisor': proc(os.getpid())}
    receipt['supervisor_limits'] = limits(receipt['supervisor']['cgroup'])
    try:
        if (OUT / 'abort.json').exists():
            raise RuntimeError('persistent abort record exists')
        if int(receipt['supervisor_limits']['memory.max']) != 64 * MIB:
            raise RuntimeError('supervisor hard budget mismatch')
        # Fresh 30s admission, including the independent monitor and margin.
        threshold = C['reserve_bytes'] + C['memory_mib'] * MIB + 64 * MIB + 128 * MIB
        with (OUT / 'admission.jsonl').open('w') as log:
            stable_since = None
            admission_deadline = time.monotonic() + 60
            while True:
                sample = host()
                log.write(json.dumps(sample) + '\n')
                log.flush()
                if (sample['available'] < threshold or sample['psi']['full']['avg10'] >= .5
                        or sample['psi']['some']['avg10'] >= 2):
                    stable_since = None
                elif stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= 30:
                    break
                if time.monotonic() >= admission_deadline:
                    raise RuntimeError('no continuous 30s admission window within 60s')
                time.sleep(.25)
        write('heartbeat.json', {'monotonic': time.monotonic()})
        properties = ['MemoryMax=' + str(C['memory_mib']) + 'M',
                      'MemoryHigh=' + str(C['memory_mib'] * 3 // 4) + 'M',
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
        if (owned['ControlGroup'] != group or int(actual['memory.max']) != C['memory_mib'] * MIB
                or actual['memory.swap.max'] != '0' or actual['pids.max'] != '96'
                or actual['cpu.max'].split()[0] == 'max'):
            raise RuntimeError('actual worker cgroup budget mismatch')
        receipt['worker'] = ready
        (OUT / 'go').touch()
        low = highmem = hightasks = psi = 0
        with (OUT / 'samples.jsonl').open('w') as log:
            while True:
                sample = host()
                minimum = min(minimum, sample['available'])
                write('heartbeat.json', {'monotonic': time.monotonic()})
                try:
                    load = metrics(group)
                except FileNotFoundError:
                    if (OUT / 'command-result.json').exists():
                        break
                    raise
                receipt['last_cgroup_metrics'] = load
                peak_memory = max(peak_memory, load['memory'])
                peak_tasks = max(peak_tasks, load['tasks'])
                low = low + 1 if sample['available'] < C['reserve_bytes'] else 0
                highmem = highmem + 1 if load['memory'] >= C['memory_mib'] * MIB * .9 else 0
                hightasks = hightasks + 1 if load['tasks'] >= 96 * .9 else 0
                psi = psi + 1 if sample['psi']['full']['avg10'] > 3 or sample['psi']['some']['avg10'] > 10 else 0
                log.write(json.dumps({'host': sample, 'load': load}) + '\n')
                log.flush()
                if low >= 2 or highmem >= 2 or hightasks >= 2 or psi >= 8:
                    raise RuntimeError('host/cgroup resource warning threshold')
                if any(load['memory_events'].get(k, 0) for k in ('max', 'oom', 'oom_kill')) or load['pids_events'].get('max', 0):
                    raise RuntimeError('hard resource event: invalid sample')
                if load['tasks'] >= 96 * .8:
                    raise RuntimeError('task admission closed at 80 percent')
                if (OUT / 'command.log').exists() and (OUT / 'command.log').stat().st_size > 20 * MIB:
                    raise RuntimeError('log budget exceeded')
                if time.monotonic() - start > C['seconds'] + 65:
                    raise RuntimeError('supervisor total deadline')
                if (OUT / 'command-result.json').exists():
                    break
                if not Path(f'/proc/{ready["identity"]["pid"]}').exists():
                    raise RuntimeError('worker lost without receipt')
                time.sleep(.25)
        receipt['command_result'] = json.loads((OUT / 'command-result.json').read_text())
        receipt['worker_final_cgroup'] = json.loads((OUT / 'worker-final-cgroup.json').read_text())
    except Exception as exc:
        reason = str(exc)
        write('abort.json', {'reason': reason, 'at': time.time()})
    finally:
        if owned:
            current = fields(unit)
            gone = not (CG / owned['ControlGroup'].lstrip('/')).exists()
            if gone and not current.get('InvocationID'):
                receipt['already_exited'] = True
                receipt['cgroup_removed'] = True
                receipt['final_unit'] = current
            elif current.get('InvocationID') != owned['InvocationID'] or (current.get('ControlGroup') and current['ControlGroup'] != owned['ControlGroup']):
                reason = (reason or '') + ' CLEANUP_UNKNOWN: unit identity changed'
            else:
                # Exact transient unit belongs solely to this job; systemd checks cgroup membership.
                call(['systemctl', '--user', 'stop', unit])
                receipt['final_unit'] = fields(unit)
                receipt['cgroup_removed'] = not (CG / owned['ControlGroup'].lstrip('/')).exists()
        receipt.update(reason=reason, min_available_bytes=minimum if minimum != 2**63 else None,
                       peak_memory_bytes=peak_memory, peak_tasks=peak_tasks,
                       elapsed_seconds=time.monotonic() - start)
        write('receipt.json', receipt)
    return 2 if reason else 0


sys.exit(guardian() if sys.argv[1] == 'guard' else worker())
