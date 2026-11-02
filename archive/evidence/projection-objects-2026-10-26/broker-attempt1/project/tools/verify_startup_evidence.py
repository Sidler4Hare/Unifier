#!/usr/bin/env python3
"""Audit saved startup measurements and report product findings separately.

Exit 0 means the measurement artifacts are internally valid. It does not mean
the measured lifecycle strategy meets its requirements; read product_verdict.
No container, compositor, or runtime is accessed by this verifier.
"""
import argparse
import hashlib
import json
from pathlib import Path


def read(path):
    return json.loads(path.read_text())


def verify(run):
    manifest = read(run / 'manifest.json')
    summary = read(run / 'summary.json')
    checks = []

    def check(name, ok, detail=None):
        checks.append({'name': name, 'passed': bool(ok), 'detail': detail})

    window_ids = []
    measurements = []
    for target in ['debian13', 'fedora44', 'arch']:
        for mode in ['cold', 'warm']:
            rows = [read(p) for p in sorted((run / 'samples' / target).glob(
                mode + '-measurement-*/result.json'))]
            measurements.extend(rows)
            case = summary['cases'][target + '/' + mode]
            check(target + '/' + mode + '/30_valid', len(rows) == 30 and
                  all(r['passed'] for r in rows) and case['valid'] == 30)
            endpoints = []
            lifecycle_ok = timestamps_ok = True
            for row in rows:
                window_ids.append(row['window']['window_id'])
                w, trace = row['window'], row['wayland']
                ready = max(w['received_ns'], trace['first_commit_received_ns'])
                endpoint = round((ready - row['started_ns']) / 1e6, 3)
                endpoints.append(endpoint)
                timestamps_ok &= (trace['surface_id'] is not None and
                    row['started_ns'] <= trace['first_attach_received_ns'] <=
                    trace['first_commit_received_ns'] <= row['close_requested_ns'] and
                    abs(endpoint - row['launch_to_ready_ms']) < .002 and
                    row['removed'] is not None and row['note_unchanged'])
                lease = row['lease_after']
                lifecycle_ok &= not lease['leases'] and (
                    row['environment_after'] == ('stopped' if mode == 'cold' else 'running'))
                if mode == 'cold':
                    lifecycle_ok &= lease['bridge'] is None and lease['auto_stop'] is True
                else:
                    lifecycle_ok &= lease['auto_stop'] is False
            check(target + '/' + mode + '/timing_and_window_evidence', timestamps_ok)
            check(target + '/' + mode + '/normal_lifecycle', lifecycle_ok)
            if len(endpoints) == 30:
                ordered = sorted(endpoints)
                reported = case['metrics']['launch_to_ready_ms']
                check(target + '/' + mode + '/percentiles',
                      reported['p50'] == ordered[14] and reported['p95'] == ordered[28])
    check('distinct_new_windows', len(window_ids) == 180 and len(set(window_ids)) == 180)
    for rel, expected in manifest['measurement_source_sha256'].items():
        path = run / ('project' if rel.startswith('src/') else 'measurement-source') / rel
        check('frozen_source/' + rel, hashlib.sha256(path.read_bytes()).hexdigest() == expected)

    diagnostics = []
    for target in ['debian13', 'fedora44', 'arch']:
        for name in ['reopen-0', 'reopen-1', 'reopen-2', 'concurrent', 'killed-launcher']:
            path = run / 'diagnostics' / target / name / 'result.json'
            row = read(path)
            diagnostics.append(row)
            check(target + '/' + name + '/execution', row['execution_passed'])
        both = read(run / 'diagnostics' / target / 'concurrent/result.json')
        check(target + '/concurrent_lease_contract', both['shared_lifetime_passed'])
    tail = read(run / 'warm-tail-processes.json')
    check('warm_tail_snapshot_identity', tail['init_cgroup_matches_container'])
    post = read(run / 'postconditions.json')
    check('host_packages_unchanged', post['host_packages_unchanged'])
    check('host_desktop_entries_unchanged', post['host_entry_names_unchanged'])
    check('source_environments_still_stopped', all(x['state'] == 'exited'
          for x in post['source_environments']))
    check('all_probe_environments_released', all(x['state'] == 'stopped' and
          not x['leases']['leases'] and x['leases']['bridge'] is None
          for x in post['probe_environments']))
    check('kwin_scripts_unloaded', all(not x['loaded'] for x in post['kwin_scripts']))
    check('test_windows_removed', not post['remaining_test_windows'])
    reopen = [r for r in diagnostics if r['scenario'].startswith('reopen-')]
    killed = [r for r in diagnostics if r['scenario'] == 'killed-launcher']
    findings = [
        {'id': 'slow-stop-and-reopen', 'observed': True,
         'normal_close_p50_ms': {k: v['metrics']['close_to_launcher_exit_ms']['p50']
                                for k, v in summary['cases'].items() if k.endswith('/cold')},
         'rapid_reopen_ready_ms': [r['second_launch_to_ready_ms'] for r in reopen],
         'evidence': 'stop-process-diagnostic.json; stop-events-diagnostic.json; diagnostics/*/reopen-*/result.json',
         'explanation': 'PID 1 sleep has no signal handlers; timed stop holds the environment lease lock.'},
        {'id': 'unreaped-children', 'observed': tail['zombie_count'] > 0,
         'target': tail['target'], 'zombie_count': tail['zombie_count'],
         'evidence': 'warm-tail-processes.json'},
        {'id': 'killed-launcher-ownership-loss',
         'observed': any(not r['following_managed_launch_restores_auto_stop'] for r in killed),
         'targets': [r['target'] for r in killed if not r['following_managed_launch_restores_auto_stop']],
         'evidence': 'diagnostics/*/killed-launcher/result.json'},
    ]
    return {'schema': 'multilinux.startup.validation.v1',
        'measurement_valid': all(x['passed'] for x in checks),
        'product_verdict': 'needs-lifecycle-fixes' if any(x['observed'] for x in findings)
                           else 'no-listed-lifecycle-failure-observed',
        'physical_presentation_measured': False,
        'measurement_count': len(measurements), 'diagnostic_case_count': len(diagnostics),
        'checks': checks, 'findings': findings}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    run = args.run_dir.expanduser().resolve()
    result = verify(run)
    (run / 'validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: result[k] for k in ['measurement_valid', 'product_verdict',
          'measurement_count', 'diagnostic_case_count']}, ensure_ascii=False))
    return 0 if result['measurement_valid'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
