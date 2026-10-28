#!/usr/bin/env python3
"""Read-only audit against the verified pre-construction snapshot."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def run():
    baseline = json.loads((HERE / 'preservation-baseline.json').read_text())
    missing, changed = [], []
    for name, digest in baseline['files'].items():
        path = ROOT / name
        if not path.is_file():
            missing.append(name)
        elif hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            changed.append(name)
    backup = json.loads((HERE / 'backup-verification.json').read_text())
    full_manifest = json.loads((Path(backup['backup']) / 'manifest.json').read_text())
    allowed_changes = {'src/multilinux/fuse_view.py','src/multilinux/view_backend.py',
                       'tests/test_fuse_view.py','tests/test_view_backend.py','README.md','HANDOFF.md'}
    unexpected_changes = []
    previous_files_checked = 0
    for name, record in full_manifest.items():
        if (record['type'] != 'file' or name.startswith('.git/')
                or '__pycache__' in name or name.endswith('.pyc') or name in allowed_changes):
            continue
        previous_files_checked += 1
        path = ROOT / name
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
            unexpected_changes.append(name)
    before = json.loads((Path(backup['backup']) / 'containers.txt').read_text())
    after = json.loads(subprocess.check_output(['podman','ps','-a','--no-trunc','--format','json']))
    fields = ('Id','Names','State','ImageID','Labels','Mounts','StartedAt','ExitedAt','ExitCode')
    def normalize(rows):
        return {r['Id']:{f:r.get(f) for f in fields} for r in rows}
    a,b = normalize(before), normalize(after)
    mountinfo = Path('/proc/self/mountinfo').read_text()
    residual_mounts = [s for s in mountinfo.splitlines() if 'fuse.unifier' in s or 'unifier-view-' in s]
    storage = subprocess.run(['podman','unshare','podman','mount','--format','json'],
                             capture_output=True,text=True,timeout=30)
    storage_mounts = json.loads(storage.stdout) if storage.returncode==0 else None
    source_hashes = {str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in (ROOT/'src').rglob('*.py')}
    result = dict(timestamp=time.time(), protected_files=len(baseline['files']),
                  main_state_files=baseline['main_state_files'],
                  historical_evidence_files=baseline['historical_evidence_files'],
                  missing=missing,changed=changed,
                  previous_files_checked=previous_files_checked,
                  unexpected_previous_file_changes=unexpected_changes,
                  original_containers=len(before), containers_identical=a==b,
                  missing_containers=sorted(set(a)-set(b)),added_containers=sorted(set(b)-set(a)),
                  changed_containers=sorted(k for k in set(a)&set(b) if a[k]!=b[k]),
                  fuse_residue=residual_mounts,
                  storage_query_returncode=storage.returncode,storage_mounts=storage_mounts,
                  source_sha256=source_hashes)
    result['passed'] = (not missing and not changed and not unexpected_changes and a==b and not residual_mounts
                        and storage.returncode==0 and storage_mounts==[])
    print(json.dumps(result,ensure_ascii=False,indent=2))
    return result['passed']


if __name__=='__main__':
    raise SystemExit(0 if run() else 1)
