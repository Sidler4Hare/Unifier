#!/usr/bin/env python3
"""Final immutable-source, evidence, preservation and cleanup checks."""
import hashlib,json,re,subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];RUN=Path(__file__).resolve().parent
BACKUP=Path('/home/omg/文档/Unifier-backups/20261029-141151-before-projection-fd')
EXPECTED={'src/multilinux/projection.py','src/multilinux/view_backend.py','src/multilinux/fuse_view.py','tests/test_projection_directory.py','tests/test_projection_boundaries.py','README.md','HANDOFF.md'}
checks={}
def verify(name,value):
    assert value,name
    checks[name]=True

def files(root):return {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and not p.is_symlink()}

manifest=json.loads((BACKUP/'manifest.json').read_text());changed=[];checked=0
for row in manifest:
    p=ROOT/row['path']
    if '__pycache__' in p.parts or row['path'].startswith('.git/refs/codex/turn-diffs/'):
        continue
    verify('original entries present',p.exists() or p.is_symlink())
    if 'link' in row:verify('original symlinks preserved',p.readlink()==Path(row['link']))
    if 'sha256' in row:
        checked+=1
        if hashlib.sha256(p.read_bytes()).hexdigest()!=row['sha256']:changed.append(row['path'])
verify('only declared original file changes',not set(changed)-EXPECTED)
(RUN/'preservation-workspace.json').write_text(json.dumps(dict(backup=str(BACKUP),regular_files_verified=checked,changed=changed,unexpected_changes=[],missing=[],socket_nodes_metadata_only=[x for x in manifest if 'special' in x]),ensure_ascii=False,indent=2))
frozen=json.loads((RUN/'source-frozen.json').read_text())['sha256']
broker=json.loads((RUN/'broker-final/result.json').read_text())
deploy=json.loads((RUN/'deployment.json').read_text())
for p,h in frozen.items():
    verify('frozen '+p,hashlib.sha256((ROOT/p).read_bytes()).hexdigest()==h)
    verify('saved '+p,hashlib.sha256((RUN/'accepted-source'/p).read_bytes()).hexdigest()==h)
    if p.startswith('src/'):
        verify('final broker source '+p,broker['source_sha256'][p]==h)
        verify('deployed source '+p,deploy['source_sha256'][p]==h)
log=(RUN/'unittest-final.log').read_text()
verify('full regression 456 with four skips','Ran 456 tests' in log and 'OK (skipped=4)' in log)
for name,count in [('core-final',22),('boundaries-final',9),('real-fd-final',3)]:
    result=json.loads((RUN/name/'result.json').read_text())
    verify(name,result['passed'] and len(result['cases'])==count and all(c['passed'] for c in result['cases']))
    if name=='real-fd-final':
        for case in result['cases']:verify('cleanup '+case['name'],case['cleanup']['normal_unmount'] and case['cleanup']['not_mounted'] and case['cleanup']['worker_exit']==0)
    else:verify('cleanup '+name,result['cleanup']['normal_unmount'] and result['cleanup']['not_mounted'])
verify('three guest broker matrix',broker['passed'] and len(broker['targets'])==3)
for target,result in broker['targets'].items():
    verify('broker '+target,result['passed'] and len(result['cases'])==27 and result['normal_cleanup'] and result['leases_after']==0 and result['container_removed'])
for app,item in deploy['apps'].items():
    result=json.loads((RUN/('installed-'+app)/'result.json').read_text())
    verify('installed '+app,result['passed'] and result['normal_revoke'])
    root=Path(item['root'])
    verify('daily state '+app,files(root/'.lab/state')==item['state_before'])
    verify('daily config '+app,files(root/'config')==item['config_before'])
    verify('daily documents '+app,files(Path(item['documents']))==item['documents_before'])
    verify('daily desktop '+app,hashlib.sha256(Path(item['desktop']).read_bytes()).hexdigest()==item['desktop_sha256'])
    info=json.loads(subprocess.check_output(['podman','inspect',item['container_id']]))[0]
    private=json.loads((Path(deploy['backup'])/'deployment-private.json').read_text())['apps'][app]
    verify('daily container state '+app,info['State']==private['before_inspect']['State'])
    for p,h in deploy['source_sha256'].items():verify('installed source '+app+' '+p,hashlib.sha256((root/p).read_bytes()).hexdigest()==h)
    verify('daily lease '+app,not (root/'.lab/leases'/(item['environment']+'.json')).exists() or not json.loads((root/'.lab/leases'/(item['environment']+'.json')).read_text()).get('leases'))
gui=json.loads((RUN/'gui-final-3/result.json').read_text())
verify('real file manager',gui['passed'] and gui['visual_review']['accepted'] and gui['cleanup']['normal_unmount'] and gui['after_normal_close']['counters']['handles']==0)
containers=json.loads(subprocess.check_output(['podman','ps','-a','--no-trunc','--format','json']))
verify('exact two original stopped daily containers',{x['Id'] for x in containers}=={x['container_id'] for x in deploy['apps'].values()} and all(x['State']=='exited' for x in containers))
mounts=Path('/proc/self/mountinfo').read_text()
verify('no test FUSE mount',not any('unifier-object-' in line or 'unifier-installed-fd-' in line or 'unifier-broker-probe-' in line for line in mounts.splitlines()))
workers=[]
for entry in Path('/proc').iterdir():
    if not entry.name.isdigit():continue
    try:argv=(entry/'cmdline').read_bytes().split(b'\0')
    except (OSError,ProcessLookupError):continue
    if any(a in {b'multilinux.fuse_view',b'multilinux.view_broker',b'_fuse',b'_broker'} for a in argv):workers.append(entry.name)
verify('no live FUSE worker or storage broker',not workers)
subprocess.run(['git','diff','--check'],cwd=ROOT,check=True)
result={'status':'LOCAL_DIRECTORY_FD_REPAIR_AND_BUDGET_ACCEPTED','passed':True,'checks':checks,
        'regression':{'total':456,'passed':452,'skipped':4},'workspace_original_files_verified':checked,
        'open_gates':['large stat/index lookup capacity','external watcher','strict snapshot/cross-volume','performance/long stress','ACL/xattr','general IDE','cross-host'],
        'uncommitted':True,'source_sha256':frozen}
(RUN/'acceptance-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
print(json.dumps({'passed':True,'checks':len(checks),'original_files_verified':checked}))
