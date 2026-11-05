import json,subprocess,sys,copy,traceback,os
from pathlib import Path
REPO=Path('/home/omg/文档/多兼容linux机');sys.path[:0]=[str(REPO/'src'),str(REPO)]
from unifier.project import load_project
from unifier.service import EnvironmentService
from tools.guarded_test import GuardedPodmanRuntime,require_headroom,require_slice
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text())/'guarded-fixture';OUT.mkdir(exist_ok=True)
BASE=REPO/'.lab/guarded-expansion-20261103';ROOT=BASE/'environment';SHARED=BASE/'shared';CID='076f6017d2a47a3fd6e2c996d6fb819c7c8ec2159d0fbb436be2a6e4bbd0e5d7';image='localhost/unifier/guarded-debian-20261103:fixture'
result={'status':'RUNNING','scope':'preserved installed fixture snapshot -> new headless test root; no host desktop/audio/GPU/device mounts','source_container':CID}
def save(): (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
try:
 require_headroom();require_slice();save()
 old=json.loads(subprocess.check_output(['podman','inspect',CID],text=True))[0]
 assert not old['State']['Running']
 assert old['Config']['Labels']['multilinux.project']==str(REPO/'.lab/extension-local-20261031/environments')
 assert subprocess.run(['podman','image','exists',image]).returncode!=0,'snapshot image already exists; refusing overwrite'
 print('committing preserved stopped fixture',flush=True)
 result['image_id']=subprocess.check_output(['podman','commit','--format','oci',CID,image],text=True,timeout=300).strip();save()
 info=json.loads(subprocess.check_output(['podman','image','inspect',image],text=True))[0]
 digest=info['Digest'];ref=image.rsplit(':',1)[0]+'@'+digest
 subprocess.run(['podman','image','exists',ref],check=True)
 result['snapshot']={'id':info['Id'],'digest':digest,'reference':ref,'size':info['Size'],'private_login_state':'retained locally in fixture image; not exported'};save()
 BASE.mkdir(exist_ok=True);SHARED.mkdir(exist_ok=True)
 config,lock,_=load_project();lock=copy.deepcopy(lock);lock['targets']['debian13'].update(source=image,manifest_digest=digest,locked_reference=ref,image_version_label=None)
 (CTL/'guarded-images.lock.json').write_text(json.dumps(lock,indent=2))
 runtime=GuardedPodmanRuntime(ROOT);svc=EnvironmentService(ROOT,runtime,config,lock)
 print('creating bounded headless fixture',flush=True)
 record=svc.create('debian13','debian13',[str(SHARED)],'pasta',user_namespace='keep-id',share_label_policy='shared-relabel')
 (OUT/'environment.json').write_text(json.dumps(record,ensure_ascii=False,indent=2));result['container_id']=record['runtime']['container_id'];result['environment_root']=str(ROOT);result['shared_root']=str(SHARED);result['status']='PASS_HEADLESS_GUARDED_FIXTURE_CREATED_STOPPED';save()
except Exception as ex:result['status']='FAIL';result['error']=str(ex);result['traceback']=traceback.format_exc();save();print(result['traceback'],flush=True);raise
print(json.dumps(result,ensure_ascii=False,indent=2))
