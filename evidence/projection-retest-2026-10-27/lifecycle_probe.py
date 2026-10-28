#!/usr/bin/env python3
"""Five default-mount lifecycle waves in one new stopped Debian fixture."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import traceback

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(HERE/'snapshot/src'))
from multilinux.service import EnvironmentService
from multilinux.runtime import PodmanRuntime
from multilinux.views import ViewManager,_control_request
from multilinux.errors import MultilinuxError


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=HERE/'default-lifecycle')
    out=parser.parse_args().out.resolve()
    out.mkdir(exist_ok=False)
    project=out/'project'
    project.mkdir()
    shared=out/'shared'
    shared.mkdir()
    (shared/'holder').write_bytes(b'fixture handle')
    service=EnvironmentService(project,PodmanRuntime(),
        tomllib.loads((HERE/'snapshot/config/targets.toml').read_text()),
        json.loads((HERE/'snapshot/config/images.lock.json').read_text()))
    manager=ViewManager(project,service)
    result=dict(waves=[],scope='default workspace mountpoint; one stopped Debian fixture reused')
    view=None
    try:
        record=service.create('debian13','retest-debian',[str(shared)],desktop=True,user_namespace='keep-id')
        result['container_id']=record['runtime']['container_id']
        for wave in range(5):
            view=manager.open('retest-debian')
            row=dict(wave=wave,view_uuid=view['view_uuid'],mountpoint=view['mountpoint'],
                     retries=[],first_post_close_success=False)
            result['waves'].append(row)
            projected=Path(view['mountpoint'])/shared.relative_to('/')/'holder'
            fd=os.open(projected,os.O_RDONLY)
            try:
                try:
                    manager.revoke(view['view_uuid'])
                except MultilinuxError as exc:
                    row['busy_while_held']=str(exc)
                    assert os.pread(fd,100,0)==b'fixture handle'
                else:
                    raise AssertionError('held FD did not block normal unmount')
            finally:
                os.close(fd)
            start=time.monotonic()
            try:
                manager.revoke(view['view_uuid'])
                row['first_post_close_success']=True
            except MultilinuxError as exc:
                row['first_post_close_error']=str(exc)
                row['status_after_error']=_control_request(view['control_socket'],{'op':'status'})
                users=subprocess.run(['fuser','-vm',view['mountpoint']],capture_output=True,text=True)
                row['fuser']=dict(stdout=users.stdout,stderr=users.stderr)
                while time.monotonic()-start<5:
                    time.sleep(.05)
                    try:
                        manager.revoke(view['view_uuid'])
                        row['retries'].append({'normal_revoke_success':True,'elapsed_seconds':time.monotonic()-start})
                        break
                    except MultilinuxError as retry:
                        row['retries'].append({'normal_revoke_success':False,'error':str(retry)})
                else:
                    raise RuntimeError('normal revoke remained busy after bounded retry')
            row['unmounted']=True
            row['elapsed_after_close_seconds']=time.monotonic()-start
            view=None
            assert not manager.registry.list()
            (out/'progress.json').write_text(json.dumps(result,indent=2))
            print(json.dumps({'wave':wave,'first_post_close_success':row['first_post_close_success']}),flush=True)
    except Exception:
        result['error']=traceback.format_exc()
    finally:
        if view:
            try:
                manager.revoke(view['view_uuid'])
            except Exception:
                result['cleanup_error']=traceback.format_exc()
        if service.store.exists('retest-debian'):
            try:
                service.remove('retest-debian')
                result['container_removed']=True
            except Exception:
                result['container_remove_error']=traceback.format_exc()
        result['remaining_views']=manager.registry.list()
        result['all_first_post_close_success']=(len(result['waves'])==5 and
                all(x['first_post_close_success'] for x in result['waves']))
        result['execution_completed']=(len(result['waves'])==5 and not result.get('error') and
                not result.get('cleanup_error') and not result.get('container_remove_error'))
        (out/'result.json').write_text(json.dumps(result,indent=2))
    return 0 if result['execution_completed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
