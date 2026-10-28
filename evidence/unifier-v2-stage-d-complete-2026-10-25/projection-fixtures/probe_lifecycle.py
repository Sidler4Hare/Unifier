"""Real busy-view, worker/broker SIGKILL and storage-reference acceptance."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback
import tomllib

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(REPO / 'src'), str(REPO)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.views import ViewManager, is_mounted
from multilinux.on_demand import process_stamp


def storage():
    return json.loads(subprocess.check_output(['podman', 'unshare', 'podman', 'mount', '--format=json'], text=True))


def has_cid(cid):
    return cid in json.dumps(storage())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    assert not args.output.exists()
    f = json.loads((HERE / 'fixtures.json').read_text())
    p = Path(f['project'])
    service = EnvironmentService(p, PodmanRuntime(), tomllib.loads((p / 'config/targets.toml').read_text()),
                                 json.loads((p / 'config/images.lock.json').read_text()))
    views = ViewManager(p, service)
    env = 'view-debian13'
    cid = service.store.load(env)['runtime']['container_id']
    assert not views.registry.list() and not has_cid(cid)
    source = {str(p.relative_to(REPO)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in (REPO / 'src/multilinux').glob('*.py')}
    report = {'cases': {}, 'source_sha256': source, 'passed': False}
    for case in ('busy', 'worker-sigkill', 'broker-sigkill', 'preexisting-storage-reference'):
        row = {'passed': False}
        report['cases'][case] = row
        view, held = None, None
        try:
            if case == 'preexisting-storage-reference':
                subprocess.run(['podman', 'unshare', 'podman', 'mount', cid], check=True, capture_output=True)
                assert has_cid(cid)
            view = views.open(env)
            row['view'] = view
            mount = Path(view['mountpoint'])
            assert (mount / 'var/lib/unifier-view/payload.txt').read_text() == 'debian13\n'
            if case == 'busy':
                held = os.open(mount / 'var/lib/unifier-view/payload.txt', os.O_RDONLY)
                error = None
                try:
                    views.revoke(view['view_uuid'])
                except Exception as exc:
                    error = str(exc)
                row['busy_revoke_error'] = error
                row['worker_alive_while_busy'] = process_stamp(view['worker_pid']) == view['worker_stamp']
                row['broker_alive_while_busy'] = process_stamp(view['broker_pid']) == view['broker_stamp']
                row['fuse_mounted_while_busy'] = is_mounted(str(mount))
                row['storage_mounted_while_busy'] = has_cid(cid)
                assert error and row['worker_alive_while_busy'] and row['broker_alive_while_busy']
                assert row['fuse_mounted_while_busy'] and row['storage_mounted_while_busy']
                assert os.read(held, 128) == b'debian13\n'
                os.close(held)
                held = None
                views.revoke(view['view_uuid'])
            elif case in ('worker-sigkill', 'broker-sigkill'):
                field = 'worker' if case == 'worker-sigkill' else 'broker'
                assert process_stamp(view[field + '_pid']) == view[field + '_stamp']
                os.kill(view[field + '_pid'], signal.SIGKILL)
                deadline = time.monotonic() + 3
                while process_stamp(view[field + '_pid']) == view[field + '_stamp'] and time.monotonic() < deadline:
                    time.sleep(.02)
                row['recover_first'] = views.recover(view['view_uuid'])
                row['recover_second'] = views.recover()
                assert not views.registry.list() and not is_mounted(str(mount))
            else:
                views.revoke(view['view_uuid'])
                row['external_storage_reference_preserved'] = has_cid(cid)
                assert row['external_storage_reference_preserved']
                subprocess.run(['podman', 'unshare', 'podman', 'umount', cid], check=True, capture_output=True)
            row['no_storage_reference_leak'] = not has_cid(cid)
            assert row['no_storage_reference_leak']
            assert not views.registry.list() and not mount.exists()
            assert service.inspect(env)['runtime_state'] == 'stopped'
            row['passed'] = True
        except Exception as exc:
            row.update(error=str(exc), traceback=traceback.format_exc())
        finally:
            if held is not None:
                os.close(held)
            if view and views.registry.find(view['view_uuid']):
                try:
                    views.revoke(view['view_uuid'])
                except Exception as exc:
                    row['cleanup_error'] = str(exc)
            if has_cid(cid):
                # Containment of this test's own one outstanding reference;
                # never hide this as successful product crash recovery.
                row['manual_fixture_storage_cleanup_needed'] = True
                subprocess.run(['podman', 'unshare', 'podman', 'umount', cid], check=True, capture_output=True)
    report['source_unchanged'] = all(hashlib.sha256((REPO / p).read_bytes()).hexdigest() == h for p, h in source.items())
    report['passed'] = report['source_unchanged'] and all(x['passed'] for x in report['cases'].values())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'cases': {k: {'passed': v['passed'], 'error': v.get('error')}
                       for k, v in report['cases'].items()}}, ensure_ascii=False))
    if not report['passed']:
        sys.exit(1)


if __name__ == '__main__':
    main()
