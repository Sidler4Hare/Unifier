#!/usr/bin/env python3
"""Prepare an isolated real-container lifecycle verification run.

The script freezes the current work tree (src/, tools/, config/) into a new
evidence directory, replaces the working-tree image lock with the local
startup-* image configuration from the previous startup round (never pulls or
refreshes anything), and creates two fresh Debian 13 and Arch Linux desktop
environments plus one on-demand Mousepad launch record in each.

It deliberately never touches existing containers or environments: every
container it creates belongs to the new evidence directory's project root and
is named/labelled by the product code itself.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TZ = ZoneInfo('Asia/Shanghai')
SOURCE_CONFIG_DEFAULT = (ROOT / 'evidence/startup-2026-10-22T182400+0800'
                         / 'project/config')
TARGETS = {
    'debian13': {'env_id': 'test-debian', 'app_id': 'life-editor-debian'},
    'arch': {'env_id': 'test-arch', 'app_id': 'life-editor-arch'},
}
SNAPSHOT_PATHS = ('src', 'tools', 'config', 'fixtures')
PROJECT_TOOLS = ('tools/prepare_lifecycle.py', 'tools/probe_lifecycle.py',
                 'tools/startup_observer.py', 'tools/kwin_window_probe.py')
APP_ENV = (
    ('GDK_BACKEND', 'wayland'),
    ('GSETTINGS_BACKEND', 'memory'),
    ('WAYLAND_DEBUG', 'client'),
    ('LC_ALL', 'C.UTF-8'),
    ('XDG_CURRENT_DESKTOP', 'KDE'),
)


def stamp() -> str:
    return datetime.now(TZ).isoformat(timespec='seconds')


def save(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n',
                         encoding='utf-8')
    temporary.replace(path)


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def command(argv, *, cwd=None, timeout=300, check=True):
    result = subprocess.run([str(item) for item in argv], cwd=cwd,
                            capture_output=True, text=True, timeout=timeout,
                            check=False)
    if check and result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(map(str, argv))}\n"
            f"stderr: {result.stderr[-3000:]}")
    return result


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
        thermal.append({'zone': zone.name, 'type': kind, 'temp_c': round(value / 1000, 2)})
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


def session_summary() -> dict:
    keys = ('WAYLAND_DISPLAY', 'DISPLAY', 'XDG_RUNTIME_DIR', 'XAUTHORITY',
            'DBUS_SESSION_BUS_ADDRESS', 'XDG_CURRENT_DESKTOP', 'XDG_SESSION_TYPE')
    return {key: os.environ.get(key) for key in keys}


def git_state() -> dict:
    def run(*argv):
        return command(['git', *argv], cwd=ROOT, timeout=60).stdout

    status = run('status', '--porcelain')
    return {
        'head': run('rev-parse', 'HEAD').strip(),
        'branch': run('rev-parse', '--abbrev-ref', 'HEAD').strip(),
        'status_porcelain': status.splitlines(),
        'status_sha256': hashlib.sha256(status.encode()).hexdigest(),
        'dirty': bool(status.strip()),
        'diff_stat': run('diff', '--stat').strip().splitlines(),
    }


def snapshot_files() -> list[str]:
    raw = command(['git', 'ls-files', '-co', '--exclude-standard', '-z',
                   *SNAPSHOT_PATHS], cwd=ROOT, timeout=120).stdout
    paths = sorted({item for item in raw.split('\0') if item})
    for relative in PROJECT_TOOLS:
        if (ROOT / relative).is_file() and relative not in paths:
            paths.append(relative)
    return sorted(paths)


def copy_worktree(run: Path) -> dict:
    """Copy the live work tree into run/project and return its fingerprint."""
    files = {}
    for relative in snapshot_files():
        source = ROOT / relative
        target = run / 'project' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        files[relative] = sha256_file(target)
    return files


def copy_image_config(run: Path, source_config: Path) -> dict:
    run_config = run / 'project/config'
    run_config.mkdir(parents=True, exist_ok=True)
    origin = {}
    for name in ('targets.toml', 'images.lock.json'):
        source = source_config / name
        if not source.is_file():
            raise RuntimeError(f'missing local image configuration: {source}')
        target = run_config / name
        shutil.copyfile(source, target)
        origin[name] = {
            'source': str(source.resolve()),
            'sha256': sha256_file(target),
        }
    return origin


def verify_images(run: Path) -> dict:
    lock = json.loads((run / 'project/config/images.lock.json').read_text())
    result = {}
    for target in TARGETS:
        item = lock['targets'][target]
        reference = item['locked_reference']
        expected = item['manifest_digest']
        inspected = json.loads(command(['podman', 'image', 'inspect', reference],
                                       timeout=120).stdout)[0]
        digest = inspected.get('Digest')
        result[target] = {
            'tag': item['source'],
            'locked_reference': reference,
            'expected_digest': expected,
            'local_digest': digest,
            'digest_matches': digest == expected,
            'image_id': inspected.get('Id'),
            'created': inspected.get('Created'),
            'size_bytes': inspected.get('Size'),
        }
        if digest != expected:
            raise RuntimeError(
                f'{target}: local image {reference} has digest {digest}, '
                f'expected {expected}; refusing to continue without the exact image')
    return result


def write_snapshot(run: Path, source_config: Path, manifest: dict) -> None:
    started = stamp()
    files = copy_worktree(run)
    origin = copy_image_config(run, source_config)
    file_hashes = {relative: sha for relative, sha in files.items()}
    for name in ('targets.toml', 'images.lock.json'):
        file_hashes['config/' + name] = origin[name]['sha256']
    snapshot = {
        'created_at': started,
        'source_config': str(source_config.resolve()),
        'config_origin': origin,
        'paths': sorted(file_hashes),
        'files_sha256': file_hashes,
        'snapshot_sha256': hashlib.sha256(
            json.dumps(file_hashes, sort_keys=True).encode()).hexdigest(),
        'git': git_state(),
        'host': host_snapshot(),
        'session': session_summary(),
        'git_branch': None,
    }
    snapshot['git_branch'] = snapshot['git']['branch']
    manifest['snapshot'] = snapshot
    manifest['snapshot_pending_recreate'] = True
    save(run / 'manifest.json', manifest)
    save(run / 'snapshot.json', snapshot)


def cli(run: Path, *argv, timeout=300, check=True):
    return command([sys.executable, str(run / 'project/tools/ml.py'), *argv],
                   cwd=run, timeout=timeout, check=check)


def read_json(path: Path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def create_environments(run: Path, manifest: dict) -> None:
    log_dir = run / 'prepare'
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest.setdefault('environments', {})
    manifest.setdefault('apps', {})
    for target, ids in TARGETS.items():
        env_id, app_id = ids['env_id'], ids['app_id']
        work = run / 'work' / env_id
        work.mkdir(parents=True, exist_ok=True)
        argv = ['env', 'create', '--target', target, '--id', env_id,
                '--share', str(work), '--network', 'none', '--desktop',
                '--portals', '--userns', 'keep-id']
        result = cli(run, *argv, timeout=600, check=False)
        (log_dir / f'{env_id}-create.log').write_text(
            f"$ {' '.join(argv)}\nreturncode={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
            encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'{env_id}: environment creation failed: {result.stderr[-3000:]}')
        record = read_json(run / 'project/.lab/state/environments' / (env_id + '.json'))
        inspect = json.loads(command(['podman', 'inspect', record['runtime']['container_name']],
                                     timeout=120).stdout)[0]
        manifest['environments'][env_id] = {
            'target': target,
            'created_at': stamp(),
            'record': record,
            'container_init': (inspect.get('HostConfig') or {}).get('Init'),
            'container_labels': (inspect.get('Config') or {}).get('Labels') or {},
            'inspect_state': inspect.get('State'),
        }
        app_argv = ['app', 'add', env_id, app_id,
                    '--exec', '/usr/bin/dbus-run-session',
                    '--workdir', str(work),
                    '--as-host-user', '--on-demand']
        for key, value in APP_ENV:
            app_argv.extend(['--set', f'{key}={value}'])
        app_argv.extend(['--', '--', '/usr/bin/mousepad', '--disable-server'])
        added = cli(run, *app_argv, timeout=300, check=False)
        (log_dir / f'{app_id}-add.log').write_text(
            f"$ {' '.join(app_argv)}\nreturncode={added.returncode}\n"
            f"--- stdout ---\n{added.stdout}\n--- stderr ---\n{added.stderr}\n",
            encoding='utf-8')
        if added.returncode:
            raise RuntimeError(f'{app_id}: application registration failed: {added.stderr[-3000:]}')
        launch = read_json(run / 'project/.lab/state/launches' / (app_id + '.json'))
        if not launch.get('on_demand') or launch.get('user') != f'{os.getuid()}:{os.getgid()}':
            raise RuntimeError(f'{app_id}: launch record is not the expected on-demand host-user app')
        for key, value in APP_ENV:
            if launch['env'].get(key) != value:
                raise RuntimeError(f'{app_id}: launch environment {key} was not persisted')
        # Preflight: the environment must be able to run the native GUI binary.
        help_result = cli(run, 'app', 'exec', app_id, '--', '/usr/bin/mousepad', '--help',
                          timeout=120, check=False)
        version_result = cli(run, 'app', 'exec', app_id, '--', '/usr/bin/mousepad', '--version',
                             timeout=120, check=False)
        (log_dir / f'{app_id}-preflight.log').write_text(
            f"mousepad --help returncode={help_result.returncode}\n{help_result.stdout}\n"
            f"{help_result.stderr}\n"
            f"mousepad --version returncode={version_result.returncode}\n"
            f"{version_result.stdout}\n{version_result.stderr}\n", encoding='utf-8')
        if help_result.returncode or '--disable-server' not in (help_result.stdout + help_result.stderr):
            raise RuntimeError(f'{app_id}: Mousepad private-instance preflight failed')
        if version_result.returncode:
            raise RuntimeError(f'{app_id}: Mousepad version query failed')
        stop = cli(run, 'env', 'stop', env_id, timeout=120, check=False)
        if stop.returncode:
            raise RuntimeError(f'{env_id}: initial stop failed: {stop.stderr[-2000:]}')
        record = read_json(run / 'project/.lab/state/environments' / (env_id + '.json'))
        inventory_path = run / 'project' / record['package_inventory_path']
        inventory = inventory_path.read_text(encoding='utf-8') if inventory_path.is_file() else ''
        manifest['environments'][env_id].update({
            'preflight_at': stamp(),
            'mousepad_version': version_result.stdout.strip(),
            'mousepad_help_ok': help_result.returncode == 0,
            'package_inventory_count': record.get('package_inventory_count'),
            'package_inventory_sha256': record.get('package_inventory_sha256'),
            'package_inventory_matches': (
                hashlib.sha256(inventory.encode()).hexdigest()
                == record.get('package_inventory_sha256')),
        })
        shutil.copyfile(run / 'project' / record['package_inventory_path'],
                        run / f'{env_id}-packages.txt')
        manifest['apps'][app_id] = {
            'environment_id': env_id,
            'registered_at': stamp(),
            'launch_record': launch,
        }
        save(run / 'manifest.json', manifest)
        print(json.dumps({'environment': env_id, 'app': app_id,
                          'mousepad_version': version_result.stdout.strip(),
                          'network': 'none', 'desktop': True, 'portals': True,
                          'userns': 'keep-id'}, ensure_ascii=False), flush=True)


def prepare_snapshot(run: Path, source_config: Path) -> dict:
    run.mkdir(parents=True, exist_ok=False)
    for name in ('samples', 'observer', 'work', 'prepare'):
        (run / name).mkdir()
    manifest = {
        'schema': 'multilinux.lifecycle.v1',
        'run_id': run.name,
        'evidence_dir': str(run.resolve()),
        'prepared_at': stamp(),
        'scope': ('Real rootless Podman environments on the Fedora 44 / KDE '
                  'host. Serial GUI measurements, container lifecycle and '
                  'lease ownership; physical display presentation is not '
                  'measured.'),
        'source_config': str(source_config.resolve()),
        'targets': TARGETS,
        'tool_sha256': {},
        'resnapshots': [],
    }
    for relative in PROJECT_TOOLS:
        path = ROOT / relative
        if path.is_file():
            manifest['tool_sha256'][relative] = sha256_file(path)
    write_snapshot(run, source_config, manifest)
    manifest['images'] = verify_images(run)
    save(run / 'manifest.json', manifest)
    return manifest


def resnapshot(run: Path, source_config: Path) -> None:
    path = run / 'manifest.json'
    if not path.is_file():
        raise RuntimeError(f'not a prepared lifecycle run: {run}')
    manifest = read_json(path)
    previous = manifest.get('snapshot', {}).get('git', {}).get('head')
    write_snapshot(run, source_config, manifest)
    manifest['resnapshots'].append({
        'at': manifest['snapshot']['created_at'],
        'previous_git_head': previous,
        'git': manifest['snapshot']['git'],
        'snapshot_sha256': manifest['snapshot']['snapshot_sha256'],
    })
    for relative in PROJECT_TOOLS:
        target = ROOT / relative
        if target.is_file():
            manifest['tool_sha256'][relative] = sha256_file(target)
    save(run / 'manifest.json', manifest)
    print(json.dumps({'resnapshot': True, 'git_head': manifest['snapshot']['git']['head'],
                      'snapshot_sha256': manifest['snapshot']['snapshot_sha256'],
                      'dirty': manifest['snapshot']['git']['dirty'],
                      'files': len(manifest['snapshot']['files_sha256'])},
                     ensure_ascii=False), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    snapshot_parser = operations.add_parser('snapshot', help='create a new evidence run and snapshot the work tree')
    snapshot_parser.add_argument('--run-dir', type=Path, required=True)
    snapshot_parser.add_argument('--source-config', type=Path, default=SOURCE_CONFIG_DEFAULT)
    create_parser = operations.add_parser('create', help='create the two test environments and applications')
    create_parser.add_argument('--run-dir', type=Path, required=True)
    resnap_parser = operations.add_parser('resnapshot', help='overwrite project/ with the current final work tree')
    resnap_parser.add_argument('--run-dir', type=Path, required=True)
    resnap_parser.add_argument('--source-config', type=Path, default=SOURCE_CONFIG_DEFAULT)
    all_parser = operations.add_parser('all', help='snapshot and create in one call')
    all_parser.add_argument('--run-dir', type=Path, required=True)
    all_parser.add_argument('--source-config', type=Path, default=SOURCE_CONFIG_DEFAULT)
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    if args.operation in {'snapshot', 'all'}:
        if run.exists():
            raise SystemExit(f'choose a new evidence directory: {run}')
        prepare_snapshot(run, args.source_config.expanduser().resolve())
        print(json.dumps({'snapshot': str(run / 'snapshot.json'),
                          'manifest': str(run / 'manifest.json')},
                         ensure_ascii=False), flush=True)
    if args.operation == 'resnapshot':
        resnapshot(run, args.source_config.expanduser().resolve())
    if args.operation in {'create', 'all'}:
        manifest = read_json(run / 'manifest.json')
        create_environments(run, manifest)
        manifest['created_at'] = stamp()
        manifest.pop('preparation_error', None)
        save(run / 'manifest.json', manifest)
        print(json.dumps({'environments': list(manifest['environments']),
                          'apps': list(manifest['apps'])}, ensure_ascii=False), flush=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f'prepare_lifecycle: {exc}', file=sys.stderr)
        raise SystemExit(1)
