import sys,json,subprocess,traceback,hashlib,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent/'source'
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService
from unifier.compatibility import observe_host
BASE=ROOT.parent; out=BASE/'evidence-v2';out.mkdir(exist_ok=False)
def save(name,data): (out/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))
def snap():
 ids=subprocess.check_output(['podman','ps','-aq'],text=True).split()
 return json.loads(subprocess.check_output(['podman','inspect',*ids],text=True)) if ids else []
before=snap();save('containers-before',before)
config,lock,_=load_project();service=EnvironmentService(BASE/'environment',PodmanRuntime(),config,lock)
result={'host':observe_host(),'steps':[],'status':'RUNNING'}; record=None
save('result',result)
def txn(label,func):
 print(label,flush=True);t=func();save(label,t)
 result['steps'].append({'label':label,'phase':t.get('phase'),'failure_phase':t.get('failure_phase'),'verification':t.get('verification'),'dependency_repair':t.get('dependency_repair')});save('result',result)
 if t.get('phase')!='completed': raise RuntimeError(label+' failed: '+str(t.get('failure_phase')))
 return t
try:
 record=service.store.load('fedora44')
 assert record['runtime']['container_id']=='8bfbd679904a5a531fb961fbef60cdc79671da2dbb6f9f347f842564519590ff'
 save('environment',record)
 recovered=service.recover_packages('fedora44');save('recovery-before-retry',recovered)
 service.start('fedora44')
 txn('dnf-jq-install',lambda:service.install_packages('fedora44',['jq'],use_argv=['jq','--version']))
 txn('dnf-alsa-repair',lambda:service.repair_dependencies('fedora44',[],libraries=['libasound.so.2']))
 query=service.query_package('fedora44','alsa-lib');save('alsa-query',query)
 txn('dnf-jq-remove',lambda:service.remove_packages('fedora44',['jq'],use_argv=['jq','--version']))
 txn('dnf-jq-reinstall',lambda:service.install_packages('fedora44',['jq'],use_argv=['jq','--version']))
 result['status']='PASS_NATIVE_DNF_AND_REPAIR'
except Exception as exc:
 result['status']='FAIL';result['error']=str(exc);result['traceback']=traceback.format_exc();print(result['traceback'],flush=True)
finally:
 if record:
  try: service.stop('fedora44');result['stopped']=True
  except Exception as exc: result['stop_error']=str(exc)
 result['containers_after']=snap();result['baseline_unchanged']=all(any(x['Id']==y['Id'] and x['State']==y['State'] and x['Mounts']==y['Mounts'] for x in result['containers_after']) for y in before)
 save('result',result)
