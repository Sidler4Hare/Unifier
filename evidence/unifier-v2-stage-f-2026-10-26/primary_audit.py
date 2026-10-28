"""Independent F preservation audit. Read only; never cleans or repairs state."""
import argparse,hashlib,json,subprocess,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);a=p.parse_args()
if a.output.exists(): raise SystemExit('refusing to overwrite audit')
b=json.loads((ROOT/'.lab/agents/unifier-v2-stage-f-20261026/baseline.json').read_text())
changes={}
for group in ('source','main_state','old_evidence'):
 bad=[]
 for name,expected in b[group].items():
  f=ROOT/name
  if not f.is_file():bad.append({'path':name,'reason':'missing'})
  elif hashlib.sha256(f.read_bytes()).hexdigest()!=expected:bad.append({'path':name,'reason':'hash changed'})
 changes[group]={'checked':len(b[group]),'changes':bad}
now=json.loads(subprocess.check_output(['podman','ps','-a','--no-trunc','--format=json']))
old={x['Id']:x for x in b['containers']}; current={x['Id']:x for x in now}
container_changes=[]
for cid,row in old.items():
 if cid not in current:container_changes.append({'id':cid,'reason':'missing'})
 else:
  for key in ('Names','State'):
   if row.get(key)!=current[cid].get(key):container_changes.append({'id':cid,'field':key,'before':row.get(key),'after':current[cid].get(key)})
volumes=json.loads(subprocess.check_output(['podman','volume','ls','--format=json']))
volume_names=lambda rows:sorted(r['Name'] for r in rows)
fixture=str(ROOT/'evidence/unifier-v2-stage-f-2026-10-26')
mounts=[l for l in Path('/proc/self/mountinfo').read_text().splitlines() if fixture in l or 'unifier-view' in l or 'multilinux-view' in l]
result={'time':time.time(),'hash_groups':changes,'original_containers':len(old),'container_changes':container_changes,'extra_containers':sorted(set(current)-set(old)),'volumes_before':volume_names(b['volumes']),'volumes_after':volume_names(volumes),'visible_matching_mounts':mounts,'scope':'hash preservation plus exact original CID/name/state; runtime private namespace and PID cleanup require separate checks'}
result['passed']=not any(g['changes'] for g in changes.values()) and not container_changes and not result['extra_containers'] and result['volumes_before']==result['volumes_after'] and not mounts
a.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
print(json.dumps({k:v for k,v in result.items() if k not in ('hash_groups','visible_matching_mounts')},ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
