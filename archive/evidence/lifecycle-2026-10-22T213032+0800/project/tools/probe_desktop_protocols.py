#!/usr/bin/env python3
"""Prepare an isolated native environment and exercise portal video buffers.

The compositor's normal selection dialog chooses the synthetic window. Frame
consumers retain metadata only. Existing application environments are untouched.
"""
import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))

from multilinux.packages import HostObserver
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService


PACKAGES = ['gcc', 'pkg-config', 'libpipewire-0.3-dev', 'libdrm-dev',
            'libgbm-dev', 'libegl-dev', 'libgles-dev', 'python3-gi',
            'libpipewire-0.3-common', 'libpipewire-0.3-modules', 'pipewire-bin']
NEW_SOURCES = ['src/multilinux/screencast.py', 'tools/probe_screencast.py',
               'fixtures/screencast_consumer.c', 'fixtures/share_target.py']
ENV_ID = 'screen-debian13'
APP_ID = 'screen-capture'
PIPEWIRE_PREFLIGHT = '''import ctypes,json,pathlib
p=ctypes.CDLL('libpipewire-0.3.so.0')
p.pw_init(None,None)
p.pw_main_loop_new.restype=ctypes.c_void_p
p.pw_main_loop_get_loop.argtypes=[ctypes.c_void_p]
p.pw_main_loop_get_loop.restype=ctypes.c_void_p
p.pw_context_new.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t]
p.pw_context_new.restype=ctypes.c_void_p
p.pw_context_destroy.argtypes=[ctypes.c_void_p]
p.pw_main_loop_destroy.argtypes=[ctypes.c_void_p]
loop=p.pw_main_loop_new(None)
context=p.pw_context_new(p.pw_main_loop_get_loop(loop),None,0) if loop else None
result={'client_config_exists':pathlib.Path('/usr/share/pipewire/client.conf').is_file(),
        'context_created':bool(context)}
if context: p.pw_context_destroy(context)
if loop: p.pw_main_loop_destroy(loop)
p.pw_deinit()
print(json.dumps(result))
raise SystemExit(0 if context else 1)
'''


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    temp.replace(path)


def service(run):
    config, lock, _ = load_project()
    return EnvironmentService(run / 'project', PodmanRuntime(), config, lock)


