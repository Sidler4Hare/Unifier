#!/usr/bin/env python3
"""Install and launch the user-supplied Tencent Meeting deb in its own Debian root."""

import argparse
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
from tools.probe_desktop_apps import save, service_for, sha, log, need
from tools.kwin_window_probe import window_info, screenshot
from multilinux.resources import ResourceProbe
from multilinux.service import now

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
    args = parser.parse_args()
    run_dir = args.run_dir.resolve(); service = service_for(run_dir)
    docs = run_dir / 'documents/tencent-meeting'; docs.mkdir(parents=True, exist_ok=True)
    root = run_dir / 'tencent-meeting'; root.mkdir(exist_ok=True)
    package = inspect_package(args.deb)
    save(root / 'package.json', package)
    if not service.store.exists(ENV_ID):
        service.create('debian13', ENV_ID, [str(docs)], 'pasta', desktop=True)
    present = service.query_package(ENV_ID, 'wemeet')
    if not present['present']:
        log('Tencent Meeting: installing the original deb with native APT')
        transaction = service.install_local(ENV_ID, str(args.deb), package_name='wemeet',
                                            use_argv=['test', '-x', '/opt/wemeet/wemeetapp.sh'])
        save(root / 'install-transaction.json', transaction)
        need(transaction['phase'] == 'completed', f'local deb install failed: {transaction.get("native_error")} {transaction.get("verification")}')
    if args.repair_package:
        transaction = service.install_packages(ENV_ID, args.repair_package)
        save(root / ('repair-' + transaction['id'] + '.json'), transaction)
        need(transaction['phase'] == 'completed', 'native dependency repair failed')
    save(root / 'package-query.json', service.query_package(ENV_ID, 'wemeet'))
    shutil.copyfile(service.store.inventory_path(ENV_ID), root / 'packages.txt')
    if not service._launch_exists(APP_ID):
        executable = '/opt/wemeet/wemeetapp.sh'
        service.add_app(ENV_ID, APP_ID, executable, [executable], workdir=str(docs),
                        env_overlay={'LD_LIBRARY_PATH': '/opt/wemeet/lib'})
    if args.generate_zh_locale:
        command = ['localedef', '-i', 'zh_CN', '-f', 'UTF-8', 'zh_CN.UTF-8']
        result = service.exec_app(APP_ID, command, capture=True, timeout=60)
        save(root / 'locale-repair.json', {'argv': command, 'exit_code': result.returncode,
                                         'stdout': result.stdout, 'stderr': result.stderr})
        need(result.returncode == 0, 'Chinese UTF-8 locale generation failed')
    save(root / 'launch-record.json', service.show_app(APP_ID))
    if args.reconfigure_vendor:
        command = ['dpkg-reconfigure', '-f', 'noninteractive', 'wemeet']
        result = service.exec_app(APP_ID, command, capture=True, timeout=60)
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
            process = subprocess.Popen([sys.executable, str(run_dir / 'project/tools/ml.py'),
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
            if process.poll() is not None:
                break
            time.sleep(0.5)
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
