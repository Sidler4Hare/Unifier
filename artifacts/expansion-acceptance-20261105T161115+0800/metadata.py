import json,subprocess,hashlib,shutil,os
from pathlib import Path
OUT=Path(__file__).resolve().parent
PRIVATE=Path('/tmp/unifier-acceptance-20261105T161115')
CIDS=['e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45','d78a392cb1750fc95f5e0476806cfda25b6b06a8c018d2277b01e38bab3fddf8']
def call(argv):
 p=subprocess.run(argv,capture_output=True,text=True,timeout=20)
 if p.returncode:raise RuntimeError(p.stderr[-500:])
 return p.stdout
allinfo=json.loads(call(['podman','ps','-a','--no-trunc','--format','json']))
result={'version':call(['podman','--version']).strip(),'all':[{k:x.get(k) for k in ('Id','Names','State','Status','Image','Created')} for x in allinfo],'selected':[],'rootless_query_only':True}
for cid in CIDS:
 info=json.loads(call(['podman','inspect','--format','json',cid]))[0]
 cfg=info['HostConfig'];labels=info['Config'].get('Labels',{})
 project=Path(labels['multilinux.project']);eid=labels['multilinux.env']
 recpath=project/'.lab/state/environments'/(eid+'.json')
 rec=json.loads(recpath.read_text());assert rec['runtime']['container_id']==cid
 files=[recpath,project/'.lab/leases'/(eid+'.json'),*list((project/'.lab/state/transactions').glob('*.json'))]
 backup=PRIVATE/cid;backup.mkdir(mode=0o700)
 hashes={}
 for f in files:
  if f.is_file():
   rel=f.relative_to(project);dest=backup/rel;dest.parent.mkdir(parents=True,exist_ok=True,mode=0o700);shutil.copy2(f,dest);hashes[str(rel)]=hashlib.sha256(f.read_bytes()).hexdigest()
 value={'cid':cid,'state':info['State'],'conmon_pid':info.get('ConmonPid'),'host_config':{k:cfg.get(k) for k in ('CgroupParent','CgroupMode','Cgroups','Memory','MemorySwap','PidsLimit','NanoCpus','CpuPeriod','CpuQuota','IpcMode','PidMode','NetworkMode','Privileged','Devices','RestartPolicy','Ulimits','SecurityOpt')},'mounts':info['Mounts'],'labels':{k:v for k,v in labels.items() if k.startswith('multilinux.')},'environment_variable_names':[x.split('=',1)[0] for x in info['Config'].get('Env',[])],'environment_binding':{k:rec.get(k) for k in ('id','target','schema_version','environment_uuid','generation','package_inventory_sha256','package_recovery_required')},'record_files_sha256':hashes,'leases':json.loads((project/'.lab/leases'/(eid+'.json')).read_text()) if (project/'.lab/leases'/(eid+'.json')).exists() else None,'abort_latch':{'exists':(project.parent/'.guard-abort.json').exists(),'sha256':hashlib.sha256((project.parent/'.guard-abort.json').read_bytes()).hexdigest() if (project.parent/'.guard-abort.json').is_file() else None}}
 result['selected'].append(value)
result['slice']=call(['systemctl','--user','show','unifier-tests.slice','-p','ActiveState','-p','ControlGroup','-p','MemoryMax','-p','MemoryHigh','-p','MemorySwapMax','-p','TasksMax','-p','TasksCurrent','-p','CPUQuotaPerSecUSec'])
(OUT/'inventory-before.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({'count':len(result['all']),'selected':[{'cid':x['cid'],'state':x['state']['Status'],'config':x['host_config'],'lease_count':len(x.get('leases') or {}),'abort_latch':x['abort_latch']} for x in result['selected']]},ensure_ascii=False))
