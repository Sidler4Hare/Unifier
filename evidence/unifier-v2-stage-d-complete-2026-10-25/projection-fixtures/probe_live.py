"""Host-only acceptance of real stopped-rootfs FUSE views on three distros.

Requires prepare_containers.py. Guest commands only prepared the fixture;
this acceptance opens ordinary host paths, never executes a guest file tool.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import traceback

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / 'src/multilinux').is_dir())
sys.path[:0] = [str(REPO / 'src'), str(REPO)]
from multilinux.errors import MultilinuxError
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.views import ViewManager, is_mounted
from multilinux.on_demand import process_stamp


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def must_fail(fn, allowed=None):
    try:
        fn()
    except (OSError, MultilinuxError) as exc:
        if allowed is not None:
            assert isinstance(exc, OSError) and exc.errno in allowed, str(exc)
        return {'refused': True, 'errno': getattr(exc, 'errno', None), 'error': str(exc)}
    raise AssertionError('an operation expected to be refused succeeded')


def cat(path):
    r = subprocess.run(['/usr/bin/cat', str(path)], capture_output=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return r.stdout


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--output', type=Path, required=True)
    ap.add_argument('--target', choices=('debian13', 'fedora44', 'arch'))
    args = ap.parse_args()
    if args.output.exists():
        ap.error('choose a new output path')
    fixture = json.loads((HERE / 'fixtures.json').read_text())
    project = Path(fixture['project'])
    service = EnvironmentService(project, PodmanRuntime(),
             tomllib.loads((project / 'config/targets.toml').read_text()),
             json.loads((project / 'config/images.lock.json').read_text()))
    views = ViewManager(project, service)
    sources = {str(p.relative_to(REPO)): sha(p) for p in (REPO / 'src/multilinux').glob('*.py')}
    report = {'targets': {}, 'source_sha256': sources, 'passed': False,
              'scope': 'native rootless Podman overlay; actual host FUSE readonly access'}
    selected = [args.target] if args.target else list(fixture['targets'])
    try:
        for target in selected:
            item = fixture['targets'][target]
            env = item['environment']['id']
            row = {'environment_id': env, 'passed': False}
            report['targets'][target] = row
            assert service.inspect(env)['runtime_state'] == 'stopped'
            start_info = json.loads(subprocess.check_output(
                ['podman', 'inspect', item['environment']['runtime']['container_id']], text=True))[0]
            view = views.open(env)
            row['view'] = view
            mount = Path(view['mountpoint'])
            try:
                assert is_mounted(str(mount))
                assert service.inspect(env)['runtime_state'] == 'stopped'
                row['top_level'] = sorted(os.listdir(mount))
                assert {'etc', 'usr', 'var'}.issubset(row['top_level'])
                os_release = cat(mount / 'etc/os-release')
                assert ('ID=' + {'debian13': 'debian', 'fedora44': 'fedora', 'arch': 'arch'}[target]).encode() in os_release
                private = mount / 'var/lib/unifier-view'
                assert cat(private / 'payload.txt') == (target + '\n').encode()
                assert cat(private / 'os-absolute') == os_release
                assert cat(private / 'a/b/os-upward') == os_release
                assert cat(private / 'relative') == (target + '\n').encode()
                assert cat(private / 'volume-absolute') == ('volume-' + target + '\n').encode()
                assert cat(mount / 'var/lib/unifier-volume/payload.txt') == ('volume-' + target + '\n').encode()
                assert cat(mount / 'var/lib/unifier-volume/nested/payload.txt') == ('nested-bind-' + target + '\n').encode()
                row['host_links'] = {'absolute': os.readlink(private / 'os-absolute'),
                                     'upward': os.readlink(private / 'a/b/os-upward'),
                                     'relative': os.readlink(private / 'relative')}
                assert all(p.startswith(str(mount) + '/') for p in row['host_links'].values())
                row['readlink_raw'] = views.readlink_raw(view['view_uuid'], '/var/lib/unifier-view/os-absolute')
                assert row['readlink_raw'].get('raw') == '/etc/os-release', row['readlink_raw']
                row['host_escape'] = must_fail(lambda: (private / 'host-escape').read_bytes(), {errno.ENOENT})
                row['runtime_unavailable'] = {p: must_fail(lambda p=p: os.listdir(mount / p))
                                               for p in ('proc', 'sys', 'dev', 'run')}
                row['uids'] = {str(n): {'uid': (private / f'uid-{n}.txt').stat().st_uid,
                                       'gid': (private / f'uid-{n}.txt').stat().st_gid}
                               for n in (0, 1, 1000, 1001)}
                assert all(v['uid'] == int(k) and v['gid'] == int(k) for k, v in row['uids'].items())
                file = private / 'payload.txt'
                row['write_refusals'] = {
                    'write': must_fail(lambda: file.write_bytes(b'wrong'), {errno.EROFS}),
                    'create': must_fail(lambda: (private / 'new.txt').write_bytes(b'wrong'), {errno.EROFS}),
                    'delete': must_fail(lambda: file.unlink(), {errno.EROFS}),
                    'rename': must_fail(lambda: file.rename(private / 'renamed.txt'), {errno.EROFS}),
                    'chmod': must_fail(lambda: file.chmod(0o777), {errno.EROFS}),
                    'mkdir': must_fail(lambda: (private / 'new-dir').mkdir(), {errno.EROFS}),
                    'symlink': must_fail(lambda: (private / 'new-link').symlink_to('/etc/passwd'), {errno.EROFS}),
                    'hardlink': must_fail(lambda: os.link(file, private / 'new-hardlink'), {errno.EROFS}),
                }
                row['start_refused'] = must_fail(lambda: service.start(env))
                row['remove_refused'] = must_fail(lambda: service.remove(env))
                row['second_view_refused'] = must_fail(lambda: views.open(env))
                assert service.inspect(env)['runtime_state'] == 'stopped'
                row['os_release_sha256'] = hashlib.sha256(os_release).hexdigest()
                # Missing/replaced bind roots must never reveal an underlying
                # merged directory or newly substituted host directory.
                bind = Path(item['bind'])
                parked = bind.with_name(bind.name + '.temporarily-unavailable')
                bind.rename(parked)
                try:
                    row['missing_bind'] = must_fail(lambda: cat(mount / 'var/lib/unifier-volume/nested/payload.txt'))
                except AssertionError:
                    # cat helper reports command failures as assertions; retain
                    # the actual native return code/error separately below.
                    rr = subprocess.run(['cat', str(mount / 'var/lib/unifier-volume/nested/payload.txt')], capture_output=True, text=True)
                    assert rr.returncode != 0
                    row['missing_bind'] = {'refused': True, 'exit': rr.returncode, 'stderr': rr.stderr}
                finally:
                    parked.rename(bind)
                row['passed'] = True
            except Exception:
                log_path = Path(view['log_path'])
                if log_path.exists():
                    row['worker_log'] = log_path.read_text(errors='replace')[-7000:]
                raise
            finally:
                views.revoke(view['view_uuid'])
                row['mount_revoked'] = not is_mounted(str(mount)) and not mount.exists()
                row['worker_stopped'] = process_stamp(view['worker_pid']) != view['worker_stamp']
                row['broker_stopped'] = process_stamp(view['broker_pid']) != view['broker_stamp']
            assert row['mount_revoked'] and row['worker_stopped'] and row['broker_stopped']
            assert not views.registry.find(env)
            end_info = json.loads(subprocess.check_output(
                ['podman', 'inspect', item['environment']['runtime']['container_id']], text=True))[0]
            row['never_started_by_view'] = start_info['State']['StartedAt'] == end_info['State']['StartedAt']
            assert row['never_started_by_view'] and service.inspect(env)['runtime_state'] == 'stopped'
        report['source_unchanged'] = all(sha(REPO / p) == h for p, h in sources.items())
        report['passed'] = report['source_unchanged'] and all(x['passed'] for x in report['targets'].values())
    except Exception as exc:
        report.update(error=str(exc), traceback=traceback.format_exc())
    finally:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'targets': list(report['targets']), 'error': report.get('error')}, ensure_ascii=False))
    if not report['passed']:
        sys.exit(1)


if __name__ == '__main__':
    main()
