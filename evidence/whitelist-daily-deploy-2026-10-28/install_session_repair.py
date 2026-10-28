"""Install the tested legacy Xauthority mount repair into both existing apps."""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import subprocess

from backup import save
from sync import replace

RUN = Path(__file__).resolve().parent
CTX = json.loads((RUN / 'context.json').read_text())


def main():
    os.umask(0o077)
    assert not (RUN / 'session-mount-repair.json').exists()
    results = {}
    for app, item in CTX['apps'].items():
        root = Path(item['root'])
        locks = []
        try:
            for suffix in ['.lock', '.lifecycle.lock']:
                fd = os.open(root / '.lab/leases' / (item['environment'] + suffix), os.O_CREAT | os.O_RDWR, 0o600)
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locks.append(fd)
            info = json.loads(subprocess.check_output(['podman', 'inspect', item['container_id']], text=True))[0]
            assert info['State']['Status'] == 'exited'
            hashes = {}
            for relative in ['src/multilinux/desktop.py', 'src/multilinux/service.py']:
                content = (Path(CTX['root']) / relative).read_bytes()
                replace(root / relative, content)
                hashes[relative] = hashlib.sha256(content).hexdigest()
            path = root / 'deployment-current.json'
            manifest = json.loads(path.read_text())
            manifest['source_sha256'].update(hashes)
            manifest['compatibility_deployment']['source_sha256'].update(hashes)
            manifest['session_mount_repair'] = {'source_sha256': hashes, 'backup': item['backup'],
                                               'evidence': str(RUN / 'session-mount-repair.json')}
            replace(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + '\n').encode())
            results[app] = {'source_sha256': hashes, 'container_id': item['container_id']}
        finally:
            for fd in reversed(locks):
                os.close(fd)
    save(RUN / 'session-mount-repair.json', {'status': 'DEPLOYED_PENDING_MENU_RETEST', 'apps': results})


if __name__ == '__main__':
    main()
