"""Retire the old meeting CID and only explicit, backed-up Unifier images."""
from pathlib import Path
import json
import subprocess

RUN=Path(__file__).resolve().parent
CTX=json.loads((RUN/'context.json').read_text())
BACKUP=Path(CTX['backup'])


def invoke(args):
    return subprocess.run(['podman',*args],capture_output=True,text=True,timeout=120)


def norm(s):return s.removeprefix('sha256:')


def main():
    assert (BACKUP/'backup-verified.json').is_file()
    for app in ['wemeet','freecad']:
        assert json.loads((RUN/(app+'-gui-acceptance.json')).read_text())['status']=='PASSED_MENU_REOPEN_AND_LIFECYCLE'
    assert json.loads((RUN/'deployed-view-validation.json').read_text())['status']=='PASSED'
    deployment=json.loads((RUN/'wemeet-deployment.json').read_text())
    old=deployment['old_container_id']
    info=json.loads(invoke(['inspect',old]).stdout)[0]
    assert info['Name'].lstrip('/')==deployment['old_retired_name']
    assert info['State']['Status']!='running'
    removal=invoke(['rm',old]);assert removal.returncode==0,removal.stderr
    checkpoints=json.loads((BACKUP/'checkpoints.json').read_text())
    meeting=next(x for x in checkpoints if x['container_id']==old)
    candidates={norm(x['image_id']) for x in checkpoints if x['container_id']!=old}
    previous=json.loads((BACKUP/'images-before.json').read_text())
    for image in previous:
        if any(t.startswith('localhost/multilinux/startup-') or t.startswith('localhost/multilinux/wemeet-ready:') for t in image.get('RepoTags') or []):
            candidates.add(norm(image['Id']))
    keep=set()
    for root in [Path(CTX['root']),Path.home()/'.local/share/multilinux/apps/wemeet',Path.home()/'.local/share/multilinux/apps/freecad']:
        lock=json.loads((root/'config/images.lock.json').read_text())
        for value in lock['targets'].values():
            i=invoke(['image','inspect',value['locked_reference']]);assert i.returncode==0,i.stderr
            keep.add(norm(json.loads(i.stdout)[0]['Id']))
    candidates-=keep
    before={norm(i['Id']) for i in json.loads(invoke(['image','inspect',*invoke(['image','ls','-a','--no-trunc','--quiet']).stdout.split()]).stdout)}
    removed=[];refusals={}
    pending=set(candidates)
    for _ in range(len(candidates)+1):
        progress=False
        consumers=json.loads(invoke(['ps','-a','--format','json']).stdout)
        referenced={norm(c['ImageID']) for c in consumers}
        assert not (pending & referenced)
        for image in sorted(pending.copy()):
            result=invoke(['rmi','--no-prune',image])
            if result.returncode==0:
                removed.append(image);pending.remove(image);refusals.pop(image,None);progress=True
            else:refusals[image]=result.stderr[-1500:]
        if not pending or not progress:break
    # Keep the current descriptive tag; drop the temporary archive alias only.
    untag=invoke(['untag',meeting['image_id'],meeting['tag']])
    assert untag.returncode==0,untag.stderr
    rows=json.loads(invoke(['ps','-a','--format','json']).stdout)
    assert len(rows)==2 and {r['Id'] for r in rows}=={deployment['new_container_id'],json.loads((RUN/'freecad-code-sync.json').read_text())['container_id_preserved']}
    assert not any(r['State']=='running' for r in rows)
    after={norm(s) for s in invoke(['image','ls','-a','--no-trunc','--quiet']).stdout.split()}
    assert keep<=after
    assert before-after<=candidates
    unrelated={norm(i['Id']) for i in previous if any(t.startswith('localhost/pi-c') or t.startswith('docker.io/library/alpine:') for t in i.get('RepoTags') or [])}
    assert unrelated<=after
    result={'status':'RETIRED','old_meeting_container_removed':old,'final_container_count':len(rows),
            'remaining_containers':[{'id':r['Id'],'name':r['Names'][0],'state':r['State']} for r in rows],
            'obsolete_unifier_image_objects_removed':len(removed),'image_ids_removed':removed,
            'image_removals_refused_without_force':refusals,'unrelated_named_images_preserved':True,
            'configured_current_images_preserved':True,'forced_removal_used':False,'global_prune_used':False,
            'final_image_object_count':len(after)}
    (RUN/'final-runtime-inventory.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['status','final_container_count','obsolete_unifier_image_objects_removed','image_removals_refused_without_force','final_image_object_count']},ensure_ascii=False))


if __name__=='__main__':main()
