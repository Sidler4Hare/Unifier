import os,sys,json,subprocess,threading,time,signal,traceback,sqlite3,hashlib
from pathlib import Path
BASE=Path('/home/agent/expansion-continuation-20261103');SOURCE=BASE/'source';ROOT=BASE/'bridge-environments';SHARED=BASE/'bridge-shared';OUT=BASE/'guarded-soak-evidence';OUT.mkdir(exist_ok=False);WORK=SHARED/'guarded-soak-v1';WORK.mkdir(mode=0o700)
sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService
from unifier.file_routes import FileRoutes
from unifier.project_sessions import ProjectSessions
config,lock,_=load_project();rt=PodmanRuntime();svc=EnvironmentService(ROOT,rt,config,lock);routes=FileRoutes(ROOT,svc);sessions=ProjectSessions(ROOT,svc)
result={'status':'RUNNING','duration_requested_seconds':1800,'rounds_per_guest':120,'interval_seconds':15,'starts':{},'outcomes':{},'host_available_min_bytes':None};threads=[];records={};stop_monitor=threading.Event();aborted=threading.Event();cids={}
def save():
 p=OUT/'result.tmp';p.write_text(json.dumps(result,ensure_ascii=False,indent=2));p.replace(OUT/'result.json')
def available():return int(next(s for s in Path('/proc/meminfo').read_text().splitlines() if s.startswith('MemAvailable:')).split()[1])*1024
def stopped_exact():
 for eid,cid in cids.items():
  r=svc.store.load(eid);assert r['runtime']['container_id']==cid;rt.stop(cid,project=str(ROOT),env_id=eid)
def watch():
 low=0
 while not stop_monitor.wait(1):
  try:
   memory=available();result['host_available_min_bytes']=min(result['host_available_min_bytes'] or memory,memory);low=low+1 if memory<2*1024**3 else 0
   if low<3:continue
   reason='low host headroom'
  except Exception as ex:reason='monitor failed: '+str(ex)
  result['abort_reason']=reason;aborted.set()
  try:stopped_exact()
  except Exception as ex:result['abort_stop_error']=str(ex)
  save();return

