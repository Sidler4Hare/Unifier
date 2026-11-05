import os,sys,json,secrets,time,threading,subprocess,traceback,signal,shutil,hashlib,uuid
from pathlib import Path
BASE=Path('/home/agent/expansion-continuation-20261103');SOURCE=BASE/'source';ROOT=BASE/'bridge-environments';SHARED=BASE/'bridge-shared';sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService
from unifier.file_routes import FileRoutes
from unifier.project_sessions import ProjectSessions
from unifier.identity import binding_for
from unifier.agent_bridge import AgentBridge,load_policy
OUT=BASE/'bridge-six-cells-v8-evidence';OUT.mkdir(exist_ok=False);PRIVATE=BASE/'bridge-policy-private';PRIVATE.mkdir(mode=0o700,exist_ok=True)
config,lock,_=load_project();rt=PodmanRuntime();svc=EnvironmentService(ROOT,rt,config,lock);routes=FileRoutes(ROOT,svc);sessions=ProjectSessions(ROOT,svc);result={'status':'RUNNING','host':'Ubuntu 24.04','cells':[],'negative':[],'origins':[]};bridges=[];origins=[];threads=[]
def save():(OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
def identity():return {'pid':os.getpid(),'uid':os.getuid(),'gid':os.getgid(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip()}
def waitfor(predicate,seconds,label):
 until=time.monotonic()+seconds
 while time.monotonic()<until:
  val=predicate()
  if val:return val
  time.sleep(.1)
 raise RuntimeError(label+' timed out')
def fail_signal(signum,frame):raise RuntimeError('bounded controller signal '+str(signum))
signal.signal(signal.SIGTERM,fail_signal)
original_show=sessions.show
def logged_show(sid):
 v=original_show(sid)
 with (OUT/'session-observations.jsonl').open('a') as f:f.write(json.dumps({'at':time.time(),'session_uuid':sid,'state':v.get('state'),'guest_state':v.get('guest_state'),'worker_live':v.get('worker_live'),'lease_live':v.get('lease_live'),'environment':v.get('environment')})+'\n')
 return v
sessions.show=logged_show
result['host_identity']=identity();result['source_sha256']={n:hashlib.sha256((SOURCE/'src/unifier'/n).read_bytes()).hexdigest() for n in ['runtime.py','exec_control.py','project_sessions.py','agent_bridge.py','bridge_client.py']};save()
TARGET='''import json,os,sys\nfrom pathlib import Path\nr={"expected_label":sys.argv[2],"pid":os.getpid(),"uid":os.getuid(),"gid":os.getgid(),"pid_namespace":os.readlink("/proc/self/ns/pid"),"os_release":Path("/etc/os-release").read_text(),"cgroup":Path("/proc/self/cgroup").read_text()}\nPath(sys.argv[1]).write_text(json.dumps(r,indent=2))\n'''
ORIGIN='''import json,os,sys,time,uuid\nfrom pathlib import Path\nbase=Path(sys.argv[1]);sys.path.insert(0,str(base/"client-code"))\nfrom unifier.bridge_client import request\nwork=base/sys.argv[2];work.mkdir(exist_ok=True)\n(work/"client-identity.json").write_text(json.dumps({"pid":os.getpid(),"uid":os.getuid(),"pid_namespace":os.readlink("/proc/self/ns/pid"),"os_release":Path("/etc/os-release").read_text()}))\nmanifest=work/"requests.json"\nwhile not manifest.exists():time.sleep(.1)\nrequests=json.loads(manifest.read_text());responses=[]\nfor item in requests:\n try:\n  answer=request(item["socket"],item["action_id"],token=("nope" if item.get("bad_token") else os.environ["UNIFIER_AGENT_TOKEN"]),request_uuid=item["request_uuid"],timeout=40)\n  responses.append({"request":item,"response":answer})\n except Exception as ex:responses.append({"request":item,"transport_error":str(ex)})\n (work/"responses.tmp").write_text(json.dumps(responses,indent=2));(work/"responses.tmp").replace(work/"responses.json")\nwhile not (work/"stop").exists():time.sleep(.1)\n'''
try:
 assert int(next(x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable')).split()[1])*1024>3*1024**3
 shutil.copytree(SOURCE/'src/unifier',SHARED/'client-code/unifier',dirs_exist_ok=True,ignore=shutil.ignore_patterns('__pycache__'))
 (SHARED/'target.py').write_text(TARGET);(SHARED/'origin.py').write_text(ORIGIN)
 projects={};projectdirs={}
 for eid in ['fedora44','arch']:
  record=svc.store.load(eid);cid=record['runtime']['container_id'];info=json.loads(subprocess.check_output(['podman','inspect',cid],text=True))[0];c=info['HostConfig'];assert c['Memory']==384*1024**2 and c['CgroupParent']=='unifier-bridge-tests.slice';assert not info['State']['Running']
  projectdirs[eid]=SHARED/('project-'+eid);projects[eid]=routes.show_project('bridge-'+eid)
 sessions.recover()
 for origin_id in ['fedora44','arch']:
  print('origin '+origin_id,flush=True);work=SHARED/('origin-v8-'+origin_id);work.mkdir(mode=0o700)
  token=secrets.token_hex(32);os.environ['UNIFIER_AGENT_TOKEN']=token
  rec=sessions.create_session(projects[origin_id]['project_uuid'],argv=['/usr/bin/python3',str(SHARED/'origin.py'),str(SHARED),work.name],cwd=str(projectdirs[origin_id]),env_keys=['UNIFIER_AGENT_TOKEN'])
  origins.append(rec['session_uuid']);outcome={}
  def execute(sid=rec['session_uuid'],outcome=outcome):
   try:outcome['returncode']=sessions.execute(sid)
   except BaseException as ex:outcome['error']=str(ex)
  thread=threading.Thread(target=execute,daemon=True);thread.start();threads.append(thread)
  view=waitfor(lambda:(v if (v:=sessions.show(rec['session_uuid'])).get('guest_live') and v.get('lease_live') else None),40,'live origin')
  proxy_argv=Path('/proc/'+str(view['child_pid'])+'/cmdline').read_bytes();assert token.encode() not in proxy_argv;assert b'UNIFIER_AGENT_TOKEN\x00' in proxy_argv;result.setdefault('credential_transport',[]).append({'origin':origin_id,'token_value_in_host_proxy_argv':False,'environment_key_reference_in_argv':True});binding=view['environment_binding'];longdir=SHARED/('private-bridge-v8-'+origin_id)/('桥接口-'+('segment-'*10));longdir.mkdir(mode=0o700,parents=True);os.chmod(longdir,0o700);sock=longdir/'bridge.sock';assert len(str(sock).encode())>107
  actions=[{'action_id':'host-info','type':'host-info'}]
  for target in ['fedora44','arch']:
   argv=['/usr/bin/python3',str(SHARED/'target.py'),str(SHARED/(origin_id+'-to-'+target+'.json')),target]
   plan=sessions.prepare(projects[target]['project_uuid'],argv=argv,cwd=str(projectdirs[target]))
   actions.append({'action_id':'run-'+target,'type':'project-run','project_uuid':projects[target]['project_uuid'],'environment_binding':binding_for(svc.store.load(target)),'argv':argv,'cwd':str(projectdirs[target]),'timeout_seconds':10,'scope_sha256':plan['scope_sha256']})
  revoke=PRIVATE/(origin_id+'.revoke');policy={'schema_version':1,'token':token,'socket':str(sock),'expires_at':time.time()+300,'origin':{'session_uuid':rec['session_uuid'],'environment_binding':binding},'actions':actions,'revoke_file':str(revoke)}
  path=PRIVATE/(origin_id+'.json');path.write_text(json.dumps(policy));path.chmod(0o600);bridge=AgentBridge(load_policy(path),sessions);bridge.bind();bridges.append(bridge);original_status=bridge.origin_status
  def diagnose(original=original_status,origin_sid=rec['session_uuid'],eid=origin_id):
   try:return original()
   except Exception:
    (OUT/('origin-status-failure-'+eid+'.json')).write_text(json.dumps(sessions.show(origin_sid),indent=2));raise
  bridge.origin_status=diagnose
  server=threading.Thread(target=bridge.serve_forever,daemon=True);server.start()
  requests=[{'socket':str(sock),'action_id':x,'request_uuid':str(uuid.uuid4())} for x in ['host-info','run-'+origin_id,'run-'+('arch' if origin_id=='fedora44' else 'fedora44')]]
  requests.append(dict(requests[-1]));requests.append({'socket':str(sock),'action_id':'unknown-action','request_uuid':str(uuid.uuid4())})
  requests.append({'socket':str(sock),'action_id':'host-info','request_uuid':str(uuid.uuid4()),'bad_token':True})
  (OUT/('origin-before-'+origin_id+'.json')).write_text(json.dumps(sessions.show(rec['session_uuid']),indent=2));(work/'requests.json').write_text(json.dumps(requests));responses=waitfor(lambda:(json.loads((work/'responses.json').read_text()) if (work/'responses.json').exists() and len(json.loads((work/'responses.json').read_text()))==len(requests) else None),100,'guest responses')
  assert token not in json.dumps(responses)
  client_identity=json.loads((work/'client-identity.json').read_text());assert client_identity['pid_namespace']!=result['host_identity']['pid_namespace'];assert ('ID=fedora' if origin_id=='fedora44' else 'ID=arch') in client_identity['os_release']
  for item in responses[:3]:
   response=item.get('response');assert response and response['status']=='ok',item
   action=item['request']['action_id'];cell={'origin':origin_id,'action_id':action,'status':'PASS','response':response,'client_identity':client_identity,'long_socket_path_bytes':len(str(sock).encode())}
   if action=='host-info':assert response['receipt']['host_info']['identity']['pid']==os.getpid()
   else:
    target=action.removeprefix('run-');proof=json.loads((SHARED/(origin_id+'-to-'+target+'.json')).read_text());assert ('ID=fedora' if target=='fedora44' else 'ID=arch') in proof['os_release'];assert proof['pid_namespace']!=result['host_identity']['pid_namespace'];i=rt.inspect(svc.store.load(target)['runtime']['container_id']);assert i.container_id==response['receipt']['target']['environment_binding']['container_id'];assert (proof['pid_namespace']==client_identity['pid_namespace'])==(target==origin_id);assert (SHARED/(origin_id+'-to-'+target+'.json')).stat().st_uid==os.getuid();assert response['receipt']['session']['exit_code']==0;cell['target_proof']=proof
   result['cells'].append(cell);save()
  for item,code in zip(responses[3:],['duplicate-request','unknown-action','bad-token']):
   response=item['response'];assert response['status']=='rejected' and response['error']['code']==code;result['negative'].append({'origin':origin_id,'status':'PASS_EXPECTED_REFUSAL','response':response})
  revoke.write_text(hashlib.sha256(token.encode()).hexdigest()+'\n')
  denied=bridge.respond({'protocol_version':1,'action_id':'host-info','request_uuid':str(uuid.uuid4()),'token':token});assert denied['error']['code']=='token-revoked';result['negative'].append({'origin':origin_id,'status':'PASS_EXPECTED_REVOKED','response':denied})
  revoke.write_text('0'*65537+'\n'+hashlib.sha256(token.encode()).hexdigest()+'\n');oversized=bridge.respond({'protocol_version':1,'action_id':'host-info','request_uuid':str(uuid.uuid4()),'token':token});assert oversized['error']['code']=='revoke-state';result['negative'].append({'origin':origin_id,'status':'PASS_EXPECTED_OVERSIZED_REVOKE_REFUSAL','response':oversized})
  ledger=json.dumps(bridge.ledger.records());assert token not in ledger
  (work/'stop').touch();thread.join(15);assert not thread.is_alive(),outcome;assert outcome.get('returncode')==0,outcome
  result['origins'].append({'origin':origin_id,'session':sessions.show(rec['session_uuid']),'returncode':outcome,'broker_unresolved_runs':bridge.unresolved_runs()});bridge.stop();bridge.close();server.join(2);save()
 result['status']='PASS_REAL_GUEST_ORIGIN_SIX_CELLS'
except Exception as ex:result.update(status='FAIL',error=str(ex),traceback=traceback.format_exc());print(result['traceback'],flush=True)
finally:
 os.environ.pop('UNIFIER_AGENT_TOKEN',None)
 for bridge in bridges:
  try:bridge.stop();bridge.close()
  except Exception as ex:result.setdefault('cleanup_errors',[]).append(str(ex))
 for sid in origins:
  try:
   v=sessions.show(sid)
   if v.get('guest_live'):sessions.cancel(sid)
  except Exception as ex:result.setdefault('cleanup_errors',[]).append(str(ex))
 for thread in threads:thread.join(10)
 result['worker_threads_live']=sum(t.is_alive() for t in threads)
 for eid in ['fedora44','arch']:
  try:r=svc.store.load(eid);rt.stop(r['runtime']['container_id'],project=str(ROOT),env_id=eid)
  except Exception as ex:result.setdefault('cleanup_errors',[]).append(str(ex))
 result['containers_after']={eid:svc.inspect(eid)['runtime_state'] for eid in ['fedora44','arch']};save()
print(json.dumps({'status':result['status'],'cells':len(result['cells']),'negative':len(result['negative']),'error':result.get('error'),'threads_live':result.get('worker_threads_live')},indent=2))
