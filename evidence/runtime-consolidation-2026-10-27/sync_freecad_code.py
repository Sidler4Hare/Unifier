"""Synchronize the current accepted code without recreating daily FreeCAD."""
from pathlib import Path
import fcntl
import hashlib
import json
import os
import shutil
import subprocess

RUN = Path(__file__).resolve().parent
CTX = json.loads((RUN/'context.json').read_text())
ROOT, BACKUP = Path(CTX['root']), Path(CTX['backup'])
APP = Path.home()/'.local/share/multilinux/apps/freecad'


def sha(p):
    return hashlib.sha256(p.read_bytes()).hexdigest()


def files(base):
    return {str(p.relative_to(base)):sha(p) for p in base.rglob('*') if p.is_file() and not p.is_symlink()}


def main():
    assert (BACKUP/'backup-verified.json').is_file()
    assert not (RUN/'freecad-code-sync.json').exists()
    baseline = files(APP/'.lab/state')
    config = files(APP/'config')
    desktop = Path.home()/'.local/share/applications/unifier-freecad.desktop'
    desktop_sha = sha(desktop)
    source = {str(p.relative_to(ROOT)):sha(p) for d in ['src','tools'] for p in (ROOT/d).rglob('*') if p.is_file() and '__pycache__' not in p.parts and p.suffix!='.pyc'}
    changed = [p for p,h in source.items() if not (APP/p).exists() or sha(APP/p)!=h]
    record=json.loads((APP/'.lab/state/environments/freecad-arch.json').read_text())
    cid=record['runtime']['container_id']
    info=json.loads(subprocess.run(['podman','inspect',cid],capture_output=True,text=True,check=True).stdout)[0]
    assert info['State']['Status'] != 'running'
    lease=APP/'.lab/leases/freecad-arch.json'
    assert not lease.exists() or not json.loads(lease.read_text()).get('leases')
    locks=[]
    try:
        for name in ['freecad-arch.lock','freecad-arch.lifecycle.lock']:
            p=APP/'.lab/leases'/name;p.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(p,os.O_CREAT|os.O_RDWR,0o600)
            fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);locks.append(fd)
        retired=BACKUP/'retired-freecad-code';retired.mkdir(mode=0o700)
        for d in ['src','tools']:
            shutil.move(APP/d,retired/d)
            shutil.copytree(ROOT/d,APP/d,ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
        p=subprocess.run(['/usr/bin/python3',str(APP/'tools/ml.py'),'--help'],capture_output=True,text=True)
        assert p.returncode==0,p.stderr
        assert all(sha(APP/p)==h for p,h in source.items())
        assert files(APP/'.lab/state')==baseline and files(APP/'config')==config
        assert sha(desktop)==desktop_sha
    finally:
        for fd in reversed(locks):os.close(fd)
    result={'status':'CODE_SYNCED_PENDING_DEPLOYMENT_SMOKE','container_id_preserved':cid,
            'changed_files':changed,'source_sha256':source,'state_preserved':True,'config_preserved':True,'desktop_preserved':True}
    (RUN/'freecad-code-sync.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    (APP/'deployment-current.json').write_text(json.dumps({'source_sha256':source,'application':'freecad','accepted_source':str(ROOT),
        'deployment_evidence':str(RUN/'freecad-code-sync.json')},ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({'source_synced':True,'changed_files':changed,'container_retained':True},ensure_ascii=False))


if __name__=='__main__':
    os.umask(0o077)
    main()
