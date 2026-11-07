import json,subprocess
from pathlib import Path
out=Path(__file__).resolve().parent
cid='e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45'
c=json.loads(subprocess.check_output(['podman','inspect','--format','json',cid],text=True,timeout=15))[0]
v={'pid_fields':{k:v for k,v in c.items() if 'pid' in k.lower()},'state':c['State'],'cmd':c['Config'].get('Cmd'),'entrypoint':c['Config'].get('Entrypoint'),'network':{k:v for k,v in c.get('NetworkSettings',{}).items() if k in ('SandboxKey','SandboxID')},'labels':c['Config'].get('Labels')}
(out/'result.json').write_text(json.dumps(v,ensure_ascii=False,indent=2));print(json.dumps(v,ensure_ascii=False))
