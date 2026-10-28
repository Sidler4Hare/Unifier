"""Real named-volume disappearance/replacement without fake empty fallback."""
import argparse
import errno
import json
import os
from pathlib import Path
import subprocess
import sys
import tomllib
import traceback

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(REPO / 'src'), str(REPO)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.views import ViewManager


def mounts():
    return json.loads(subprocess.check_output(['podman', 'unshare', 'podman', 'mount', '--format=json'], text=True))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    fixture = json.loads((HERE / 'fixtures.json').read_text())
    p = Path(fixture['project'])
    service = EnvironmentService(p, PodmanRuntime(), tomllib.loads((p / 'config/targets.toml').read_text()),
                                 json.loads((p / 'config/images.lock.json').read_text()))
    manager = ViewManager(p, service)
    env = 'view-debian13'
    source = Path(next(m['Source'] for m in fixture['targets']['debian13']['inspect']['Mounts'] if m['Type'] == 'volume'))
    parked = source.with_name('_data.unifier-c-parked')
    assert not parked.exists() and not manager.registry.list()
    baseline = mounts()
    report = {'passed': False, 'volume_source': str(source), 'before': baseline}
    view = None
    try:
        source.rename(parked)
        try:
            try:
                view = manager.open(env)
            except Exception as exc:
                report['missing_before_open_error'] = str(exc)
            assert view is None, 'missing volume was silently replaced with an empty projection'
        finally:
            parked.rename(source)
        assert not manager.registry.list() and mounts() == baseline
        view = manager.open(env)
        target = Path(view['mountpoint']) / 'var/lib/unifier-volume/payload.txt'
        assert target.read_bytes() == b'volume-debian13\n'
        source.rename(parked)
        try:
            for case in ('missing', 'empty-replacement'):
                if case == 'empty-replacement':
                    source.mkdir(mode=0o700)
                try:
                    target.read_bytes()
                except OSError as exc:
                    report[case] = {'errno': exc.errno, 'error': str(exc)}
                    assert exc.errno == errno.EIO
                else:
                    raise AssertionError('missing/replaced backend remained readable')
        finally:
            if source.exists():
                source.rmdir()
            parked.rename(source)
        assert target.read_bytes() == b'volume-debian13\n'
        manager.revoke(view['view_uuid'])
        view = None
        assert not manager.registry.list() and mounts() == baseline
        report.update(passed=True, source_restored=True, no_storage_leak=True,
                      environment_stopped=service.inspect(env)['runtime_state'] == 'stopped')
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        if parked.exists():
            if source.exists():
                source.rmdir()
            parked.rename(source)
        if view is not None and manager.registry.find(view['view_uuid']):
            manager.revoke(view['view_uuid'])
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'error': report.get('error')}))
    if not report['passed']:
        sys.exit(1)


if __name__ == '__main__':
    main()
