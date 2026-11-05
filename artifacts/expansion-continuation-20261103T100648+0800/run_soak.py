import json,os,sys,time,subprocess,shlex,datetime,hashlib,traceback
from pathlib import Path
CTL=Path(__file__).resolve().parent; EVID=Path((CTL/'evidence-path').read_text())/'soak';EVID.mkdir(exist_ok=True)
SOURCE=CTL/'soak-source';ENVROOT=Path('/home/omg/文档/多兼容linux机/.lab/extension-local-20261031/environments');SHARED=Path('/home/omg/文档/多兼容linux机/.lab/extension-local-20261031/shared-project/continuation-20261103')
sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.project import load_project
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService
from unifier.resources import ResourceProbe
config,lock,_=load_project();svc=EnvironmentService(ENVROOT,PodmanRuntime(),config,lock);probe=ResourceProbe(svc)
env=dict(os.environ,UNIFIER_HOME=str(ENVROOT),PYTHONPATH=str(SOURCE/'src')+':'+str(SOURCE),PYTHONDONTWRITEBYTECODE='1')
processes={};logs={};result={'started_at':datetime.datetime.now().astimezone().isoformat(),'duration_seconds':1800,'scope':'dual-guest bounded functional persistence soak; not a controlled performance benchmark','samples':[],'status':'RUNNING'}
def save():
 p=EVID/'result.json';tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(result,ensure_ascii=False,indent=2));tmp.replace(p)
try:
 for target in ['debian13','arch']:
  db=SHARED/(target+'-soak.db');progress=SHARED/(target+'-soak.progress')
  cmd='set -eu\nn=0\nwhile [ "$n" -lt 120 ]; do\n n=$((n+1))\n sqlite3 "$1" "CREATE TABLE IF NOT EXISTS saves(n INTEGER PRIMARY KEY, marker TEXT NOT NULL); BEGIN IMMEDIATE; INSERT INTO saves VALUES($n, \'round-$n\'); COMMIT; PRAGMA integrity_check;" | grep -qx ok\n printf \'%s\\n\' "$n" > "$2.tmp"\n mv -- "$2.tmp" "$2"\n sleep 15\ndone\nsqlite3 "$1" \'PRAGMA integrity_check; SELECT count(*),sum(n) FROM saves;\'\n'
  log=(EVID/(target+'.log')).open('w');logs[target]=log
  processes[target]=subprocess.Popen([str(SOURCE/'bin/unifier'),'project','run','shared-'+target,'--','/bin/sh','-c',cmd,'soak',str(db),str(progress)],env=env,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
 save();started=time.monotonic()
 while any(p.poll() is None for p in processes.values()):
  sample={'elapsed_seconds':round(time.monotonic()-started,3),'targets':{}}
  for target,p in processes.items():
   row={'launcher_returncode':p.poll()}
   try:row['resources']=probe.sample(target)
   except Exception as ex:row['resources']='UNKNOWN';row['error']=str(ex)
   progress=SHARED/(target+'-soak.progress');row['saves']=int(progress.read_text()) if progress.exists() else None
   sample['targets'][target]=row
  result['samples'].append(sample);save()
  if time.monotonic()-started>1980:raise RuntimeError('soak deadline exceeded; retain sessions for directed cancellation')
  time.sleep(15)
 result['returncodes']={t:p.returncode for t,p in processes.items()}
 result['status']='PASS' if all(x==0 for x in result['returncodes'].values()) else 'FAIL'
 for target in processes:
  r=subprocess.run([str(SOURCE/'bin/unifier'),'project','run','shared-'+target,'--','sqlite3',str(SHARED/(target+'-soak.db')),'PRAGMA integrity_check; SELECT count(*),sum(n) FROM saves;'],env=env,capture_output=True,text=True,timeout=45)
  result.setdefault('final_oracles',{})[target]={'returncode':r.returncode,'stdout':r.stdout,'stderr':r.stderr,'ok':r.returncode==0 and r.stdout=='ok\n120|7260\n'}
 if not all(r['ok'] for r in result['final_oracles'].values()):result['status']='FAIL'
except Exception as ex:result['status']='FAIL';result['error']=str(ex);result['traceback']=traceback.format_exc()
finally:
 for log in logs.values():log.close()
 result['finished_at']=datetime.datetime.now().astimezone().isoformat();save()
print(json.dumps({k:v for k,v in result.items() if k!='samples'},ensure_ascii=False,indent=2))
