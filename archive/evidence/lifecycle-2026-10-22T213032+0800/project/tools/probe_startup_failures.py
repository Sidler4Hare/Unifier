#!/usr/bin/env python3
"""Exercise rapid reopen, concurrent leases, and killed-launcher recovery.

Only environments created by prepare_startup.py are addressed. A diagnosed
product failure is retained as a finding, separately from probe execution
errors. These scenarios run after the serial latency measurements.
"""
import argparse
import json
import os
from pathlib import Path
import signal
import shutil
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.on_demand import LaunchGroup, process_stamp
from multilinux.resources import ResourceProbe
from tools.probe_startup import (KWinStartupObserver, Launch, PREFIX, TARGETS,
                                reset, save, service, stamp)

OWNED_LAUNCHES = []


def wait(observer, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        observer.pump()
        time.sleep(.005)


def start(run, target, directory, suffix, observer):
    child_dir = directory / suffix
    child_dir.mkdir()
    launch = Launch(run, target, PREFIX + target + '-' + directory.name + '-' + suffix,
                    child_dir, observer)
    OWNED_LAUNCHES.append(launch)
    launch.ready()
    return launch


def cleanup_launches(observer):
    for launch in OWNED_LAUNCHES:
        window = launch.window or next((e for e in observer.events if e['event'] == 'added'
            and launch.marker in (e['caption'] or '')), None)
        removed = window and any(e['event'] == 'removed' and e['window_id'] ==
            window['window_id'] for e in observer.events)
        if window and not removed:
            try:
                observer.close_window(launch.marker, window['window_id'])
            except Exception:
                pass
        if launch.process.poll() is None:
            try:
                launch.wait_exit(timeout=15)
            except TimeoutError:
                if process_stamp(launch.process.pid) == launch.process_stamp:
                    os.killpg(launch.process.pid, signal.SIGKILL)
                launch.process.wait(timeout=3)
    OWNED_LAUNCHES.clear()


def state(run, target):
    lab = service(run)
    record = lab.store.load('startup-' + target)
    raw = json.loads(subprocess.check_output(['podman', 'inspect', '--format', 'json',
        record['runtime']['container_name']], text=True))[0]['State']
    lease_path = run / 'project/.lab/leases' / ('startup-' + target + '.json')
    return {'state': raw,
            'lease_file': json.loads(lease_path.read_text()) if lease_path.exists() else None}


def rapid_reopen(run, target, directory, observer):
    reset(run, target)
    first = start(run, target, directory, 'first', observer)
    first_close_ns = time.monotonic_ns()
    observer.close_window(first.marker, first.window['window_id'])
    first_exit = {}

    def reap_first():
        first.process.wait()
        first_exit['observed_ns'] = time.monotonic_ns()

    reaper = threading.Thread(target=reap_first)
    reaper.start()
    wait(observer, .15)
    before = state(run, target)
    second = start(run, target, directory, 'second', observer)
    second_ready_ns = max(second.window['received_ns'], second.trace.commit_ns)
    reaper.join(timeout=35)
    if reaper.is_alive():
        raise RuntimeError('first launcher did not finish during reopen')
    second.close()
    for thread in first.threads:
        thread.join(timeout=2)
    return {'execution_passed': first.process.returncode == 0 and second.process.returncode == 0,
        'first_close_requested_ns': first_close_ns, 'first_launcher_exit': first_exit,
        'state_before_reopen': before,
        'reopen_delay_ms': (second.started_ns - first_close_ns) / 1e6,
        'second_launch_to_ready_ms': (second_ready_ns - second.started_ns) / 1e6,
        'close_to_second_ready_ms': (second_ready_ns - first_close_ns) / 1e6,
        'second': second.result(), 'state_after': state(run, target)}


def concurrent(run, target, directory, observer):
    reset(run, target)
    first = start(run, target, directory, 'first', observer)
    second = start(run, target, directory, 'second', observer)
    with_both = state(run, target)
    first.close()
    after_first = state(run, target)
    second_removed = any(e['event'] == 'removed' and e['window_id'] ==
                         second.window['window_id'] for e in observer.events)
    second.close()
    after_second = state(run, target)
    ok = (len(with_both['lease_file']['leases']) == 2
        and after_first['state']['Running'] and not second_removed
        and len(after_first['lease_file']['leases']) == 1
        and not after_second['state']['Running']
        and not after_second['lease_file']['leases']
        and after_second['lease_file']['bridge'] is None)
    return {'execution_passed': first.process.returncode == 0 and second.process.returncode == 0,
        'shared_lifetime_passed': ok, 'with_two_apps': with_both,
        'after_first_close': after_first, 'after_final_close': after_second,
        'first': first.result(), 'second': second.result()}


def killed_launcher(run, target, directory, observer):
    reset(run, target)
    first = start(run, target, directory, 'killed', observer)
    before = state(run, target)
    if process_stamp(first.process.pid) != first.process_stamp:
        raise RuntimeError('launcher identity changed before owned SIGKILL test')
    killed_ns = time.monotonic_ns()
    first.process.kill()  # exact Popen-owned launcher, not a process-name match
    first.process.wait(timeout=3)
    wait(observer, 2)
    after_kill = state(run, target)
    observer.close_window(first.marker, first.window['window_id'])
    wait(observer, .2)
    after_orphan_window_close = state(run, target)
    for thread in first.threads:
        thread.join(timeout=2)
    following = start(run, target, directory, 'following', observer)
    following.close()
    after_following = state(run, target)
    return {'execution_passed': first.process.returncode == -signal.SIGKILL
        and following.process.returncode == 0,
        'killed_ns': killed_ns, 'observation_seconds': 2,
        'state_before_kill': before, 'state_after_kill': after_kill,
        'state_after_orphan_window_close': after_orphan_window_close,
        'state_after_following_managed_close': after_following,
        'automatic_recovery_after_window_close': not after_orphan_window_close['state']['Running'],
        'following_managed_launch_restores_auto_stop': not after_following['state']['Running'],
        'following': following.result()}


def resources(run, target, observer):
    lab = service(run)
    reset(run, target)
    lab.start('startup-' + target)
    idle = ResourceProbe(lab).sample('startup-' + target)
    directory = run / 'diagnostics' / target / 'resource-retention'
    directory.mkdir()
    gui = start(run, target, directory, 'gui', observer)
    gui.close()
    after_gui = ResourceProbe(lab).sample('startup-' + target)
    cleanup_launches(observer)
    reset(run, target)
    stopped = ResourceProbe(lab).sample('startup-' + target)
    save(run / 'diagnostics' / target / 'resource-states.json',
         {'fresh_running_idle': idle, 'idle_after_gui_close': after_gui,
          'stopped': stopped})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--targets', nargs='+', choices=TARGETS, default=list(TARGETS))
    parser.add_argument('--reopen-repeats', type=int, default=3)
    parser.add_argument('--resume', action='store_true')
    args = parser.parse_args()
    if not 1 <= args.reopen_repeats <= 7:
        parser.error('reopen repeats must be 1..7')
    run = args.run_dir.expanduser().resolve()
    journal = run / 'diagnostic-results.json'
    outcomes = json.loads(journal.read_text()) if journal.exists() else []
    source_snapshot = run / 'diagnostic-source' / str(time.monotonic_ns())
    for rel in ['tools/probe_startup.py', 'tools/probe_startup_failures.py',
                'tools/kwin_window_probe.py', 'tools/startup_observer.py']:
        target = source_snapshot / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    with KWinStartupObserver(run / 'diagnostic-observer', PREFIX) as observer:
        try:
            for target in args.targets:
                scenarios = [(f'reopen-{n}', rapid_reopen) for n in range(args.reopen_repeats)]
                scenarios += [('concurrent', concurrent), ('killed-launcher', killed_launcher)]
                for name, operation in scenarios:
                    directory = run / 'diagnostics' / target / name
                    if directory.exists():
                        prior = directory / 'result.json'
                        if args.resume and prior.exists() and json.loads(prior.read_text()).get('execution_passed'):
                            continue
                        if not args.resume:
                            raise RuntimeError('diagnostic directory exists; use --resume')
                        attempt = 1
                        while directory.with_name(name + f'-attempt{attempt}').exists():
                            attempt += 1
                        directory.rename(directory.with_name(name + f'-attempt{attempt}'))
                    directory.mkdir(parents=True)
                    result = {'target': target, 'scenario': name, 'started_at': stamp(),
                              'source_snapshot': str(source_snapshot.relative_to(run))}
                    try:
                        result.update(operation(run, target, directory, observer))
                    except Exception as exc:
                        result.update(execution_passed=False, error=f'{type(exc).__name__}: {exc}')
                    finally:
                        result['finished_at'] = stamp()
                        save(directory / 'result.json', result)
                        cleanup_launches(observer)
                        reset(run, target)
                    outcomes.append(result)
                    save(run / 'diagnostic-results.json', outcomes)
                    print(json.dumps({key: result.get(key) for key in ['target', 'scenario',
                        'execution_passed', 'second_launch_to_ready_ms', 'shared_lifetime_passed',
                        'automatic_recovery_after_window_close',
                        'following_managed_launch_restores_auto_stop', 'error']},
                        ensure_ascii=False), flush=True)
                    if not result['execution_passed']:
                        raise RuntimeError('diagnostic execution failed; inspect retained result')
                if not (run / 'diagnostics' / target / 'resource-states.json').exists():
                    resources(run, target, observer)
        finally:
            cleanup_launches(observer)
            save(run / ('diagnostic-observer-events-' + source_snapshot.name + '.json'), observer.events)
            for target in args.targets:
                reset(run, target)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
