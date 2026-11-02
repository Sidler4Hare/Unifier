#!/usr/bin/env python3
"""Independent current-source local probe; synthetic data and exact owned CIDs."""
import argparse,hashlib,itertools,json,os,shlex,subprocess,time,traceback
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
EVID=Path(__file__).resolve().parent
LAB=REPO/'.lab/extension-local-20261031'
BIN=REPO/'bin/unifier'
TARGETS=('debian13','arch')
q=shlex.quote

class Probe:
 def __init__(self,label,root):
  self.out=EVID/label;self.out.mkdir(exist_ok=False)
  self.root=root;self.rows=[]
  self.env={**os.environ,'UNIFIER_HOME':str(root),'LC_ALL':'C','LANG':'C','PYTHONDONTWRITEBYTECODE':'1'}
 def save(self):
  (self.out/'steps.json').write_text(json.dumps(self.rows,ensure_ascii=False,indent=2)+'\n')
 def call(self,name,args,*,root=None,expect=0,input=None,timeout=240):
  argv=[str(BIN),*map(str,args)];env={**self.env,'UNIFIER_HOME':str(root or self.root)};t=time.monotonic()
  p=subprocess.run(argv,cwd=REPO,env=env,input=input,text=True,capture_output=True,timeout=timeout)
  (self.out/(name+'.stdout')).write_text(p.stdout);(self.out/(name+'.stderr')).write_text(p.stderr)
  row={'name':name,'argv':argv,'root':env['UNIFIER_HOME'],'exit':p.returncode,'expected_exit':expect,'seconds':round(time.monotonic()-t,3),'ok':p.returncode==expect if expect is not None else True}
  self.rows.append(row);self.save();print(name,p.returncode,flush=True)
  if expect is not None and p.returncode!=expect:raise RuntimeError(name+': '+p.stderr[-1800:])
  return p
 def js(self,name,args,**kw):return json.loads(self.call(name,args,**kw).stdout)
 def note(self,name,**x):
  self.rows.append({'name':name,**x});self.save()
  if x.get('ok') is False:raise AssertionError((name,x))

def root_uuid(d):
 return d.get('root_uuid') or d.get('uuid') or d['id']

def record(root,t):return json.loads((root/'.lab/state/environments'/f'{t}.json').read_text())

def cleanup(p,root):
 for t in TARGETS:
  f=root/'.lab/state/environments'/f'{t}.json'
  if not f.exists():continue
  r=record(root,t);cid=r['runtime']['container_id']
  x=subprocess.run(['podman','inspect',cid],capture_output=True,text=True)
  if x.returncode==0:
   actual=json.loads(x.stdout)[0]
   if actual['Id']!=cid:raise RuntimeError('cleanup CID differs')
  p.call('cleanup-stop-'+t,['env','stop',t],root=root,expect=None)
  p.call('cleanup-remove-'+t,['env','remove',t,'--yes'],root=root)


def canary(label):
 root=LAB/('init-'+label);p=Probe(label,root)
 try:
  p.call('init',['init','--target','debian13','--target','arch','--yes','--desktop','--network','pasta','--json'])
  for t in TARGETS:
   folder=root/'containers'/t/'files';rr=p.js(t+'-root',['root','add',folder]);project=p.js(t+'-project',['project','add','--root',root_uuid(rr),'--environment',t,'--alias','canary-'+t])
   for cycle in (1,2):
    path=folder/'中文 save $; sample.txt';content=f'{t} cycle {cycle} 中文\n'
    script='test "$(id -u)" = '+str(os.getuid())+' && printf %s '+q(content)+' > '+q(str(path))
    p.call(f'{t}-write-{cycle}',['project','run','canary-'+t,'--','sh','-c',script])
    p.note(f'{t}-owner-{cycle}',ok=path.read_text()==content and path.stat().st_uid==os.getuid(),uid=path.stat().st_uid,expected_uid=os.getuid())
    p.call(f'{t}-stop-{cycle}',['stop',t,'--json'])
    listing=p.js(f'{t}-stopped-ls-{cycle}',['ls',t,'/','--json'])
    p.note(f'{t}-stopped-ls-oracle-{cycle}',ok=bool(listing),result=listing)
    read=p.call(f'{t}-reopen-{cycle}',['project','run','canary-'+t,'--','cat',path])
    p.note(f'{t}-content-{cycle}',ok=read.stdout==content)
   p.call(t+'-last-stop',['stop',t,'--json'])
 finally:cleanup(p,root)
 x=p.js('list-after-remove',['list','--json']);dirs=p.js('dirs-after-remove',['dirs','--json'])
 for t in TARGETS:
  row=next(z for z in x['targets'] if z['target']==t)
  p.note(t+'-missing-oracle',ok=row['state']!='ready' and not row['files_mounted'] and bool(row.get('error') or row.get('failure') or row['state']=='missing'),result=row)
 p.note('complete',ok=True)


