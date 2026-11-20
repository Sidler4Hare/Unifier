"""Collect bounded CPU correctness and read-only Linux resource evidence."""

import ctypes
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import threading
import time


CPU_SYSFS = Path('/sys/devices/system/cpu')
NODE_SYSFS = Path('/sys/devices/system/node')
CGROUP_MOUNT = Path('/sys/fs/cgroup')
EXPECTED_COUNT = 20_000
THREAD_TIMEOUT = 2.0
MAX_EVIDENCE_BYTES = 16 * 1024 - 1
MAX_LIBRARY_BYTES = 64 * 1024 - 1
MAX_CPU_ID = 65_535
MAX_TOPOLOGY_CPUS = 256
CGROUP_LIMITS = ('cpu.max', 'memory.max', 'memory.high', 'memory.swap.max', 'pids.max',
                 'cpuset.cpus.effective', 'cpuset.mems.effective')


def read_text(path, limit=8192, *, contains_paths=False):
    with path.open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f'read limit exceeded: {path}')
    text = os.fsdecode(data) if contains_paths else data.decode('ascii')
    return text.strip()


def read_optional(path, limit=8192):
    try:
        return read_text(path, limit)
    except FileNotFoundError:
        return None


def id_set(text):
    if not text:
        return set()
    ids = set()
    end = -1
    for part in text.split(','):
        if not re.fullmatch(r'[0-9]+(?:-[0-9]+)?', part):
            raise ValueError(f'malformed CPU or NUMA range: {part!r}')
        first, separator, last = part.partition('-')
        low = int(first)
        high = int(last) if separator else low
        if not end < low <= high <= MAX_CPU_ID:
            raise ValueError(f'invalid CPU or NUMA bounds: {part!r}')
        ids.update(range(low, high + 1))
        end = high
    return ids


def compact_ids(ids):
    ranges = []
    start = end = None
    for identifier in sorted(ids):
        if end is not None and identifier != end + 1:
            ranges.append(str(start) if start == end else f'{start}-{end}')
            start = None
        if start is None:
            start = identifier
        end = identifier
    if end is not None:
        ranges.append(str(start) if start == end else f'{start}-{end}')
    return ','.join(ranges)


def read_proc_status():
    fields = {}
    for line in read_text(Path('/proc/self/status'), 65_536).splitlines():
        name, _, text = line.partition(':')
        if name in ('Cpus_allowed_list', 'Mems_allowed_list', 'CapEff'):
            fields[name] = text.strip()
    return fields


def mountinfo_field(text):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), text)


def current_cgroup():
    raw = read_text(Path('/proc/self/cgroup'), contains_paths=True)
    unified = [line[3:] for line in raw.splitlines() if line.startswith('0::')]
    if len(unified) != 1:
        raise ValueError('one unified cgroup v2 entry is required')
    membership = PurePosixPath(unified[0])
    if not membership.is_absolute() or '..' in membership.parts:
        raise ValueError('unusable unified cgroup member path')

    for line in read_text(Path('/proc/self/mountinfo'), 262_144, contains_paths=True).splitlines():
        before, separator, after = line.partition(' - ')
        if not separator or after.split(' ', 1)[0] != 'cgroup2':
            continue
        fields = before.split()
        mountpoint = Path(mountinfo_field(fields[4]))
        if mountpoint != CGROUP_MOUNT:
            continue
        root = PurePosixPath(mountinfo_field(fields[3]))
        # In a cgroup namespace the membership can be relative to that root.
        relative = membership.relative_to(root if membership.is_relative_to(root) else '/')
        leaf = mountpoint.joinpath(*relative.parts)
        processes = read_optional(leaf / 'cgroup.procs', 65_536)
        if processes is not None and str(os.getpid()) in processes.splitlines():
            return mountpoint, leaf, str(root), str(membership)
    raise ValueError('current PID was not found under the visible cgroup v2 mount')


def cpu_stat(path):
    raw = read_optional(path / 'cpu.stat')
    if raw is None:
        return None
    counters = {}
    for line in raw.splitlines():
        name, amount = line.split()
        value = int(amount)
        if value < 0 or name in counters:
            raise ValueError(f'invalid CPU statistic: {line!r}')
        counters[name] = value
    return counters


