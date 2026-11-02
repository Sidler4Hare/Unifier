#!/usr/bin/env python3
"""Small-budget identity must survive churn after all node anchors release."""
import argparse,json,os,sys,tempfile,traceback
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--source',type=Path,required=True);p.add_argument('--out',type=Path,required=True)
a=p.parse_args();sys.path.insert(0,str(a.source.resolve()))
from multilinux.view_backend import IdMap,ViewBackend
r={'source':str(a.source.resolve()),'passed':False};b=None
try:
    with tempfile.TemporaryDirectory(prefix='unifier-inode-contract-') as t:
        root=Path(t);back=root/'merged';back.mkdir();(back/'original').touch()
        fd=os.open(back,os.O_PATH|os.O_DIRECTORY)
        try:
            b=ViewBackend(mountpoint=str(root/'view'),merged_fd=fd,merged_stat=os.fstat(fd),mounts=[],capabilities=[],
                id_map=IdMap(os.getuid(),os.getgid(),0,0,0,0,0,0,'default'),view_uuid='identity-contract',max_nodes=4)
            baseline=b.stats();fh=b.open_dir(1)
            listed=next(e['ino'] for e in b.readdir_handle(fh,0)[1] if e['name']=='original')
            b.release_handle(fh)
            assert b.stats()['nodes']==1,b.stats()
            for i in range(64):
                q=back/f'churn-{i}';q.touch();b.getattr('/'+q.name);q.unlink()
                assert b.stats()['nodes']==1,b.stats()
            node=b.lookup_node(1,'original');observed=b.node_attr(node)['ino'];b.forget_node(node.nodeid,1)
            r.update(listed_inode=listed,after_churn_stat_inode=observed,churn=64,baseline=baseline,after=b.stats())
            assert listed==observed,r
            assert b.stats()['nodes']==1 and b.stats()['handles']==0,r
            r['passed']=True
        finally:
            if b:b.close()
            os.close(fd)
except Exception:r['error']=traceback.format_exc()
a.out.write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n');print(json.dumps(r,ensure_ascii=False))
raise SystemExit(0 if r['passed'] else 1)
