#!/usr/bin/env python3
"""Inject an external-inode collision; require refusal without leaking anchors."""
import argparse,errno,json,os,sys,tempfile,traceback
from pathlib import Path
from unittest.mock import patch
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();sys.path.insert(0,str(a.source.resolve()))
from multilinux.view_backend import IdMap,ViewBackend,ViewError
from multilinux import projection
r={'source':str(a.source.resolve()),'passed':False};b=None
try:
    with tempfile.TemporaryDirectory(prefix='unifier-collision-contract-') as t:
        root=Path(t);back=root/'merged';back.mkdir();(back/'a').touch();(back/'b').touch()
        fd=os.open(back,os.O_PATH|os.O_DIRECTORY)
        try:
            b=ViewBackend(mountpoint=str(root/'view'),merged_fd=fd,merged_stat=os.fstat(fd),mounts=[],capabilities=[],
                id_map=IdMap(os.getuid(),os.getgid(),0,0,0,0,0,0,'default'),view_uuid='collision-contract')
            with patch.object(projection,'_external_inode',return_value=123):
                held=b.lookup_node(1,'a');before=len(os.listdir('/proc/self/fd'));refused=0
                for i in range(10):
                    try:b.lookup_node(1,'b')
                    except ViewError as exc:assert exc.errno==errno.EOVERFLOW,exc;refused+=1
                    else:raise AssertionError('distinct object silently aliased')
                after=len(os.listdir('/proc/self/fd'))
            b.forget_node(held.nodeid,1)
            r.update(refused=refused,before_fds=before,after_fds=after,after=b.stats())
            assert refused==10 and after==before and b.stats()['nodes']==1,r
            r['passed']=True
        finally:
            if b:b.close()
            os.close(fd)
except Exception:r['error']=traceback.format_exc()
a.out.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False))
raise SystemExit(0 if r['passed'] else 1)