def prepare(label):
 source=LAB/'environments';manage=LAB/'management';project=LAB/'shared-project';project.mkdir(parents=True,exist_ok=True);p=Probe(label,manage)
 try:
  for t in TARGETS:
   private=manage/'containers'/t/'files';private.mkdir(parents=True,exist_ok=True)
   p.call(t+'-create',['env','create','--target',t,'--id',t,'--network','pasta','--share',project,'--share',private,'--share-label','preserve-host','--desktop','--userns','keep-id'],root=source)
   p.call(t+'-adopt',['adopt','--target',t,'--root',source,'--environment',t,'--json'])
  # One data root has one project, and a project binds one environment.
  # The shared directory is mounted into both containers; each environment's
  # project root is its own private directory so both can run against that mount.
  for t in TARGETS:
   private=manage/'containers'/t/'files'
   rr=p.js(t+'-private-root',['root','add',private],root=source)
   p.js(t+'-project',['project','add','--root',root_uuid(rr),'--environment',t,'--alias','shared-'+t],root=source)
   rec=record(source,t)
   mounted={os.path.realpath(m['host']) for m in rec.get('shared_mounts') or [] if isinstance(m,dict) and isinstance(m.get('host'),str)}
   p.note(t+'-record-mounts',ok={os.path.realpath(project),os.path.realpath(private)}<=mounted,mounted=sorted(mounted))
   p.call(t+'-start',['start',t,'--json'])
   x=p.call(t+'-identity',['project','run','shared-'+t,'--','sh','-c','printf "uid="; id -u; printf "gid="; id -g; printf "home=%s\\n" "$HOME"; printf "mnt="; readlink /proc/self/ns/mnt; printf "pidns="; readlink /proc/self/ns/pid; cat /etc/os-release'],root=source)
   assert 'uid='+str(os.getuid()) in x.stdout
   visible='test -d '+q(str(project))+' && test -w '+q(str(project))+' && test -d '+q(str(private))+' && test -w '+q(str(private))
   p.call(t+'-mounts',['project','run','shared-'+t,'--','sh','-c',visible],root=source)
  p.js('management-list',['list','--json'])
  (EVID/'fixture.json').write_text(json.dumps({'source_root':str(source),'management_root':str(manage),'project':str(project),'records':{t:record(source,t) for t in TARGETS}},ensure_ascii=False,indent=2)+'\n')
 except:cleanup(p,source);raise
 p.note('complete',ok=True)


