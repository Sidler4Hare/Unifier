import sys,os,json,subprocess,traceback,hashlib
from pathlib import Path
CTL=Path(__file__).resolve().parent;OUT=Path((CTL/'evidence-path').read_text())/'enospc';OUT.mkdir();SOURCE=CTL/'soak-source';sys.path[:0]=[str(SOURCE/'src'),str(SOURCE)]
from unifier.registry import DataRootRegistry
from unifier.file_operations import FileOperations
from unifier.errors import UnifierError
base=CTL/'enospc-fixture';base.mkdir();src=base/'source';src.mkdir();dst=base/'mount';dst.mkdir();image=base/'tiny-ext4.img';result={'status':'RUNNING','scope':'bounded managed copy failure and recovery on own 8MiB fuse2fs image'};mounted=False
try:
 with image.open('xb') as f:f.truncate(8*1024*1024)
 subprocess.run(['mkfs.ext4','-F','-q',str(image)],check=True,capture_output=True)
 subprocess.run(['fuse2fs',str(image),str(dst),'-o','fakeroot'],check=True,capture_output=True);mounted=True
 large=src/'dense.bin'
 with large.open('xb') as f:
  for _ in range(12):f.write(os.urandom(1024*1024))
 before=hashlib.sha256(large.read_bytes()).hexdigest();reg=DataRootRegistry(base/'state');a=reg.add(str(src));b=reg.add(str(dst));ops=FileOperations(base/'state',reg)
 try:r=ops.copy_file(a['root_uuid'],'dense.bin',b['root_uuid'],'dense.bin');result['operation']=r
 except UnifierError as ex:result['error']=str(ex)
 result['source_sha256_unchanged']=hashlib.sha256(large.read_bytes()).hexdigest()==before
 result['destination_not_published']=not (dst/'dense.bin').exists()
 (src/'small').write_bytes(b'after-enospc\n');r2=ops.copy_file(a['root_uuid'],'small',b['root_uuid'],'small');result['next_copy_state']=r2.get('state');result['next_copy_bytes_ok']=(dst/'small').read_bytes()==b'after-enospc\n'
 result['status']='PASS_EXPECTED_ENOSPC_AND_RECOVERY' if result['source_sha256_unchanged'] and result['destination_not_published'] and result['next_copy_bytes_ok'] else 'FAIL'
except Exception as ex:result['status']='FAIL';result['fatal_error']=str(ex);result['traceback']=traceback.format_exc()
finally:
 if mounted:
  r=subprocess.run(['fusermount3','-u',str(dst)],capture_output=True,text=True,timeout=15);result['unmounted']=r.returncode==0;result['unmount_error']=r.stderr
 (OUT/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in result.items() if k!='operation'},ensure_ascii=False,indent=2))
