import os,json,subprocess,time,tarfile,shutil
from pathlib import Path
P=Path(__file__).resolve().parent;R=P.parents[2];CID='8399ccd38b137d9283b6587007a43744e8d334809ed4864f002f091009ca555e'
os.environ['GOMAXPROCS']='2'
def run(a,**kw):return subprocess.run(a,check=True,**kw)
i=json.loads(subprocess.check_output(['podman','inspect',CID]))[0]
assert i['Id']==CID and not i['State']['Running'] and not i['Mounts'] and i['HostConfig']['Memory']==0 and i['HostConfig']['NanoCpus']==0
if not (P/'container-before.json').exists():(P/'container-before.json').write_text(json.dumps(i,indent=2))
available=int(next(x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')).split()[1])*1024
assert available>2800*1024**2,available
assert not (P/'guardian-abort.json').exists()
native=P/'native-29183';native.mkdir(exist_ok=True)
rpm=next((P/'downloads').glob('wps-office*.rpm'))
if not (native/'opt/kingsoft/wps-office/office6/wpp').exists():
 with subprocess.Popen(['rpm2cpio',str(rpm)],stdout=subprocess.PIPE) as a:
  run(['cpio','-idmu','--quiet'],stdin=a.stdout,cwd=native);a.stdout.close();assert a.wait()==0
run(['podman','start',CID])
pid=json.loads(subprocess.check_output(['podman','inspect',CID]))[0]['State']['Pid'];(P/'container-pid').write_text(str(pid))
ns=['podman','unshare','nsenter','--target',str(pid),'--net','--']
probe=run(ns+['/usr/bin/id'],capture_output=True,text=True);print('native namespace identity:',probe.stdout.strip(),flush=True)
(P/'native-ns.json').write_text(json.dumps(ns))
guest='/tmp/unifier-wps-ab-20261103'
run(['podman','exec','--user','0',CID,'install','-d','-o','0','-g','0',guest])
deb=R/'.lab/agents/extension-local-20261031T111237+0800/downloads/wps-office.deb'
run(['podman','exec','--user','0',CID,'rm','-f',guest+'/wps.deb'])
run(['podman','exec','--user','0',CID,'mkdir','-p',guest+'/root'])
a=subprocess.Popen(['ar','p',str(deb),'data.tar.xz'],stdout=subprocess.PIPE)
b=subprocess.Popen(['xz','-d','--threads=1','--memlimit-decompress=256MiB'],stdin=a.stdout,stdout=subprocess.PIPE);a.stdout.close()
c=subprocess.Popen(['podman','exec','-i','--user','0',CID,'tar','-x','-C',guest+'/root'],stdin=subprocess.PIPE)
tick=time.monotonic();total=0
while buf:=b.stdout.read(1024*1024):
 c.stdin.write(buf);total+=len(buf)
 time.sleep(max(0,total/(24*1024**2)-(time.monotonic()-tick)))
c.stdin.close();assert c.wait()==b.wait()==a.wait()==0
res=run(['podman','exec','--user','0',CID,'ldd',guest+'/root/opt/kingsoft/wps-office/office6/wps'],capture_output=True,text=True)
(P/'guest-ldd.txt').write_text(res.stdout);print('missing:',[s for s in res.stdout.splitlines() if 'not found' in s],flush=True)
print('PREPARED',flush=True)
(P/'prepared.json').write_text(json.dumps({'pid':pid,'guest_prefix':guest,'native_ns':ns}))
while not (P/'prep.done').exists() and not (P/'guardian-abort.json').exists():time.sleep(.5)
run(['podman','stop','--time','2',CID])
