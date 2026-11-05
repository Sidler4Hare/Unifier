import sys,json,subprocess,traceback,hashlib,time
from pathlib import Path
ROOT=Path(__file__).resolve().parent/'source'
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime, creation_argv
class BoundedRemoteRuntime(PodmanRuntime):
 def create(self,spec):
  assert spec.project==str(BASE/"environment-v3") and not spec.session_mounts and not spec.devices
  self._ensure_rootless()
  assert self.inspect(spec.name) is None
  argv=creation_argv(spec)
  argv[2:2]=["--memory=768m","--memory-swap=896m","--cpus=1","--pids-limit=192","--ipc=private","--ulimit=core=0:0"]
  result=self.runner(argv,capture=True,timeout=60)
  if result.returncode: raise RuntimeError(result.stderr[-2000:])
  cid=result.stdout.strip()
  info=json.loads(subprocess.check_output(["podman","inspect",cid],text=True))[0]
  c=info["HostConfig"]
  assert c["Memory"]==768*1024**2 and c["MemorySwap"]==896*1024**2 and c["PidsLimit"]==192 and c["NanoCpus"]==1000000000
  save("resource-limits",info)
  return cid
from unifier.service import EnvironmentService
from unifier.compatibility import observe_host
BASE=ROOT.parent; out=BASE/'evidence-v3';out.mkdir(exist_ok=False)
def save(name,data): (out/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2))
def snap():
 ids=subprocess.check_output(['podman','ps','-aq'],text=True).split()
 return json.loads(subprocess.check_output(['podman','inspect',*ids],text=True)) if ids else []
before=snap();save('containers-before',before)
config,lock,_=load_project();service=EnvironmentService(BASE/'environment-v3',BoundedRemoteRuntime(),config,lock)
result={'host':observe_host(),'steps':[],'status':'RUNNING','retry_of':'evidence/result.json','original_failed_environment_preserved':True}; record=None
save('result',result)
def txn(label,func):
 print(label,flush=True);t=func();save(label,t)
 result['steps'].append({'label':label,'phase':t.get('phase'),'failure_phase':t.get('failure_phase'),'verification':t.get('verification'),'dependency_repair':t.get('dependency_repair')});save('result',result)
 if t.get('phase')!='completed': raise RuntimeError(label+' failed: '+str(t.get('failure_phase')))
 return t
try:
 record=service.create('fedora44','fedora44',[],'pasta');save('environment',record)
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
 result['containers_after']=snap();result['preexisting_containers_unchanged']=all(any(x['Id']==y['Id'] and x['State']==y['State'] and x['Mounts']==y['Mounts'] for x in result['containers_after']) for y in before)
 save('result',result)
