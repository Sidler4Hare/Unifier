#!/usr/bin/env python3
"""Install and launch the user-supplied Tencent Meeting deb in its own Debian root."""

import argparse
import atexit
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import traceback

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / 'src'))
from tools.probe_desktop_apps import save, service_for, source_paths, sha, log, need
from tools.kwin_window_probe import window_info, screenshot
from multilinux.project import load_project
from multilinux.resources import ResourceProbe
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService, now

DEFAULT_DEB = Path('/home/omg/下载/TencentMeeting_0300000000_3.26.10.401_x86_64_default.publish.officialwebsite.deb')
ENV_ID, APP_ID = 'meeting-debian13', 'meeting-wemeet'


def inspect_package(path):
    members = subprocess.check_output(['ar', 't', str(path)], text=True).splitlines()
    member = next(name for name in members if name.startswith('control.tar'))
    data = subprocess.check_output(['ar', 'p', str(path), member])
    files = {}
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        for item in archive:
            if item.isfile():
                files[item.name] = archive.extractfile(item).read().decode('utf-8', 'replace')
    return {'path': str(path), 'size_bytes': path.stat().st_size, 'sha256': sha(path),
            'members': members, 'control_files': files}


def app_pids(sample):
    found = set()
    for entry in (sample.get('processes') or {}).get('details', []):
        if 'wemeet' not in (entry.get('comm') or '').lower():
            continue
        pid = entry['pid']; found.add(pid)
        try:
            for line in Path(f'/proc/{pid}/status').read_text().splitlines():
                if line.startswith('NSpid:'):
                    found.update(map(int, line.split()[1:]))
        except FileNotFoundError:
            pass
    return sorted(found)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--deb', type=Path, default=DEFAULT_DEB)
    parser.add_argument('--repair-package', action='append', default=[])
    parser.add_argument('--install-only', action='store_true')
    parser.add_argument('--generate-zh-locale', action='store_true')
    parser.add_argument('--reconfigure-vendor', action='store_true')
    parser.add_argument('--as-host-user', action='store_true')
    args = parser.parse_args()
    run_dir = args.run_dir.resolve(); service = service_for(run_dir)
    root = run_dir / 'tencent-meeting'; root.mkdir(exist_ok=True)
    docs = run_dir / 'documents/tencent-meeting'
    if args.as_host_user:
        root = root / 'keep-id'; root.mkdir(exist_ok=True)
        docs = run_dir / 'documents/tencent-meeting-user'
        project = run_dir / 'user-project'
        snapshot = root / 'source-snapshot.json'
        if not snapshot.exists():
            hashes = {}
            for source in source_paths():
                rel = source.relative_to(ROOT); destination = project / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination); hashes[str(rel)] = sha(source)
            save(snapshot, {'project_root': str(project), 'created_at': now(),
                             'source_sha256': hashes})
        recorded = json.loads(snapshot.read_text())
        for rel, expected in recorded['source_sha256'].items():
            need(sha(project / rel) == expected, f'user snapshot changed: {rel}')
            need(sha(ROOT / rel) == expected, f'current source differs from user snapshot: {rel}')
        config, lock, _ = load_project()
        service = EnvironmentService(project, PodmanRuntime(), config, lock)
    docs.mkdir(parents=True, exist_ok=True)
    package = inspect_package(args.deb)
    save(root / 'package.json', package)
    if not service.store.exists(ENV_ID):
        service.create('debian13', ENV_ID, [str(docs)], 'pasta', desktop=True,
                        user_namespace='keep-id' if args.as_host_user else 'rootless-default')
    def stop_environment():
        with contextlib.suppress(Exception):
            service.stop(ENV_ID)
    atexit.register(stop_environment)
    if args.repair_package:
        transaction = service.install_packages(ENV_ID, args.repair_package)
        save(root / ('repair-' + transaction['id'] + '.json'), transaction)
        need(transaction['phase'] == 'completed', 'native dependency repair failed')
    present = service.query_package(ENV_ID, 'wemeet')
    if not present['present']:
        log('Tencent Meeting: installing the original deb with native APT')
        transaction = service.install_local(ENV_ID, str(args.deb), package_name='wemeet',
                                            use_argv=['test', '-x', '/opt/wemeet/wemeetapp.sh'])
        save(root / 'install-transaction.json', transaction)
        need(transaction['phase'] == 'completed', f'local deb install failed: {transaction.get("native_error")} {transaction.get("verification")}')
    save(root / 'package-query.json', service.query_package(ENV_ID, 'wemeet'))
    shutil.copyfile(service.store.inventory_path(ENV_ID), root / 'packages.txt')
    if not service._launch_exists(APP_ID):
        executable = '/opt/wemeet/wemeetapp.sh'
        service.add_app(ENV_ID, APP_ID, executable, [executable], workdir=str(docs),
                        env_overlay={'LD_LIBRARY_PATH': '/opt/wemeet/lib'},
                        as_host_user=args.as_host_user)
    if args.as_host_user:
        service.start(ENV_ID)
        record = service.store.load(ENV_ID)
        package_user = service._exec_record(record, ['id'], timeout=20, native=False)
        application_user = service.exec_app(APP_ID, ['id'], capture=True, timeout=20)
        passwd = service.exec_app(APP_ID, ['getent', 'passwd', str(os.getuid())], capture=True, timeout=20)
        pulse = service.exec_app(APP_ID, ['pactl', 'info'], capture=True, timeout=20)
        owner_file = docs / 'uid-mapping-check.txt'
        touch = service.exec_app(APP_ID, ['touch', str(owner_file)], capture=True, timeout=20)
        identity = {'package_user': package_user.__dict__, 'application_user': application_user.__dict__,
                    'passwd': passwd.__dict__, 'pulse': pulse.__dict__, 'touch': touch.__dict__,
                    'host_file_uid': owner_file.stat().st_uid if owner_file.exists() else None,
                    'host_file_gid': owner_file.stat().st_gid if owner_file.exists() else None}
        save(root / 'identity-check.json', identity)
        need('uid=0(' in package_user.stdout, 'package management is not container root')
        need(f'uid={os.getuid()}(' in application_user.stdout, 'application is not the host UID')
        need(passwd.returncode == 0, 'application UID has no passwd entry')
        need(pulse.returncode == 0, 'application user cannot reach the audio server')
        need(touch.returncode == 0 and identity['host_file_uid'] == os.getuid(), 'host file ownership mismatch')
    if args.generate_zh_locale:
        command = ['localedef', '-i', 'zh_CN', '-f', 'UTF-8', 'zh_CN.UTF-8']
        service.start(ENV_ID)
        result = service._exec_record(service.store.load(ENV_ID), command, timeout=60, native=False)
        save(root / 'locale-repair.json', {'argv': command, 'exit_code': result.returncode,
                                         'stdout': result.stdout, 'stderr': result.stderr})
        need(result.returncode == 0, 'Chinese UTF-8 locale generation failed')
    save(root / 'launch-record.json', service.show_app(APP_ID))
    if args.reconfigure_vendor:
        command = ['dpkg-reconfigure', '-f', 'noninteractive', 'wemeet']
        service.start(ENV_ID)
        result = service._exec_record(service.store.load(ENV_ID), command, timeout=60, native=False)
        save(root / 'vendor-reconfigure.json', {'argv': command, 'exit_code': result.returncode,
                                              'stdout': result.stdout, 'stderr': result.stderr})
        need(result.returncode == 0, 'vendor post-install reconfiguration failed')
    diagnostics = service.exec_app(APP_ID, ['ldd', '/opt/wemeet/bin/wemeetapp'], capture=True, timeout=30)
    (root / 'ldd.txt').write_text(diagnostics.stdout + diagnostics.stderr)
    save(root / 'environment-installed.json', service.inspect(ENV_ID))
    service.stop(ENV_ID)
    if args.install_only:
        log('Tencent Meeting: native installation completed; dependency audit saved')
        return 0
    number = len(list(root.glob('launch-attempt-*'))) + 1
    directory = root / f'launch-attempt-{number}'; directory.mkdir()
    shutil.copyfile(__file__, directory / 'driver.py')
    attempt = {'started_at': now(), 'package_sha256': package['sha256'], 'installed': True,
               'launched': False, 'driver_sha256': sha(__file__), 'errors': [],
               'scope': 'Installation and initial application window only. No login, meeting, camera, microphone or screen-share acceptance.'}
    process = None
    try:
        log('Tencent Meeting: launching the unmodified vendor launcher')
        with (directory / 'startup.log').open('w') as output:
            process = subprocess.Popen([sys.executable, str(service.root / 'tools/ml.py'),
                                        'app', 'open', APP_ID, '--'], stdout=output,
                                       stderr=subprocess.STDOUT, cwd=service.root)
        probe = ResourceProbe(service)
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            sample = probe.sample(ENV_ID, include_disk=False)
            pids = app_pids(sample)
            windows = window_info(directory, '', pids=pids, resource_hint='wemeet') if pids else []
            if windows:
                main_window = max(windows, key=lambda w: w['width'] * w['height'])
                attempt['windows'] = windows
                attempt['screenshot'] = screenshot(directory, '', directory / 'startup.png',
                                                     pids=pids, resource_hint='wemeet',
                                                     window_id=main_window['internalId'])
                save(directory / 'resources.json', sample)
                attempt['launched'] = True
                break
            time.sleep(0.5)
        save(directory / 'last-resources.json', sample)
        need(attempt['launched'], f'no application window; launcher exit={process.poll()}')
        attempt['backend'] = attempt['windows'][0]['objectType']
        log('Tencent Meeting: visible application window reached')
    except Exception as exc:
        attempt['errors'].append(f'{type(exc).__name__}: {exc}')
        (directory / 'failure-traceback.txt').write_text(traceback.format_exc())
        log(f'Tencent Meeting launch failed: {exc}')
    finally:
        service.stop(ENV_ID)
        if process is not None:
            try: process.wait(timeout=12)
            except subprocess.TimeoutExpired: process.terminate(); process.wait(timeout=5)
        attempt['finished_at'] = now()
        save(directory / 'result.json', attempt)
        save(root / 'environment-final.json', service.inspect(ENV_ID))
    return 0 if attempt['launched'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
