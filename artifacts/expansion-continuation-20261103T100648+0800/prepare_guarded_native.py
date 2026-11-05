import sys,json,traceback,os
from pathlib import Path
REPO=Path('/home/omg/文档/多兼容linux机');sys.path[:0]=[str(REPO/'src'),str(REPO)]
from unifier.project import load_project
from unifier.service import EnvironmentService
from tools.guarded_test import GuardedPodmanRuntime,HostReserveMonitor
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text())/'guarded-native-prepare';OUT.mkdir(exist_ok=True)
ROOT=REPO/'.lab/guarded-expansion-20261103/environment';config,_,_=load_project();lock=json.loads((CTL/'guarded-images.lock.json').read_text());rt=GuardedPodmanRuntime(ROOT);svc=EnvironmentService(ROOT,rt,config,lock)
r=svc.store.load('debian13');cid=r['runtime']['container_id'];result={'status':'RUNNING','container_id':cid,'host_desktop_exposed':False,'guarded_test_root':str(ROOT)}
def save(): (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
save()
try:
 info=rt.verify_limits(cid,expected_id=cid)
 assert info['HostConfig']['IpcMode']=='private'
 assert all('/run/user/' not in m.get('Source','') and '/tmp/.X11-unix' not in m.get('Source','') for m in info['Mounts'])
 with HostReserveMonitor(rt,cid,project=str(ROOT),env_id='debian13',receipt=OUT/'reserve-abort.json') as monitor:
  svc.start('debian13')
  execution=rt.start_session_exec(cid,['/bin/true'],project=str(ROOT),env_id='debian13',workdir=None,env={},user='1000:1000',interactive=False)
  result['control_handshake']={'guest_pid':execution.guest.pid,'guest_start':execution.guest.start,'exit_code':execution.wait(timeout=5),'cleanup':execution.cleanup()};save()
  assert result['control_handshake']['exit_code']==0 and result['control_handshake']['cleanup']=='gone'
  txn=svc.install_packages('debian13',['xvfb','xauth','x11-apps','fonts-noto-cjk'])
  (OUT/'transaction.json').write_text(json.dumps(txn,ensure_ascii=False,indent=2));result['package_phase']=txn.get('phase');result['package_verification']=txn.get('verification');result['reserve_monitor_aborted']=monitor.aborted
  if txn.get('phase')!='completed' or monitor.aborted:raise RuntimeError('bounded package preparation did not complete')
  result['status']='PASS_GUARDED_NATIVE_HANDSHAKE_AND_PRIVATE_DISPLAY_DEPENDENCIES'
except Exception as ex:result['status']='FAIL';result['error']=str(ex);result['traceback']=traceback.format_exc();print(result['traceback'],flush=True)
finally:
 try:svc.stop('debian13');result['stopped']=True
 except Exception as ex:result['stopped']=False;result['stop_error']=str(ex)
 save()
print(json.dumps(result,ensure_ascii=False,indent=2))
