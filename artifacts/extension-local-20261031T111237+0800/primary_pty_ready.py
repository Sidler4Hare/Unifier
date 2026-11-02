#!/usr/bin/env python3
"""PTY acceptance with readiness barrier, after explicit pager installation."""
import errno,fcntl,json,os,pty,select,signal,struct,subprocess,termios,time,sys
from pathlib import Path
REPO=Path(__file__).resolve().parents[2];EVID=Path(__file__).resolve().parent
SOURCE=REPO/'.lab/extension-local-20261031/environments';PROJECT=REPO/'.lab/extension-local-20261031/shared-project'
ENV={**os.environ,'UNIFIER_HOME':str(SOURCE),'LC_ALL':'C','LANG':'C','TERM':'xterm-256color','BAT_PAGER':'/usr/bin/less -R'}
OUT=EVID/(sys.argv[1] if len(sys.argv)>1 else 'pty-attempt3');OUT.mkdir(exist_ok=False);results=[]
text=PROJECT/'pager 中文.txt'
for target in ('debian13','arch'):
 for app in ('htop','bat'):
  exe='htop' if app=='htop' else ('batcat' if target=='debian13' else 'bat')
  argv=[str(REPO/'bin/unifier'),'project','run','--tty','--env','TERM','--env','BAT_PAGER','shared-'+target,'--',exe]
  if app=='bat':argv+=['--paging=always','--color=never',str(text)]
  master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,100,0,0));pid=os.fork()
  if pid==0:
   try:
    os.close(master);os.setsid();fcntl.ioctl(slave,termios.TIOCSCTTY,0)
    for fd in (0,1,2):os.dup2(slave,fd)
    if slave>2:os.close(slave)
    os.execve(argv[0],argv,ENV)
   except BaseException:os._exit(127)
  os.close(slave);data=bytearray();start=time.monotonic();ready_at=None;resize_at=None;quit_at=None;status=None;size_before_resize=None;size_before_quit=None;error=None
  try:
   while time.monotonic()-start<120:
    now=time.monotonic();elapsed=now-start
    ready,_,_=select.select([master],[],[],.15)
    if ready:
     try:
      b=os.read(master,65536)
      if b:data.extend(b)
     except OSError as ex:
      if ex.errno!=errno.EIO:raise
    if ready_at is None:
     ok=(b'Help' in data and b'Quit' in data and b'\x1b[?1049h' in data) if app=='htop' else (b'\x1b[?1049h' in data and len(data)>500)
     if ok:ready_at=elapsed
    if ready_at is not None and elapsed-ready_at>2 and resize_at is None:
     size_before_resize=len(data);fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',38,120,0,0));resize_at=elapsed
    if resize_at is not None and elapsed-resize_at>3 and quit_at is None:
     size_before_quit=len(data);os.write(master,b'q');quit_at=elapsed
    done,code=os.waitpid(pid,os.WNOHANG)
    if done:status=os.waitstatus_to_exitcode(code);break
   if status is None:
    error='timeout before verified q/normal exit'
    rows=json.loads(subprocess.check_output([str(REPO/'bin/unifier'),'project','session','list'],env=ENV,text=True))
    for s in rows:
     if s.get('environment_id')==target and s.get('argv',[None])[0]==exe and s.get('effective_state') not in ['exited','failed','cancelled','interrupted']:
      sid=s.get('session_uuid') or s.get('uuid') or s.get('id')
      if sid:subprocess.run([str(REPO/'bin/unifier'),'project','session','cancel',sid,'--signal','TERM'],env=ENV,capture_output=True,text=True,timeout=30)
    os.kill(pid,signal.SIGTERM);_,code=os.waitpid(pid,0);status=os.waitstatus_to_exitcode(code)
  finally:os.close(master)
  name=target+'-'+app;(OUT/(name+'.terminal.bin')).write_bytes(data)
  rendering=ready_at is not None;post_resize=size_before_quit is not None and size_before_resize is not None and size_before_quit>size_before_resize
  paging=(b'0299' not in data) if app=='bat' else True
  row={'target':target,'app':app,'argv':argv,'exit':status,'seconds':round(time.monotonic()-start,3),'bytes':len(data),'ready_at':ready_at,'resized_at':resize_at,'q_sent_at':quit_at,'bytes_before_resize':size_before_resize,'bytes_before_quit':size_before_quit,'repaint_after_resize':post_resize,'pager_did_not_dump_full_file':paging,'ok':status==0 and rendering and resize_at is not None and quit_at is not None and post_resize and paging,'error':error}
  results.append(row);(OUT/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n');print(json.dumps(row),flush=True)
raise SystemExit(0 if all(x['ok'] for x in results) else 1)
