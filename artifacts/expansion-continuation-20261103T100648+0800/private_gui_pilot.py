import sys,os,json,subprocess,time,traceback,shutil,datetime,re
from pathlib import Path
REPO=Path('/home/omg/文档/多兼容linux机');sys.path[:0]=[str(REPO/'src'),str(REPO)]
from unifier.project import load_project
from unifier.service import EnvironmentService
from tools.guarded_test import GuardedPodmanRuntime,HostReserveMonitor
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text())/'private-gui-pilot';OUT.mkdir(exist_ok=True)
ROOT=REPO/'.lab/guarded-expansion-20261103/environment';SHARED=ROOT.parent/'shared';config,_,_=load_project();lock=json.loads((CTL/'guarded-images.lock.json').read_text());rt=GuardedPodmanRuntime(ROOT);svc=EnvironmentService(ROOT,rt,config,lock);cid=svc.store.load('debian13')['runtime']['container_id'];result={'status':'RUNNING','container_id':cid,'display':'guest-local Xvfb :97','host_display_sockets_mounted':False,'workflow_accepted':False};launcher=None
GUEST_ENV={'DISPLAY':':97','XAUTHORITY':'/tmp/unifier-private-display/Xauthority','LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
def save(): (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
def guest(argv,timeout=20):
 cmd=['podman','exec','--user','1000:1000']
 for k,v in GUEST_ENV.items():cmd+=['--env',k+'='+v]
 return subprocess.run(cmd+[cid,*argv],capture_output=True,text=True,timeout=timeout)
def host_identity():return {'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'kwin_pids':subprocess.check_output(['pgrep','-u',str(os.getuid()),'-x','kwin_wayland'],text=True).split()}
result['host_before']=host_identity();save()
try:
 shutil.copy2(REPO/'.lab/extension-local-20261031/shared-project/office-fixtures/中文 文档.docx',SHARED/'writer.docx')
 with HostReserveMonitor(rt,cid,project=str(ROOT),env_id='debian13',receipt=OUT/'reserve-abort.json') as monitor:
  svc.start('debian13')
  script="""import os,subprocess,secrets,json
from pathlib import Path
p=Path('/tmp/unifier-private-display');p.mkdir(mode=0o700,exist_ok=True)
auth=p/'Xauthority';auth.touch(mode=0o600,exist_ok=True);os.chmod(auth,0o600)
subprocess.run(['xauth','-f',str(auth),'add',':97','. ',secrets.token_hex(16)],check=True)
log=(p/'xvfb.log').open('ab');popen=subprocess.Popen(['Xvfb',':97','-screen','0','1280x800x24','-nolisten','tcp','-auth',str(auth)],stdout=log,stderr=log,start_new_session=True)
print(json.dumps({'pid':popen.pid,'display':':97'}))
""".replace("'. '","'.'")
  r=guest(['python3','-c',script]);assert r.returncode==0,r.stderr;result['xvfb']=json.loads(r.stdout);save()
  for _ in range(30):
   r=guest(['xdotool','getdisplaygeometry'])
   if r.returncode==0:break
   time.sleep(.2)
  assert r.returncode==0,r.stderr
  home='/home/multilinux-1000/guarded-writer-home';r=guest(['mkdir','-p',home]);assert r.returncode==0
  if not (svc.store.launch_path('guarded-writer') if hasattr(svc.store,'launch_path') else ROOT/'.absent').exists():
   try:svc.show_app('guarded-writer')
   except Exception:
    svc.add_app('debian13','guarded-writer','/usr/bin/env',['/usr/bin/env','HOME='+home,'XDG_CONFIG_HOME='+home+'/.config','/usr/bin/wps'],as_host_user=True,on_demand=True,desktop_required=False,workdir=str(SHARED),env_overlay={**GUEST_ENV,'QT_QPA_PLATFORM':'xcb','LIBGL_ALWAYS_SOFTWARE':'1'})
  env=dict(os.environ,UNIFIER_HOME=str(ROOT));log=(CTL/'private-gui-writer.log').open('w')
  launcher=subprocess.Popen([str(REPO/'bin/unifier'),'app','open','guarded-writer','--',str(SHARED/'writer.docx')],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
  windows=[]
  for _ in range(60):
   if monitor.aborted:raise RuntimeError('host reserve monitor aborted fixture')
   r=guest(['xdotool','search','--onlyvisible','--class','wps|wpsoffice'])
   if r.returncode==0:
    windows=[int(x) for x in r.stdout.split()];break
   if launcher.poll() is not None:raise RuntimeError('application launcher exited '+str(launcher.returncode))
   time.sleep(.5)
  assert windows,'no visible guest-local WPS window'
  owned=[]
  for w in windows:
   props=guest(['xprop','-id',str(w),'_NET_WM_PID','WM_CLASS','_NET_WM_NAME']);m=re.search(r'_NET_WM_PID.*?= (\d+)',props.stdout)
   if not m:continue
   pid=int(m.group(1));exe=guest(['readlink','/proc/'+str(pid)+'/exe']).stdout.strip()
   if '/kingsoft/' not in exe:continue
   geom=guest(['xdotool','getwindowgeometry','--shell',str(w)]);g=dict(line.split('=',1) for line in geom.stdout.splitlines() if '=' in line)
   owned.append({'window_id':w,'guest_pid':pid,'exe':exe,'properties':props.stdout,'width':int(g['WIDTH']),'height':int(g['HEIGHT'])})
  assert owned,'visible window ownership not proven';owned.sort(key=lambda r:r['width']*r['height'],reverse=True);selected=owned[0];result['windows']=owned;save()
  r=guest(['xwd','-silent','-id',str(selected['window_id']),'-out','/tmp/unifier-private-display/window.xwd']);assert r.returncode==0,r.stderr
  subprocess.run(['podman','cp',cid+':/tmp/unifier-private-display/window.xwd',str(CTL/'private-gui-writer.xwd')],check=True,capture_output=True,timeout=20)
  subprocess.run(['magick',str(CTL/'private-gui-writer.xwd'),str(CTL/'private-gui-writer.png')],check=True,capture_output=True,timeout=20)
  result['status']='PASS_PRIVATE_DISPLAY_OWNED_WINDOW_PILOT';result['screenshot_private']=str(CTL/'private-gui-writer.png');result['normal_exit']='NOT_TESTED_PILOT';result['reserve_monitor_aborted']=monitor.aborted
except Exception as ex:result['status']='FAIL';result['error']=str(ex);result['traceback']=traceback.format_exc();print(result['traceback'],flush=True)
finally:
 # The pilot deliberately ends its whole dedicated fixture, never a host GUI session.
 try:rt.verify_limits(cid,expected_id=cid);rt.stop(cid,project=str(ROOT),env_id='debian13');result['fixture_cleanup']='DIRECTED_TEST_CONTAINER_STOP'
 except Exception as ex:result['cleanup_error']=str(ex)
 if launcher is not None:
  try:result['launcher_returncode']=launcher.wait(timeout=15)
  except subprocess.TimeoutExpired:result['launcher_returncode']='UNKNOWN'
 result['host_after']=host_identity();result['desktop_identity_unchanged']=result['host_before']==result['host_after'];save()
print(json.dumps(result,ensure_ascii=False,indent=2))
