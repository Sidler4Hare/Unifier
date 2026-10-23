#!/usr/bin/env python3
"""Real-container lifecycle verification for the multilinux environment manager.

The driver exercises the product CLI (project/tools/ml.py from the frozen
evidence snapshot) against two fresh, isolated Podman environments and records
raw evidence for every scenario:

A. cold managed open -> close -> automatic environment stop (10 samples/target)
B. close then re-open after 150-250 ms (3 samples/target)
C. 30 consecutive warm open/close cycles, then zombie/child accounting
D. SIGKILL only the top-level launcher; window must remain and closing it must
   still stop the environment and the desktop bridge
E. SIGKILL the launcher and its worker; a following normal managed cycle must
   recover auto-stop ownership without leaving leases
F. two concurrent windows on one environment: one container, two leases, the
   environment survives the first close and stops after the last
G. manually pre-started environment is retained across a managed open/close
H. three concurrent managed opens from stopped: exactly one container/start

Every scenario is serial; only scenarios F and H intentionally use several
windows of the same environment at the same time.  The script never touches
pre-existing containers, environments or evidence.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo('Asia/Shanghai')
PREFIX = 'ML-LIFE-'
SCENARIOS = ('A', 'B', 'C', 'D', 'E', 'F', 'G', 'H')
STOP_TIMEOUT_S = 25.0
READY_TIMEOUT_S = 40.0
STARTUP_NOTE = 'Multilinux lifecycle verification note\n固定测试文本，不修改现有文档。\n'


def stamp() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def save(path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    temporary.replace(path)


def sha256_file(path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def host_snapshot() -> dict:
    def read(path):
        try:
            return Path(path).read_text(encoding='utf-8')
        except OSError:
            return ''

    meminfo = {}
    for line in read('/proc/meminfo').splitlines():
        key, _, rest = line.partition(':')
        fields = rest.split()
        if fields and fields[0].isdigit():
            meminfo[key] = int(fields[0]) * 1024
    pressure = {}
    for name in ('memory', 'cpu', 'io'):
        for line in read(f'/proc/pressure/{name}').splitlines():
            fields = line.split()
            if len(fields) < 2:
                continue
            values = {}
            for item in fields[1:]:
                key, _, raw = item.partition('=')
                try:
                    values['total_us' if key == 'total' else key] = (
                        int(raw) if key == 'total' else float(raw))
                except ValueError:
                    continue
            pressure[f'{name}.{fields[0]}'] = values
    load = read('/proc/loadavg').split()
    frequencies = []
    for cpu in sorted(Path('/sys/devices/system/cpu').glob('cpu[0-9]*/cpufreq/scaling_cur_freq')):
        try:
            frequencies.append(int(cpu.read_text().strip()))
        except (OSError, ValueError):
            continue
    thermal = []
    for zone in sorted(Path('/sys/class/thermal').glob('thermal_zone*')):
        try:
            kind = (zone / 'type').read_text().strip()
            value = int((zone / 'temp').read_text().strip())
        except (OSError, ValueError):
            continue
        if kind.startswith(('x86_pkg_temp', 'k10temp', 'acpitz', 'pch_')):
            thermal.append({'type': kind, 'temp_c': round(value / 1000, 2)})
    return {
        'taken_at': stamp(),
        'meminfo': {
            'MemTotal_bytes': meminfo.get('MemTotal'),
            'MemFree_bytes': meminfo.get('MemFree'),
            'MemAvailable_bytes': meminfo.get('MemAvailable'),
            'Cached_bytes': meminfo.get('Cached'),
            'SwapTotal_bytes': meminfo.get('SwapTotal'),
            'SwapFree_bytes': meminfo.get('SwapFree'),
        },
        'pressure': pressure,
        'load_average': [float(item) for item in load[:3]] if len(load) >= 3 else load,
        'cpu_freq_khz': frequencies,
        'thermal': thermal,
    }


def stats(values):
    numbers = sorted(item for item in values if isinstance(item, (int, float)))
    if not numbers:
        return None
    count = len(numbers)
    return {
        'count': count,
        'min': numbers[0],
        'p50': numbers[math.ceil(count * .5) - 1],
        'p95': numbers[math.ceil(count * .95) - 1],
        'max': numbers[-1],
        'mean': round(sum(numbers) / count, 3),
    }


def elapsed_ms(start_ns, end_ns):
    if start_ns is None or end_ns is None:
        return None
    return round((end_ns - start_ns) / 1e6, 3)


def run_command(argv, *, timeout=120, check=False):
    result = subprocess.run([str(item) for item in argv], capture_output=True,
                            text=True, timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"command failed ({result.returncode}): {' '.join(map(str, argv))}\n"
                           f"stderr: {result.stderr[-2500:]}")
    return result


def podman_inspect(name: str):
    result = run_command(['podman', 'inspect', '--format', 'json', name], timeout=60)
    if result.returncode:
        detail = (result.stderr or '') + (result.stdout or '')
        if 'no such' in detail.lower():
            return None
        raise RuntimeError(f'podman inspect {name} failed: {detail[-1500:]}')
    payload = json.loads(result.stdout)
    return payload[0] if payload else None


def pid_stamp(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
        return None if fields[0] == 'Z' else int(fields[19])
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None


def pid_state(pid):
    try:
        fields = Path(f'/proc/{pid}/stat').read_text(encoding='utf-8').rsplit(')', 1)[1].split()
        return fields[0]
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError):
        return None


def proc_table() -> dict:
    table = {}
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        try:
            raw = (entry / 'stat').read_text(encoding='utf-8')
            comm = raw.split('(', 1)[1].rsplit(')', 1)[0]
            fields = raw.rsplit(')', 1)[1].split()
            ppid = int(fields[1])
            state = fields[0]
        except (OSError, ValueError, IndexError):
            continue
        try:
            cmdline = (entry / 'cmdline').read_bytes().replace(b'\0', b' ').decode(
                'utf-8', 'replace').strip()
        except OSError:
            cmdline = ''
        table[pid] = {'pid': pid, 'ppid': ppid, 'state': state, 'comm': comm,
                      'cmdline': cmdline, 'stamp': pid_stamp(pid)}
    return table


def descendant_pids(table: dict, root_pid: int) -> list:
    children = {}
    for pid, item in table.items():
        children.setdefault(item['ppid'], []).append(pid)
    found, queue = [], [root_pid]
    while queue:
        current = queue.pop(0)
        for child in children.get(current, []):
            if child not in found:
                found.append(child)
                queue.append(child)
    return found


def kill_pid(pid: int, sig: int) -> dict:
    try:
        os.kill(pid, sig)
        return {'pid': pid, 'signal': int(sig), 'sent': True}
    except ProcessLookupError:
        return {'pid': pid, 'signal': int(sig), 'sent': False, 'reason': 'not found'}
    except PermissionError as exc:
        return {'pid': pid, 'signal': int(sig), 'sent': False, 'reason': str(exc)}


def wait_for(predicate, timeout: float, interval: float = 0.02):
    deadline = time.monotonic() + float(timeout)
    value = None
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(interval)


class EventRecorder:
    """Collect podman events for one container into a JSON-lines file."""

    def __init__(self, directory: Path, container_name: str):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'events.jsonl'
        self.events = []
        self._lock = threading.Lock()
        self._stream = self.path.open('w', encoding='utf-8')
        self.process = subprocess.Popen(
            ['podman', 'events', '--format', 'json',
             '--filter', f'container={container_name}'],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            start_new_session=True)
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    def _read(self):
        for line in iter(self.process.stdout.readline, ''):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            with self._lock:
                self.events.append(payload)
            self._stream.write(json.dumps(payload, ensure_ascii=False) + '\n')
            self._stream.flush()

    def snapshot(self) -> list:
        with self._lock:
            return list(self.events)

    def wait_for(self, predicate, timeout: float):
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            for item in self.snapshot():
                if predicate(item):
                    return item
            time.sleep(0.02)
        for item in self.snapshot():
            if predicate(item):
                return item
        return None

    def status_counts(self) -> dict:
        counts = {}
        for item in self.snapshot():
            status = item.get('Status') or 'unknown'
            counts[status] = counts.get(status, 0) + 1
        return counts

    def stop(self) -> list:
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        self._thread.join(timeout=5)
        try:
            self._stream.close()
        except OSError:
            pass
        return self.snapshot()


class WaylandTrace:
    """Associate attach/commit/frame callbacks with the titled toplevel only."""

    def __init__(self, marker: str):
        self.marker = marker
        self.xdg_surfaces = {}
        self.toplevels = {}
        self.surface = None
        self.attach_ns = self.commit_ns = self.frame_callback_ns = None
        self.callbacks = set()
        self._pending_attach = {}

    def feed(self, line: str, received_ns: int) -> None:
        match = re.search(r'get_xdg_surface\(new id xdg_surface[#@](\d+), wl_surface[#@](\d+)\)', line)
        if match:
            self.xdg_surfaces[match[1]] = match[2]
        match = re.search(r'xdg_surface[#@](\d+)\.get_toplevel\(new id xdg_toplevel[#@](\d+)\)', line)
        if match:
            self.toplevels[match[2]] = self.xdg_surfaces.get(match[1])
        match = re.search(r'xdg_toplevel[#@](\d+)\.set_title\(', line)
        if match and self.marker in line:
            self.surface = self.toplevels.get(match[1])
        match = re.search(r'wl_surface[#@](\d+)\.attach\(wl_buffer[#@]\d+', line)
        if match:
            self._pending_attach[match[1]] = received_ns
        if self.surface:
            if self.attach_ns is None:
                self.attach_ns = self._pending_attach.get(self.surface)
            if re.search(r'wl_surface[#@]' + self.surface + r'\.commit\(', line) and self.attach_ns:
                if self.commit_ns is None:
                    self.commit_ns = received_ns
            match = re.search(r'wl_surface[#@]' + self.surface + r'\.frame\(new id wl_callback[#@](\d+)\)', line)
            if match:
                self.callbacks.add(match[1])
            match = re.search(r'wl_callback[#@](\d+)\.done\(', line)
            if match and match[1] in self.callbacks and self.commit_ns and self.frame_callback_ns is None:
                self.frame_callback_ns = received_ns

    def metadata(self) -> dict:
        return {'surface_id': self.surface,
                'first_attach_received_ns': self.attach_ns,
                'first_commit_received_ns': self.commit_ns,
                'first_frame_callback_received_ns': self.frame_callback_ns}


class ManagedLaunch:
    """One real ml.py app open launch with its own KWin window and logs."""

    def __init__(self, ctx, directory: Path, target: str, marker: str):
        self.ctx = ctx
        self.target = target
        self.marker = marker
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        env_id, app_id = ctx.ids(target)
        self.env_id, self.app_id = env_id, app_id
        self.trace = WaylandTrace(marker)
        self.threads = []
        note = ctx.run / 'work' / env_id / (marker + '.txt')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(STARTUP_NOTE, encoding='utf-8')
        self.note, self.note_sha256 = note, sha256_file(note)
        self.command = [sys.executable, str(ctx.run / 'project/tools/ml.py'),
                        'app', 'open', app_id, '--', str(note)]
        self.started_ns = time.monotonic_ns()
        self.started_wall_ns = time.time_ns()
        self.process = subprocess.Popen(self.command, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        start_new_session=True)
        self.launcher_pid = self.process.pid
        self.launcher_stamp = pid_stamp(self.launcher_pid)
        for stream, name in ((self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')):
            thread = threading.Thread(target=self._read, args=(stream, name), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.window = None
        self.added_ns = None
        self.trace_extra = {}
        self.close_ns = self.close_wall_ns = self.close_ack = None
        self.removed = None
        self.exit_ns = self.returncode = None
        self._exit_event = threading.Event()
        watcher = threading.Thread(target=self._watch_exit, daemon=True)
        watcher.start()
        self.threads.append(watcher)

    def _watch_exit(self):
        try:
            code = self.process.wait()
        except Exception:
            return
        self.returncode = code
        self.exit_ns = time.monotonic_ns()
        self._exit_event.set()

    def _read(self, stream, name: str):
        with (self.directory / (name + '.log')).open('wb') as log:
            for raw in iter(stream.readline, b''):
                received = time.monotonic_ns()
                log.write(raw)
                if name == 'stderr':
                    self.trace.feed(raw.decode('utf-8', 'replace'), received)

    def ready(self, timeout: float = READY_TIMEOUT_S) -> dict:
        deadline = time.monotonic() + float(timeout)
        while time.monotonic() < deadline:
            self.ctx.observer.pump()
            if self.window is None:
                self.window = next((event for event in self.ctx.observer.events
                                    if event['event'] == 'added'
                                    and self.marker in (event.get('caption') or '')), None)
                if self.window is not None:
                    self.added_ns = self.window['received_ns']
            if self.window and self.trace.commit_ns:
                if 'XdgToplevelWindow' not in (self.window.get('type') or ''):
                    raise RuntimeError('test application did not create a native Wayland toplevel')
                geometry = self.window.get('buffer_geometry') or {}
                if not all((geometry.get(key) or 0) > 0 for key in ('width', 'height')):
                    from tools.kwin_window_probe import window_info
                    found = window_info(self.directory / 'geometry-check', self.marker,
                                        window_id=self.window['window_id'])
                    if len(found) != 1 or min(found[0].get('bufferWidth', 0),
                                              found[0].get('bufferHeight', 0)) <= 0:
                        time.sleep(0.003)
                        continue
                    self.trace_extra = {'geometry_verified_by_probe': True,
                                        'added_received_ns': self.window['received_ns'],
                                        'received_ns': time.monotonic_ns(),
                                        'probe': found[0]}
                    self.window = {**self.window,
                                   'buffer_geometry': {'width': found[0]['bufferWidth'],
                                                       'height': found[0]['bufferHeight']}}
                ready_ns = max(self.trace.commit_ns,
                               self.trace_extra.get('received_ns') or self.window['received_ns'])
                return {
                    'ready_ns': ready_ns,
                    'launch_to_ready_ms': elapsed_ms(self.started_ns, ready_ns),
                    'launch_to_window_ms': elapsed_ms(self.started_ns, self.window['received_ns']),
                    'launch_to_buffer_commit_ms': elapsed_ms(self.started_ns, self.trace.commit_ns),
                    'launch_to_frame_callback_ms': elapsed_ms(
                        self.started_ns, self.trace.frame_callback_ns),
                    'window': self.window,
                    'wayland': self.trace.metadata(),
                    'geometry_extra': self.trace_extra,
                }
            if self._exit_event.is_set():
                raise RuntimeError('launcher exited before a verified native window appeared')
            time.sleep(0.003)
        raise TimeoutError('native window plus associated non-null buffer commit did not appear')

    def close_window(self) -> dict:
        if self.window is None:
            raise RuntimeError('cannot close a window that was never observed')
        self.close_ns = time.monotonic_ns()
        self.close_wall_ns = time.time_ns()
        self.close_ack = self.ctx.observer.close_window(self.marker, self.window['window_id'])
        return self.close_ack

    def wait_removed(self, timeout: float = 20.0) -> dict:
        window_id = self.window['window_id'] if self.window else None

        def find():
            self.ctx.observer.pump()
            return next((event for event in self.ctx.observer.events
                         if event['event'] == 'removed' and event.get('window_id') == window_id), None)

        found = wait_for(find, timeout, 0.01)
        if found is None:
            raise TimeoutError('compositor never removed the closed window')
        self.removed = found
        return found

    def wait_exit(self, timeout: float = 60.0) -> dict:
        deadline = time.monotonic() + float(timeout)
        while not self._exit_event.is_set() and time.monotonic() < deadline:
            self.ctx.observer.pump()
            time.sleep(0.01)
        if not self._exit_event.is_set():
            raise TimeoutError('top-level launcher did not exit after the window closed')
        return {'returncode': self.returncode, 'exit_ns': self.exit_ns}

    def finish(self) -> dict:
        for thread in self.threads:
            thread.join(timeout=5)
        return {'returncode': self.returncode, 'exit_ns': self.exit_ns,
                'launcher_pid': self.launcher_pid, 'launcher_stamp': self.launcher_stamp}

    def worker_pids(self) -> list:
        table = proc_table()
        found = []
        for pid in descendant_pids(table, self.launcher_pid):
            item = table.get(pid)
            if item is None:
                continue
            parts = item['cmdline'].split()
            if '_worker' in parts and 'app' in parts:
                found.append(pid)
        return found

    def process_chain(self) -> dict:
        table = proc_table()
        pids = [self.launcher_pid] + descendant_pids(table, self.launcher_pid)
        items = []
        for pid in pids:
            item = table.get(pid)
            if item is not None:
                items.append(item)
        return {'launcher_pid': self.launcher_pid, 'pids': items,
                'worker_pids': [item['pid'] for item in items
                                if '_worker' in item['cmdline'].split() and 'app' in item['cmdline'].split()]}

    def cleanup(self) -> list:
        actions = []
        if not self._exit_event.is_set():
            if self.window is not None:
                try:
                    self.ctx.observer.close_window(self.marker, self.window['window_id'])
                    actions.append({'close_window': 'requested'})
                except Exception as exc:
                    actions.append({'close_window': f'{type(exc).__name__}: {exc}'})
                try:
                    self.wait_removed(timeout=5)
                except Exception:
                    pass
                if self._exit_event.wait(10):
                    actions.append({'launcher_exit': self.returncode})
            if not self._exit_event.is_set():
                if pid_stamp(self.launcher_pid) == self.launcher_stamp:
                    kill_pid(self.launcher_pid, signal.SIGTERM)
                if not self._exit_event.wait(5):
                    if pid_stamp(self.launcher_pid) == self.launcher_stamp:
                        kill_pid(self.launcher_pid, signal.SIGKILL)
                    if not self._exit_event.wait(5):
                        actions.append({'launcher_kill': 'failed'})
        return actions


class Context:
    def __init__(self, run: Path, observer):
        self.run = Path(run).resolve()
        self.observer = observer
        self.manifest = json.loads((self.run / 'manifest.json').read_text(encoding='utf-8'))
        self.targets = self.manifest.get('targets') or {}
        self.project = self.run / 'project'

    def ids(self, target: str):
        item = self.targets[target]
        return item['env_id'], item['app_id']

    def env_record(self, env_id: str) -> dict:
        path = self.project / '.lab/state/environments' / (env_id + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def launch_record(self, app_id: str) -> dict:
        path = self.project / '.lab/state/launches' / (app_id + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def lease_path(self, env_id: str) -> Path:
        return self.project / '.lab/leases' / (env_id + '.json')

    def inspect(self, env_id: str):
        record = self.env_record(env_id)
        return podman_inspect(record['runtime']['container_name'])

    def env_state(self, env_id: str, *, save_to=None) -> dict:
        record = self.env_record(env_id)
        name = record['runtime']['container_name']
        info = podman_inspect(name)
        state = {
            'env_id': env_id,
            'container_name': name,
            'container_id_recorded': record['runtime']['container_id'],
            'record_runtime_state': record.get('runtime_state'),
            'record_full_init': (record.get('runtime') or {}).get('full_init'),
            'inspect': None,
        }
        if info is not None:
            runtime = info.get('State') or {}
            host_config = info.get('HostConfig') or {}
            state['inspect'] = {
                'id': info.get('Id'),
                'running': runtime.get('Running'),
                'status': runtime.get('Status'),
                'pid': runtime.get('Pid'),
                'exit_code': runtime.get('ExitCode'),
                'oom_killed': runtime.get('OOMKilled'),
                'started_at': runtime.get('StartedAt'),
                'finished_at': runtime.get('FinishedAt'),
                'init': host_config.get('Init'),
                'image': info.get('ImageName') or info.get('Image'),
            }
        if save_to is not None:
            save(save_to, state)
        return state

    def container_id(self, env_id: str) -> str:
        return self.env_record(env_id)['runtime']['container_id']

    def lease_state(self, env_id: str) -> dict:
        path = self.lease_path(env_id)
        raw = {'leases': {}, 'auto_stop': False, 'bridge': None}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                raw = {'leases': {}, 'auto_stop': False, 'bridge': None, 'unreadable': True}
        live = {token: item for token, item in (raw.get('leases') or {}).items()
                if item.get('stamp') is not None and pid_stamp(item.get('pid')) == item.get('stamp')}
        bridge = raw.get('bridge')
        bridge_alive = bool(bridge and bridge.get('stamp') is not None
                            and pid_stamp(bridge.get('pid')) == bridge.get('stamp'))
        return {'path': str(path), 'exists': path.is_file(), 'raw': raw,
                'live': live, 'live_count': len(live), 'bridge': bridge,
                'bridge_alive': bridge_alive}

    def portal_directory(self, env_id: str) -> str:
        record = self.env_record(env_id)
        return ((record.get('desktop') or {}).get('portal_proxy') or {}).get('directory') or ''

    def bridge_processes(self, env_id: str) -> list:
        """Find the filtered portal proxy by its private working directory.

        The proxy is started with cwd equal to this environment's private
        portal directory and binds a relative socket name, so the directory
        path does not appear in its argv. The /proc cwd link is the reliable
        identity check and never matches another environment's proxy.
        """
        directory = self.portal_directory(env_id)
        if not directory:
            return []
        target = Path(directory).resolve()
        found = []
        for item in proc_table().values():
            if 'xdg-dbus-proxy' not in item['cmdline']:
                continue
            try:
                cwd = Path(os.readlink(f"/proc/{item['pid']}/cwd")).resolve()
            except OSError:
                continue
            if cwd != target:
                continue
            found.append({'pid': item['pid'], 'cmdline': item['cmdline'][:400],
                          'state': item['state'], 'cwd': str(cwd),
                          'socket_exists': (target / 'bus').is_socket()})
        return found

    def container_ps(self, env_id: str, output=None) -> dict:
        record = self.env_record(env_id)
        name = record['runtime']['container_name']
        result = run_command(['podman', 'exec', name, 'sh', '-c',
                              'ps -eo pid,ppid,stat,comm'], timeout=30)
        text = result.stdout or ''
        if output is not None:
            Path(output).write_text(f"returncode={result.returncode}\n--- stdout ---\n"
                                    f"{text}\n--- stderr ---\n{result.stderr}\n",
                                    encoding='utf-8')
        rows = []
        zombies = []
        children_of_one = []
        for line in text.splitlines()[1:]:
            fields = line.split(None, 3)
            if len(fields) < 4 or not fields[0].isdigit():
                continue
            row = {'pid': int(fields[0]), 'ppid': int(fields[1]),
                   'stat': fields[2], 'comm': fields[3]}
            rows.append(row)
            if 'Z' in row['stat']:
                zombies.append(row)
            if row['ppid'] == 1:
                children_of_one.append(row)
        return {'returncode': result.returncode, 'stderr': result.stderr[-1000:],
                'rows': rows, 'zombies': zombies,
                'children_of_pid1': children_of_one,
                'zombie_count': len(zombies),
                'child_count': len(children_of_one)}

    def pid1_status(self, env_id: str, directory: Path) -> dict:
        record = self.env_record(env_id)
        name = record['runtime']['container_name']
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        outputs = {}
        for label, argv in (
                ('status', ['cat', '/proc/1/status']),
                ('comm', ['cat', '/proc/1/comm']),
                ('cmdline', ['cat', '/proc/1/cmdline']),
                ('ps', ['sh', '-c', 'ps -eo pid,ppid,stat,comm']),
        ):
            result = run_command(['podman', 'exec', name, *argv], timeout=30)
            (directory / f'pid1-{label}.txt').write_text(
                f"returncode={result.returncode}\n--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}\n", encoding='utf-8')
            outputs[label] = {'returncode': result.returncode,
                              'stdout': result.stdout, 'stderr': result.stderr}
        parsed = {}
        for line in outputs['status']['stdout'].splitlines():
            key, _, value = line.partition(':')
            if key in {'Name', 'SigCgt', 'SigIgn', 'SigBlk', 'SigPnd', 'ShdPnd'}:
                parsed[key] = value.strip()
        return {
            'name': parsed.get('Name'),
            'sigcgt': parsed.get('SigCgt'),
            'sigign': parsed.get('SigIgn'),
            'sigblk': parsed.get('SigBlk'),
            'sigpnd': parsed.get('SigPnd'),
            'comm': outputs['comm']['stdout'].strip(),
            'cmdline': outputs['cmdline']['stdout'].replace('\x00', ' ').strip(),
            'ps_stdout': outputs['ps']['stdout'],
            'exec_returncodes': {key: value['returncode'] for key, value in outputs.items()},
        }

    def project_containers(self) -> list:
        project = str(self.project.resolve())
        result = run_command(['podman', 'ps', '-a', '--filter',
                              f'label=multilinux.project={project}',
                              '--format', 'json'], timeout=60, check=True)
        payload = json.loads(result.stdout or '[]')
        rows = []
        for item in payload:
            names = item.get('Names') or []
            rows.append({'id': item.get('Id'), 'names': names,
                         'state': item.get('State'), 'image': item.get('Image')})
        return rows

    def env_containers(self, env_id: str) -> list:
        name = self.env_record(env_id)['runtime']['container_name']
        return [row for row in self.project_containers()
                if name in (row.get('names') or [])]

    # ------------------------------------------------------------------ setup
    def env_cli(self, *argv, timeout=180):
        return run_command([sys.executable, str(self.project / 'tools/ml.py'), *argv],
                           timeout=timeout)

    def pressure_guard(self, directory: Path, label: str = '') -> dict:
        """Record host conditions; pause when the host is under real pressure."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + 180
        attempts = []
        while True:
            host = host_snapshot()
            meminfo = host['meminfo']
            pressure = host['pressure'].get('memory', {})
            some = (pressure.get('some') or {}).get('avg10')
            full = (pressure.get('full') or {}).get('avg10')
            available = meminfo.get('MemAvailable_bytes')
            blocked = ((available is not None and available < 400 * 1024 * 1024)
                       or (full is not None and full >= 25.0)
                       or (some is not None and some >= 60.0))
            attempts.append({'at': host['taken_at'], 'available_bytes': available,
                             'memory_some_avg10': some, 'memory_full_avg10': full,
                             'load': host['load_average'], 'blocked': bool(blocked)})
            if not blocked:
                result = {'taken_at': host['taken_at'], 'blocked': False,
                          'available_bytes': available, 'memory_some_avg10': some,
                          'memory_full_avg10': full, 'load': host['load_average'],
                          'attempts': attempts, 'label': label}
                save(directory / 'host-guard.json', result)
                return result
            if time.monotonic() >= deadline:
                result = {'taken_at': host['taken_at'], 'blocked': True,
                          'available_bytes': available, 'memory_some_avg10': some,
                          'memory_full_avg10': full, 'load': host['load_average'],
                          'attempts': attempts, 'label': label,
                          'reason': 'host memory or PSI stayed above the safety threshold for 180s'}
                save(directory / 'host-guard.json', result)
                raise RuntimeError(result['reason'])
            time.sleep(5)

    def reset_target(self, target: str, directory: Path) -> dict:
        """Stop only this run's own environment and clear its own bridge lease."""
        env_id, _ = self.ids(target)
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        state = self.env_state(env_id)
        actions = []
        if state['inspect'] and state['inspect']['running']:
            result = self.env_cli('env', 'stop', env_id, timeout=180)
            actions.append({'stop': result.returncode, 'stdout': result.stdout.strip(),
                            'stderr': result.stderr.strip()})
            if result.returncode:
                raise RuntimeError(f'{env_id}: could not stop environment before reset')
        stopped = self.wait_env(env_id, running=False, timeout=30)
        actions.append({'wait_stopped': stopped})
        lease = self.lease_state(env_id)
        if lease['live_count']:
            raise RuntimeError(f'{env_id}: refusing to reset with {lease["live_count"]} live leases')
        bridges = self.bridge_processes(env_id)
        killed = []
        for item in bridges:
            killed.append({'pid': item['pid'], 'term': kill_pid(item['pid'], signal.SIGTERM)})
        wait_for(lambda: not self.bridge_processes(env_id), 10, 0.05)
        for item in self.bridge_processes(env_id):
            killed.append({'pid': item['pid'], 'kill': kill_pid(item['pid'], signal.SIGKILL)})
        socket_path = Path(self.portal_directory(env_id) or '/nonexistent') / 'bus'
        if socket_path.is_socket():
            try:
                socket_path.unlink()
                actions.append({'socket_removed': str(socket_path)})
            except OSError:
                pass
        lease = self.lease_state(env_id)
        state = self.env_state(env_id)
        payload = {'target': target, 'env_id': env_id, 'actions': actions,
                   'lease_before': {'live_count': lease['live_count'],
                                    'auto_stop': lease['raw'].get('auto_stop'),
                                    'bridge': lease['bridge']},
                   'bridges_killed': killed,
                   'state_after': state}
        save(directory / 'reset.json', payload)
        containers = self.env_containers(env_id)
        if len(containers) != 1:
            raise RuntimeError(f'unexpected number of containers for {env_id}: {containers}')
        return payload

    def wait_env(self, env_id: str, *, running=None, stopped=None, timeout: float = 30.0) -> dict:
        started = time.monotonic()
        last = None
        while True:
            last = self.env_state(env_id)
            info = last['inspect']
            ok = False
            if info is not None:
                if running is not None:
                    ok = bool(info['running']) == bool(running)
                elif stopped is not None:
                    ok = (not info['running']) == bool(stopped)
            if ok:
                return {'ok': True, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                        'state': last}
            if time.monotonic() - started >= timeout:
                return {'ok': False, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                        'state': last, 'timeout_s': timeout}
            time.sleep(0.01)

    def wait_stopped(self, env_id: str, directory: Path, timeout: float = STOP_TIMEOUT_S,
                     died_event=None, close_wall_ns=None) -> dict:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        deadline = started + float(timeout)
        last = None
        while time.monotonic() < deadline:
            last = self.env_state(env_id)
            info = last['inspect']
            if info is not None and not info['running'] and last['record_runtime_state'] == 'stopped':
                result = {'ok': True, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                          'state': last}
                save(directory / 'stopped.json', result)
                return result
            time.sleep(0.01)
        result = {'ok': False, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                  'state': last, 'timeout_s': timeout}
        save(directory / 'stopped.json', result)
        return result

    def wait_bridge_gone(self, env_id: str, timeout: float = 20.0) -> dict:
        started = time.monotonic()

        def check():
            lease = self.lease_state(env_id)
            if lease['bridge_alive']:
                return None
            if self.bridge_processes(env_id):
                return None
            return {'lease_bridge': lease['bridge']}

        found = wait_for(check, timeout, 0.02)
        return {'ok': found is not None,
                'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                'bridge': self.lease_state(env_id)['bridge'],
                'processes': self.bridge_processes(env_id)}

    def probe_window(self, marker: str, window_id: str, directory: Path) -> list:
        from tools.kwin_window_probe import window_info
        try:
            return window_info(directory, marker, window_id=window_id)
        except Exception:
            return []

    def remaining_windows(self, marker_prefix: str, directory: Path) -> list:
        from tools.kwin_window_probe import window_info
        try:
            return window_info(directory, marker_prefix)
        except Exception:
            return []


