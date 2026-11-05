import sys,os,json,subprocess,time,pty,select,fcntl,termios,struct,signal,re,traceback
from pathlib import Path
BASE=Path('/home/agent/expansion-continuation-20261103');SOURCE=BASE/'source';ROOT=BASE/'cli-environment-v1';OUT=BASE/'fedora-pty-resize-evidence';OUT.mkdir(exist_ok=False);sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService
from unifier.project_sessions import ProjectSessions
config,lock,_=load_project();rt=PodmanRuntime();svc=EnvironmentService(ROOT,rt,config,lock);sessions=ProjectSessions(ROOT,svc);before={r['session_uuid'] for r in sessions.store.list()};cid=svc.store.load('fedora44')['runtime']['container_id'];assert not rt.inspect(cid).running;process=None;master=None;slave=None;result={'status':'RUNNING'}
def read_size(view):
 pid=view['guest_pid'];r=rt.exec(cid,['readlink','/proc/'+str(pid)+'/fd/0'],project=str(ROOT),env_id='fedora44',workdir=None,env={},capture=True,timeout=10);assert r.returncode==0;r=r.stdout.strip();assert r.startswith('/dev/pts/')
 out=rt.exec(cid,['stty','-F',r,'size'],project=str(ROOT),env_id='fedora44',workdir=None,env={},capture=True,timeout=10);assert out.returncode==0;return out.stdout.strip()
try:
 master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',31,103,0,0));env=dict(os.environ,UNIFIER_HOME=str(ROOT),TERM='xterm-256color',PYTHONPATH=str(SOURCE/'src'));process=subprocess.Popen([str(SOURCE/'bin/unifier'),'project','run','fedora-cli','--tty','--','htop'],cwd=SOURCE,env=env,stdin=slave,stdout=slave,stderr=slave,start_new_session=True);os.close(slave);slave=None;buf=bytearray();deadline=time.monotonic()+40;view=None
 while time.monotonic()<deadline and process.poll() is None:
  ready,_,_=select.select([master],[],[],.1)
  if ready:
   try:buf.extend(os.read(master,65536))
   except OSError:break
  candidates=[r['session_uuid'] for r in sessions.store.list() if r['session_uuid'] not in before]
  if candidates:
   v=sessions.show(candidates[-1])
   if v.get('guest_live'):
    view=v;break
 assert view,'real htop guest did not become live';result['initial_guest_tty_size']=read_size(view);assert result['initial_guest_tty_size']=='31 103'
 fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',37,111,0,0));os.kill(process.pid,signal.SIGWINCH);deadline=time.monotonic()+10;observed=[]
 while time.monotonic()<deadline:
  size=read_size(view);observed.append(size)
  if size=='37 111':break
  time.sleep(.1)
 assert size=='37 111',observed;result['resized_guest_tty_size']=size
 os.write(master,b'q');deadline=time.monotonic()+15
 while time.monotonic()<deadline and process.poll() is None:
  ready,_,_=select.select([master],[],[],.1)
  if ready:
   try:buf.extend(os.read(master,65536))
   except OSError:break
 code=process.wait(timeout=10);assert code==0;final=sessions.show(view['session_uuid']);assert final['guest_state']=='gone' and final['lease_token'] is None;result.update(status='PASS_REAL_GUEST_PTY_RESIZE_AND_Q_EXIT',returncode=code,session=final);(OUT/'terminal.raw').write_bytes(buf)
except Exception as ex:result.update(status='FAIL',error=str(ex),traceback=traceback.format_exc());print(result['traceback'],flush=True)
finally:
 if process is not None and process.poll() is None:
  try:os.killpg(process.pid,signal.SIGTERM);process.wait(timeout=10)
  except Exception:pass
 for fd in [master,slave]:
  if fd is not None:
   try:os.close(fd)
   except OSError:pass
 rt.stop(cid,project=str(ROOT),env_id='fedora44');result['container_stopped']=not rt.inspect(cid).running;(OUT/'result.json').write_text(json.dumps(result,indent=2))
print(json.dumps({k:result.get(k) for k in ['status','initial_guest_tty_size','resized_guest_tty_size','returncode','error']},indent=2))
