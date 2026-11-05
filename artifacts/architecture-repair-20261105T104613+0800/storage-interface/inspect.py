import json, subprocess
from pathlib import Path
OUT=Path(__file__).resolve().parent
def call(argv):
 p=subprocess.run(argv,capture_output=True,text=True,timeout=20)
 if p.returncode: raise RuntimeError(p.stderr[-500:])
 return p.stdout
ids=call(['podman','ps','-aq','--no-trunc']).splitlines()
values={'containers_before':ids,'version':call(['podman','--version']).strip(),'selected':[]}
for cid in ['e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45','d78a392cb1750fc95f5e0476806cfda25b6b06a8c018d2277b01e38bab3fddf8']:
 p=json.loads(call(['podman','inspect','--format','json',cid]))[0]
 values['selected'].append({'cid':p['Id'],'state':p.get('State'),'storage':p.get('GraphDriver'),'mounts':p.get('Mounts'),'labels':{k:v for k,v in p.get('Config',{}).get('Labels',{}).items() if k.startswith('multilinux.')}})
values['containers_after']=call(['podman','ps','-aq','--no-trunc']).splitlines()
values['no_start_mount_or_exec_issued']=True
(OUT/'interface.json').write_text(json.dumps(values,ensure_ascii=False,indent=2))
print(json.dumps({'count_before':len(ids),'count_after':len(values['containers_after']),'states':[p['state']['Status'] for p in values['selected']],'scope':'read-only metadata only; no storage mount or inventory helper executed'}))