def prepare(run):
    if run.exists():
        raise RuntimeError(f'choose a new experiment directory: {run}')
    run.mkdir(parents=True)
    snapshot = run / 'project'
    work = run / 'work'
    work.mkdir()
    paths = subprocess.check_output(
        ['git', 'ls-files', '-z', 'src', 'tools', 'config', 'fixtures'],
        cwd=ROOT).decode().split('\0')
    hashes = {}
    for rel in filter(None, paths):
        source = ROOT / rel
        if not source.is_file():
            continue
        target = snapshot / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[rel] = digest(target)
    manifest = {'schema_version': 1, 'started_at': stamp(),
                'source_root': str(ROOT), 'project_root': str(snapshot),
                'baseline_commit': subprocess.check_output(
                    ['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                'baseline_source_sha256': hashes,
                'host_before': HostObserver().snapshot(),
                'environment_id': ENV_ID, 'app_id': APP_ID,
                'target': 'debian13', 'capture_results': [],
                'scope': 'Single Fedora host; native Debian consumer; portal-selected '
                         'synthetic window; no image/audio/camera bytes retained.'}
    save(run / 'manifest.json', manifest)
    lab = service(run)
    try:
        created = lab.create('debian13', ENV_ID, [str(work)], 'pasta',
                             desktop=True, user_namespace='keep-id',
                             portals=True, gpu=True)
        save(run / 'environment-created.json', created)
        txn = lab.install_packages(ENV_ID, PACKAGES)
        save(run / 'dependencies.json', txn)
        if txn['phase'] != 'completed':
            raise RuntimeError('native build dependencies failed to install')
        launch = lab.add_app(
            ENV_ID, APP_ID, '/usr/bin/python3',
            ['/usr/bin/python3', str(work / 'source/tools/probe_screencast.py')],
            workdir=str(work), as_host_user=True, on_demand=True,
            env_overlay={'XDG_CURRENT_DESKTOP': 'KDE'})
        save(run / 'launch-created.json', launch)
        manifest['prepared_at'] = stamp()
    except Exception as exc:
        manifest['prepare_error'] = str(exc)
        raise
    finally:
        if lab.store.exists(ENV_ID):
            lab.stop(ENV_ID)
        manifest['host_after_prepare'] = HostObserver().snapshot()
        save(run / 'manifest.json', manifest)


def build(run):
    manifest = json.loads((run / 'manifest.json').read_text())
    hashes = {}
    for rel in NEW_SOURCES:
        source = ROOT / rel
        if not source.is_file():
            raise RuntimeError(f'implementation not ready: {rel}')
        target = run / 'work/source' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        hashes[rel] = digest(target)
    core_rel = 'src/multilinux/on_demand.py'
    shutil.copyfile(ROOT / core_rel, run / 'project' / core_rel)
    manifest['tested_core_sha256'] = {core_rel: digest(ROOT / core_rel)}
    lab = service(run)
    artifact = run / 'work/artifacts/debian13/screencast-consumer'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    code = (
        'import shlex,subprocess,sys; '
        'flags=shlex.split(subprocess.check_output(["pkg-config","--cflags","--libs",'
        '"libpipewire-0.3","libdrm","gbm","egl","glesv2"],text=True)); '
        'sys.exit(subprocess.call(["cc","-std=c11","-O2","-Wall","-Wextra",'
        '"-Werror",sys.argv[1],"-o",sys.argv[2]]+flags))')
    try:
        result = lab.exec_app(APP_ID, ['/usr/bin/python3', '-c', PIPEWIRE_PREFLIGHT],
                              capture=True, timeout=30)
        save(run / 'pipewire-runtime-preflight.json',
             {'returncode': result.returncode, 'stdout': result.stdout,
              'stderr': result.stderr})
        if result.returncode:
            raise RuntimeError('PipeWire client runtime preflight failed before selection')
        result = lab.exec_app(APP_ID, ['/usr/bin/python3', '-c', code,
                             str(run / 'work/source/fixtures/screencast_consumer.c'),
                             str(artifact)], capture=True, timeout=120)
        save(run / 'build.json', {'returncode': result.returncode,
                                 'stdout': result.stdout, 'stderr': result.stderr,
                                 'source_sha256': hashes})
        if result.returncode:
            raise RuntimeError(f'native Debian compilation failed: {result.stderr[-2000:]}')
        result = lab.exec_app(APP_ID, [str(artifact), '--self-test'],
                              capture=True, timeout=30)
        save(run / 'native-selftest.json', {'returncode': result.returncode,
                                          'stdout': result.stdout,
                                          'stderr': result.stderr})
        if result.returncode:
            raise RuntimeError('native Debian consumer self-test failed')
        manifest['tested_source_sha256'] = hashes
        manifest['consumer_sha256'] = digest(artifact)
        manifest['built_at'] = stamp()
    finally:
        lab.stop(ENV_ID)
        save(run / 'manifest.json', manifest)


def capture(run, mode, frames, timeout, selection_timeout, render_node):
    manifest = json.loads((run / 'manifest.json').read_text())
    if not manifest.get('tested_source_sha256'):
        raise RuntimeError('run build before capture')
    for rel, expected in manifest.get('tested_source_sha256', {}).items():
        if digest(ROOT / rel) != expected or digest(run / 'work/source' / rel) != expected:
            raise RuntimeError(f'source changed after build: {rel}; run build again')
    for rel, expected in manifest.get('tested_core_sha256', {}).items():
        if digest(ROOT / rel) != expected or digest(run / 'project' / rel) != expected:
            raise RuntimeError(f'core source changed after build: {rel}; run build again')
    attempt = run / 'work' / ('capture-' + mode + '-' + datetime.now(
        ZoneInfo('Asia/Shanghai')).strftime('%H%M%S'))
    attempt.mkdir()
    provenance = {key: manifest.get(key) for key in (
        'baseline_commit', 'built_at', 'tested_source_sha256',
        'tested_core_sha256', 'consumer_sha256')}
    save(attempt / 'provenance.json', provenance)
    for rel in manifest['tested_source_sha256']:
        target = attempt / 'source' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run / 'work/source' / rel, target)
    state = attempt / 'target-state.json'
    with (attempt / 'target-stderr.log').open('w') as target_log:
        child = subprocess.Popen([sys.executable, str(ROOT / 'fixtures/share_target.py'),
                                  '--state', str(state)], stdout=subprocess.DEVNULL,
                                 stderr=target_log)
    lab = service(run)
    try:
        deadline = time.monotonic() + 10
        while not state.exists() and time.monotonic() < deadline:
            if child.poll() is not None:
                raise RuntimeError('synthetic target exited before mapping a window')
            time.sleep(.05)
        if not state.exists():
            raise RuntimeError('synthetic target did not map a window')
        print('Select only "Multilinux Screen Share Test" in the system sharing dialog.',
              flush=True)
        result = lab.open_app(APP_ID, [
            '--consumer', str(run / 'work/artifacts/debian13/screencast-consumer'),
            '--output', str(attempt / 'capture.json'), '--mode', mode,
            '--frames', str(frames), '--timeout', str(timeout),
            '--selection-timeout', str(selection_timeout), '--source', 'window',
            '--render-node', render_node, '--expect-pattern'])
        save(attempt / 'exit.json', {'returncode': result.returncode,
                                     'finished_at': stamp()})
        manifest['capture_results'].append({'path': str(attempt.relative_to(run)),
                                             'mode': mode,
                                             'returncode': result.returncode})
        return result.returncode
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        manifest['last_environment_state'] = lab.inspect(ENV_ID)['runtime_state']
        manifest['host_after_capture'] = HostObserver().snapshot()
        save(run / 'manifest.json', manifest)


def gpu_roundtrip(run):
    """Compile inside Debian and import a private, generated GBM buffer."""
    source = ROOT / 'fixtures/dmabuf_roundtrip.c'
    target = run / 'work/source/fixtures/dmabuf_roundtrip.c'
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, target)
    artifact = run / 'work/artifacts/debian13/dmabuf-roundtrip'
    artifact.parent.mkdir(parents=True, exist_ok=True)
    lab = service(run)
    record = {'started_at': stamp(), 'source_sha256': digest(target),
              'scope': 'Private synthetic buffer; no compositor capture; CPU readback.'}
    code = (
        'import shlex,subprocess,sys; '
        'flags=shlex.split(subprocess.check_output(["pkg-config","--cflags","--libs",'
        '"libdrm","gbm","egl","glesv2"],text=True)); '
        'sys.exit(subprocess.call(["cc","-std=c11","-O2","-Wall","-Wextra",'
        '"-Werror",sys.argv[1],"-o",sys.argv[2]]+flags))')
    try:
        result = lab.exec_app(APP_ID, ['/usr/bin/python3', '-c', code,
                              str(target), str(artifact)], capture=True, timeout=120)
        record['build'] = {'returncode': result.returncode,
                           'stdout': result.stdout, 'stderr': result.stderr}
        if result.returncode:
            raise RuntimeError(f'Debian GPU fixture compilation failed: {result.stderr}')
        result = lab.exec_app(APP_ID, [str(artifact), '--render-node',
                                      '/dev/dri/renderD128'], capture=True, timeout=30)
        record.update(returncode=result.returncode, stderr=result.stderr,
                      result=json.loads(result.stdout))
        record['passed'] = result.returncode == 0 and record['result'].get('passed') is True
        print(json.dumps({'passed': record['passed'],
                          'returncode': result.returncode,
                          'result': record['result']}, ensure_ascii=False), flush=True)
        return 0 if record['passed'] else 1
    finally:
        lab.stop(ENV_ID)
        record['finished_at'] = stamp()
        save(run / 'gpu-debian13-roundtrip.json', record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=('prepare', 'build', 'capture', 'gpu'))
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--mode', choices=('memfd', 'dmabuf', 'auto'), default='memfd')
    parser.add_argument('--frames', type=int, default=60)
    parser.add_argument('--timeout', type=float, default=20)
    parser.add_argument('--selection-timeout', type=float, default=180)
    parser.add_argument('--render-node', default='/dev/dri/renderD128')
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    if args.operation == 'prepare':
        prepare(run)
    elif args.operation == 'build':
        build(run)
    elif args.operation == 'gpu':
        return gpu_roundtrip(run)
    else:
        return capture(run, args.mode, args.frames, args.timeout,
                       args.selection_timeout, args.render_node)
    print(str(run), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
