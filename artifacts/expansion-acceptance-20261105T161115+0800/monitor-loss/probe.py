import json,os,signal,subprocess,sys,time
from pathlib import Path
root=Path(__file__).resolve().parents[3];sys.path[:0]=[str(root/'src'),str(root)]
from tools.reuse_test_guard import unit,put,process
out=Path(__file__).resolve().parent
name='unifier-accept-161115-orphan.service'
subprocess.run(['systemd-run','--user','--quiet','--unit='+name,'--slice=unifier-tests.slice','--property=MemoryMax=32M','--property=MemorySwapMax=0','--property=TasksMax=8','--property=CPUQuota=50%','--property=RuntimeMaxSec=120','--property=TimeoutStopSec=2s','--property=KillMode=control-group','--property=Restart=no','/usr/bin/python3','-c','import time;time.sleep(120)'],check=True,timeout=10)
identity=unit(name);put(out/'worker-unit.json',identity);put(out/'registered-process.json',process(int(identity['MainPID'])))
time.sleep(.2)
os.kill(os.getpid(),signal.SIGKILL)
