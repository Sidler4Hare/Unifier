#!/usr/bin/env python3
"""Three guest distributions on this Fedora host, exact product broker path."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import traceback

from boundary_cases import CASES, watcher
sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'evidence/projection-objects-2026-10-26'))
import real_fuse_probe as probe

ROOT = probe.ROOT


def inventory():
    rows = json.loads(subprocess.check_output(['podman', 'ps', '-a', '--no-trunc', '--format', 'json']))
    return {row['Id']: dict(names=row['Names'], state=row['State']) for row in rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    project = out / 'project'
    project.mkdir()
    for name in ('src', 'tools', 'config'):
        shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    sys.path.insert(0, str(project / 'src'))
    from multilinux.runtime import PodmanRuntime
    from multilinux.service import EnvironmentService
    from multilinux.views import ViewManager, _control_request, is_mounted
    from multilinux.errors import MultilinuxError
    config = tomllib.loads((project / 'config/targets.toml').read_text())
    lock = json.loads((project / 'config/images.lock.json').read_text())
    service = EnvironmentService(project, PodmanRuntime(), config, lock)
    manager = ViewManager(project, service)
    before = inventory()
    result = dict(scope='Fedora local host; Debian 13/Fedora 44/Arch guest distributions',
                  before=before, targets={}, started_at=time.time(),
                  source_sha256={str(p.relative_to(project)):hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (project / 'src').rglob('*.py')})
    created = []
    def save():
        (out / 'result.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
    try:
        for target in ('debian13', 'fedora44', 'arch'):
            item = result['targets'][target] = dict(cases=[], passed=False)
            shared = out / ('shared-' + target)
            shared.mkdir()
            env_id = 'projection-' + target
            view = None
            mount_parent = Path(tempfile.mkdtemp(prefix='unifier-broker-probe-'))
            try:
                record = service.create(target, env_id, [str(shared)],
                                        desktop=True, user_namespace='keep-id')
                created.append((env_id, record['runtime']['container_id']))
                item['container_id'] = record['runtime']['container_id']
                item['environment_uuid'] = record['environment_uuid']
                # Keep a management rootfs out of the workspace's recursively
                # scanned evidence tree. The complete spec and journals stay
                # in the isolated project, and the mount is still real FUSE.
                view = manager.open(env_id, mountpoint=str(mount_parent/'view'))
                item['view_uuid'] = view['view_uuid']
                mount = Path(view['mountpoint'])
                assert is_mounted(str(mount))
                release = (mount / 'etc/os-release').read_text()
                expected = {'debian13':'debian','fedora44':'fedora','arch':'arch'}[target]
                assert ('ID=' + expected) in release or ('ID="' + expected + '"') in release
                item['os_release_sha256'] = hashlib.sha256(release.encode()).hexdigest()
                item['status'] = _control_request(view['control_socket'], {'op':'status'})
                for name, call in [('start', service.start), ('remove', service.remove)]:
                    try:
                        call(env_id)
                    except MultilinuxError as exc:
                        assert 'active read-only view' in str(exc), str(exc)
                        item[name + '_blocked'] = True
                    else:
                        raise AssertionError(name + ' unexpectedly succeeded with active view')
                # Adapt the independent kernel assertions to the product mount.
                class F:
                    back = shared
                    view = mount / shared.relative_to('/')
                    def status(self):
                        return _control_request(view['control_socket'], {'op':'status'})
                f = F()
                calls = [(f'replace-{a}-{b}-{primed}',lambda f,a=a,b=b,primed=primed:probe.replace_case(f,a,b,primed))
                         for a,b in ((32,128),(128,32),(32,32)) for primed in (False,True)]
                calls += [('unlink',probe.unlink_case),('hardlinks',probe.hardlinks_case),
                          ('inplace',probe.inplace_case),('mmap',probe.mmap_case),
                          ('mmap-unprimed',lambda f:probe.mmap_case(f,False)),
                          ('concurrent-replace',probe.concurrent_case),
                          ('directory-pagination',probe.directory_case),
                          ('directory-identity',probe.directory_identity_case),('dirent-identity',probe.dirent_identity_case),
                          ('readonly',probe.readonly_case),('permissions',probe.permissions_case)]
                calls += CASES
                for name, call in calls:
                    try:
                        data = call(f)
                        passed = True
                        case = dict(name=name,passed=passed,data=data)
                    except Exception as exc:
                        case = dict(name=name,passed=False,error=repr(exc),traceback=traceback.format_exc())
                    item['cases'].append(case)
                    save()
                item['known_open_gate_external_watcher'] = watcher(f)
                busy = os.open(f.view / 'link-a', os.O_RDONLY)
                try:
                    try:
                        manager.revoke(view['view_uuid'])
                    except MultilinuxError as exc:
                        item['busy_revoke'] = str(exc)
                        assert is_mounted(str(mount))
                        assert os.pread(busy,6,0) == b'abcdef'
                    else:
                        raise AssertionError('busy revoke unexpectedly succeeded')
                finally:
                    os.close(busy)
                item['passed'] = all(c['passed'] for c in item['cases'])
            except Exception:
                item['fatal'] = traceback.format_exc()
            finally:
                if view:
                    try:
                        drain_started = time.monotonic()
                        while True:
                            status = _control_request(view['control_socket'], {'op':'status'})
                            counts = status.get('counters', {})
                            if counts.get('handles') == 0:
                                break
                            if time.monotonic()-drain_started > 5:
                                raise AssertionError('FUSE RELEASE/RELEASEDIR did not drain: '+repr(counts))
                            time.sleep(.02)
                        item['quiescent_status'] = status
                        item['release_drain_seconds'] = time.monotonic()-drain_started
                        manager.revoke(view['view_uuid'])
                        item['normal_cleanup'] = not is_mounted(view['mountpoint'])
                        item['leases_after'] = len(manager.registry.list())
                        assert item['leases_after'] == 0
                    except Exception:
                        item['cleanup_error'] = traceback.format_exc()
                if service.store.exists(env_id):
                    try:
                        service.remove(env_id)
                        item['container_removed'] = True
                    except Exception:
                        item['remove_error'] = traceback.format_exc()
                item['passed'] = (item['passed'] and item.get('normal_cleanup', False)
                                  and item.get('container_removed', False)
                                  and not any(k in item for k in ('fatal','cleanup_error','remove_error')))
                if not (mount_parent/'view').exists():
                    mount_parent.rmdir()
                save()
            print(json.dumps(dict(target=target,passed=item['passed'])),flush=True)
            if item.get('cleanup_error') or item.get('remove_error'):
                break
    finally:
        after = inventory()
        result['after'] = after
        result['preservation'] = dict(original_container_identity_state_unchanged=before==after,
                                      new_containers_remaining=[cid for _,cid in created if cid in after])
        result['finished_at'] = time.time()
        result['passed'] = (len(result['targets'])==3 and all(t['passed'] for t in result['targets'].values())
                            and before==after)
        save()
    print(json.dumps(dict(passed=result['passed'],out=str(out))))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
