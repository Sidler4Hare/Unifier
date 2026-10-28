#!/usr/bin/env python3
"""Separate, instrumented GUI diagnostics; excluded from the latency cohort."""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from tools.probe_startup import (KWinStartupObserver, Launch, PREFIX, reset,
                                save, service, stamp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    target = 'debian13'
    outcomes = []
    source = run / 'phase-source'
    for rel in ['tools/probe_startup_phases.py', 'tools/probe_startup.py',
                'tools/trace_startup_entry.py', 'tools/kwin_window_probe.py']:
        output = source / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, output)
    with KWinStartupObserver(run / 'phase-observer', PREFIX) as observer:
        try:
            for mode in ['cold', 'warm']:
                reset(run, target)
                if mode == 'warm':
                    service(run).start('startup-' + target)
                directory = run / 'phases' / mode
                directory.mkdir(parents=True)
                metadata_path = directory / 'podman-spans.json'
                actual_command = []
                real_popen = subprocess.Popen

                def spawn(command, *positional, **keywords):
                    if len(command) > 1 and Path(command[1]) == run / 'project/tools/ml.py':
                        command = [command[0], str(ROOT / 'tools/trace_startup_entry.py'),
                            '--project', str(run / 'project'), '--output', str(metadata_path),
                            *command[2:]]
                        actual_command[:] = command
                    return real_popen(command, *positional, **keywords)

                launch = None
                try:
                    with patch('subprocess.Popen', side_effect=spawn):
                        launch = Launch(run, target, PREFIX + 'phase-' + mode,
                                        directory, observer)
                    launch.command = actual_command
                    launch.ready()
                    deadline = time.monotonic() + .15
                    while time.monotonic() < deadline:
                        observer.pump()
                        time.sleep(.003)
                    launch.close()
                    data = launch.result()
                    profile = json.loads(metadata_path.read_text())
                    ready_ns = max(launch.window['received_ns'], launch.trace.commit_ns)
                    completed = [s for s in profile['podman_spans'] if s['finished_ns'] <= ready_ns]
                    counts, durations = {}, {}
                    for span in completed:
                        action = span['action']
                        counts[action] = counts.get(action, 0) + 1
                        durations[action] = round(durations.get(action, 0) + span['duration_ms'], 3)
                    data.update(target=target, mode=mode, observed_at=stamp(),
                        instrumented=True, excluded_from_latency_cohort=True,
                        completed_before_ready={'count': len(completed), 'by_action': counts,
                                               'duration_ms_by_action': durations},
                        product_import_ms=profile['product_import_ms'],
                        passed=launch.process.returncode == 0 and profile['returncode'] == 0)
                    save(directory / 'result.json', data)
                    outcomes.append(data)
                    print(json.dumps({'mode': mode, 'passed': data['passed'],
                        'ready_ms': data['launch_to_ready_ms'],
                        'before_ready': data['completed_before_ready']}, ensure_ascii=False), flush=True)
                finally:
                    if launch and launch.process.poll() is None:
                        if launch.window:
                            observer.close_window(launch.marker, launch.window['window_id'])
                            launch.wait_exit()
                        else:
                            launch.process.kill()
                            launch.process.wait(timeout=3)
                    reset(run, target)
        finally:
            save(run / 'phase-observer-events.json', observer.events)
            reset(run, target)
    save(run / 'phase-results.json', outcomes)


if __name__ == '__main__':
    main()
