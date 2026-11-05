import os,subprocess,json,time,hashlib
from pathlib import Path
P=Path(__file__).resolve().parent;CID='8399ccd38b137d9283b6587007a43744e8d334809ed4864f002f091009ca555e';os.environ['GOMAXPROCS']='2'
def run(a,**kw):return subprocess.run(a,check=True,**kw)
i=json.loads(subprocess.check_output(['podman','inspect',CID]))[0];assert i['Id']==CID and not i['State']['Running'] and not i['Mounts'] and i['HostConfig']['Memory']==i['HostConfig']['NanoCpus']==0
assert not (P/'guardian-abort.json').exists()
cloud=P/'native-29183/opt/kingsoft/wps-office/office6/wpscloudsvr';sha=hashlib.sha256(cloud.read_bytes()).hexdigest();cloud.rename(cloud.with_name('wpscloudsvr.ab-disabled'))
run(['podman','start',CID]);pid=json.loads(subprocess.check_output(['podman','inspect',CID]))[0]['State']['Pid'];(P/'container-pid').write_text(str(pid))
(P/'native-ns.json').write_text(json.dumps(['podman','unshare','nsenter','--target',str(pid),'--net','--']))
guest='/tmp/unifier-wps-ab-20261103/root/opt/kingsoft/wps-office/office6/'
run(['podman','exec','--user','0',CID,'mv',guest+'wpscloudsvr',guest+'wpscloudsvr.ab-disabled'])
(P/'core-only.json').write_text(json.dumps({'reason':'match local-editor process sets; native cloud auxiliary SIGSEGV invalidates full-suite baseline','cloud_sha256':sha,'restoration':'rename wpscloudsvr.ab-disabled back in both private test payloads; host installation unchanged','container_id':CID}))
(P/'prepared.json').write_text(json.dumps({'pid':pid,'mode':'LOCAL_CORE_ONLY'}));print('READY_LOCAL_CORE_ONLY',flush=True)
try:
 while not (P/'prep.done').exists() and not (P/'guardian-abort.json').exists():time.sleep(.25)
finally:
 run(['podman','stop','--time','2',CID])
