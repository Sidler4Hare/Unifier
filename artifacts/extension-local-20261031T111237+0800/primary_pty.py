#!/usr/bin/env python3
"""Real project-run PTY read/resize/q; only this isolated installation."""
import errno,fcntl,json,os,pty,select,signal,struct,subprocess,termios,time,traceback
from pathlib import Path
REPO=Path(__file__).resolve().parents[2];EVID=Path(__file__).resolve().parent
SOURCE=REPO/'.lab/extension-local-20261031/environments';PROJECT=REPO/'.lab/extension-local-20261031/shared-project'
ENV={**os.environ,'UNIFIER_HOME':str(SOURCE),'LC_ALL':'C','LANG':'C','TERM':'xterm-256color','BAT_PAGER':'less -R'}
OUT=EVID/'pty-attempt1';OUT.mkdir(exist_ok=False);results=[]
text=PROJECT/'pager 中文.txt';text.write_text(''.join(f'{i:04d} 中文 row\n' for i in range(300)))
for target in ('debian13','arch'):
 for app in ('htop','bat'):
  exe='htop' if app=='htop' else ('batcat' if target=='debian13' else 'bat')
  argv=[str(REPO/'bin/unifier'),'project','run','--tty','--env','TERM','--env','BAT_PAGER','shared-'+target,'--',exe]
  if app=='bat':argv+=['--paging=always','--color=never',str(text)]
  master,slave=pty.openpty();fcntl.ioctl(slave,termios.TIOCSWINSZ,struct.pack('HHHH',30,100,0,0))
  # A session with an actual controlling terminal, required by the product.
  pid=os.fork()
  if pid==0:
   try:
    os.close(master);os.setsid();fcntl.ioctl(slave,termios.TIOCSCTTY,0)
    for fd in (0,1,2):os.dup2(slave,fd)
    if slave>2:os.close(slave)
    os.execve(argv[0],argv,ENV)
   except BaseException:os._exit(127)
  os.close(slave);chunks=[];start=time.monotonic();resized=False;quit_sent=False;status=None
  try:
   while time.monotonic()-start<45:
    elapsed=time.monotonic()-start
    if elapsed>4 and not resized:
     fcntl.ioctl(master,termios.TIOCSWINSZ,struct.pack('HHHH',38,120,0,0));resized=True
    if elapsed>7 and not quit_sent:os.write(master,b'q');quit_sent=True
    ready,_,_=select.select([master],[],[],.2)
    if ready:
     try:
      b=os.read(master,65536)
      if b:chunks.append(b)
     except OSError as x:
      if x.errno!=errno.EIO:raise
    done,code=os.waitpid(pid,os.WNOHANG)
    if done:status=os.waitstatus_to_exitcode(code);break
   if status is None:
    # Cancel only sessions in our dedicated root, using the product contract.
    rows=json.loads(subprocess.check_output([str(REPO/'bin/unifier'),'project','session','list'],env=ENV,text=True))
    sessions=rows if isinstance(rows,list) else rows.get('sessions',[])
    for s in sessions:
     if s.get('phase') not in ['completed','failed','cancelled','interrupted','gone']:
      sid=s.get('session_uuid') or s.get('uuid') or s.get('id')
      if sid:subprocess.run([str(REPO/'bin/unifier'),'project','session','cancel',sid,'--signal','TERM'],env=ENV,capture_output=True,text=True,timeout=30)
    os.kill(pid,signal.SIGTERM);_,code=os.waitpid(pid,0);status=os.waitstatus_to_exitcode(code)
  finally:os.close(master)
  data=b''.join(chunks);name=target+'-'+app;(OUT/(name+'.terminal.bin')).write_bytes(data)
  row={'target':target,'app':app,'argv':argv,'seconds':round(time.monotonic()-start,3),'exit':status,'bytes':len(data),'real_pty':True,'resized':resized,'q_sent':quit_sent,'ok':status==0 and len(data)>200 and resized and quit_sent,'scope':'render/refresh/resize/quit only; no process termination actions'}
  results.append(row);(OUT/'result.json').write_text(json.dumps(results,ensure_ascii=False,indent=2)+'\n');print(json.dumps(row),flush=True)
raise SystemExit(0 if all(x['ok'] for x in results) else 1)
