import sys,os,json,subprocess,traceback,time,hashlib,pty,select,fcntl,termios,struct,signal,re
from pathlib import Path
BASE=Path('/home/agent/expansion-continuation-20261103');SOURCE=BASE/'source';ROOT=BASE/'cli-environment-v1';SHARE=BASE/'cli-shared-v1';OUT=BASE/'fedora-cli-evidence';OUT.mkdir(exist_ok=False);SHARE.mkdir(mode=0o700)
sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.runtime import PodmanRuntime,creation_argv
from unifier.project import load_project
from unifier.service import EnvironmentService
from unifier.file_routes import FileRoutes
from unifier.project_sessions import ProjectSessions
class Bounded(PodmanRuntime):
 def create(self,spec):
  assert spec.project==str(ROOT) and not spec.session_mounts and not spec.devices
  self._ensure_rootless();assert self.inspect(spec.name) is None;argv=creation_argv(spec);argv[2:2]=['--memory=512m','--memory-swap=640m','--pids-limit=128','--cpus=1','--ipc=private','--ulimit=core=0:0','--cgroup-parent=unifier-bridge-tests.slice'];r=self.runner(argv,capture=True,timeout=60)
  if r.returncode:raise RuntimeError(r.stderr[-2000:])
  return r.stdout.strip()
config,lock,_=load_project();rt=Bounded();svc=EnvironmentService(ROOT,rt,config,lock);result={'status':'RUNNING','commands':[],'scope':'Fedora guest on Ubuntu; all test workloads share the bounded 1 GiB/1 CPU remote slice'};cid=None;process=None;master=None;slave=None
def save():(OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
def cmd(argv,want=0):
 r=rt.exec(cid,argv,project=str(ROOT),env_id='fedora44',workdir=str(SHARE),env={'LANG':'C.UTF-8'},capture=True,timeout=30);row={'argv':argv,'returncode':r.returncode,'stdout':r.stdout[:6000],'stderr':r.stderr[:1500]};result['commands'].append(row);save();assert r.returncode==want,row;return r.stdout
try:
 assert int(next(x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable')).split()[1])>3*1024**2
 rec=svc.create('fedora44','fedora44',[str(SHARE)],'pasta',share_label_policy='shared-relabel');cid=rec['runtime']['container_id'];info=json.loads(subprocess.check_output(['podman','inspect',cid],text=True))[0];h=info['HostConfig'];assert h['Memory']==512*1024**2 and h['MemorySwap']==640*1024**2 and h['CgroupParent']=='unifier-bridge-tests.slice';(OUT/'environment.json').write_text(json.dumps(rec,indent=2));(OUT/'resource-limits.json').write_text(json.dumps(info,indent=2));svc.start('fedora44')
 t=svc.install_packages('fedora44',['jq','ripgrep','fd-find','bat','htop','sqlite'],use_argv=['jq','--version']);(OUT/'install.json').write_text(json.dumps(t,indent=2));assert t['phase']=='completed'
 text=SHARE/'中文 literal $; text.txt';text.write_text('alpha\n中文 literal $;\nomega\n');j=SHARE/'input.json';j.write_text(json.dumps({'中文':'$;','n':100},ensure_ascii=False))
 for argv in [['jq','--version'],['rg','--version'],['fd','--version'],['bat','--version'],['sqlite3','--version'],['htop','--version']]:cmd(argv)
 assert cmd(['jq','.n + 1',str(j)]).strip()=='101';cmd(['jq','-e','.missing',str(j)],1)
 assert '中文 literal $;' in cmd(['rg','--line-number','--fixed-strings','$;',str(text)]);cmd(['rg','absent-pattern',str(text)],1);cmd(['rg','[',str(text)],2)
 assert str(text) in cmd(['fd','--fixed-strings','--type','f','literal',str(SHARE)]);assert cmd(['bat','--plain','--paging=never',str(text)])==text.read_text()
 db=SHARE/'中文 空格 sqlite.db';assert cmd(['sqlite3',str(db),"create table t(n integer,s text); insert into t values(200,'中文 $;'); select n,s from t;"]).strip()=='200|中文 $;'
 svc.stop('fedora44');svc.start('fedora44');assert cmd(['sqlite3',str(db),'pragma integrity_check; select n,s from t;']).strip()=='ok\n200|中文 $;';svc.stop('fedora44')
 routes=FileRoutes(ROOT,svc);root=routes.roots.add(str(SHARE));project=routes.add_project(root['root_uuid'],'fedora44',alias='fedora-cli',default_save_root=str(SHARE),intent_handlers={})
 master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',31,103,0,0));env=dict(os.environ,UNIFIER_HOME=str(ROOT),TERM='xterm-256color',PYTHONPATH=str(SOURCE/'src'))
 process=subprocess.Popen([str(SOURCE/'bin/unifier'),'project','run','fedora-cli','--tty','--','htop'],cwd=SOURCE,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave);slave=None;buf=bytearray();deadline=time.monotonic()+40
 while time.monotonic()<deadline and process.poll() is None:
  ready,_,_=select.select([master],[],[],.2)
  if ready:
   try:buf.extend(os.read(master,65536))
   except OSError:break
  clean=re.sub(rb'\x1b\[[0-?]*[ -/]*[@-~]',b'',bytes(buf))
  if b'Tasks:' in clean and (b'Mem' in clean or b'Swp' in clean):break
 assert process.poll() is None and b'Tasks:' in clean,'htop did not render interactive terminal'
 fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',37,111,0,0));os.kill(process.pid,signal.SIGWINCH);time.sleep(.5);os.write(master,b'q')
 deadline=time.monotonic()+15
 while time.monotonic()<deadline and process.poll() is None:
  ready,_,_=select.select([master],[],[],.2)
  if ready:
   try:buf.extend(os.read(master,65536))
   except OSError:break
 code=process.wait(timeout=10);assert code==0,code;(OUT/'htop-pty.raw').write_bytes(buf)
 manager=ProjectSessions(ROOT,svc);views=[manager.show(r['session_uuid']) for r in manager.store.list()];assert len(views)==1 and views[0]['guest_state']=='gone' and views[0]['lease_token'] is None;(OUT/'htop-session.json').write_text(json.dumps(views[0],indent=2));result['htop_pty']={'status':'PASS_RENDER_RESIZE_NORMAL_Q_EXIT','sizes':[[31,103],[37,111]],'returncode':code};result['status']='PASS_FEDORA_SIX_OSS_CLI_AND_HTOP_PTY';result['shared_db_uid']=db.stat().st_uid;assert db.stat().st_uid==os.getuid()
except Exception as ex:result.update(status='FAIL',error=str(ex),traceback=traceback.format_exc());print(result['traceback'],flush=True)
finally:
 if process is not None and process.poll() is None:
  try:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=10)
  except Exception:pass
 for fd in [master,slave]:
  if fd is not None:
   try:os.close(fd)
   except OSError:pass
 if cid:
  try:rt.stop(cid,project=str(ROOT),env_id='fedora44');result['container_stopped']=not rt.inspect(cid).running
  except Exception as ex:result['stop_error']=str(ex)
 save()
print(json.dumps({'status':result['status'],'commands':len(result['commands']),'error':result.get('error')},indent=2))
