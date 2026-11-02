"""Remove only exact stopped probe instances and their labelled named volumes."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import tomllib

ROOT = next(p for p in Path(__file__).resolve().parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.views import ViewRegistry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--fixtures', type=Path, required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    fixture = json.loads(args.fixtures.read_text())
    project = Path(fixture['project'])
    service = EnvironmentService(project, PodmanRuntime(),
              tomllib.loads((project / 'config/targets.toml').read_text()),
              json.loads((project / 'config/images.lock.json').read_text()))
    assert not ViewRegistry(project).list(), 'revoke probe views before cleanup'
    mounts = subprocess.check_output(['podman', 'unshare', 'podman', 'mount', '--format=json'], text=True)
    removed = []
    for target, item in fixture['targets'].items():
        record = service.store.load(item['environment']['id'])
        assert record['environment_uuid'] == item['environment']['environment_uuid']
        assert record['runtime']['container_id'] == item['environment']['runtime']['container_id']
        info = service.runtime.inspect(record['runtime']['container_name'])
        assert info.container_id == record['runtime']['container_id'] and not info.running
        assert info.container_id not in mounts
        volume = json.loads(subprocess.check_output(['podman', 'volume', 'inspect', item['volume']], text=True))[0]
        assert volume['Labels']['unifier.probe'] == item['volume'].rsplit('-', 1)[-1]
        service.remove(record['id'])
        subprocess.run(['podman', 'volume', 'rm', item['volume']], check=True, capture_output=True)
        removed.append({'target': target, 'container_id': info.container_id, 'volume': item['volume']})
    args.output.write_text(json.dumps({'passed': True, 'removed': removed}, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'removed_probe_instances': len(removed)}))


if __name__ == '__main__':
    main()
