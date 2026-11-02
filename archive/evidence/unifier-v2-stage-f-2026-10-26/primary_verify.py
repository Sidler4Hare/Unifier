"""Independent read-only checks of the completed Stage F local evidence.

Uses raw session records, Podman events, workload samples and resource samples;
does not import the product or accept a probe's status as the only evidence.
"""
import argparse
from collections import Counter
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path

E = Path(__file__).resolve().parent
ROOT = E.parents[1]
p = argparse.ArgumentParser()
p.add_argument('--output', type=Path, required=True)
a = p.parse_args()
if a.output.exists():
    raise SystemExit('refusing to overwrite independent verification')


def read(path):
    return json.loads(Path(path).read_text())


def lines(path):
    return [json.loads(s) for s in Path(path).read_text().splitlines() if s]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def stats(values):
    v = sorted(values)
    return {'count': len(v), 'min': v[0], 'p50': v[math.ceil(len(v)*.5)-1],
            'p95': v[math.ceil(len(v)*.95)-1], 'max': v[-1]}


checks = {}
f = read(E / 'fixtures.json')
s = read(E / 'sessions-formal-1/summary.json')
v = read(E / 'projection-formal-2/summary.json')
k = read(E / 'soak-formal-2/summary.json')
recheck = read(E / 'semantic-recheck-1.json')
snapshot = read(E / 'primary-session-registry.json')['records']
records = list(snapshot.values()) if isinstance(snapshot, dict) else snapshot
formal = [r for r in records if s['started_at'] <= r['created_at'] <= s['finished_at']]
checks['three_waves_at_each_level'] = Counter(r['level'] for r in s['waves']) == {1: 3, 2: 3, 4: 3, 8: 3}
checks['two_mixed_waves'] = len(s['mixed']) == 2 and all(r['per_target'] == {'debian13': 2, 'fedora44': 2, 'arch': 2} for r in s['mixed'])
checks['wave_same_cid_leases_and_exit'] = all(
    r['container_id'] == f['targets']['fedora44']['container_id']
    and r['lease_peak'] == r['guest_identity_count'] == r['session_count'] == r['level']
    and r['final_leases'] == r['unresolved_sessions'] == r['container_create_delta'] == 0
    and r['final_auto_stop']
    for r in s['waves'])
checks['237_actual_session_records'] = len(formal) == 237
checks['all_formal_sessions_terminal'] = all(
    r['state'] == 'exited' and r['guest_state'] == 'gone'
    and r['exit_code'] == 0 and r['lease_token'] is None
    and r['container_id'] in f['new_container_ids']
    and r['guest_pid'] is not None and r['guest_start'] is not None
    for r in formal)
checks['timings_30_each'] = all(len(t[z]) == 30 for t in s['timings'].values() for z in ('cold', 'warm'))
timings = {t: {z: stats([r['wall_ms'] for r in row[z]]) for z in ('cold', 'warm')} for t, row in s['timings'].items()}
for target, row in timings.items():
    for z, observed in row.items():
        reported = s['timings'][target][z+'_seconds']
        checks[f'timing_recompute.{target}.{z}'] = all(abs(observed[n]/1000-reported[n]) < 1e-9 for n in ('min', 'p50', 'p95', 'max')) and observed['count'] == reported['count']

events = lines(E / 'primary-session-events.jsonl')
own_events = [e for e in events if e.get('ID') in f['new_container_ids'] and e.get('Type') == 'container']
starts = Counter(e['ID'] for e in own_events if e.get('Status') == 'start')
expected = {f['targets'][t]['container_id']: 30 + 2 + 1 + (12 if t == 'fedora44' else 0) for t in f['targets']}
checks['runtime_start_events_exact'] = dict(starts) == expected
checks['runtime_no_create_events'] = not any(e.get('Status') == 'create' for e in own_events)
source_checked = 0
for folder, key in (('src', 'source_sha256'), ('tools', 'tools_sha256'), ('config', 'config_sha256')):
    for name, expected_hash in f['source'][key].items():
        source_checked += 1
        checks[f'source.{folder}/{name}'] = sha(Path(f['project'])/folder/name) == expected_hash and sha(ROOT/folder/name) == expected_hash

