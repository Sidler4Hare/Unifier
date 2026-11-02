#!/usr/bin/env python3
"""Prepare isolated startup tests from three stopped, native GUI environments.

Source containers are snapshotted by exact id, never started or modified. The
new local images and project-owned containers are retained for reproducibility.
No package or desktop configuration is installed on the host.
"""
import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.packages import HostObserver
from tools.probe_startup import TARGETS, save, service, sha, stamp


def prepare(run, source_project):
    if run.exists():
        raise RuntimeError('choose a new experiment directory')
    containers = json.loads(subprocess.check_output(
        ['podman', 'ps', '-a', '--format', 'json'], text=True))
    selected = {}
    for target in TARGETS:
        matches = [c for c in containers if c.get('Labels', {}).get('multilinux.env') ==
            'native-' + target and c.get('Labels', {}).get('multilinux.project') ==
            str(source_project)]
        if len(matches) != 1 or matches[0]['State'] != 'exited':
            raise RuntimeError(f'{target}: requires exactly one stopped source environment')
        selected[target] = matches[0]
    run.mkdir(parents=True)
    (run / 'work').mkdir()
    (run / 'observer').mkdir()
    paths = set(filter(None, subprocess.check_output(['git', 'ls-files', '-z',
        'src', 'tools', 'config', 'fixtures'], cwd=ROOT).decode().split('\0')))
    paths.update(['tools/prepare_startup.py', 'tools/probe_startup.py',
                  'tools/startup_observer.py'])
    for rel in paths:
        target = run / 'project' / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / rel, target)
    manifest = {'schema': 'multilinux.startup.v1', 'started_at': stamp(),
        'baseline_commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'],
            cwd=ROOT, text=True).strip(), 'host_before': HostObserver().snapshot(),
        'scope': 'Native Mousepad; container-cold versus environment-warm. '
                 'Filesystem caches retained; physical presentation unmeasured.',
        'source_project': str(source_project), 'samples_per_case': 30,
        'targets': {}, 'preparation_driver_sha256': sha(__file__)}
    save(run / 'manifest.json', manifest)
    config_path = run / 'project/config/targets.toml'
    config_text = config_path.read_text()
    config = tomllib.loads(config_text)
    lock = json.loads((run / 'project/config/images.lock.json').read_text())
    suffix = ''.join(c for c in stamp() if c.isdigit())[:14]
    for target, source in selected.items():
        tag = f'localhost/multilinux/startup-{target}:{suffix}'
        subprocess.run(['podman', 'commit', '--pause=false', source['Id'], tag],
                       capture_output=True, text=True, check=True, timeout=120)
        image = json.loads(subprocess.check_output(
            ['podman', 'image', 'inspect', tag], text=True))[0]
        digest = image['Digest']
        reference = tag.rsplit(':', 1)[0] + '@' + digest
        config_text = config_text.replace('source = ' + json.dumps(config['targets'][target]['source']),
                                          'source = ' + json.dumps(tag))
        lock['targets'][target].update(source=tag, manifest_digest=digest,
            source_manifest_digest=digest, locked_reference=reference,
            image_created_at=image.get('Created'), compressed_layer_bytes=None)
        manifest['targets'][target] = {'source_container_id': source['Id'],
            'source_container_name': source['Names'][0], 'source_state': source['State'],
            'source_base_image': source['Image'], 'image_tag': tag,
            'image_id': image['Id'], 'image_digest': digest, 'locked_reference': reference}
        save(run / 'manifest.json', manifest)
    config_path.write_text(config_text)
    save(run / 'project/config/images.lock.json', lock)
    lab = service(run)
    try:
        for target in TARGETS:
            env_id, app_id = 'startup-' + target, 'startup-editor-' + target
            work = run / 'work' / target
            work.mkdir()
            created = lab.create(target, env_id, [str(work)], 'pasta', desktop=True,
                user_namespace='keep-id', portals=True, gpu=True)
            save(run / (target + '-environment.json'), created)
            launch = lab.add_app(env_id, app_id, '/usr/bin/dbus-run-session',
                ['/usr/bin/dbus-run-session', '--', '/usr/bin/mousepad', '--disable-server'],
                workdir=str(work), as_host_user=True, on_demand=True,
                env_overlay={'GDK_BACKEND': 'wayland', 'GSETTINGS_BACKEND': 'memory',
                    'WAYLAND_DEBUG': 'client', 'LC_ALL': 'C.UTF-8', 'XDG_CURRENT_DESKTOP': 'KDE'})
            save(run / (target + '-launch.json'), launch)
            help_result = lab.exec_app(app_id, ['/usr/bin/mousepad', '--help'],
                                       capture=True, timeout=20)
            if help_result.returncode or '--disable-server' not in help_result.stdout:
                raise RuntimeError(f'{target}: Mousepad private-instance preflight failed')
            version = lab.exec_app(app_id, ['/usr/bin/mousepad', '--version'],
                                   capture=True, timeout=20)
            if version.returncode:
                raise RuntimeError(f'{target}: Mousepad version query failed')
            manifest['targets'][target].update(environment_id=env_id, app_id=app_id,
                mousepad_version=version.stdout.strip(),
                package_inventory_sha256=lab.store.load(env_id)['package_inventory_sha256'])
            shutil.copyfile(lab.store.inventory_path(env_id), run / (target + '-packages.txt'))
            lab.stop(env_id)
            save(run / 'manifest.json', manifest)
    finally:
        for target in TARGETS:
            if lab.store.exists('startup-' + target):
                lab.stop('startup-' + target)
    manifest['prepared_at'] = stamp()
    save(run / 'manifest.json', manifest)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    parser.add_argument('--source-project', type=Path, default=ROOT /
        'evidence/wayland-media-2026-10-22T082942+0800/project')
    args = parser.parse_args()
    prepare(args.run_dir.expanduser().resolve(), args.source_project.expanduser().resolve())


if __name__ == '__main__':
    main()
