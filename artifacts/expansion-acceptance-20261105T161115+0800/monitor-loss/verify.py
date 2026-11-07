import json,subprocess,sys
from pathlib import Path
root=Path(__file__).resolve().parents[3];sys.path[:0]=[str(root/'src'),str(root)]
from tools.reuse_test_guard import unit
out=Path(__file__).resolve().parent
post='/usr/bin/python3 '+str(root/'tools/reuse_test_guard.py')+' rescue '+str(out/'rescue-spec.json')
argv=['systemd-run','--user','--wait','--pipe','--unit=unifier-accept-161115-loss-probe','--slice=unifier-tests.slice','--property=MemoryMax=32M','--property=MemorySwapMax=0','--property=TasksMax=8','--property=CPUQuota=25%','--property=RuntimeMaxSec=30','--property=TimeoutStopSec=2s','--property=KillMode=control-group','--property=Restart=no','--property=ExecStopPost='+post,'/usr/bin/python3',str(out/'probe.py')]
r=subprocess.run(argv,capture_output=True,text=True,timeout=40)
assert r.returncode!=0,r.stdout
rescue=json.loads((out/'rescue.json').read_text());assert rescue['status']=='NO_START',rescue
child=unit('unifier-accept-161115-orphan.service');assert child.get('MainPID')=='0' and not child.get('ControlGroup'),child
result={'status':'PASS','scope':'T1 host-only disjoint-unit monitor-loss proof; no container started','probe_returncode':r.returncode,'rescue':rescue,'orphan_final':child}
(out/'result.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
