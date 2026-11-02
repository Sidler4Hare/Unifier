"""Read-only preservation audit; excludes mutable test fixture records."""
from pathlib import Path
import hashlib,json,os,subprocess
R=Path(__file__).resolve().parents[2];E=Path(__file__).resolve().parent;A=R/'.lab/agents/extension-local-20261031T111237+0800'
def sha(p):
 h=hashlib.sha256()
 with p.open('rb') as f:
  while b:=f.read(1024*1024):h.update(b)
 return h.hexdigest()
before=json.loads((A/'containers-before.json').read_text());daily=[]
for old in before:
 cur=json.loads(subprocess.check_output(['podman','inspect',old['Id']],text=True))[0]
 same=all(cur.get(k)==old.get(k) for k in ['Id','Name','Image','Mounts']) and cur['State']==old['State']
 daily.append({'name':old['Name'],'container_id':old['Id'],'state_before':old['State'],'state_after':cur['State'],'unchanged':same})
state=json.loads((A/'daily-state-before.json').read_text());changed=[s for s,h in state.items() if not Path(s).is_file() or sha(Path(s))!=h]
old_index=subprocess.check_output(['git','ls-files','--stage','-z'],cwd=R,env={**os.environ,'GIT_INDEX_FILE':str(A/'git-index-before')});new_index=subprocess.check_output(['git','ls-files','--stage','-z'],cwd=R)
pre=json.loads((R/'artifacts/extension-test-plan-20261031/preflight.json').read_text());historical=pre.get('historical_inputs_sha256',{});history_changed=[p for p,h in historical.items() if not (R/p).is_file() or sha(R/p)!=h]
source={}
for top in ['src','tools','bin','config']:
 for p in (R/top).rglob('*'):
  if p.is_file() and '__pycache__' not in p.parts and not p.name.endswith('.pyc'):
   source[str(p.relative_to(R))]=sha(p)
(E/'final-source-sha256.json').write_text(json.dumps(source,ensure_ascii=False,indent=2)+'\n')
result={'daily_containers':daily,'daily_state_files_checked':len(state),'changed_daily_state_files':changed,'git_index_entries_unchanged':old_index==new_index,'git_index_entries_sha256':hashlib.sha256(new_index).hexdigest(),'git_index_raw_bytes_unchanged':(A/'git-index-before').read_bytes()==(R/'.git/index').read_bytes(),'historical_inputs_checked':len(historical),'historical_inputs_changed':history_changed,'historical_scope':'Only named revision-3 historical inputs; no claim of an initial full archive hash audit','product_code_manifest':'final-source-sha256.json','source_tree_sha256':hashlib.sha256(json.dumps(source,sort_keys=True).encode()).hexdigest(),'ok':all(x['unchanged'] for x in daily) and not changed and old_index==new_index and not history_changed}
(E/'preservation-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n');print(json.dumps(result,ensure_ascii=False,indent=2))
