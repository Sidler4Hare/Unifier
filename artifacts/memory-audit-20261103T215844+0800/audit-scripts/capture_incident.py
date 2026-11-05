import json,subprocess,re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text())
def query(args):
 r=subprocess.run(['journalctl','-b','-1','--no-pager','-o','json',*args],capture_output=True,text=True,timeout=30);assert r.returncode==0,r.stderr;return [json.loads(l) for l in r.stdout.splitlines() if l.startswith('{')]
def compact(x):
 us=int(x['__REALTIME_TIMESTAMP']);return {'at':datetime.fromtimestamp(us/1e6,ZoneInfo('Asia/Shanghai')).isoformat(timespec='microseconds'),'monotonic_us':x.get('__MONOTONIC_TIMESTAMP'),'boot_id':x.get('_BOOT_ID'),'comm':x.get('_COMM'),'message':x.get('MESSAGE')}
window=['--since','2026-11-03 10:00:00','--until','2026-11-03 10:42:00'];kernel=query(['-k',*window]);oomd=query(['_COMM=systemd-oomd',*window]);kwin=query(['_COMM=kwin_wayland',*window]);prior=query(['_COMM=kwin_wayland','--until','2026-11-03 10:00:00','--grep','^atomic commit failed','-n','8'])
oom=[x for x in kernel if re.search(r'invoked oom-killer|Out of memory:|Memory cgroup out of memory:|Killed process [0-9]+|oom-kill:|oom_reaper:',x.get('MESSAGE',''))]
allocation=[x for x in kernel if 'page allocation failure:' in x.get('MESSAGE','')];selected=[]
for x in kernel:
 m=x.get('MESSAGE','')
 if ('2026-11-03T10:29:04' in compact(x)['at'] and not re.search(r'authenticate|associate|local address|RX Assoc|Limiting TX power|Unhandled alg',m)) or re.search(r'PM: suspend (entry|exit)|page allocation failure|Tx queue alloc failed',m):selected.append(compact(x))
(OUT/'kernel-allocation-excerpt.json').write_text(json.dumps(selected,ensure_ascii=False,indent=2));(OUT/'kwin-timeline.json').write_text(json.dumps([compact(x) for x in kwin if re.search(r'atomic commit failed|main thread was hanging|No backend specified',x.get('MESSAGE',''))],ensure_ascii=False,indent=2));(OUT/'prior-kwin-errors.json').write_text(json.dumps([compact(x) for x in prior],ensure_ascii=False,indent=2))
(OUT/'incident-journal-summary.json').write_text(json.dumps({'window':window,'kernel_records_scanned':len(kernel),'kernel_oom_kill_records':[compact(x) for x in oom],'oomd_process_records':[compact(x) for x in oomd],'actual_page_allocation_failures':[compact(x) for x in allocation],'prior_same_boot_kwin_errors':[compact(x) for x in prior],'negative_scope':'Only the retained journal in the specified previous-boot window; absence is not proof of no unlogged failure.'},ensure_ascii=False,indent=2));print(json.dumps({'kernel_records':len(kernel),'oom_kill_records':len(oom),'oomd_records':len(oomd),'allocation_failures':len(allocation),'prior_kwin_atomic_errors':len(prior)}))
