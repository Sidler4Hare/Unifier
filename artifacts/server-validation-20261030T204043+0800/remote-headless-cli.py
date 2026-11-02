import datetime,json,os,subprocess,sys,traceback
from pathlib import Path
BASE=Path(__file__).resolve().parent;ROOT=BASE/'unifier-current'
OUT=BASE/('cli-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S'));OUT.mkdir()
ENV=dict(os.environ,LC_ALL='C',LANG='C',PYTHONDONTWRITEBYTECODE='1',UNIFIER_HOME=str(OUT/'installation'),PYTHONPATH=str(ROOT/'src')+os.pathsep+str(ROOT))
report={'scope':'Headless CLI, cached images, private bind read/write, exit17, running/stopped guest listing; no GUI-app launch acceptance','commands':{},'targets':{}}
def save(): (OUT/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
def cmd(name,*args,expected=0,timeout=180):
    p=subprocess.run([sys.executable,'tools/unifier.py',*args],cwd=ROOT,env=ENV,capture_output=True,text=True,timeout=timeout)
    (OUT/(name+'.stdout')).write_text(p.stdout);(OUT/(name+'.stderr')).write_text(p.stderr)
    report['commands'][name]={'argv':list(args),'returncode':p.returncode,'expected':expected,'matched_expected':p.returncode==expected};save()
    print(json.dumps({'step':name,**report['commands'][name]}),flush=True)
    return p
ids=subprocess.check_output(['podman','ps','-aq'],text=True).split()
def inventory(ids):
    if not ids:return []
    data=json.loads(subprocess.check_output(['podman','inspect',*ids],text=True))
    return sorted([{'id':x['Id'],'name':x['Name'],'state':x['State']} for x in data],key=lambda x:x['id'])
report['before']=inventory(ids)
lock=json.loads((ROOT/'config/images.lock.json').read_text())['targets']
cached=[name for name,item in lock.items() if subprocess.run(['podman','image','exists',item['locked_reference']],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0]
args=['init','--network','none','--yes']
for target in cached:args.extend(['--target',target])
if cached:cmd('init',*args)
try:
    for target in cached:
        files=Path(cmd(target+'-files','path',target,'--files').stdout.strip())
        assert files.is_relative_to(OUT/'installation')
        (files/'host-marker.txt').write_text('host')
        cmd(target+'-start','start',target)
        cmd(target+'-write','env','run',target,'--workdir',str(files),'--','sh','-c','test "$(cat host-marker.txt)" = host && printf guest > guest-marker.txt')
        marker=(files/'guest-marker.txt')
        report['targets'][target]={'private_bind_write_verified':marker.exists() and marker.read_text()=='guest'};save()
        cmd(target+'-exit17','env','run',target,'--workdir','/','--','sh','-c','exit 17',expected=17)
        cmd(target+'-ls-running','ls',target,'/etc','--json')
        cmd(target+'-stop','stop',target)
        for guest in ('/','/usr','/etc'):
            cmd(target+'-ls-stopped-'+guest.replace('/','_'),'ls',target,guest,'--json')
        # Preserve the underlying exception which the user CLI summarizes.
        code=r"""import json,sys,traceback
from pathlib import Path
from unifier.shortcuts import Context
from unifier.guest_paths import list_guest_directories
ctx=Context(Path(sys.argv[1]));manager=ctx.view_manager_for(sys.argv[2]);view=manager.open(sys.argv[2])
try:
    try: print(json.dumps({'result':list_guest_directories(Path(view['mountpoint']),'/etc',projected=True)}))
    except Exception as exc: print(json.dumps({'exception':repr(exc),'errno':getattr(exc,'errno',None),'traceback':traceback.format_exc()}))
finally:manager.revoke(view['view_uuid'])
"""
        p=subprocess.run([sys.executable,'-c',code,str(OUT/'installation'),target],cwd=ROOT,env=ENV,capture_output=True,text=True,timeout=180)
        (OUT/(target+'-view-diagnostic.stdout')).write_text(p.stdout);(OUT/(target+'-view-diagnostic.stderr')).write_text(p.stderr)
finally:
    for target in cached:
        cmd(target+'-cleanup-stop','stop',target)
        cmd(target+'-cleanup-remove','env','remove',target,'--yes')
report['after']=inventory(subprocess.check_output(['podman','ps','-aq'],text=True).split())
report['existing_containers_preserved']=report['before']==report['after'];save()
print(json.dumps({'results':str(OUT),'existing_containers_preserved':report['existing_containers_preserved']}),flush=True)
