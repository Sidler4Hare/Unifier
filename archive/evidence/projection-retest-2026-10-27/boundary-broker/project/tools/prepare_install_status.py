#!/usr/bin/env python3
"""Prepare an isolated Arch environment for install-status accuracy tests.

The source container is a retained, stopped Arch experiment environment that
already contains the real Sunshine package and a synced pacman database.  It is
committed to a local image (the source container is not modified), a fresh
project snapshot and environment are created, and two synthetic local packages
are built:

  ml-benign     -- succeeds without maintenance-hook output
  ml-hooktest   -- its post-transaction hook runs /bin/false; pacman still
                   returns 0 but writes "error: command failed to execute
                   correctly" to stderr.

The resulting run directory is later driven by tools/probe_install_status.py.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
STARTUP_CONFIG = ROOT / 'evidence/startup-2026-10-22T182400+0800/project/config'
DEFAULT_SOURCE = 'ml-edae1d18-complex-arch'


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def run(argv, *, cwd=None, timeout=120, check=True):
    result = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                            timeout=timeout, check=False)
    if check and result.returncode:
        raise RuntimeError(f"{' '.join(argv)} exited {result.returncode}: "
                           f"{(result.stderr or result.stdout)[-2000:]}")
    return result


def podman(*args, timeout=120, check=True):
    return run(['podman', *args], timeout=timeout, check=check)


def snapshot_project(experiment):
    project = experiment / 'project'
    for name in ('src', 'tools', 'config'):
        target = project / name
        if target.exists():
            raise RuntimeError(f'refusing to overwrite {target}')
        shutil.copytree(ROOT / name, target,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    files = {}
    for path in sorted(project.rglob('*')):
        if path.is_file():
            files[str(path.relative_to(project))] = sha(path)
    git = run(['git', 'rev-parse', 'HEAD'], cwd=ROOT).stdout.strip()
    status = run(['git', 'status', '--porcelain'], cwd=ROOT).stdout
    return {'git_head': git, 'git_status': status, 'files': files}


def commit_image(run, source, suffix):
    inspected = json.loads(podman('inspect', source).stdout)[0]
    if inspected['State']['Running'] or inspected['State']['Status'] != 'exited':
        raise RuntimeError(f'source container {source} must be stopped')
    tag = f'localhost/multilinux/installstatus-arch:{suffix}'
    podman('commit', '--pause=false', source, tag, timeout=900)
    image = json.loads(podman('image', 'inspect', tag).stdout)[0]
    return {
        'source_container': source,
        'source_container_id': inspected['Id'],
        'source_state': inspected['State']['Status'],
        'image_tag': tag,
        'image_id': image['Id'],
        'image_digest': image['Digest'],
        'image_created_at': image.get('Created'),
        'locked_reference': tag.rsplit(':', 1)[0] + '@' + image['Digest'],
    }


def write_config(run, image):
    project = run / 'project'
    targets = (STARTUP_CONFIG / 'targets.toml').read_text()
    lock = json.loads((STARTUP_CONFIG / 'images.lock.json').read_text())
    tag = image['image_tag']
    digest = image['image_digest']
    repository = tag.rsplit(':', 1)[0]
    old = 'source = "localhost/multilinux/startup-arch:20261022-1824"'
    if old not in targets:
        raise RuntimeError('unexpected startup target source')
    targets = targets.replace(old, f'source = "{tag}"')
    lock['targets']['arch'].update({
        'source': tag,
        'source_manifest_digest': digest,
        'manifest_digest': digest,
        'locked_reference': repository + '@' + digest,
        'image_created_at': image['image_created_at'],
        'compressed_layer_bytes': None,
    })
    (project / 'config/targets.toml').write_text(targets)
    save(project / 'config/images.lock.json', lock)


def build_package(directory, name, version, *, failing_hook):
    pkgroot = directory / (name + '-root')
    (pkgroot / 'usr/share' / name).mkdir(parents=True)
    (pkgroot / 'usr/share' / name / 'payload.txt').write_text(
        f'{name} payload\n', encoding='utf-8')
    payload_size = len((pkgroot / 'usr/share' / name / 'payload.txt').read_bytes())
    (pkgroot / '.PKGINFO').write_text(
        f'pkgname = {name}\n'
        f'pkgver = {version}\n'
        f'pkgdesc = Multilinux install-status verification package\n'
        f'url = https://example.invalid/\n'
        f'builddate = 1761000000\n'
        f'packager = Multilinux Test\n'
        f'size = {payload_size}\n'
        f'arch = x86_64\n'
        f'license = MIT\n', encoding='utf-8')
    (pkgroot / '.INSTALL').write_text(
        'post_install() {\n'
        f'  echo "{name} post_install executes"\n'
        '  return 0\n'
        '}\n', encoding='utf-8')
    if failing_hook:
        hooks = pkgroot / 'usr/share/libalpm/hooks'
        hooks.mkdir(parents=True)
        (hooks / (name + '.hook')).write_text(
            '[Trigger]\n'
            'Operation = Install\n'
            'Type = Path\n'
            f'Target = usr/share/{name}/*\n'
            '[Action]\n'
            f'Description = {name} test hook that fails on purpose\n'
            'When = PostTransaction\n'
            'Exec = /bin/false\n', encoding='utf-8')
    archive = directory / f'{name}-{version}-x86_64.pkg.tar.zst'
    if archive.exists():
        raise RuntimeError(f'{archive} already exists')
    entries = ['.PKGINFO', '.INSTALL', 'usr']
    tar = directory / (name + '.tar')
    run(['tar', '-cf', str(tar), *entries], cwd=pkgroot)
    run(['zstd', '-q', '-19', '-f', str(tar), '-o', str(archive)])
    tar.unlink()
    shutil.rmtree(pkgroot)
    return {'name': name, 'version': version, 'path': str(archive),
            'sha256': sha(archive), 'failing_hook': failing_hook}


def create_environment(run):
    sys.path[:0] = [str(run / 'project/src'), str(run / 'project')]
    from multilinux.runtime import PodmanRuntime
    from multilinux.service import EnvironmentService
    import tomllib
    project = run / 'project'
    config = tomllib.loads((project / 'config/targets.toml').read_text())
    lock = json.loads((project / 'config/images.lock.json').read_text())
    service = EnvironmentService(project, PodmanRuntime(), config, lock)
    record = service.create('arch', 'install-arch', [], 'pasta')
    save(run / 'environment.json', record)
    inventory = (project / record['package_inventory_path']).read_text()
    (run / 'environment-packages.txt').write_text(inventory)
    service.stop('install-arch')
    return record


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--source-container', default=DEFAULT_SOURCE)
    p.add_argument('--suffix', default=''.join(c for c in stamp() if c.isdigit())[:14])
    a = p.parse_args()
    run_dir = a.run_dir.expanduser().resolve()
    if run_dir.exists():
        raise SystemExit(f'choose a new run directory: {run_dir}')
    for tool in ('podman', 'tar', 'zstd'):
        if not shutil.which(tool):
            raise SystemExit(f'{tool} is required')
    run_dir.mkdir(parents=True)
    (run_dir / 'packages').mkdir()
    (run_dir / 'work').mkdir()
    manifest = {'schema': 'multilinux.install-status.v1', 'started_at': stamp(),
                'scope': 'Layered package transaction status on a real Arch '
                         'environment; synthetic hook failure plus optionally the '
                         'retained Sunshine package.',
                'project': snapshot_project(run_dir),
                'preparation_source_sha256': sha(__file__)}
    save(run_dir / 'manifest.json', manifest)
    image = commit_image(run_dir, a.source_container, a.suffix)
    manifest['image'] = image
    save(run_dir / 'manifest.json', manifest)
    write_config(run_dir, image)
    packages = [
        build_package(run_dir / 'packages', 'ml-benign', '1.0.1-1', failing_hook=False),
        build_package(run_dir / 'packages', 'ml-hooktest', '1.0.1-1', failing_hook=True),
    ]
    manifest['packages'] = packages
    save(run_dir / 'manifest.json', manifest)
    record = create_environment(run_dir)
    manifest['environment'] = {
        'id': record['id'], 'target': record['target'],
        'writable_revision': record['writable_revision'],
        'content_revision': record['content_revision'],
        'package_inventory_sha256': record['package_inventory_sha256'],
    }
    manifest['prepared_at'] = stamp()
    save(run_dir / 'manifest.json', manifest)
    print(json.dumps({'run_dir': str(run_dir), 'image': image['image_tag'],
                      'environment': record['id'], 'packages': packages},
                     ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
