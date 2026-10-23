#!/usr/bin/env python3
"""Bounded architecture probe; no package installs or existing container execs.

Run with --output NEW.json; never overwrites evidence. Temporary containers use
cached locked images, no network, read-only roots and one read-only test mount.
label=disable is explicit here to avoid relabeling even the temporary input.
It is an experiment profile, not the proposed product default.
"""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from multilinux.runtime import CreateSpec, creation_argv
from multilinux.service import EnvironmentService


def run(argv, timeout=45):
    p = subprocess.run(argv, text=True, capture_output=True, timeout=timeout)
    return {'argv': argv, 'exit_code': p.returncode,
            'stdout': p.stdout, 'stderr': p.stderr}


def metadata(path):
    s = path.stat()
    return {'dev': s.st_dev, 'ino': s.st_ino, 'uid': s.st_uid, 'gid': s.st_gid,
            'mode': s.st_mode, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'xattrs': {key: os.getxattr(path, key).hex() for key in os.listxattr(path)}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; use a new name')
    locks = json.loads((ROOT / 'config/images.lock.json').read_text())['targets']
    report = {
        'time': dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'purpose': 'execution context, absolute symlink, mount scope, current validation behavior',
        'product_acceptance': False,
        'podman': run(['podman', '--version']),
        'kernel': os.uname().release,
        'source_sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                          for p in (ROOT / 'src/multilinux').glob('*.py')},
        'containers_before': run(['podman', 'ps', '-aq', '--no-trunc']),
        'cases': [], 'cleanup': [],
    }
    names = []
    try:
        with tempfile.TemporaryDirectory(prefix='ml-context-') as temporary:
            base = Path(temporary)
            shared = base / 'shared'
            shared.mkdir()
            hidden = base / 'host-only-marker'
            hidden.write_text('host-only\n')
            (shared / 'absolute-link').symlink_to('/etc/os-release')
            script = shared / 'context.sh'
            script.write_text('''#!/bin/sh
set -eu
. /etc/os-release
printf 'release=%s\\n' "$ID"
printf 'shell=%s\\n' "$(readlink /proc/$$/exe)"
printf 'shared_inode=%s\\n' "$(stat -c %i "$0")"
printf 'symlink_release='
( . "$(dirname "$0")/absolute-link"; printf '%s\\n' "$ID" )
if test -e "$1"; then echo 'host_marker=visible'; else echo 'host_marker=absent'; fi
if command -v python3 >/dev/null 2>&1; then
    python3 -c 'import sys; print("python=" + sys.executable); print("python_prefix=" + sys.prefix)'
else
    echo 'python=absent-no-fallback'
fi
''')
            script.chmod(0o755)
            report['input_before'] = metadata(script)
            report['cases'].append({'context': 'host', **run([str(script), str(hidden)])})
            for target, lock in locks.items():
                image = lock['locked_reference']
                present = run(['podman', 'image', 'exists', image])
                if present['exit_code']:
                    report['cases'].append({'context': target, 'skipped': 'locked image not cached',
                                            'image_check': present})
                    continue
                name = 'ml-context-' + uuid.uuid4().hex[:12]
                names.append(name)
                argv = ['podman', 'run', '--rm', '--name', name, '--pull=never',
                        '--network=none', '--read-only', '--cap-drop=all',
                        '--security-opt=no-new-privileges', '--security-opt=label=disable',
                        '--http-proxy=false', '--userns=keep-id',
                        '--env=PATH=/usr/bin:/bin', '--volume', f'{shared}:{shared}:ro',
                        '--entrypoint', str(script), image, str(hidden)]
                report['cases'].append({'context': target, 'image': image, **run(argv)})
            report['input_after'] = metadata(script)
            report['input_unchanged'] = report['input_before'] == report['input_after']
            # Pure method calls: record validation behavior without starting a service.
            service = EnvironmentService.__new__(EnvironmentService)
            service.root = ROOT
            record = {'shared_mounts': [{'host': str(shared), 'container': str(shared)}]}
            traversal = str(shared) + '/../host-only-marker'
            report['static_observations'] = {
                'shared_workdir_dotdot_returned': service._shared_workdir(record, traversal),
                'shared_workdir_resolved_outside': not Path(traversal).resolve().is_relative_to(shared),
                'generated_create_argv': creation_argv(CreateSpec(
                    name='not-executed', env_id='probe', project=str(ROOT),
                    image=locks['debian13']['locked_reference'],
                    mounts=((str(shared), str(shared)),))),
                'note': 'accepted dotdot is a validation gap, not proof of access to host-only files',
            }
    except Exception as exc:
        report['error'] = repr(exc)
    finally:
        for name in names:
            exists = run(['podman', 'container', 'exists', name])
            if exists['exit_code'] == 0:
                report['cleanup'].append(run(['podman', 'rm', '-f', name]))
            else:
                report['cleanup'].append({'name': name, 'already_removed': True,
                                           'exists_check': exists})
        report['containers_after'] = run(['podman', 'ps', '-aq', '--no-trunc'])
        before, after = report['containers_before'], report['containers_after']
        report['container_set_unchanged'] = (before['exit_code'] == after['exit_code'] == 0
            and set(before['stdout'].splitlines()) == set(after['stdout'].splitlines()))
        with args.output.open('x') as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
    print(json.dumps({k: report.get(k) for k in ('time', 'error', 'input_unchanged',
                                               'container_set_unchanged')}, ensure_ascii=False))
    for case in report['cases']:
        print(case['context'], case.get('exit_code', 'skipped'), case.get('stdout', ''),
              case.get('stderr', ''))
    return int(bool(report.get('error')) or any(c.get('exit_code', 1) != 0 for c in report['cases'])
               or not report.get('input_unchanged') or not report['container_set_unchanged'])


if __name__ == '__main__':
    raise SystemExit(main())
