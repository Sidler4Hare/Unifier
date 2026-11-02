#!/usr/bin/env python3
"""First remote round: current snapshot, offline regression, cached-image headless checks."""
import datetime, hashlib, json, os, platform, resource, shutil, subprocess, sys, time
from pathlib import Path
BASE=Path(__file__).resolve().parent
ROOT=BASE/'unifier-current'
OUT=BASE/('results-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
OUT.mkdir()
ENV=dict(os.environ,LC_ALL='C',LANG='C',PYTHONDONTWRITEBYTECODE='1',MULTILINUX_INTEGRATION='0',MULTILINUX_EVIDENCE='0',UNIFIER_HOME=str(OUT/'installation'),PYTHONPATH=str(ROOT/'src')+os.pathsep+str(ROOT))
report={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'hostname':platform.node(),'kernel':platform.release(),'python':sys.version,'uid':os.getuid(),'nofile':resource.getrlimit(resource.RLIMIT_NOFILE),'source_root':str(ROOT),'scope':'Per-host offline regression, policy preflight and cached-image headless lifecycle; no application/GUI/performance acceptance','commands':{}}
def save():
    (OUT/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
def cmd(name,args,timeout=240):
    start=time.monotonic()
    try:
        p=subprocess.run(args,cwd=ROOT,env=ENV,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=timeout)
        record={'argv':args,'returncode':p.returncode,'elapsed_seconds':round(time.monotonic()-start,3)}
        (OUT/(name+'.stdout')).write_text(p.stdout)
        (OUT/(name+'.stderr')).write_text(p.stderr)
    except (OSError,subprocess.TimeoutExpired) as exc:
        record={'argv':args,'returncode':None,'error':str(exc),'elapsed_seconds':round(time.monotonic()-start,3)}
    report['commands'][name]=record
    save()
    print(json.dumps({'host':report['hostname'],'step':name,**record}),flush=True)
    return record
manifest=json.loads((ROOT/'BUNDLE-MANIFEST.json').read_text())
for rel,item in manifest['source_files'].items():
    p=ROOT/rel
    if item['kind']=='symlink':
        assert p.is_symlink() and os.readlink(p)==item['target'],rel
    else:
        assert hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256'],rel
report['source_manifest_sha256']=hashlib.sha256((ROOT/'BUNDLE-MANIFEST.json').read_bytes()).hexdigest()
report['source_files_verified']=len(manifest['source_files'])
report['os_release']=Path('/etc/os-release').read_text()
cmd('identity',['id'])
cmd('virtualization',['systemd-detect-virt'])
cmd('mounts',['findmnt','--json','--target',str(BASE)])
cmd('user-session',['loginctl','show-user',str(os.getuid()),'-p','Linger','-p','RuntimePath','-p','State'])
cmd('offline',[sys.executable,'-c','from pathlib import Path; from tools.check_repo import check; check(Path.cwd(), quick=False)'],timeout=300)
cmd('compatibility',[sys.executable,'tools/unifier.py','compat','matrix'])
cmd('doctor',[sys.executable,'tools/lab.py','doctor','--output',str(OUT/'doctor.json')])
cmd('cli-none',[sys.executable,'tools/unifier.py','init','--none','--yes'])
cmd('cli-status',[sys.executable,'tools/unifier.py','status'])
if shutil.which('podman'):
    cmd('podman-info',['podman','info','--format','json'])
    cmd('containers-before',['podman','ps','-a','--format','json'])
    cmd('images',['podman','images','--format','json'])
    cached=[]
    for target,item in json.loads((ROOT/'config/images.lock.json').read_text())['targets'].items():
        result=cmd('image-'+target,['podman','image','exists',item['locked_reference']])
        if result['returncode']==0:
            cached.append(target)
    report['cached_targets']=cached
    if cached:
        args=[sys.executable,'tools/probe_headless.py','--output',str(OUT/'headless.json')]
        for target in cached:
            args.extend(['--target',target])
        cmd('headless',args,timeout=300)
    cmd('containers-after',['podman','ps','-a','--format','json'])
else:
    report['runtime_status']='BLOCKED_MISSING_PODMAN'
report['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat()
save()
print(json.dumps({'results':str(OUT),'runtime_status':report.get('runtime_status'),'cached_targets':report.get('cached_targets')}),flush=True)
