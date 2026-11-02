"""PTY 24x80 -> wait for ASK: -> resize to 38x120 + SIGWINCH -> answer; child prints stty size after."""
import fcntl,os,pty,select,signal,struct,subprocess,sys,termios,time
m,s=pty.openpty(); fcntl.ioctl(s,termios.TIOCSWINSZ,struct.pack("HHHH",24,80,0,0))
p=subprocess.Popen(sys.argv[1:],stdin=s,stdout=s,stderr=s,start_new_session=True); os.close(s)
d=b"";done=False;end=time.time()+120
while time.time()<end:
    if select.select([m],[],[],0.2)[0]:
        try: c=os.read(m,65536)
        except OSError: break
        if not c: break
        d+=c
    if not done and os.environ.get("PAT","ASK:").encode() in d:
        done=True; fcntl.ioctl(m,termios.TIOCSWINSZ,struct.pack("HHHH",38,120,0,0)); p.send_signal(signal.SIGWINCH); time.sleep(0.5); os.write(m,b"ok\n")
print("exit",p.wait(10)); print(d.decode(errors="replace").replace("\r","")[-300:])
