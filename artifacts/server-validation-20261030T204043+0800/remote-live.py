#!/usr/bin/env python3
import datetime,hashlib,json,os,subprocess,sys,time
from pathlib import Path
BASE=Path(__file__).resolve().parent
ROOT=BASE/'unifier-current'
OUT=BASE/('live-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
OUT.mkdir()
ENV=dict(os.environ,LC_ALL='C',LANG='C',PYTHONDONTWRITEBYTECODE='1',UNIFIER_HOME=str(OUT/'cli-installation'),PYTHONPATH=str(ROOT/'src')+os.pathsep+str(ROOT))
report={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'scope':'Cached-image CLI init, stopped guest listing, controlled shell launch and private files; no third-party install or GUI acceptance','commands':{},'targets':{}}
def save(): (OUT/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
def cmd(name,args,expected=0,timeout=180):
    start=time.monotonic()
    try:
        p=subprocess.run(args,cwd=ROOT,env=ENV,capture_output=True,text=True,timeout=timeout)
        row={'argv':args,'returncode':p.returncode,'expected':expected,'matched_expected':p.returncode==expected,'elapsed_seconds':round(time.monotonic()-start,3)}
        (OUT/(name+'.stdout')).write_text(p.stdout);(OUT/(name+'.stderr')).write_text(p.stderr)
    except (OSError,subprocess.TimeoutExpired) as exc:
        row={'argv':args,'returncode':None,'expected':expected,'matched_expected':False,'error':str(exc)}
    report['commands'][name]=row;save()
    print(json.dumps({'step':name,**row}),flush=True)
    return row
def cli(name,*args,expected=0): return cmd(name,[sys.executable,'tools/unifier.py',*args],expected)
def inventory():
    p=subprocess.run(['podman','ps','-aq'],capture_output=True,text=True,check=True)
    ids=p.stdout.split()
    if not ids:return []
    data=json.loads(subprocess.check_output(['podman','inspect',*ids],text=True))
    return sorted([{'id':x['Id'],'name':x['Name'],'status':x['State']['Status'],'started_at':x['State']['StartedAt'],'finished_at':x['State']['FinishedAt']} for x in data],key=lambda x:x['id'])
report['before']=inventory();save()
cmd('doctor-module',[sys.executable,'-m','tools.lab','doctor','--output',str(OUT/'doctor-module.json')])
lock=json.loads((ROOT/'config/images.lock.json').read_text())['targets']
cached=[name for name,item in lock.items() if subprocess.run(['podman','image','exists',item['locked_reference']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0]
report['cached_targets']=cached;save()
if os.uname().nodename=='b0512':
    args=[sys.executable,'tools/probe_headless.py','--output',str(OUT/'headless.json')]
    for target in cached:args.extend(['--target',target])
    if cached:cmd('headless',args,timeout=300)
args=['init','--network','none','--yes']
for target in cached:args.extend(['--target',target])
if cached:cli('init',*args)
try:
    for target in cached:
        status={'target':target}
        try:
            cli(target+'-status','status','--json')
            row=cli(target+'-files','path',target,'--files')
            if not row['matched_expected']:continue
            files=Path((OUT/(target+'-files.stdout')).read_text().strip())
            assert files.is_relative_to(OUT/'cli-installation'),files
            (files/'host-marker.txt').write_text('host')
            cli(target+'-ls-stopped','ls',target,'/etc','--json')
            alias='probe-shell-'+target
            cli(target+'-app-add','app','add',target,alias,'--exec','/bin/sh','--workdir',str(files),'--as-host-user','--on-demand')
            cli(target+'-run','run','--target',target,alias,'--','-c','test "$(cat host-marker.txt)" = host && printf guest > guest-marker.txt')
            status['private_bind_write_verified']=(files/'guest-marker.txt').exists() and (files/'guest-marker.txt').read_text()=='guest'
            cli(target+'-exit17','run','--target',target,alias,'--','-c','exit 17',expected=17)
            cli(target+'-stop','stop',target)
            cli(target+'-ls-after-stop','ls',target,'/etc','--json')
        except Exception as exc:status['error']=repr(exc)
        finally:report['targets'][target]=status;save()
finally:
    for target in cached:
        cli(target+'-cleanup-stop','stop',target)
        cli(target+'-cleanup-remove','env','remove',target,'--yes')
report['after']=inventory()
report['existing_containers_preserved']=report['before']==report['after']
report['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
print(json.dumps({'results':str(OUT),'existing_containers_preserved':report['existing_containers_preserved']}),flush=True)
