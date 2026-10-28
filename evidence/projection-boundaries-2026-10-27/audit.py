#!/usr/bin/env python3
"""Audit original files/state and exact test-resource retirement after repair."""
import hashlib,json,os,stat,subprocess,sys,time
from pathlib import Path
HERE=Path(__file__).resolve().parent;ROOT=HERE.parents[1]
sys.path.insert(0,str(ROOT/'src'))
from multilinux.on_demand import process_stamp

def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def main():
    baseline=json.loads((HERE/'baseline.json').read_text())
    backup=json.loads((HERE/'backup-verification.json').read_text())
    original_entries=json.loads((Path(backup['backup'])/'manifest.json').read_text())
    metadata_changes=[]
    for name,item in original_entries.items():
        path=ROOT/name
        try:observed=path.lstat()
        except OSError:metadata_changes.append(name);continue
        valid_type={'file':stat.S_ISREG,'dir':stat.S_ISDIR,'symlink':stat.S_ISLNK,'socket':stat.S_ISSOCK}[item['type']]
        if not valid_type(observed.st_mode) or stat.S_IMODE(observed.st_mode)!=item['mode']:
            metadata_changes.append(name)
        elif item['type']=='symlink' and os.readlink(path)!=item['target']:
            metadata_changes.append(name)
    allowed={'README.md','HANDOFF.md','src/multilinux/projection.py','src/multilinux/view_backend.py','src/multilinux/fuse_view.py',
             'tests/test_projection_objects.py','tests/test_projection_directory.py','tests/test_projection_controller.py',
             'tests/test_projection_support.py','tests/test_fuse_view.py','tests/test_view_backend.py'}
    missing=[n for n in baseline['files'] if not (ROOT/n).is_file()]
    changed=[n for n,h in baseline['files'].items() if (ROOT/n).is_file() and sha(ROOT/n)!=h]
    unexpected=[n for n in changed if n not in allowed]
    source={str(p.relative_to(ROOT)):sha(p) for p in (ROOT/'src').rglob('*.py')}
    after=json.loads(subprocess.check_output(['podman','ps','-a','--no-trunc','--format','json']))
    fields=('Id','Names','State','ImageID','Labels','Mounts','StartedAt','ExitedAt','ExitCode')
    def inventory(rows):return {r['Id']:{f:r.get(f) for f in fields} for r in rows}
    original,now=inventory(baseline['containers']),inventory(after)
    differences={cid:{f:{'before':original[cid][f],'after':now[cid][f]} for f in fields if original[cid][f]!=now[cid][f]}
                 for cid in original.keys()&now.keys() if original[cid]!=now[cid]}
    mounts=[s for s in Path('/proc/self/mountinfo').read_text().splitlines() if 'fuse.unifier' in s or 'unifier-view-' in s]
    storage=subprocess.run(['podman','unshare','podman','mount','--format','json'],capture_output=True,text=True,timeout=30)
    storage_mounts=json.loads(storage.stdout) if storage.returncode==0 else None
    workers=[]
    for path in HERE.rglob('result.json'):
        v=json.loads(path.read_text())
        for status in [v.get('initial_status',{})]+[x.get('status',{}) for x in v.get('targets',{}).values()]:
            if status.get('worker_pid'):
                workers.append({'result':str(path.relative_to(HERE)),'pid':status['worker_pid'],
                    'same_identity_alive':process_stamp(status['worker_pid'])==status['worker_stamp']})
    remaining=[]
    for path in Path('/proc').iterdir():
        if not path.name.isdigit() or int(path.name)==os.getpid():continue
        try:args=(path/'cmdline').read_bytes().decode('utf-8','replace').split('\0')
        except OSError:continue
        if ('_broker' in args or '_fuse' in args) and any(str(HERE) in a for a in args):remaining.append(int(path.name))
    current_files=[]
    for base,dirs,files in os.walk(ROOT):
        dirs[:]=[d for d in dirs if d not in ('.git','__pycache__')]
        for name in files:
            p=Path(base)/name;n=str(p.relative_to(ROOT))
            if not n.endswith('.pyc') and n not in original_entries and not n.startswith(('evidence/projection-boundaries-2026-10-27/','.lab/agents/projection-boundaries-20261027/')):
                current_files.append(n)
    allowed_new_tests={'tests/test_projection_boundaries.py','设计/投影边界修复-2026-10-27.md'}
    unexpected_new=[n for n in current_files if n not in allowed_new_tests]
    r={'timestamp':time.time(),'original_files_checked':len(baseline['files']),'changed':changed,'missing':missing,
       'unexpected_changes':unexpected,'original_metadata_changes':metadata_changes,
       'new_files_outside_run':current_files,'unexpected_new_files':unexpected_new,
       'source_sha256':source,'main_state_files':sum(n.startswith('.lab/state/') for n in baseline['files']),
       'historical_evidence_files':sum(n.startswith('evidence/') for n in baseline['files']),
       'original_containers':len(original),'containers_unchanged':original==now,'container_differences':differences,
       'new_containers_remaining':sorted(now.keys()-original.keys()),'missing_original_containers':sorted(original.keys()-now.keys()),
       'unifier_mounts':mounts,'storage_mounts':storage_mounts,'storage_query_returncode':storage.returncode,
       'recorded_workers':workers,'remaining_workers_or_brokers':remaining}
    r['passed']=(not missing and not unexpected and not unexpected_new and not metadata_changes and original==now and not mounts and storage_mounts==[]
                 and storage.returncode==0 and not any(w['same_identity_alive'] for w in workers) and not remaining)
    (HERE/'audit.json').write_text(json.dumps(r,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps({k:v for k,v in r.items() if k not in ('source_sha256','recorded_workers')},ensure_ascii=False))
    return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
