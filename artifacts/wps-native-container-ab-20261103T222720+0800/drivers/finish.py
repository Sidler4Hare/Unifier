import os,json,subprocess,time,shutil,hashlib
from pathlib import Path
P=Path(__file__).resolve().parent;EV=Path((P/'evidence-path').read_text().strip());CID='8399ccd38b137d9283b6587007a43744e8d334809ed4864f002f091009ca555e';os.environ['GOMAXPROCS']='2'
def run(a,**kw):return subprocess.run(a,capture_output=True,text=True,timeout=kw.pop('timeout',20),**kw)
while True:
 try:
  batch=json.loads((P/'core-batch.json').read_text())
  if batch['status']!='RUNNING':break
 except (FileNotFoundError,json.JSONDecodeError):pass
 if (P/'guardian-abort.json').exists():break
 time.sleep(.5)
result={'at':time.time(),'errors':[]}
try:
 info=json.loads(run(['podman','inspect',CID]).stdout)[0]
 if info['State']['Running']:
  group=Path('/proc/'+str(info['State']['Pid'])+'/cgroup').read_text().split('0::',1)[1].strip()
  run(['podman','unshare','/usr/bin/python3',str(P/'metrics.py'),'/sys/fs/cgroup'+group,str(P/'final-container-idle.jsonl'),'1'])
  for name in ['unifier-wps-ab-prep.service','unifier-wps-ab-display.service']:
   group=run(['systemctl','--user','show',name,'-p','ControlGroup','--value']).stdout.strip()
   if group:run(['/usr/bin/python3',str(P/'metrics.py'),'/sys/fs/cgroup'+group,str(P/('final-'+name+'.jsonl')),'1'])
  guest='/tmp/unifier-wps-ab-20261103/root/opt/kingsoft/wps-office/office6/'
  r=run(['podman','exec','--user','0',CID,'mv',guest+'wpscloudsvr.ab-disabled',guest+'wpscloudsvr']);result['guest_cloud_restored']=r.returncode==0
  r=run(['podman','exec','--user','0',CID,'cat','/var/lib/dpkg/status']);(P/'dpkg-after.txt').write_text(r.stdout)
  r=run(['podman','exec','--user','1000:1000',CID,'sha256sum',*['/tmp/unifier-wps-ab-20261103/common/'+x for x in ['writer.docx','sheet.xlsx','slides.pptx']]]);(P/'guest-fixtures-after.sha256').write_text(r.stdout)
 cloud=P/'native-29183/opt/kingsoft/wps-office/office6/wpscloudsvr.ab-disabled'
 if cloud.exists():cloud.rename(cloud.with_name('wpscloudsvr'));result['native_cloud_restored']=True
except Exception as e:result['errors'].append(str(e))
for name in ['unifier-wps-ab-native.service','unifier-wps-ab-display.service']:run(['systemctl','--user','stop',name])
(P/'prep.done').touch()
for _ in range(20):
 i=json.loads(run(['podman','inspect',CID]).stdout)[0]
 if not i['State']['Running']:break
 time.sleep(.25)
else:run(['podman','stop','--time','1',CID])
(P/'guardian.done').touch();time.sleep(.5)
after=json.loads(run(['podman','inspect',CID]).stdout)[0];before=json.loads((P/'container-before.json').read_text())
result.update(container_stopped=not after['State']['Running'],host_config_unchanged=before['HostConfig']==after['HostConfig'],mounts_unchanged=before['Mounts']==after['Mounts'],boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip(),kwin=run(['pgrep','-u',str(os.getuid()),'-x','kwin_wayland']).stdout.split(),native_installed_version=run(['rpm','-q','wps-office']).stdout.strip())
(P/'container-after.json').write_text(json.dumps(after,indent=2));(P/'finish.json').write_text(json.dumps(result,indent=2))
EV.mkdir(exist_ok=True)
for name in ['core-batch.json','core-firstpass.json','core-only.json','comparison-inputs.json','native-package.json','finish.json','guardian-finished.json','guardian-abort.json','guardian-samples.jsonl','guest-fixtures-after.sha256','display-ready.json']:
 if (P/name).exists():shutil.copy2(P/name,EV/name)
for f in P.glob('final-*.jsonl'):shutil.copy2(f,EV/f.name)
for attempt in ['attempt1','attempt2']:
 if (P/attempt).exists():shutil.copytree(P/attempt,EV/attempt,dirs_exist_ok=True)
for cell in (P/'cells').iterdir():
 dest=EV/'cells'/cell.name;dest.mkdir(parents=True,exist_ok=True)
 for f in cell.iterdir():
  if f.is_file() and f.suffix in ['.json','.jsonl','.png','.log']:shutil.copy2(f,dest/f.name)
drivers=EV/'drivers';drivers.mkdir(exist_ok=True)
for f in P.glob('*.py'):shutil.copy2(f,drivers/f.name)
hashes={f.name:hashlib.sha256(f.read_bytes()).hexdigest() for f in (P/'common').iterdir() if f.suffix in ['.docx','.xlsx','.pptx']};(EV/'native-fixtures-after.json').write_text(json.dumps(hashes,indent=2))
print(json.dumps(result,indent=2),flush=True)