def limit_value(name, text):
    if name == 'cpu.max':
        quota, period = text.split()
        rate = [None if quota == 'max' else int(quota), int(period)]
        if rate[1] <= 0 or (rate[0] is not None and rate[0] <= 0):
            raise ValueError(f'invalid cpu.max: {text!r}')
        return rate
    if name.startswith('cpuset.'):
        return compact_ids(id_set(text))
    value = None if text == 'max' else int(text)
    if value is not None and value < 0:
        raise ValueError(f'invalid {name}: {text!r}')
    return value


def context_cgroup():
    mount, leaf, root, membership = current_cgroup()
    paths = [leaf]
    while paths[-1] != mount:
        if len(paths) == 24:
            raise ValueError('visible cgroup depth exceeds probe limit')
        paths.append(paths[-1].parent)
    rows = []
    effective = {}
    present_files = set()
    for path in paths:
        relative = path.relative_to(mount)
        values = {}
        for name in CGROUP_LIMITS:
            text = read_optional(path / name)
            if text is not None:
                values[name] = limit_value(name, text)
                present_files.add(name)
        rows.append({'path': '/' + relative.as_posix() if relative.parts else '/',
                     'limits': values, 'cpu_stat_before': cpu_stat(path)})
        for name, value in values.items():
            if name == 'cpu.max':
                quota, period = value
                previous = effective.get(name)
                if name not in effective or (quota is not None and (
                        previous[0] is None or quota * previous[1] < previous[0] * period)):
                    effective[name] = value
            elif name.startswith('cpuset.'):
                if name in effective:
                    effective[name] &= id_set(value)
                else:
                    effective[name] = id_set(value)
            elif name not in effective or (value is not None and (
                    effective[name] is None or value < effective[name])):
                effective[name] = value
    for name in ('cpuset.cpus.effective', 'cpuset.mems.effective'):
        if name in effective:
            effective[name] = compact_ids(effective[name])
    return {'status': 'PARTIAL', 'scope': 'visible_cgroup_v2', 'mount_root': root,
            'membership': membership, 'chain': rows, 'effective': effective,
            'unobserved_limit_files': sorted(set(CGROUP_LIMITS) - present_files),
            'forced_throttling': 'NOT_RUN'}, paths


def finish_cgroup(report, paths):
    for row, path in zip(report['chain'], paths):
        after = cpu_stat(path)
        row['cpu_stat_after'] = after
        before = row['cpu_stat_before']
        if before is not None and after is not None:
            row['cpu_stat_delta'] = {key: after[key] - before[key] for key in before.keys() & after.keys()}


def read_id_fields(root, names):
    return {name: read_optional(root / name) for name in names}