def interwrite(label):
 source=LAB/'environments';manage=LAB/'management';folder=LAB/'shared-project';p=Probe(label,manage);checks=[]
 def guest(t,name,script):return p.call(name,['project','run','shared-'+t,'--','sh','-c',script],root=source).stdout
 for a,b in itertools.permutations(('host',*TARGETS),2):
  for mode in ['direct','inplace','atomic']:
   for cycle in range(1,4):
    name=f'{a}-to-{b}-{mode}-{cycle}';path=folder/(name+' 中文 $; literal.txt');data=f'{name} 中文\n';path.write_bytes(b'0'*len(data.encode()))
    old=path.stat().st_ino
    if a=='host':
     if mode=='direct':path.write_text(data)
     elif mode=='inplace':
      with path.open('r+b') as f:f.write(data.encode())
     else:
      temp=Path(str(path)+'.tmp');temp.write_text(data);os.replace(temp,path)
    else:
     if mode=='direct':s='printf %s '+q(data)+' > '+q(str(path))
     elif mode=='inplace':s='printf %s '+q(data)+' | dd of='+q(str(path))+' conv=notrunc status=none'
     else:s='printf %s '+q(data)+' > '+q(str(path)+'.tmp')+' && mv -- '+q(str(path)+'.tmp')+' '+q(str(path))
     guest(a,name+'-write',s)
    read=path.read_text() if b=='host' else guest(b,name+'-read','cat -- '+q(str(path)))
    row={'from':a,'to':b,'mode':mode,'cycle':cycle,'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'uid':path.stat().st_uid,'inode_changed':path.stat().st_ino!=old,'ok':read==data and path.stat().st_uid==os.getuid() and (mode!='inplace' or path.stat().st_ino==old)}
    checks.append(row);(p.out/'replays.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2)+'\n');p.note(name,**row)
 p.note('54-oracle',ok=len(checks)==54 and all(z['ok'] for z in checks),replays=len(checks),scope='ordinary-user product project sessions on explicit shared bind; native filesystem oracle, not guest-origin control bridge')


def oss(label):
 source=LAB/'environments';manage=LAB/'management';folder=LAB/'shared-project';p=Probe(label,manage)
 text=folder/'中文 sample.txt';text.write_text('alpha 中文\nbeta space\nalpha last\n');numbers=folder/'numbers.json';numbers.write_text('[{"n":2,"name":"中文"},{"n":3,"name":"space"}]\n');bad=folder/'invalid.json';bad.write_text('{')
 def run(t,name,args,expect=0,input=None):return p.call(t+'-'+name,['project','run','shared-'+t,'--',*args],root=source,expect=expect,input=input).stdout
 def check(t,name,args,expected,expect=0,input=None):
  out=run(t,name,args,expect,input);p.note(t+'-'+name+'-oracle',ok=out==expected,stdout=out,expected=expected)
 for t in TARGETS:
  mapping={'jq':('jq','/usr/bin/jq'),'rg':('ripgrep','/usr/bin/rg'),'fd':('fd-find' if t=='debian13' else 'fd','/usr/bin/fdfind' if t=='debian13' else '/usr/bin/fd'),'bat':('bat','/usr/bin/batcat' if t=='debian13' else '/usr/bin/bat'),'sqlite':('sqlite3' if t=='debian13' else 'sqlite','/usr/bin/sqlite3'),'htop':('htop','/usr/bin/htop')}
  for alias,(pkg,exe) in mapping.items():p.js(t+'-install-'+alias,['install','--target',t,'--exec',exe,'--app',alias,'--json',pkg],timeout=1200)
  check(t,'jq-sum',['jq','-r','map(.n)|add',str(numbers)],'5\n')
  check(t,'jq-string',['jq','-r','.[0].name',str(numbers)],'中文\n')
  check(t,'jq-false',['jq','-e','false',str(numbers)],'false\n',expect=1)
  check(t,'jq-empty',['jq','-e','empty',str(numbers)],'',expect=4)
  run(t,'jq-bad',['jq','-e','.',str(bad)],expect=5)
  check(t,'rg-match',['rg','--no-heading','--color','never','-F','--','中文',str(text)],'alpha 中文\n')
  check(t,'rg-none',['rg','--','not-in-file',str(text)],'',expect=1)
  run(t,'rg-error',['rg','--','x',str(folder/'absent')],expect=2)
  fd=mapping['fd'][1];out=run(t,'fd-zero',[fd,'--max-depth','1','--type','f','--print0','--','^中文 sample\\.txt$',str(folder)]);p.note(t+'-fd-set',ok=set(out.split('\0'))=={str(text),''},stdout=out)
  check(t,'bat-bytes',[mapping['bat'][1],'--color=never','--paging=never','--style=plain','--',str(text)],text.read_text())
  db=folder/(t+' 中文.db')
  check(t,'sqlite-create',['sqlite3',str(db),"CREATE TABLE sample(n INTEGER, label TEXT); BEGIN; INSERT INTO sample VALUES(2,'中文'),(3,'space'); COMMIT; SELECT sum(n) FROM sample;"],'5\n')
  for cycle in (1,2):
   p.call(t+'-stop-db-'+str(cycle),['stop',t,'--json'])
   check(t,'sqlite-reopen-'+str(cycle),['sqlite3',str(db),'PRAGMA integrity_check; SELECT count(*),sum(n) FROM sample;'],'ok\n2|5\n')
   p.call(t+'-start-after-db-'+str(cycle),['start',t,'--json'])
  run(t,'jq-version',['jq','--version'])
  out=p.call(t+'-shortcut-run',['run','--target',t,'jq','--','-r','map(.n)|add',str(numbers)]).stdout;p.note(t+'-shortcut-oracle',ok=out=='5\n')
 p.call('ambiguous-alias',['run','jq','--','--version'],expect=1)
 p.note('noninteractive-oss-complete',ok=True,scope='6 project repository installs per target, 5 noninteractive workflows; htop/bat PTY and local-file acquisition are separate next-stage checks')


def main():
 a=argparse.ArgumentParser();a.add_argument('mode',choices=['canary','prepare','interwrite','oss','cleanup']);a.add_argument('--label',required=True);x=a.parse_args()
 if x.mode=='cleanup':
  p=Probe(x.label,LAB/'management');cleanup(p,LAB/'environments');p.js('list-after-cleanup',['list','--json']);return
 try:globals()[x.mode](x.label)
 except:
  print(traceback.format_exc(),flush=True);raise
if __name__=='__main__':main()
