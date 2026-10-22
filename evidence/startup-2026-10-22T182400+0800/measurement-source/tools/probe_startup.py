#!/usr/bin/env python3
"""Measure native GUI startup and managed lifetime without changing product code.

The endpoint is a compositor-added native window plus its first non-null
Wayland buffer commit. It is not physical display presentation. Cold means a
stopped container; filesystem caches are retained. Warm means the environment
and desktop bridge are already running, with a new Mousepad process each time.
"""
import argparse
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.on_demand import LaunchGroup, process_stamp, stop_bridge
from multilinux.packages import HostObserver
from multilinux.resources import ResourceProbe, host_snapshot
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from tools.startup_observer import KWinStartupObserver

TARGETS = ('debian13', 'fedora44', 'arch')
PREFIX = 'MLSTARTUP-'


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temporary.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def service(run):
    project = run / 'project'
    return EnvironmentService(project, PodmanRuntime(),
        tomllib.loads((project / 'config/targets.toml').read_text()),
        json.loads((project / 'config/images.lock.json').read_text()))


def reset(run, target):
    """Stop only this experiment's environment and its validated proxy PID."""
    lab = service(run)
    env_id = 'startup-' + target
    if not lab.store.exists(env_id):
        return
    lab.stop(env_id)
    group = LaunchGroup(lab, env_id)
    with group.locked():
        state = group.read()
        if state['leases']:
            raise RuntimeError('refusing to reset an environment with live leases')
        stop_bridge(state.get('bridge'))
        state.update(bridge=None, auto_stop=False)
        group.save(state)


class WaylandTrace:
    """Associate attach/commit/frame callbacks with the titled toplevel only."""
    def __init__(self, marker):
        self.marker = marker
        self.xdg_surfaces = {}
        self.toplevels = {}
        self.surface = None
        self.attach_ns = self.commit_ns = self.frame_callback_ns = None
        self.callbacks = set()
        self._pending_attach = {}

    def feed(self, line, received_ns):
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

    def metadata(self):
        return {'surface_id': self.surface, 'first_attach_received_ns': self.attach_ns,
                'first_commit_received_ns': self.commit_ns,
                'first_frame_callback_received_ns': self.frame_callback_ns}


