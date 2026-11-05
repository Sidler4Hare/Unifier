import os,subprocess,time,json,struct,secrets,signal
from pathlib import Path
P=Path(__file__).resolve().parent
def field(b):return struct.pack('>H',len(b))+b
auth=P/'Xauthority';auth.write_bytes(struct.pack('>H',65535)+field(b'')+field(b'200')+field(b'MIT-MAGIC-COOKIE-1')+field(secrets.token_bytes(16)));auth.chmod(0o600)
assert not Path('/tmp/.X199-lock').exists()
children=[]
def stop(*args):
 for c in reversed(children):
  if c.poll() is None:c.terminate()
 raise SystemExit(0)
signal.signal(signal.SIGTERM,stop)
log=(P/'display.log').open('a')
children.append(subprocess.Popen([str(P/'native-tools/usr/bin/Xvfb'),':199','-screen','0','1280x800x24','-nolisten','tcp','-auth',str(auth),'-fp','built-ins'],stdout=log,stderr=log))
for _ in range(100):
 if Path('/tmp/.X11-unix/X199').exists():break
 if children[0].poll() is not None:raise RuntimeError('Xvfb failed')
 time.sleep(.1)
else:raise RuntimeError('Xvfb not ready')
children.append(subprocess.Popen(['/usr/bin/python3',str(P/'relay.py')],stdout=log,stderr=log))
ns=json.loads((P/'native-ns.json').read_text());children.append(subprocess.Popen(ns+['/usr/bin/python3',str(P/'relay.py')],env=dict(os.environ,GOMAXPROCS='2'),stdout=log,stderr=log))
time.sleep(1);assert all(c.poll() is None for c in children)
(P/'display-ready.json').write_text(json.dumps({'xvfb':children[0].pid,'native_relay':children[1].pid,'guest_relay_wrapper':children[2].pid,'geometry':'1280x800x24','display':'127.0.0.1:200','tcp_listener':'loopback only, in each namespace','auth':'MIT-MAGIC-COOKIE-1'}))
while True:time.sleep(.5)
