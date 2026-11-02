#!/usr/bin/env python3
"""Read-only revision-3 consistency and captured-baseline preservation checks."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.dont_write_bytecode = True
sys.path.insert(0, str(ROOT / 'src'))
from unifier.compatibility import POLICY_VERSION, relationship_plan

def read(path):
    return json.loads(path.read_text())

def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()

checks = []
def check(name, condition):
    checks.append({'name': name, 'passed': bool(condition)})

p = read(HERE / 'plan.json')
r = read(HERE / 'relationship-plan.json')
b = read(HERE / 'preflight.json')
old = read(ROOT / 'archive/evidence/extension-test-plan-2026-10-29/plan.json')
oldr = read(ROOT / 'archive/evidence/extension-test-plan-2026-10-29/relationship-plan.json')
for key, count in [('primary_installation_cells', 9), ('agent_contract_families', 14),
                   ('filesystem_families', 20), ('agent_origin_target_cells', 12)]:
    check(key + ' retained IDs', len(p[key]) == count and
          {x['id'] for x in p[key]} == {x['id'] for x in old[key]})
check('18 relationship IDs', len(r['relationship_families']) == 18 and
      {x['id'] for x in r['relationship_families']} == {x['id'] for x in oldr['relationship_families']})
check('12 historical shared edges retained as templates', len(p['shared_data_ordered_edges']) == 12 and
      {(x['from'], x['to']) for x in p['shared_data_ordered_edges']} ==
      {(x['from'], x['to']) for x in old['shared_data_ordered_edges']})
check('historical 12/12 are not executable', all(x['template_only'] for key in
      ['shared_data_ordered_edges', 'agent_origin_target_cells'] for x in p[key]))
check('CLI01-CLI14', {x['id'] for x in p['cli_families']} == {f'CLI{i:02d}' for i in range(1, 15)} and p['cli_family_count'] == 14)
check('REG01-REG05', {x['id'] for x in p['release_regressions']} == {f'REG{i:02d}' for i in range(1, 6)})
check('policy version matches code', p['policy']['version'] == POLICY_VERSION == 2)
check('four explicit host contexts', len(p['host_matrices']) == 4)
for h in p['host_matrices']:
    facts = {'os_release': {'ID': h['host_id']}, 'system': 'Linux',
             'machine': 'x86_64', 'libc': 'glibc', 'ostree': False, 'transactional': False}
    admitted = {t for t in ['debian13', 'fedora44', 'arch'] if relationship_plan(facts, t)['allowed']}
    check(h['id'] + ' placement matches real policy implementation', admitted == set(h['allowed_targets']) and
          set(h['refused_targets']) == {'debian13', 'fedora44', 'arch'} - admitted)
    nodes = {'host', *admitted}
    check(h['id'] + ' six shared directions', len(h['shared_data_ordered_edges']) == 6 and
          {(x['from'], x['to']) for x in h['shared_data_ordered_edges']} == {(a, c) for a in nodes for c in nodes if a != c})
    check(h['id'] + ' six Agent controls', len(h['agent_origin_target_cells']) == 6 and
          {(x['origin'], x['target']) for x in h['agent_origin_target_cells']} == {(a, c) for a in admitted for c in nodes})
    check(h['id'] + ' 54 deterministic replays', h['deterministic_interwrite_replays'] == 6 * 3 * 3)
    check(h['id'] + ' controls gated', all(('G-GUEST-BRIDGE' in x['user_task_required_gates']) == (x['origin'] != x['target']) for x in h['agent_origin_target_cells']))
contexts = {h['id']: h for h in p['host_matrices']}
check('9 applications placed only on eligible hosts', all(
      x['eligible_host_contexts'] and x['preferred_host_context'] in x['eligible_host_contexts'] and
      all(x['target'] in contexts[h]['allowed_targets'] for h in x['eligible_host_contexts'])
      for x in p['primary_installation_cells']))
check('four Fedora RPM cells wait for desktop', sum(x['target'] == 'fedora44' for x in p['primary_installation_cells']) == 4 and
      all('G-DESKTOP' in x['blocked_by'] for x in p['primary_installation_cells'] if x['target'] == 'fedora44'))
check('new workflows not upgraded by prior evidence', all(x['status'] == 'NOT_RUN' for key in
      ['primary_installation_cells', 'agent_contract_families', 'filesystem_families', 'cli_families', 'release_regressions'] for x in p[key]) and
      all(x['workflow_status'] == 'NOT_RUN' for x in r['relationship_families']))
check('server scope updated, November unscheduled', p['tracks']['server_hosts']['results_imported'] and
      p['tracks']['server_hosts']['status'] == 'SCOPED_BASELINE_VERIFIED' and
      p['tracks']['full_extension']['start_month'] == '2026-11' and
      p['tracks']['full_extension']['start_day'] is None and
      not p['tracks']['full_extension']['automatic_scheduling_created'])
gates = {g['id'] for g in p['gates']}
missing_gates = set()
def inspect_gates(v):
    if isinstance(v, dict):
        for k, x in v.items():
            if (k.endswith('required_gates') or k == 'blocked_by') and isinstance(x, list):
                missing_gates.update(g for g in x if g not in gates)
            inspect_gates(x)
    elif isinstance(v, list):
        for x in v: inspect_gates(x)
inspect_gates(p); inspect_gates(r)
check('all structured gate references resolve', not missing_gates)
missing_refs = []
ref_count = 0
def inspect_refs(v):
    global ref_count
    if isinstance(v, dict):
        for x in v.values(): inspect_refs(x)
    elif isinstance(v, list):
        for x in v: inspect_refs(x)
    elif isinstance(v, str) and v.startswith('../') and '\n' not in v:
        ref_count += 1
        if not (HERE / v).exists(): missing_refs.append(v)
inspect_refs(p); inspect_refs(r)
check('JSON relative references exist', not missing_refs)
md_refs = 0
for f in [ROOT/'设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md', HERE/'README.md', ROOT/'README.md', ROOT/'HANDOFF.md']:
    for target in re.findall(r'\]\(([^)]+)\)', f.read_text()):
        if '://' in target or target.startswith('#'): continue
        md_refs += 1
        if not (f.parent / target.split('#')[0]).exists(): missing_refs.append(f'{f}:{target}')
check('Markdown local links exist', not missing_refs)
check('historical JSON inputs unchanged', all(digest(ROOT/n) == v for n, v in b['historical_inputs_sha256'].items()))
unchanged = True
for n, v in b['product_entries_before'].items():
    f = ROOT/n
    unchanged &= (f.is_symlink() and os.readlink(f) == v['target']) if v['kind'] == 'symlink' else (f.is_file() and digest(f) == v['sha256'])
check('product source/tests/tools/config/fixtures unchanged', unchanged)
check('formal release package unchanged', digest(HERE/b['release_package']) == b['release_package_sha256'])
index = subprocess.check_output(['git', 'ls-files', '--stage', '-z'], cwd=ROOT)
check('Git staged entries unchanged', hashlib.sha256(index).hexdigest() == b['git_index_entries_sha256'])
live = json.loads(subprocess.check_output(['podman', 'ps', '-a', '--format', 'json']))
current = [{k: x.get(k) for k in ['Id', 'Names', 'State', 'Image', 'StartedAt', 'ExitedAt']} for x in live]
check('container identities/state/start-stop times unchanged', sorted(current, key=lambda x: x['Id']) == sorted(b['local_containers'], key=lambda x: x['Id']))
for host in ['ubuntu', 'arch', 'debian']:
    summary = read(ROOT/f'artifacts/v0.0.1-20261030/remote/{host}/results/summary.json')
    check(host + ' imported driver commands passed', len(summary['commands']) == 32 and all(x['passed'] for x in summary['commands'].values()))
check('FreeCAD prior scoped workflow passed', read(ROOT/'artifacts/v0.0.1-20261030/freecad-workflow/result.json')['passed'])
check('targeted diff whitespace', subprocess.run(['git', 'diff', '--check', '--', 'README.md', 'HANDOFF.md', '设计/扩展可用性与Agent跨环境测试计划-2026-10-27.md'], cwd=ROOT).returncode == 0)
oss = read(HERE / 'oss-cli-plan.json')
sources = read(HERE / 'oss-sources.json')
check('six open-source CLI projects', len(oss['projects']) == 6 and
      {x['id'] for x in oss['projects']} == {'jq', 'ripgrep', 'fd', 'bat', 'sqlite', 'htop'})
check('18 independent OSS project-target cells', len(oss['installation_cells']) == oss['cell_count'] == 18 and
      {(x['project'], x['target']) for x in oss['installation_cells']} ==
      {(x['id'], t) for x in oss['projects'] for t in ['debian13', 'fedora44', 'arch']})
check('OSS placements admit actual target and need no desktop', all(
      'G-DESKTOP' not in x['required_gates'] and x['preferred_host_context'] in x['eligible_host_contexts'] and
      all(x['target'] in contexts[h]['allowed_targets'] for h in x['eligible_host_contexts'])
      for x in oss['installation_cells']))
check('jq repository and local-native lanes', all(x['install_lanes'] == ['repository-name', 'local-native-file'] for x in oss['installation_cells'] if x['project'] == 'jq'))
check('ten format cases with five native and five unsupported', len(oss['format_cases']) == 10 and
      sum(x['capability'].startswith('SUPPORTED_NATIVE') for x in oss['format_cases']) == 5 and
      sum(x['capability'] == 'UNSUPPORTED_INSTALL_BOUNDARY' for x in oss['format_cases']) == 5)
check('native suffixes match current driver source',
      __import__('unifier.packages', fromlist=['PacmanDriver']).PacmanDriver.suffixes == ('.pkg.tar.zst', '.pkg.tar.xz', '.pkg.tar.gz') and
      {x['suffix_or_kind'] for x in oss['format_cases'] if x['capability'].startswith('SUPPORTED_NATIVE')} == {'.deb', '.rpm', '.pkg.tar.zst', '.pkg.tar.xz', '.pkg.tar.gz'})
check('PKG01-PKG14', {x['id'] for x in oss['cli_package_cases']} == {f'PKG{i:02d}' for i in range(1, 15)})
check('three implemented package managers plus explicit apk gap', len(oss['package_manager_lanes']) == 4 and
      {x['managed_driver'] for x in oss['package_manager_lanes'] if x['target'] != 'alpine'} == {'apt-get', 'dnf', 'pacman'} and
      next(x for x in oss['package_manager_lanes'] if x['target'] == 'alpine')['positive_lane_status'] == 'BLOCKED_IMPLEMENTATION')
source_ids = {x['id'] for x in sources['sources']}
check('project evidence IDs resolve', all(set(x['sources']) <= source_ids for x in oss['projects']))
check('packages not locked or tested by browsing', all(x['status'] == 'NOT_RUN' and x['artifact_sha256'] is None for x in oss['installation_cells']) and
      all(x['status'] == 'NOT_RUN' for x in oss['format_cases'] + oss['cli_package_cases']) and
      all(not x['artifact_locked'] for x in sources['sources']))
inspect_refs(oss)
inspect_gates(oss)
check('OSS local references and gate IDs resolve', not missing_refs and not missing_gates and (HERE/oss['sources']).is_file())
result = {'scope': 'plan consistency and preserved local baseline only; no expanded workflow or regression execution',
          'passed': all(c['passed'] for c in checks), 'checks': checks,
          'counts': {'checks': len(checks), 'json_relative_references': ref_count, 'markdown_local_links': md_refs},
          'missing_references': missing_refs, 'missing_gates': sorted(missing_gates)}
print(json.dumps(result, ensure_ascii=False, indent=2))
sys.exit(0 if result['passed'] else 1)
