#!/usr/bin/env python3
"""Atomic source-only deployment, under both daily environment lease locks."""
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from zoneinfo import ZoneInfo
ROOT=Path(__file__).resolve().parents[2]
RUN=Path(__file__).resolve().parent
DELTA=['src/multilinux/projection.py','src/multilinux/view_backend.py','src/multilinux/fuse_view.py']

def files(root):
    return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob('*') if p.is_file() and not p.is_symlink()}

def replace(path,data):
    fd,name=tempfile.mkstemp(prefix='.projection-fd-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
            os.fchmod(stream.fileno(),stat.S_IMODE(path.stat().st_mode))
        os.replace(name,path)
    finally:Path(name).unlink(missing_ok=True)

def main():
    os.umask(0o077)
    assert not (RUN/'deployment.json').exists()
    prior=json.loads((ROOT/'evidence/whitelist-daily-deploy-2026-10-28/context.json').read_text())
    backup=Path('/home/omg/.local/state/unifier-deployments')/('projection-fd-'+datetime.datetime.now(ZoneInfo('Asia/Shanghai')).strftime('%Y%m%d-%H%M%S'))
    backup.mkdir(mode=0o700)
    result={'backup':str(backup),'apps':{},'source_sha256':{p:hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in DELTA}}
    locks=[]
    try:
        for app,old in prior['apps'].items():
            root=Path(old['root']);item={k:old[k] for k in ['root','environment','container_id','desktop','documents']}
            for suffix in ['.lock','.lifecycle.lock']:
                fd=os.open(root/'.lab/leases'/(item['environment']+suffix),os.O_CREAT|os.O_RDWR,0o600)
                locks.append(fd);fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            info=json.loads(subprocess.check_output(['podman','inspect',item['container_id']]))[0]
            assert info['State']['Status']=='exited'
            lease=root/'.lab/leases'/(item['environment']+'.json')
            assert not lease.exists() or not json.loads(lease.read_text()).get('leases')
            item['before_inspect']=info
            target=backup/app;target.mkdir()
            shutil.copytree(root,target/'deployment',symlinks=True)
            assert files(root)==files(target/'deployment')
            item['state_before']=files(root/'.lab/state')
            item['config_before']=files(root/'config')
            item['documents_before']=files(Path(item['documents']))
            item['desktop_sha256']=hashlib.sha256(Path(item['desktop']).read_bytes()).hexdigest()
            shutil.copy2(item['desktop'],target/Path(item['desktop']).name)
            shutil.copytree(item['documents'],target/'documents',symlinks=True)
            assert files(target/'documents')==item['documents_before']
            result['apps'][app]=item
        (backup/'before.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        for app,item in result['apps'].items():
            root=Path(item['root'])
            for p in DELTA:replace(root/p,(ROOT/p).read_bytes())
            manifest_path=root/'deployment-current.json';manifest=json.loads(manifest_path.read_text())
            manifest.setdefault('source_sha256',{}).update(result['source_sha256'])
            manifest['projection_fd_repair']={'source_sha256':result['source_sha256'],'backup':str(backup/app),
                                            'evidence':str(RUN/'report.md'),'deployed_at':datetime.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()}
            replace(manifest_path,(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n').encode())
            assert all(hashlib.sha256((root/p).read_bytes()).hexdigest()==h for p,h in result['source_sha256'].items())
            assert files(root/'.lab/state')==item['state_before']
            assert files(root/'config')==item['config_before']
            assert files(Path(item['documents']))==item['documents_before']
            assert hashlib.sha256(Path(item['desktop']).read_bytes()).hexdigest()==item['desktop_sha256']
            current=json.loads(subprocess.check_output(['podman','inspect',item['container_id']]))[0]
            assert current['State']==item['before_inspect']['State']
            check=subprocess.run(['python3',str(root/'tools/ml.py'),'view','--help'],capture_output=True,text=True)
            assert check.returncode==0,check.stderr
            item['source_deployed']=True
        # Private source/state recovery metadata remains outside the repository.
        (backup/'deployment-private.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
        public={'backup':str(backup),'source_sha256':result['source_sha256'],
                'apps':{app:{k:v for k,v in item.items() if k!='before_inspect'} for app,item in result['apps'].items()}}
        (RUN/'deployment.json').write_text(json.dumps(public,ensure_ascii=False,indent=2))
    finally:
        for fd in reversed(locks):os.close(fd)
    print(json.dumps({'deployed':list(result['apps']),'backup':str(backup)}))
if __name__=='__main__':main()
