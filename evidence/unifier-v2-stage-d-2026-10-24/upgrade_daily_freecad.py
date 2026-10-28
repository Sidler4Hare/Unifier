"""Install only a verified code snapshot into the retained daily application.

Preserves config, application/environment identities, private HOME and host
project. Holds its existing lifecycle lock during the brief source swap.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import time
import tomllib
import uuid

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
APP = Path.home() / '.local/share/multilinux/apps/freecad'


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--gate', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    gate = json.loads(args.gate.read_text())
    assert gate['passed']
    assert all(digest(ROOT / p) == sha for p, sha in gate['source_sha256'].items())
    assert APP.is_dir() and not APP.is_symlink()
    sys.path[:0] = [str(APP / 'src'), str(APP)]
    from multilinux.service import EnvironmentService
    from multilinux.runtime import PodmanRuntime
    service = EnvironmentService(APP, PodmanRuntime(),
             tomllib.loads((APP / 'config/targets.toml').read_text()),
             json.loads((APP / 'config/images.lock.json').read_text()))
    nonce = uuid.uuid4().hex
    work = APP / ('.upgrade-' + nonce)
    work.mkdir(mode=0o700)
    backup = APP / 'backups'
    backup.mkdir(mode=0o700, exist_ok=True)
    archive = backup / ('code-before-v2-d-' + nonce + '.tar.gz')
    before_state = {str(p.relative_to(APP)): digest(p) for p in (APP / '.lab/state').rglob('*') if p.is_file()}
    before_config = {str(p.relative_to(APP)): digest(p) for p in (APP / 'config').rglob('*') if p.is_file()}
    desktop = Path.home() / '.local/share/applications/unifier-freecad.desktop'
    before_desktop = digest(desktop)
    for directory in ('src', 'tools'):
        shutil.copytree(ROOT / directory, work / directory, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    installed = []
    try:
        with service._lifecycle_lock('freecad-arch'):
            views = APP / '.lab/state/views/registry.json'
            assert not views.exists() or not json.loads(views.read_text())['records'], 'an active view must be revoked first'
            with tarfile.open(archive, 'w:gz') as stream:
                for directory in ('src', 'tools'):
                    stream.add(APP / directory, arcname=directory,
                               filter=lambda item: None if '__pycache__' in Path(item.name).parts else item)
            for directory in ('src', 'tools'):
                (APP / directory).rename(work / ('previous-' + directory))
                try:
                    (work / directory).rename(APP / directory)
                except Exception:
                    (work / ('previous-' + directory)).rename(APP / directory)
                    raise
                installed.append(directory)
            check = subprocess.run([sys.executable, str(APP / 'tools/ml.py'), '--help'], capture_output=True, text=True)
            assert check.returncode == 0, check.stderr[-1000:]
            assert all(digest(APP / p) == sha for p, sha in gate['source_sha256'].items())
            assert all(digest(APP / p) == sha for p, sha in before_state.items())
            assert all(digest(APP / p) == sha for p, sha in before_config.items())
            assert digest(desktop) == before_desktop
    except Exception:
        for directory in reversed(installed):
            shutil.rmtree(APP / directory)
            (work / ('previous-' + directory)).rename(APP / directory)
        raise
    result = {'passed': True, 'app_root': str(APP), 'source_sha256': gate['source_sha256'],
              'backup': str(archive), 'backup_sha256': digest(archive),
              'state_preserved': True, 'config_preserved': True, 'desktop_preserved': True,
              'environment_not_stopped_or_recreated': True, 'updated_at': time.time()}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    shutil.rmtree(work)
    print(json.dumps({'passed': True, 'app_root': str(APP), 'state_preserved': True}))


if __name__ == '__main__':
    main()
