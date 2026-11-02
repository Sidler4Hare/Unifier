"""Make a private, content-verified recovery archive before runtime retirement."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import stat
import subprocess
import tarfile
import time

RUN = Path(__file__).resolve().parent
CONTEXT = json.loads((RUN / 'context.json').read_text())
ROOT = Path(CONTEXT['root'])
BACKUP = Path(CONTEXT['backup'])
APPS = Path.home() / '.local/share/multilinux/apps'


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def cmd(argv, timeout=1200):
    p = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f'{argv[:4]}: {p.returncode}: {p.stderr[-2000:]}')
    return p.stdout


def sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''):
            h.update(data)
    return h.hexdigest()


def main():
    assert not (BACKUP / 'backup-verified.json').exists()
    rows = json.loads(cmd(['podman', 'ps', '-a', '--format', 'json']))
    assert len(rows) == 27 and all(r['State'] != 'running' for r in rows)
    inspect = json.loads(cmd(['podman', 'inspect', *[r['Id'] for r in rows]]))
    assert all((i.get('Config', {}).get('Labels') or {}).get('multilinux.role') == 'environment' for i in inspect)
    assert not any(m['Type'] == 'volume' for i in inspect for m in i.get('Mounts', []))
    original_images = sorted(set(cmd(['podman', 'image', 'ls', '-a', '--no-trunc', '--quiet']).split()))
    if not (BACKUP / 'containers-before.json').exists():
        save(BACKUP / 'containers-before.json', inspect)
        save(BACKUP / 'images-before.json', json.loads(cmd(['podman', 'image', 'inspect', *original_images])))
    original_images = [i['Id'] for i in json.loads((BACKUP / 'images-before.json').read_text())]
    roots = [ROOT, APPS / 'freecad', APPS / 'wemeet',
             Path.home() / '.local/share/multilinux/documents/wemeet',
             ROOT.parent / 'Unifier-CAD',
             Path.home() / '.local/share/applications/wemeetapp.desktop',
             Path.home() / '.local/share/applications/unifier-freecad.desktop',
             Path.home() / '.local/share/icons/hicolor/scalable/apps/wemeetapp.svg',
             Path.home() / '.config/mimeapps.list']
    roots = [p for p in roots if p.exists()]
    archive = BACKUP / 'host-state.tar.zst'
    if archive.exists():
        files = json.loads((BACKUP / 'host-files-before.json').read_text())
        special = json.loads((BACKUP / 'special-nodes.json').read_text())
    else:
        files, special = {}, []
        for base in roots:
            for p in ([base] if base.is_file() else base.rglob('*')):
                if p.is_symlink():
                    continue
                mode = p.lstat().st_mode
                if stat.S_ISREG(mode):
                    files[str(p)] = sha(p)
                elif not stat.S_ISDIR(mode):
                    special.append({'path': str(p), 'mode': mode, 'kind': 'socket' if stat.S_ISSOCK(mode) else 'special'})
        save(BACKUP / 'host-files-before.json', files)
        save(BACKUP / 'special-nodes.json', special)
        (BACKUP / 'host-paths.nul').write_bytes(b'\0'.join(str(p).lstrip('/').encode() for p in roots) + b'\0')
        cmd(['tar', '--zstd', '--acls', '--xattrs', '--sparse', '-cf', archive,
             '-C', '/', '--null', '-T', BACKUP / 'host-paths.nul'])
    # Check every pre-archive regular file against the actual archive bytes.
    decoder = subprocess.Popen(['zstd', '-q', '-dc', str(archive)], stdout=subprocess.PIPE)
    seen = {}
    hardlinks = []
    with tarfile.open(fileobj=decoder.stdout, mode='r|') as stream:
        for member in stream:
            path = '/' + member.name.lstrip('./')
            if member.isfile():
                f = stream.extractfile(member)
                h = hashlib.sha256()
                for data in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(data)
                seen[path] = h.hexdigest()
            elif member.islnk():
                hardlinks.append((path, '/' + member.linkname.lstrip('./')))
    decoder.stdout.close()
    assert decoder.wait() == 0
    for path, target in hardlinks:
        seen[path] = seen[target]
    mismatches = [p for p, h in files.items() if seen.get(p) != h]
    assert not mismatches, mismatches[:10]
    print(json.dumps({'stage': 'host-backup-verified', 'files': len(files)}, ensure_ascii=False), flush=True)
    checkpoints = json.loads((BACKUP / 'checkpoints.json').read_text()) if (BACKUP / 'checkpoints.json').exists() else []
    for n, row in enumerate(rows, 1):
        if row['Names'] == ['ml-53c5c8a5-freecad-arch']:
            continue
        cid = row['Id']
        if any(x['container_id'] == cid for x in checkpoints):
            continue
        current = json.loads(cmd(['podman', 'inspect', cid]))[0]
        assert current['State']['Status'] != 'running'
        tag = f"localhost/unifier-archive/{CONTEXT['stamp']}:{cid[:12]}"
        image = cmd(['podman', 'commit', '--quiet', '--pause=false', '--format', 'oci', cid, tag]).strip()
        info = json.loads(cmd(['podman', 'image', 'inspect', image]))[0]
        checkpoints.append({'container_id': cid, 'name': row['Names'][0], 'image_id': image,
                            'tag': tag, 'digest': info['Digest'], 'size': info.get('Size')})
        save(BACKUP / 'checkpoints.json', checkpoints)
        print(json.dumps({'stage': 'checkpoint', 'number': len(checkpoints), 'name': row['Names'][0]}, ensure_ascii=False), flush=True)
    images = sorted({'sha256:' + s.removeprefix('sha256:') for s in original_images + [x['image_id'] for x in checkpoints]})
    target = BACKUP / 'runtime-images.docker.tar.zst'
    if not target.exists():
        with (BACKUP / 'podman-save.log').open('w') as log:
            saver = subprocess.Popen(['podman', 'save', '--quiet', '--multi-image-archive', *images], stdout=subprocess.PIPE, stderr=log)
            zipper = subprocess.Popen(['zstd', '-q', '-T2', '-3', '-o', str(target)], stdin=saver.stdout)
            saver.stdout.close()
            zip_code = zipper.wait()
            save_code = saver.wait()
        assert save_code == 0 and zip_code == 0
    print(json.dumps({'stage': 'image-archive-written', 'bytes': target.stat().st_size}), flush=True)
    # A Docker archive has uncompressed layers. Verify each against the config's
    # diff_ids, and each config against its content-addressed image ID.
    decoder = subprocess.Popen(['zstd', '-q', '-dc', str(target)], stdout=subprocess.PIPE)
    hashes, configs, manifests = {}, {}, None
    with tarfile.open(fileobj=decoder.stdout, mode='r|') as stream:
        for member in stream:
            if not member.isfile():
                continue
            f = stream.extractfile(member)
            h = hashlib.sha256()
            small = bytearray() if member.size < 8 * 1024 * 1024 and member.name.endswith('.json') else None
            for data in iter(lambda: f.read(1024 * 1024), b''):
                h.update(data)
                if small is not None:
                    small.extend(data)
            hashes[member.name] = h.hexdigest()
            if member.name == 'manifest.json':
                manifests = json.loads(small)
            elif small is not None:
                configs[member.name] = json.loads(small)
    decoder.stdout.close()
    assert decoder.wait() == 0 and manifests
    found = set()
    for item in manifests:
        name = item['Config']
        config = configs[name]
        image_id = 'sha256:' + hashes[name]
        found.add(image_id)
        layers = item['Layers']
        expected = config['rootfs']['diff_ids']
        assert len(layers) == len(expected)
        assert all('sha256:' + hashes[path] == digest for path, digest in zip(layers, expected))
    assert set(images) <= found, (set(images) - found)
    result = {'status': 'VERIFIED_BEFORE_ANY_REMOVAL',
              'recorded_at': datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
              'original_containers': len(rows), 'retirement_checkpoints': len(checkpoints),
              'archived_image_configs': len(found), 'regular_host_files_verified': len(files),
              'host_archive_sha256': sha(archive), 'runtime_archive_sha256': sha(target),
              'host_archive_bytes': archive.stat().st_size, 'runtime_archive_bytes': target.stat().st_size,
              'every_image_config_and_layer_verified': True,
              'special_nodes': special,
              'scope': 'Stopped filesystem and host data recovery; runtime sockets/processes are recreated. Restored containers receive new IDs and must be rebound explicitly.'}
    save(BACKUP / 'backup-verified.json', result)
    save(RUN / 'backup-verification.json', {k: v for k, v in result.items() if k != 'special_nodes'} | {'private_backup': str(BACKUP)})
    print(json.dumps({'stage': 'backup-verified', 'images': len(found), 'checkpoints': len(checkpoints), 'archive_bytes': target.stat().st_size}), flush=True)


if __name__ == '__main__':
    os.umask(0o077)
    main()
