"""Bounded original-CID SH01..08 acceptance through product APP workers."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

ROOT=Path(__file__).resolve().parents[3]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from tools.reuse_test_guard import make_service,get,info,process,same_execution

spec=Path(os.environ['UNIFIER_TEST_SPEC']);config=get(spec);out=spec.parent
service=make_service(spec);entry=out.parent/'guarded_entry.py'
share=Path(config['shared'])/'acceptance-1106'
share.mkdir(mode=0o700,exist_ok=config.get("reuse_registered_apps",False))
apps={tag:'accept1106-'+tag.lower() for tag in ('A','B','C','BAD')}
children=[]
result={'status':'RUNNING','cases':[],'cid':config['cid'],
        'registrations':apps,'shared_inputs':str(share)}

PAYLOAD='''import json,os,sys,time
from pathlib import Path
tag,call=sys.argv[1],sys.argv[2]
directory=Path.cwd();home=Path('/tmp/unifier-accept1106-'+tag+'-'+call)
home.mkdir(mode=0o700,exist_ok=True)
os.environ.update(HOME=str(home),XDG_CONFIG_HOME=str(home/'config'),XDG_CACHE_HOME=str(home/'cache'))
raw=Path('/proc/self/stat').read_text().rsplit(')',1)[1].split()
value={'tag':tag,'call':call,'pid':os.getpid(),'starttime':raw[19],
       'uid':os.getuid(),'cwd':str(directory),'environment':os.environ['UNIFIER_TEST_CASE'],
       'counter':0,'done':False}
end=time.monotonic()+90
while time.monotonic()<end:
 value['counter']+=1;value['done']=(directory/(call+'.finish')).exists()
 tmp=directory/(call+'.tmp');tmp.write_text(json.dumps(value));os.replace(tmp,directory/(call+'.json'))
 if value['done']:raise SystemExit(0)
 time.sleep(.15)
raise SystemExit(23)
'''

def save():
    (out/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))

def passed(case,**evidence):
    result['cases'].append({'id':case,'status':'PASS','execution':'live Debian original CID',
                            'evidence':evidence})
    save()

def until(check,seconds=30):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        value=check()
        if value:return value
        time.sleep(.1)
    raise RuntimeError('bounded oracle wait expired')

def lease():
    return get(Path(config['project'])/'.lab/leases'/(config['env_id']+'.json'))

def launch(tag,call):
    call += config.get("call_suffix", "")
    directory=share/tag
    log=(out/(tag+'-'+call+'.log')).open('wb')
    child=subprocess.Popen([sys.executable,str(entry),'app','open',apps[tag],'--',call],
        stdout=log,stderr=subprocess.STDOUT,env=os.environ.copy())
    value={'tag':tag,'call':call,'process':child,'identity':process(child.pid),
           'directory':directory,'log':log}
    children.append(value)
    return value

def state(child):
    path=child['directory']/(child['call']+'.json')
    return get(path) if path.exists() else None

def ready(child):
    return until(lambda:state(child))

def finish(child,expected=0):
    (child['directory']/(child['call']+'.finish')).write_text('finish')
    if child['process'].poll() is None:child['process'].wait(timeout=30)
    assert child['process'].returncode==expected,(child['tag'],child['process'].returncode)
    return until(lambda:(state(child) or {}).get('done'))

def stopped():
    return until(lambda:not info(config)['State']['Running'])

def history(name,start,end):
    since=get(out/'event-since.json')['since']
    native=service.runtime.runner(['podman','events','--stream=false','--since',since,
        '--filter','type=container','--format','json'],capture=True,timeout=15)
    assert native.returncode==0,native.stderr
    (out/(name+'-events.jsonl')).write_text(native.stdout)
    events=[json.loads(line) for line in native.stdout.splitlines() if line]
    subset=[e for e in events if start<=e.get('timeNano',0)<=end]
    assert not [e for e in events if e.get('ID')!=config['cid'] and e.get('Status')
                in ('create','start','restart','exec','exec_create','exec_start')]
    starts=[e for e in subset if e.get('ID')==config['cid'] and e.get('Status')=='start']
    assert len(starts)==1,{'phase':name,'starts':len(starts),'events':subset}
    return {'start_events':len(starts),'target_events':len(subset)}

try:
    for tag,app in apps.items():
        record=service.store.load_launch(app)
        wanted=(['/usr/bin/python3','--unifier-deliberately-invalid-option'] if tag=='BAD'
                else ['/usr/bin/python3','-c',PAYLOAD,tag])
        assert record['argv']==wanted and record['workdir']==str(share/tag),record
        assert record['environment_binding']['container_id']==config['cid']
    assert not info(config)['State']['Running'] and not lease()['leases']
    first=time.time_ns()
    a=launch('A','shared1');sa=ready(a);cold=info(config)['State']['StartedAt']
    b=launch('B','shared1');sb=ready(b)
    assert info(config)['State']['StartedAt']==cold
    passed('SH01',started_at=cold,environment_binding=service.store.load(config['env_id'])['runtime'])
    c=launch('C','shared1');sc=ready(c)
    assert len(lease()['leases'])==3,lease()
    for child,record in ((a,sa),(b,sb),(c,sc)):
        assert record['tag']==child['tag'] and record['environment']==child['tag']
        assert record['cwd']==str(child['directory']) and record['uid']==1000,record
    passed('SH02',contexts=[sa,sb,sc],lease_tokens=list(lease()['leases']))
    finish(a);before_b=state(b);time.sleep(.4);after_b=state(b)
    assert len(lease()['leases'])==2 and info(config)['State']['Running']
    assert before_b['pid']==after_b['pid'] and before_b['starttime']==after_b['starttime']
    assert after_b['counter']>before_b['counter']
    passed('SH03',surviving=after_b,lease_tokens=list(lease()['leases']))
    bad=launch('BAD','invalid');bad['process'].wait(timeout=30)
    assert bad['process'].returncode!=0
    assert len(lease()['leases'])==2 and info(config)['State']['Running']
    passed('SH08-live',bad_exit=bad['process'].returncode,surviving=state(b))
    finish(b);assert info(config)['State']['Running'];finish(c);stopped()
    assert not lease()['leases']
    events=history('shared1',first,time.time_ns())
    result['cases'][0]['evidence'].update(events)
    passed('SH04',leases=lease(),**events)
    first=time.time_ns()
    a=launch('A','context2');sa=ready(a);b=launch('B','context2');sb=ready(b)
    assert sa['environment']!=sb['environment'] and sa['cwd']!=sb['cwd'] and sa['pid']!=sb['pid']
    passed('SH05',contexts=[sa,sb])
    assert same_execution(a['identity'],process(a['process'].pid))
    descriptor=os.pidfd_open(a['process'].pid)
    signal.pidfd_send_signal(descriptor,signal.SIGKILL);os.close(descriptor)
    a['process'].wait(timeout=5)
    before=state(a);time.sleep(.5);after=state(a)
    assert after['counter']>before['counter'] and len(lease()['leases'])==2
    assert info(config)['State']['Running']
    (a['directory']/(a['call']+'.finish')).write_text('finish')
    until(lambda:(state(a) or {}).get('done'));until(lambda:len(lease()['leases'])==1)
    assert info(config)['State']['Running'] and state(b)['pid']==sb['pid']
    finish(b);stopped()
    passed('SH06',launcher_exit=a['process'].returncode,live_after_launcher_loss=after,
           **history('context2',first,time.time_ns()))
    service.start(config['env_id'])
    a=launch('A','manual3');ready(a);finish(a)
    assert not lease()['leases'] and info(config)['State']['Running']
    passed('SH07',leases=lease(),remained_running=True)
    service.stop(config['env_id']);stopped();result['status']='PASS';save()
except Exception as exc:
    result.update(status='FAIL',error=str(exc),traceback=traceback.format_exc());save()
    raise
finally:
    for child in children:
        if child['tag']!='BAD':
            (child['directory']/(child['call']+'.finish')).write_text('finish')
    for child in children:
        if child['process'].poll() is None:
            try:child['process'].wait(timeout=10)
            except subprocess.TimeoutExpired:pass
        child['log'].close()
print(json.dumps(result,ensure_ascii=False))
