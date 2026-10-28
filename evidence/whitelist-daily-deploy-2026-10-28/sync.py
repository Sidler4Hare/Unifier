"""Deploy only the whitelist delta, preserving installed identities and images."""
from pathlib import Path
import datetime
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import tomllib
from zoneinfo import ZoneInfo

from backup import files, save

RUN = Path(__file__).resolve().parent
CONTEXT = json.loads((RUN / 'context.json').read_text())
ROOT = Path(CONTEXT['root'])
DELTA = ['src/multilinux/compatibility.py', 'src/multilinux/service.py',
         'src/multilinux/cli.py', 'src/multilinux/project_sessions.py', 'tools/lab.py']


def replace(path, data):
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
    descriptor, temporary = tempfile.mkstemp(prefix='.whitelist-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
            os.fchmod(stream.fileno(), mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    os.umask(0o077)
    assert not (RUN / 'deployment.json').exists()
    assert json.loads((Path(CONTEXT['backup']) / 'backup-verified.json').read_text())['verified']
    results = {}
    locks = []
    try:
        for item in CONTEXT['apps'].values():
            root = Path(item['root'])
            for suffix in ['.lock', '.lifecycle.lock']:
                lock = root / '.lab/leases' / (item['environment'] + suffix)
                descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locks.append(descriptor)
            current = json.loads(subprocess.check_output(['podman', 'inspect', item['container_id']], text=True))[0]
            assert current['State']['Status'] == 'exited'
            lease = root / '.lab/leases' / (item['environment'] + '.json')
            assert not lease.exists() or not json.loads(lease.read_text()).get('leases')
            assert files(root / '.lab/state') == item['state_sha256']
        for app, item in CONTEXT['apps'].items():
            root = Path(item['root'])
            before_config = tomllib.loads((root / 'config/targets.toml').read_text())
            lock_sha = hashlib.sha256((root / 'config/images.lock.json').read_bytes()).hexdigest()
            hashes = {}
            for relative in DELTA:
                content = (ROOT / relative).read_bytes()
                replace(root / relative, content)
                hashes[relative] = hashlib.sha256(content).hexdigest()
            config_path = root / 'config/targets.toml'
            assert 'compatibility' not in before_config
            replace(config_path, config_path.read_bytes() + b'\n[compatibility]\nmode = "whitelist"\n')
            after_config = tomllib.loads(config_path.read_text())
            assert after_config.pop('compatibility') == {'mode': 'whitelist'}
            assert after_config == before_config
            assert hashlib.sha256((root / 'config/images.lock.json').read_bytes()).hexdigest() == lock_sha
            check = subprocess.run(['/usr/bin/python3', str(root / 'tools/ml.py'), 'compat', 'matrix'],
                                   capture_output=True, text=True, timeout=30)
            assert check.returncode == 0, check.stderr
            plans = json.loads(check.stdout)
            assert len(plans) == 3 and all(p['allowed'] and p['host']['observation'] == 'live-host' for p in plans)
            assert files(root / '.lab/state') == item['state_sha256']
            assert files(Path(item['documents'])) == item['documents_sha256']
            assert hashlib.sha256(Path(item['desktop']).read_bytes()).hexdigest() == item['desktop_sha256']
            manifest_path = root / 'deployment-current.json'
            manifest = json.loads(manifest_path.read_text())
            manifest.setdefault('source_sha256', {}).update(hashes)
            manifest['compatibility_deployment'] = {
                'mode': 'whitelist', 'deployed_at': datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                'source': str(ROOT), 'source_sha256': hashes, 'backup': item['backup'],
                'config_sha256': hashlib.sha256(config_path.read_bytes()).hexdigest(),
                'image_lock_sha256': lock_sha, 'evidence': str(RUN / 'deployment.json'),
            }
            replace(manifest_path, (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
            assert all(hashlib.sha256((root / p).read_bytes()).hexdigest() == h
                       for p, h in manifest['source_sha256'].items())
            results[app] = {'source_sha256': hashes, 'config_mode': 'whitelist',
                            'image_lock_preserved': True, 'state_preserved': True,
                            'desktop_preserved': True, 'documents_preserved': True,
                            'container_id': item['container_id'], 'matrix': plans}
    finally:
        for descriptor in reversed(locks):
            os.close(descriptor)
    save(RUN / 'deployment.json', {'status': 'SYNCED_PENDING_MENU_ACCEPTANCE', 'apps': results})
    print(json.dumps({'synced': list(results), 'preserved': 'state, images, menus, documents and private HOME'}))


if __name__ == '__main__':
    main()
