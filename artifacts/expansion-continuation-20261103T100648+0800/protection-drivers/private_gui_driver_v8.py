"""Bounded private X11 controller; every GUI input stays inside its own guest."""
import sys,os,json,subprocess,time,traceback,signal,shutil,secrets
from pathlib import Path
REPO=Path('/home/omg/文档/多兼容linux机');sys.path[:0]=[str(REPO/'src'),str(REPO)]
from unifier.project import load_project
from unifier.service import EnvironmentService
from tools.guarded_test import GuardedPodmanRuntime,HostReserveMonitor
CTL=Path(__file__).resolve().parent
OUT=Path((CTL/'evidence-path').read_text().strip())/'private-gui-workflows-v8';OUT.mkdir(exist_ok=True)
QUEUE=CTL/'gui-commands-v8';QUEUE.mkdir(mode=0o700,exist_ok=True)
ROOT=REPO/'.lab/guarded-expansion-20261103/environment';SHARED=ROOT.parent/'shared'
config,_,_=load_project();lock=json.loads((CTL/'guarded-images.lock.json').read_text());rt=GuardedPodmanRuntime(ROOT);svc=EnvironmentService(ROOT,rt,config,lock)
cid=svc.store.load('debian13')['runtime']['container_id']
assert cid=='e58754f0645ae1ff2924cb09a7264cc821e44893e7589a7fd0ebca09ce5f5e45'
GUEST_ENV={'DISPLAY':':97','XAUTHORITY':'/tmp/unifier-private-display/Xauthority','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
launchers=[];result={'status':'STARTING','container_id':cid,'host_display_sockets_mounted':False,'events':[]}
def save(): (OUT/'controller.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
def identity():return {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kwin_pids':subprocess.check_output(['pgrep','-u',str(os.getuid()),'-x','kwin_wayland'],text=True).split()}
def argv_guest(argv):
 cmd=['podman','exec','--user','1000:1000']
 for k,v in GUEST_ENV.items():cmd+=['--env',k+'='+v]
 return cmd+[cid,'/usr/bin/prlimit','--core=0:0','--',*argv]
def guest(argv,timeout=25):return subprocess.run(argv_guest(argv),capture_output=True,text=True,timeout=timeout)
def start_display():
 script="""import os,subprocess,secrets,json,resource
from pathlib import Path
resource.setrlimit(resource.RLIMIT_CORE,(0,0))
p=Path('/tmp/unifier-private-display');p.mkdir(mode=0o700,exist_ok=True)
for proc in Path('/proc').iterdir():
 if not proc.name.isdigit():continue
 try:
  if os.readlink(proc/'exe').endswith('/Xvfb'):raise RuntimeError('display server already running')
 except (FileNotFoundError,PermissionError):pass
for stale in [Path('/tmp/.X97-lock'),Path('/tmp/.X11-unix/X97')]:
 if stale.exists():
  assert stale.lstat().st_uid==os.getuid();stale.unlink()
backup=p/('stale-ipc-'+secrets.token_hex(4));backup.mkdir(mode=0o700)
for stale in Path('/tmp').iterdir():
 if 'wps' in stale.name.lower() and (stale.name.startswith('__#_ACCOUNTSDK') or stale.name.startswith('qipc_')):
  assert stale.lstat().st_uid==os.getuid();stale.rename(backup/stale.name)
auth=p/'Xauthority';auth.touch(mode=0o600,exist_ok=True);os.chmod(auth,0o600)
subprocess.run(['xauth','-f',str(auth),'add',':97','.',secrets.token_hex(16)],check=True)
log=(p/'xvfb.log').open('ab');proc=subprocess.Popen(['Xvfb',':97','-screen','0','1280x800x24','-nolisten','tcp','-auth',str(auth)],stdout=log,stderr=log,start_new_session=True)
print(json.dumps({'pid':proc.pid,'display':':97'}))
"""
 r=guest(['python3','-c',script]);assert r.returncode==0,r.stderr
 for _ in range(30):
  ready=guest(['xdotool','getdisplaygeometry'])
  if ready.returncode==0:return json.loads(r.stdout)
  time.sleep(.2)
 raise RuntimeError('private display did not become ready')
def app_launch(component,file):
 executables={'writer':'/usr/bin/wps','sheet':'/usr/bin/et','slides':'/usr/bin/wpp','compare':'/usr/bin/bcompare'}
 exe=executables[component];name='private-wrapper-'+component;home='/home/multilinux-1000'
 if not svc.store.launch_path(name).exists():
  svc.add_app('debian13',name,'/usr/bin/prlimit',['/usr/bin/prlimit','--core=0:0','--','/usr/bin/env','HOME='+home,'XDG_CONFIG_HOME='+home+'/.config',exe],as_host_user=True,on_demand=True,desktop_required=False,workdir=str(SHARED),env_overlay={**GUEST_ENV,'QT_QPA_PLATFORM':'xcb','LIBGL_ALWAYS_SOFTWARE':'1'})
 files=file if isinstance(file,list) else [file]
 paths=[SHARED/f for f in files]
 if not paths or len(paths)>2 or any(not path.is_relative_to(SHARED) or '..' in Path(f).parts or path.resolve()!=path for f,path in zip(files,paths)):raise RuntimeError('file outside own share')
 log=(CTL/('private-wrapper-'+component+'-'+str(len(launchers))+'.log')).open('w')
 state=rt.verify_limits(cid,expected_id=cid)
 if not state['State']['Running']:raise RuntimeError('fixture stopped; refusing an implicit app start')
 proc=subprocess.Popen([sys.executable,str(CTL/'guarded_app_entry.py'),'app','open',name,'--',*[str(path) for path in paths]],env=dict(os.environ,UNIFIER_HOME=str(ROOT)),stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 launchers.append(proc);return {'host_launcher_pid':proc.pid,'app_id':name,'files':[str(path) for path in paths]}
def capture(name):
 if not name.replace('-','').replace('_','').isalnum():raise RuntimeError('bad capture name')
 xwd=CTL/(name+'.xwd');png=CTL/(name+'.png')
 with xwd.open('wb') as output:subprocess.run(argv_guest(['xwd','-silent','-root','-screen','-out','/dev/stdout']),stdout=output,stderr=subprocess.PIPE,check=True,timeout=15)
 subprocess.run(['magick',str(xwd),str(png)],check=True,capture_output=True,timeout=20)
 return {'private_screenshot':str(png)}
def interrupted(signum,frame):raise RuntimeError('controller stopped by signal '+str(signum))
signal.signal(signal.SIGTERM,interrupted);signal.signal(signal.SIGINT,interrupted)
result['host_before']=identity();save()
try:
 for filename,new in [('中文 文档.docx','writer-workflow.docx'),('中文 表格.xlsx','sheet-workflow.xlsx'),('中文 演示.pptx','slides-workflow.pptx')]:
  if not (SHARED/new).exists():shutil.copy2(REPO/'.lab/extension-local-20261031/shared-project/office-fixtures'/filename,SHARED/new)
 with HostReserveMonitor(rt,cid,project=str(ROOT),env_id='debian13',receipt=OUT/'reserve-abort.json') as monitor:
  svc.start('debian13');result['display']=start_display();result['status']='READY';save();deadline=time.monotonic()+1200
  while time.monotonic()<deadline:
   if monitor.aborted:raise RuntimeError('reserve monitor aborted fixture')
   request=next(iter(sorted(QUEUE.glob('*.request.json'))),None)
   if request is None:time.sleep(.25);continue
   payload=json.loads(request.read_text());request.rename(request.with_suffix('.processing'))
   action=payload['action'];reply={'action':action,'started_at':time.time()}
   try:
    if action=='guest':
     r=guest(payload['argv'],min(45,payload.get('timeout',25)));reply.update(returncode=r.returncode,stdout=r.stdout[-16000:],stderr=r.stderr[-8000:])
    elif action=='launch':reply.update(app_launch(payload['component'],payload['file']))
    elif action=='capture':reply.update(capture(payload['name']))
    elif action=='cycle':
     svc.stop('debian13');reply['launchers']=[p.poll() for p in launchers];svc.start('debian13');reply['display']=start_display()
    elif action=='finish':reply['finished']=True
    else:raise RuntimeError('unsupported command')
   except Exception as exc:
    reply.update(error=str(exc),traceback=traceback.format_exc())
    if isinstance(exc,subprocess.CalledProcessError):reply['command_stderr']=str(exc.stderr)[-8000:]
    try:
     d=guest(['cat','/sys/fs/cgroup/memory.events','/sys/fs/cgroup/memory.peak','/sys/fs/cgroup/pids.events']);reply['guest_cgroup_after_error']={'returncode':d.returncode,'stdout':d.stdout,'stderr':d.stderr}
    except Exception as de:reply['diagnostic_error']=str(de)
   reply['finished_at']=time.time();reply['launcher_states']=[p.poll() for p in launchers];(QUEUE/(request.name.removesuffix('.request.json')+'.reply.json')).write_text(json.dumps(reply,ensure_ascii=False,indent=2));result['events'].append({'command':request.name,**reply});save()
   if 'error' in reply:raise RuntimeError('command failed; abandoning dependent GUI commands: '+str(reply['error']))
   if action=='finish':break
  result['status']='FINISHED';result['reserve_monitor_aborted']=monitor.aborted
except Exception as exc:result.update(status='FAILED',error=str(exc),traceback=traceback.format_exc());print(result['traceback'],flush=True)
finally:
 try:rt.verify_limits(cid,expected_id=cid);rt.stop(cid,project=str(ROOT),env_id='debian13');result['fixture_cleanup']='DIRECTED_TEST_CONTAINER_STOP'
 except Exception as exc:result['cleanup_error']=str(exc)
 for proc in launchers:
  try:proc.wait(timeout=15)
  except subprocess.TimeoutExpired:pass
 result['launcher_states']=[p.poll() for p in launchers];result['host_after']=identity();result['desktop_identity_unchanged']=result['host_before']==result['host_after'];save()
