import os,sys,json,time
from pathlib import Path
def pairs(p):
 try:return {s.split()[0]:int(s.split()[1]) for s in Path(p).read_text().splitlines() if len(s.split())==2}
 except OSError:return None
def snapshot(cg):
 p=Path(cg);pids=set();errors=[];rows=[]
 for f in p.rglob('cgroup.procs'):
  try:pids.update(map(int,f.read_text().split()))
  except OSError as e:errors.append(str(e))
 for pid in sorted(pids):
  try:
   proc=Path('/proc')/str(pid);exe=os.readlink(proc/'exe');sm={s.split(':')[0]:int(s.split()[1])*1024 for s in (proc/'smaps_rollup').read_text().splitlines() if s.partition(':')[0].replace('_','').isalpha()};st=(proc/'status').read_text()
   rows.append({'pid':pid,'exe':exe,'app':'/office6/' in exe,'pss':sm.get('Pss'),'swap_pss':sm.get('SwapPss'),'pss_anon':sm.get('Pss_Anon'),'pss_file':sm.get('Pss_File'),'threads':int(next(x for x in st.splitlines() if x.startswith('Threads:')).split()[1])})
  except FileNotFoundError:pass
  except (OSError,StopIteration) as e:errors.append({'pid':pid,'error':str(e)})
 simple={}
 for k in ['memory.current','memory.peak','memory.swap.current','pids.current','pids.peak']:
  try:simple[k]=int((p/k).read_text())
  except OSError:simple[k]=None
 return {'at':time.time(),'rows':rows,'errors':errors,'cgroup':simple,'cpu':pairs(p/'cpu.stat'),'memory_stat':pairs(p/'memory.stat'),'memory_events':pairs(p/'memory.events'),'pids_events':pairs(p/'pids.events'),'host_available':int(next(x for x in Path('/proc/meminfo').read_text().splitlines() if x.startswith('MemAvailable:')).split()[1])*1024,'host_psi':Path('/proc/pressure/memory').read_text()}
if __name__=='__main__':
 for _ in range(int(sys.argv[3])):
  with Path(sys.argv[2]).open('a') as out:out.write(json.dumps(snapshot(sys.argv[1]))+'\n')
  time.sleep(1)