projection = {}
for target, row in v['targets'].items():
    counts = Counter(r['path_kind'] for r in row['samples'])
    checks[f'projection_sample_counts.{target}'] = counts == {'host-direct': 30, 'container-bind': 30, 'fuse-projection': 30}
    checks[f'projection_15_cycles.{target}'] = len(row['view_cycles']) == 15
    checks[f'projection_lifecycle.{target}'] = all(all(r['cleanup'].get(n) is True for n in ('registry_empty', 'not_mounted', 'control_socket_gone', 'storage_socket_gone', 'worker_gone', 'broker_gone', 'launcher_gone', 'no_process_references')) for r in row['view_cycles'])
    sem = row['semantics']
    checks[f'projection_reopen_fresh.{target}'] = sem['stale_replacement'].get('delayed_matches_new') is True and sem['open_fd_across_replace'].get('reopened_matches_new') is True
    projection[target] = {'metrics_ms': {kind: {phase: stats([r[phase+'_ns']/1e6 for r in row['samples'] if r['path_kind'] == kind]) for phase in ('stat','read','seq')} for kind in counts}, 'semantics': sem}

checks['soak_180_seconds'] = k['actual_seconds'] >= 180 and k['bounded_seconds'] == 180
checks['soak_reader_writer_progress'] = k['reader_iterations'] >= 50 and k['writer']['writes'] >= 170 and not k['errors'] and k['writer']['error'] is None
checks['soak_host_change_events'] = k['inotify']['host_event_count'] > 0
checks['session_and_soak_check_sets_true'] = all(all(r['checks'].values()) for r in (s,k))
checks['original_projection_failure_is_only_link_oracle'] = [n for n, ok in v['checks'].items() if not ok] == ['relative_link_mapping']
checks['corrected_projection_link_contract_rechecked'] = recheck['passed'] and len(recheck['targets']) == 3 and all(all(t['checks'].values()) for t in recheck['targets'].values())
resource = {}
for name in ('sessions-formal-1', 'soak-formal-2'):
    raw = lines(E/name/'resources.jsonl')
    summary = read(E/name/'resources-summary.json')
    checks[f'resources_sample_count.{name}'] = len(raw) == summary['sample_count'] and len(raw) >= 30
    checks[f'resources_no_guard_abort.{name}'] = not summary['abort']['aborted'] and all(r.get('guard',{}).get('ok') is True for r in raw)
    checks[f'resources_no_sample_errors.{name}'] = not any(r.get('errors') for r in raw)
    gaps = [(y['monotonic_ns']-x['monotonic_ns'])/1e9 for x,y in zip(raw,raw[1:])]
    if name.startswith('soak'):
        checks['continuous_soak_sampling'] = max(gaps) < 5
    resource[name] = {'samples': len(raw), 'coverage': summary['coverage'], 'metrics': summary['metrics'], 'sampling': summary['sampling'], 'sample_gap_seconds': stats(gaps)}

result = {'scope': 'Fedora host only; local measurement completion is separate from filesystem semantic acceptance',
          'passed': all(checks.values()), 'checks': checks,
          'source_files_verified': source_checked, 'formal_session_records': len(formal),
          'session_start_events': dict(starts), 'expected_start_events': expected,
          'timings_ms': timings, 'projection': projection, 'semantic_recheck': recheck, 'resources': resource,
          'known_product_limitations': ['projected open descriptors may follow replacement path', 'no verified external-change notification through projection', 'path cache growth has no proven long-term bound']}
a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
print(json.dumps({'passed': result['passed'], 'checks': len(checks), 'failed': [n for n,v in checks.items() if not v], 'formal_sessions': len(formal)}, ensure_ascii=False))
raise SystemExit(0 if result['passed'] else 1)
