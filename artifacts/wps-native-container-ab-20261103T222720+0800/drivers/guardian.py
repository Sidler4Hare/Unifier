import json,os,subprocess,time
from pathlib import Path
P=Path(__file__).resolve().parent
CID='8399ccd38b137d9283b6587007a43744e8d334809ed4864f002f091009ca555e'
ENV=dict(os.environ,GOMAXPROCS='2')
def cmd(a):return subprocess.run(a,env=ENV,capture_output=True,text=True,timeout=12)
def identity():return {'boot':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kwin':cmd(['pgrep','-u',str(os.getuid()),'-x','kwin_wayland']).stdout.split()}
before=identity();start=time.monotonic();low=0;pressure=0
with (P/'guardian-samples.jsonl').open('a',buffering=1) as log:
 while not (P/'guardian.done').exists():
  mem=dict((s.split(':')[0],int(s.split()[1])*1024) for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith(('MemAvailable:','SwapFree:')))
  psi={s.split()[0]:dict(v.split('=') for v in s.split()[1:]) for s in Path('/proc/pressure/memory').read_text().splitlines()}
  low=low+1 if mem['MemAvailable']<2048*1024**2 else 0
  pressure=pressure+1 if float(psi['full']['avg10'])>3 or float(psi['some']['avg10'])>10 else 0
  log.write(json.dumps({'at':time.time(),**mem,'psi':psi})+'\n')
  reason='host_reserve' if low>=2 else 'host_psi' if pressure>=8 else 'runtime' if time.monotonic()-start>2400 else None
  if (P/'abort.request').exists():reason='explicit_stop'
  if reason:
   receipt={'reason':reason,'at':time.time(),'memory':mem,'psi':psi,'before':before}
   (P/'guardian-abort.json').write_text(json.dumps(receipt,indent=2))
   for name in ['unifier-wps-ab-native.service','unifier-wps-ab-display.service','unifier-wps-ab-prep.service']:
    cmd(['systemctl','--user','stop',name])
   info=cmd(['podman','inspect',CID])
   if info.returncode==0:
    i=json.loads(info.stdout)[0]
    if i['Id']==CID and i['Name']=='ml-45dc9c7d-deps-debian' and not i['Mounts']:
     receipt['stop']=cmd(['podman','stop','--time','1',CID]).returncode
   receipt['after']=identity();(P/'guardian-abort.json').write_text(json.dumps(receipt,indent=2));break
  time.sleep(.25)
(P/'guardian-finished.json').write_text(json.dumps({'before':before,'after':identity(),'aborted':(P/'guardian-abort.json').exists()}))
