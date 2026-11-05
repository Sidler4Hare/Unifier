import os,json,time,collections
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text());HZ=os.sysconf('SC_CLK_TCK');UID=os.getuid()
def txt(p):
 try:return Path(p).read_text()
 except OSError:return None
def mem():return {l.split(':')[0]:int(l.split()[1])*1024 for l in Path('/proc/meminfo').read_text().splitlines() if len(l.split())>=3 and l.split()[2]=='kB'}
def psi():return {kind:txt('/proc/pressure/'+kind) for kind in ['memory','cpu','io']}
def vms():return {x.split()[0]:int(x.split()[1]) for x in Path('/proc/vmstat').read_text().splitlines()}
def processes():
 out={}
 for p in Path('/proc').iterdir():
  if not p.name.isdigit():continue
  try:
   if p.stat().st_uid!=UID:continue
   raw=(p/'stat').read_text();end=raw.rfind(')');fields=raw[end+2:].split();status={line.split(':',1)[0]:line.split(':',1)[1].strip() for line in (p/'status').read_text().splitlines() if ':' in line}
   out[int(p.name)]={'pid':int(p.name),'ppid':int(fields[1]),'comm':raw[raw.find('(')+1:end],'start':int(fields[19]),'ticks':int(fields[11])+int(fields[12]),'rss_bytes':int(status.get('VmRSS','0 kB').split()[0])*1024,'threads':int(status['Threads'])}
  except (OSError,ValueError,KeyError):continue
 return out
before=processes();start=time.monotonic();initial={'memory':mem(),'psi':psi(),'vmstat':vms()};time.sleep(5);after=processes();duration=time.monotonic()-start
z=txt('/sys/block/zram0/mm_stat');zv=list(map(int,z.split())) if z else []
rootset={pid for pid,r in after.items() if r['comm'] in ['ChatGPT','codex']}
def group(r):
 if r['pid']==os.getpid():return 'measurement'
 if r['comm'] in ['conmon','podman','pasta','catatonit','podman-init']:return 'container-runtime'
 cmd=txt('/proc/'+str(r['pid'])+'/cmdline') or ''
 if any(x in cmd for x in ['src/unifier','tools/unifier.py','bin/unifier','.lab/agents/expansion-continuation']):return 'unifier-controller'
 if '@deepseek-ai/dsh' in cmd:return 'dsh-client'
 pid=r['pid'];seen=set()
 while pid in after and pid not in seen:
  if pid in rootset:return 'codex-app-and-helpers'
  seen.add(pid);pid=after[pid]['ppid']
 if r['comm'] in ['kwin_wayland','qs','kos-platform','plasmashell','Xwayland','kded6','xdg-desktop-por','xdg-document-po','xdg-permission-']:return 'desktop-core-and-portals'
 return 'other-user-apps'
for r in after.values():
 r['group']=group(r);old=before.get(r['pid']);r['cpu_one_core_percent']=round(100*(r['ticks']-old['ticks'])/HZ/duration,3) if old and old['start']==r['start'] else None
selected=sorted(after.values(),key=lambda x:x['rss_bytes'],reverse=True)[:120]
for r in selected:
 raw=txt('/proc/'+str(r['pid'])+'/smaps_rollup')
 if raw is None:r['pss_bytes']=None;r['swap_pss_bytes']=None;continue
 fields={l.split(':')[0]:int(l.split()[1])*1024 for l in raw.splitlines() if ':' in l and len(l.split())>=3 and l.split()[2]=='kB'};r['pss_bytes']=fields.get('Pss');r['swap_pss_bytes']=fields.get('SwapPss')
groups={}
for r in selected:
 g=groups.setdefault(r['group'],{'measured_processes':0,'pss_partial_bytes':0,'swap_pss_partial_bytes':0,'unreadable_pss':0,'cpu_one_core_percent':0.,'threads':0})
 g['measured_processes']+=1;g['pss_partial_bytes']+=r['pss_bytes'] or 0;g['swap_pss_partial_bytes']+=r['swap_pss_bytes'] or 0;g['unreadable_pss']+=r['pss_bytes'] is None;g['cpu_one_core_percent']+=r['cpu_one_core_percent'] or 0;g['threads']+=r['threads']
final=vms();r={'captured_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds'),'scope':'Read-only observational snapshot with audit/Codex active; not an isolated benchmark. No guest was launched.','boot_id':txt('/proc/sys/kernel/random/boot_id').strip(),'duration_seconds':duration,'memory_before':initial['memory'],'memory_after':mem(),'psi_before':initial['psi'],'psi_after':psi(),'vmstat_delta':{k:final[k]-initial['vmstat'][k] for k in ['pswpin','pswpout','pgmajfault','pgscan_direct','pgscan_kswapd','compact_stall','allocstall_normal'] if k in initial['vmstat'] and k in final},'zram':{'original_data_bytes':zv[0] if zv else None,'compressed_data_bytes':zv[1] if zv else None,'physical_allocated_bytes':zv[2] if zv else None,'historical_physical_peak_bytes':zv[4] if zv else None,'compression_algorithm':txt('/sys/block/zram0/comp_algorithm'),'logical_capacity_bytes':int(txt('/sys/block/zram0/disksize'))},'swap_devices':txt('/proc/swaps'),'own_uid_process_count':len(after),'pss_selected_processes':len(selected),'pss_unselected_processes':len(after)-len(selected),'groups':groups,'top_processes':sorted(selected,key=lambda x:x['pss_bytes'] or 0,reverse=True)[:25],'accounting':'PSS and cgroup memory overlap; do not add them. SwapPss is logical swapped memory, not physical zram allocation. Unreadable PSS remains null.'}
(OUT/'current-host.json').write_text(json.dumps(r,ensure_ascii=False,indent=2));print(json.dumps({'captured_at':r['captured_at'],'available_GiB':round(r['memory_after']['MemAvailable']/2**30,2),'zram_physical_GiB':round(r['zram']['physical_allocated_bytes']/2**30,2),'groups':groups,'coverage':[r['pss_selected_processes'],r['own_uid_process_count']]},ensure_ascii=False,indent=2))
