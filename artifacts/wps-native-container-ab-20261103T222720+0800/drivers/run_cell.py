import sys,os,json,subprocess,time,shutil,traceback,statistics
from pathlib import Path
P=Path(__file__).resolve().parent;CID='8399ccd38b137d9283b6587007a43744e8d334809ed4864f002f091009ca555e';G='/tmp/unifier-wps-ab-20261103';os.environ['GOMAXPROCS']='2'
side,case,label=sys.argv[1:4];assert side in ['native','guest','installed'];assert case in ['writer','sheet','slides','family'];assert label.isalnum()
O=P/'cells'/label;O.mkdir(parents=True);result={'side':side,'case':case,'label':label,'status':'RUNNING'}
def save():(O/'result.json').write_text(json.dumps(result,indent=2))
def cmd(a,**kw):return subprocess.run(a,env=kw.pop('env',None),capture_output=True,text=True,timeout=kw.pop('timeout',20),**kw)
def guest(a,user='1000:1000',**kw):return cmd(['podman','exec','--user',user,CID,*a],**kw)
common=P/'common';env=dict(DISPLAY='127.0.0.1:200',XAUTHORITY=str(common/'Xauthority'),LANG='C.UTF-8',LC_ALL='C.UTF-8',QT_QPA_PLATFORM='xcb',LIBGL_ALWAYS_SOFTWARE='1',QT_OPENGL='software',QT_QUICK_BACKEND='software',OMP_NUM_THREADS='2',OPENBLAS_NUM_THREADS='2')
xenv=dict(os.environ,**env,LD_LIBRARY_PATH=str(P/'native-tools/usr/lib64'))
def x(a):return cmd([str(P/'native-tools/usr/bin/xdotool'),*a],env=xenv)
def windows():
 r=x(['search','--onlyvisible','--name','.']);out=[]
 for wid in r.stdout.split():out.append({'id':wid,'name':x(['getwindowname',wid]).stdout.strip()})
 return out
def capture(name):
 r=cmd(['magick','import','-window','root',str(O/(name+'.png'))],env=dict(xenv,MAGICK_MEMORY_LIMIT='64MiB',MAGICK_MAP_LIMIT='64MiB'));return {'returncode':r.returncode,'stderr':r.stderr[-1000:]}
def cg_of_pid(pid):return '/sys/fs/cgroup'+Path('/proc/'+str(pid)+'/cgroup').read_text().split('0::',1)[1].strip()
def metrics(out,n):
 a=['/usr/bin/python3',str(P/'metrics.py'),cg,str(out),str(n)]
 r=cmd(a if side!='guest' else ['podman','unshare',*a],timeout=n+20)
 if r.returncode:raise RuntimeError(r.stderr[-2000:])
def cleanup():
 if side!='guest':cmd(['systemctl','--user','stop','unifier-wps-ab-native.service'],timeout=12)
 else:
  r=guest(['sh','-c',"pids=''; for f in /proc/[0-9]*/exe; do e=$(readlink \"$f\") || continue; case \"$e\" in /tmp/unifier-wps-ab-20261103/root/opt/kingsoft/wps-office/office6/*) p=${f#/proc/}; p=${p%/exe}; pids=\"$pids $p\";; esac; done; for p in $pids; do kill -TERM \"$p\" 2>/dev/null; done; sleep 1; for p in $pids; do kill -KILL \"$p\" 2>/dev/null; done; printf '%s' \"$pids\""],timeout=8)
  result['cleanup_guest']=r.stdout
