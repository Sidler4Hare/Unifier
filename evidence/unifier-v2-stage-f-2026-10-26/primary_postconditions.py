"""Independent read-only residue audit after exact-CID fixture cleanup."""
import json
import os
from pathlib import Path
import subprocess
import sys

E = Path(__file__).resolve().parent
sys.path.insert(0, str(E/'implementation'))
import common

out = E/'primary-postconditions.json'
if out.exists():
    raise SystemExit('refusing overwrite')
cleanup = common.read_json(E/'cleanup-final/summary.json')
assert cleanup['status'] == 'passed', cleanup.get('error')
f = common.read_json(E/'fixtures.json')
before = common.read_json(E/'primary-cleanup-container-observation.json')
mount_targets = [str(Path(r['graphdriver']['Data']['UpperDir']).parent)
                 for r in before if r.get('graphdriver',{}).get('Data',{}).get('UpperDir')]
assert len(mount_targets) == 3 and all(mount_targets)
private_mounts = subprocess.check_output(['podman','unshare','cat','/proc/self/mountinfo'], text=True)
host_mounts = Path('/proc/self/mountinfo').read_text()
mount_residue = [line for line in (private_mounts+'\n'+host_mounts).splitlines()
                 if str(E/'implementation/stage-f-project/.lab/state/views') in line
                 or any(m in line for m in mount_targets)]
table = common.proc_table()
exclude = set()
pid = os.getpid()
while pid in table and pid not in exclude:
    exclude.add(pid)
    pid = table[pid]['ppid']
matching = []
for pid in table:
    if pid in exclude:
        continue
    cmd = common.cmdline_of(pid)
    if str(f['project']) in cmd or any(cid in cmd for cid in f['new_container_ids']):
        matching.append({'pid':pid,'starttime':table[pid]['starttime'],'cmdline':cmd})
registries = {}
project = Path(f['project'])
for name in ('views','project-sessions'):
    r = common.read_json(project/'.lab/state'/name/'registry.json')['records']
    registries[name] = list(r.values()) if isinstance(r,dict) else r
sessions = registries['project-sessions']
live_workers = [r['session_uuid'] for r in sessions if common.pid_alive(r.get('worker_pid'),r.get('worker_stamp'))]
leases = {}
for target, item in f['targets'].items():
    p = project/'.lab/leases'/(item['environment']['id']+'.json')
    leases[target] = common.read_json(p).get('leases',{}) if p.exists() else {}
resources = [json.loads(s) for s in (E/'soak-formal-2/resources.jsonl').read_text().splitlines()]
captured_views = {v['view_uuid']:v for r in resources for v in r.get('views',[])}
cache = common.read_json(E/'primary-cache-attempt1.json')
if cache.get('view'):
    captured_views[cache['view']['view_uuid']] = cache['view']
live_view_processes = []
runtime_dirs = []
for view in captured_views.values():
    for key, stamp in (('worker_pid','worker_stamp'),('broker_pid','broker_stamp'),('broker_launcher_pid','broker_launcher_stamp')):
        if common.pid_alive(view.get(key),view.get(stamp)):
            live_view_processes.append({'view':view['view_uuid'],'role':key,'pid':view[key]})
    p = Path('/run/user/1000/unifier-views')/view['view_uuid']
    if p.exists():
        runtime_dirs.append(str(p))
checks = {
    'exact_three_cids_removed': sorted(r['container_id'] for r in cleanup['removed']) == sorted(f['new_container_ids']),
    'container_inventory_original_27': len(common.container_inventory()) == 27,
    'host_and_rootless_mounts_clean': not mount_residue,
    'fixture_processes_gone': not matching,
    'view_registry_empty': not registries['views'],
    'session_records_terminal': bool(sessions) and all(r['state'] in ('exited','retired') and r.get('guest_state') == 'gone' and r.get('lease_token') is None for r in sessions),
    'captured_session_workers_gone': not live_workers,
    'leases_empty': not any(leases.values()),
    'captured_view_processes_gone': not live_view_processes,
    'captured_view_runtime_dirs_gone': not runtime_dirs,
    'environment_records_gone': all(not (project/'.lab/state/environments'/(r['environment']['id']+'.json')).exists() for r in f['targets'].values()),
}
result = {'passed':all(checks.values()),'checks':checks,'mount_targets':mount_targets,
          'matching_mounts':mount_residue,'matching_processes':matching,
          'live_session_workers':live_workers,'live_view_processes':live_view_processes,
          'runtime_dirs':runtime_dirs,'leases':leases,'terminal_session_records':len(sessions)}
common.write_new_json(out,result)
print(json.dumps({'passed':result['passed'],'checks':checks},ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
