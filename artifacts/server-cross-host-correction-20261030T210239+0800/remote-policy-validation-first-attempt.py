import datetime,hashlib,json,os,subprocess,sys
from pathlib import Path
BASE=Path(__file__).resolve().parent;ROOT=BASE/'unifier-current';OUT=BASE/'results';OUT.mkdir()
ENV=dict(os.environ,LC_ALL='C',LANG='C',PYTHONDONTWRITEBYTECODE='1',MULTILINUX_INTEGRATION='0',MULTILINUX_EVIDENCE='0',PYTHONPATH=os.pathsep.join(str(ROOT/p) for p in ('src','tests','')))
ENV['UNIFIER_HOME']=str(OUT/'negative-installation')
report={'started_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'hostname':os.uname().nodename,'policy_version':2,'commands':{},'positive_cli':{},'scope':'Live cross-family lifecycle and private files, same-family rejection before creation, policy regressions; not complete GUI/Agent/application acceptance'}
def save(): (OUT/'summary.json').write_text(json.dumps(report,indent=2)+'\n')
def cmd(name,args,expected=0,env=None,timeout=300):
    p=subprocess.run(args,cwd=ROOT,env=env or ENV,capture_output=True,text=True,timeout=timeout)
    (OUT/(name+'.stdout')).write_text(p.stdout);(OUT/(name+'.stderr')).write_text(p.stderr)
    report['commands'][name]={'argv':args,'returncode':p.returncode,'expected':expected,'matched_expected':p.returncode==expected};save()
    print(json.dumps({'step':name,**report['commands'][name]}),flush=True)
    return p
def cli(name,*args,expected=0,env=None):return cmd(name,[sys.executable,'tools/unifier.py',*args],expected,env)
def inventory():
    ids=subprocess.check_output(['podman','ps','-aq'],text=True).split()
    if not ids:return []
    data=json.loads(subprocess.check_output(['podman','inspect',*ids],text=True))
    return sorted([{'id':x['Id'],'name':x['Name'],'status':x['State']['Status'],'started_at':x['State']['StartedAt'],'finished_at':x['State']['FinishedAt']} for x in data],key=lambda x:x['id'])
manifest=json.loads((ROOT/'BUNDLE-MANIFEST.json').read_text())
for rel,item in manifest['source_files'].items():
    p=ROOT/rel
    if item['kind']=='symlink':assert p.is_symlink() and str(p.readlink())==item['target']
    else:assert hashlib.sha256(p.read_bytes()).hexdigest()==item['sha256'],rel
report['source_files_verified']=len(manifest['source_files']);report['before']=inventory();save()
p=cli('matrix','compat','matrix',expected=1);plans=json.loads(p.stdout)
allowed=[x['target'] for x in plans if x['allowed']];blocked=[x['target'] for x in plans if not x['allowed']]
assert len(allowed)==2 and len(blocked)==1,plans
assert all(x['relationship']=='cross-family' for x in plans if x['allowed'])
assert all(any('same-family containers are forbidden' in r for r in x['reasons']) for x in plans if not x['allowed'])
report['allowed_targets']=allowed;report['blocked_targets']=blocked;save()
cmd('doctor-direct',[sys.executable,'tools/lab.py','doctor','--output',str(OUT/'doctor.json')])
for target in blocked:
    cli('refuse-init-'+target,'init','--target',target,'--yes',expected=1)
    cli('refuse-create-'+target,'env','create',target,target,expected=1)
    cmd('refuse-pull-'+target,[sys.executable,'tools/lab.py','pull','--target',target],expected=1)
    cmd('refuse-probe-'+target,[sys.executable,'tools/probe_headless.py','--target',target,'--output',str(OUT/'forbidden-headless.json')],expected=2)
assert not (Path(ENV['UNIFIER_HOME'])/'install-state.json').exists()
assert not (OUT/'forbidden-headless.json').exists()
report['same_family_rejection_without_registration']=True;save()
# The old Debian-on-Debian environment is retained, but cannot be started/executed.
if os.uname().nodename=='debian-lab01':
    legacy=Path.home()/'.local/share/unifier'
    from unifier.store import EnvironmentStore
    store=EnvironmentStore(legacy)
    if store.exists('debian13'):
        record=store.load('debian13')
        extra=dict(ENV,UNIFIER_HOME=str(legacy))
        cli('legacy-refuse-start','env','start','debian13',expected=1,env=extra)
        cli('legacy-refuse-run','env','run','debian13','--workdir','/','--','true',expected=1,env=extra)
        assert store.load('debian13')==record
        report['legacy_same_family_record_preserved']=True;save()
cmd('policy-unit-tests',[sys.executable,'-m','unittest','-q','test_compatibility','test_lab','test_cli_shortcuts.InitAndConfigure.test_same_family_selection_refuses_before_image_or_container_side_effects','test_cli_shortcuts.InitAndConfigure.test_interactive_selection_omits_host_family_and_keeps_legacy_data','test_cli_shortcuts.InitAndConfigure.test_start_cannot_acknowledge_an_already_running_same_family_environment'])
cmd('headless-default',[sys.executable,'tools/probe_headless.py','--output',str(OUT/'headless.json')])
h=json.loads((OUT/'headless.json').read_text());assert set(h['targets'])==set(allowed),h
report['headless_passed']=h['passed'];save()
positive=dict(ENV,UNIFIER_HOME=str(OUT/'positive-installation'))
args=['init','--network','none','--yes']
for target in allowed:args.extend(['--target',target])
cli('positive-init',*args,env=positive)
try:
    for target in allowed:
        p=cli(target+'-files','path',target,'--files',env=positive)
        files=Path(p.stdout.strip());assert files.is_relative_to(OUT/'positive-installation')
        (files/'host-marker.txt').write_text('host')
        cli(target+'-start','start',target,env=positive)
        cli(target+'-write','env','run',target,'--workdir',str(files),'--','sh','-c','test "$(cat host-marker.txt)" = host && printf guest > guest-marker.txt',env=positive)
        marker=files/'guest-marker.txt'
        report['positive_cli'][target]={'private_bind_write_verified':marker.exists() and marker.read_text()=='guest'};save()
        cli(target+'-exit17','env','run',target,'--workdir','/','--','sh','-c','exit 17',expected=17,env=positive)
finally:
    for target in allowed:
        cli(target+'-cleanup-stop','stop',target,env=positive)
        cli(target+'-cleanup-remove','env','remove',target,'--yes',env=positive)
report['after']=inventory();report['existing_containers_preserved']=report['before']==report['after']
report['remaining_task_mounts']=[x for x in Path('/proc/self/mountinfo').read_text().splitlines() if BASE.name in x]
report['passed_scope']=(all(x['matched_expected'] for x in report['commands'].values()) and report['headless_passed'] and report['existing_containers_preserved'] and all(x['private_bind_write_verified'] for x in report['positive_cli'].values()) and not report['remaining_task_mounts'])
report['completed_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();save()
print(json.dumps({'results':str(OUT),'passed_scope':report['passed_scope']}),flush=True)
