"""Package current source without this host's deployments, state or images."""
from pathlib import Path
import hashlib
import io
import json
import tarfile
import tempfile

RUN = Path(__file__).resolve().parent
ROOT = RUN.parents[1]
NAME = 'unifier-ubuntu-server-20261028'


def main():
    output = ROOT / 'artifacts' / (NAME + '.tar.gz')
    output.parent.mkdir(exist_ok=True)
    assert not output.exists(), 'preserve earlier bundles; choose another name'
    contents = {}
    for directory in ['src', 'tools', 'config', 'fixtures', 'tests']:
        for source in (ROOT / directory).rglob('*'):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix != '.pyc':
                assert source.resolve().is_relative_to(ROOT.resolve()), 'bundle source link leaves the checkout'
                contents[str(source.relative_to(ROOT))] = source.read_bytes()
    contents['UBUNTU-SERVER.md'] = (ROOT / '设计/Ubuntu-Server白名单与容器验收-2026-10-28.md').read_bytes()
    contents['README.md'] = (
        '# Unifier headless host validation\n\n'
        'Current source snapshot for Linux x86_64, Python 3.11+, rootless Podman and cgroup v2.\n'
        'See UBUNTU-SERVER.md for the Ubuntu Server procedure.\n'
        'This archive contains no deployed applications, private HOME, runtime state or container images.\n'
        'Run compat matrix, lab doctor, lab pull, then tools/probe_headless.py with a fresh --output.\n'
        'A successful Fedora replay is not Ubuntu acceptance; run on the actual Ubuntu host.\n'
    ).encode()
    hashes = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    contents['BUNDLE-MANIFEST.json'] = (json.dumps(hashes, indent=2) + '\n').encode()
    with tarfile.open(output, 'w:gz') as archive:
        for name, data in sorted(contents.items()):
            member = tarfile.TarInfo(NAME + '/' + name)
            member.mode = 0o644
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n')
    stage = Path(tempfile.mkdtemp(prefix='ubuntu-bundle-', dir=ROOT / '.lab'))
    with tarfile.open(output, 'r:gz') as archive:
        archive.extractall(stage, filter='data')
    extracted = stage / NAME
    assert all(hashlib.sha256((extracted / name).read_bytes()).hexdigest() == value for name, value in hashes.items())
    assert not (extracted / '.lab').exists()
    result = {'bundle': str(output), 'sha256': digest, 'files': len(hashes),
              'size_bytes': output.stat().st_size, 'extracted': str(extracted),
              'all_members_verified': True, 'runtime_state_excluded': True}
    (RUN / 'server-bundle.json').write_text(json.dumps(result, indent=2) + '\n')
    print(json.dumps(result))


if __name__ == '__main__':
    main()
