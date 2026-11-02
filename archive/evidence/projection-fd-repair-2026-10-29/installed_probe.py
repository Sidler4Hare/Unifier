#!/usr/bin/env python3
"""Existing daily apps: installed broker/worker, no container start or package change."""
import argparse,hashlib,json,os,resource,shutil,subprocess,sys,tempfile,time,traceback
from pathlib import Path
RUN=Path(__file__).resolve().parent
p=argparse.ArgumentParser();p.add_argument('--app',choices=['freecad','wemeet'],required=True);args=p.parse_args()
deployment=json.loads((RUN/'deployment.json').read_text());spec=deployment['apps'][args.app]
root=Path(spec['root']);sys.path[:0]=[str(root/'src'),str(root)]
from multilinux.cli import default_service
from multilinux.views import ViewManager,_control_request,is_mounted
from multilinux.errors import MultilinuxError
service=default_service();assert service.root==root
manager=ViewManager(root,service)
r={'app':args.app,'deployed_root':str(root),'passed':False};view=None;temporary=None
out=RUN/('installed-'+args.app);out.mkdir(exist_ok=False)
mount_parent=Path(tempfile.mkdtemp(prefix='unifier-installed-fd-'))
try:
    assert not manager.list()
    temporary=Path(tempfile.mkdtemp(prefix='projection-fd-acceptance-',dir=spec['documents']))
    for i in range(3000):(temporary/f'file-{i:04d}').write_bytes(b'FD acceptance')
    view=manager.open(spec['environment'],mountpoint=str(mount_parent/'view'))
    status=_control_request(view['control_socket'],{'op':'status'})
    r['initial_status']=status
    old=resource.prlimit(status['worker_pid'],resource.RLIMIT_NOFILE)
    resource.prlimit(status['worker_pid'],resource.RLIMIT_NOFILE,(256,old[1]))
    projected=Path(view['mountpoint'])/temporary.relative_to('/')
    before=_control_request(view['control_socket'],{'op':'status'})
    fd=os.open(projected,os.O_RDONLY|os.O_DIRECTORY)
    try:
        assert len(os.listdir(fd))==3000
        during=_control_request(view['control_socket'],{'op':'status'})
        assert during['counters']['snapshot_identity_refs']==3000
        assert during['counters']['snapshot_anchor_fds']==0
        assert during['counters']['process_fds']<256-16
        r['held_directory_status']=during
    finally:os.close(fd)
    assert (projected/'file-2999').read_bytes()==b'FD acceptance'
    try:os.open(projected/'file-2999',os.O_WRONLY)
    except OSError as exc:assert exc.errno==30
    else:raise AssertionError('writable projection')
    try:service.start(spec['environment'])
    except MultilinuxError as exc:assert 'active read-only view' in str(exc)
    else:raise AssertionError('start was allowed while view active')
    deadline=time.monotonic()+5
    while _control_request(view['control_socket'],{'op':'status'})['counters']['handles']:
        assert time.monotonic()<deadline;time.sleep(.02)
    r['before_revoke']=_control_request(view['control_socket'],{'op':'status'})
    manager.revoke(view['view_uuid']);assert not is_mounted(view['mountpoint']);view=None
    assert not manager.list()
    r['normal_revoke']=True
    r['source_sha256']={path:hashlib.sha256((root/path).read_bytes()).hexdigest() for path in deployment['source_sha256']}
    assert r['source_sha256']==deployment['source_sha256']
    before_private=json.loads((Path(deployment['backup'])/'deployment-private.json').read_text())['apps'][args.app]
    after=json.loads(subprocess.check_output(['podman','inspect',spec['container_id']]))[0]
    assert after['State']==before_private['before_inspect']['State']
    r['container_state_and_times_preserved']=True
    r['passed']=True
except Exception:r['error']=traceback.format_exc()
finally:
    if view:
        try:manager.revoke(view['view_uuid'])
        except Exception:r['cleanup_error']=traceback.format_exc()
    if temporary and (r.get('normal_revoke') or not view):shutil.rmtree(temporary)
    if not (mount_parent/'view').exists():mount_parent.rmdir()
    (out/'result.json').write_text(json.dumps(r,ensure_ascii=False,indent=2))
print(json.dumps({'app':args.app,'passed':r['passed']}))
raise SystemExit(0 if r['passed'] else 1)
