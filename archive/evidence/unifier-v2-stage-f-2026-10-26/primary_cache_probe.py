"""Independent real-view cache/rename/churn observations, no product edits."""
import argparse,ctypes,hashlib,json,mmap,os,select,sys,time,tomllib,traceback
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--project',type=Path,required=True);p.add_argument('--environment',required=True);p.add_argument('--share',type=Path,required=True);p.add_argument('--output',type=Path,required=True);p.add_argument('--paths',type=int,default=12000);a=p.parse_args()
if a.output.exists():raise SystemExit('output exists')
sys.path.insert(0,str(a.project/'src'))
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.views import ViewManager,is_mounted
service=EnvironmentService(a.project,PodmanRuntime(),tomllib.loads((a.project/'config/targets.toml').read_text()),json.loads((a.project/'config/images.lock.json').read_text()))
manager=ViewManager(a.project,service)
base=a.share/'primary-cache';base.mkdir(exist_ok=False)
old=b'A'*8192;new=b'B'*8192
file=base/'replace.bin';file.write_bytes(old)
view=None;fds=[];report={'completed':False,'cases':{},'churn':[],'scope':'external mutation of authorized shared bind only; no live rootfs mutation; bounded unique-path traversal'}
def save():a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
def memory(pid):
 r={'pid':pid,'time':time.time()}
 try:
  r['start']=int(Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[19]); text=Path(f'/proc/{pid}/smaps_rollup').read_text();r['pss_kib']=int(next(x.split()[1] for x in text.splitlines() if x.startswith('Pss:')));r['fds']=len(list(Path(f'/proc/{pid}/fd').iterdir()))
 except Exception as e:r['error']=str(e);r['pss_kib']=None
 return r
try:
 view=manager.open(a.environment);report['view']=view;save()
 mapped=Path(view['mountpoint'])/str(base).lstrip('/')
 target=mapped/'replace.bin'
 # Hold an unread FD across atomic external rename: ordinary filesystem keeps old inode.
 fd=os.open(target,os.O_RDONLY);fds.append(fd)
 tmp=base/'next.bin';tmp.write_bytes(new);os.replace(tmp,file)
 read=os.pread(fd,8192,0)
 report['cases']['open_fd_across_replace']={'expected':'old inode A bytes','observed_sha256':hashlib.sha256(read).hexdigest(),'old_sha256':hashlib.sha256(old).hexdigest(),'new_sha256':hashlib.sha256(new).hexdigest(),'posix_old_inode_preserved':read==old}
 os.close(fd);fds.remove(fd)
 time.sleep(1.2)
 report['cases']['reopen_after_expiry']={'new_bytes':target.read_bytes()==new}
 with target.open('rb') as f:
  with mmap.mmap(f.fileno(),0,access=mmap.ACCESS_READ) as mm:report['cases']['mmap']={'bytes_match':mm[:]==new}
 libc=ctypes.CDLL(None,use_errno=True);ino=libc.inotify_init1(os.O_NONBLOCK|os.O_CLOEXEC)
 if ino<0:report['cases']['external_inotify']={'status':'unavailable','errno':ctypes.get_errno()}
 else:
  fds.append(ino);wd=libc.inotify_add_watch(ino,os.fsencode(mapped),0x100|0x80|0x8|0x200)
  (base/'event.txt').write_text('event')
  ready=select.select([ino],[],[],1)[0];data=os.read(ino,65536) if ready else b''
  report['cases']['external_inotify']={'watch_registered':wd>=0,'external_event_delivered':bool(data),'raw_hex':data.hex(),'interpretation':'external backend changes do not imply FUSE invalidation events'}
  os.close(ino);fds.remove(ino)
 report['churn'].append({'unique_paths':0,**memory(view['worker_pid'])});save()
 for batch in range(0,a.paths,2000):
  start=time.monotonic()
  for index in range(batch,min(batch+2000,a.paths)):
   name=f'churn-{index:06d}';src=base/name;src.write_bytes(b'x');assert (mapped/name).read_bytes()==b'x';src.unlink()
  report['churn'].append({'unique_paths':min(batch+2000,a.paths),'seconds':time.monotonic()-start,**memory(view['worker_pid'])});save()
 time.sleep(3);report['churn'].append({'unique_paths':a.paths,'after_idle_seconds':3,**memory(view['worker_pid'])})
 report['completed']=True
except BaseException:
 report['error']=traceback.format_exc()
finally:
 for fd in fds:
  try:os.close(fd)
  except OSError:pass
 if view:
  try:report['cleanup']=manager.revoke(view['view_uuid']);report['unmounted']=not is_mounted(view['mountpoint'])
  except BaseException:report['cleanup_error']=traceback.format_exc()
 save()
print(json.dumps({'completed':report['completed'],'cases':report['cases'],'churn':report['churn'],'unmounted':report.get('unmounted'),'error':report.get('error')}))
raise SystemExit(0 if report['completed'] and report.get('unmounted') else 1)
