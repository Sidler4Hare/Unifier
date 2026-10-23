#!/usr/bin/env python3
"""Real-container concurrency verification for the multilinux environment manager.

Seven serial scenarios exercise the product CLI from the frozen evidence
snapshot against five fresh Podman environments plus the native host
distribution (KDE Konsole on Fedora 44):

S1 shared topology: cold N=3 waves and a warm add-two variant on one Debian
   environment; single container/id, one start event, three leases, three
   in-container Mousepad processes, partial close keeps running, last close
   stops the environment.
S2 performance at shared scale N=1..4 (3 cold waves each): per-window ready,
   a wave's first-launch-to-all-ready, cgroup memory.current/peak, PSS, SizeRw,
   host memory/PSI/load.
S3 two distributions: 3 Debian windows + 3 Arch windows at once x3 waves;
   exactly two running project containers, three leases each, no extra
   container.
S4 native host distribution: 3 Konsole --separate windows x3 waves; no
   container, environment or lease is created, three independent host PIDs.
S5 mixed: 2 native + 2 Debian + 2 Arch windows x2 waves; exactly two running
   project containers.
S6 one-environment-per-process control: conc-sep-1/2/3 each open one Debian
   window at once x3 waves; three containers and totals for comparison with
   S2 N=3.
S7 agile concurrency: 10 waves of three concurrent cold first opens plus three
   waves that reopen three windows 150-250 ms after the last close while the
   environment is stopping; final zombie/lease/bridge check.

All GUI waves are serial; only the windows of one designed wave run at the
same time.  The script never touches pre-existing environments or evidence.
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
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.probe_lifecycle import (  # noqa: E402  - shared verified helpers
    EventRecorder, FocusGuard, WaylandTrace, elapsed_ms, host_snapshot,
    kill_pid, pid_stamp, podman_inspect, proc_table, descendant_pids,
    run_command, save, sha256_file, stamp, stats, wait_for,
    write_final_checksums,
)

TZ = ZoneInfo('Asia/Shanghai')
PREFIX = 'ML-CONC-'
SCENARIOS = ('S1', 'S2', 'S3', 'S4', 'S5', 'S6', 'S7')
SHARED_ENV = 'conc-debian'
SECOND_ENV = 'conc-arch'
CONTROL_ENVS = ('conc-sep-1', 'conc-sep-2', 'conc-sep-3')
ENV_IDS = (SHARED_ENV, SECOND_ENV) + CONTROL_ENVS
STOP_REF_MS = 1500.0
REOPEN_REF_MS = 3000.0
NATIVE_READY_TIMEOUT = 25.0
CONTAINER_READY_TIMEOUT = 45.0
STARTUP_NOTE = 'Multilinux concurrency verification note\n固定测试文本，不修改现有文档。\n'


# --------------------------------------------------------------- small helpers
def log(message: str) -> None:
    print(message, flush=True)


def count_status(events, status: str, start: int = 0) -> int:
    return sum(1 for item in events[start:] if item.get('Status') == status)


def process_pss_kb(pids) -> dict:
    total = 0
    rows = []
    for pid in pids:
        try:
            text = Path(f'/proc/{pid}/smaps_rollup').read_text(encoding='utf-8')
        except OSError:
            continue
        pss = None
        for line in text.splitlines():
            if line.startswith('Pss:'):
                try:
                    pss = int(line.split()[1])
                except (IndexError, ValueError):
                    pss = None
                break
        if pss is not None:
            total += pss
            rows.append({'pid': pid, 'pss_kb': pss})
    return {'pss_kb': total, 'processes': rows}


def cgroup_metrics(pid) -> dict:
    result = {'cgroup_path': None, 'memory_current_bytes': None,
              'memory_peak_bytes': None, 'memory_swap_current_bytes': None,
              'pids_current': None, 'oom_kill': None, 'error': None}
    if not pid:
        result['error'] = 'container has no host pid'
        return result
    try:
        lines = Path(f'/proc/{pid}/cgroup').read_text(encoding='utf-8').strip().splitlines()
    except OSError as exc:
        result['error'] = f'{type(exc).__name__}: {exc}'
        return result
    scope = None
    for line in lines:
        if '::' in line:
            scope = line.split('::', 1)[1].strip()
    if not scope:
        result['error'] = 'cgroup path not found'
        return result
    result['cgroup_path'] = scope
    base = Path('/sys/fs/cgroup') / scope.lstrip('/')

    def read(name):
        try:
            return (base / name).read_text(encoding='utf-8').strip()
        except OSError:
            return None

    for name, key in (('memory.current', 'memory_current_bytes'),
                      ('memory.peak', 'memory_peak_bytes'),
                      ('memory.swap.current', 'memory_swap_current_bytes'),
                      ('pids.current', 'pids_current')):
        raw = read(name)
        try:
            result[key] = int(raw) if raw is not None else None
        except ValueError:
            result[key] = None
    events = read('memory.events')
    if events:
        for line in events.splitlines():
            fields = line.split()
            if len(fields) == 2 and fields[0] == 'oom_kill':
                try:
                    result['oom_kill'] = int(fields[1])
                except ValueError:
                    pass
    return result


def container_metrics(ctx, env_id: str, output: Path | None = None) -> dict:
    record = ctx.env_record(env_id)
    name = record['runtime']['container_name']
    info = podman_inspect(name)
    data = {
        'env_id': env_id,
        'container_name': name,
        'record_state': record.get('runtime_state'),
        'container_id_recorded': record['runtime']['container_id'],
        'inspect': None,
        'cgroup': None,
        'processes': None,
        'disk': {'size_rw_bytes': None, 'size_root_fs_bytes': None,
                 'error': None},
    }
    if info is not None:
        state = info.get('State') or {}
        data['inspect'] = {
            'id': info.get('Id'),
            'running': state.get('Running'),
            'status': state.get('Status'),
            'pid': state.get('Pid'),
            'exit_code': state.get('ExitCode'),
            'started_at': state.get('StartedAt'),
            'finished_at': state.get('FinishedAt'),
            'init': (info.get('HostConfig') or {}).get('Init'),
        }
        if state.get('Running') and state.get('Pid'):
            data['cgroup'] = cgroup_metrics(state.get('Pid'))
        ps = ctx.container_ps(env_id, output.parent / (output.stem + '-ps.txt')
                              if output is not None else None)
        pss = ctx.container_pss(env_id)
        data['processes'] = {
            'returncode': ps['returncode'],
            'process_count': len(ps['rows']),
            'mousepad_count': sum(1 for row in ps['rows'] if 'mousepad' in row['comm']),
            'zombie_count': ps['zombie_count'],
            'rows': ps['rows'],
            'pss_kb': pss['pss_kb'],
            'pss_error': pss.get('error'),
        }
    sized = run_command(['podman', 'container', 'inspect', '--size', '--format',
                         '{{json .}}', name], timeout=120)
    if sized.returncode == 0:
        try:
            payload = json.loads(sized.stdout)
            data['disk']['size_rw_bytes'] = payload.get('SizeRw')
            data['disk']['size_root_fs_bytes'] = payload.get('SizeRootFs')
        except ValueError as exc:
            data['disk']['error'] = f'bad json: {exc}'
    else:
        data['disk']['error'] = (sized.stderr or '')[-500:]
    if output is not None:
        save(output, data)
    return data


# ------------------------------------------------------------------- launches
class NativeLaunch:
    """One native host KDE Konsole window (no container, no lease)."""

    def __init__(self, ctx, directory: Path, label: str):
        self.ctx = ctx
        self.label = label
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        # Marker must be unique for the whole run: KWin observer events are
        # kept for the lifetime of one probe process, so reusing a label in a
        # later scenario would match a stale window id that no longer exists.
        self.marker = f'{PREFIX}native-{label}-{uuid.uuid4().hex[:8]}'
        self.command = ['konsole', '--separate', '-p', 'tabtitle=' + self.marker,
                        '-e', 'sleep', '300']
        self.started_ns = time.monotonic_ns()
        self.started_wall_ns = time.time_ns()
        self.process = subprocess.Popen(
            self.command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, start_new_session=True)
        self.pid = self.process.pid
        self.threads = []
        for stream, name in ((self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')):
            thread = threading.Thread(target=self._read, args=(stream, name), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.window = None
        self.added_ns = None
        self.geometry = None
        self.removed = None
        self.close_ns = self.close_wall_ns = None
        self.exit_ns = self.returncode = None
        self._exit_event = threading.Event()
        watcher = threading.Thread(target=self._watch_exit, daemon=True)
        watcher.start()
        self.threads.append(watcher)

    def _read(self, stream, name: str):
        with (self.directory / (name + '.log')).open('wb') as handle:
            for raw in iter(stream.readline, b''):
                handle.write(raw)

    def _watch_exit(self):
        try:
            code = self.process.wait()
        except Exception:
            return
        self.returncode = code
        self.exit_ns = time.monotonic_ns()
        self._exit_event.set()

    def ready(self, timeout: float = NATIVE_READY_TIMEOUT) -> dict:
        from tools.kwin_window_probe import window_info
        deadline = time.monotonic() + float(timeout)
        probes = 0
        while time.monotonic() < deadline:
            self.ctx.observer.pump()
            if self.window is None:
                self.window = next((event for event in self.ctx.observer.events
                                    if event['event'] == 'added'
                                    and self.marker in (event.get('caption') or '')), None)
                if self.window is not None:
                    self.added_ns = self.window['received_ns']
            if self.window is not None:
                # Native readiness uses the KWin frame geometry, not the Wayland
                # buffer geometry: under mixed concurrency a native Konsole
                # window can report bufferWidth/Height = 0 while its frame
                # geometry is already non-zero. Containers keep the stricter
                # windowAdded + non-empty buffer-commit criterion.
                probes += 1
                found = window_info(self.directory / 'geometry-check', self.marker,
                                    window_id=self.window['window_id'])
                if len(found) == 1 and min(found[0].get('width', 0),
                                           found[0].get('height', 0)) > 0:
                    self.geometry = found[0]
                    self.probe_count = probes
                    ready_ns = time.monotonic_ns()
                    return {
                        'kind': 'native',
                        'marker': self.marker,
                        'host_pid': self.pid,
                        'window': self.window,
                        'geometry': self.geometry,
                        'readiness_basis': 'kwin_windowAdded+non_empty_frameGeometry',
                        'frame_geometry': {'width': found[0].get('width'),
                                           'height': found[0].get('height')},
                        'buffer_geometry': {'width': found[0].get('bufferWidth'),
                                            'height': found[0].get('bufferHeight')},
                        'geometry_probe_count': probes,
                        'launch_to_window_ms': elapsed_ms(self.started_ns, self.added_ns),
                        'launch_to_ready_ms': elapsed_ms(self.started_ns, ready_ns),
                        'ready_ns': ready_ns,
                    }
            if self._exit_event.is_set():
                raise RuntimeError('konsole exited before its native window was ready')
            time.sleep(0.1)
        raise TimeoutError('native konsole window did not reach non-empty frame geometry')

    def close(self, timeout: float = 15.0) -> dict:
        """The design closes a native window by killing its independent process."""
        self.close_ns = time.monotonic_ns()
        self.close_wall_ns = time.time_ns()
        self.kill = kill_pid(self.pid, signal.SIGTERM)
        if not self._exit_event.wait(5):
            self.kill_sigkill = kill_pid(self.pid, signal.SIGKILL)
            self._exit_event.wait(5)
        try:
            self.removed = self.wait_removed(timeout)
        except Exception as exc:
            self.removed_error = f'{type(exc).__name__}: {exc}'
        return {'kill': self.kill, 'returncode': self.returncode,
                'removed': self.removed,
                'close_to_window_removed_ms': elapsed_ms(
                    self.close_ns, (self.removed or {}).get('received_ns')),
                'close_to_process_exit_ms': elapsed_ms(self.close_ns, self.exit_ns)}

    def wait_removed(self, timeout: float = 15.0):
        window_id = self.window['window_id'] if self.window else None

        def find():
            self.ctx.observer.pump()
            return next((event for event in self.ctx.observer.events
                         if event['event'] == 'removed' and event.get('window_id') == window_id),
                        None)

        found = wait_for(find, timeout, 0.01)
        if found is None:
            raise TimeoutError('native window was not removed after the process was killed')
        self.removed = found
        return found

    def pss(self) -> dict:
        found = process_pss_kb([self.pid] + descendant_pids(proc_table(), self.pid))
        return {'pss_kb': found['pss_kb'], 'processes': found['processes']}

    def finish(self) -> dict:
        for thread in self.threads:
            thread.join(timeout=5)
        pss = self.pss()
        return {'returncode': self.returncode, 'host_pid': self.pid,
                'pss_kb': pss['pss_kb'], 'pss_processes': pss['processes']}

    def cleanup(self) -> list:
        actions = []
        if not self._exit_event.is_set():
            actions.append({'kill': kill_pid(self.pid, signal.SIGTERM)})
            if not self._exit_event.wait(5):
                actions.append({'kill': kill_pid(self.pid, signal.SIGKILL)})
                self._exit_event.wait(5)
        return actions

    def summary(self) -> dict:
        return {'kind': 'native', 'marker': self.marker, 'host_pid': self.pid,
                'command': self.command, 'window': self.window,
                'geometry': self.geometry, 'removed': self.removed,
                'returncode': self.returncode,
                'stdout_log': str(self.directory / 'stdout.log'),
                'stderr_log': str(self.directory / 'stderr.log')}


class ContainerLaunch:
    """One real ml.py app open launch in one prepared environment."""

    def __init__(self, ctx, directory: Path, env_id: str, marker: str):
        self.ctx = ctx
        self.env_id = env_id
        self.app_id = ctx.app_id(env_id)
        self.marker = marker
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.trace = WaylandTrace(marker)
        self.threads = []
        note = ctx.run / 'work' / env_id / (marker + '.txt')
        note.parent.mkdir(parents=True, exist_ok=True)
        note.unlink(missing_ok=True)
        note.write_text(STARTUP_NOTE, encoding='utf-8')
        note.chmod(0o444)
        self.note, self.note_sha256 = note, sha256_file(note)
        self.command = [sys.executable, str(ctx.run / 'project/tools/ml.py'),
                        'app', 'open', self.app_id, '--', str(note)]
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
        self.close_ns = self.close_wall_ns = None
        self.removed = None
        self.exit_ns = self.returncode = None
        self._exit_event = threading.Event()
        watcher = threading.Thread(target=self._watch_exit, daemon=True)
        watcher.start()
        self.threads.append(watcher)

    def _read(self, stream, name: str):
        with (self.directory / (name + '.log')).open('wb') as handle:
            for raw in iter(stream.readline, b''):
                received = time.monotonic_ns()
                handle.write(raw)
                if name == 'stderr':
                    self.trace.feed(raw.decode('utf-8', 'replace'), received)

    def _watch_exit(self):
        try:
            code = self.process.wait()
        except Exception:
            return
        self.returncode = code
        self.exit_ns = time.monotonic_ns()
        self._exit_event.set()

    def ready(self, timeout: float = CONTAINER_READY_TIMEOUT) -> dict:
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
                    raise RuntimeError('application did not create a native Wayland toplevel')
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
                                        'received_ns': time.monotonic_ns(),
                                        'probe': found[0]}
                    self.window = {**self.window,
                                   'buffer_geometry': {'width': found[0]['bufferWidth'],
                                                       'height': found[0]['bufferHeight']}}
                ready_ns = max(self.trace.commit_ns,
                               self.trace_extra.get('received_ns') or self.window['received_ns'])
                return {
                    'kind': 'container',
                    'env_id': self.env_id,
                    'marker': self.marker,
                    'launcher_pid': self.launcher_pid,
                    'window': self.window,
                    'wayland': self.trace.metadata(),
                    'launch_to_window_ms': elapsed_ms(self.started_ns, self.window['received_ns']),
                    'launch_to_buffer_commit_ms': elapsed_ms(self.started_ns, self.trace.commit_ns),
                    'launch_to_ready_ms': elapsed_ms(self.started_ns, ready_ns),
                    'ready_ns': ready_ns,
                }
            if self._exit_event.is_set():
                raise RuntimeError('launcher exited before a verified native window appeared')
            time.sleep(0.003)
        raise TimeoutError('container window did not reach verified native readiness')

    def close_window(self) -> dict:
        self.close_ns = time.monotonic_ns()
        self.close_wall_ns = time.time_ns()
        self.close_ack = self.ctx.observer.close_window(self.marker, self.window['window_id'])
        return self.close_ack

    def wait_removed(self, timeout: float = 20.0) -> dict:
        window_id = self.window['window_id']

        def find():
            self.ctx.observer.pump()
            return next((event for event in self.ctx.observer.events
                         if event['event'] == 'removed' and event.get('window_id') == window_id),
                        None)

        found = wait_for(find, timeout, 0.01)
        if found is None:
            raise TimeoutError('compositor never removed the closed container window')
        self.removed = found
        return found

    def wait_exit(self, timeout: float = 60.0) -> dict:
        if not self._exit_event.wait(float(timeout)):
            raise TimeoutError('container launcher did not exit')
        return {'returncode': self.returncode, 'exit_ns': self.exit_ns}

    def finish(self) -> dict:
        for thread in self.threads:
            thread.join(timeout=5)
        return {'returncode': self.returncode, 'exit_ns': self.exit_ns,
                'launcher_pid': self.launcher_pid}

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
                actions.append({'kill': kill_pid(self.launcher_pid, signal.SIGTERM)})
                if not self._exit_event.wait(5):
                    actions.append({'kill': kill_pid(self.launcher_pid, signal.SIGKILL)})
                    self._exit_event.wait(5)
        return actions

    def summary(self) -> dict:
        return {'kind': 'container', 'env_id': self.env_id, 'marker': self.marker,
                'launcher_pid': self.launcher_pid, 'command': self.command,
                'window': self.window, 'wayland': self.trace.metadata(),
                'removed': self.removed, 'returncode': self.returncode,
                'note': str(self.note),
                'note_unchanged': sha256_file(self.note) == self.note_sha256,
                'stdout_log': str(self.directory / 'stdout.log'),
                'stderr_log': str(self.directory / 'stderr.log')}


def wait_all_ready(launches, timeout: float) -> tuple:
    deadline = time.monotonic() + float(timeout)
    results = [None] * len(launches)
    errors = {}
    for index, launch in enumerate(launches):
        remaining = max(0.5, deadline - time.monotonic())
        try:
            results[index] = launch.ready(timeout=remaining)
        except Exception as exc:
            errors[index] = f'{type(exc).__name__}: {exc}'
    return results, errors


# -------------------------------------------------------------------- context
class Context:
    def __init__(self, run: Path, observer):
        self.run = Path(run).resolve()
        self.observer = observer
        self.manifest = json.loads((self.run / 'manifest.json').read_text(encoding='utf-8'))
        self.project = self.run / 'project'
        self.env_map = self.manifest.get('env_map') or {}
        self.env_ids = list(self.env_map)
        self.focus_guard = None

    def app_id(self, env_id: str) -> str:
        return self.env_map[env_id]['app_id']

    def env_record(self, env_id: str) -> dict:
        path = self.project / '.lab/state/environments' / (env_id + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def launch_record(self, env_id: str) -> dict:
        app_id = self.app_id(env_id)
        path = self.project / '.lab/state/launches' / (app_id + '.json')
        return json.loads(path.read_text(encoding='utf-8'))

    def lease_path(self, env_id: str) -> Path:
        return self.project / '.lab/leases' / (env_id + '.json')

    def lease_state(self, env_id: str) -> dict:
        path = self.lease_path(env_id)
        raw = {'leases': {}, 'auto_stop': False, 'bridge': None}
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding='utf-8'))
            except json.JSONDecodeError:
                raw = {'leases': {}, 'auto_stop': False, 'bridge': None}
        live = {}
        for token, item in (raw.get('leases') or {}).items():
            if not isinstance(item, dict):
                continue
            if item.get('stamp') is not None and pid_stamp(item.get('pid')) == item.get('stamp'):
                live[token] = item
        bridge = raw.get('bridge')
        return {'path': str(path), 'exists': path.is_file(), 'raw': raw,
                'live': live, 'live_count': len(live), 'bridge': bridge,
                'bridge_alive': bool(bridge and bridge.get('stamp') is not None
                                     and pid_stamp(bridge.get('pid')) == bridge.get('stamp'))}

    def portal_directory(self, env_id: str) -> str:
        record = self.env_record(env_id)
        return ((record.get('desktop') or {}).get('portal_proxy') or {}).get('directory') or ''

    def bridge_processes(self, env_id: str) -> list:
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
            if cwd == target:
                found.append({'pid': item['pid'], 'cwd': str(cwd),
                              'socket_exists': (target / 'bus').is_socket()})
        return found

    def env_state(self, env_id: str, *, save_to: Path | None = None) -> dict:
        record = self.env_record(env_id)
        name = record['runtime']['container_name']
        info = podman_inspect(name)
        state = {'env_id': env_id, 'container_name': name,
                 'container_id_recorded': record['runtime']['container_id'],
                 'record_runtime_state': record.get('runtime_state'),
                 'inspect': None}
        if info is not None:
            runtime = info.get('State') or {}
            state['inspect'] = {
                'id': info.get('Id'),
                'running': runtime.get('Running'),
                'status': runtime.get('Status'),
                'pid': runtime.get('Pid'),
                'exit_code': runtime.get('ExitCode'),
                'init': (info.get('HostConfig') or {}).get('Init'),
            }
        if save_to is not None:
            save(save_to, state)
        return state

    def container_id(self, env_id: str) -> str:
        return self.env_record(env_id)['runtime']['container_id']

    def project_containers(self) -> list:
        project = str(self.project.resolve())
        result = run_command(['podman', 'ps', '-a', '--filter',
                              f'label=multilinux.project={project}',
                              '--format', 'json'], timeout=60, check=True)
        payload = json.loads(result.stdout or '[]')
        rows = []
        for item in payload:
            rows.append({'id': item.get('Id'), 'names': item.get('Names') or [],
                         'state': item.get('State'), 'image': item.get('Image')})
        return rows

    def env_containers(self, env_id: str) -> list:
        name = self.env_record(env_id)['runtime']['container_name']
        return [row for row in self.project_containers()
                if name in (row.get('names') or [])]

    def running_env_containers(self) -> list:
        return [row for row in self.project_containers()
                if (row.get('state') or '').lower() == 'running']

    def env_record_ids(self) -> list:
        directory = self.project / '.lab/state/environments'
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob('*.json'))

    def container_ps(self, env_id: str, output: Path | None = None) -> dict:
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
        for line in text.splitlines()[1:]:
            fields = line.split(None, 3)
            if len(fields) < 4 or not fields[0].isdigit():
                continue
            row = {'pid': int(fields[0]), 'ppid': int(fields[1]),
                   'stat': fields[2], 'comm': fields[3]}
            rows.append(row)
            if 'Z' in row['stat']:
                zombies.append(row)
        return {'returncode': result.returncode, 'rows': rows, 'zombies': zombies,
                'zombie_count': len(zombies), 'stderr': result.stderr[-500:]}

    def container_pss(self, env_id: str) -> dict:
        record = self.env_record(env_id)
        name = record['runtime']['container_name']
        script = ('s=0; for f in /proc/[0-9]*/smaps_rollup; do while read k v u; '
                  'do [ "$k" = "Pss:" ] && s=$((s+v)); done < "$f" 2>/dev/null; '
                  'done; echo pss_kb=$s')
        result = run_command(['podman', 'exec', name, 'sh', '-c', script], timeout=30)
        kb = None
        for line in (result.stdout or '').splitlines():
            if line.strip().startswith('pss_kb='):
                try:
                    kb = int(line.split('=', 1)[1])
                except ValueError:
                    kb = None
        return {'returncode': result.returncode, 'pss_kb': kb,
                'error': None if result.returncode == 0 else (result.stderr or '')[-500:]}

    def metrics(self, env_id: str, output: Path | None = None) -> dict:
        return container_metrics(self, env_id, output)

    # ------------------------------------------------------------------ setup
    def env_cli(self, *argv, timeout=240):
        return run_command([sys.executable, str(self.project / 'tools/ml.py'), *argv],
                           timeout=timeout)

    def wait_env(self, env_id: str, running=True, timeout: float = 30.0) -> dict:
        started = time.monotonic()
        last = None
        while True:
            last = self.env_state(env_id)
            info = last['inspect']
            if info is not None and bool(info['running']) == bool(running):
                return {'ok': True, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                        'state': last}
            if time.monotonic() - started >= timeout:
                return {'ok': False, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                        'state': last}
            time.sleep(0.01)

    def wait_stopped(self, env_id: str, directory: Path, timeout: float = 40.0) -> dict:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        last = None
        while time.monotonic() - started < timeout:
            last = self.env_state(env_id)
            info = last['inspect']
            if info is not None and not info['running'] and last['record_runtime_state'] == 'stopped':
                result = {'ok': True, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                          'state': last}
                save(directory / 'stopped.json', result)
                return result
            time.sleep(0.01)
        result = {'ok': False, 'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                  'state': last}
        save(directory / 'stopped.json', result)
        return result

    def wait_bridge_gone(self, env_id: str, timeout: float = 20.0) -> dict:
        started = time.monotonic()

        def check():
            lease = self.lease_state(env_id)
            if lease['bridge_alive'] or self.bridge_processes(env_id):
                return None
            return {'bridge': lease['bridge']}

        found = wait_for(check, timeout, 0.02)
        return {'ok': found is not None,
                'elapsed_ms': round((time.monotonic() - started) * 1000, 3),
                'bridge': found['bridge'] if found else self.lease_state(env_id)['bridge'],
                'processes': self.bridge_processes(env_id)}

    def reset_env(self, env_id: str, directory: Path) -> dict:
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=True)
        actions = []
        state = self.env_state(env_id)
        if state['inspect'] and state['inspect']['running']:
            result = self.env_cli('env', 'stop', env_id)
            actions.append({'stop': result.returncode, 'stderr': result.stderr.strip()[-500:]})
            if result.returncode:
                raise RuntimeError(f'{env_id}: reset stop failed')
        actions.append({'wait_stopped': self.wait_env(env_id, running=False, timeout=40)})
        lease = self.lease_state(env_id)
        if lease['live_count']:
            raise RuntimeError(f'{env_id}: refusing reset with {lease["live_count"]} live leases')
        killed = []
        for item in self.bridge_processes(env_id):
            killed.append({'pid': item['pid'], 'term': kill_pid(item['pid'], signal.SIGTERM)})
        wait_for(lambda: not self.bridge_processes(env_id), 10, 0.05)
        for item in self.bridge_processes(env_id):
            killed.append({'pid': item['pid'], 'kill': kill_pid(item['pid'], signal.SIGKILL)})
        socket_path = Path(self.portal_directory(env_id) or '/nonexistent') / 'bus'
        if socket_path.is_socket():
            try:
                socket_path.unlink()
            except OSError:
                pass
        if self.lease_path(env_id).is_file():
            raw = self.lease_state(env_id)['raw']
            raw['leases'] = {}
            raw['bridge'] = None
            raw['auto_stop'] = False
            save(self.lease_path(env_id), raw)
        containers = self.env_containers(env_id)
        if len(containers) != 1:
            raise RuntimeError(f'{env_id}: expected one container, saw {containers}')
        payload = {'env_id': env_id, 'actions': actions, 'bridges_killed': killed,
                   'lease_before': lease['raw'].get('auto_stop'), 'containers': containers}
        save(directory / 'reset.json', payload)
        return payload

    def start_env(self, env_id: str, directory: Path | None = None) -> dict:
        started = time.monotonic()
        result = self.env_cli('env', 'start', env_id)
        payload = {'returncode': result.returncode, 'seconds': round(time.monotonic() - started, 3),
                   'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()[-800:]}
        if directory is not None:
            save(directory / 'manual-start.json', payload)
        return payload

    def stop_env(self, env_id: str, directory: Path | None = None) -> dict:
        started = time.monotonic()
        result = self.env_cli('env', 'stop', env_id)
        payload = {'returncode': result.returncode, 'seconds': round(time.monotonic() - started, 3),
                   'stdout': result.stdout.strip(), 'stderr': result.stderr.strip()[-800:]}
        if directory is not None:
            save(directory / 'manual-stop.json', payload)
        return payload

    def pressure_guard(self, directory: Path, label: str = '') -> dict:
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
            blocked = ((available is not None and available < 600 * 1024 * 1024)
                       or (full is not None and full >= 20.0)
                       or (some is not None and some >= 55.0))
            attempts.append({'at': host['taken_at'], 'available_bytes': available,
                             'memory_some_avg10': some, 'memory_full_avg10': full,
                             'blocked': bool(blocked)})
            if not blocked:
                result = {'taken_at': host['taken_at'], 'blocked': False,
                          'available_bytes': available, 'memory_some_avg10': some,
                          'memory_full_avg10': full, 'load': host['load_average'],
                          'attempts': attempts, 'label': label}
                save(directory / 'host-guard.json', result)
                return result
            if time.monotonic() >= deadline:
                result = {'taken_at': host['taken_at'], 'blocked': True,
                          'available_bytes': available, 'attempts': attempts,
                          'reason': 'host memory/PSI stayed above the safety threshold for 180s'}
                save(directory / 'host-guard.json', result)
                raise RuntimeError(result['reason'])
            time.sleep(5)

    def start_events(self, env_id: str, directory: Path) -> EventRecorder:
        name = self.env_record(env_id)['runtime']['container_name']
        recorder = EventRecorder(directory, name)
        # podman events has a short attach delay; wait before recording a wave base.
        time.sleep(0.25)
        return recorder

    def remaining_windows(self, directory: Path) -> list:
        from tools.kwin_window_probe import window_info
        try:
            return window_info(directory, PREFIX)
        except Exception:
            return []

    def native_processes(self) -> list:
        found = []
        for item in proc_table().values():
            if 'konsole' not in item['cmdline'] or PREFIX + 'native-' not in item['cmdline']:
                continue
            found.append({'pid': item['pid'], 'cmdline': item['cmdline'][:300]})
        return found


# ------------------------------------------------------------------ scenarios
def wave_directory(ctx: Context, scenario: str, label: str, index: int) -> Path:
    directory = ctx.run / 'samples' / scenario / f'{label}-{index:03d}'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def finalize_row(ctx: Context, directory: Path, row: dict, rows: list) -> None:
    row['passed'] = not row['failures']
    row['finished_at'] = stamp()
    save(directory / 'result.json', row)
    rows.append(row)
    log(json.dumps({'scenario': row['scenario'], 'target': row.get('label'),
                    'index': row.get('index'), 'passed': row['passed'],
                    'ready_ms': (row.get('timings') or {}).get('wave_ready_ms'),
                    'stop_ms': (row.get('timings') or {}).get('stop_ms'),
                    'failures': row['failures']}, ensure_ascii=False))


def scenario_s1(ctx: Context, rows: list) -> None:
    env_id = SHARED_ENV
    scenario_row = {'scenario': 'S1', 'label': 'shared-topology',
                    'started_at': stamp(), 'failures': [], 'waves': []}
    try:
        # Five cold waves: three concurrent first opens of one shared environment.
        for index in range(5):
            directory = wave_directory(ctx, 'S1', 'cold', index)
            row = {'scenario': 'S1', 'variant': 'cold', 'index': index,
                   'started_at': stamp(), 'failures': []}
            launches = []
            recorder = None
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S1-cold')
                row['reset'] = ctx.reset_env(env_id, directory / 'reset')
                recorder = ctx.start_events(env_id, directory)
                base = len(recorder.snapshot())
                launches = [ContainerLaunch(ctx, directory / f'window-{i}', env_id,
                                            f'{PREFIX}{env_id}-S1-cold-{index:03d}-{i}')
                            for i in range(3)]
                ready, errors = wait_all_ready(launches, CONTAINER_READY_TIMEOUT)
                row['ready'] = ready
                row['ready_errors'] = errors
                events = recorder.snapshot()
                row['events'] = {'create': count_status(events, 'create', base),
                                 'init': count_status(events, 'init', base),
                                 'start': count_status(events, 'start', base),
                                 'statuses': _status_counts(events[base:])}
                row['topology'] = _topology(ctx, env_id, expected_leases=3,
                                            expected_mousepad=3)
                row['metrics_running'] = ctx.metrics(env_id, directory / 'metrics-running.json')
                row['host_after_ready'] = host_snapshot()
                # Close one window: the environment must stay running for the other two.
                launches[0].close_window()
                row['first_removed'] = launches[0].wait_removed(timeout=20)
                row['first_launcher_exit'] = launches[0].wait_exit(timeout=60)
                time.sleep(0.5)
                row['partial_close'] = {
                    'env_state': ctx.env_state(env_id),
                    'lease': ctx.lease_state(env_id),
                    'bridge_processes': ctx.bridge_processes(env_id),
                }
                last_close_wall = None
                for launch in launches[1:]:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    launch.wait_exit(timeout=60)
                    last_close_wall = launch.close_wall_ns
                row['last_close_wall_ns'] = last_close_wall
                died = recorder.wait_for(lambda item: item.get('Status') == 'died', timeout=40)
                row['died_event'] = died
                row['wait_stopped'] = ctx.wait_stopped(env_id, directory / 'stopped')
                row['wait_bridge'] = ctx.wait_bridge_gone(env_id)
                row['lease_after'] = ctx.lease_state(env_id)
                row['containers_after'] = ctx.env_containers(env_id)
                row['timings'] = {
                    'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                         for item in ready),
                    'stop_ms': (round((died['timeNano'] - last_close_wall) / 1e6, 3)
                                if died and last_close_wall else
                                (row['wait_stopped'] or {}).get('elapsed_ms')),
                }
                failures = row['failures']
                _evaluate_topology(row, ctx, env_id, expected_leases=3,
                                   expected_mousepad=3, failures=failures)
                if any(item is None for item in ready):
                    failures.append('not every window became ready')
                if row['events']['create']:
                    failures.append('container create event happened during the wave')
                if row['events']['start'] != 1:
                    failures.append(f'expected exactly one start event, saw {row["events"]["start"]}')
                if not ((row['partial_close']['env_state'].get('inspect') or {}).get('running')):
                    failures.append('environment stopped when only one of three windows closed')
                if row['partial_close']['lease']['live_count'] != 2:
                    failures.append('expected two live leases after the first close')
                if not (row['wait_stopped'] or {}).get('ok'):
                    failures.append('environment did not stop after the last window closed')
                if (row['lease_after'] or {}).get('live_count'):
                    failures.append('leases remain after the last window closed')
                if not (row['wait_bridge'] or {}).get('ok'):
                    failures.append('bridge remained after the last window closed')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    try:
                        launch.finish()
                    except Exception:
                        pass
                if recorder is not None:
                    row['events_status_counts'] = recorder.status_counts()
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'variant': 'cold', 'index': index,
                                              'passed': row['passed'],
                                              'failures': row['failures']})
        # Two warm waves: an already running environment gets two more concurrent windows.
        for index in range(2):
            directory = wave_directory(ctx, 'S1', 'warm', index)
            row = {'scenario': 'S1', 'variant': 'warm', 'index': index,
                   'started_at': stamp(), 'failures': []}
            launches = []
            recorder = None
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S1-warm')
                row['reset'] = ctx.reset_env(env_id, directory / 'reset')
                recorder = ctx.start_events(env_id, directory)
                base = len(recorder.snapshot())
                first = ContainerLaunch(ctx, directory / 'window-first', env_id,
                                        f'{PREFIX}{env_id}-S1-warm-{index:03d}-first')
                launches.append(first)
                row['first_ready'] = first.ready()
                cold_events = recorder.snapshot()
                row['cold_start_events'] = count_status(cold_events, 'start', base)
                base_extra = len(cold_events)
                extras = [ContainerLaunch(ctx, directory / f'window-extra-{i}', env_id,
                                          f'{PREFIX}{env_id}-S1-warm-{index:03d}-extra-{i}')
                          for i in range(2)]
                launches.extend(extras)
                extra_ready, extra_errors = wait_all_ready(extras, CONTAINER_READY_TIMEOUT)
                row['extra_ready'] = extra_ready
                row['extra_ready_errors'] = extra_errors
                events = recorder.snapshot()
                row['extra_start_events'] = count_status(events, 'start', base_extra)
                row['events'] = {'create': count_status(events, 'create', base),
                                 'start_total': count_status(events, 'start', base)}
                row['topology'] = _topology(ctx, env_id, expected_leases=3,
                                            expected_mousepad=3)
                row['metrics_running'] = ctx.metrics(env_id, directory / 'metrics-running.json')
                last_close_wall = None
                for launch in launches:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    launch.wait_exit(timeout=60)
                    last_close_wall = launch.close_wall_ns
                died = recorder.wait_for(lambda item: item.get('Status') == 'died', timeout=40)
                row['wait_stopped'] = ctx.wait_stopped(env_id, directory / 'stopped')
                row['lease_after'] = ctx.lease_state(env_id)
                row['timings'] = {'stop_ms': (round((died['timeNano'] - last_close_wall) / 1e6, 3)
                                              if died and last_close_wall else None)}
                failures = row['failures']
                _evaluate_topology(row, ctx, env_id, expected_leases=3,
                                   expected_mousepad=3, failures=failures)
                if (row.get('first_ready') or {}).get('launch_to_ready_ms') is None:
                    failures.append('first warm window did not become ready')
                if any(item is None for item in extra_ready):
                    failures.append('an extra window in the running environment did not become ready')
                if row['cold_start_events'] != 1:
                    failures.append(f'expected one cold start event, saw {row["cold_start_events"]}')
                if row['extra_start_events']:
                    failures.append('extra windows caused an additional container start event')
                if not (row['wait_stopped'] or {}).get('ok'):
                    failures.append('environment did not stop after the warm group closed')
                if (row['lease_after'] or {}).get('live_count'):
                    failures.append('leases remain after the warm group closed')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    try:
                        launch.finish()
                    except Exception:
                        pass
                if recorder is not None:
                    row['events_status_counts'] = recorder.status_counts()
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'variant': 'warm', 'index': index,
                                              'passed': row['passed'],
                                              'failures': row['failures']})
    finally:
        scenario_row['passed'] = not scenario_row['failures'] and all(
            wave['passed'] for wave in scenario_row['waves'])
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S1/scenario.json', scenario_row)


def _status_counts(events) -> dict:
    counts = {}
    for item in events:
        status = item.get('Status') or 'unknown'
        counts[status] = counts.get(status, 0) + 1
    return counts


def _topology(ctx: Context, env_id: str, expected_leases: int,
              expected_mousepad: int) -> dict:
    containers = ctx.env_containers(env_id)
    lease = ctx.lease_state(env_id)
    ps = ctx.container_ps(env_id)
    return {
        'container_count': len(containers),
        'container_id': containers[0].get('id') if len(containers) == 1 else None,
        'container_id_recorded': ctx.container_id(env_id),
        'container_id_matches': bool(len(containers) == 1
                                     and containers[0].get('id') == ctx.container_id(env_id)),
        'leases': lease['live_count'],
        'mousepad_processes': sum(1 for row in ps['rows'] if 'mousepad' in row['comm']),
        'container_processes': len(ps['rows']),
        'zombies': ps['zombie_count'],
        'expected_leases': expected_leases,
        'expected_mousepad': expected_mousepad,
    }


def _evaluate_topology(row: dict, ctx: Context, env_id: str, expected_leases: int,
                       expected_mousepad: int, failures: list) -> None:
    topology = row.get('topology') or {}
    if topology.get('container_count') != 1:
        failures.append(f'expected one container, saw {topology.get("container_count")}')
    if not topology.get('container_id_matches'):
        failures.append('container id is not the id recorded at prepare time')
    if topology.get('leases') != expected_leases:
        failures.append(f'expected {expected_leases} live leases, saw {topology.get("leases")}')
    if topology.get('mousepad_processes') != expected_mousepad:
        failures.append(f'expected {expected_mousepad} in-container Mousepad processes, '
                        f'saw {topology.get("mousepad_processes")}')


def scenario_s2(ctx: Context, rows: list) -> None:
    env_id = SHARED_ENV
    scenario_row = {'scenario': 'S2', 'label': 'shared-scale', 'started_at': stamp(),
                    'failures': [], 'waves': []}
    try:
        for scale in (1, 2, 3, 4):
            for index in range(3):
                directory = wave_directory(ctx, 'S2', f'N{scale}', index)
                row = {'scenario': 'S2', 'variant': 'cold', 'scale': scale,
                       'index': index, 'started_at': stamp(), 'failures': []}
                launches = []
                recorder = None
                try:
                    row['host_before'] = host_snapshot()
                    row['host_guard'] = ctx.pressure_guard(directory / 'guard', f'S2-N{scale}')
                    row['reset'] = ctx.reset_env(env_id, directory / 'reset')
                    recorder = ctx.start_events(env_id, directory)
                    base = len(recorder.snapshot())
                    launches = [ContainerLaunch(ctx, directory / f'window-{i}', env_id,
                                                f'{PREFIX}{env_id}-S2-N{scale}-{index:03d}-{i}')
                                for i in range(scale)]
                    ready, errors = wait_all_ready(launches, CONTAINER_READY_TIMEOUT + scale * 10)
                    row['ready'] = ready
                    row['ready_errors'] = errors
                    events = recorder.snapshot()
                    row['events'] = {'create': count_status(events, 'create', base),
                                     'start': count_status(events, 'start', base)}
                    row['topology'] = _topology(ctx, env_id, expected_leases=scale,
                                                expected_mousepad=scale)
                    row['metrics_running'] = ctx.metrics(env_id, directory / 'metrics-running.json')
                    row['host_after_ready'] = host_snapshot()
                    last_close_wall = None
                    for launch in launches:
                        launch.close_window()
                        launch.wait_removed(timeout=20)
                        launch.wait_exit(timeout=60)
                        last_close_wall = launch.close_wall_ns
                    died = recorder.wait_for(lambda item: item.get('Status') == 'died', timeout=40)
                    row['wait_stopped'] = ctx.wait_stopped(env_id, directory / 'stopped')
                    row['wait_bridge'] = ctx.wait_bridge_gone(env_id)
                    row['lease_after'] = ctx.lease_state(env_id)
                    row['timings'] = {
                        'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                             for item in ready),
                        'stop_ms': (round((died['timeNano'] - last_close_wall) / 1e6, 3)
                                    if died and last_close_wall else
                                    (row['wait_stopped'] or {}).get('elapsed_ms')),
                    }
                    row['disk_size_rw_bytes'] = (row['metrics_running'] or {}).get(
                        'disk', {}).get('size_rw_bytes')
                    failures = row['failures']
                    _evaluate_topology(row, ctx, env_id, scale, scale, failures)
                    if any(item is None for item in ready):
                        failures.append('not every window became ready')
                    if row['events']['create']:
                        failures.append('container create event happened during the wave')
                    if row['events']['start'] != 1:
                        failures.append(f'expected exactly one start event, saw {row["events"]["start"]}')
                    if not (row['wait_stopped'] or {}).get('ok'):
                        failures.append('environment did not stop after the last window closed')
                    if (row['lease_after'] or {}).get('live_count'):
                        failures.append('leases remain after the wave')
                    if not (row['wait_bridge'] or {}).get('ok'):
                        failures.append('bridge remained after the wave')
                    if (row['metrics_running'] or {}).get('cgroup', {}).get(
                            'memory_current_bytes') is None:
                        failures.append('cgroup memory.current was not readable while running')
                except Exception as exc:
                    row['error'] = f'{type(exc).__name__}: {exc}'
                    row['failures'].append('exception: ' + row['error'])
                finally:
                    for launch in launches:
                        row.setdefault('cleanup', []).append(launch.cleanup())
                        try:
                            launch.finish()
                        except Exception:
                            pass
                    if recorder is not None:
                        recorder.stop()
                    finalize_row(ctx, directory, row, rows)
                    scenario_row['waves'].append({'scale': scale, 'index': index,
                                                  'passed': row['passed'],
                                                  'failures': row['failures']})
    finally:
        scenario_row['passed'] = all(wave['passed'] for wave in scenario_row['waves'])
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S2/scenario.json', scenario_row)


def scenario_s3(ctx: Context, rows: list) -> None:
    scenario_row = {'scenario': 'S3', 'label': 'two-distributions',
                    'started_at': stamp(), 'failures': [], 'waves': []}
    try:
        for index in range(3):
            directory = wave_directory(ctx, 'S3', 'wave', index)
            row = {'scenario': 'S3', 'index': index, 'started_at': stamp(), 'failures': []}
            launches = []
            recorders = {}
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S3')
                row['reset'] = {env: ctx.reset_env(env, directory / f'reset-{env}')
                                for env in (SHARED_ENV, SECOND_ENV)}
                for env in (SHARED_ENV, SECOND_ENV):
                    recorders[env] = ctx.start_events(env, directory / f'events-{env}')
                bases = {env: len(recorders[env].snapshot()) for env in recorders}
                counters = {env: 0 for env in recorders}
                for env, app_target in ((SHARED_ENV, 'debian'), (SECOND_ENV, 'arch')):
                    for i in range(3):
                        counters[env] += 1
                        launches.append(ContainerLaunch(
                            ctx, directory / f'{env}-window-{i}', env,
                            f'{PREFIX}{env}-S3-{index:03d}-{i}'))
                ready, errors = wait_all_ready(launches, CONTAINER_READY_TIMEOUT + 20)
                row['ready'] = ready
                row['ready_errors'] = errors
                row['running_containers'] = ctx.running_env_containers()
                row['events'] = {}
                for group_env, recorder in recorders.items():
                    events = recorder.snapshot()
                    row['events'][group_env] = {
                        'create': count_status(events, 'create', bases[group_env]),
                        'start': count_status(events, 'start', bases[group_env]),
                        'statuses': _status_counts(events[bases[group_env]:]),
                    }
                row['topology'] = {
                    SHARED_ENV: _topology(ctx, SHARED_ENV, 3, 3),
                    SECOND_ENV: _topology(ctx, SECOND_ENV, 3, 3),
                    'running_project_containers': len(row['running_containers']),
                }
                row['metrics_running'] = {
                    env: ctx.metrics(env, directory / f'metrics-{env}.json')
                    for env in (SHARED_ENV, SECOND_ENV)}
                last_close_wall = None
                for launch in launches:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    launch.wait_exit(timeout=60)
                    last_close_wall = launch.close_wall_ns
                row['died_events'] = {}
                row['wait_stopped'] = {}
                for env, recorder in recorders.items():
                    row['died_events'][env] = recorder.wait_for(
                        lambda item: item.get('Status') == 'died', timeout=40)
                    row['wait_stopped'][env] = ctx.wait_stopped(
                        env, directory / f'stopped-{env}')
                row['wait_bridge'] = {env: ctx.wait_bridge_gone(env)
                                      for env in (SHARED_ENV, SECOND_ENV)}
                row['lease_after'] = {env: ctx.lease_state(env)
                                      for env in (SHARED_ENV, SECOND_ENV)}
                row['timings'] = {
                    'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                         for item in ready),
                    'stop_ms': max((round((row['died_events'][env]['timeNano'] - last_close_wall) / 1e6, 3)
                                    if row['died_events'][env] and last_close_wall else 0)
                                   for env in (SHARED_ENV, SECOND_ENV)),
                }
                failures = row['failures']
                if any(item is None for item in ready):
                    failures.append('not every Debian/Arch window became ready')
                if len(row['running_containers']) != 2:
                    failures.append(f'expected exactly two running project containers, '
                                    f'saw {len(row["running_containers"])}')
                for env, expected in ((SHARED_ENV, 'debian'), (SECOND_ENV, 'arch')):
                    topology = row['topology'][env]
                    _evaluate_topology({'topology': topology}, ctx, env, 3, 3, failures)
                    if row['events'][env]['create']:
                        failures.append(f'{env}: container create event happened during the wave')
                    if row['events'][env]['start'] != 1:
                        failures.append(f'{env}: expected one start event, saw '
                                        f'{row["events"][env]["start"]}')
                    if not (row['wait_stopped'][env] or {}).get('ok'):
                        failures.append(f'{env}: environment did not stop after the wave')
                    if (row['lease_after'][env] or {}).get('live_count'):
                        failures.append(f'{env}: leases remain after the wave')
                    if not (row['wait_bridge'][env] or {}).get('ok'):
                        failures.append(f'{env}: bridge remained after the wave')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    try:
                        launch.finish()
                    except Exception:
                        pass
                for recorder in recorders.values():
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'index': index, 'passed': row['passed'],
                                              'failures': row['failures']})
    finally:
        scenario_row['passed'] = len(scenario_row['waves']) == 3 and all(
            wave['passed'] for wave in scenario_row['waves'])
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S3/scenario.json', scenario_row)


def scenario_s4(ctx: Context, rows: list) -> None:
    scenario_row = {'scenario': 'S4', 'label': 'native-host', 'started_at': stamp(),
                    'failures': [], 'waves': []}
    try:
        for index in range(3):
            directory = wave_directory(ctx, 'S4', 'wave', index)
            row = {'scenario': 'S4', 'index': index, 'started_at': stamp(), 'failures': []}
            launches = []
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S4')
                row['containers_before'] = ctx.project_containers()
                row['env_records_before'] = ctx.env_record_ids()
                row['lease_before'] = {env: ctx.lease_state(env)['live_count']
                                       for env in ctx.env_ids}
                launches = [NativeLaunch(ctx, directory / f'native-{i}',
                                         f'{index:03d}-{i}') for i in range(3)]
                ready, errors = wait_all_ready(launches, NATIVE_READY_TIMEOUT)
                row['ready'] = ready
                row['ready_errors'] = errors
                row['host_pids'] = [launch.pid for launch in launches]
                row['host_pids_unique'] = len(set(row['host_pids'])) == 3
                row['containers_during'] = ctx.project_containers()
                row['env_records_during'] = ctx.env_record_ids()
                row['lease_during'] = {env: ctx.lease_state(env)['live_count']
                                       for env in ctx.env_ids}
                row['host_after_ready'] = host_snapshot()
                row['pss'] = [launch.pss().get('pss_kb') for launch in launches]
                row['pss_processes'] = [launch.pss().get('processes') for launch in launches]
                close_rows = []
                for launch in launches:
                    close_rows.append(launch.close())
                row['close'] = close_rows
                row['windows_after'] = ctx.remaining_windows(directory / 'windows-after')
                row['timings'] = {
                    'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                         for item in ready),
                    'close_ms': max((item or {}).get('close_to_window_removed_ms') or 0
                                    for item in close_rows),
                }
                failures = row['failures']
                if any(item is None for item in ready):
                    failures.append('not every native Konsole window became ready')
                if not row['host_pids_unique']:
                    failures.append('native windows did not use three independent host PIDs')
                if row['containers_during'] != row['containers_before']:
                    failures.append('native wave changed the project container set')
                if row['env_records_during'] != row['env_records_before']:
                    failures.append('native wave created an environment record')
                if any(count for count in row['lease_during'].values()):
                    failures.append('native wave created a product lease')
                if row['windows_after']:
                    failures.append('native windows were not removed after killing their processes')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'index': index, 'passed': row['passed'],
                                              'failures': row['failures']})
    finally:
        scenario_row['passed'] = len(scenario_row['waves']) == 3 and all(
            wave['passed'] for wave in scenario_row['waves'])
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S4/scenario.json', scenario_row)


def scenario_s5(ctx: Context, rows: list) -> None:
    scenario_row = {'scenario': 'S5', 'label': 'mixed', 'started_at': stamp(),
                    'failures': [], 'waves': []}
    try:
        for index in range(2):
            directory = wave_directory(ctx, 'S5', 'wave', index)
            row = {'scenario': 'S5', 'index': index, 'started_at': stamp(), 'failures': []}
            launches = []
            recorders = {}
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S5')
                row['reset'] = {env: ctx.reset_env(env, directory / f'reset-{env}')
                                for env in (SHARED_ENV, SECOND_ENV)}
                for env in (SHARED_ENV, SECOND_ENV):
                    recorders[env] = ctx.start_events(env, directory / f'events-{env}')
                bases = {env: len(recorders[env].snapshot()) for env in recorders}
                native = [NativeLaunch(ctx, directory / f'native-{i}', f'{index:03d}-{i}')
                          for i in range(2)]
                launches.extend(native)
                for env in (SHARED_ENV, SECOND_ENV):
                    for i in range(2):
                        launches.append(ContainerLaunch(
                            ctx, directory / f'{env}-window-{i}', env,
                            f'{PREFIX}{env}-S5-{index:03d}-{i}'))
                ready, errors = wait_all_ready(launches, CONTAINER_READY_TIMEOUT + 20)
                row['ready'] = ready
                row['ready_errors'] = errors
                row['running_containers'] = ctx.running_env_containers()
                row['topology'] = {env: _topology(ctx, env, 2, 2)
                                   for env in (SHARED_ENV, SECOND_ENV)}
                row['events'] = {}
                for env, recorder in recorders.items():
                    events = recorder.snapshot()
                    row['events'][env] = {'create': count_status(events, 'create', bases[env]),
                                          'start': count_status(events, 'start', bases[env])}
                row['metrics_running'] = {env: ctx.metrics(env, directory / f'metrics-{env}.json')
                                          for env in (SHARED_ENV, SECOND_ENV)}
                row['host_pids'] = [launch.pid for launch in native]
                row['host_pids_unique'] = len(set(row['host_pids'])) == 2
                row['native_pss'] = [launch.pss().get('pss_kb') for launch in native]
                row['host_after_ready'] = host_snapshot()
                last_close_wall = None
                for launch in launches:
                    if isinstance(launch, NativeLaunch):
                        launch.close()
                    else:
                        launch.close_window()
                        launch.wait_removed(timeout=20)
                        launch.wait_exit(timeout=60)
                        last_close_wall = launch.close_wall_ns
                row['wait_stopped'] = {}
                row['died_events'] = {}
                for env, recorder in recorders.items():
                    row['died_events'][env] = recorder.wait_for(
                        lambda item: item.get('Status') == 'died', timeout=40)
                    row['wait_stopped'][env] = ctx.wait_stopped(
                        env, directory / f'stopped-{env}')
                row['lease_after'] = {env: ctx.lease_state(env)
                                      for env in (SHARED_ENV, SECOND_ENV)}
                row['windows_after'] = ctx.remaining_windows(directory / 'windows-after')
                row['timings'] = {
                    'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                         for item in ready),
                }
                failures = row['failures']
                if any(item is None for item in ready):
                    failures.append('not every mixed window became ready')
                if len(row['running_containers']) != 2:
                    failures.append('mixed wave did not run exactly two project containers')
                for env in (SHARED_ENV, SECOND_ENV):
                    _evaluate_topology({'topology': row['topology'][env]}, ctx, env, 2, 2, failures)
                    if row['events'][env]['create']:
                        failures.append(f'{env}: container create event during the mixed wave')
                    if row['events'][env]['start'] > 1:
                        failures.append(f'{env}: more than one start event during the mixed wave')
                    if not (row['wait_stopped'][env] or {}).get('ok'):
                        failures.append(f'{env}: environment did not stop after the mixed wave')
                    if (row['lease_after'][env] or {}).get('live_count'):
                        failures.append(f'{env}: leases remain after the mixed wave')
                if not row['host_pids_unique']:
                    failures.append('native windows in the mixed wave were not independent')
                if row['windows_after']:
                    failures.append('windows remained after the mixed wave cleanup')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    if isinstance(launch, ContainerLaunch):
                        try:
                            launch.finish()
                        except Exception:
                            pass
                for recorder in recorders.values():
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'index': index, 'passed': row['passed'],
                                              'failures': row['failures']})
    finally:
        scenario_row['passed'] = len(scenario_row['waves']) == 2 and all(
            wave['passed'] for wave in scenario_row['waves'])
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S5/scenario.json', scenario_row)


def scenario_s6(ctx: Context, rows: list) -> None:
    scenario_row = {'scenario': 'S6', 'label': 'one-env-per-process',
                    'started_at': stamp(), 'failures': [], 'waves': []}
    try:
        for index in range(3):
            directory = wave_directory(ctx, 'S6', 'wave', index)
            row = {'scenario': 'S6', 'index': index, 'started_at': stamp(), 'failures': []}
            launches = []
            recorders = {}
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S6')
                row['reset'] = {env: ctx.reset_env(env, directory / f'reset-{env}')
                                for env in CONTROL_ENVS}
                for env in CONTROL_ENVS:
                    recorders[env] = ctx.start_events(env, directory / f'events-{env}')
                bases = {env: len(recorders[env].snapshot()) for env in recorders}
                for env in CONTROL_ENVS:
                    launches.append(ContainerLaunch(ctx, directory / f'{env}-window', env,
                                                    f'{PREFIX}{env}-S6-{index:03d}'))
                ready, errors = wait_all_ready(launches, CONTAINER_READY_TIMEOUT + 20)
                row['ready'] = ready
                row['ready_errors'] = errors
                row['running_containers'] = ctx.running_env_containers()
                row['topology'] = {env: _topology(ctx, env, 1, 1) for env in CONTROL_ENVS}
                row['events'] = {}
                for env, recorder in recorders.items():
                    events = recorder.snapshot()
                    row['events'][env] = {'create': count_status(events, 'create', bases[env]),
                                          'start': count_status(events, 'start', bases[env])}
                row['metrics_running'] = {env: ctx.metrics(env, directory / f'metrics-{env}.json')
                                          for env in CONTROL_ENVS}
                row['totals_running'] = _sum_metrics(row['metrics_running'])
                last_close_wall = None
                for launch in launches:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    launch.wait_exit(timeout=60)
                    last_close_wall = launch.close_wall_ns
                row['wait_stopped'] = {}
                row['died_events'] = {}
                for env, recorder in recorders.items():
                    row['died_events'][env] = recorder.wait_for(
                        lambda item: item.get('Status') == 'died', timeout=40)
                    row['wait_stopped'][env] = ctx.wait_stopped(
                        env, directory / f'stopped-{env}')
                row['lease_after'] = {env: ctx.lease_state(env) for env in CONTROL_ENVS}
                row['timings'] = {
                    'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                         for item in ready),
                    'stop_ms': max((round((row['died_events'][env]['timeNano'] - last_close_wall) / 1e6, 3)
                                    if row['died_events'][env] and last_close_wall else 0)
                                   for env in CONTROL_ENVS),
                }
                failures = row['failures']
                if any(item is None for item in ready):
                    failures.append('not every one-env-per-process window became ready')
                if len(row['running_containers']) != 3:
                    failures.append('the control wave did not run exactly three project containers')
                expected_ids = {ctx.container_id(env) for env in CONTROL_ENVS}
                if len(expected_ids) != 3:
                    failures.append('the three control environments do not have three distinct container ids')
                for env in CONTROL_ENVS:
                    _evaluate_topology({'topology': row['topology'][env]}, ctx, env, 1, 1, failures)
                    if row['events'][env]['create']:
                        failures.append(f'{env}: container create event during the control wave')
                    if row['events'][env]['start'] != 1:
                        failures.append(f'{env}: expected one start event, saw '
                                        f'{row["events"][env]["start"]}')
                    if not (row['wait_stopped'][env] or {}).get('ok'):
                        failures.append(f'{env}: environment did not stop after the control wave')
                    if (row['lease_after'][env] or {}).get('live_count'):
                        failures.append(f'{env}: leases remain after the control wave')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    try:
                        launch.finish()
                    except Exception:
                        pass
                for recorder in recorders.values():
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'index': index, 'passed': row['passed'],
                                              'failures': row['failures']})
    finally:
        scenario_row['passed'] = len(scenario_row['waves']) == 3 and all(
            wave['passed'] for wave in scenario_row['waves'])
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S6/scenario.json', scenario_row)


def _sum_metrics(metrics_by_env: dict) -> dict:
    total = {'memory_current_bytes': 0, 'memory_peak_bytes': 0, 'pss_kb': 0,
             'size_rw_bytes': 0, 'process_count': 0, 'mousepad_count': 0}
    for env, data in metrics_by_env.items():
        cgroup = (data or {}).get('cgroup') or {}
        processes = (data or {}).get('processes') or {}
        disk = (data or {}).get('disk') or {}
        total['memory_current_bytes'] += cgroup.get('memory_current_bytes') or 0
        total['memory_peak_bytes'] += cgroup.get('memory_peak_bytes') or 0
        total['pss_kb'] += processes.get('pss_kb') or 0
        total['size_rw_bytes'] += disk.get('size_rw_bytes') or 0
        total['process_count'] += processes.get('process_count') or 0
        total['mousepad_count'] += processes.get('mousepad_count') or 0
    return total


def scenario_s7(ctx: Context, rows: list) -> None:
    env_id = SHARED_ENV
    scenario_row = {'scenario': 'S7', 'label': 'agile-concurrency',
                    'started_at': stamp(), 'failures': [], 'waves': []}
    try:
        for index in range(10):
            directory = wave_directory(ctx, 'S7', 'agile', index)
            row = {'scenario': 'S7', 'variant': 'cold-burst', 'index': index,
                   'started_at': stamp(), 'failures': []}
            launches = []
            recorder = None
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S7')
                row['reset'] = ctx.reset_env(env_id, directory / 'reset')
                recorder = ctx.start_events(env_id, directory)
                base = len(recorder.snapshot())
                launches = [ContainerLaunch(ctx, directory / f'window-{i}', env_id,
                                            f'{PREFIX}{env_id}-S7-{index:03d}-{i}')
                            for i in range(3)]
                ready, errors = wait_all_ready(launches, CONTAINER_READY_TIMEOUT)
                row['ready'] = ready
                row['ready_errors'] = errors
                events = recorder.snapshot()
                row['events'] = {'create': count_status(events, 'create', base),
                                 'start': count_status(events, 'start', base)}
                row['topology'] = _topology(ctx, env_id, 3, 3)
                row['metrics_running'] = ctx.metrics(env_id, directory / 'metrics-running.json')
                last_close_wall = None
                for launch in launches:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    launch.wait_exit(timeout=60)
                    last_close_wall = launch.close_wall_ns
                died = recorder.wait_for(lambda item: item.get('Status') == 'died', timeout=40)
                row['wait_stopped'] = ctx.wait_stopped(env_id, directory / 'stopped')
                row['wait_bridge'] = ctx.wait_bridge_gone(env_id)
                row['lease_after'] = ctx.lease_state(env_id)
                row['timings'] = {
                    'wave_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                         for item in ready),
                    'stop_ms': (round((died['timeNano'] - last_close_wall) / 1e6, 3)
                                if died and last_close_wall else
                                (row['wait_stopped'] or {}).get('elapsed_ms')),
                }
                failures = row['failures']
                _evaluate_topology(row, ctx, env_id, 3, 3, failures)
                if any(item is None for item in ready):
                    failures.append('not every agile wave window became ready')
                if row['events']['create']:
                    failures.append('container create event happened during the agile wave')
                if row['events']['start'] != 1:
                    failures.append(f'expected exactly one start event, saw {row["events"]["start"]}')
                if not (row['wait_stopped'] or {}).get('ok'):
                    failures.append('environment did not stop after the agile wave')
                if (row['lease_after'] or {}).get('live_count'):
                    failures.append('leases remain after the agile wave')
                if not (row['wait_bridge'] or {}).get('ok'):
                    failures.append('bridge remained after the agile wave')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    try:
                        launch.finish()
                    except Exception:
                        pass
                if recorder is not None:
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'variant': 'cold-burst', 'index': index,
                                              'passed': row['passed'],
                                              'failures': row['failures']})
        # Three waves that reopen three windows 150-250 ms after the last close,
        # while the environment is still stopping from the previous group.
        delays = (150.0, 200.0, 250.0)
        for index in range(3):
            directory = wave_directory(ctx, 'S7', 'reopen', index)
            row = {'scenario': 'S7', 'variant': 'reopen-during-stop', 'index': index,
                   'started_at': stamp(), 'failures': []}
            launches = []
            recorder = None
            try:
                row['host_before'] = host_snapshot()
                row['host_guard'] = ctx.pressure_guard(directory / 'guard', 'S7-reopen')
                row['reset'] = ctx.reset_env(env_id, directory / 'reset')
                recorder = ctx.start_events(env_id, directory)
                base = len(recorder.snapshot())
                first = [ContainerLaunch(ctx, directory / f'first-{i}', env_id,
                                         f'{PREFIX}{env_id}-S7r-{index:03d}-first-{i}')
                         for i in range(3)]
                launches.extend(first)
                first_ready, first_errors = wait_all_ready(first, CONTAINER_READY_TIMEOUT)
                row['first_ready'] = first_ready
                row['first_ready_errors'] = first_errors
                # Close the first group and measure the last close *request*.
                # Do not wait for the launchers to exit here: the next group is
                # launched 150-250 ms after the last close, exactly while the
                # environment is still stopping and the lease lock is held.
                last_close_wall = None
                for launch in first:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    last_close_wall = launch.close_wall_ns
                target_delay = delays[index] * 1e6
                elapsed = time.time_ns() - last_close_wall
                if elapsed < target_delay:
                    time.sleep((target_delay - elapsed) / 1e9)
                row['reopen_started_after_close_ms'] = round(
                    (time.time_ns() - last_close_wall) / 1e6, 3)
                row['state_at_reopen'] = ctx.env_state(env_id)
                second = [ContainerLaunch(ctx, directory / f'second-{i}', env_id,
                                          f'{PREFIX}{env_id}-S7r-{index:03d}-second-{i}')
                          for i in range(3)]
                launches.extend(second)
                second_ready, second_errors = wait_all_ready(second, CONTAINER_READY_TIMEOUT)
                row['second_ready'] = second_ready
                row['second_ready_errors'] = second_errors
                row['first_launcher_exit'] = [launch.wait_exit(timeout=60) for launch in first]
                events = recorder.snapshot()
                row['events'] = {'create': count_status(events, 'create', base),
                                 'start': count_status(events, 'start', base),
                                 'statuses': _status_counts(events[base:])}
                row['topology'] = _topology(ctx, env_id, 3, 3)
                row['metrics_running'] = ctx.metrics(env_id, directory / 'metrics-running.json')
                for launch in second:
                    launch.close_window()
                    launch.wait_removed(timeout=20)
                    launch.wait_exit(timeout=60)
                row['wait_stopped'] = ctx.wait_stopped(env_id, directory / 'stopped')
                row['lease_after'] = ctx.lease_state(env_id)
                row['wait_bridge'] = ctx.wait_bridge_gone(env_id)
                row['timings'] = {
                    'reopen_ready_ms': max((item or {}).get('launch_to_ready_ms') or 0
                                           for item in second_ready),
                    'stop_ms': (row['wait_stopped'] or {}).get('elapsed_ms'),
                }
                failures = row['failures']
                if any(item is None for item in first_ready):
                    failures.append('a window in the first phase of the reopen wave did not become ready')
                if any(item is None for item in second_ready):
                    failures.append('a reopened window did not become ready while the environment was stopping')
                _evaluate_topology(row, ctx, env_id, 3, 3, failures)
                if row['events']['create']:
                    failures.append('container create event happened during the reopen wave')
                if row['events']['start'] > 2:
                    failures.append(f'more than two start events during the reopen wave '
                                    f'({row["events"]["start"]})')
                if not (row['wait_stopped'] or {}).get('ok'):
                    failures.append('environment did not stop after the reopen wave')
                if (row['lease_after'] or {}).get('live_count'):
                    failures.append('leases remain after the reopen wave')
                if not (row['wait_bridge'] or {}).get('ok'):
                    failures.append('bridge remained after the reopen wave')
            except Exception as exc:
                row['error'] = f'{type(exc).__name__}: {exc}'
                row['failures'].append('exception: ' + row['error'])
            finally:
                for launch in launches:
                    row.setdefault('cleanup', []).append(launch.cleanup())
                    try:
                        launch.finish()
                    except Exception:
                        pass
                if recorder is not None:
                    recorder.stop()
                finalize_row(ctx, directory, row, rows)
                scenario_row['waves'].append({'variant': 'reopen-during-stop', 'index': index,
                                              'passed': row['passed'],
                                              'failures': row['failures']})
        # Final residual check: manually start the shared environment, inspect the
        # process table for zombies, then stop it again.
        directory = wave_directory(ctx, 'S7', 'final-check', 0)
        final = {'scenario': 'S7', 'variant': 'final-residual-check',
                 'index': 0, 'started_at': stamp(), 'failures': []}
        try:
            start = ctx.start_env(env_id, directory)
            if start['returncode']:
                raise RuntimeError(f'manual start failed: {start["stderr"]}')
            time.sleep(0.5)
            final['processes'] = ctx.container_ps(env_id, directory / 'processes.txt')
            final['zombie_count'] = final['processes']['zombie_count']
            final['lease_before_stop'] = ctx.lease_state(env_id)
            stop = ctx.stop_env(env_id, directory)
            final['stop'] = stop
            final['wait_stopped'] = ctx.wait_stopped(env_id, directory / 'stopped')
            final['lease_after'] = ctx.lease_state(env_id)
            final['wait_bridge'] = ctx.wait_bridge_gone(env_id)
            final['containers_after'] = ctx.env_containers(env_id)
            if final['zombie_count']:
                final['failures'].append('zombie processes remained after the agile waves')
            if final['lease_before_stop']['live_count']:
                final['failures'].append('live leases without an open window')
            if not (final['wait_stopped'] or {}).get('ok'):
                final['failures'].append('manual final stop did not finish')
            if (final['lease_after'] or {}).get('live_count'):
                final['failures'].append('leases remain after the final check')
            if not (final['wait_bridge'] or {}).get('ok'):
                final['failures'].append('bridge remains after the final check')
        except Exception as exc:
            final['error'] = f'{type(exc).__name__}: {exc}'
            final['failures'].append('exception: ' + final['error'])
        finally:
            finalize_row(ctx, directory, final, rows)
            scenario_row['waves'].append({'variant': 'final-residual-check', 'passed': final['passed'],
                                          'failures': final['failures']})
    finally:
        scenario_row['passed'] = (
            len([wave for wave in scenario_row['waves'] if wave['variant'] == 'cold-burst']) == 10
            and len([wave for wave in scenario_row['waves']
                     if wave['variant'] == 'reopen-during-stop']) == 3
            and all(wave['passed'] for wave in scenario_row['waves']))
        scenario_row['finished_at'] = stamp()
        save(ctx.run / 'samples/S7/scenario.json', scenario_row)

# ------------------------------------------------------------- summary/report
def collect_rows(run: Path) -> list:
    rows = []
    for path in sorted((run / 'samples').rglob('result.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, dict) and 'scenario' in payload:
            rows.append(payload)
    return rows


def collect_scenarios(run: Path) -> dict:
    found = {}
    for path in sorted((run / 'samples').rglob('scenario.json')):
        try:
            payload = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, json.JSONDecodeError):
            continue
        if payload.get('scenario'):
            found[payload['scenario']] = payload
    return found


def fmt(value, digits: int = 1) -> str:
    if value is None:
        return '—'
    if isinstance(value, (int, float)):
        return f'{value:.{digits}f}'
    return str(value)


def _wave_ready_values(rows) -> list:
    values = []
    for row in rows:
        for item in row.get('ready') or []:
            if isinstance(item, dict) and item.get('launch_to_ready_ms') is not None:
                values.append(item['launch_to_ready_ms'])
    return values


def _metrics_values(rows, section: str, key: str) -> list:
    values = []
    for row in rows:
        data = row.get('metrics_running')
        if isinstance(data, dict):
            if section in {'cgroup', 'processes', 'disk'}:
                value = ((data.get(section) or {}).get(key))
            else:
                value = data.get(key)
            if value is not None:
                values.append(value)
    return values


def build_summary(ctx: Context, rows: list, scenarios: dict) -> dict:
    summary = {
        'schema': 'multilinux.concurrency.summary.v1',
        'generated_at': stamp(),
        'run_id': ctx.run.name,
        'readiness_endpoint': ('container: KWin windowAdded native toplevel plus a non-empty '
                               'Wayland buffer commit from the client; native Konsole: KWin '
                               'windowAdded plus non-empty buffer geometry. Physical display '
                               'presentation is not measured.'),
        'sessions': 'serial GUI waves; only the windows of one designed wave run concurrently',
        'conditions': {},
        'scenarios': {},
    }
    snapshot = ctx.manifest.get('snapshot') or {}
    host_rows = [row.get('host_before') for row in rows
                 if isinstance(row.get('host_before'), dict)]
    if host_rows:
        available = [item['meminfo']['MemAvailable_bytes'] for item in host_rows
                     if item['meminfo'].get('MemAvailable_bytes') is not None]
        summary['conditions']['mem_available_min_bytes'] = min(available) if available else None
        summary['conditions']['memory_some_avg10_max'] = max(
            ((item['pressure'].get('memory.some') or {}).get('avg10', 0)
             for item in host_rows), default=None)
        summary['conditions']['memory_full_avg10_max'] = max(
            ((item['pressure'].get('memory.full') or {}).get('avg10', 0)
             for item in host_rows), default=None)
        loads = [item['load_average'][0] for item in host_rows
                 if isinstance(item.get('load_average'), list) and item['load_average']]
        summary['conditions']['load_1min_max'] = max(loads) if loads else None
        summary['conditions']['host_snapshots'] = len(host_rows)
    summary['conditions']['container_creation_seconds'] = {
        env: (item or {}).get('creation_seconds')
        for env, item in (ctx.manifest.get('environments') or {}).items()}

    s1_rows = [row for row in rows if row['scenario'] == 'S1']
    s1_cold = [row for row in s1_rows if row.get('variant') == 'cold']
    s1_warm = [row for row in s1_rows if row.get('variant') == 'warm']
    summary['scenarios']['S1'] = {
        'label': 'shared topology on one Debian environment',
        'cold_attempts': len(s1_cold),
        'cold_passed': sum(1 for row in s1_cold if row['passed']),
        'warm_attempts': len(s1_warm),
        'warm_passed': sum(1 for row in s1_warm if row['passed']),
        'metrics': {
            'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms')
                                    for row in s1_cold]),
            'stop_ms': stats([(row.get('timings') or {}).get('stop_ms') for row in s1_cold]),
        },
        'partial_close_keeps_running': sum(
            1 for row in s1_cold
            if ((row.get('partial_close') or {}).get('env_state') or {}).get('inspect', {}).get('running')),
        'single_container_waves': sum(
            1 for row in s1_rows if (row.get('topology') or {}).get('container_count') == 1),
        'create_events': sum((row.get('events') or {}).get('create', 0) for row in s1_rows),
        'start_events_cold': [((row.get('events') or {}).get('start')
                               if row.get('variant') == 'cold' else
                               (row.get('cold_start_events'))) for row in s1_rows],
        'extra_start_events_warm': [row.get('extra_start_events') for row in s1_warm],
        'warm_first_ready_ms': stats([
            ((row.get('first_ready') or {}).get('launch_to_ready_ms')) for row in s1_warm]),
        'warm_extra_ready_ms': stats([
            (item or {}).get('launch_to_ready_ms')
            for row in s1_warm for item in (row.get('extra_ready') or [])]),
        'warm_note': ('Warm waves verify that opening extra windows in an already running '
                      'environment adds no container start event and shares the same single '
                      'container; their readiness values are reported separately and are not '
                      'used as cold-start performance samples.'),
        'failures': [{'variant': row.get('variant'), 'index': row.get('index'),
                      'failures': row.get('failures')} for row in s1_rows if not row['passed']],
    }

    s2_rows = [row for row in rows if row['scenario'] == 'S2']
    s2 = {}
    for scale in (1, 2, 3, 4):
        selected = [row for row in s2_rows if row.get('scale') == scale]
        valid = [row for row in selected if row['passed']]
        s2[f'N{scale}'] = {
            'attempts': len(selected), 'passed': len(valid),
            'ready_ms': stats(_wave_ready_values(valid)),
            'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms')
                                    for row in valid]),
            'stop_ms': stats([(row.get('timings') or {}).get('stop_ms') for row in valid]),
            'cgroup_memory_current_bytes': stats(_metrics_values(valid, 'cgroup',
                                                                 'memory_current_bytes')),
            'cgroup_memory_peak_bytes': stats(_metrics_values(valid, 'cgroup',
                                                              'memory_peak_bytes')),
            'pss_kb': stats(_metrics_values(valid, 'processes', 'pss_kb')),
            'size_rw_bytes': stats(_metrics_values(valid, 'disk', 'size_rw_bytes')),
            'container_count': [((row.get('topology') or {}).get('container_count'))
                                for row in selected],
            'failures': [{'index': row.get('index'), 'failures': row.get('failures')}
                         for row in selected if not row['passed']],
        }
    summary['scenarios']['S2'] = s2

    s3_rows = [row for row in rows if row['scenario'] == 'S3']
    summary['scenarios']['S3'] = {
        'attempts': len(s3_rows), 'passed': sum(1 for row in s3_rows if row['passed']),
        'ready_ms': stats(_wave_ready_values(s3_rows)),
        'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms')
                                for row in s3_rows if row['passed']]),
        'stop_ms': stats([(row.get('timings') or {}).get('stop_ms')
                          for row in s3_rows if row['passed']]),
        'running_containers': [len(row.get('running_containers') or []) for row in s3_rows],
        'failures': [{'index': row.get('index'), 'failures': row.get('failures')}
                     for row in s3_rows if not row['passed']],
    }

    s4_rows = [row for row in rows if row['scenario'] == 'S4']
    s4_pss = []
    for row in s4_rows:
        s4_pss.extend([value for value in (row.get('pss') or []) if value is not None])
    summary['scenarios']['S4'] = {
        'attempts': len(s4_rows), 'passed': sum(1 for row in s4_rows if row['passed']),
        'native_ready_ms': stats(_wave_ready_values(s4_rows)),
        'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms')
                                for row in s4_rows if row['passed']]),
        'close_ms': stats([(row.get('timings') or {}).get('close_ms')
                           for row in s4_rows if row['passed']]),
        'native_pss_kb': stats(s4_pss),
        'host_pids_unique': [row.get('host_pids_unique') for row in s4_rows],
        'containers_changed': [
            bool((row.get('containers_during') or []) != (row.get('containers_before') or []))
            for row in s4_rows],
        'failures': [{'index': row.get('index'), 'failures': row.get('failures')}
                     for row in s4_rows if not row['passed']],
    }

    s5_rows = [row for row in rows if row['scenario'] == 'S5']
    summary['scenarios']['S5'] = {
        'attempts': len(s5_rows), 'passed': sum(1 for row in s5_rows if row['passed']),
        'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms')
                                for row in s5_rows if row['passed']]),
        'running_containers': [len(row.get('running_containers') or []) for row in s5_rows],
        'host_pids_unique': [row.get('host_pids_unique') for row in s5_rows],
        'failures': [{'index': row.get('index'), 'failures': row.get('failures')}
                     for row in s5_rows if not row['passed']],
    }

    s6_rows = [row for row in rows if row['scenario'] == 'S6']
    s6_totals = {'memory_current_bytes': [], 'memory_peak_bytes': [], 'pss_kb': [],
                 'size_rw_bytes': [], 'process_count': [], 'mousepad_count': []}
    for row in s6_rows:
        totals = row.get('totals_running') or {}
        for key in s6_totals:
            if totals.get(key) is not None:
                s6_totals[key].append(totals[key])
    summary['scenarios']['S6'] = {
        'attempts': len(s6_rows), 'passed': sum(1 for row in s6_rows if row['passed']),
        'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms')
                                for row in s6_rows if row['passed']]),
        'stop_ms': stats([(row.get('timings') or {}).get('stop_ms')
                          for row in s6_rows if row['passed']]),
        'running_containers': [len(row.get('running_containers') or []) for row in s6_rows],
        'totals_running': {key: stats(values) for key, values in s6_totals.items()},
        'failures': [{'index': row.get('index'), 'failures': row.get('failures')}
                     for row in s6_rows if not row['passed']],
    }

    s7_rows = [row for row in rows if row['scenario'] == 'S7']
    s7_burst = [row for row in s7_rows if row.get('variant') == 'cold-burst']
    s7_reopen = [row for row in s7_rows if row.get('variant') == 'reopen-during-stop']
    s7_final = [row for row in s7_rows if row.get('variant') == 'final-residual-check']
    summary['scenarios']['S7'] = {
        'agile_waves_attempts': len(s7_burst),
        'agile_waves_passed': sum(1 for row in s7_burst if row['passed']),
        'ready_ms': stats(_wave_ready_values(s7_burst)),
        'wave_ready_ms': stats([(row.get('timings') or {}).get('wave_ready_ms') for row in s7_burst]),
        'stop_ms': stats([(row.get('timings') or {}).get('stop_ms')
                          for row in s7_burst if row['passed']]),
        'reopen_waves_attempts': len(s7_reopen),
        'reopen_waves_passed': sum(1 for row in s7_reopen if row['passed']),
        'reopen_ready_ms': stats([(row.get('timings') or {}).get('reopen_ready_ms')
                                  for row in s7_reopen if row['passed']]),
        'reopen_started_after_close_ms': stats([row.get('reopen_started_after_close_ms')
                                               for row in s7_reopen]),
        'final_residual_check': [{
            'zombie_count': row.get('zombie_count'),
            'lease_before_stop': (row.get('lease_before_stop') or {}).get('live_count'),
            'passed': row.get('passed'),
        } for row in s7_final],
        'failures': [{'variant': row.get('variant'), 'index': row.get('index'),
                      'failures': row.get('failures')} for row in s7_rows if not row['passed']],
    }

    # Shared (S2 N=3) versus one-environment-per-process (S6) comparison. Both use
    # the same Debian image; values are descriptive, no fabricated thresholds.
    shared = s2.get('N3') or {}
    separate = summary['scenarios']['S6'].get('totals_running') or {}
    comparison = {
        'shared_N3': {
            'containers': 1,
            'wave_ready_ms': shared.get('wave_ready_ms'),
            'cgroup_memory_current_bytes': shared.get('cgroup_memory_current_bytes'),
            'cgroup_memory_peak_bytes': shared.get('cgroup_memory_peak_bytes'),
            'pss_kb': shared.get('pss_kb'),
            'size_rw_bytes': shared.get('size_rw_bytes'),
        },
        'separate_3_envs': {
            'containers': 3,
            'wave_ready_ms': summary['scenarios']['S6'].get('wave_ready_ms'),
            'cgroup_memory_current_bytes': separate.get('memory_current_bytes'),
            'cgroup_memory_peak_bytes': separate.get('memory_peak_bytes'),
            'pss_kb': separate.get('pss_kb'),
            'size_rw_bytes': separate.get('size_rw_bytes'),
        },
    }
    for key in ('wave_ready_ms', 'cgroup_memory_current_bytes',
                'cgroup_memory_peak_bytes', 'pss_kb', 'size_rw_bytes'):
        shared_mean = (comparison['shared_N3'].get(key) or {}).get('mean')
        separate_mean = (comparison['separate_3_envs'].get(key) or {}).get('mean')
        comparison[f'separate_over_shared_{key}'] = (
            round(separate_mean / shared_mean, 3)
            if shared_mean and separate_mean is not None else None)
    summary['scenarios']['S6']['comparison'] = comparison

    cleanup_path = ctx.run / 'postconditions.json'
    summary['cleanup'] = (json.loads(cleanup_path.read_text(encoding='utf-8'))
                          if cleanup_path.is_file() else None)
    summary['snapshot'] = {
        'git_head': snapshot.get('git', {}).get('head'),
        'git_branch': snapshot.get('git', {}).get('branch'),
        'git_dirty': snapshot.get('git', {}).get('dirty'),
        'snapshot_sha256': snapshot.get('snapshot_sha256'),
    }
    return summary


def build_validation(ctx: Context, summary: dict, rows: list, scenarios: dict) -> dict:
    s = summary['scenarios']
    failures = []
    criteria = {}

    def record(name: str, passed: bool, detail: dict) -> None:
        criteria[name] = {'passed': bool(passed), 'detail': detail}
        if not passed:
            failures.append(name)

    def rows_passed(scenario: str, variants=None) -> bool:
        selected = [row for row in rows if row['scenario'] == scenario
                    and (variants is None or row.get('variant') in variants)]
        return bool(selected) and all(row['passed'] for row in selected)

    def gate(scenario: str, passed: bool, variants=None) -> bool:
        return bool(passed) and rows_passed(scenario, variants)

    s1_rows = [row for row in rows if row['scenario'] == 'S1']
    shared_container_rows = [row for row in rows
                             if row['scenario'] in {'S1', 'S2', 'S7'}
                             and row.get('variant') != 'final-residual-check']
    shared_container_ok = all(
        (row.get('topology') or {}).get('container_count') == 1
        and (row.get('topology') or {}).get('container_id_matches')
        for row in shared_container_rows)
    record('shared_single_container',
           gate('S1', shared_container_ok) and gate('S2', shared_container_ok)
           and gate('S7', shared_container_ok),
           {'waves': len(shared_container_rows),
            'bad': [{'scenario': row['scenario'], 'index': row.get('index'),
                     'topology': row.get('topology')}
                    for row in shared_container_rows
                    if not ((row.get('topology') or {}).get('container_count') == 1
                            and (row.get('topology') or {}).get('container_id_matches'))]})
    create_events = []
    for row in rows:
        events = row.get('events')
        if isinstance(events, dict):
            if 'create' in events and isinstance(events['create'], (int, float)):
                create_events.append(events['create'])
            else:
                for value in events.values():
                    if isinstance(value, dict) and 'create' in value:
                        create_events.append(value['create'])
    record('no_create_events_after_prepare', all(value == 0 for value in create_events),
           {'recorded_create_events': sum(create_events), 'waves_with_events': len(create_events)})
    warm_rows = [row for row in s1_rows if row.get('variant') == 'warm']
    record('warm_extra_launch_no_extra_start',
           bool(warm_rows) and all(row.get('extra_start_events') == 0 for row in warm_rows)
           and all(row.get('cold_start_events') == 1 for row in warm_rows)
           and rows_passed('S1', {'warm'}),
           {'waves': len(warm_rows),
            'extra_start_events': [row.get('extra_start_events') for row in warm_rows],
            'cold_start_events': [row.get('cold_start_events') for row in warm_rows]})
    topology_rows = [row for row in rows
                     if row['scenario'] in {'S1', 'S2', 'S3', 'S5', 'S6', 'S7'}]
    topology_ok = True
    bad_topology = []
    for row in topology_rows:
        topology = row.get('topology')
        if not isinstance(topology, dict):
            continue
        groups = [topology] if 'leases' in topology else [
            value for value in topology.values() if isinstance(value, dict) and 'leases' in value]
        for group in groups:
            if (group.get('leases') != group.get('expected_leases')
                    or group.get('mousepad_processes') != group.get('expected_mousepad')):
                topology_ok = False
                bad_topology.append({'scenario': row['scenario'], 'index': row.get('index'),
                                     'group': group})
    record('leases_and_process_counts', topology_ok and all(
        rows_passed(name) for name in ('S1', 'S2', 'S3', 'S5', 'S6', 'S7')),
           {'waves_checked': len(topology_rows), 'bad': bad_topology[:10]})
    partial_ok = all(
        ((row.get('partial_close') or {}).get('env_state') or {}).get('inspect', {}).get('running')
        is True
        and ((row.get('partial_close') or {}).get('lease') or {}).get('live_count') == 2
        for row in [r for r in s1_rows if r.get('variant') == 'cold'])
    record('partial_close_keeps_running', partial_ok and rows_passed('S1', {'cold'}),
           {'cold_waves': len([r for r in s1_rows if r.get('variant') == 'cold'])})
    stop_checks = []
    stop_bad = []
    for row in rows:
        stopped = row.get('wait_stopped')
        if not isinstance(stopped, dict):
            continue
        if 'ok' in stopped:
            groups = [stopped]
        else:
            groups = [value for value in stopped.values() if isinstance(value, dict)]
        for group in groups:
            stop_checks.append(bool(group.get('ok')))
            if not group.get('ok'):
                stop_bad.append({'scenario': row['scenario'], 'index': row.get('index')})
    record('last_close_stops', bool(stop_checks) and all(stop_checks)
           and all(rows_passed(name) for name in ('S1', 'S2', 'S3', 'S5', 'S6', 'S7')),
           {'checks': len(stop_checks), 'passed': sum(1 for value in stop_checks if value),
            'bad': stop_bad[:10]})
    s3_rows = [row for row in rows if row['scenario'] == 'S3']
    two_ok = bool(s3_rows) and all(len(row.get('running_containers') or []) == 2
                                   for row in s3_rows)
    record('two_distro_two_containers', gate('S3', two_ok),
           {'waves': len(s3_rows),
            'running_containers': [len(row.get('running_containers') or []) for row in s3_rows]})
    s4_rows = [row for row in rows if row['scenario'] == 'S4']
    native_ok = bool(s4_rows) and all(
        (row.get('containers_during') or []) == (row.get('containers_before') or [])
        and (row.get('env_records_during') or []) == (row.get('env_records_before') or [])
        and not any(count for count in (row.get('lease_during') or {}).values())
        and row.get('host_pids_unique') is True
        for row in s4_rows)
    record('native_no_container', gate('S4', native_ok),
           {'waves': len(s4_rows),
            'host_pids_unique': [row.get('host_pids_unique') for row in s4_rows],
            'containers_changed': [
                (row.get('containers_during') or []) != (row.get('containers_before') or [])
                for row in s4_rows]})
    s5_rows = [row for row in rows if row['scenario'] == 'S5']
    mixed_ok = bool(s5_rows) and all(len(row.get('running_containers') or []) == 2
                                     for row in s5_rows)
    record('mixed_two_containers', gate('S5', mixed_ok),
           {'waves': len(s5_rows),
            'running_containers': [len(row.get('running_containers') or []) for row in s5_rows]})
    s6_rows = [row for row in rows if row['scenario'] == 'S6']
    separate_ok = bool(s6_rows) and all(
        len(row.get('running_containers') or []) == 3
        and len({(row.get('topology') or {}).get(env, {}).get('container_id') for env in CONTROL_ENVS}) == 3
        and all((row.get('topology') or {}).get(env, {}).get('container_count') == 1
                for env in CONTROL_ENVS)
        for row in s6_rows)
    record('separate_envs_topology', gate('S6', separate_ok),
           {'waves': len(s6_rows),
            'running_containers': [len(row.get('running_containers') or []) for row in s6_rows],
            'topology': [row.get('topology') for row in s6_rows]})
    agile_waves = s['S7']['agile_waves_passed']
    agile_ok = agile_waves == 10
    record('agile_waves', agile_ok,
           {'passed': agile_waves, 'attempts': s['S7']['agile_waves_attempts'],
            'ready_ms': s['S7']['ready_ms'], 'stop_ms': s['S7']['stop_ms'],
            'stop_p95_below_reference': (
                (s['S7']['stop_ms'] or {}).get('p95') is not None
                and (s['S7']['stop_ms'] or {}).get('p95') <= STOP_REF_MS),
            'reference_stop_p95_ms': STOP_REF_MS})
    reopen = s['S7']
    reopen_ok = (reopen['reopen_waves_passed'] == 3
                 and (reopen.get('reopen_ready_ms') or {}).get('p95') is not None
                 and (reopen.get('reopen_ready_ms') or {}).get('p95') <= REOPEN_REF_MS)
    record('reopen_during_stop', reopen_ok,
           {'passed': reopen['reopen_waves_passed'],
            'attempts': reopen['reopen_waves_attempts'],
            'reopen_ready_ms': reopen.get('reopen_ready_ms'),
            'reopen_started_after_close_ms': reopen.get('reopen_started_after_close_ms'),
            'reference_reopen_p95_ms': REOPEN_REF_MS})
    designed = {'S1': 7, 'S2': 12, 'S3': 3, 'S4': 3, 'S5': 2, 'S6': 3, 'S7': 14}
    scenario_wave_failures = {}
    all_waves_passed = True
    for scenario, count in designed.items():
        selected = [row for row in rows if row['scenario'] == scenario]
        failed = [{'variant': row.get('variant'), 'index': row.get('index'),
                   'failures': row.get('failures')} for row in selected if not row['passed']]
        scenario_wave_failures[scenario] = {
            'designed_waves': count, 'recorded_waves': len(selected),
            'failed_waves': failed,
            'passed': bool(selected) and len(selected) == count and not failed,
        }
        all_waves_passed = all_waves_passed and scenario_wave_failures[scenario]['passed']
    record('all_designed_waves_passed', all_waves_passed,
           {'per_scenario': scenario_wave_failures,
            'designed_total': sum(designed.values()),
            'recorded_total': sum(item['recorded_waves'] for item in scenario_wave_failures.values()),
            'failed_total': sum(len(item['failed_waves']) for item in scenario_wave_failures.values())})
    scenario_wave_failures_summary = scenario_wave_failures
    post = summary.get('cleanup') or {}
    record('cleanup_no_residue', bool(post.get('all_clean')), post)
    expected = {'S1': 7, 'S2': 12, 'S3': 3, 'S4': 3, 'S5': 2, 'S6': 3, 'S7': 14}
    counts = {scenario: len([row for row in rows if row['scenario'] == scenario])
              for scenario in SCENARIOS}
    measurement_valid = all(counts.get(name) == expected[name] for name in SCENARIOS)
    product_verdict = ('concurrency-verified' if (measurement_valid and not failures)
                       else 'needs-concurrency-fixes')
    return {
        'schema': 'multilinux.concurrency.validation.v1',
        'generated_at': stamp(),
        'measurement_valid': measurement_valid,
        'product_verdict': product_verdict,
        'wave_counts': counts,
        'expected_wave_counts': expected,
        'criteria': criteria,
        'scenario_wave_failures': scenario_wave_failures_summary,
        'failures': failures,
        'limitations': [
            'The native side is KDE Konsole on Fedora 44; the container side is native Mousepad in Debian 13 / Arch. Different software stacks and toolkits: the numbers are not an application ranking and only describe this host.',
            'Readiness for containers is KWin windowAdded + non-empty Wayland buffer commit; for native Konsole it is windowAdded + non-empty buffer geometry because host WAYLAND_DEBUG logging is not enabled. Physical display presentation is not measured.',
            'Container memory uses cgroup memory.current/memory.peak plus in-container PSS; native memory uses host PSS of the Konsole process tree. PSS and cgroup accounting are different views and are not summed together.',
            'The host is a shared daily-use Fedora 44 KDE machine with no frequency locking; absolute timings are descriptive (p50/p95), not an application benchmark.',
            'Only Debian 13 and Arch were exercised; Fedora container concurrency and cross-host (M5) behaviour are not claimed.',
        ],
    }


def render_report(ctx: Context, summary: dict, validation: dict) -> str:
    lines = []
    add = lines.append
    snapshot = summary.get('snapshot') or {}
    conditions = summary.get('conditions') or {}
    s = summary['scenarios']
    add('# 并发测试：共享环境、双发行版 + 母发行版、性能与敏捷启停')
    add('')
    add(f"运行目录：{summary['run_id']}，生成时间 {stamp()}。宿主 Fedora 44 KDE / KWin 6.7.5，"
        f"rootless Podman，基线提交 {snapshot.get('git_head')}"
        f"（分支 {snapshot.get('git_branch')}，工作树{'脏' if snapshot.get('git_dirty') else '干净'}）。"
        '本轮只新增测量工具与新证据目录；产品代码由 project/ 冻结快照决定，未改写已有证据。')
    add('')
    verdict = validation.get('product_verdict')
    add('**判定：' + ('concurrency-verified，并发拓扑、资源与敏捷启停在该场景集上成立。'
                     if verdict == 'concurrency-verified'
                     else 'needs-concurrency-fixes，仍有失败项，详见下文。') + '**')
    add('')
    add(f"机器核验：measurement_valid={str(validation.get('measurement_valid')).lower()}，"
        f"product_verdict={verdict}。判定标准见 validation.json。")
    add('')
    add('## 方法与矩阵')
    add('')
    add('五个独立环境：conc-debian（Debian 13）、conc-arch（Arch）、conc-sep-1/2/3（Debian，'
        '每进程一套环境的对照）。均 --network none --desktop --portals --userns keep-id，'
        '各注册一个 on-demand Mousepad（as_host_user、GDK_BACKEND=wayland、WAYLAND_DEBUG=client、'
        'LC_ALL=C.UTF-8）。镜像来自上一轮 startup-* 本地摘要，未刷新、未下载。')
    add('母发行版侧为宿主 Fedora 44 原生 Konsole：每个窗口独立进程 '
        'konsole --separate -p tabtitle=<marker> -e sleep 300，关闭即 kill 该进程；不安装宿主包。')
    add('- 就绪口径按软件栈分别定义并写入 validation 限制：容器 Mousepad 为 KWin windowAdded + '
        '客户端非空 Wayland buffer commit；宿主原生 Konsole 为 KWin windowAdded + 非空 '
        'frameGeometry（混合并发下其 bufferWidth/Height 可能为 0，因此不把 buffer geometry '
        '当作原生就绪前提；KWin 几何查询间隔 100 ms，避免生成大量临时脚本）。')
    add('')
    add('| 场景 | 设计 | waves | 判定 |')
    add('|---|---|---:|---|')
    add(f"| S1 共享拓扑 | 1 个 Debian 环境并发 3 窗口；另 2 wave 热追加 2 窗口 | "
        f"{s['S1']['cold_attempts']}+{s['S1']['warm_attempts']} | "
        f"{s['S1']['cold_passed']}/{s['S1']['cold_attempts']} + "
        f"{s['S1']['warm_passed']}/{s['S1']['warm_attempts']} |")
    add(f"| S2 共享规模 | Debian N=1/2/3/4，各 3 wave | {sum((s['S2'][f'N{n}']['attempts']) for n in (1,2,3,4))} | "
        + ', '.join(f"N{n}:{s['S2'][f'N{n}']['passed']}/{s['S2'][f'N{n}']['attempts']}" for n in (1, 2, 3, 4))
        + ' |')
    add(f"| S3 双发行版 | Debian 3 + Arch 3 同时 | {s['S3']['attempts']} | "
        f"{s['S3']['passed']}/{s['S3']['attempts']} |")
    add(f"| S4 母发行版原生 | 3 个 Konsole 窗口并发 | {s['S4']['attempts']} | "
        f"{s['S4']['passed']}/{s['S4']['attempts']} |")
    add(f"| S5 混合 | 原生 2 + Debian 2 + Arch 2 | {s['S5']['attempts']} | "
        f"{s['S5']['passed']}/{s['S5']['attempts']} |")
    add(f"| S6 每进程一套环境 | conc-sep-1/2/3 各 1 窗口 | {s['S6']['attempts']} | "
        f"{s['S6']['passed']}/{s['S6']['attempts']} |")
    add(f"| S7 敏捷并发 | 10 wave × 3 并发首启 + 3 次停止中重开 + 终检 | "
        f"{s['S7']['agile_waves_attempts']}+{s['S7']['reopen_waves_attempts']} | "
        f"{s['S7']['agile_waves_passed']}/{s['S7']['agile_waves_attempts']} + "
        f"{s['S7']['reopen_waves_passed']}/{s['S7']['reopen_waves_attempts']} |")
    add('')
    wave_report = validation.get('scenario_wave_failures') or {}
    add('波次判定（validation.all_designed_waves_passed）：'
        + '；'.join(f"{name} {item.get('recorded_waves')}/{item.get('designed_waves')}"
                   + (f"，失败 {len(item.get('failed_waves') or [])} wave"
                      if item.get('failed_waves') else '')
                   for name, item in wave_report.items())
        + f"。失败总数 {sum(len(item.get('failed_waves') or []) for item in wave_report.values())}。")
    add('')
    add('## S1 共享环境拓扑（Debian，3 窗口并发）')
    add('')
    add(f"- 冷波次 {s['S1']['cold_passed']}/{s['S1']['cold_attempts']} 通过：每 wave 容器数 1、"
        f"容器 ID 与 prepare 记录一致、create 事件 0、start 事件 1、live lease 3、容器内 Mousepad 进程 3。")
    add(f"- 关 1 个后环境仍 running、lease=2 的 wave 数：{s['S1']['partial_close_keeps_running']}/"
        f"{s['S1']['cold_attempts']}；关最后一个后 stopped、lease=0。")
    add(f"- wave ready p50/p95：{fmt((s['S1']['metrics']['wave_ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S1']['metrics']['wave_ready_ms'] or {}).get('p95'))} ms；"
        f"关最后一个到容器停止 p50/p95：{fmt((s['S1']['metrics']['stop_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S1']['metrics']['stop_ms'] or {}).get('p95'))} ms。")
    add(f"- 热追加 2 窗口 {s['S1']['warm_passed']}/{s['S1']['warm_attempts']} 通过："
        f"额外 start 事件 {s['S1']['extra_start_events_warm']}，lease=3，"
        f"单容器 ID 恒定。")
    add(f"- S1 warm 的设计目标是验证“已运行环境追加窗口不新增 start 事件”，其增量窗口就绪 "
        f"p50/p95 为 {fmt((s['S1'].get('warm_extra_ready_ms') or {}).get('p50'))}/"
        f"{fmt((s['S1'].get('warm_extra_ready_ms') or {}).get('p95'))} ms（首窗口 "
        f"{fmt((s['S1'].get('warm_first_ready_ms') or {}).get('p50'))} ms）；"
        '这些数字不作为冷启动性能样本，也不单独构成性能阈值。')
    add('')
    add('## S2 共享规模 N=1..4（每 N 3 wave，cold）')
    add('')
    add('| N | 通过 | 单窗口就绪 p50/p95 | wave 就绪 p50/p95 | 停止 p50/p95 | cgroup memory.current p50 | peak p50 | PSS p50 | SizeRw p50 |')
    add('|---:|---|---:|---:|---:|---:|---:|---:|---:|')
    for n in (1, 2, 3, 4):
        item = s['S2'][f'N{n}']
        add(f"| {n} | {item['passed']}/{item['attempts']} | "
            f"{fmt((item['ready_ms'] or {}).get('p50'))}/{fmt((item['ready_ms'] or {}).get('p95'))} | "
            f"{fmt((item['wave_ready_ms'] or {}).get('p50'))}/{fmt((item['wave_ready_ms'] or {}).get('p95'))} | "
            f"{fmt((item['stop_ms'] or {}).get('p50'))}/{fmt((item['stop_ms'] or {}).get('p95'))} | "
            f"{fmt(((item['cgroup_memory_current_bytes'] or {}).get('p50') or 0) / 1048576)} MiB | "
            f"{fmt(((item['cgroup_memory_peak_bytes'] or {}).get('p50') or 0) / 1048576)} MiB | "
            f"{fmt((item['pss_kb'] or {}).get('p50'))} kB | "
            f"{fmt(((item['size_rw_bytes'] or {}).get('p50') or 0) / 1024)} KiB |")
    add('')
    add('## S3 双发行版并发（Debian 3 + Arch 3）')
    add('')
    add(f"- 通过 {s['S3']['passed']}/{s['S3']['attempts']}；wave ready p50/p95 "
        f"{fmt((s['S3']['wave_ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S3']['wave_ready_ms'] or {}).get('p95'))} ms；"
        f"停止 p50/p95 {fmt((s['S3']['stop_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S3']['stop_ms'] or {}).get('p95'))} ms。")
    add(f"- 运行中项目容器数（每 wave）：{s['S3']['running_containers']}；每个环境 1 个容器、3 个 lease、"
        '3 个容器内 Mousepad 进程；create 事件 0。')
    add('')
    add('## S4 母发行版原生（3 个 Konsole 窗口）')
    add('')
    add(f"- 通过 {s['S4']['passed']}/{s['S4']['attempts']}；native 就绪 p50/p95 "
        f"{fmt((s['S4']['native_ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S4']['native_ready_ms'] or {}).get('p95'))} ms；"
        f"关闭到窗口移除 p50/p95 {fmt((s['S4']['close_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S4']['close_ms'] or {}).get('p95'))} ms。")
    add(f"- 3 个独立宿主 PID：{s['S4']['host_pids_unique']}；项目容器集合与环境记录前后不变："
        f"{[not changed for changed in s['S4']['containers_changed']]}；未产生 lease。")
    add(f"- 宿主 Konsole 进程树 PSS p50/p95：{fmt((s['S4']['native_pss_kb'] or {}).get('p50'))}/"
        f"{fmt((s['S4']['native_pss_kb'] or {}).get('p95'))} kB。")
    add('')
    add('## S5 混合（原生 2 + Debian 2 + Arch 2）')
    add('')
    add(f"- 通过 {s['S5']['passed']}/{s['S5']['attempts']}；wave ready p50/p95 "
        f"{fmt((s['S5']['wave_ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S5']['wave_ready_ms'] or {}).get('p95'))} ms；"
        f"运行中项目容器数：{s['S5']['running_containers']}；native 独立 PID：{s['S5']['host_pids_unique']}。")
    add('')
    add('## S6 每进程一套环境对照（3 个 Debian 环境各 1 窗口）')
    add('')
    add(f"- 通过 {s['S6']['passed']}/{s['S6']['attempts']}；wave ready p50/p95 "
        f"{fmt((s['S6']['wave_ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S6']['wave_ready_ms'] or {}).get('p95'))} ms；"
        f"停止 p50/p95 {fmt((s['S6']['stop_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S6']['stop_ms'] or {}).get('p95'))} ms。")
    comparison = s['S6'].get('comparison') or {}
    shared = comparison.get('shared_N3') or {}
    separate = comparison.get('separate_3_envs') or {}
    add('')
    add('| 指标（描述性） | 共享 1 环境 N=3 | 每进程 3 环境 | 3 环境 / 共享 |')
    add('|---|---:|---:|---:|')
    for key, label in (('wave_ready_ms', 'wave 就绪 p50 (ms)'),
                       ('cgroup_memory_current_bytes', 'cgroup memory.current p50 (MiB)'),
                       ('cgroup_memory_peak_bytes', 'cgroup memory.peak p50 (MiB)'),
                       ('pss_kb', 'PSS p50 (kB)'),
                       ('size_rw_bytes', 'SizeRw p50 (KiB)')):
        shared_stats = shared.get(key) or {}
        separate_stats = separate.get(key) or {}
        shared_value = shared_stats.get('p50')
        separate_value = separate_stats.get('p50')
        if key in {'cgroup_memory_current_bytes', 'cgroup_memory_peak_bytes'}:
            shared_text = fmt((shared_value or 0) / 1048576)
            separate_text = fmt((separate_value or 0) / 1048576)
        elif key == 'size_rw_bytes':
            shared_text = fmt((shared_value or 0) / 1024)
            separate_text = fmt((separate_value or 0) / 1024)
        else:
            shared_text = fmt(shared_value)
            separate_text = fmt(separate_value)
        ratio = comparison.get(f'separate_over_shared_{key}')
        add(f"| {label} | {shared_text} | {separate_text} | {fmt(ratio, 2)}x |")
    add('')
    add('- 该对照只用于回答“每进程一套环境”与共享环境的资源/启动差异，不是产品默认路径；'
        '3 套环境同时启动会额外产生 3 个容器、3 个桥接与 3 份 writable layer。')
    add('')
    add('## S7 敏捷并发（3 个并发首启 ×10 + 停止中重开 ×3）')
    add('')
    add(f"- 10 个并发首启 wave 通过 {s['S7']['agile_waves_passed']}/{s['S7']['agile_waves_attempts']}；"
        f"单窗口就绪 p50/p95 {fmt((s['S7']['ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S7']['ready_ms'] or {}).get('p95'))} ms；"
        f"停止 p50/p95 {fmt((s['S7']['stop_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S7']['stop_ms'] or {}).get('p95'))} ms（参考阈值 stop p95 < "
        f"{fmt(STOP_REF_MS, 0)} ms）。")
    add(f"- 停止中重开 3 个 wave 通过 {s['S7']['reopen_waves_passed']}/{s['S7']['reopen_waves_attempts']}；"
        f"重开就绪 p50/p95 {fmt((s['S7']['reopen_ready_ms'] or {}).get('p50'))}/"
        f"{fmt((s['S7']['reopen_ready_ms'] or {}).get('p95'))} ms（参考 reopen p95 < "
        f"{fmt(REOPEN_REF_MS, 0)} ms）；重开发起时间 "
        f"{s['S7']['reopen_started_after_close_ms']}。")
    add(f"- 每次重开均核对单容器/容器 ID 恒定/3 lease；最终手动进程表僵尸检查："
        f"{s['S7']['final_residual_check']}。")
    add('')
    add('## 宿主条件')
    add('')
    add(f"- 可用内存最低 {fmt((conditions.get('mem_available_min_bytes') or 0) / 1048576)} MiB；"
        f"内存 PSI some avg10 最大 {conditions.get('memory_some_avg10_max')}%，"
        f"full avg10 最大 {conditions.get('memory_full_avg10_max')}%；"
        f"1 分钟 load 最大 {conditions.get('load_1min_max')}；宿主快照 {conditions.get('host_snapshots')} 个。")
    add(f"- 五个环境创建耗时（秒）：{conditions.get('container_creation_seconds')}。")
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
    add(f"- all_clean={str(post.get('all_clean')).lower()}：运行中容器 {post.get('running_containers')}，"
        f"存活租约 {post.get('live_leases')}，桥接进程 {post.get('bridge_processes')}，"
        f"遗留窗口 {post.get('remaining_windows')}，遗留 ml.py/worker {post.get('leftover_processes')}，"
        f"遗留实验 Konsole 进程 {post.get('leftover_native_processes')}。")
    add(f"- 宿主包清单哈希前后一致：{post.get('host_packages_unchanged')}；"
        f"桌面入口清单哈希前后一致：{post.get('host_desktop_entries_unchanged')}。")
    add('- 五个自建环境均停止并保留以便复现；未触碰既有 m3debian/wemeet/startup-*/gui-*/complex-arch 等环境。')
    add('')
    add('## 限制')
    add('')
    for item in validation.get('limitations') or []:
        add(f'- {item}')
    add('')
    add('## 证据与复现')
    add('')
    add('- 冻结源码与配置：project/（逐文件 SHA-256 与 git 状态见 manifest.json 的 snapshot）。')
    add('- 原始样本：samples/S1..S7/<wave>/{result.json,window-*,native-*,events.jsonl,metrics-*.json,...}。')
    add('- 机器判定：validation.json；汇总：summary.json；残留核对：postconditions.json；审计哈希：SHA256SUMS。')
    add('')
    add('    python3 tools/prepare_concurrency.py snapshot --run-dir <new-evidence-dir>')
    add('    python3 tools/prepare_concurrency.py create --run-dir <new-evidence-dir>')
    add('    python3 tools/probe_concurrency.py run --run-dir <new-evidence-dir>')
    add('    python3 tools/probe_concurrency.py summary --run-dir <new-evidence-dir>')
    add('')
    return '\n'.join(lines) + '\n'


def summarize(ctx: Context, rows: list, scenarios: dict) -> dict:
    summary = build_summary(ctx, rows, scenarios)
    save(ctx.run / 'summary.json', summary)
    validation = build_validation(ctx, summary, rows, scenarios)
    save(ctx.run / 'validation.json', validation)
    (ctx.run / 'report.md').write_text(render_report(ctx, summary, validation),
                                       encoding='utf-8')
    return {'summary': summary, 'validation': validation}


# ------------------------------------------------------------------- cleanup
def cleanup_run(ctx: Context, started_at: str) -> dict:
    from tools.prepare_concurrency import host_desktop_entries_hash, host_packages_hash
    snapshot = ctx.manifest.get('snapshot') or {}
    windows = ctx.remaining_windows(ctx.run / 'observer/final-window-check')
    closed = []
    for window in windows:
        try:
            ctx.observer.close_window(PREFIX, window.get('internalId'))
            closed.append({'internalId': window.get('internalId'),
                           'caption': window.get('caption')})
        except Exception as exc:
            closed.append({'internalId': window.get('internalId'),
                           'error': f'{type(exc).__name__}: {exc}'})
    wait_for(lambda: not ctx.remaining_windows(ctx.run / 'observer/final-window-check-2'),
             10, 0.05)
    native_killed = []
    for item in ctx.native_processes():
        native_killed.append({'pid': item['pid'], 'cmdline': item['cmdline'],
                              'term': kill_pid(item['pid'], signal.SIGTERM)})
    wait_for(lambda: not ctx.native_processes(), 10, 0.05)
    for item in ctx.native_processes():
        native_killed.append({'pid': item['pid'], 'kill': kill_pid(item['pid'], signal.SIGKILL)})
    project_marker = str(ctx.project)
    leftover = []
    for item in proc_table().values():
        if project_marker not in item['cmdline'] or 'tools/ml.py' not in item['cmdline']:
            continue
        if 'app open' in item['cmdline'] or '_worker' in item['cmdline']:
            leftover.append({'pid': item['pid'], 'term': kill_pid(item['pid'], signal.SIGTERM)})
    wait_for(lambda: not [item for item in proc_table().values()
                          if project_marker in item['cmdline']
                          and 'tools/ml.py' in item['cmdline']
                          and ('app open' in item['cmdline'] or '_worker' in item['cmdline'])],
             10, 0.05)
    for item in proc_table().values():
        if project_marker in item['cmdline'] and 'tools/ml.py' in item['cmdline'] and (
                'app open' in item['cmdline'] or '_worker' in item['cmdline']):
            leftover.append({'pid': item['pid'], 'kill': kill_pid(item['pid'], signal.SIGKILL)})
    env_states = {}
    for env_id in ctx.env_ids:
        state = ctx.env_state(env_id)
        if (state.get('inspect') or {}).get('running'):
            ctx.env_cli('env', 'stop', env_id)
            state = ctx.env_state(env_id)
        env_states[env_id] = state
        for item in ctx.bridge_processes(env_id):
            kill_pid(item['pid'], signal.SIGTERM)
        lease = ctx.lease_state(env_id)
        if lease['live_count'] == 0 and ctx.lease_path(env_id).is_file():
            raw = lease['raw']
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
    wait_for(lambda: not any(ctx.bridge_processes(env) for env in ctx.env_ids), 10, 0.05)
    running = ctx.running_env_containers()
    live_leases = sum(ctx.lease_state(env)['live_count'] for env in ctx.env_ids)
    bridge_total = sum(len(ctx.bridge_processes(env)) for env in ctx.env_ids)
    windows_left = ctx.remaining_windows(ctx.run / 'observer/final-window-check-3')
    native_left = ctx.native_processes()
    leftover_left = [item for item in proc_table().values()
                     if project_marker in item['cmdline'] and 'tools/ml.py' in item['cmdline']
                     and ('app open' in item['cmdline'] or '_worker' in item['cmdline'])]
    packages_now = host_packages_hash()
    desktop_now = host_desktop_entries_hash()
    result = {
        'taken_at': stamp(),
        'started_at': started_at,
        'closed_windows': closed,
        'remaining_windows': len(windows_left),
        'remaining_window_details': windows_left[:10],
        'native_killed': native_killed,
        'leftover_native_processes': len(native_left),
        'leftover_native_details': native_left[:10],
        'leftover_processes': len(leftover_left),
        'leftover_process_details': [{'pid': item['pid'], 'cmdline': item['cmdline'][:300]}
                                     for item in leftover_left[:10]],
        'running_containers': len(running),
        'project_containers': ctx.project_containers(),
        'live_leases': live_leases,
        'bridge_processes': bridge_total,
        'environment_states': {env: {'record_runtime_state': value.get('record_runtime_state'),
                                     'inspect': value.get('inspect') or {}}
                               for env, value in env_states.items()},
        'host_packages_sha256_before': snapshot.get('host_packages_sha256'),
        'host_packages_sha256_after': packages_now,
        'host_packages_unchanged': snapshot.get('host_packages_sha256') == packages_now,
        'host_desktop_entries_sha256_before': snapshot.get('host_desktop_entries_sha256'),
        'host_desktop_entries_sha256_after': desktop_now,
        'host_desktop_entries_unchanged': (
            snapshot.get('host_desktop_entries_sha256') == desktop_now),
        'all_clean': (not windows_left and not native_left and not leftover_left
                      and not running and live_leases == 0 and bridge_total == 0),
    }
    save(ctx.run / 'postconditions.json', result)
    return result


# ------------------------------------------------------------------- run all
def run_all(ctx: Context, scenarios: list, rows: list) -> int:
    started_at = stamp()
    manifest = ctx.manifest
    manifest.setdefault('probe', {})
    manifest['probe'].update({
        'started_at': started_at,
        'tool_sha256': {relative: sha256_file(ROOT / relative)
                        for relative in ('tools/probe_concurrency.py',
                                         'tools/prepare_concurrency.py',
                                         'tools/probe_lifecycle.py',
                                         'tools/startup_observer.py',
                                         'tools/kwin_window_probe.py')
                        if (ROOT / relative).is_file()},
        'scenarios': list(scenarios),
        'focus_guard': getattr(ctx, 'focus_guard', None),
        'note_mode': 'read-only (0444) work documents',
    })
    save(ctx.run / 'manifest.json', manifest)
    try:
        for scenario in scenarios:
            function = {'S1': scenario_s1, 'S2': scenario_s2, 'S3': scenario_s3,
                        'S4': scenario_s4, 'S5': scenario_s5, 'S6': scenario_s6,
                        'S7': scenario_s7}[scenario]
            function(ctx, rows)
    finally:
        scenarios_found = collect_scenarios(ctx.run)
        cleanup = cleanup_run(ctx, started_at)
        manifest = ctx.manifest
        manifest['probe']['finished_at'] = stamp()
        manifest['probe']['cleanup'] = cleanup
        save(ctx.run / 'manifest.json', manifest)
    # A partial rerun (for example only S5 after a harness fix) must summarise
    # every previously recorded scenario as well as the rows just produced.
    rows = collect_rows(ctx.run)
    result = summarize(ctx, rows, scenarios_found)
    manifest = ctx.manifest
    manifest['probe']['summary_sha256'] = sha256_file(ctx.run / 'summary.json')
    manifest['probe']['validation_sha256'] = sha256_file(ctx.run / 'validation.json')
    manifest['probe']['report_sha256'] = sha256_file(ctx.run / 'report.md')
    manifest['finished_at'] = stamp()
    save(ctx.run / 'manifest.json', manifest)
    hashes = write_final_checksums(ctx.run)
    log(json.dumps({'validation': result['validation'], 'sha256sums': hashes},
                   ensure_ascii=False, indent=2))
    return 0 if result['validation']['product_verdict'] == 'concurrency-verified' else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    run_parser = operations.add_parser('run', help='execute the concurrency scenarios')
    run_parser.add_argument('--run-dir', type=Path, required=True)
    run_parser.add_argument('--scenarios', nargs='+', choices=SCENARIOS,
                            default=list(SCENARIOS))
    summary_parser = operations.add_parser(
        'summary', help='rebuild summary/validation/report from raw samples')
    summary_parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    if not (run / 'manifest.json').is_file():
        raise SystemExit(f'not a prepared concurrency evidence directory: {run}')
    if args.operation == 'summary':
        manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
        ctx = Context(run, None)
        ctx.env_map = manifest.get('env_map') or {}
        ctx.env_ids = list(ctx.env_map)
        rows = collect_rows(run)
        scenarios = collect_scenarios(run)
        result = summarize(ctx, rows, scenarios)
        hashes = write_final_checksums(run)
        log(json.dumps({'validation': result['validation'], 'sha256sums': hashes},
                       ensure_ascii=False, indent=2))
        return 0 if result['validation']['product_verdict'] == 'concurrency-verified' else 1
    from tools.startup_observer import KWinStartupObserver
    manifest = json.loads((run / 'manifest.json').read_text(encoding='utf-8'))
    if len(manifest.get('env_map') or {}) != len(ENV_IDS):
        raise SystemExit('prepare create has not created all five environments yet')
    with KWinStartupObserver(run / 'observer', PREFIX) as observer:
        ctx = Context(run, observer)
        for env_id in ctx.env_ids:
            if not (ctx.project / '.lab/state/environments' / (env_id + '.json')).is_file():
                raise SystemExit(f'{env_id}: environment record is missing; run prepare create first')
            if not (ctx.project / '.lab/state/launches' / (ctx.app_id(env_id) + '.json')).is_file():
                raise SystemExit(f'{ctx.app_id(env_id)}: launch record is missing')
        guard = None
        ctx.focus_guard = {'loaded': False}
        try:
            guard = FocusGuard(run / 'observer', PREFIX)
            ctx.focus_guard = {'loaded': True, 'plugin': guard.plugin,
                               'script': guard.script}
        except Exception as exc:
            ctx.focus_guard = {'loaded': False, 'error': f'{type(exc).__name__}: {exc}'}
        try:
            return run_all(ctx, list(args.scenarios), [])
        finally:
            if guard is not None:
                guard.close()
                ctx.focus_guard['closed'] = True


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('probe_concurrency: interrupted', file=sys.stderr)
        raise SystemExit(130)
