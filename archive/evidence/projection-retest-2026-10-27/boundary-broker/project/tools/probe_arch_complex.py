#!/usr/bin/env python3
"""Exercise an installed FreeCAD in the independent arch-complex project.

Requires a fresh run directory containing project/, work/probe.FCMacro and an
installed complex-arch environment. Keeps results and leaves environment stopped.
"""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from tools.probe_desktop_apps import save, service_for
from tools.kwin_window_probe import window_info, screenshot


def run_probe(run):
    service = service_for(run)
    work = run / 'work'
    results = {}
    try:
        for phase in ('build', 'reopen'):
            app = 'freecad-' + phase
            result_path = work / (phase + '-result.json')
            if result_path.exists():
                raise RuntimeError('Choose fresh evidence; refusing to overwrite completed phase')
            service.add_app('complex-arch', app, '/usr/bin/FreeCAD',
                ['/usr/bin/FreeCAD', str(work / 'probe.FCMacro')],
                workdir=str(work), as_host_user=True, on_demand=True,
                env_overlay={'QT_QPA_PLATFORM': 'wayland', 'LC_ALL': 'C.UTF-8',
                    'ML_CAD_WORK': str(work), 'ML_CAD_PHASE': phase})
            service.stop('complex-arch')
            with (run / (phase + '.log')).open('w') as log:
                process = subprocess.Popen([sys.executable, str(run/'project/tools/ml.py'),
                    'app', 'open', app], stdout=log, stderr=subprocess.STDOUT)
                try:
                    deadline = time.monotonic() + 90
                    while not result_path.exists() and time.monotonic() < deadline:
                        if process.poll() is not None:
                            raise RuntimeError(f'{phase}: launcher exited {process.returncode}')
                        time.sleep(.25)
                    if not result_path.exists():
                        raise RuntimeError(f'{phase}: macro timed out')
                    result = json.loads(result_path.read_text())
                    results[phase] = result
                    save(run / 'gui-results.json', results)
                    if not result['passed']:
                        raise RuntimeError(result.get('error'))
                    marker = 'Multilinux FreeCAD ' + phase
                    windows = window_info(run/'observer', marker)
                    save(run/(phase+'-window.json'), windows)
                    if len(windows) != 1:
                        raise RuntimeError('expected exactly one real experiment window')
                    save(run/(phase+'-screenshot.json'), screenshot(run/'observer', marker,
                        run/(phase+'-window.png')))
                    results[phase]['launcher_exit'] = process.wait(timeout=55)
                    if results[phase]['launcher_exit'] != 0:
                        raise RuntimeError('GUI launcher failed')
                    results[phase]['environment_after'] = service.inspect('complex-arch')
                    save(run/'gui-results.json', results)
                finally:
                    if process.poll() is None:
                        service.stop('complex-arch')
                        process.wait(timeout=20)
            if phase == 'build':
                initial = hashlib.sha256((work/'中文 模型.FCStd').read_bytes()).hexdigest()
        final = hashlib.sha256((work/'中文 模型.FCStd').read_bytes()).hexdigest()
        assert initial == final, 'Reopen modified model unexpectedly'
        save(run/'files.json', {p.name: {'sha256': hashlib.sha256(p.read_bytes()).hexdigest(),
            'bytes': p.stat().st_size, 'uid': p.stat().st_uid, 'gid': p.stat().st_gid}
            for p in sorted(work.iterdir()) if p.is_file()})
    finally:
        service.stop('complex-arch')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', required=True, type=Path)
    args = parser.parse_args()
    run_probe(args.run_dir.resolve())
