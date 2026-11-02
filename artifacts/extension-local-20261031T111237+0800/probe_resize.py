"""Compare direct Podman and product PTY dimensions with a readiness barrier."""
import os,pty,fcntl,termios,struct,time,select,errno,json,sys
from pathlib import Path
r=Path(__file__).resolve().parents[2];e=Path(__file__).resolve().parent;label=sys.argv[1];out=e/label;out.mkdir(exist_ok=False);rows=[]
cid=json.loads((e/'fixture.json').read_text())['records']['arch']['runtime']['container_id'];script='printf "SIZE1 "; stty size; read line; printf "SIZE2 "; stty size'
for mode in ['podman-control','product']:
 argv=['podman','exec','-it','--user','1000:1000',cid,'sh','-c',script] if mode=='podman-control' else [str(r/'bin/unifier'),'project','run','--tty','shared-arch','--','sh','-c',script]
 env={**os.environ,'UNIFIER_HOME':str(r/'.lab/extension-local-20261031/environments'),'LC_ALL':'C','LANG':'C','TERM':'xterm-256color'}
 m,s=pty.openpty();fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack('HHHH',30,100,0,0));pid=os.fork()
 if pid==0:
  os.close(m);os.setsid();fcntl.ioctl(s,termios.TIOCSCTTY,0)
  for fd in [0,1,2]:os.dup2(s,fd)
  if s>2:os.close(s)
  os.execvpe(argv[0],argv,env)
 os.close(s);b=bytearray();t=time.monotonic();changed=None;sent=False;rc=None
 try:
  while time.monotonic()-t<150:
   ready,_,_=select.select([m],[],[],.1)
   if ready:
    try:b.extend(os.read(m,65536))
    except OSError as x:
     if x.errno!=errno.EIO:raise
   if changed is None and b'SIZE1 30 100' in b:
    fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack('HHHH',38,120,0,0));changed=time.monotonic()
   if changed is not None and time.monotonic()-changed>3 and not sent:os.write(m,b'next\n');sent=True
   done,status=os.waitpid(pid,os.WNOHANG)
   if done:rc=os.waitstatus_to_exitcode(status);break
  if rc is None:raise RuntimeError('resize probe did not finish')
 finally:os.close(m)
 data=b.decode('utf8','replace');(out/(mode+'.terminal.txt')).write_text(data);row={'mode':mode,'argv':argv,'exit':rc,'output':data,'resized':changed is not None,'guest_changed':('SIZE2 38 120' in data),'seconds':round(time.monotonic()-t,3)};rows.append(row);print(json.dumps(row),flush=True)
(out/'result.json').write_text(json.dumps(rows,indent=2)+'\n')
