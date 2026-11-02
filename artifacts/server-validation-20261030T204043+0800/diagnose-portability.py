import fcntl,json,os,platform,sys,tempfile,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path.cwd()/'src'));sys.path.insert(0,str(Path.cwd()/'tests'))
from test_projection_support import fixture
from unifier import fuse_view
report={'python':platform.python_version(),'kernel':platform.release(),'hostname':platform.node(),'fcntl_symbols':{name:getattr(fcntl,name,None) for name in ('F_SETOWN_EX','F_SETSIG','F_SETLEASE','F_GETLEASE')},'raw_metadata_trials':{},'lookup_trials':[]}
for delay in (0,0.025):
    collisions=0
    with tempfile.TemporaryDirectory() as tmp:
        for i in range(50):
            p=Path(tmp)/str(i);p.write_bytes(b'O'*4096);before=p.stat()
            if delay:time.sleep(delay)
            p.write_bytes(b'N'*4096);os.utime(p,ns=(before.st_atime_ns,before.st_mtime_ns));after=p.stat()
            collisions+=((before.st_size,before.st_mtime_ns,before.st_ctime_ns)==(after.st_size,after.st_mtime_ns,after.st_ctime_ns))
    report['raw_metadata_trials'][str(delay)]={'trials':50,'unchanged_signature_despite_changed_bytes':collisions}
for delay in (0,0.025):
    for i in range(10):
        with fixture() as (root,backend,server):
            p=root/'file';p.write_bytes(b'O'*4096);node=server.lookup('file');before=p.stat()
            if delay:time.sleep(delay)
            p.write_bytes(b'N'*4096);os.utime(p,ns=(before.st_atime_ns,before.st_mtime_ns));after=p.stat()
            with patch.object(server,'_notify',return_value=False):
                reply=server.call(fuse_view.FUSE_LOOKUP,1,b'file\0')
            report['lookup_trials'].append({'delay':delay,'ctime_before_ns':before.st_ctime_ns,'ctime_after_ns':after.st_ctime_ns,'reply_errno':reply[2],'nlookup':backend.get_node(node).nlookup})
print(json.dumps(report,indent=2))
