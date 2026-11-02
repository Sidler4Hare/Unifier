"""Current release launches the retained FreeCAD app; synthetic CAD data only."""
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / 'freecad'
OUT.mkdir(exist_ok=False)
APP = Path.home() / '.local/share/multilinux/apps/freecad'
launch_file = APP / '.lab/state/launches/freecad.json'
launch = json.loads(launch_file.read_text())
work = Path(launch['workdir']) / ('v0.0.1-acceptance-' + datetime.now().strftime('%Y%m%dT%H%M%S'))
work.mkdir(exist_ok=False)
report = {'scope': 'retained Arch FreeCAD, exact release CLI/worker, native GUI macro save/reopen; not fresh install or general app acceptance',
          'work': str(work), 'sessions': [], 'launch_record_before': hashlib.sha256(launch_file.read_bytes()).hexdigest()}
with tempfile.TemporaryDirectory(prefix='unifier-v001-app-') as temporary:
    with tarfile.open(REPO / 'artifacts/unifier-0.0.1.tar.gz') as archive:
        archive.extractall(temporary, filter='data')
    source = Path(temporary) / 'unifier-0.0.1'
    sys.path[:0] = [str(source / 'src'), str(source)]
    from tools.check_repo import verify_bundle
    verify_bundle(source)
    from tools.kwin_window_probe import window_info, screenshot
    from unifier.service import EnvironmentService
    from unifier.runtime import PodmanRuntime
    from unifier.install import load_catalog
    config, lock = load_catalog(source)
    service = EnvironmentService(APP, PodmanRuntime(), config, lock)
    cid = launch['container_id']
    macro = (source / 'tools/probe_freecad_macro.py').read_text()
    process = None
    try:
        assert service.inspect('freecad-arch')['runtime_state'] == 'stopped'
        for phase in ['build', 'reopen']:
            payload = work / (phase + '.FCMacro')
            payload.write_text('import os\nos.environ["ML_CAD_WORK"] = ' + repr(str(work)) + '\nos.environ["ML_CAD_PHASE"] = ' + repr(phase) + '\n' + macro)
            with (OUT / (phase + '.log')).open('w') as log:
                process = subprocess.Popen([sys.executable, str(source / 'tools/unifier.py'),
                    'app', 'open', 'freecad', '--', str(payload)], cwd=source,
                    env={**os.environ, 'UNIFIER_HOME': str(APP), 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONPATH': str(source / 'src')},
                    stdout=log, stderr=log)
                result_file = work / (phase + '-result.json')
                deadline = time.monotonic() + 55
                while not result_file.exists():
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise RuntimeError('FreeCAD phase did not produce result: ' + phase)
                    time.sleep(.3)
                result = json.loads(result_file.read_text())
                assert result['passed'], result
                top = subprocess.check_output(['podman', 'top', cid, 'hpid', 'comm'], text=True)
                pids = [int(row.split()[0]) for row in top.splitlines()[1:] if row.split() and row.split()[0].isdigit()]
                windows = window_info(OUT / 'observer', 'Multilinux FreeCAD ' + phase, pids=pids, resource_hint='freecad')
                windows = [w for w in windows if w['width'] > 400 and w['height'] > 300]
                assert len(windows) == 1, windows
                shot = screenshot(OUT / 'observer', '', OUT / (phase + '-window.png'),
                    pids=[windows[0]['pid']], resource_hint='freecad', window_id=windows[0]['internalId'])
                process.wait(timeout=55)
                assert process.returncode == 0, process.returncode
                stopped = service.inspect('freecad-arch')
                assert stopped['runtime_state'] == 'stopped', stopped
                assert stopped['runtime']['container_id'] == cid
                report['sessions'].append({'phase': phase, 'result': result, 'window': windows[0],
                    'normal_exit': process.returncode, 'stopped_same_cid': True, 'screenshot': str(OUT / (phase + '-window.png'))})
                print(json.dumps({'phase': phase, 'passed': True}), flush=True)
    except Exception:
        report['error'] = traceback.format_exc()
        if process is not None and process.poll() is None:
            # The macro normally closes itself. Do not kill unrelated GUI apps.
            try:
                process.wait(timeout=45)
            except subprocess.TimeoutExpired:
                report['cleanup_needs_attention'] = 'owned FreeCAD did not exit normally'
    report['launch_record_after'] = hashlib.sha256(launch_file.read_bytes()).hexdigest()
    report['launch_record_preserved'] = report['launch_record_after'] == report['launch_record_before']
    report['final_state'] = service.inspect('freecad-arch')['runtime_state']
report['passed'] = len(report['sessions']) == 2 and not report.get('error') and report['launch_record_preserved'] and report['final_state'] == 'stopped'
(OUT / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
print(json.dumps({'passed': report['passed'], 'error': report.get('error'), 'work': str(work)}, ensure_ascii=False), flush=True)
raise SystemExit(0 if report['passed'] else 1)
