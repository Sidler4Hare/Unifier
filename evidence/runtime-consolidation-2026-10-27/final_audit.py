"""Independent final inventory, file-preservation and deployed-version audit."""
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
import subprocess
import sys

RUN=Path(__file__).resolve().parent
CTX=json.loads((RUN/'context.json').read_text())
ROOT,BACKUP=Path(CTX['root']),Path(CTX['backup'])
APPS=Path.home()/'.local/share/multilinux/apps'
sys.path.insert(0,str(ROOT/'src'))


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for data in iter(lambda:f.read(1024*1024),b''):h.update(data)
    return h.hexdigest()


def cmd(args):
    p=subprocess.run(list(map(str,args)),capture_output=True,text=True,timeout=90)
    assert p.returncode==0,(args,p.stderr[-1500:])
    return p.stdout


def main():
    before=json.loads((BACKUP/'host-files-before.json').read_text())
    protected=[];problems=[];archived_state=[];expected=[]
    for name,digest in before.items():
        p=Path(name)
        if p.is_relative_to(RUN) or '.git' in p.parts or '__pycache__' in p.parts or p.suffix=='.pyc':continue
        if p.is_relative_to(ROOT/'.lab/state'):
            q=BACKUP/'retired-main-state'/p.relative_to(ROOT/'.lab/state')
            archived_state.append(name)
            if not q.is_file() or sha(q)!=digest:problems.append({'path':name,'kind':'retired-state-not-preserved'})
            continue
        mutable=False
        if p in [ROOT/'README.md',ROOT/'HANDOFF.md']:mutable=True
        for app in ['freecad','wemeet']:
            base=APPS/app
            if not p.is_relative_to(base):continue
            rel=p.relative_to(base)
            if rel.parts[0] in ['src','tools']:mutable=True
            if app=='wemeet' and rel.parts[0]=='config':mutable=True
            if str(rel) in ['.lab/state/environments/'+('meeting' if app=='wemeet' else 'freecad-arch')+'.json',
                            '.lab/leases/'+('meeting' if app=='wemeet' else 'freecad-arch')+'.json']:mutable=True
            if app=='wemeet' and str(rel) in ['.lab/state/environments/meeting.packages.txt','.lab/state/launches/wemeet.json','.lab/desktop/meeting/Xauthority']:mutable=True
        if p==Path.home()/'.local/share/applications/wemeetapp.desktop':mutable=True
        if mutable:
            expected.append(name);continue
        protected.append(name)
        if not p.is_file() or sha(p)!=digest:problems.append({'path':name,'kind':'protected-original-changed-or-missing'})
    assert len(archived_state)==23
    from multilinux.on_demand import process_stamp
    from multilinux.views import ViewRegistry,view_active
    from multilinux.project_sessions import SessionStore
    source=json.loads((RUN/'wemeet-deployment.json').read_text())['source_sha256']
    app_checks={}
    for app,env in [('wemeet','meeting'),('freecad','freecad-arch')]:
        base=APPS/app
        identical=all((base/rel).is_file() and sha(base/rel)==digest==sha(ROOT/rel) for rel,digest in source.items())
        lease=json.loads((base/'.lab/leases'/(env+'.json')).read_text())
        inactive=not lease['leases'] and lease.get('bridge') is None and not lease.get('auto_stop')
        no_views=not any(view_active(v) for v in ViewRegistry(base).list())
        terminal=all(s.get('guest_state')=='gone' and not s.get('lease_token') for s in SessionStore(base).list())
        pointer=json.loads((RUN/(app+'-gui-acceptance.json')).read_text())
        gui=json.loads(Path(pointer['result']).read_text())
        dead_workers=all(process_stamp(v['pid'])!=v['stamp'] for c in gui['cycles'] for v in c['leases_during']['leases'].values())
        info=json.loads(cmd(['/usr/bin/python3',base/'tools/ml.py','env','list']))
        record=json.loads((base/'.lab/state/environments'/(env+'.json')).read_text())
        inspect=json.loads(cmd(['podman','inspect',record['runtime']['container_id']]))[0]
        app_checks[app]={'current_src_tools_identical':identical,'no_live_leases_or_bridge':inactive,
                        'no_active_views':no_views,'sessions_confirmed_gone':terminal,'gui_workers_gone':dead_workers,
                        'native_menu_cycles_passed':len(gui['cycles'])==2 and gui['status']=='PASSED_MENU_REOPEN_AND_LIFECYCLE',
                        'init_enabled':inspect['HostConfig'].get('Init') is True,
                        'actual_container_stopped':inspect['State']['Status']!='running',
                        'installed_cli_config_and_lock_valid':len(info)==1}
    acceptance=json.loads((ROOT/'evidence/projection-boundaries-2026-10-27/acceptance-result.json').read_text())
    source_accepted=all(sha(ROOT/p)==h for p,h in acceptance['source_sha256'].items())
    inventory=json.loads((RUN/'final-runtime-inventory.json').read_text())
    rows=json.loads(cmd(['podman','ps','-a','--format','json']))
    two= len(rows)==2 and {r['Id'] for r in rows}=={r['id'] for r in inventory['remaining_containers']}
    mounted=cmd(['podman','unshare','podman','mount']).strip()
    view_mounts=[l for l in Path('/proc/self/mountinfo').read_text().splitlines() if 'unifier-consolidation-' in l or 'fuse.unifier' in l]
    backup_record=json.loads((BACKUP/'backup-verified.json').read_text())
    archives_ok=sha(BACKUP/'host-state.tar.zst')==backup_record['host_archive_sha256'] and sha(BACKUP/'runtime-images.docker.tar.zst')==backup_record['runtime_archive_sha256']
    doc_roots=[Path.home()/'.local/share/multilinux/documents/wemeet',ROOT.parent/'Unifier-CAD']
    doc_count=sum(any(Path(p).is_relative_to(base) for base in doc_roots) for p in protected)
    history_count=sum(Path(p).is_relative_to(ROOT/'evidence') for p in protected)
    cli_empty=json.loads(cmd(['/usr/bin/python3',ROOT/'tools/ml.py','env','list']))==[]
    result={'status':'PASSED' if not problems and all(all(c.values()) for c in app_checks.values()) and source_accepted and two and not mounted and not view_mounts and archives_ok and cli_empty else 'REVIEW_REQUIRED',
            'recorded_at':datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
            'protected_original_files_verified':len(protected),'historical_evidence_files_verified':history_count,
            'original_document_files_verified':doc_count,'retired_main_state_files_verified':len(archived_state),
            'expected_mutable_deployment_or_document_paths':expected,'problems':problems,'applications':app_checks,
            'current_product_sources_match_last_accepted_sources':source_accepted,'final_container_count':len(rows),
            'exact_expected_containers_only':two,'mounted_container_rootfs_listing':mounted,'observed_unifier_mounts':view_mounts,
            'both_backup_archives_rehashed_after_retirement':archives_ok,'root_experiment_registry_empty':cli_empty,
            'known_limitations':['No account login or real meeting/media acceptance in this migration','No new cross-host claim','Existing FUSE notification and other open gates remain'],
            'product_source_modified_this_turn':False,'committed_or_pushed':False}
    (RUN/'final-audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:result[k] for k in ['status','protected_original_files_verified','historical_evidence_files_verified','original_document_files_verified','retired_main_state_files_verified','problems','applications','final_container_count','both_backup_archives_rehashed_after_retirement']},ensure_ascii=False,indent=2))
    assert result['status']=='PASSED'


if __name__=='__main__':main()