guest_source=r'''import os,sys,time,json,sqlite3,hashlib
from pathlib import Path
root=Path(sys.argv[1]);target=sys.argv[2];start=time.monotonic()
def atomic(p,r):
 t=p.with_suffix('.tmp');t.write_text(json.dumps(r,indent=2));t.replace(p)
atomic(root/(target+'-identity.json'),{'pid':os.getpid(),'uid':os.getuid(),'pid_namespace':os.readlink('/proc/self/ns/pid'),'os_release':Path('/etc/os-release').read_text()})
local=sqlite3.connect(root/(target+'.db'),timeout=10);local.execute('pragma journal_mode=WAL');local.execute('create table saves(n integer primary key, payload text not null)');local.commit()
shared=sqlite3.connect(root/'coordination.db',timeout=10);shared.execute('pragma journal_mode=WAL');shared.execute('create table if not exists saves(origin text, n integer, payload text not null, primary key(origin,n))');shared.commit()
for n in range(1,121):
 deadline=start+(n-1)*15
 while time.monotonic()<deadline:time.sleep(min(.5,deadline-time.monotonic()))
 payload=hashlib.sha256((target+':'+str(n)).encode()).hexdigest()
 with local:local.execute('insert into saves values (?,?)',(n,payload))
 with shared:shared.execute('insert into saves values (?,?,?)',(target,n,payload))
 atomic(root/(target+'-progress.json'),{'round':n,'elapsed_seconds':time.monotonic()-start,'payload':payload})
 if n%20==0:local.execute('pragma wal_checkpoint(TRUNCATE)')
while time.monotonic()-start<1800:time.sleep(.2)
check=local.execute('pragma integrity_check').fetchall();local.close();shared.close()
atomic(root/(target+'-done.json'),{'rounds':120,'elapsed_seconds':time.monotonic()-start,'integrity':check})
'''
result['host_before']={'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'uid':os.getuid()};result['source_sha256']={n:hashlib.sha256((SOURCE/'src/unifier'/n).read_bytes()).hexdigest() for n in ['runtime.py','exec_control.py','project_sessions.py','agent_bridge.py','bridge_client.py']};save()
def interrupt(signum,frame):raise RuntimeError('bounded controller interrupted '+str(signum))
signal.signal(signal.SIGTERM,interrupt);signal.signal(signal.SIGINT,interrupt)
try:
 assert available()>3*1024**3
 (WORK/'soak-guest.py').write_text(guest_source)
 for eid in ['fedora44','arch']:
  env=svc.store.load(eid);cid=env['runtime']['container_id'];info=json.loads(subprocess.check_output(['podman','inspect',cid],text=True))[0];h=info['HostConfig'];assert not info['State']['Running'] and h['Memory']==384*1024**2 and h['CgroupParent']=='unifier-bridge-tests.slice';cids[eid]=cid
 monitor=threading.Thread(target=watch,daemon=True);monitor.start()
 start=time.monotonic()
 for eid in ['fedora44','arch']:
  project=routes.show_project('bridge-'+eid);rec=sessions.create_session(project['project_uuid'],argv=['/usr/bin/python3',str(WORK/'soak-guest.py'),str(WORK),eid],cwd=project['default_save_root']);records[eid]=rec['session_uuid'];result['starts'][eid]=time.time()
  def execute(eid=eid,sid=rec['session_uuid']):
   try:result['outcomes'][eid]={'returncode':sessions.execute(sid)}
   except BaseException as ex:result['outcomes'][eid]={'error':str(ex)}
  thread=threading.Thread(target=execute,daemon=True);thread.start();threads.append(thread)
 while any(t.is_alive() for t in threads):
  if aborted.is_set():raise RuntimeError(result.get('abort_reason'))
  if time.monotonic()-start>1920:raise RuntimeError('soak deadline exceeded')
  result['elapsed_seconds']=time.monotonic()-start;result['progress']={}
  for eid in ['fedora44','arch']:
   p=WORK/(eid+'-progress.json')
   if p.exists():result['progress'][eid]=json.loads(p.read_text())
  save();time.sleep(2)
 assert time.monotonic()-start>=1800
 for eid in ['fedora44','arch']:
  assert result['outcomes'][eid].get('returncode')==0,result['outcomes'];done=json.loads((WORK/(eid+'-done.json')).read_text());assert done['rounds']==120 and done['elapsed_seconds']>=1800 and done['integrity']==[['ok']];result[eid+'-done']=done
  result[eid+'-session']=sessions.show(records[eid]);assert result[eid+'-session']['guest_state']=='gone' and result[eid+'-session']['lease_token'] is None
 result['database_checks']={}
 for name in ['fedora44.db','arch.db','coordination.db']:
  db=WORK/name;con=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True);integrity=con.execute('pragma integrity_check').fetchall();rows=con.execute('select * from saves').fetchall();con.close();assert integrity==[('ok',)];assert len(rows)==(240 if name=='coordination.db' else 120);result['database_checks'][name]={'integrity':integrity,'rows':len(rows),'sha256':hashlib.sha256(db.read_bytes()).hexdigest(),'uid':db.stat().st_uid};assert db.stat().st_uid==os.getuid()
 result['status']='PASS_BOUNDED_30_MIN_HEADLESS_SAVES'
except BaseException as ex:result.update(status='ABORTED' if aborted.is_set() else 'FAIL',error=str(ex),traceback=traceback.format_exc());print(result['traceback'],flush=True)
finally:
 stop_monitor.set()
 for sid in records.values():
  try:
   view=sessions.show(sid)
   if view.get('guest_live'):sessions.cancel(sid)
  except Exception as ex:result.setdefault('cleanup_errors',[]).append(str(ex))
 try:stopped_exact()
 except Exception as ex:result.setdefault('cleanup_errors',[]).append(str(ex))
 for t in threads:t.join(10)
 result['worker_threads_live']=sum(t.is_alive() for t in threads);result['host_boot_unchanged']=Path('/proc/sys/kernel/random/boot_id').read_text().strip()==result['host_before']['boot_id'];result['containers_after']={e:svc.inspect(e)['runtime_state'] for e in cids};save()
print(json.dumps({'status':result['status'],'elapsed_seconds':result.get('elapsed_seconds'),'error':result.get('error'),'worker_threads_live':result['worker_threads_live']},indent=2))