class Launch:
    def __init__(self, run, target, marker, directory, observer):
        self.run, self.target, self.marker = run, target, marker
        self.directory, self.observer = directory, observer
        self.trace = WaylandTrace(marker)
        self.threads = []
        self.exit_ns = None
        self.started_ns = time.monotonic_ns()
        note = run / 'work' / target / (marker + '.txt')
        note.write_text('Native GUI startup test\n固定测试文本，不修改现有文档。\n')
        self.note, self.note_sha = note, sha(note)
        command = [sys.executable, str(run / 'project/tools/ml.py'), 'app', 'open',
                   'startup-editor-' + target, '--', str(note)]
        self.command = command
        self.started_ns = time.monotonic_ns()
        self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        self.process_stamp = process_stamp(self.process.pid)
        for stream, name in [(self.process.stdout, 'stdout'), (self.process.stderr, 'stderr')]:
            thread = threading.Thread(target=self._read, args=(stream, name), daemon=True)
            thread.start()
            self.threads.append(thread)
        self.window = None

    def _read(self, stream, name):
        with (self.directory / (name + '.log')).open('wb') as log:
            for raw in iter(stream.readline, b''):
                received = time.monotonic_ns()
                log.write(raw)
                if name == 'stderr':
                    self.trace.feed(raw.decode('utf-8', 'replace'), received)

    def ready(self, timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.observer.pump()
            if self.window is None:
                self.window = next((e for e in self.observer.events if e['event'] == 'added'
                    and self.marker in (e['caption'] or '')), None)
            if self.window and self.trace.commit_ns:
                if 'XdgToplevelWindow' not in (self.window['type'] or ''):
                    raise RuntimeError('test application did not use native Wayland')
                if not all((self.window['buffer_geometry'].get(k) or 0) > 0 for k in ['width', 'height']):
                    raise RuntimeError('compositor window has no positive buffer geometry')
                return
            if self.process.poll() is not None:
                raise RuntimeError('launcher exited before verified GUI readiness')
            time.sleep(.003)
        raise TimeoutError('no compositor window and associated Wayland buffer commit')

    def wait_exit(self, timeout=35):
        deadline = time.monotonic() + timeout
        while self.process.poll() is None and time.monotonic() < deadline:
            self.observer.pump()
            time.sleep(.005)
        if self.process.poll() is None:
            raise TimeoutError('launcher did not exit after test-window close')
        self.exit_ns = time.monotonic_ns()
        self.observer.pump()
        for thread in self.threads:
            thread.join(timeout=2)

    def close(self):
        self.close_ns = time.monotonic_ns()
        self.close_ack = self.observer.close_window(self.marker, self.window['window_id'])
        self.wait_exit()

    def result(self):
        removal = next((e for e in self.observer.events if e['event'] == 'removed'
            and e['window_id'] == self.window['window_id']), None)
        ready_ns = max(self.trace.commit_ns, self.window['received_ns'])
        def ms(end, start):
            return round((end - start) / 1e6, 3) if end is not None else None
        return {'marker': self.marker, 'command': self.command,
            'started_ns': self.started_ns, 'window': self.window,
            'wayland': self.trace.metadata(), 'removed': removal,
            'close_requested_ns': self.close_ns, 'launcher_exit_ns': self.exit_ns,
            'returncode': self.process.returncode,
            'launch_to_window_ms': ms(self.window['received_ns'], self.started_ns),
            'launch_to_buffer_commit_ms': ms(self.trace.commit_ns, self.started_ns),
            'launch_to_ready_ms': ms(ready_ns, self.started_ns),
            'launch_to_frame_callback_ms': ms(self.trace.frame_callback_ns, self.started_ns),
            'close_to_window_removed_ms': ms(removal['received_ns'], self.close_ns) if removal else None,
            'close_to_launcher_exit_ms': ms(self.exit_ns, self.close_ns),
            'note_unchanged': sha(self.note) == self.note_sha}


def single(run, target, mode, number, observer, label='measurement'):
    lab = service(run)
    env_id = 'startup-' + target
    if mode == 'cold':
        reset(run, target)
    elif lab.inspect(env_id)['runtime_state'] != 'running':
        lab.start(env_id)
    directory = run / 'samples' / target / (mode + '-' + label + f'-{number:03}')
    directory.mkdir(parents=True)
    marker = PREFIX + target + '-' + mode + '-' + label + f'-{number:03}'
    launch = None
    row = {'target': target, 'mode': mode, 'number': number, 'label': label,
           'started_at': stamp(), 'passed': False, 'directory': str(directory.relative_to(run))}
    try:
        row['host_before'] = host_snapshot()
        launch = Launch(run, target, marker, directory, observer)
        launch.ready()
        deadline = time.monotonic() + .15
        while time.monotonic() < deadline:
            observer.pump()
            time.sleep(.003)
        launch.close()
        row.update(launch.result())
        info = lab.inspect(env_id)
        row['environment_after'] = info['runtime_state']
        row['lease_after'] = LaunchGroup(lab, env_id).read()
        expected = 'stopped' if mode == 'cold' else 'running'
        row['passed'] = (launch.process.returncode == 0 and row['note_unchanged']
            and info['runtime_state'] == expected and not row['lease_after']['leases']
            and row['removed'] is not None
            and (mode != 'cold' or row['lease_after']['bridge'] is None))
        if not row['passed']:
            row['error'] = 'exit status, removal, unchanged document, or lifecycle check failed'
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
    finally:
        if launch and launch.process.poll() is None:
            if launch.window:
                try:
                    observer.close_window(marker, launch.window['window_id'])
                    launch.wait_exit()
                except Exception:
                    if process_stamp(launch.process.pid) == launch.process_stamp:
                        os.killpg(launch.process.pid, signal.SIGTERM)
                    try:
                        launch.process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(launch.process.pid, signal.SIGKILL)
                        launch.process.wait(timeout=3)
        row['finished_at'] = stamp()
        save(directory / 'result.json', row)
    return row


def stats(values):
    numbers = sorted(v for v in values if isinstance(v, (int, float)))
    if not numbers:
        return None
    n = len(numbers)
    return {'count': n, 'min': numbers[0], 'p50': numbers[math.ceil(n * .5) - 1],
            'p95': numbers[math.ceil(n * .95) - 1], 'max': numbers[-1],
            'mean': round(sum(numbers) / n, 3)}


def summarize(run):
    rows = [json.loads(p.read_text()) for p in sorted((run / 'samples').rglob('result.json'))]
    cases = {}
    metrics = ['launch_to_ready_ms', 'launch_to_window_ms', 'launch_to_buffer_commit_ms',
               'launch_to_frame_callback_ms', 'close_to_window_removed_ms',
               'close_to_launcher_exit_ms']
    for target in TARGETS:
        for mode in ['cold', 'warm']:
            selected = [r for r in rows if r['target'] == target and r['mode'] == mode
                        and r['label'] == 'measurement']
            valid = [r for r in selected if r['passed']]
            cases[target + '/' + mode] = {'attempts': len(selected), 'valid': len(valid),
                'failures': len(selected) - len(valid),
                'metrics': {key: stats([r.get(key) for r in valid]) for key in metrics}}
    result = {'schema': 'multilinux.startup.summary.v1', 'observed_at': stamp(),
              'endpoint': 'native compositor window plus first associated non-null buffer commit',
              'physical_presentation_measured': False, 'cases': cases}
    save(run / 'summary.json', result)
    return result


def measure(run, targets, modes, samples, warmups):
    manifest = json.loads((run / 'manifest.json').read_text())
    source_hashes = {str(p.relative_to(ROOT)): sha(p) for p in [Path(__file__),
                    ROOT / 'tools/startup_observer.py', ROOT / 'src/multilinux/on_demand.py']}
    manifest['measurement_source_sha256'] = source_hashes
    save(run / 'manifest.json', manifest)
    with KWinStartupObserver(run / 'observer', PREFIX) as observer:
        try:
            for target in targets:
                for mode in modes:
                    if mode == 'warm':
                        reset(run, target)
                        service(run).start('startup-' + target)
                    for index in range(warmups):
                        row = single(run, target, mode, index, observer, 'warmup')
                        print(json.dumps({'target': target, 'mode': mode, 'warmup': index,
                            'passed': row['passed'], 'ready_ms': row.get('launch_to_ready_ms'),
                            'release_ms': row.get('close_to_launcher_exit_ms'),
                            'error': row.get('error')}, ensure_ascii=False), flush=True)
                        if not row['passed']:
                            raise RuntimeError('warmup failed; inspect its retained logs')
                    for index in range(samples):
                        row = single(run, target, mode, index, observer)
                        print(json.dumps({'target': target, 'mode': mode, 'sample': index,
                            'passed': row['passed'], 'ready_ms': row.get('launch_to_ready_ms'),
                            'release_ms': row.get('close_to_launcher_exit_ms'),
                            'error': row.get('error')}, ensure_ascii=False), flush=True)
                        summarize(run)
                        if not row['passed']:
                            raise RuntimeError('sample failed; no silent replacement or exclusion')
                    reset(run, target)
        finally:
            save(run / 'observer-events.json', observer.events)
            for target in targets:
                reset(run, target)
    manifest['measurements_finished_at'] = stamp()
    save(run / 'manifest.json', manifest)
    summarize(run)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('operation', choices=['measure', 'summary'])
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--targets', nargs='+', choices=TARGETS, default=list(TARGETS))
    p.add_argument('--modes', nargs='+', choices=['cold', 'warm'], default=['cold', 'warm'])
    p.add_argument('--samples', type=int, default=30)
    p.add_argument('--warmups', type=int, default=1)
    a = p.parse_args()
    run = a.run_dir.expanduser().resolve()
    if a.operation == 'measure':
        if not 1 <= a.samples <= 100 or not 0 <= a.warmups <= 5:
            p.error('samples must be 1..100; warmups 0..5')
        measure(run, a.targets, a.modes, a.samples, a.warmups)
    else:
        print(json.dumps(summarize(run), ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
