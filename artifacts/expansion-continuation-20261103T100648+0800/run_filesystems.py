import os,sys,json,subprocess,tempfile,traceback,hashlib,itertools,shutil
from pathlib import Path
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text())/'filesystems';OUT.mkdir(exist_ok=True)
SOURCE=CTL/'soak-source';sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.registry import DataRootRegistry
from unifier.file_operations import FileOperations
from unifier.errors import UnifierError
base=CTL/'fs-fixture';base.mkdir();btrfs=base/'btrfs';btrfs.mkdir();mount=base/'ext4';mount.mkdir();tmpfs=Path(tempfile.mkdtemp(prefix='unifier-fs-',dir='/dev/shm'));image=base/'ext4.img'
result={'status':'RUNNING','scope':'host managed ordinary-file operations over Btrfs, tmpfs and userspace fuse2fs ext4 image; no kernel loop mount or guest bind acceptance','checks':[]};mounted=False

def save(): (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
def check(name,ok,**data):
 row={'name':name,'ok':bool(ok),**data};result['checks'].append(row);save()
 if not ok:raise RuntimeError(name+' failed')
def call(argv):
 r=subprocess.run(argv,capture_output=True,text=True,timeout=30)
 if r.returncode:raise RuntimeError(str(argv[0])+': '+r.stderr[-700:])
 return r.stdout
try:
 with image.open('xb') as f:f.truncate(96*1024*1024)
 result['mkfs']=call(['mkfs.ext4','-F','-q',str(image)])
 result['fuse2fs']=call(['fuse2fs',str(image),str(mount),'-o','fakeroot']);mounted=True
 result['mounts']={}
 roots={'btrfs':btrfs,'tmpfs':tmpfs,'ext4-fuse':mount}
 registry=DataRootRegistry(base/'state');ops=FileOperations(base/'state',registry)
 records={name:registry.add(str(p.resolve()),cleanup_policy='controlled') for name,p in roots.items()}
 for name,path in roots.items():
  result['mounts'][name]=call(['findmnt','--json','-T',str(path)])
  for filename,data in [('empty.txt',b''),('中文 space $; trailing ', 'roundtrip 中文\n'.encode()),('block.bin',bytes(range(256))*512)]: (path/filename).write_bytes(data)
 for source,target in itertools.permutations(roots,2):
  for n,filename in enumerate(['empty.txt','中文 space $; trailing ','block.bin']):
   dest=source+'-'+str(n)+'.copy';r=ops.copy_file(records[source]['root_uuid'],filename,records[target]['root_uuid'],dest)
   check(source+'->'+target+':'+str(n), (roots[target]/dest).read_bytes()==(roots[source]/filename).read_bytes(),state=r.get('state'),source=source,target=target,bytes=(roots[target]/dest).stat().st_size)
 for source,target in [('btrfs','ext4-fuse'),('ext4-fuse','tmpfs'),('tmpfs','btrfs')]:
  name='move-'+source+'.txt';data=('move '+source).encode();(roots[source]/name).write_bytes(data)
  r=ops.move_file(records[source]['root_uuid'],name,records[target]['root_uuid'],name,cleanup_source=True)
  check('controlled-move:'+source+'->'+target,not (roots[source]/name).exists() and (roots[target]/name).read_bytes()==data,state=r.get('state'))
 # Existing destination is a conflict and preserves both original byte strings.
 (btrfs/'conflict-source').write_text('source');(mount/'conflict-target').write_text('keep')
 try:r=ops.copy_file(records['btrfs']['root_uuid'],'conflict-source',records['ext4-fuse']['root_uuid'],'conflict-target');refused=r.get('state') not in {'COMPLETED','completed','COMMITTED'}
 except UnifierError:refused=True
 check('existing-destination-preserved',refused and (mount/'conflict-target').read_text()=='keep' and (btrfs/'conflict-source').read_text()=='source')
 (btrfs/'symlink').symlink_to('block.bin')
 try:ops.copy_file(records['btrfs']['root_uuid'],'symlink',records['tmpfs']['root_uuid'],'link-copy');refused=False
 except UnifierError:refused=True
 check('symlink-refused',refused and not (tmpfs/'link-copy').exists())
 acl=btrfs/'acl-source';acl.write_text('acl');call(['setfacl','-m','u:65534:r',str(acl)])
 try:ops.copy_file(records['btrfs']['root_uuid'],'acl-source',records['tmpfs']['root_uuid'],'acl-copy');refused=False
 except UnifierError:refused=True
 check('complex-acl-refused',refused and not (tmpfs/'acl-copy').exists())
 result['status']='PASS_BOUNDED_FILESYSTEM_MATRIX'
except Exception as ex:result['status']='FAIL';result['error']=str(ex);result['traceback']=traceback.format_exc()
finally:
 if mounted:
  try:result['unmount']=call(['fusermount3','-u',str(mount)]);result['unmounted']=True
  except Exception as ex:result['unmounted']=False;result['unmount_error']=str(ex)
 # Only remove this exact private tmpfs path after all operations and logs settled.
 if str(tmpfs).startswith('/dev/shm/unifier-fs-'):
  shutil.rmtree(tmpfs);result['tmpfs_removed']=True
 save()
print(json.dumps({k:v for k,v in result.items() if k not in ['mounts']},ensure_ascii=False,indent=2))
