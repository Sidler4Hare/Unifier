"""Activate the exact release at existing entry points, preserving data roots."""
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile

REPO = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BACKUP = Path('/home/omg/文档/Unifier-backups/20261030-224546-before-v0.0.1-fixes')
BUNDLE = REPO / 'artifacts/unifier-0.0.1.tar.gz'
digest = hashlib.sha256(BUNDLE.read_bytes()).hexdigest()
parent = Path.home() / '.local/share/unifier-code/releases'
parent.mkdir(parents=True, exist_ok=True)
release = parent / ('0.0.1-' + digest[:12])
if release.exists():
    raise SystemExit('release already installed; inspect the existing receipt before retrying')
with tempfile.TemporaryDirectory(prefix='.prepare-v001-', dir=parent) as temporary:
    with tarfile.open(BUNDLE) as archive:
        archive.extractall(temporary, filter='data')
    os.replace(Path(temporary) / 'unifier-0.0.1', release)
sys.path[:0] = [str(release / 'src'), str(release)]
from tools.check_repo import verify_bundle
from unifier.service import EnvironmentService
from unifier.runtime import PodmanRuntime
from unifier.install import load_catalog
verify_bundle(release)
config, lock = load_catalog(release)
files = {}
roots = {}
for app in ['freecad', 'wemeet']:
    root = Path.home() / '.local/share/multilinux/apps' / app
    roots[app] = root
    for name in ['tools/ml.py', 'tools/unifier.py', 'deployment-current.json']:
        path = root / name
        assert path.is_file() and not path.is_symlink(), path
        files[path] = (path.read_bytes(), path.stat().st_mode & 0o777)
launcher = Path.home() / '.local/bin/unifier'
assert launcher.is_symlink()
previous_launcher = os.readlink(launcher)
backup_file = BACKUP / 'release-entrypoints-before.tar.gz'
if backup_file.exists():
    raise SystemExit('backup already exists; refuse to overwrite')
with tarfile.open(backup_file, 'w:gz', dereference=False) as archive:
    for path in [*files, launcher]:
        archive.add(path, arcname=str(path.relative_to(Path.home())), recursive=False)
with tarfile.open(backup_file) as archive:
    for path, (content, mode) in files.items():
        assert archive.extractfile(str(path.relative_to(Path.home()))).read() == content
    assert archive.getmember(str(launcher.relative_to(Path.home()))).linkname == previous_launcher

def atomic(path, payload, mode):
    fd, name = tempfile.mkstemp(prefix='.unifier-v001-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'wb') as stream:
            stream.write(payload); stream.flush(); os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)

receipt = {'version': '0.0.1', 'release': str(release), 'bundle_sha256': digest,
           'backup': str(backup_file), 'deployments': {}}
try:
    # Validate real existing bindings before changing any entry point.
    for app, root in roots.items():
        service = EnvironmentService(root, PodmanRuntime(), config, lock)
        service.validate_app_binding(app)
        if app == 'wemeet':
            desktop = Path.home() / '.local/share/applications/wemeetapp.desktop'
            import shlex
            command = shlex.split(next(s[5:] for s in desktop.read_text().splitlines() if s.startswith('Exec=')))
            def arg(name): return command[command.index(name) + 1]
            service.validate_integration_launch(arg('--integration'), app_id=app,
                project_uuid=arg('--project'), app_instance_uuid=arg('--app-instance'),
                execution_fingerprint=arg('--execution-fingerprint'))
        receipt['deployments'][app] = {'binding_preflight': 'passed'}
    for app, root in roots.items():
        text = '''#!/usr/bin/env python3
"""Unifier v0.0.1 entry point; the historical data root stays authoritative."""
import os
from pathlib import Path
import sys
APP_ROOT = Path(ROOT_LITERAL)
RELEASE = Path(RELEASE_LITERAL)
environment = dict(os.environ)
environment["UNIFIER_HOME"] = str(APP_ROOT)
environment["PYTHONPATH"] = str(RELEASE / "src") + os.pathsep + str(RELEASE)
os.execve(sys.executable, [sys.executable, str(RELEASE / "tools/unifier.py"), *sys.argv[1:]], environment)
'''.replace('ROOT_LITERAL', repr(str(root))).replace('RELEASE_LITERAL', repr(str(release)))
        compile(text, str(root / 'tools/unifier.py'), 'exec')
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py') as candidate:
            candidate.write(text); candidate.flush()
            p = subprocess.run([sys.executable, candidate.name, '--version'], capture_output=True, text=True, check=True)
            assert p.stdout.strip() == 'Unifier 0.0.1'
        for name in ['tools/ml.py', 'tools/unifier.py']:
            path = root / name
            atomic(path, text.encode(), files[path][1])
            p = subprocess.run([sys.executable, str(path), '--version'], capture_output=True, text=True, check=True)
            assert p.stdout.strip() == 'Unifier 0.0.1'
        metadata = root / 'deployment-current.json'
        data = json.loads(metadata.read_text())
        data['active_entrypoint_release'] = {'version': '0.0.1', 'code_root': str(release), 'bundle_sha256': digest,
            'activated_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(), 'backup': str(backup_file)}
        atomic(metadata, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode(), files[metadata][1])
        receipt['deployments'][app]['both_entrypoints_version'] = 'Unifier 0.0.1'
    p = subprocess.run([sys.executable, str(release / 'tools/install_unifier.py'), '--upgrade'],
                       capture_output=True, text=True, check=True)
    receipt['user_launcher_install_output'] = p.stdout
    receipt['user_launcher_previous_target'] = previous_launcher
    version = subprocess.check_output([str(launcher), '--version'], text=True).strip()
    assert version == 'Unifier 0.0.1'
    receipt['user_launcher_version'] = version
    receipt['passed'] = True
except Exception:
    for path, (payload, mode) in files.items():
        atomic(path, payload, mode)
    if os.readlink(launcher) != previous_launcher:
        fd, name = tempfile.mkstemp(dir=launcher.parent); os.close(fd); os.unlink(name)
        os.symlink(previous_launcher, name); os.replace(name, launcher)
    raise
(OUT / 'deployment.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
print(json.dumps(receipt, ensure_ascii=False, indent=2))
