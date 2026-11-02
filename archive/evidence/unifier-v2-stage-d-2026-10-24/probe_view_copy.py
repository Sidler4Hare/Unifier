"""Copy real stopped private files via the registered C-view D source path."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import tomllib
import traceback

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
C_EVIDENCE = ROOT / 'evidence/unifier-v2-stage-c-2026-10-24'
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.control import Control
from multilinux.views import ViewManager
from multilinux.on_demand import process_stamp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--fixtures', type=Path, default=C_EVIDENCE / 'fixtures.json')
    args = ap.parse_args()
    assert not args.output.exists()
    fixture = json.loads(args.fixtures.read_text())
    project = Path(fixture['project'])
    service = EnvironmentService(project, PodmanRuntime(),
                    tomllib.loads((project / 'config/targets.toml').read_text()),
                    json.loads((project / 'config/images.lock.json').read_text()))
    views, control = ViewManager(project, service), Control(project)
    report = {'passed': False, 'targets': {}, 'source_sha256': {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in (ROOT / 'src/multilinux').glob('*.py')}}
    for target, fixture_item in fixture['targets'].items():
        env = fixture_item['environment']['id']
        row = {'passed': False}
        report['targets'][target] = row
        view, roots = None, []
        try:
            assert service.inspect(env)['runtime_state'] == 'stopped'
            view = views.open(env)
            row['view'] = view
            with tempfile.TemporaryDirectory(prefix='unifier-d-view-', dir='/dev/shm') as tmp:
                destination = Path(tmp)
                sr = control.add_root(view['mountpoint'], source_view_uuid=view['view_uuid'])
                roots.append(sr['root_uuid'])
                tr = control.add_root(str(destination))
                roots.append(tr['root_uuid'])
                def busy(point):
                    if point != 'STAGING_READY':
                        return
                    try:
                        views.revoke(view['view_uuid'])
                    except Exception as exc:
                        row['busy_revoke_error'] = str(exc)
                    else:
                        raise AssertionError('view revoked while D held source/root FDs')
                    assert process_stamp(view['worker_pid']) == view['worker_stamp']
                    assert process_stamp(view['broker_pid']) == view['broker_stamp']
                    assert (Path(view['mountpoint']) / 'var/lib/unifier-view/payload.txt').read_text() == target + '\n'
                operation = control.copy_file(sr['root_uuid'], 'var/lib/unifier-view/payload.txt',
                                  tr['root_uuid'], '宿主副本.txt', fault_hook=busy)
                row['operation'] = operation
                assert operation['state'] == 'DONE' and operation['source']['source_view']['environment_binding'] == view['environment_binding']
                assert (destination / '宿主副本.txt').read_text() == target + '\n'
                assert (destination / '宿主副本.txt').stat().st_nlink == 1
                row['target_uid'] = (destination / '宿主副本.txt').stat().st_uid
                control.resolve_file(tr['root_uuid'], '宿主副本.txt')
                assert service.inspect(env)['runtime_state'] == 'stopped'
                views.revoke(view['view_uuid'])
                mountpoint = Path(view['mountpoint'])
                mountpoint.mkdir(parents=True)  # simulate a stale empty host path, never actual data
                before = len(control.list_file_operations())
                try:
                    try:
                        control.copy_file(sr['root_uuid'], 'var/lib/unifier-view/payload.txt', tr['root_uuid'], 'must-not-copy')
                    except Exception as exc:
                        row['revoked_source_refusal'] = str(exc)
                    else:
                        raise AssertionError('revoked source view was accepted as a host directory')
                    assert len(control.list_file_operations()) == before
                    assert not (destination / 'must-not-copy').exists()
                finally:
                    mountpoint.rmdir()
                    mountpoint.parent.rmdir()
                row.update(passed=True, environment_stayed_stopped=True, copied_bytes_match=True,
                           source_fds_protect_busy_view=True, revoked_view_not_empty_fallback=True)
                view = None
        except Exception as exc:
            row.update(error=str(exc), traceback=traceback.format_exc())
        finally:
            for root in roots:
                control.remove_root(root)
            if view is not None and views.registry.find(view['view_uuid']):
                views.revoke(view['view_uuid'])
        print(json.dumps({'target': target, 'passed': row['passed'], 'error': row.get('error')}), flush=True)
    report['source_unchanged'] = all(hashlib.sha256((ROOT / p).read_bytes()).hexdigest() == v
                                      for p, v in report['source_sha256'].items())
    report['passed'] = report['source_unchanged'] and all(x['passed'] for x in report['targets'].values())
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    if not report['passed']:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
