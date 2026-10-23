#!/usr/bin/env python3
"""Prepare an isolated concurrency-verification run.

The script freezes the current work tree into a new evidence directory, copies
the local startup-* image configuration from the previous startup round (no
pull, no refresh), and creates five stopped desktop environments:

    conc-debian   Debian 13  (shared topology / performance / agile waves)
    conc-arch     Arch Linux (second distribution)
    conc-sep-1    Debian 13  (one-environment-per-process control)
    conc-sep-2    Debian 13
    conc-sep-3    Debian 13

Every environment gets one on-demand Mousepad launch record, exactly like a
managed user application.  Existing containers, environments and evidence are
never modified.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.prepare_lifecycle import (  # noqa: E402  - shared evidence helpers
    command, git_state, host_snapshot, read_json, save, session_summary,
    sha256_file, stamp,
)

TZ = ZoneInfo('Asia/Shanghai')
SOURCE_CONFIG_DEFAULT = (ROOT / 'evidence/startup-2026-10-22T182400+0800'
                         / 'project/config')
SNAPSHOT_PATHS = ('src', 'tools', 'config', 'fixtures')
TOOL_PATHS = (
    'tools/prepare_concurrency.py', 'tools/probe_concurrency.py',
    'tools/prepare_lifecycle.py', 'tools/probe_lifecycle.py',
    'tools/startup_observer.py', 'tools/kwin_window_probe.py',
)
ENV_SPECS = (
    {'env_id': 'conc-debian', 'target': 'debian13', 'app_id': 'conc-editor-debian'},
    {'env_id': 'conc-arch', 'target': 'arch', 'app_id': 'conc-editor-arch'},
    {'env_id': 'conc-sep-1', 'target': 'debian13', 'app_id': 'conc-editor-sep-1'},
    {'env_id': 'conc-sep-2', 'target': 'debian13', 'app_id': 'conc-editor-sep-2'},
    {'env_id': 'conc-sep-3', 'target': 'debian13', 'app_id': 'conc-editor-sep-3'},
)
APP_ENV = (
    ('GDK_BACKEND', 'wayland'),
    ('GSETTINGS_BACKEND', 'memory'),
    ('WAYLAND_DEBUG', 'client'),
    ('LC_ALL', 'C.UTF-8'),
    ('XDG_CURRENT_DESKTOP', 'KDE'),
)


def log(message: str) -> None:
    print(message, flush=True)


def snapshot_files() -> list[str]:
    raw = command(['git', 'ls-files', '-co', '--exclude-standard', '-z',
                   *SNAPSHOT_PATHS], cwd=ROOT, timeout=120).stdout
    paths = sorted({item for item in raw.split('\0') if item})
    for relative in TOOL_PATHS:
        if (ROOT / relative).is_file() and relative not in paths:
            paths.append(relative)
    return sorted(paths)


def copy_worktree(run: Path) -> dict:
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
        origin[name] = {'source': str(source.resolve()), 'sha256': sha256_file(target)}
    return origin


def verify_images(run: Path) -> dict:
    lock = json.loads((run / 'project/config/images.lock.json').read_text())
    result = {}
    for target in ('debian13', 'arch'):
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
                f'{target}: local image {reference} has digest {digest}, expected {expected}')
    return result


def write_snapshot(run: Path, source_config: Path, manifest: dict) -> None:
    files = copy_worktree(run)
    origin = copy_image_config(run, source_config)
    file_hashes = dict(files)
    for name in ('targets.toml', 'images.lock.json'):
        file_hashes['config/' + name] = origin[name]['sha256']
    snapshot = {
        'created_at': stamp(),
        'source_config': str(source_config.resolve()),
        'config_origin': origin,
        'paths': sorted(file_hashes),
        'files_sha256': file_hashes,
        'snapshot_sha256': hashlib.sha256(
            json.dumps(file_hashes, sort_keys=True).encode()).hexdigest(),
        'git': git_state(),
        'host': host_snapshot(),
        'session': session_summary(),
        'konsole': {
            'path': shutil.which('konsole'),
            'version': command(['konsole', '--version'], timeout=30).stdout.strip(),
        },
        'host_packages_sha256': host_packages_hash(),
        'host_desktop_entries_sha256': host_desktop_entries_hash(),
    }
    manifest['snapshot'] = snapshot
    save(run / 'manifest.json', manifest)
    save(run / 'snapshot.json', snapshot)


def host_packages_hash() -> str:
    try:
        text = command(['rpm', '-qa'], timeout=120).stdout
    except Exception:
        return 'unavailable'
    return hashlib.sha256('\n'.join(sorted(text.splitlines())).encode()).hexdigest()


def host_desktop_entries_hash() -> str:
    entries = []
    for directory in (Path('/usr/share/applications'),
                      Path.home() / '.local/share/applications'):
        try:
            entries.extend(sorted(path.name for path in directory.iterdir()
                                  if path.suffix == '.desktop'))
        except OSError:
            continue
    return hashlib.sha256('\n'.join(sorted(set(entries))).encode()).hexdigest()


def cli(run: Path, *argv, timeout=600):
    return command([sys.executable, str(run / 'project/tools/ml.py'), *argv],
                   cwd=run, timeout=timeout)


def prepare_snapshot(run: Path, source_config: Path) -> dict:
    run.mkdir(parents=True, exist_ok=False)
    for name in ('samples', 'observer', 'work', 'prepare'):
        (run / name).mkdir()
    manifest = {
        'schema': 'multilinux.concurrency.v1',
        'run_id': run.name,
        'evidence_dir': str(run.resolve()),
        'prepared_at': stamp(),
        'scope': ('Real rootless Podman concurrency verification on the Fedora 44 / '
                  'KDE host: shared environments, two distributions plus the native '
                  'host distribution, one-environment-per-process control, performance '
                  'and agile start/stop. GUI waves are serial except the windows that '
                  'belong to the same designed wave.'),
        'source_config': str(source_config.resolve()),
        'env_specs': list(ENV_SPECS),
        'tool_sha256': {},
        'resnapshots': [],
    }
    for relative in TOOL_PATHS:
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
        raise RuntimeError(f'not a prepared concurrency run: {run}')
    manifest = read_json(path)
    previous = manifest.get('snapshot', {}).get('git', {}).get('head')
    write_snapshot(run, source_config, manifest)
    manifest['resnapshots'].append({
        'at': manifest['snapshot']['created_at'],
        'previous_git_head': previous,
        'git': manifest['snapshot']['git'],
        'snapshot_sha256': manifest['snapshot']['snapshot_sha256'],
    })
    for relative in TOOL_PATHS:
        target = ROOT / relative
        if target.is_file():
            manifest['tool_sha256'][relative] = sha256_file(target)
    save(run / 'manifest.json', manifest)
    log(json.dumps({'resnapshot': True, 'git_head': manifest['snapshot']['git']['head'],
                    'snapshot_sha256': manifest['snapshot']['snapshot_sha256'],
                    'dirty': manifest['snapshot']['git']['dirty'],
                    'files': len(manifest['snapshot']['files_sha256'])},
                   ensure_ascii=False))


def create_environments(run: Path, manifest: dict) -> None:
    log_dir = run / 'prepare'
    log_dir.mkdir(parents=True, exist_ok=True)
    manifest.setdefault('environments', {})
    manifest.setdefault('apps', {})
    manifest.setdefault('env_map', {})
    manifest['creation_started_at'] = stamp()
    for spec in ENV_SPECS:
        env_id, target_name, app_id = spec['env_id'], spec['target'], spec['app_id']
        work = run / 'work' / env_id
        work.mkdir(parents=True, exist_ok=True)
        started = time.monotonic()
        argv = ['env', 'create', '--target', target_name, '--id', env_id,
                '--share', str(work), '--network', 'none', '--desktop',
                '--portals', '--userns', 'keep-id']
        result = cli(run, *argv)
        created_seconds = round(time.monotonic() - started, 3)
        (log_dir / f'{env_id}-create.log').write_text(
            f"$ {' '.join(argv)}\nreturncode={result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}\n",
            encoding='utf-8')
        if result.returncode:
            raise RuntimeError(f'{env_id}: environment creation failed: {result.stderr[-3000:]}')
        record = read_json(run / 'project/.lab/state/environments' / (env_id + '.json'))
        inspect = json.loads(command(['podman', 'inspect',
                                      record['runtime']['container_name']], timeout=120).stdout)[0]
        manifest['environments'][env_id] = {
            'target': target_name,
            'created_at': stamp(),
            'creation_seconds': created_seconds,
            'record': record,
            'container_init': (inspect.get('HostConfig') or {}).get('Init'),
            'inspect_state': inspect.get('State'),
        }
        app_started = time.monotonic()
        app_argv = ['app', 'add', env_id, app_id,
                    '--exec', '/usr/bin/dbus-run-session',
                    '--workdir', str(work),
                    '--as-host-user', '--on-demand']
        for key, value in APP_ENV:
            app_argv.extend(['--set', f'{key}={value}'])
        app_argv.extend(['--', '--', '/usr/bin/mousepad', '--disable-server'])
        added = cli(run, *app_argv)
        app_seconds = round(time.monotonic() - app_started, 3)
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
        help_result = cli(run, 'app', 'exec', app_id, '--', '/usr/bin/mousepad', '--help',
                          timeout=120)
        version_result = cli(run, 'app', 'exec', app_id, '--', '/usr/bin/mousepad', '--version',
                             timeout=120)
        (log_dir / f'{app_id}-preflight.log').write_text(
            f"mousepad --help returncode={help_result.returncode}\n{help_result.stdout}\n"
            f"{help_result.stderr}\n"
            f"mousepad --version returncode={version_result.returncode}\n"
            f"{version_result.stdout}\n{version_result.stderr}\n", encoding='utf-8')
        if help_result.returncode or '--disable-server' not in (
                help_result.stdout + help_result.stderr):
            raise RuntimeError(f'{app_id}: Mousepad private-instance preflight failed')
        if version_result.returncode:
            raise RuntimeError(f'{app_id}: Mousepad version query failed')
        stop = cli(run, 'env', 'stop', env_id)
        if stop.returncode:
            raise RuntimeError(f'{env_id}: initial stop failed: {stop.stderr[-2000:]}')
        record = read_json(run / 'project/.lab/state/environments' / (env_id + '.json'))
        inventory_path = run / 'project' / record['package_inventory_path']
        inventory = inventory_path.read_text(encoding='utf-8') if inventory_path.is_file() else ''
        manifest['environments'][env_id].update({
            'app_id': app_id,
            'app_registration_seconds': app_seconds,
            'mousepad_version': version_result.stdout.strip(),
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
        manifest['env_map'][env_id] = {'target': target_name, 'app_id': app_id,
                                       'work': str(work)}
        save(run / 'manifest.json', manifest)
        log(json.dumps({'environment': env_id, 'target': target_name, 'app': app_id,
                        'creation_seconds': created_seconds,
                        'mousepad_version': version_result.stdout.strip().splitlines()[0],
                        'network': 'none', 'desktop': True, 'portals': True,
                        'userns': 'keep-id'}, ensure_ascii=False))
    manifest['creation_finished_at'] = stamp()
    manifest['host_after_create'] = host_snapshot()
    save(run / 'manifest.json', manifest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(dest='operation', required=True)
    for name, help_text in (('snapshot', 'create a new evidence run and snapshot the work tree'),
                            ('create', 'create the five stopped test environments'),
                            ('resnapshot', 'overwrite project/ with the current work tree'),
                            ('all', 'snapshot and create in one call')):
        sub = operations.add_parser(name, help=help_text)
        sub.add_argument('--run-dir', type=Path, required=True)
        if name in {'snapshot', 'resnapshot', 'all'}:
            sub.add_argument('--source-config', type=Path, default=SOURCE_CONFIG_DEFAULT)
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    try:
        if args.operation in {'snapshot', 'all'}:
            if run.exists():
                raise SystemExit(f'choose a new evidence directory: {run}')
            prepare_snapshot(run, args.source_config.expanduser().resolve())
            log(json.dumps({'snapshot': str(run / 'snapshot.json'),
                            'manifest': str(run / 'manifest.json')}, ensure_ascii=False))
        if args.operation == 'resnapshot':
            resnapshot(run, args.source_config.expanduser().resolve())
        if args.operation in {'create', 'all'}:
            manifest = read_json(run / 'manifest.json')
            create_environments(run, manifest)
            manifest['created_at'] = stamp()
            manifest.pop('preparation_error', None)
            save(run / 'manifest.json', manifest)
            log(json.dumps({'environments': list(manifest['environments']),
                            'apps': list(manifest['apps'])}, ensure_ascii=False))
    except RuntimeError as exc:
        print(f'prepare_concurrency: {exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
