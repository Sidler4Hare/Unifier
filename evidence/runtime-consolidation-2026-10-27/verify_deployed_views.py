"""Mount both deployed applications' stopped environments using their own CLI."""
from pathlib import Path
import errno
import json
import os
import subprocess
import tempfile
import time
import uuid

RUN=Path(__file__).resolve().parent


def cli(app,*args):
    p=subprocess.run(['/usr/bin/python3',str(app/'tools/ml.py'),*args],capture_output=True,text=True,timeout=90)
    return p


def main():
    results=[]
    for name,env_id,expected in [('wemeet','meeting',b'ID=debian'),('freecad','freecad-arch',b'ID=arch')]:
        app=Path.home()/'.local/share/multilinux/apps'/name
        mount=Path(tempfile.gettempdir())/('unifier-consolidation-'+uuid.uuid4().hex)
        p=cli(app,'view','open',env_id,'--mountpoint',str(mount))
        assert p.returncode==0,p.stderr
        view=json.loads(p.stdout)
        (RUN/(name+'-view-open.json')).write_text(p.stdout)
        try:
            data=(mount/'etc/os-release').read_bytes()
            assert expected in data
            assert (mount/'etc/os-release').stat().st_size==len(data)
            with os.scandir(mount/'etc') as iterator:
                entries=[(e.name,e.inode()) for e in iterator]
            mismatches=[n for n,i in entries if os.stat(mount/'etc'/n,follow_symlinks=False).st_ino!=i]
            assert not mismatches,mismatches[:5]
            rejected=False
            try:
                fd=os.open(mount/('.unifier-readonly-'+uuid.uuid4().hex),os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            except OSError as exc:
                assert exc.errno==errno.EROFS,exc
                rejected=True
            else:
                os.close(fd)
            assert rejected
            blocked=cli(app,'env','start',env_id)
            assert blocked.returncode!=0 and 'view' in blocked.stderr
            result={'application':name,'real_fuse_mounted':True,'distribution_content_correct':True,
                    'content_size_correct':True,'readdir_inode_matches_stat_entries':len(entries),
                    'readonly_write_errno':errno.EROFS,'start_while_view_active_rejected':True,
                    'view_uuid':view['view_uuid'],'mountpoint':str(mount)}
        finally:
            attempts=[]
            for _ in range(3):
                revoked=cli(app,'view','revoke',view['view_uuid'])
                attempts.append({'exit':revoked.returncode,'stdout':revoked.stdout,'stderr':revoked.stderr})
                if revoked.returncode==0:break
                time.sleep(.3)
            (RUN/(name+'-view-revoke.json')).write_text(json.dumps(attempts,ensure_ascii=False,indent=2)+'\n')
            assert revoked.returncode==0,revoked.stderr
            assert not os.path.ismount(mount)
        result['normal_revoke_complete']=True
        results.append(result)
        (RUN/'deployed-view-validation.json').write_text(json.dumps({'status':'PASSED' if len(results)==2 else 'IN_PROGRESS','results':results},ensure_ascii=False,indent=2)+'\n')
        print(json.dumps({'application':name,'deployed_real_fuse':'passed','entries':len(entries)},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
