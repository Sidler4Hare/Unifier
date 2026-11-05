import sys,json,time
from pathlib import Path
ctl=Path(__file__).resolve().parent;version,start=sys.argv[1],int(sys.argv[2]);queue=ctl/('gui-commands-'+version);queue.mkdir(mode=0o700,exist_ok=True)
actions=json.load(sys.stdin)
for n,a in enumerate(actions,start):
 p=queue/f'{n:03}.request.json';assert not p.exists() and not (queue/f'{n:03}.reply.json').exists();tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(a));tmp.replace(p)
end=time.monotonic()+55
for n in range(start,start+len(actions)):
 p=queue/f'{n:03}.reply.json'
 while not p.exists():
  if time.monotonic()>end:raise RuntimeError('GUI reply pending: '+str(p))
  time.sleep(.25)
 r=json.loads(p.read_text());print(json.dumps({'command':n,**r},ensure_ascii=False),flush=True)
 if 'error' in r:raise RuntimeError(r['error'])
