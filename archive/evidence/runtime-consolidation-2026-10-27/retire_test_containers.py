"""Remove only the 25 explicitly inventoried, stopped historical test CIDs."""
from pathlib import Path
import hashlib
import json
import shutil
import subprocess

RUN=Path(__file__).resolve().parent
CTX=json.loads((RUN/'context.json').read_text())
ROOT,BACKUP=Path(CTX['root']),Path(CTX['backup'])


def main():
    assert json.loads((BACKUP/'backup-verified.json').read_text())['every_image_config_and_layer_verified']
    original=json.loads((BACKUP/'containers-before.json').read_text())
    checkpoints={x['container_id'] for x in json.loads((BACKUP/'checkpoints.json').read_text())}
    candidates=[i for i in original if (i['Config']['Labels'].get('multilinux.project')==str(ROOT)
                or i['Config']['Labels'].get('multilinux.project','').startswith(str(ROOT)+'/evidence/'))]
    assert len(candidates)==25
    removed=[]
    for old in candidates:
        cid=old['Id'];assert cid in checkpoints
        info=json.loads(subprocess.run(['podman','inspect',cid],capture_output=True,text=True,check=True).stdout)[0]
        assert info['State']['Status']!='running'
        assert info['State']['StartedAt']==old['State']['StartedAt']
        assert info['State']['FinishedAt']==old['State']['FinishedAt']
        assert info['Config']['Labels']==old['Config']['Labels']
        subprocess.run(['podman','rm',cid],check=True,capture_output=True,text=True)
        removed.append({'id':cid,'name':old['Name'],'project':old['Config']['Labels']['multilinux.project']})
        (RUN/'retired-test-containers.json').write_text(json.dumps({'removed':removed,'forced':False,'volumes_or_bind_data_removed':False},ensure_ascii=False,indent=2)+'\n')
    # Root state contains only the original m3 experiment and old transactions.
    # Keep historical evidence snapshots in place; retire the active registry.
    files=json.loads((BACKUP/'host-files-before.json').read_text())
    state=ROOT/'.lab/state'
    for p in state.rglob('*'):
        if p.is_file() and not p.is_symlink():
            assert hashlib.sha256(p.read_bytes()).hexdigest()==files[str(p)]
    shutil.move(state,BACKUP/'retired-main-state')
    state.mkdir(mode=0o700)
    result={'historical_test_containers_removed':len(removed),'main_registry_archived':True,
            'historical_evidence_not_modified':True,'backup':str(BACKUP)}
    (RUN/'test-retirement.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