def context_topology(fields, limits):
    affinity = os.sched_getaffinity(0)
    cpu = read_id_fields(CPU_SYSFS, ('possible', 'present', 'online'))
    node = read_id_fields(NODE_SYSFS, ('possible', 'online', 'has_cpu', 'has_memory'))
    counts = os.cpu_count()
    allowed_cpus = fields.get('Cpus_allowed_list')
    allowed_mems = fields.get('Mems_allowed_list')
    cpuset_cpus = limits.get('cpuset.cpus.effective')
    cpuset_mems = limits.get('cpuset.mems.effective')
    checks = {
        'proc_matches_sched_affinity': affinity == id_set(allowed_cpus) if allowed_cpus is not None else None,
        'affinity_within_online': affinity <= id_set(cpu['online']) if cpu['online'] is not None else None,
        'affinity_within_effective_cpuset': affinity <= id_set(cpuset_cpus) if cpuset_cpus is not None else None,
        'allowed_mems_within_online_nodes': id_set(allowed_mems) <= id_set(node['online'])
        if allowed_mems is not None and node['online'] is not None else None,
        'allowed_mems_within_effective_cpuset': id_set(allowed_mems) <= id_set(cpuset_mems)
        if allowed_mems is not None and cpuset_mems is not None else None,
    }
    packages = {}
    missing = []
    for identifier in sorted(affinity)[:MAX_TOPOLOGY_CPUS]:
        location = CPU_SYSFS / f'cpu{identifier}' / 'topology'
        package = read_optional(location / 'physical_package_id')
        core = read_optional(location / 'core_id')
        if not package or not core or int(package) < 0 or int(core) < 0:
            missing.append(identifier)
            continue
        package_data = packages.setdefault(package, {'cpus': [], 'cores': set()})
        package_data['cpus'].append(identifier)
        package_data['cores'].add(int(core))
    numa_nodes = {}
    online_nodes = id_set(node['online'])
    for identifier in sorted(online_nodes)[:MAX_TOPOLOGY_CPUS]:
        listed = read_optional(NODE_SYSFS / f'node{identifier}' / 'cpulist')
        numa_nodes[str(identifier)] = None if listed is None else {
            'visible_cpus': listed, 'affinity_cpus': compact_ids(id_set(listed) & affinity)}
    partial = (None in checks.values() or counts is None or missing
               or len(affinity) > MAX_TOPOLOGY_CPUS or len(online_nodes) > MAX_TOPOLOGY_CPUS
               or any(value is None for value in numa_nodes.values()))
    return {'status': 'FAIL' if False in checks.values() else ('PARTIAL' if partial else 'PASS'),
            'python_cpu_count': counts, 'sched_affinity': compact_ids(affinity),
            'proc_cpus_allowed': allowed_cpus, 'proc_mems_allowed': allowed_mems,
            'sysfs_cpu': cpu, 'sysfs_numa': node, 'numa_nodes': numa_nodes, 'checks': checks,
            'affinity_cpu_topology_limit': MAX_TOPOLOGY_CPUS, 'topology_missing': compact_ids(missing),
            'packages': {name: {'affinity_cpus': compact_ids(data['cpus']),
                                'affinity_core_count': len(data['cores'])}
                         for name, data in packages.items()}}


def context_perf(fields):
    paranoid = read_optional(Path('/proc/sys/kernel/perf_event_paranoid'))
    raw = fields.get('CapEff')
    caps = int(raw, 16) if raw is not None else None
    return {'status': 'NOT_RUN', 'paranoid': int(paranoid) if paranoid is not None else None,
            'cap_eff': raw, 'cap_perfmon': bool(caps & (1 << 38)) if caps is not None else None,
            'cap_sys_admin': bool(caps & (1 << 21)) if caps is not None else None}


