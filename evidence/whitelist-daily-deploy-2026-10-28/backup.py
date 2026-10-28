"""Back up installed code/state, documents and private HOME before deployment."""
from pathlib import Path
import datetime
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import tarfile
from zoneinfo import ZoneInfo

RUN = Path(__file__).resolve().parent
ROOT = RUN.parents[1]
APPS = Path.home() / '.local/share/multilinux/apps'


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def files(root):
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}


def archive_container(cid, path, output):
    with output.open('wb') as raw, gzip.GzipFile(fileobj=raw, mode='wb') as zipped:
        process = subprocess.Popen(['podman', 'cp', cid + ':' + path, '-'], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        shutil.copyfileobj(process.stdout, zipped)
        process.stdout.close()
        error = process.stderr.read().decode(errors='replace')
        assert process.wait() == 0, error
    entries = {}
    with tarfile.open(output, 'r:gz') as archive:
        for item in archive:
            entry = {'size': item.size, 'mode': item.mode, 'uid': item.uid, 'gid': item.gid,
                     'type': item.type.decode(), 'link': item.linkname}
            if item.isfile():
                with archive.extractfile(item) as stream:
                    entry['sha256'] = hashlib.file_digest(stream, 'sha256').hexdigest()
            entries[item.name] = entry
    save(output.with_suffix('.manifest.json'), entries)
    return {'archive': str(output), 'sha256': hashlib.sha256(output.read_bytes()).hexdigest(),
            'entries': len(entries), 'regular_files': sum('sha256' in v for v in entries.values())}


def main():
    os.umask(0o077)
    assert not (RUN / 'context.json').exists(), 'use a fresh run directory'
    stamp = datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S')
    backup = Path.home() / '.local/state/unifier-deployments' / ('whitelist-' + stamp)
    backup.mkdir(parents=True, mode=0o700)
    context = {'root': str(ROOT), 'run': str(RUN), 'backup': str(backup), 'stamp': stamp, 'apps': {}}
    for app, env_id, desktop, docs in (
        ('freecad', 'freecad-arch', 'unifier-freecad', Path.home() / '文档/Unifier-CAD'),
        ('wemeet', 'meeting', 'wemeetapp', Path.home() / '.local/share/multilinux/documents/wemeet'),
    ):
        root = APPS / app
        record = json.loads((root / '.lab/state/environments' / (env_id + '.json')).read_text())
        cid = record['runtime']['container_id']
        info = json.loads(subprocess.check_output(['podman', 'inspect', cid], text=True))[0]
        assert info['State']['Status'] == 'exited'
        assert info['Config']['Labels']['multilinux.project'] == str(root)
        lease = root / '.lab/leases' / (env_id + '.json')
        assert not lease.exists() or not json.loads(lease.read_text()).get('leases')
        target = backup / app
        target.mkdir(mode=0o700)
        shutil.copytree(root, target / 'deployment', symlinks=True)
        assert files(root) == files(target / 'deployment')
        shutil.copytree(docs, target / 'documents', symlinks=True)
        assert files(docs) == files(target / 'documents')
        menu = Path.home() / '.local/share/applications' / (desktop + '.desktop')
        shutil.copy2(menu, target / menu.name)
        save(target / 'container-inspect.json', info)
        home = archive_container(cid, '/home/multilinux-1000', target / 'private-home.tar.gz')
        database = '/var/lib/pacman/local' if app == 'freecad' else '/var/lib/dpkg'
        packages = archive_container(cid, database, target / 'package-state.tar.gz')
        context['apps'][app] = {
            'root': str(root), 'environment': env_id, 'container_id': cid,
            'desktop': str(menu), 'documents': str(docs), 'backup': str(target),
            'state_sha256': files(root / '.lab/state'), 'documents_sha256': files(docs),
            'deployment_sha256': files(root), 'desktop_sha256': hashlib.sha256(menu.read_bytes()).hexdigest(),
            'private_home': home, 'package_state': packages, 'package_database': database,
        }
    save(backup / 'context.json', context)
    save(RUN / 'context.json', context)
    save(backup / 'backup-verified.json', {'verified': True, 'apps': {
        app: {'home_entries': info['private_home']['entries'], 'package_entries': info['package_state']['entries'],
              'deployment_files': len(info['deployment_sha256']), 'document_files': len(info['documents_sha256'])}
        for app, info in context['apps'].items()}})
    print(json.dumps({'backup_verified': True, 'backup': str(backup)}))


if __name__ == '__main__':
    main()
