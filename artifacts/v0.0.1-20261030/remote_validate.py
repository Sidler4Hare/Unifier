"""Validate one exact v0.0.1 package on a real host without touching old roots."""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import traceback

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, required=True)
parser.add_argument('--out', type=Path, required=True)
args = parser.parse_args()
source, out = args.source.resolve(), args.out.resolve()
out.mkdir(parents=True, exist_ok=False)
sys.path[:0] = [str(source / 'src'), str(source)]
from tools.check_repo import verify_bundle
verify_bundle(source)
from unifier import __version__
from unifier.shortcuts import Context
from unifier.store import EnvironmentStore
report = {'version': __version__, 'hostname': platform.node(), 'python': platform.python_version(),
          'kernel': platform.release(), 'scope': 'exact package offline tests, real cross-family lifecycle, CLI, bind and stopped-view browsing; no GUI/media',
          'commands': {}, 'allowed': [], 'blocked': [], 'error': None}
environment = {**os.environ, 'LC_ALL': 'C', 'LANG': 'C', 'PYTHONDONTWRITEBYTECODE': '1',
               'MULTILINUX_INTEGRATION': '0', 'MULTILINUX_EVIDENCE': '0',
               'PYTHONPATH': str(source / 'src') + os.pathsep + str(source),
               'UNIFIER_HOME': str(out / 'installation')}

def save():
    (out / 'summary.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')


def command(name, argv, expected=0, env=None, timeout=300):
    completed = subprocess.run(argv, cwd=source, env=env or environment,
                               capture_output=True, text=True, timeout=timeout)
    (out / (name + '.stdout')).write_text(completed.stdout)
    (out / (name + '.stderr')).write_text(completed.stderr)
    report['commands'][name] = {'argv': argv, 'returncode': completed.returncode,
                               'expected': expected, 'passed': completed.returncode == expected}
    save()
    print(json.dumps({'step': name, 'passed': completed.returncode == expected}), flush=True)
    return completed


def cli(name, *argv, expected=0, env=None):
    return command(name, [sys.executable, 'tools/unifier.py', *argv], expected, env)


def inventory():
    ids = subprocess.check_output(['podman', 'ps', '-aq'], text=True).split()
    if not ids:
        return []
    rows = json.loads(subprocess.check_output(['podman', 'inspect', *ids], text=True))
    return sorted([{'id': row['Id'], 'name': row['Name'], 'state': row['State']}
                   for row in rows], key=lambda row: row['id'])

report['before'] = inventory()
save()
try:
    command('offline', [sys.executable, 'tools/check_repo.py'], timeout=600)
    version = cli('version', '--version')
    assert version.stdout.strip() == 'Unifier 0.0.1'
    command('doctor', [sys.executable, 'tools/lab.py', 'doctor'])
    plans = json.loads(cli('matrix', 'compat', 'matrix').stdout)
    report['allowed'] = [p['target'] for p in plans if p['allowed']]
    report['blocked'] = [p['target'] for p in plans if not p['allowed']]
    assert len(report['allowed']) == 2 and len(report['blocked']) == 1
    for target in report['blocked']:
        negative = {**environment, 'UNIFIER_HOME': str(out / 'negative')}
        cli('refuse-init-' + target, 'init', '--target', target, '--yes', expected=1, env=negative)
        cli('refuse-create-' + target, 'env', 'create', '--target', target, '--id', 'forbidden', expected=1, env=negative)
        assert not (out / 'negative/install-state.json').exists()
    command('headless', [sys.executable, 'tools/probe_headless.py', '--output', str(out / 'headless.json')])
    report['headless_passed'] = json.loads((out / 'headless.json').read_text())['passed']
    options = ['init', '--yes', '--network', 'none']
    for target in report['allowed']:
        options.extend(['--target', target])
    assert cli('init', *options).returncode == 0
    for target in report['allowed']:
        files = Path(cli(target + '-files', 'path', target, '--files').stdout.strip())
        assert files.is_relative_to(out / 'installation')
        (files / 'host-marker').write_text('HOST')
        cli(target + '-start', 'start', target)
        cli(target + '-bind', 'env', 'run', target, '--workdir', str(files), '--', 'sh', '-c',
            'test "$(cat host-marker)" = HOST && printf GUEST > guest-marker')
        assert (files / 'guest-marker').read_text() == 'GUEST'
        cli(target + '-exit17', 'env', 'run', target, '--workdir', '/', '--', 'sh', '-c', 'exit 17', expected=17)
        cli(target + '-running-etc', 'ls', target, '/etc', '--json')
        cli(target + '-stop', 'stop', target)
        for path in ['/', '/usr', '/etc']:
            listing = cli(target + '-stopped-' + path.replace('/', '_'), 'ls', target, path, '--json')
            assert listing.returncode == 0, listing.stderr
        # A stopped view must not start its container as a side effect.
        state = json.loads(cli(target + '-state', 'env', 'inspect', target).stdout)
        assert state['runtime_state'] == 'stopped', state
except Exception:
    report['error'] = traceback.format_exc()
finally:
    store = EnvironmentStore(out / 'installation')
    for target in report['allowed']:
        if store.exists(target):
            cli(target + '-cleanup-stop', 'stop', target)
            cli(target + '-cleanup-remove', 'env', 'remove', target, '--yes')
    report['after'] = inventory()
    report['existing_containers_preserved'] = report['after'] == report['before']
    report['remaining_mounts'] = [row for row in Path('/proc/self/mountinfo').read_text().splitlines() if str(out) in row]
    report['passed'] = (not report['error'] and report.get('headless_passed', False)
                        and report['existing_containers_preserved'] and not report['remaining_mounts']
                        and all(c['passed'] for c in report['commands'].values()))
    save()
print(json.dumps({'passed': report['passed'], 'error': report['error'], 'preserved': report['existing_containers_preserved']}, ensure_ascii=False), flush=True)
raise SystemExit(0 if report['passed'] else 1)