def configure_library(path, expected):
    library_path = Path(path)
    if not library_path.is_absolute() or '..' in library_path.parts or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('expected an absolute library path and a lowercase SHA-256')
    descriptor = os.open(library_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= MAX_LIBRARY_BYTES:
            raise ValueError('library must be a regular file smaller than 64 KiB')
        payload = stream.read(MAX_LIBRARY_BYTES + 1)
        if len(payload) != metadata.st_size or hashlib.sha256(payload).hexdigest() != expected:
            raise ValueError('library SHA-256 differs from the registered fixture')
        library = ctypes.CDLL(f'/proc/self/fd/{descriptor}', mode=ctypes.RTLD_LOCAL)
    for name in ('reset', 'add_fixed', 'load'):
        getattr(library, name).argtypes = []
    library.reset.restype = None
    library.add_fixed.restype = None
    library.load.restype = ctypes.c_ulong
    return library


def run_atomic(path, expected, report):
    library = configure_library(path, expected)
    state = {'status': 'INVALID', 'library_sha256': expected, 'expected': EXPECTED_COUNT,
             'workers': [{}, {}]}
    report['c03'] = state
    barrier = threading.Barrier(2, timeout=THREAD_TIMEOUT)

    def worker(index):
        result = state['workers'][index]
        try:
            result['start_ns'] = time.monotonic_ns()
            result['barrier_index'] = barrier.wait()
            result['after_barrier_ns'] = time.monotonic_ns()
            library.add_fixed()
            result['end_ns'] = time.monotonic_ns()
        except Exception as exc:
            result['error'] = str(exc) or type(exc).__name__

    library.reset()
    state['initial'] = library.load()
    start_ns = time.monotonic_ns()
    deadline = time.monotonic() + THREAD_TIMEOUT
    workers = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    active = []
    try:
        for thread in workers:
            thread.start()
            active.append(thread)
    except Exception as exc:
        state['start_error'] = str(exc)
        barrier.abort()
    for thread in active:
        thread.join(timeout=max(0, deadline - time.monotonic()))
    if any(thread.is_alive() for thread in active):
        state['error'] = 'worker threads did not terminate within 2 seconds'
        try:
            emit(report)
        finally:
            os._exit(2)
    end_ns = time.monotonic_ns()
    state['duration_ns'] = end_ns - start_ns
    state['actual'] = library.load()
    if 'start_error' in state or any('error' in result for result in state['workers']):
        return
    synchronized = {result['barrier_index'] for result in state['workers']} == {0, 1}
    latest_start = max(result['start_ns'] for result in state['workers'])
    state['monotonic'] = all(start_ns <= result['start_ns'] <= latest_start <= result['after_barrier_ns']
                             <= result['end_ns'] <= end_ns for result in state['workers'])
    state['status'] = 'PASS' if (synchronized and state['monotonic'] and state['initial'] == 0
                                 and state['actual'] == EXPECTED_COUNT) else 'FAIL'


def record_error(report, key, exc):
    report[key] = {'status': 'PARTIAL', 'error': str(exc) or type(exc).__name__}


def cpu_provenance():
    try:
        system = os.uname()
        machine, release = system.machine, system.release
    except OSError:
        machine = release = None
    return {
        'status': 'PARTIAL',
        'scope': 'current-process',
        'system': {'source': 'os.uname', 'machine': machine, 'kernel_release': release},
        'cpuid': {'source': None, 'vendor': None, 'leaf1_eax': None},
    }


def emit(report):
    encoded = json.dumps(report, ensure_ascii=True, separators=(',', ':'), sort_keys=True).encode('ascii') + b'\n'
    if len(encoded) > MAX_EVIDENCE_BYTES and 'cpu_provenance' in report:
        # Keep the pre-existing evidence budget and exit decision if optional
        # provenance would put a valid receipt over the size limit.
        pre_provenance = {key: value for key, value in report.items() if key != 'cpu_provenance'}
        return emit(pre_provenance)
    if len(encoded) > MAX_EVIDENCE_BYTES:
        encoded = b'{"schema":1,"status":"INVALID","error":"evidence exceeds 16 KiB"}\n'
        code = 2
    else:
        code = 0 if (report['c03']['status'] == 'PASS' and report['c04']['status'] != 'FAIL') else 1
    sys.stdout.buffer.write(encoded)
    sys.stdout.buffer.flush()
    return code


def main():
    if len(sys.argv) != 3:
        print(f'usage: {sys.argv[0]} /absolute/path/to/libcpu_atomic.so sha256', file=sys.stderr)
        return 2
    report = {'schema': 1, 'c03': {'status': 'NOT_RUN'}, 'c04': {'status': 'NOT_RUN'},
              'c05': {'status': 'NOT_RUN'}, 'c07': {'status': 'NOT_RUN'},
              'cpu_provenance': cpu_provenance()}
    fields = {}
    try:
        fields = read_proc_status()
    except (OSError, ValueError) as exc:
        report['proc_status_error'] = str(exc)
    paths = []
    try:
        report['c05'], paths = context_cgroup()
    except (OSError, ValueError) as exc:
        record_error(report, 'c05', exc)
    try:
        report['c04'] = context_topology(fields, report['c05'].get('effective', {}))
    except (OSError, ValueError) as exc:
        record_error(report, 'c04', exc)
    try:
        report['c07'] = context_perf(fields)
    except (OSError, ValueError) as exc:
        report['c07']['error'] = str(exc)
    try:
        run_atomic(sys.argv[1], sys.argv[2], report)
    except (OSError, ValueError, AttributeError) as exc:
        report['c03'] = {'status': 'INVALID', 'error': str(exc)}
    if paths:
        try:
            finish_cgroup(report['c05'], paths)
        except (OSError, ValueError) as exc:
            report['c05']['after_error'] = str(exc)
    return emit(report)


if __name__ == '__main__':
    raise SystemExit(main())
