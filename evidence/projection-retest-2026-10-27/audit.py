#!/usr/bin/env python3
"""Verify this retest left product code, old evidence and runtime state intact."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'src'))
from multilinux.on_demand import process_stamp


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    baseline=json.loads((HERE/'baseline.json').read_text())
    expected_document_updates={'README.md','HANDOFF.md'}
    changed,missing=[],[]
    for name,value in baseline['files'].items():
        path=ROOT/name
        if not path.is_file():
            missing.append(name)
        elif digest(path)!=value:
            changed.append(name)
    unexpected=[n for n in changed if n not in expected_document_updates]
    source={str(p.relative_to(ROOT)):digest(p) for p in (ROOT/'src').rglob('*.py')}
    after=json.loads(subprocess.check_output(['podman','ps','-a','--no-trunc','--format','json']))
    fields=('Id','Names','State','ImageID','Labels','Mounts','StartedAt','ExitedAt','ExitCode')
    def inventory(rows):
        return {r['Id']:{f:r.get(f) for f in fields} for r in rows}
    containers_unchanged=inventory(after)==inventory(baseline['containers'])
    before_by_id={r['Id']:r for r in baseline['containers']}
    after_by_id={r['Id']:r for r in after}
    container_differences={}
    for cid in before_by_id.keys() & after_by_id.keys():
        changes={f:{'before':before_by_id[cid].get(f),'after':after_by_id[cid].get(f)}
                 for f in fields if before_by_id[cid].get(f)!=after_by_id[cid].get(f)}
        if changes:
            container_differences[cid]=changes
    confirmation_path=HERE/'runtime-activity-confirmation.json'
    confirmation=json.loads(confirmation_path.read_text()) if confirmation_path.exists() else {}
    confirmed_activity_only=(set(before_by_id)==set(after_by_id))
    for cid,changes in container_differences.items():
        confirmed_activity_only &= (cid==confirmation.get('container_id') and
            confirmation.get('confirmed_external_to_this_retest') is True and
            set(changes)<=set(confirmation.get('observed_after',{})) and
            all(value['after']==confirmation['observed_after'][field] for field,value in changes.items()))
    mounts=[s for s in Path('/proc/self/mountinfo').read_text().splitlines()
            if 'fuse.unifier' in s or 'unifier-view-' in s]
    storage=subprocess.run(['podman','unshare','podman','mount','--format','json'],
                           capture_output=True,text=True,timeout=30)
    mounted_storage=json.loads(storage.stdout) if storage.returncode==0 else None
    workers=[]
    for path in HERE.rglob('result.json'):
        value=json.loads(path.read_text())
        states=[value.get('initial_status',{})]+[v.get('status',{}) for v in value.get('targets',{}).values()]
        for state in states:
            if state.get('worker_pid'):
                workers.append(dict(result=str(path.relative_to(HERE)),pid=state['worker_pid'],
                    same_identity_alive=process_stamp(state['worker_pid'])==state['worker_stamp']))
    remaining=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name)==os.getpid():
            continue
        try:
            args=(path/'cmdline').read_bytes().decode('utf-8','replace').split('\0')
        except (OSError,ProcessLookupError):
            continue
        if ('_broker' in args or '_fuse' in args) and any(str(HERE) in a for a in args):
            remaining.append(int(path.name))
    result=dict(timestamp=time.time(),original_files_checked=len(baseline['files']),
                original_source_matches_previous_acceptance=baseline['source_matches_previous_acceptance'],
                source_unchanged=source==baseline['source_sha256'],source_sha256=source,
                main_state_files=sum(n.startswith('.lab/state/') for n in baseline['files']),
                historical_evidence_files=sum(n.startswith('evidence/') for n in baseline['files']),
                missing=missing,changed=changed,unexpected_changes=unexpected,
                original_containers=len(baseline['containers']),containers_unchanged=containers_unchanged,
                strict_container_runtime_unchanged=containers_unchanged,
                container_differences=container_differences,
                user_confirmed_runtime_activity_only=confirmed_activity_only,
                unifier_mounts=mounts,storage_mounts=mounted_storage,storage_query_returncode=storage.returncode,
                recorded_workers=workers,remaining_workers_or_brokers=remaining)
    result['passed']=(not missing and not unexpected and result['source_unchanged'] and confirmed_activity_only
                      and not mounts and mounted_storage==[] and storage.returncode==0
                      and not any(w['same_identity_alive'] for w in workers) and not remaining)
    result['status']=('PRESERVED_WITH_USER_CONFIRMED_RUNTIME_ACTIVITY' if result['passed'] and not containers_unchanged
                      else 'PRESERVED' if result['passed'] else 'AUDIT_FAILED')
    (HERE/'audit.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    print(json.dumps({k:v for k,v in result.items() if k not in ('source_sha256','recorded_workers')},ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
