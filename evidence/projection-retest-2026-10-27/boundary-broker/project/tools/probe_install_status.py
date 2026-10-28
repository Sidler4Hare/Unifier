#!/usr/bin/env python3
"""Drive and validate layered install-status transactions on a real Arch env.

Reads a run directory prepared by tools/prepare_install_status.py.  Each case
invokes the snapshot CLI (not an in-process shortcut) so exit codes, stdout and
stderr are the user-visible contract.  Raw JSON and process logs are retained;
failures are recorded instead of being retried silently.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT)]
from multilinux.packages import HostObserver

SUNSHINE = ROOT / ('evidence/arch-complex-2026-10-22T202359+0800/downloads/'
                   'sunshine-2026.914.233613-1-x86_64.pkg.tar.zst')


def stamp():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def refresh_project(run):
    """Replace the snapshot source with the current working tree.

    config/ is deliberately not refreshed: it carries the committed image lock
    created by the preparation step.  File hashes and git state are recorded so
    the evidence says exactly which code ran.
    """
    project = run / 'project'
    for name in ('src', 'tools'):
        target = project / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(ROOT / name, target,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    files = {}
    for path in sorted(project.rglob('*')):
        if path.is_file():
            files[str(path.relative_to(project))] = sha(path)
    head = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True,
                          capture_output=True, check=False).stdout.strip()
    status = subprocess.run(['git', 'status', '--porcelain'], cwd=ROOT, text=True,
                            capture_output=True, check=False).stdout
    manifest = json.loads((run / 'manifest.json').read_text())
    manifest['project'].update({
        'refreshed_at': stamp(), 'git_head': head, 'git_status': status,
        'files': files,
    })
    manifest['probe_source_sha256'] = sha(__file__)
    save(run / 'manifest.json', manifest)
    return manifest['project']


def cli(project, argv, timeout=900):
    command = [sys.executable, str(Path(project) / 'tools/ml.py'), *argv]
    result = subprocess.run(command, capture_output=True, text=True,
                            timeout=timeout, check=False, cwd=project)
    return {
        'argv': argv,
        'command': command,
        'returncode': result.returncode,
        'stdout': result.stdout,
        'stderr': result.stderr,
    }


def transaction(result):
    text = result['stdout'].strip()
    if not text:
        raise ValueError('CLI wrote no JSON transaction')
    return json.loads(text)


def common(txn):
    problems = []
    if txn.get('phase') not in {'completed', 'failed'}:
        problems.append(f"unexpected phase {txn.get('phase')!r}")
    maintenance = txn.get('maintenance')
    if not isinstance(maintenance, dict):
        problems.append('missing maintenance object')
        maintenance = {}
    if maintenance.get('system_integration') != 'unverified':
        problems.append('system_integration must be unverified in a container environment')
    if maintenance.get('full_init') is not False:
        problems.append('full_init must be false')
    if txn.get('verdict') not in {'verified', 'verified-with-warnings', 'failed'}:
        problems.append(f"unexpected verdict {txn.get('verdict')!r}")
    return problems, maintenance


def case_benign(run, project, package):
    result = cli(project, ['pkg', 'install', 'install-arch', '--file', package,
                           '--package', 'ml-benign', '--use-command=/bin/true'])
    txn = transaction(result)
    problems, maintenance = common(txn)
    if txn.get('phase') != 'completed':
        problems.append(f"phase must be completed, got {txn.get('phase')!r}")
    if txn.get('verdict') != 'verified':
        problems.append(f"verdict must be verified, got {txn.get('verdict')!r}")
    if maintenance.get('status') != 'ok':
        problems.append(f"maintenance.status must be ok, got {maintenance.get('status')!r}")
    if maintenance.get('items'):
        problems.append('clean install unexpectedly reported maintenance items')
    if result['returncode'] != 0:
        problems.append(f"CLI exit code {result['returncode']} != 0")
    if 'ml: warning' in result['stderr'].lower():
        problems.append('clean install unexpectedly wrote a warning')
    return result, txn, problems


def case_hook(run, project, package):
    result = cli(project, ['pkg', 'install', 'install-arch', '--file', package,
                           '--package', 'ml-hooktest', '--use-command=/bin/true'])
    txn = transaction(result)
    problems, maintenance = common(txn)
    if txn.get('phase') != 'completed':
        problems.append(f"phase must stay completed for an installed package, got {txn.get('phase')!r}")
    if txn.get('verdict') != 'verified-with-warnings':
        problems.append(f"verdict must be verified-with-warnings, got {txn.get('verdict')!r}")
    if maintenance.get('status') != 'warnings':
        problems.append(f"maintenance.status must be warnings, got {maintenance.get('status')!r}")
    items = maintenance.get('items') or []
    joined = '\n'.join(str(item.get('line') or item.get('message') or '') for item in items)
    if 'command failed to execute correctly' not in joined:
        problems.append('missing pacman hook-failure item')
    if result['returncode'] != 0:
        problems.append(f"CLI exit code {result['returncode']} != 0")
    if 'warning' not in result['stderr'].lower():
        problems.append('CLI did not surface a warning on stderr')
    if not txn.get('verification', {}).get('packages', {}).get('ml-hooktest'):
        problems.append('package ml-hooktest is not recorded as present')
    return result, txn, problems


def case_sunshine(run, project):
    if not SUNSHINE.is_file():
        return None
    result = cli(project, ['pkg', 'install', 'install-arch', '--file', str(SUNSHINE),
                           '--package', 'sunshine',
                           '--use-command=/usr/bin/sunshine', '--use-command=--version'])
    txn = transaction(result)
    problems, maintenance = common(txn)
    if txn.get('phase') != 'completed':
        problems.append(f"phase must be completed, got {txn.get('phase')!r}")
    if txn.get('verdict') != 'verified-with-warnings':
        problems.append(f"verdict must be verified-with-warnings, got {txn.get('verdict')!r}")
    if maintenance.get('status') != 'warnings':
        problems.append(f"maintenance.status must be warnings, got {maintenance.get('status')!r}")
    items = maintenance.get('items') or []
    joined = '\n'.join(str(item.get('line') or item.get('message') or '') for item in items)
    required = ['command failed to execute correctly', 'modprobe', 'systemd']
    for marker in required:
        if marker.lower() not in joined.lower():
            problems.append(f'missing maintenance marker {marker!r}')
    if result['returncode'] != 0:
        problems.append(f"CLI exit code {result['returncode']} != 0")
    if 'warning' not in result['stderr'].lower():
        problems.append('CLI did not surface a warning on stderr')
    return result, txn, problems


def run_case(run, name, function, *args):
    directory = run / 'cases' / name
    directory.mkdir(parents=True, exist_ok=True)
    row = {'name': name, 'started_at': stamp(), 'passed': False,
           'raw': str(directory.relative_to(run))}
    try:
        result, txn, problems = function(run, run / 'project', *args)
        (directory / 'cli.stdout.txt').write_text(result['stdout'])
        (directory / 'cli.stderr.txt').write_text(result['stderr'])
        save(directory / 'transaction.json', txn)
        audit = cli(run / 'project', ['pkg', 'log', 'install-arch'])
        (directory / 'pkg-log.json').write_text(audit['stdout'])
        show = cli(run / 'project', ['pkg', 'show', txn['id']])
        (directory / 'pkg-show.json').write_text(show['stdout'])
        row.update({'returncode': result['returncode'], 'stderr': result['stderr'][-1000:],
                    'transaction_id': txn.get('id'), 'phase': txn.get('phase'),
                    'verdict': txn.get('verdict'),
                    'maintenance_status': (txn.get('maintenance') or {}).get('status'),
                    'maintenance_items': (txn.get('maintenance') or {}).get('items') or [],
                    'problems': problems, 'audit_returncode': audit['returncode'],
                    'show_returncode': show['returncode']})
        if 'maintenance' not in (json.loads(show['stdout']) if show['stdout'].strip() else {}):
            row['problems'].append('pkg show output lacks maintenance field')
        if audit['returncode'] != 0:
            row['problems'].append(f'pkg log exited {audit["returncode"]}')
        log = json.loads(audit['stdout']) if audit['stdout'].strip() else []
        if not any(item.get('verdict') == txn.get('verdict') for item in log):
            row['problems'].append('pkg log does not expose the new verdict')
        row['passed'] = not row['problems']
    except Exception as exc:
        row['error'] = f'{type(exc).__name__}: {exc}'
        row['passed'] = False
    row['finished_at'] = stamp()
    save(directory / 'result.json', row)
    return row


def reset_environment(run):
    """Recreate the prepared environment from the committed image.

    A reinstall of the same package version does not run a hook that belongs to
    that package again, so the hook case needs a package that was never seen by
    this container.  Removing the environment and creating it again from the
    locked image restores that condition and is itself recorded.
    """
    project = run / 'project'
    removed = cli(project, ['env', 'remove', 'install-arch', '--yes'])
    if removed['returncode'] != 0 and 'does not exist' not in removed['stderr']:
        raise RuntimeError(f"cannot remove old environment: {removed['stderr'][-1000:]}")
    created = cli(project, ['env', 'create', '--target', 'arch', '--id', 'install-arch',
                            '--network', 'pasta'])
    if created['returncode'] != 0:
        raise RuntimeError(f"cannot create fresh environment: {created['stderr'][-1500:]}")
    inspected = json.loads(cli(project, ['env', 'inspect', 'install-arch'])['stdout'])
    save(run / 'environment.json', inspected)
    inventory_path = project / inspected['package_inventory_path']
    if inventory_path.is_file():
        shutil.copyfile(inventory_path, run / 'environment-packages.txt')
    manifest = json.loads((run / 'manifest.json').read_text())
    manifest['environment'].update({
        'id': inspected['id'], 'writable_revision': inspected['writable_revision'],
        'content_revision': inspected['content_revision'],
        'package_inventory_sha256': inspected['package_inventory_sha256'],
        'reset_at': stamp(),
    })
    save(run / 'manifest.json', manifest)
    return inspected


def host_state():
    snapshot = HostObserver().snapshot()
    return {'packages': snapshot['packages'], 'entries': snapshot['entries']}


def compare_host(before, after):
    before_packages = before.get('packages') or {}
    after_packages = after.get('packages') or {}
    before_names = (before.get('entries') or {}).get('names') or []
    after_names = (after.get('entries') or {}).get('names') or []
    return {
        'packages_unchanged': (before_packages.get('sha256') == after_packages.get('sha256')
                               and before_packages.get('count') == after_packages.get('count')),
        'desktop_entries_unchanged': before_names == after_names,
        'before_packages': before_packages, 'after_packages': after_packages,
        'before_entries': before_names, 'after_entries': after_names,
    }


def inspect_state(run):
    result = cli(run / 'project', ['env', 'inspect', 'install-arch'])
    record = json.loads(result['stdout'])
    lease = run / 'project/.lab/leases/install-arch.json'
    state = json.loads(lease.read_text()) if lease.is_file() else None
    return {'runtime_state': record['runtime_state'],
            'resources': record.get('resources'),
            'content_revision': record.get('content_revision'),
            'lease_state': state, 'inspect_returncode': result['returncode']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run-dir', type=Path, required=True)
    p.add_argument('--skip-sunshine', action='store_true')
    p.add_argument('--refresh-project', action='store_true',
                   help='replace project/src and project/tools with the current tree')
    p.add_argument('--fresh-environment', action='store_true',
                   help='remove and recreate install-arch from the locked image')
    a = p.parse_args()
    run = a.run_dir.expanduser().resolve()
    if a.refresh_project:
        refresh_project(run)
    if a.fresh_environment:
        reset_environment(run)
    manifest = json.loads((run / 'manifest.json').read_text())
    packages = {item['name']: item for item in manifest['packages']}
    host_before = host_state()
    cases = []
    cases.append(run_case(run, 'benign', case_benign, packages['ml-benign']['path']))
    cases.append(run_case(run, 'hook-failure', case_hook, packages['ml-hooktest']['path']))
    if not a.skip_sunshine:
        cases.append(run_case(run, 'sunshine-real', case_sunshine))
    state = inspect_state(run)
    host_after = host_state()
    summary = {'schema': 'multilinux.install-status.summary.v1', 'observed_at': stamp(),
               'cases': [{key: value for key, value in row.items() if key != 'maintenance_items'}
                         for row in cases],
               'environment_after': state,
               'host': compare_host(host_before, host_after)}
    save(run / 'summary.json', summary)
    host_ok = (summary['host']['packages_unchanged']
               and summary['host']['desktop_entries_unchanged'])
    valid = bool(cases) and all(row['passed'] for row in cases) and host_ok
    validation = {'schema': 'multilinux.install-status.validation.v1',
                  'measurement_valid': valid,
                  'product_verdict': ('layered-status-verified' if valid
                                      else 'requires-review'),
                  'environment_stopped': state['runtime_state'] == 'stopped',
                  'host_unchanged': host_ok,
                  'cases': len(cases), 'passed': sum(row['passed'] for row in cases),
                  'run_dir': str(run)}
    save(run / 'validation.json', validation)
    print(json.dumps({'run_dir': str(run), 'summary': summary['cases'],
                      'validation': validation}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