# ----------------------------------------------------------------- scenarios
def launch_sample_context(ctx: Context, target: str, scenario: str, index: int):
    directory = ctx.run / 'samples' / target / scenario / f'{index:03d}'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def evaluate_common(row: dict, failures: list) -> None:
    ready = row.get('ready') or {}
    window = ready.get('window') or {}
    if not ready:
        failures.append('window never reached verified native Wayland readiness')
        return
    if not all((window.get('buffer_geometry') or {}).get(key) for key in ('width', 'height')):
        failures.append('window buffer geometry is empty')
    pid1 = row.get('pid1') or {}
    if pid1 and not pid1.get('name'):
        failures.append('PID 1 identity was not captured')
    if row.get('note_unchanged') is not True:
        failures.append('the opened document changed on disk')
    if row.get('removed') is None:
        failures.append('no window-removed event after close')
    if (row.get('launcher_exit') or {}).get('returncode') != 0:
        failures.append('launcher exit status was not zero: '
                        + str((row.get('launcher_exit') or {}).get('returncode')))


def scenario_a(ctx: Context, target: str, count: int, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'A'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    for index in range(count):
        directory = launch_sample_context(ctx, target, 'A', index)
        row = {'scenario': 'A', 'target': target, 'index': index, 'started_at': stamp(),
               'sample_dir': str(directory.relative_to(ctx.run)), 'failures': []}
        launch = recorder = None
        try:
            row['host_before'] = host_snapshot()
            row['host_guard'] = ctx.pressure_guard(directory, 'A')
            row['reset'] = ctx.reset_target(target, directory / 'reset')
            row['env_before'] = ctx.env_state(env_id, save_to=directory / 'env-before.json')
            recorder = EventRecorder(directory, row['env_before']['container_name'])
            marker = f'{PREFIX}{target}-A-{index:03d}'
            launch = ManagedLaunch(ctx, directory, target, marker)
            row['ready'] = launch.ready()
            row['process_chain'] = launch.process_chain()
            row['pid1'] = ctx.pid1_status(env_id, directory)
            row['processes_open'] = ctx.container_ps(env_id, directory / 'processes-open.txt')
            row['lease_open'] = ctx.lease_state(env_id)
            row['window_probe_open'] = ctx.probe_window(marker, launch.window['window_id'],
                                                        directory / 'probe-open')
            time.sleep(0.1)
            launch.close_window()
            row['removed'] = launch.wait_removed(timeout=20)
            died = recorder.wait_for(lambda item: item.get('Status') == 'died', timeout=40)
            row['container_died_event'] = died
            row['launcher_exit'] = launch.wait_exit(timeout=60)
            row['launcher_finish'] = launch.finish()
            row['wait_stopped'] = ctx.wait_stopped(env_id, directory)
            row['wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
            row['env_after'] = ctx.env_state(env_id, save_to=directory / 'env-after.json')
            row['lease_after'] = ctx.lease_state(env_id)
            row['post_window_probe'] = ctx.probe_window(marker, launch.window['window_id'],
                                                        directory / 'probe-after-close')
            row['containers_after'] = ctx.env_containers(env_id)
            row['note_unchanged'] = sha256_file(launch.note) == launch.note_sha256
            died_ns = (died or {}).get('timeNano')
            row['timings'] = {
                'launch_to_ready_ms': (row['ready'] or {}).get('launch_to_ready_ms'),
                'close_to_window_removed_ms': elapsed_ms(launch.close_ns,
                                                         (row['removed'] or {}).get('received_ns')),
                'close_to_launcher_exit_ms': elapsed_ms(launch.close_ns,
                                                        (row['launcher_exit'] or {}).get('exit_ns')),
                'close_to_container_died_ms': (
                    round((died_ns - launch.close_wall_ns) / 1e6, 3)
                    if died_ns and launch.close_wall_ns else None),
                'close_to_stopped_ms': (row.get('wait_stopped') or {}).get('elapsed_ms'),
                'close_to_bridge_gone_ms': (row.get('wait_bridge') or {}).get('elapsed_ms'),
            }
            inspect = (row.get('env_after') or {}).get('inspect') or {}
            row['container_exit_code'] = inspect.get('exit_code')
            row['event_exit_code'] = (died or {}).get('ContainerExitCode')
            row['container_init'] = inspect.get('init')
            row['single_container'] = (
                len(row['containers_after']) == 1
                and (row['containers_after'][0].get('id') or '') == ctx.container_id(env_id))
            failures = row['failures']
            evaluate_common(row, failures)
            if not (row.get('wait_stopped') or {}).get('ok'):
                failures.append(f'environment did not reach stopped within {STOP_TIMEOUT_S}s')
            if (row.get('lease_after') or {}).get('live_count'):
                failures.append('leases remain after the window closed')
            if not (row.get('wait_bridge') or {}).get('ok'):
                failures.append('desktop bridge was still alive after the window closed')
            exit_code = row['container_exit_code']
            if exit_code == 137:
                failures.append('container PID 1 was still killed by the stop timeout (exit 137)')
            elif exit_code not in (0, 143):
                failures.append(f'unexpected container exit code {exit_code!r}')
            if not row['single_container']:
                failures.append(f'expected one owned container, saw {row["containers_after"]}')
            pid1 = row.get('pid1') or {}
            if not (pid1.get('sigcgt') or pid1.get('sigign')):
                failures.append('PID 1 signal disposition was not recorded')
            if not row.get('container_init') and (pid1.get('name') in {None, '', 'sleep'}):
                failures.append('no container init process was observed as PID 1')
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            row['failures'].append('exception: ' + row['error'])
        finally:
            if launch is not None:
                row['cleanup'] = launch.cleanup()
                try:
                    launch.finish()
                except Exception:
                    pass
            if recorder is not None:
                row['events_status_counts'] = recorder.status_counts()
                recorder.stop()
            row['passed'] = not row['failures']
            row['finished_at'] = stamp()
            save(directory / 'result.json', row)
            rows.append(row)
            print(json.dumps({'scenario': 'A', 'target': target, 'index': index,
                              'passed': row['passed'],
                              'ready_ms': (row.get('timings') or {}).get('launch_to_ready_ms'),
                              'stop_ms': (row.get('timings') or {}).get('close_to_stopped_ms'),
                              'died_ms': (row.get('timings') or {}).get('close_to_container_died_ms'),
                              'exit_code': row.get('container_exit_code'),
                              'failures': row['failures']}, ensure_ascii=False), flush=True)
    save(scenario_dir / 'scenario.json', {
        'scenario': 'A', 'target': target, 'requested': count,
        'rows': [summarize_row(row) for row in rows if row['target'] == target],
    })


def scenario_b(ctx: Context, target: str, count: int, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'B'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    delays = [0.150, 0.200, 0.250]
    for index in range(count):
        directory = launch_sample_context(ctx, target, 'B', index)
        row = {'scenario': 'B', 'target': target, 'index': index, 'started_at': stamp(),
               'sample_dir': str(directory.relative_to(ctx.run)), 'failures': [],
               'reopen_delay_ms': round(delays[index % len(delays)] * 1000, 1)}
        first = second = recorder = None
        try:
            row['host_before'] = host_snapshot()
            row['host_guard'] = ctx.pressure_guard(directory, 'B')
            row['reset'] = ctx.reset_target(target, directory / 'reset')
            record = ctx.env_record(env_id)
            recorder = EventRecorder(directory, record['runtime']['container_name'])
            marker = f'{PREFIX}{target}-B-{index:03d}-first'
            first = ManagedLaunch(ctx, directory, target, marker)
            row['first_ready'] = first.ready()
            first.close_window()
            row['first_removed'] = first.wait_removed(timeout=20)
            target_delay = delays[index % len(delays)] * 1000
            elapsed = (time.monotonic_ns() - first.close_ns) / 1e6
            if elapsed < target_delay:
                time.sleep((target_delay - elapsed) / 1000)
            row['reopen_started_after_close_ms'] = round(
                (time.monotonic_ns() - first.close_ns) / 1e6, 3)
            row['state_at_reopen'] = ctx.env_state(env_id,
                                                   save_to=directory / 'state-at-reopen.json')
            marker2 = f'{PREFIX}{target}-B-{index:03d}-second'
            second = ManagedLaunch(ctx, directory, target, marker2)
            row['second_ready'] = second.ready()
            row['first_launcher_exit'] = first.wait_exit(timeout=60)
            row['first_finish'] = first.finish()
            row['second_process_chain'] = second.process_chain()
            row['second_pid1'] = ctx.pid1_status(env_id, directory / 'second-pid1')
            row['second_lease_open'] = ctx.lease_state(env_id)
            row['second_processes_open'] = ctx.container_ps(env_id,
                                                             directory / 'second-processes-open.txt')
            row['containers_during'] = ctx.env_containers(env_id)
            second.close_window()
            row['second_removed'] = second.wait_removed(timeout=20)
            row['second_launcher_exit'] = second.wait_exit(timeout=60)
            row['wait_stopped'] = ctx.wait_stopped(env_id, directory)
            row['wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
            row['env_after'] = ctx.env_state(env_id, save_to=directory / 'env-after.json')
            row['lease_after'] = ctx.lease_state(env_id)
            row['containers_after'] = ctx.env_containers(env_id)
            row['note_unchanged'] = (sha256_file(first.note) == first.note_sha256
                                     and sha256_file(second.note) == second.note_sha256)
            row['timings'] = {
                'first_launch_to_ready_ms': (row['first_ready'] or {}).get('launch_to_ready_ms'),
                'second_launch_to_ready_ms': (row['second_ready'] or {}).get('launch_to_ready_ms'),
                'close_to_first_launcher_exit_ms': elapsed_ms(
                    first.close_ns, (row['first_launcher_exit'] or {}).get('exit_ns')),
                'close_to_stopped_ms': (row.get('wait_stopped') or {}).get('elapsed_ms'),
            }
            row['single_container'] = (
                len(row['containers_during']) == 1
                and (row['containers_during'][0].get('id') or '') == ctx.container_id(env_id))
            row['container_id_unchanged'] = (ctx.container_id(env_id)
                                             == record['runtime']['container_id'])
            failures = row['failures']
            if not (row.get('first_ready') and row.get('second_ready')):
                failures.append('one of the reopen windows never reached readiness')
            if (row.get('first_launcher_exit') or {}).get('returncode') != 0:
                failures.append('first launcher exit status was not zero')
            if (row.get('second_launcher_exit') or {}).get('returncode') != 0:
                failures.append('second launcher exit status was not zero')
            if not row['single_container']:
                failures.append(f'expected one owned container during reopen, saw {row["containers_during"]}')
            if not row['container_id_unchanged']:
                failures.append('container id changed across the reopen')
            if not (row.get('wait_stopped') or {}).get('ok'):
                failures.append('environment did not stop after the reopened window closed')
            if (row.get('lease_after') or {}).get('live_count'):
                failures.append('leases remain after the reopened window closed')
            if not (row.get('wait_bridge') or {}).get('ok'):
                failures.append('bridge remained after the reopened window closed')
            if not row['note_unchanged']:
                failures.append('the opened documents changed on disk')
            reopen_ready = (row['second_ready'] or {}).get('launch_to_ready_ms') or 0
            if reopen_ready > 3000:
                failures.append(f'fast reopen took {reopen_ready} ms (threshold 3000 ms)')
        except Exception as exc:
            row['error'] = f'{type(exc).__name__}: {exc}'
            row['failures'].append('exception: ' + row['error'])
        finally:
            for item in (first, second):
                if item is not None:
                    row.setdefault('cleanup', []).append(item.cleanup())
                    try:
                        item.finish()
                    except Exception:
                        pass
            if recorder is not None:
                row['events_status_counts'] = recorder.status_counts()
                recorder.stop()
            row['passed'] = not row['failures']
            row['finished_at'] = stamp()
            save(directory / 'result.json', row)
            rows.append(row)
            print(json.dumps({'scenario': 'B', 'target': target, 'index': index,
                              'passed': row['passed'],
                              'reopen_ready_ms': (row.get('timings') or {}).get('second_launch_to_ready_ms'),
                              'failures': row['failures']}, ensure_ascii=False), flush=True)
    save(scenario_dir / 'scenario.json', {
        'scenario': 'B', 'target': target, 'requested': count,
        'rows': [summarize_row(row) for row in rows if row['target'] == target],
    })


def scenario_c(ctx: Context, target: str, count: int, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'C'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    row = {'scenario': 'C', 'target': target, 'index': 0, 'started_at': stamp(),
           'sample_dir': str(scenario_dir.relative_to(ctx.run)), 'failures': [],
           'planned_cycles': count}
    try:
        row['host_before'] = host_snapshot()
        row['host_guard'] = ctx.pressure_guard(scenario_dir, 'C')
        row['reset'] = ctx.reset_target(target, scenario_dir / 'reset')
        start = ctx.env_cli('env', 'start', env_id, timeout=180)
        row['manual_start'] = {'returncode': start.returncode, 'stdout': start.stdout.strip(),
                               'stderr': start.stderr.strip()}
        if start.returncode:
            raise RuntimeError(f'manual env start failed: {start.stderr[-1500:]}')
        row['env_started'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-started.json')
        cycles = []
        for index in range(count):
            directory = scenario_dir / f'cycle-{index:03d}'
            directory.mkdir(parents=True, exist_ok=True)
            entry = {'index': index, 'started_at': stamp()}
            launch = None
            try:
                marker = f'{PREFIX}{target}-C-{index:03d}'
                launch = ManagedLaunch(ctx, directory, target, marker)
                entry['ready'] = launch.ready()
                entry['lease_open'] = ctx.lease_state(env_id)
                entry['env_open'] = ctx.env_state(env_id)
                launch.close_window()
                entry['removed'] = launch.wait_removed(timeout=20)
                entry['launcher_exit'] = launch.wait_exit(timeout=60)
                entry['lease_after'] = ctx.lease_state(env_id)
                entry['env_after'] = ctx.env_state(env_id)
                entry['note_unchanged'] = sha256_file(launch.note) == launch.note_sha256
                running = ((entry['env_after'] or {}).get('inspect') or {}).get('running')
                entry['passed'] = (
                    (entry.get('launcher_exit') or {}).get('returncode') == 0
                    and entry.get('removed') is not None
                    and running is True
                    and entry['note_unchanged'])
            except Exception as exc:
                entry['error'] = f'{type(exc).__name__}: {exc}'
                entry['passed'] = False
            finally:
                if launch is not None:
                    entry['cleanup'] = launch.cleanup()
                    try:
                        launch.finish()
                    except Exception:
                        pass
                save(directory / 'result.json', entry)
                cycles.append(entry)
                print(json.dumps({'scenario': 'C', 'target': target, 'cycle': index,
                                  'passed': entry['passed'],
                                  'ready_ms': (entry.get('ready') or {}).get('launch_to_ready_ms'),
                                  'error': entry.get('error')}, ensure_ascii=False), flush=True)
        row['cycles'] = cycles
        row['cycles_passed'] = sum(1 for item in cycles if item['passed'])
        row['processes_after'] = ctx.container_ps(env_id, scenario_dir / 'processes-after-30.txt')
        time.sleep(0.3)
        row['processes_after_recheck'] = ctx.container_ps(
            env_id, scenario_dir / 'processes-after-30-recheck.txt')
        row['pid1_after'] = ctx.pid1_status(env_id, scenario_dir / 'pid1-after-30')
        row['env_after_cycles'] = ctx.env_state(env_id,
                                                save_to=scenario_dir / 'env-after-cycles.json')
        row['lease_after_cycles'] = ctx.lease_state(env_id)
        row['bridges_after_cycles'] = ctx.bridge_processes(env_id)
        stop = ctx.env_cli('env', 'stop', env_id, timeout=180)
        row['manual_stop'] = {'returncode': stop.returncode, 'stdout': stop.stdout.strip(),
                              'stderr': stop.stderr.strip()}
        row['wait_stopped'] = ctx.wait_stopped(env_id, scenario_dir / 'stop')
        row['bridge_after_stop_before_cleanup'] = ctx.bridge_processes(env_id)
        row['bridge_cleanup'] = [
            {'pid': item['pid'], 'term': kill_pid(item['pid'], signal.SIGTERM)}
            for item in row['bridge_after_stop_before_cleanup']]
        wait_for(lambda: not ctx.bridge_processes(env_id), 10, 0.05)
        row['wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['env_final'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-final.json')
        row['lease_final'] = ctx.lease_state(env_id)
        first_snapshot = row['processes_after']
        recheck = row['processes_after_recheck']
        row['zombie_count'] = recheck['zombie_count']
        row['pid1_child_count'] = recheck['child_count']
        row['zombie_count_first_snapshot'] = first_snapshot['zombie_count']
        failures = row['failures']
        if row['cycles_passed'] != count:
            failures.append(f'only {row["cycles_passed"]}/{count} warm cycles passed')
        if row['zombie_count']:
            failures.append(f'{row["zombie_count"]} zombie processes accumulated in the warm group')
        if not (row.get('wait_stopped') or {}).get('ok'):
            failures.append('environment did not stop after the warm group')
        if (row.get('lease_final') or {}).get('live_count'):
            failures.append('leases remain after the warm group')
        if recheck['returncode'] or first_snapshot['returncode']:
            failures.append('could not inspect the container process table')
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['failures'].append('exception: ' + row['error'])
        try:
            row['cleanup_stop'] = ctx.env_cli('env', 'stop', env_id, timeout=180).returncode
        except Exception:
            pass
    finally:
        row['passed'] = not row['failures']
        row['finished_at'] = stamp()
        save(scenario_dir / 'scenario.json', row)
        rows.append(row)
        print(json.dumps({'scenario': 'C', 'target': target, 'passed': row['passed'],
                          'zombies': row.get('zombie_count'),
                          'cycles_passed': row.get('cycles_passed'),
                          'failures': row['failures']}, ensure_ascii=False), flush=True)


def scenario_d(ctx: Context, target: str, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'D'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    row = {'scenario': 'D', 'target': target, 'index': 0, 'started_at': stamp(),
           'sample_dir': str(scenario_dir.relative_to(ctx.run)), 'failures': []}
    launch = recorder = recovery = None
    try:
        row['host_before'] = host_snapshot()
        row['host_guard'] = ctx.pressure_guard(scenario_dir, 'D')
        row['reset'] = ctx.reset_target(target, scenario_dir / 'reset')
        record = ctx.env_record(env_id)
        recorder = EventRecorder(scenario_dir, record['runtime']['container_name'])
        marker = f'{PREFIX}{target}-D-000'
        launch = ManagedLaunch(ctx, scenario_dir, target, marker)
        row['ready'] = launch.ready()
        row['process_chain_before_kill'] = launch.process_chain()
        row['pid1'] = ctx.pid1_status(env_id, scenario_dir / 'pid1')
        row['lease_before_kill'] = ctx.lease_state(env_id)
        row['window_before_kill'] = ctx.probe_window(marker, launch.window['window_id'],
                                                     scenario_dir / 'probe-before-kill')
        workers = (row['process_chain_before_kill'] or {}).get('worker_pids') or []
        row['worker_pids'] = workers
        row['lease_owner_is_worker'] = any(
            item.get('pid') in workers
            for item in (row['lease_before_kill'].get('live') or {}).values())
        row['kill_top'] = kill_pid(launch.launcher_pid, signal.SIGKILL)
        kill_ns = time.monotonic_ns()
        row['top_killed_at'] = stamp()
        time.sleep(1.5)
        ctx.observer.pump()
        row['top_dead'] = bool(wait_for(lambda: launch._exit_event.is_set(), 10))
        row['top_returncode'] = launch.returncode
        row['top_exit_after_kill_ms'] = elapsed_ms(kill_ns, launch.exit_ns)
        row['workers_alive_after_kill'] = [
            {'pid': pid, 'stamp': pid_stamp(pid), 'state': pid_state(pid)} for pid in workers]
        row['window_after_kill'] = ctx.probe_window(marker, launch.window['window_id'],
                                                    scenario_dir / 'probe-after-kill')
        row['env_after_kill'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-after-kill.json')
        row['lease_after_kill'] = ctx.lease_state(env_id)
        row['window_survived_kill'] = (
            len(row['window_after_kill']) == 1
            and (row['window_after_kill'][0].get('internalId') or '') == launch.window['window_id'])
        launch.close_window()
        row['removed_after_kill'] = launch.wait_removed(timeout=20)
        row['wait_stopped_after_kill'] = ctx.wait_stopped(env_id,
                                                          scenario_dir / 'stopped-after-kill')
        row['wait_bridge_after_kill'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['env_stopped_after_kill'] = ctx.env_state(
            env_id, save_to=scenario_dir / 'env-stopped-after-kill.json')
        row['lease_stopped_after_kill'] = ctx.lease_state(env_id)
        row['worker_exit_after_close'] = [
            {'pid': pid, 'stamp': pid_stamp(pid), 'state': pid_state(pid)} for pid in workers]
        row['auto_stop_after_kill'] = bool(
            ((row.get('env_stopped_after_kill') or {}).get('inspect') or {}).get('running') is False
            and (row.get('wait_stopped_after_kill') or {}).get('ok'))
        marker2 = f'{PREFIX}{target}-D-001-recovery'
        recovery = ManagedLaunch(ctx, scenario_dir / 'recovery', target, marker2)
        row['recovery_ready'] = recovery.ready()
        recovery.close_window()
        row['recovery_removed'] = recovery.wait_removed(timeout=20)
        row['recovery_launcher_exit'] = recovery.wait_exit(timeout=60)
        row['recovery_finish'] = recovery.finish()
        row['recovery_wait_stopped'] = ctx.wait_stopped(env_id, scenario_dir / 'recovery-stop')
        row['recovery_wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['recovery_env'] = ctx.env_state(env_id, save_to=scenario_dir / 'recovery-env.json')
        row['recovery_lease'] = ctx.lease_state(env_id)
        row['recovery_stopped'] = bool((row.get('recovery_wait_stopped') or {}).get('ok'))
        failures = row['failures']
        if not row['window_survived_kill']:
            failures.append('the window did not survive a SIGKILL of the top-level launcher')
        if not row.get('workers_alive_after_kill'):
            failures.append('no lease-holding worker process was found after the launcher was killed')
        if not row['auto_stop_after_kill']:
            failures.append('closing the surviving window did not stop the environment')
        if not (row.get('wait_bridge_after_kill') or {}).get('ok'):
            failures.append('bridge remained after the surviving window closed')
        if (row.get('lease_stopped_after_kill') or {}).get('live_count'):
            failures.append('leases remain after the surviving window closed')
        if not row['recovery_stopped']:
            failures.append('a following normal managed cycle did not auto-stop the environment')
        if (row.get('recovery_lease') or {}).get('live_count'):
            failures.append('leases remain after the recovery cycle')
        if not (row.get('recovery_wait_bridge') or {}).get('ok'):
            failures.append('bridge remained after the recovery cycle')
        if (row.get('recovery_launcher_exit') or {}).get('returncode') != 0:
            failures.append('recovery launcher exit status was not zero')
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['failures'].append('exception: ' + row['error'])
    finally:
        row.setdefault('cleanup', [])
        if launch is not None:
            row['cleanup'].append(launch.cleanup())
            try:
                launch.finish()
            except Exception:
                pass
        if recovery is not None:
            row['cleanup'].append(recovery.cleanup())
            try:
                recovery.finish()
            except Exception:
                pass
        if recorder is not None:
            row['events_status_counts'] = recorder.status_counts()
            recorder.stop()
        row['passed'] = not row['failures']
        row['finished_at'] = stamp()
        save(scenario_dir / 'scenario.json', row)
        rows.append(row)
        print(json.dumps({'scenario': 'D', 'target': target, 'passed': row['passed'],
                          'window_survived': row.get('window_survived_kill'),
                          'auto_stop_after_kill': row.get('auto_stop_after_kill'),
                          'recovery_stopped': row.get('recovery_stopped'),
                          'failures': row['failures']}, ensure_ascii=False), flush=True)


def scenario_e(ctx: Context, target: str, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'E'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    row = {'scenario': 'E', 'target': target, 'index': 0, 'started_at': stamp(),
           'sample_dir': str(scenario_dir.relative_to(ctx.run)), 'failures': []}
    launch = recovery = None
    try:
        row['host_before'] = host_snapshot()
        row['host_guard'] = ctx.pressure_guard(scenario_dir, 'E')
        row['reset'] = ctx.reset_target(target, scenario_dir / 'reset')
        marker = f'{PREFIX}{target}-E-000'
        launch = ManagedLaunch(ctx, scenario_dir, target, marker)
        row['ready'] = launch.ready()
        chain = launch.process_chain()
        row['process_chain'] = chain
        row['lease_before'] = ctx.lease_state(env_id)
        workers = chain.get('worker_pids') or []
        victims = [launch.launcher_pid] + workers
        row['victims'] = victims
        row['kills'] = [kill_pid(pid, signal.SIGKILL) for pid in victims]
        time.sleep(1.5)
        ctx.observer.pump()
        row['window_after_kill'] = ctx.probe_window(marker, launch.window['window_id'],
                                                    scenario_dir / 'probe-after-kill')
        row['window_survived_kill'] = (
            len(row['window_after_kill']) == 1
            and (row['window_after_kill'][0].get('internalId') or '') == launch.window['window_id'])
        row['env_after_kill'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-after-kill.json')
        row['lease_after_kill'] = ctx.lease_state(env_id)
        launch.close_window()
        row['removed_after_kill'] = launch.wait_removed(timeout=20)
        time.sleep(2.0)
        row['env_2s_after_close'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-2s.json')
        row['lease_2s_after_close'] = ctx.lease_state(env_id)
        row['bridge_2s_after_close'] = ctx.bridge_processes(env_id)
        marker2 = f'{PREFIX}{target}-E-001-recovery'
        recovery = ManagedLaunch(ctx, scenario_dir / 'recovery', target, marker2)
        row['recovery_ready'] = recovery.ready()
        row['recovery_lease_open'] = ctx.lease_state(env_id)
        recovery.close_window()
        row['recovery_removed'] = recovery.wait_removed(timeout=20)
        row['recovery_launcher_exit'] = recovery.wait_exit(timeout=60)
        row['recovery_finish'] = recovery.finish()
        row['recovery_wait_stopped'] = ctx.wait_stopped(env_id, scenario_dir / 'recovery-stop')
        row['recovery_wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['recovery_env'] = ctx.env_state(env_id, save_to=scenario_dir / 'recovery-env.json')
        row['recovery_lease'] = ctx.lease_state(env_id)
        row['recovery_stopped'] = bool((row.get('recovery_wait_stopped') or {}).get('ok'))
        failures = row['failures']
        if not row['window_survived_kill']:
            failures.append('the window did not survive the launcher/worker SIGKILL')
        if not row['recovery_stopped']:
            failures.append('the normal managed cycle after the abnormal kill did not stop the environment')
        if (row.get('recovery_lease') or {}).get('live_count'):
            failures.append('leases remain after the recovery cycle')
        if not (row.get('recovery_wait_bridge') or {}).get('ok'):
            failures.append('bridge remained after the recovery cycle')
        if (row.get('recovery_launcher_exit') or {}).get('returncode') != 0:
            failures.append('recovery launcher exit status was not zero')
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['failures'].append('exception: ' + row['error'])
    finally:
        row.setdefault('cleanup', [])
        if launch is not None:
            row['cleanup'].append(launch.cleanup())
            try:
                launch.finish()
            except Exception:
                pass
        if recovery is not None:
            row['cleanup'].append(recovery.cleanup())
            try:
                recovery.finish()
            except Exception:
                pass
        row['passed'] = not row['failures']
        row['finished_at'] = stamp()
        save(scenario_dir / 'scenario.json', row)
        rows.append(row)
        print(json.dumps({'scenario': 'E', 'target': target, 'passed': row['passed'],
                          'recovery_stopped': row.get('recovery_stopped'),
                          'failures': row['failures']}, ensure_ascii=False), flush=True)


def scenario_f(ctx: Context, target: str, rows: list) -> None:
    _two_launch_scenario(ctx, target, 'F', rows)


def scenario_g(ctx: Context, target: str, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'G'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    row = {'scenario': 'G', 'target': target, 'index': 0, 'started_at': stamp(),
           'sample_dir': str(scenario_dir.relative_to(ctx.run)), 'failures': []}
    launch = None
    try:
        row['host_before'] = host_snapshot()
        row['host_guard'] = ctx.pressure_guard(scenario_dir, 'G')
        row['reset'] = ctx.reset_target(target, scenario_dir / 'reset')
        start = ctx.env_cli('env', 'start', env_id, timeout=180)
        row['manual_start'] = {'returncode': start.returncode, 'stdout': start.stdout.strip(),
                               'stderr': start.stderr.strip()}
        if start.returncode:
            raise RuntimeError('manual pre-start failed')
        row['env_started'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-started.json')
        marker = f'{PREFIX}{target}-G-000'
        launch = ManagedLaunch(ctx, scenario_dir, target, marker)
        row['ready'] = launch.ready()
        row['lease_open'] = ctx.lease_state(env_id)
        launch.close_window()
        row['removed'] = launch.wait_removed(timeout=20)
        row['launcher_exit'] = launch.wait_exit(timeout=60)
        row['finish'] = launch.finish()
        time.sleep(1.0)
        row['env_after'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-after.json')
        row['lease_after'] = ctx.lease_state(env_id)
        row['bridges_after'] = ctx.bridge_processes(env_id)
        row['retained'] = bool(((row.get('env_after') or {}).get('inspect') or {}).get('running'))
        stop = ctx.env_cli('env', 'stop', env_id, timeout=180)
        row['manual_stop'] = {'returncode': stop.returncode, 'stdout': stop.stdout.strip(),
                              'stderr': stop.stderr.strip()}
        row['wait_stopped'] = ctx.wait_stopped(env_id, scenario_dir / 'stop')
        row['wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['env_final'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-final.json')
        row['lease_final'] = ctx.lease_state(env_id)
        failures = row['failures']
        if not row['retained']:
            failures.append('a manually pre-started environment was stopped by the managed close')
        if (row.get('launcher_exit') or {}).get('returncode') != 0:
            failures.append('launcher exit status was not zero')
        if (row.get('lease_after') or {}).get('live_count'):
            failures.append('leases remain after a manually retained cycle')
        if not (row.get('wait_stopped') or {}).get('ok'):
            failures.append('manual stop after the retained cycle did not finish')
        if (row.get('lease_final') or {}).get('live_count'):
            failures.append('leases remain after the final manual stop')
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['failures'].append('exception: ' + row['error'])
    finally:
        if launch is not None:
            row['cleanup'] = launch.cleanup()
            try:
                launch.finish()
            except Exception:
                pass
        row['passed'] = not row['failures']
        row['finished_at'] = stamp()
        save(scenario_dir / 'scenario.json', row)
        rows.append(row)
        print(json.dumps({'scenario': 'G', 'target': target, 'passed': row['passed'],
                          'retained': row.get('retained'),
                          'failures': row['failures']}, ensure_ascii=False), flush=True)


def scenario_h(ctx: Context, target: str, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / 'H'
    scenario_dir.mkdir(parents=True, exist_ok=True)
    row = {'scenario': 'H', 'target': target, 'index': 0, 'started_at': stamp(),
           'sample_dir': str(scenario_dir.relative_to(ctx.run)), 'failures': []}
    launches = []
    recorder = None
    try:
        row['host_before'] = host_snapshot()
        row['host_guard'] = ctx.pressure_guard(scenario_dir, 'H')
        row['reset'] = ctx.reset_target(target, scenario_dir / 'reset')
        record = ctx.env_record(env_id)
        container_id = record['runtime']['container_id']
        row['container_id_before'] = container_id
        row['containers_before'] = ctx.env_containers(env_id)
        recorder = EventRecorder(scenario_dir, record['runtime']['container_name'])
        row['environment_stopped_before'] = ctx.env_state(
            env_id, save_to=scenario_dir / 'env-before.json')
        for index in range(3):
            launches.append(ManagedLaunch(ctx, scenario_dir / f'launch-{index}', target,
                                          f'{PREFIX}{target}-H-{index:03d}'))
        row['ready'] = [item.ready() for item in launches]
        row['process_chains'] = [item.process_chain() for item in launches]
        row['lease_during'] = ctx.lease_state(env_id)
        row['containers_during'] = ctx.env_containers(env_id)
        row['bridges_during'] = ctx.bridge_processes(env_id)
        row['container_id_during'] = (row['containers_during'][0].get('id')
                                      if row['containers_during'] else None)
        row['single_container'] = (
            len(row['containers_during']) == 1
            and row['container_id_during'] == container_id)
        row['three_leases'] = row['lease_during']['live_count'] == 3
        for index, item in enumerate(launches):
            item.close_window()
            row[f'removed_{index}'] = item.wait_removed(timeout=20)
            row[f'launcher_exit_{index}'] = item.wait_exit(timeout=60)
            row[f'finish_{index}'] = item.finish()
        row['wait_stopped'] = ctx.wait_stopped(env_id, scenario_dir / 'final-stop')
        row['wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['env_final'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-final.json')
        row['lease_final'] = ctx.lease_state(env_id)
        row['containers_after'] = ctx.env_containers(env_id)
        events = recorder.status_counts()
        row['events_status_counts'] = events
        row['events_created'] = events.get('create', 0)
        row['events_started'] = events.get('start', 0) + events.get('Started', 0)
        row['events_init'] = events.get('init', 0)
        row['auto_stop_after_last'] = bool((row.get('wait_stopped') or {}).get('ok'))
        failures = row['failures']
        if not all(item.get('launch_to_ready_ms') is not None for item in row['ready']):
            failures.append('one of the three concurrent first opens never reached readiness')
        if not row['single_container']:
            failures.append('concurrent first opens did not share exactly one container')
        if row['events_created']:
            failures.append(f'{row["events_created"]} extra container create events during concurrent first start')
        if row['events_started'] > 1:
            failures.append(f'{row["events_started"]} container start events for three concurrent first opens')
        if not row['three_leases']:
            failures.append(f'expected three live leases, saw {row["lease_during"]["live_count"]}')
        if not row['auto_stop_after_last']:
            failures.append('environment did not stop after all three windows closed')
        if (row.get('lease_final') or {}).get('live_count'):
            failures.append('leases remain after all three windows closed')
        if not (row.get('wait_bridge') or {}).get('ok'):
            failures.append('bridge remained after all three windows closed')
        for index in range(3):
            if (row.get(f'launcher_exit_{index}') or {}).get('returncode') != 0:
                failures.append(f'concurrent launcher {index} exited with a non-zero status')
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['failures'].append('exception: ' + row['error'])
    finally:
        for item in launches:
            row.setdefault('cleanup', []).append(item.cleanup())
            try:
                item.finish()
            except Exception:
                pass
        if recorder is not None:
            recorder.stop()
        row['passed'] = not row['failures']
        row['finished_at'] = stamp()
        save(scenario_dir / 'scenario.json', row)
        rows.append(row)
        print(json.dumps({'scenario': 'H', 'target': target, 'passed': row['passed'],
                          'single_container': row.get('single_container'),
                          'events_created': row.get('events_created'),
                          'events_started': row.get('events_started'),
                          'failures': row['failures']}, ensure_ascii=False), flush=True)


def _two_launch_scenario(ctx: Context, target: str, scenario: str, rows: list) -> None:
    env_id, _ = ctx.ids(target)
    scenario_dir = ctx.run / 'samples' / target / scenario
    scenario_dir.mkdir(parents=True, exist_ok=True)
    row = {'scenario': scenario, 'target': target, 'index': 0, 'started_at': stamp(),
           'sample_dir': str(scenario_dir.relative_to(ctx.run)), 'failures': []}
    launches = []
    recorder = None
    try:
        row['host_before'] = host_snapshot()
        row['host_guard'] = ctx.pressure_guard(scenario_dir, scenario)
        row['reset'] = ctx.reset_target(target, scenario_dir / 'reset')
        record = ctx.env_record(env_id)
        recorder = EventRecorder(scenario_dir, record['runtime']['container_name'])
        first = ManagedLaunch(ctx, scenario_dir / 'first', target,
                              f'{PREFIX}{target}-{scenario}-000-first')
        launches.append(first)
        row['first_ready'] = first.ready()
        second = ManagedLaunch(ctx, scenario_dir / 'second', target,
                               f'{PREFIX}{target}-{scenario}-001-second')
        launches.append(second)
        row['second_ready'] = second.ready()
        row['lease_open'] = ctx.lease_state(env_id)
        row['containers_during'] = ctx.env_containers(env_id)
        row['bridges_during'] = ctx.bridge_processes(env_id)
        row['container_id'] = ctx.container_id(env_id)
        row['single_container'] = (
            len(row['containers_during']) == 1
            and (row['containers_during'][0].get('id') or '') == row['container_id'])
        row['lease_two'] = row['lease_open']['live_count'] == 2
        first.close_window()
        row['first_removed'] = first.wait_removed(timeout=20)
        row['first_launcher_exit'] = first.wait_exit(timeout=60)
        row['first_finish'] = first.finish()
        time.sleep(1.0)
        row['env_after_first_close'] = ctx.env_state(
            env_id, save_to=scenario_dir / 'env-after-first-close.json')
        row['lease_after_first_close'] = ctx.lease_state(env_id)
        row['bridge_after_first_close'] = ctx.bridge_processes(env_id)
        row['running_after_first_close'] = bool(
            ((row.get('env_after_first_close') or {}).get('inspect') or {}).get('running'))
        second.close_window()
        row['second_removed'] = second.wait_removed(timeout=20)
        row['second_launcher_exit'] = second.wait_exit(timeout=60)
        row['second_finish'] = second.finish()
        row['wait_stopped'] = ctx.wait_stopped(env_id, scenario_dir / 'final-stop')
        row['wait_bridge'] = ctx.wait_bridge_gone(env_id, timeout=20)
        row['env_final'] = ctx.env_state(env_id, save_to=scenario_dir / 'env-final.json')
        row['lease_final'] = ctx.lease_state(env_id)
        row['containers_after'] = ctx.env_containers(env_id)
        row['auto_stop_after_last'] = bool((row.get('wait_stopped') or {}).get('ok'))
        row['one_container_after'] = len(row['containers_after']) == 1
        failures = row['failures']
        if not (row.get('first_ready') and row.get('second_ready')):
            failures.append('one of the two windows never reached readiness')
        if not row['single_container']:
            failures.append(f'expected exactly one owned container, saw {row["containers_during"]}')
        if not row['lease_two']:
            failures.append(f'expected two live leases, saw {row["lease_open"]}')
        if not row['running_after_first_close']:
            failures.append('environment stopped when only one of two windows closed')
        if (row.get('lease_after_first_close') or {}).get('live_count') != 1:
            failures.append('expected one live lease after the first close')
        if not row['auto_stop_after_last']:
            failures.append('environment did not stop after the last window closed')
        if (row.get('lease_final') or {}).get('live_count'):
            failures.append('leases remain after the last window closed')
        if not (row.get('wait_bridge') or {}).get('ok'):
            failures.append('bridge remained after the last window closed')
        for item in (row.get('first_launcher_exit'), row.get('second_launcher_exit')):
            if (item or {}).get('returncode') != 0:
                failures.append('a concurrent launcher exited with a non-zero status')
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['failures'].append('exception: ' + row['error'])
    finally:
        for item in launches:
            row.setdefault('cleanup', []).append(item.cleanup())
            try:
                item.finish()
            except Exception:
                pass
        if recorder is not None:
            row['events_status_counts'] = recorder.status_counts()
            recorder.stop()
        row['passed'] = not row['failures']
        row['finished_at'] = stamp()
        save(scenario_dir / 'scenario.json', row)
        rows.append(row)
        print(json.dumps({'scenario': scenario, 'target': target, 'passed': row['passed'],
                          'lease_two': row.get('lease_two'),
                          'auto_stop_after_last': row.get('auto_stop_after_last'),
                          'failures': row['failures']}, ensure_ascii=False), flush=True)


def summarize_row(row: dict) -> dict:
    keep = ('scenario', 'target', 'index', 'passed', 'failures', 'error', 'timings',
            'container_exit_code', 'event_exit_code', 'zombie_count', 'pid1_child_count',
            'retained', 'auto_stop_after_last', 'single_container', 'lease_two',
            'window_survived_kill', 'auto_stop_after_kill', 'recovery_stopped',
            'cycles_passed', 'planned_cycles', 'reopen_delay_ms', 'events_created',
            'events_started', 'running_after_first_close', 'three_leases')
    return {key: row.get(key) for key in keep if key in row}


# ------------------------------------------------------------- summary/report
def collect_rows(run: Path) -> list:
    rows = []
    for path in sorted((run / 'samples').rglob('result.json')):
        try:
            rows.append(json.loads(path.read_text(encoding='utf-8')))
        except (OSError, json.JSONDecodeError):
            continue
    return rows


def collect_scenarios(run: Path) -> dict:
    found = {}
    for path in sorted((run / 'samples').rglob('scenario.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        scenario = payload.get('scenario')
        target = payload.get('target')
        if scenario and target:
            found[(scenario, target)] = payload
    return found


def build_summary(ctx: Context, rows: list, scenarios: dict) -> dict:
    summary = {
        'schema': 'multilinux.lifecycle.summary.v1',
        'generated_at': stamp(),
        'run_id': ctx.run.name,
        'endpoint': 'native compositor window plus first associated non-null Wayland buffer commit',
        'physical_presentation_measured': False,
        'sessions': 'serial; F and H intentionally use two/three windows of one environment',
        'targets': {},
        'conditions': {},
    }
    host_snapshots = [row['host_before'] for row in rows if isinstance(row.get('host_before'), dict)]
    if host_snapshots:
        available = [item['meminfo']['MemAvailable_bytes'] for item in host_snapshots
                     if item['meminfo'].get('MemAvailable_bytes') is not None]
        summary['conditions']['mem_available_min_bytes'] = min(available) if available else None
        summary['conditions']['memory_some_avg10_max'] = max(
            ((item['pressure'].get('memory.some') or {}).get('avg10', 0)
             for item in host_snapshots), default=None)
        summary['conditions']['memory_full_avg10_max'] = max(
            ((item['pressure'].get('memory.full') or {}).get('avg10', 0)
             for item in host_snapshots), default=None)
        loads = [item['load_average'][0] for item in host_snapshots
                 if isinstance(item.get('load_average'), list) and item['load_average']]
        summary['conditions']['load_1min_max'] = max(loads) if loads else None
        summary['conditions']['host_snapshots'] = len(host_snapshots)
    for target in sorted(ctx.targets):
        target_summary = {}
        a_rows = [row for row in rows if row['scenario'] == 'A' and row['target'] == target]
        if a_rows:
            valid = [row for row in a_rows if row['passed']]
            target_summary['A'] = {
                'attempts': len(a_rows), 'passed': len(valid),
                'failures': len(a_rows) - len(valid),
                'metrics_ms': {
                    key: stats([(row.get('timings') or {}).get(key) for row in valid])
                    for key in ('launch_to_ready_ms', 'close_to_window_removed_ms',
                                'close_to_launcher_exit_ms', 'close_to_container_died_ms',
                                'close_to_stopped_ms', 'close_to_bridge_gone_ms')},
                'exit_codes': {},
                'pid1_names': {},
                'init_flags': {},
            }
            for row in a_rows:
                code = row.get('container_exit_code')
                target_summary['A']['exit_codes'][str(code)] = (
                    target_summary['A']['exit_codes'].get(str(code), 0) + 1)
                pid1 = row.get('pid1') or {}
                name = pid1.get('name') or 'untracked'
                target_summary['A']['pid1_names'][name] = (
                    target_summary['A']['pid1_names'].get(name, 0) + 1)
                init = str(row.get('container_init'))
                target_summary['A']['init_flags'][init] = (
                    target_summary['A']['init_flags'].get(init, 0) + 1)
            target_summary['A']['failures_detail'] = [
                {'index': row['index'], 'failures': row['failures']}
                for row in a_rows if not row['passed']]
        b_rows = [row for row in rows if row['scenario'] == 'B' and row['target'] == target]
        if b_rows:
            valid = [row for row in b_rows if row['passed']]
            target_summary['B'] = {
                'attempts': len(b_rows), 'passed': len(valid),
                'reopen_ready_ms': stats([(row.get('timings') or {}).get('second_launch_to_ready_ms')
                                          for row in valid]),
                'reopen_delay_ms': [row.get('reopen_delay_ms') for row in b_rows],
                'single_container': all(row.get('single_container') for row in b_rows),
                'failures_detail': [{'index': row['index'], 'failures': row['failures']}
                                    for row in b_rows if not row['passed']],
            }
        c_scenario = scenarios.get(('C', target))
        if c_scenario:
            row = c_scenario
            target_summary['C'] = {
                'cycles': row.get('planned_cycles'),
                'cycles_passed': row.get('cycles_passed'),
                'zombie_count': row.get('zombie_count'),
                'pid1_child_count': row.get('pid1_child_count'),
                'processes_after_path': (str((ctx.run / row['sample_dir']
                                             / 'processes-after-30.txt').relative_to(ctx.run))),
                'passed': row.get('passed'), 'failures': row.get('failures'),
            }
        for scenario in ('D', 'E', 'F', 'G', 'H'):
            payload = scenarios.get((scenario, target))
            if payload:
                target_summary[scenario] = summarize_row(payload)
        summary['targets'][target] = target_summary
    summary['scenarios'] = {}
    for (scenario, target), payload in scenarios.items():
        summary['scenarios'].setdefault(scenario, {})[target] = {
            'passed': payload.get('passed'),
            'failures': payload.get('failures'),
        }
    cleanup_path = ctx.run / 'postconditions.json'
    summary['cleanup'] = (json.loads(cleanup_path.read_text(encoding='utf-8'))
                          if cleanup_path.is_file() else None)
    return summary


def build_validation(ctx: Context, summary: dict, rows: list, scenarios: dict) -> dict:
    targets = sorted(ctx.targets)
    probe_counts = (ctx.manifest.get('probe') or {}).get('counts') or {}
    expected_a = (probe_counts.get('A') or 10) * len(targets)
    expected_b = (probe_counts.get('B') or 3) * len(targets)
    expected_c = (probe_counts.get('C') or 30)  # cycles per target, scenario rows are per target
    failures = []
    criteria = {}

    def record(name: str, passed: bool, detail: dict) -> None:
        criteria[name] = {'passed': bool(passed), 'detail': detail}
        if not passed:
            failures.append(name)

    a_rows = [row for row in rows if row['scenario'] == 'A']
    a_ok = (len(a_rows) == expected_a and all(row['passed'] for row in a_rows))
    stop_values = [(row.get('timings') or {}).get('close_to_stopped_ms') for row in a_rows
                   if row['passed']]
    died_values = [(row.get('timings') or {}).get('close_to_container_died_ms') for row in a_rows
                   if row['passed']]
    stop_stats = stats(stop_values)
    died_stats = stats(died_values)
    exit_codes = {}
    pid1_names = {}
    init_flags = {}
    for row in a_rows:
        exit_codes[str(row.get('container_exit_code'))] = (
            exit_codes.get(str(row.get('container_exit_code')), 0) + 1)
        pid1_names[(row.get('pid1') or {}).get('name') or 'untracked'] = (
            pid1_names.get((row.get('pid1') or {}).get('name') or 'untracked', 0) + 1)
        init_flags[str(row.get('container_init'))] = init_flags.get(
            str(row.get('container_init')), 0) + 1
    record('a_all_samples_pass', a_ok,
           {'attempts': len(a_rows), 'passed': sum(1 for row in a_rows if row['passed']),
            'expected': expected_a, 'stop_ms': stop_stats, 'died_event_ms': died_stats,
            'exit_codes': exit_codes, 'pid1_names': pid1_names, 'init_flags': init_flags,
            'failures_detail': [{'target': row['target'], 'index': row['index'],
                                 'failures': row['failures']} for row in a_rows if not row['passed']]})
    record('a_agile_stop', bool(stop_stats and stop_stats['p50'] <= 1500),
           {'p50_ms': (stop_stats or {}).get('p50'), 'p95_ms': (stop_stats or {}).get('p95'),
            'threshold_ms': 1500, 'old_defect_reference_ms': 8372})
    record('a_no_stop_timeout_137',
           not any(row.get('container_exit_code') == 137 for row in a_rows),
           {'exit_137_count': sum(1 for row in a_rows
                                  if row.get('container_exit_code') == 137),
            'exit_codes': exit_codes})
    b_rows = [row for row in rows if row['scenario'] == 'B']
    b_ready = stats([(row.get('timings') or {}).get('second_launch_to_ready_ms')
                     for row in b_rows if row['passed']])
    record('b_fast_reopen',
           bool(len(b_rows) == expected_b and all(row['passed'] for row in b_rows)
                and b_ready and b_ready['max'] <= 3000),
           {'attempts': len(b_rows), 'passed': sum(1 for row in b_rows if row['passed']),
            'expected': expected_b, 'reopen_ready_ms': b_ready, 'threshold_ms': 3000,
            'old_defect_reference_ms': 8942,
            'failures_detail': [{'target': row['target'], 'index': row['index'],
                                 'failures': row['failures']} for row in b_rows if not row['passed']]})
    c_rows = [scenarios[key] for key in scenarios if key[0] == 'C']
    zombie_total = sum(row.get('zombie_count') or 0 for row in c_rows)
    c_ok = (len(c_rows) == len(targets) and all(row.get('passed') for row in c_rows)
            and zombie_total == 0)
    record('c_zero_zombies', c_ok,
           {'targets': len(c_rows), 'zombie_total': zombie_total,
            'cycles_per_target': expected_c,
            'per_target': {row['target']: {'zombies': row.get('zombie_count'),
                                           'children_of_pid1': row.get('pid1_child_count'),
                                           'cycles_passed': row.get('cycles_passed'),
                                           'planned': row.get('planned_cycles'),
                                           'failures': row.get('failures')}
                           for row in c_rows}})
    for scenario in ('D', 'E', 'F', 'G', 'H'):
        selected = [scenarios[key] for key in scenarios if key[0] == scenario]
        passed = len(selected) == len(targets) and all(row['passed'] for row in selected)
        detail = {'attempts': len(selected), 'expected': len(targets),
                  'passed_count': sum(1 for row in selected if row['passed']),
                  'per_target': {row['target']: {'passed': row['passed'],
                                                 'failures': row['failures']}
                                 for row in selected}}
        if scenario == 'D':
            detail['window_survived_kill'] = {row['target']: row.get('window_survived_kill')
                                              for row in selected}
            detail['auto_stop_after_kill'] = {row['target']: row.get('auto_stop_after_kill')
                                              for row in selected}
        if scenario == 'E':
            detail['recovery_stopped'] = {row['target']: row.get('recovery_stopped')
                                          for row in selected}
        if scenario == 'F':
            detail['lease_two'] = {row['target']: row.get('lease_two') for row in selected}
            detail['running_after_first_close'] = {
                row['target']: row.get('running_after_first_close') for row in selected}
        if scenario == 'G':
            detail['retained'] = {row['target']: row.get('retained') for row in selected}
        if scenario == 'H':
            detail['single_container'] = {row['target']: row.get('single_container')
                                          for row in selected}
            detail['events_created'] = {row['target']: row.get('events_created')
                                        for row in selected}
            detail['events_started'] = {row['target']: row.get('events_started')
                                        for row in selected}
        record(f'{scenario.lower()}_scenario_pass', passed, detail)
    post = summary.get('cleanup') or {}
    record('cleanup_no_residue', bool(post.get('all_clean')), post)
    measurement_valid = bool(
        len(a_rows) == expected_a and len(b_rows) == expected_b
        and len(c_rows) == len(targets)
        and all(len([key for key in scenarios if key[0] == scenario]) == len(targets)
                for scenario in ('D', 'E', 'F', 'G', 'H')))
    product_verdict = ('lifecycle-fixed' if (measurement_valid and not failures)
                       else 'needs-lifecycle-fixes')
    return {
        'schema': 'multilinux.lifecycle.validation.v1',
        'generated_at': stamp(),
        'measurement_valid': measurement_valid,
        'product_verdict': product_verdict,
        'criteria': criteria,
        'failures': failures,
        'limitations': [
            'Endpoint is a native compositor window plus a non-empty buffer commit; physical display presentation is not measured.',
            'Single host: Fedora 44 KDE, KWin 6.7.5, rootless Podman; no frequency locking on a shared daily-use machine.',
            'Scenarios F/H intentionally run two/three windows of one environment at once; all other GUI work is serial.',
            'Container images and package inventories were frozen from the preceding startup round; no network or image refresh was used.',
            'Lifecycle sampling counts are 10 cold cycles, 3 fast reopens and 30 warm cycles per target, plus one pass per targeted scenario.',
        ],
    }


def format_ms(value):
    if value is None:
        return '—'
    if isinstance(value, (int, float)):
        return f'{value:.1f}'
    return str(value)


def render_report(ctx: Context, summary: dict, validation: dict) -> str:
    def metric(target, scenario, key):
        item = ((summary['targets'].get(target) or {}).get(scenario) or {})
        return format_ms(((item.get('metrics_ms') or {}).get(key) or {}).get('p50'))

    def metric_p95(target, scenario, key):
        item = ((summary['targets'].get(target) or {}).get(scenario) or {})
        return format_ms(((item.get('metrics_ms') or {}).get(key) or {}).get('p95'))

    manifest = ctx.manifest
    snapshot = manifest.get('snapshot') or {}
    git = snapshot.get('git') or {}
    targets = sorted(summary['targets'])
    lines = []
    add = lines.append
    add('# 生命周期真机验证（停止 / 快速重开 / 僵尸 / 所有权 / 并发租约）')
    add('')
    add(f"运行目录：{ctx.run.name}，生成时间 {stamp()}。宿主 Fedora 44 KDE / KWin 6.7.5，"
        f"rootless Podman 5.8.9，基线提交 {git.get('head', 'unknown')}"
        f"（分支 {git.get('branch', 'unknown')}，工作树{'脏' if git.get('dirty') else '干净'}）。"
        '本轮只新增测量工具与新证据目录；产品代码由 project/ 冻结快照决定，未改写已有证据。')
    add('')
    verdict = validation.get('product_verdict')
    add('**判定：' + ('lifecycle-fixed，生命周期修复在该场景集上成立。'
                     if verdict == 'lifecycle-fixed'
                     else 'needs-lifecycle-fixes，仍存在失败项，详见下文。') + '**')
    add('')
    add(f"机器核验：measurement_valid={str(validation.get('measurement_valid')).lower()}，"
        f"product_verdict={verdict}。判定标准见 validation.json；"
        '不能把仅离线单测通过当成真机生命周期通过。')
    add('')
    add('## 方法与环境')
    add('')
    add('- 两个全新独立环境：test-debian（Debian 13）与 test-arch（Arch Linux），'
        '由上一轮 startup-* 本地镜像摘要创建，--network none --desktop --portals --userns keep-id；'
        '各自独立 work 目录。镜像摘要与 SHA-256 见 manifest.json。')
    add('- 应用：每个环境一个 on-demand Mousepad（as_host_user、GDK_BACKEND=wayland、'
        'WAYLAND_DEBUG=client、LC_ALL=C.UTF-8），窗口标题含每样本唯一 marker。')
    add('- 就绪口径：KWin windowAdded 的原生 Wayland toplevel + 客户端非空 buffer commit；'
        '不是物理屏幕呈现。全部场景串行执行；只有 F/H 按需求并发 2/3 个窗口。')
    add('- 停止时长：关窗请求 → Podman died 事件（timeNano）；'
        '另记录启动器退出、环境记录 stopped、桥接消失与 PID 1 信号处置。')
    add('')
    conditions = summary.get('conditions') or {}
    add('## 宿主条件')
    add('')
    add(f"- 可用内存最小值：{format_ms((conditions.get('mem_available_min_bytes') or 0) / 1048576)} MiB；"
        f"内存 PSI some avg10 最大 {conditions.get('memory_some_avg10_max')}%，"
        f"full avg10 最大 {conditions.get('memory_full_avg10_max')}%；"
        f"1 分钟 load 最大 {conditions.get('load_1min_max')}。")
    add('- 每个样本的原始宿主快照在 samples/<target>/<scenario>/*/result.json 的 host_before。'
        '宿主压力超过阈值时脚本会记录并等待，而不是继续加窗。')
    add('')
    add('## A. 冷启动 → 关窗自动停止（每目标 10 次）')
    add('')
    add('| 环境 | 就绪 p50 | 就绪 p95 | 关窗→移除 p50 | 关窗→启动器退出 p50 | 关窗→容器 died p50 | 关窗→记录 stopped p50 |')
    add('|---|---:|---:|---:|---:|---:|---:|')
    for target in targets:
        a = summary['targets'][target].get('A') or {}
        if not a:
            continue
        add(f"| {target} | {metric(target, 'A', 'launch_to_ready_ms')} | "
            f"{metric_p95(target, 'A', 'launch_to_ready_ms')} | "
            f"{metric(target, 'A', 'close_to_window_removed_ms')} | "
            f"{metric(target, 'A', 'close_to_launcher_exit_ms')} | "
            f"{metric(target, 'A', 'close_to_container_died_ms')} | "
            f"{metric(target, 'A', 'close_to_stopped_ms')} |")
    add('')
    for target in targets:
        a = summary['targets'][target].get('A') or {}
        if a:
            add(f"- {target}：{a.get('passed')}/{a.get('attempts')} 通过；"
                f"容器退出码分布 {a.get('exit_codes')}；PID 1 名称 {a.get('pid1_names')}；"
                f"HostConfig.Init {a.get('init_flags')}。")
    add('')
    add('## B. 关窗后 150–250 ms 快速重开（每目标 3 次）')
    add('')
    for target in targets:
        b = summary['targets'][target].get('B') or {}
        if b:
            add(f"- {target}：{b.get('passed')}/{b.get('attempts')} 通过；"
                f"重开就绪 p50/p95/max = "
                f"{format_ms((b.get('reopen_ready_ms') or {}).get('p50'))}/"
                f"{format_ms((b.get('reopen_ready_ms') or {}).get('p95'))}/"
                f"{format_ms((b.get('reopen_ready_ms') or {}).get('max'))} ms"
                f"（阈值 3000 ms，旧缺陷约 8.9 s）；单容器 = {b.get('single_container')}。")
    add('')
    add('## C. 热组 30 次开关后僵尸统计')
    add('')
    for target in targets:
        c = summary['targets'][target].get('C') or {}
        if c:
            add(f"- {target}：{c.get('cycles_passed')}/{c.get('cycles')} 热组通过；"
                f"僵尸进程 {c.get('zombie_count')} 个，PID 1 直接子进程 {c.get('pid1_child_count')} 个；"
                f"原始进程表 {c.get('processes_after_path')}。")
    add('')
    add('## D. 只 SIGKILL 顶层启动器')
    add('')
    for target in targets:
        d = summary['targets'][target].get('D') or {}
        if d:
            add(f"- {target}：窗口存活 = {d.get('window_survived_kill')}，"
                f"关窗后自动停止 = {d.get('auto_stop_after_kill')}，"
                f"随后正常受管循环恢复自动停止 = {d.get('recovery_stopped')}；"
                f"{'通过' if d.get('passed') else '失败：' + str(d.get('failures'))}")
    add('')
    add('## E. 同时 SIGKILL 启动器与 worker')
    add('')
    for target in targets:
        e = summary['targets'][target].get('E') or {}
        if e:
            add(f"- {target}：窗口存活 = {e.get('window_survived_kill')}；"
                '异常关窗后环境未自动停属可接受行为（记录于样本 JSON），'
                f"异常后的正常受管循环最终自动停止 = {e.get('recovery_stopped')}；"
                f"{'通过' if e.get('passed') else '失败：' + str(e.get('failures'))}")
    add('')
    add('## F. 双窗口并发租约')
    add('')
    for target in targets:
        f = summary['targets'][target].get('F') or {}
        if f:
            add(f"- {target}：单容器 = {f.get('single_container')}，lease=2 = {f.get('lease_two')}，"
                f"关一个后仍运行 = {f.get('running_after_first_close')}，"
                f"关最后一个自动停止 = {f.get('auto_stop_after_last')}；"
                f"{'通过' if f.get('passed') else '失败：' + str(f.get('failures'))}")
    add('')
    add('## G. 手动预启动保留')
    add('')
    for target in targets:
        g = summary['targets'][target].get('G') or {}
        if g:
            add(f"- {target}：受管关窗后环境保持 running = {g.get('retained')}；"
                f"{'通过' if g.get('passed') else '失败：' + str(g.get('failures'))}")
    add('')
    add('## H. 停止态并发 3 次首启')
    add('')
    for target in targets:
        h = summary['targets'][target].get('H') or {}
        if h:
            add(f"- {target}：单容器 = {h.get('single_container')}，"
                f"场景内 create 事件 = {h.get('events_created')}，start 事件 = {h.get('events_started')}，"
                f"三窗口全部关闭后自动停止 = {h.get('auto_stop_after_last')}；"
                f"{'通过' if h.get('passed') else '失败：' + str(h.get('failures'))}")
    add('')
    add('## 失败项')
    add('')
    if validation.get('failures'):
        for name in validation['failures']:
            detail = (validation['criteria'].get(name) or {}).get('detail') or {}
            add(f"- {name}：{json.dumps(detail, ensure_ascii=False)[:1600]}")
    else:
        add('无。所有预设判定项通过。')
    add('')
    add('## 清理与残留核对')
    add('')
    post = summary.get('cleanup') or {}
    add(f"- all_clean={str(post.get('all_clean')).lower()}："
        f"运行中容器 {post.get('running_containers')} 个，存活租约 {post.get('live_leases')} 个，"
        f"桥接进程 {post.get('bridge_processes')} 个，遗留窗口 {post.get('remaining_windows')} 个，"
        f"遗留启动器/worker 进程 {post.get('leftover_processes')} 个。")
    add('- 两个自建环境保留为停止状态以便复现；未删除环境记录、未触碰任何既有容器/环境/证据。')
    add('')
    add('## 限制')
    add('')
    for item in validation.get('limitations') or []:
        add(f'- {item}')
    add('')
    add('## 证据与复现')
    add('')
    add('- 冻结源码与配置：project/（逐文件 SHA-256 与 git 状态见 manifest.json 的 snapshot）。')
    add('- 原始样本：samples/<target>/<A-H>/*/result.json、stdout.log、stderr.log、'
        'pid1-*.txt、processes-*.txt、events.jsonl。')
    add('- 机器判定：validation.json；汇总：summary.json；审计哈希：SHA256SUMS。')
    add('')
    add('    python3 tools/prepare_lifecycle.py snapshot --run-dir <new-evidence-dir>')
    add('    python3 tools/prepare_lifecycle.py create --run-dir <new-evidence-dir>')
    add('    python3 tools/probe_lifecycle.py run --run-dir <new-evidence-dir>')
    add('    python3 tools/probe_lifecycle.py summary --run-dir <new-evidence-dir>')
    add('')
    return '\n'.join(lines) + '\n'


def write_sha256sums(run: Path) -> dict:
    lines = []
    for path in sorted(run.rglob('*')):
        if not path.is_file():
            continue
        relative = path.relative_to(run)
        if relative.name == 'SHA256SUMS':
            continue
        lines.append(f'{sha256_file(path)}  {relative.as_posix()}')
    payload = '\n'.join(lines) + '\n'
    (run / 'SHA256SUMS').write_text(payload, encoding='utf-8')
    return {'files': len(lines), 'sha256': hashlib.sha256(payload.encode()).hexdigest()}


def summarize(ctx: Context, rows: list, scenarios: dict, validation_extra=None) -> dict:
    summary = build_summary(ctx, rows, scenarios)
    save(ctx.run / 'summary.json', summary)
    validation = build_validation(ctx, summary, rows, scenarios)
    if validation_extra:
        validation.update(validation_extra)
    save(ctx.run / 'validation.json', validation)
    report = render_report(ctx, summary, validation)
    (ctx.run / 'report.md').write_text(report, encoding='utf-8')
    return {'summary': summary, 'validation': validation}


# ------------------------------------------------------------------ cleanup
def cleanup_run(ctx: Context, rows: list, started_at: str) -> dict:
    remaining = ctx.remaining_windows(PREFIX, ctx.run / 'observer/final-window-check')
    closed = []
    for window in remaining:
        try:
            ctx.observer.close_window(PREFIX, window.get('internalId'))
            closed.append({'internalId': window.get('internalId'),
                           'caption': window.get('caption')})
        except Exception as exc:
            closed.append({'internalId': window.get('internalId'),
                           'error': f'{type(exc).__name__}: {exc}'})
    wait_for(lambda: not ctx.remaining_windows(PREFIX,
                                               ctx.run / 'observer/final-window-check'),
             10, 0.05)
    project_marker = str(ctx.project)
    leftover = []
    for item in proc_table().values():
        if project_marker not in item['cmdline'] or 'tools/ml.py' not in item['cmdline']:
            continue
        if 'app open' in item['cmdline'] or '_worker' in item['cmdline']:
            leftover.append({'pid': item['pid'], 'cmdline': item['cmdline'][:400],
                             'term': kill_pid(item['pid'], signal.SIGTERM)})
    wait_for(lambda: not [item for item in proc_table().values()
                          if project_marker in item['cmdline']
                          and 'tools/ml.py' in item['cmdline']
                          and ('app open' in item['cmdline'] or '_worker' in item['cmdline'])],
             10, 0.05)
    leftover += [
        {'pid': item['pid'], 'cmdline': item['cmdline'][:400],
         'kill': kill_pid(item['pid'], signal.SIGKILL)}
        for item in proc_table().values()
        if project_marker in item['cmdline'] and 'tools/ml.py' in item['cmdline']
        and ('app open' in item['cmdline'] or '_worker' in item['cmdline'])]
    env_states = {}
    live_leases = {}
    bridge_processes = {}
    for target in sorted(ctx.targets):
        env_id, _ = ctx.ids(target)
        state = ctx.env_state(env_id)
        if (state.get('inspect') or {}).get('running'):
            ctx.env_cli('env', 'stop', env_id, timeout=180)
            state = ctx.env_state(env_id)
        env_states[env_id] = state
        lease = ctx.lease_state(env_id)
        for item in ctx.bridge_processes(env_id):
            kill_pid(item['pid'], signal.SIGTERM)
        live_leases[env_id] = ctx.lease_state(env_id)
        bridge_processes[env_id] = ctx.bridge_processes(env_id)
        if live_leases[env_id]['live_count'] == 0 and ctx.lease_path(env_id).is_file():
            raw = live_leases[env_id]['raw']
            if raw.get('bridge') or raw.get('leases'):
                raw['leases'] = {}
                raw['bridge'] = None
                raw['auto_stop'] = False
                save(ctx.lease_path(env_id), raw)
        socket_path = Path(ctx.portal_directory(env_id) or '/nonexistent') / 'bus'
        if socket_path.is_socket():
            try:
                socket_path.unlink()
            except OSError:
                pass
    wait_for(lambda: not any(ctx.bridge_processes(ctx.ids(target)[0])
                             for target in sorted(ctx.targets)), 10, 0.05)
    running_containers = [row for row in ctx.project_containers()
                          if (row.get('state') or '').lower() == 'running']
    live_lease_total = sum(ctx.lease_state(ctx.ids(target)[0])['live_count']
                           for target in sorted(ctx.targets))
    bridge_total = sum(len(ctx.bridge_processes(ctx.ids(target)[0]))
                       for target in sorted(ctx.targets))
    remaining = ctx.remaining_windows(PREFIX, ctx.run / 'observer/final-window-check-2')
    leftover = [item for item in proc_table().values()
                if project_marker in item['cmdline'] and 'tools/ml.py' in item['cmdline']
                and ('app open' in item['cmdline'] or '_worker' in item['cmdline'])]
    result = {
        'taken_at': stamp(),
        'started_at': started_at,
        'closed_windows': closed,
        'remaining_windows': len(remaining),
        'remaining_window_details': remaining[:10],
        'leftover_processes': len(leftover),
        'leftover_process_details': [
            {'pid': item['pid'], 'cmdline': item['cmdline'][:300]} for item in leftover[:10]],
        'running_containers': len(running_containers),
        'project_containers': ctx.project_containers(),
        'live_leases': live_lease_total,
        'bridge_processes': bridge_total,
        'environment_states': {key: {'record_runtime_state': value.get('record_runtime_state'),
                                     'inspect': (value.get('inspect') or {})}
                               for key, value in env_states.items()},
        'all_clean': (not remaining and not leftover and not running_containers
                      and live_lease_total == 0 and bridge_total == 0),
    }
    save(ctx.run / 'postconditions.json', result)
    return result


def run_all(ctx: Context, targets, scenarios, counts: dict) -> int:
    started_at = stamp()
    manifest = ctx.manifest
    manifest.setdefault('probe', {})
    manifest['probe'].update({
        'started_at': started_at,
        'tool_sha256': {relative: sha256_file(ROOT / relative)
                        for relative in ('tools/probe_lifecycle.py',
                                         'tools/startup_observer.py',
                                         'tools/kwin_window_probe.py')
                        if (ROOT / relative).is_file()},
        'scenarios': list(scenarios),
        'targets': list(targets),
        'counts': counts,
    })
    save(ctx.run / 'manifest.json', manifest)
    rows = []
    try:
        for target in targets:
            if 'A' in scenarios:
                scenario_a(ctx, target, counts['A'], rows)
            if 'B' in scenarios:
                scenario_b(ctx, target, counts['B'], rows)
            if 'C' in scenarios:
                scenario_c(ctx, target, counts['C'], rows)
            if 'D' in scenarios:
                scenario_d(ctx, target, rows)
            if 'E' in scenarios:
                scenario_e(ctx, target, rows)
            if 'F' in scenarios:
                scenario_f(ctx, target, rows)
            if 'G' in scenarios:
                scenario_g(ctx, target, rows)
            if 'H' in scenarios:
                scenario_h(ctx, target, rows)
            try:
                ctx.reset_target(target, ctx.run / 'samples' / target / 'final-reset')
            except Exception as exc:
                print(json.dumps({'warning': f'final reset failed for {target}: {exc}'}),
                      file=sys.stderr, flush=True)
    finally:
        scenarios_found = collect_scenarios(ctx.run)
        cleanup = cleanup_run(ctx, rows, started_at)
        manifest = ctx.manifest
        manifest['probe']['finished_at'] = stamp()
        manifest['probe']['cleanup'] = cleanup
        save(ctx.run / 'manifest.json', manifest)
    result = summarize(ctx, rows, scenarios_found)
    hashes = write_sha256sums(ctx.run)
    manifest = ctx.manifest
    manifest['probe']['summary_sha256'] = sha256_file(ctx.run / 'summary.json')
    manifest['probe']['validation_sha256'] = sha256_file(ctx.run / 'validation.json')
    manifest['probe']['report_sha256'] = sha256_file(ctx.run / 'report.md')
    manifest['probe']['sha256sums'] = hashes
    manifest['finished_at'] = stamp()
    save(ctx.run / 'manifest.json', manifest)
    print(json.dumps({'validation': result['validation']}, ensure_ascii=False, indent=2),
          flush=True)
    return 0 if result['validation']['product_verdict'] == 'lifecycle-fixed' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    run_parser = operations.add_parser('run', help='execute the lifecycle scenarios')
    run_parser.add_argument('--run-dir', type=Path, required=True)
    run_parser.add_argument('--targets', nargs='+', choices=('debian13', 'arch'),
                            default=['debian13', 'arch'])
    run_parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS,
                            default=list(SCENARIOS))
    run_parser.add_argument('--a-samples', type=int, default=10)
    run_parser.add_argument('--b-samples', type=int, default=3)
    run_parser.add_argument('--c-samples', type=int, default=30)
    summary_parser = operations.add_parser(
        'summary', help='rebuild summary/validation/report from raw samples')
    summary_parser.add_argument('--run-dir', type=Path, required=True)
    summary_parser.add_argument('--targets', nargs='+', choices=('debian13', 'arch'),
                                default=['debian13', 'arch'])
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    if not (run / 'manifest.json').is_file():
        raise SystemExit(f'not a prepared lifecycle evidence directory: {run}')
    sys.path.insert(0, str(ROOT))
    if args.operation == 'summary':
        manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
        ctx = Context(run, None)
        ctx.targets = {target: manifest['targets'][target] for target in args.targets
                       if target in manifest['targets']}
        rows = collect_rows(run)
        scenarios = collect_scenarios(run)
        result = summarize(ctx, rows, scenarios)
        write_sha256sums(run)
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        return 0 if result['validation']['product_verdict'] == 'lifecycle-fixed' else 1
    from tools.startup_observer import KWinStartupObserver
    counts = {'A': args.a_samples, 'B': args.b_samples, 'C': args.c_samples}
    with KWinStartupObserver(run / 'observer', PREFIX) as observer:
        ctx = Context(run, observer)
        ctx.targets = {target: ctx.targets[target] for target in args.targets}
        for target in args.targets:
            env_id, app_id = ctx.ids(target)
            if not (ctx.project / '.lab/state/environments' / (env_id + '.json')).is_file():
                raise SystemExit(f'{env_id}: environment record is missing; run prepare create first')
            if not (ctx.project / '.lab/state/launches' / (app_id + '.json')).is_file():
                raise SystemExit(f'{app_id}: launch record is missing; run prepare create first')
        return run_all(ctx, list(args.targets), list(args.scenarios), counts)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('probe_lifecycle: interrupted', file=sys.stderr)
        raise SystemExit(130)
