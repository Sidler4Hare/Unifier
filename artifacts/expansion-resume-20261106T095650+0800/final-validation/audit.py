"""Read-only final evidence; never start, recover, or stop anything."""
import datetime,hashlib,json,os,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[3];OUT=Path(__file__).resolve().parent;RUN=OUT.parent
def call(argv):
    value=subprocess.run(argv,capture_output=True,text=True,timeout=20)
    assert value.returncode==0,value.stderr[-1000:]
    return value.stdout
def digest(path):return hashlib.sha256(path.read_bytes()).hexdigest()
before=json.loads((RUN/'inventory-before.json').read_text())
allinfo=json.loads(call(['podman','ps','-a','--no-trunc','--format','json']))
allinfo=[{k:x.get(k) for k in ('Id','Names','State','Image','Created')} for x in allinfo]
old={x['Id']:x for x in before['all']};new={x['Id']:x for x in allinfo}
checks={'same_container_ids':set(old)==set(new),
        'same_container_states':all(new[k]['State']==v['State'] for k,v in old.items() if k in new)}
selected=[]
for prior in before['selected']:
    cid=prior['cid'];info=json.loads(call(['podman','inspect','--format','json',cid]))[0]
    labels=info['Config']['Labels'];project=Path(labels['multilinux.project']);eid=labels['multilinux.env']
    record=json.loads((project/'.lab/state/environments'/(eid+'.json')).read_text())
    lease_path=project/'.lab/leases'/(eid+'.json')
    lease=json.loads(lease_path.read_text()) if lease_path.exists() else None
    stable_config=all(info['HostConfig'].get(k)==v for k,v in prior['host_config'].items())
    checks['config_'+eid]=stable_config and info['Mounts']==prior['mounts']
    checks['binding_'+eid]=(record['environment_uuid']==prior['environment_binding']['environment_uuid']
        and record['generation']==prior['environment_binding']['generation']
        and record['runtime']['container_id']==cid)
    checks['leases_'+eid]=((lease is None and prior['leases'] is None)
        or (isinstance(lease,dict) and not lease.get('leases') and not lease.get('bridge')))
    checks['stopped_'+eid]=not info['State']['Running'] and info['State']['Pid']==0
    checks['inventory_'+eid]=(record['package_inventory_sha256']==prior['environment_binding']['package_inventory_sha256'])
    selected.append({'cid':cid,'state':info['State'],'leases':lease,
        'package_inventory_sha256':record['package_inventory_sha256']})
snap=json.loads((RUN/'before.json').read_text())
checks['head_unchanged']=call(['git','rev-parse','HEAD']).strip()==snap['head']
checks['index_unchanged']=digest(ROOT/'.git/index')==snap['index_sha256']
checks['boot_unchanged']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()==snap['boot_id']
allowed={'src/unifier/runtime.py','src/unifier/exec_control.py','tools/reuse_test_guard.py',
         'tools/reuse_guard_policy.py','tests/test_runtime_control.py',
         'tests/test_reuse_guard_policy.py','tests/test_reuse_test_guard.py',
         'HANDOFF.md','docs/共享容器与依赖专项测试计划-20261104.md'}
changed=[name for name,sha in snap['files'].items() if (ROOT/name).exists() and digest(ROOT/name)!=sha]
checks['original_files_preserved']=set(changed)<=allowed
missing=[name for name in snap['files'] if not (ROOT/name).exists()]
checks['original_files_present']=not missing
prefixes=set()
for base in (RUN,ROOT/'artifacts/expansion-acceptance-20261105T161115+0800'):
    for path in base.rglob('spec.json'):
        config=json.loads(path.read_text());unit=config.get('unit')
        if unit and unit.startswith('unifier-') and unit!=os.environ.get(
                'UNIFIER_AUDIT_UNIT','unifier-resume-1106-final'):prefixes.add(unit)
units={}
for prefix in sorted(prefixes):
    for suffix in ('-work.service','-guard.service'):
        name=prefix+suffix
        value=call(['systemctl','--user','show',name,'-p','ActiveState','-p','MainPID','-p','ControlGroup'])
        units[name]=dict(line.split('=',1) for line in value.splitlines() if '=' in line)
checks['previous_units_quiescent']=all(v['MainPID']=='0' and not v['ControlGroup']
    and v['ActiveState'] in ('inactive','failed') for v in units.values())
slice_fields=call(['systemctl','--user','show','unifier-tests.slice','-p','TasksCurrent','-p','ControlGroup',
    '-p','MemoryMax','-p','MemorySwapMax','-p','TasksMax'])
checks['aggregate_empty']='TasksCurrent=0\n' in slice_fields
launcher=Path('/home/omg/.local/bin/unifier')
launcher_target=str(launcher.resolve())
checks['daily_release_not_changed']='/releases/0.0.2-b656bf73abfe/' in launcher_target
value={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'checks':checks,
    'all':allinfo,'selected':selected,'units':units,'aggregate':slice_fields,
    'changed_snapshotted_files':changed,'missing_files':missing,'daily_launcher':launcher_target,
    'source_hashes':{str(p.relative_to(ROOT)):digest(p) for base in ('src','tools','tests')
        for p in (ROOT/base).rglob('*.py') if '__pycache__' not in p.parts}}
value['status']='PASS' if all(checks.values()) else 'FAIL'
(RUN/'final-audit.json').write_text(json.dumps(value,ensure_ascii=False,indent=2))
print(json.dumps({'audit':value['status'],'checks':checks},ensure_ascii=False))
raise SystemExit(0 if value['status']=='PASS' else 1)