save()
try:
 assert not (P/'guardian-abort.json').exists(),'guardian latched'
 available=int(next(x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')).split()[1])*1024
 assert available>=2900*1024**2,('admission refused',available)
 assert not windows(),('previous windows remain',windows())
 components=['writer','sheet','slides'] if case=='family' else [case];exes={'writer':'wps','sheet':'et','slides':'wpp'};files={'writer':'writer.docx','sheet':'sheet.xlsx','slides':'slides.pptx'}
 if side!='guest':
  home=O/'home';(home/'.config/Kingsoft').mkdir(parents=True);(home/'tmp').mkdir();(home/'.cache').mkdir();(home/'runtime').mkdir(mode=0o700);shutil.copy2(common/'Office.conf',home/'.config/Kingsoft/Office.conf')
  env.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'.config'),XDG_CACHE_HOME=str(home/'.cache'),XDG_RUNTIME_DIR=str(home/'runtime'),XDG_SESSION_TYPE='x11',DBUS_SESSION_BUS_ADDRESS='unix:path='+str(home/'runtime/no-bus'),TMPDIR=str(home/'tmp'),FONTCONFIG_FILE=str(common/'fonts.conf'))
  prefix=Path('/opt/kingsoft/wps-office/office6')
  payload={'env':env,'commands':[[str(prefix/exes[c]),str(common/files[c])] for c in components]};(P/'native-launch.json').write_text(json.dumps(payload))
  cmd(['systemctl','--user','reset-failed','unifier-wps-ab-native.service'])
  mount=['--property=BindReadOnlyPaths='+str(P/'native-29183/opt/kingsoft/wps-office')+':/opt/kingsoft/wps-office'] if side=='native' else ['--property=InaccessiblePaths=/dev/dri /opt/kingsoft/wps-office/office6/wpscloudsvr']
  t=time.monotonic();r=cmd(['systemd-run','--user','--unit=unifier-wps-ab-native','--property=MemoryMax=1400M','--property=MemorySwapMax=128M','--property=TasksMax=768','--property=RuntimeMaxSec=180','--property=TimeoutStopSec=3','--property=PrivateTmp=true','--property=PrivateIPC=true','--property=TemporaryFileSystem=/dev/shm:rw,size=64M,mode=1777','--property=InaccessiblePaths=/dev/dri','--property=LimitCORE=0',*mount,'/usr/bin/python3',str(P/'native_launch.py')]);assert r.returncode==0,r.stderr
  for _ in range(30):
   group=cmd(['systemctl','--user','show','unifier-wps-ab-native.service','-p','ControlGroup','--value']).stdout.strip()
   if group and Path('/sys/fs/cgroup'+group).exists():break
   time.sleep(.1)
  cg='/sys/fs/cgroup'+group
 else:
  home=G+'/profiles/'+label
  assert guest(['mkdir','-p',home+'/.config/Kingsoft',home+'/.cache',home+'/tmp',home+'/runtime'],user='0').returncode==0
  assert guest(['chown','-R','1000:1000',home],user='0').returncode==0
  assert guest(['cp',G+'/common/Office.conf',home+'/.config/Kingsoft/Office.conf']).returncode==0
  guest(['chmod','700',home+'/runtime'])
  env.update(HOME=home,XDG_CONFIG_HOME=home+'/.config',XDG_CACHE_HOME=home+'/.cache',XDG_RUNTIME_DIR=home+'/runtime',XDG_SESSION_TYPE='x11',DBUS_SESSION_BUS_ADDRESS='unix:path='+home+'/runtime/no-bus',TMPDIR=home+'/tmp',FONTCONFIG_FILE=G+'/common/fonts.conf',XAUTHORITY=G+'/common/Xauthority')
  pid=int((P/'container-pid').read_text());cg=cg_of_pid(pid)
  metrics(O/'baseline.jsonl',1);t=time.monotonic()
  for c in components:
   a=['podman','exec','--user','1000:1000']
   for k,v in env.items():a+=['--env',k+'='+v]
   log=(O/(c+'-guest.log')).open('w');subprocess.Popen(a+[CID,'/opt/kingsoft/wps-office/office6/'+exes[c],G+'/common/'+files[c]],stdout=log,stderr=subprocess.STDOUT)
 result['cgroup']=cg;result['env']=env
 deadline=time.monotonic()+35;ready=False
 while time.monotonic()<deadline:
  if (P/'guardian-abort.json').exists():raise RuntimeError('host protection abort')
  ws=windows()
  if all(any(files[c] in w['name'] for w in ws) for c in components):ready=True;break
  time.sleep(.5)
 result.update(ready=ready,open_window_seconds=time.monotonic()-t,windows=ws,capture=capture('opened'));save()
 if not ready:raise RuntimeError('expected document titles absent')
 time.sleep(12)
 result['dismissed_default_app_dialogs']=[]
 for w in windows():
  if w['name']=='WPS Office':
   r=cmd(['/usr/bin/python3',str(P/'close_window.py'),w['id']],env=xenv);assert r.returncode==0,r.stderr
   result['dismissed_default_app_dialogs'].append(w);time.sleep(.5)
 assert not any(w['name']=='WPS Office' for w in windows()),'default app modal still present'
 time.sleep(1)
 metrics(O/'samples.jsonl',16)
 assert not (P/'guardian-abort.json').exists(),'host protection aborted measurement'
 result['capture_sampled']=capture('sampled')
 result['windows_sampled']=windows()
 rows=[json.loads(s) for s in (O/'samples.jsonl').read_text().splitlines()]
 sums=[sum(x['pss'] for x in r['rows'] if x['app']) for r in rows];swaps=[sum(x['swap_pss'] or 0 for x in r['rows'] if x['app']) for r in rows]
 dt=rows[-1]['at']-rows[0]['at'];cpu=(rows[-1]['cpu']['usage_usec']-rows[0]['cpu']['usage_usec'])/1e6/dt*100
 result['summary']={'app_pss_median_mib':statistics.median(sums)/1024**2,'app_pss_range_mib':[min(sums)/1024**2,max(sums)/1024**2],'app_swap_pss_median_mib':statistics.median(swaps)/1024**2,'app_processes_median':statistics.median(sum(x['app'] for x in r['rows']) for r in rows),'app_threads_median':statistics.median(sum(x['threads'] for x in r['rows'] if x['app']) for r in rows),'idle_cgroup_cpu_one_core_pct':cpu,'host_min_available_mib':min(r['host_available'] for r in rows)/1024**2,'pss_errors':sum(len(r['errors']) for r in rows),'memory_events_before':rows[0]['memory_events'],'memory_events_after':rows[-1]['memory_events'],'pids_events_before':rows[0]['pids_events'],'pids_events_after':rows[-1]['pids_events'],'cpu_throttle_delta':rows[-1]['cpu'].get('nr_throttled',0)-rows[0]['cpu'].get('nr_throttled',0)}
 result['local_core_only']=(P/'core-only.json').exists()
 # Only private X199/200 document windows receive input.
 for w in windows():
  if any(files[c] in w['name'] for c in components):cmd(['/usr/bin/python3',str(P/'close_window.py'),w['id']],env=xenv);time.sleep(.7)
 time.sleep(2);result['windows_after_quit']=windows();metrics(O/'after-quit.jsonl',1)
 result['status']='MEASURED' if not result['summary']['pss_errors'] and any(sums) else 'INCOMPLETE_PSS'
except Exception as e:result.update(status='FAILED',error=str(e),traceback=traceback.format_exc());capture('failure')
finally:
 cleanup();save()
print(json.dumps(result,indent=2))
