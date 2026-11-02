#!/usr/bin/env python3
"""Write a stopped rootfs only through podman unshare, then read it back.

Separate from result-unshare.json. Does not pull, install packages, write an
overlay upper directory by path, or touch containers that already existed.
"""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
STORAGE = Path.home() / '.local/share/containers/storage'

SETUP = r'''
mkdir -p /var/ml-persistent
echo container-private > /var/ml-persistent/private.txt
echo probe_done=setup
'''

VERIFY = r'''
echo euid=$(id -u)
if test -f /var/ml-persistent/from-namespace.txt; then
    echo namespace=$(cat /var/ml-persistent/from-namespace.txt)
    stat -c 'stat_namespace uid=%u gid=%g mode=%a' /var/ml-persistent/from-namespace.txt
else
    echo namespace=absent
fi
if test -f /var/ml-persistent/from-chroot.txt; then
    echo chroot=$(cat /var/ml-persistent/from-chroot.txt)
    stat -c 'stat_chroot uid=%u gid=%g mode=%a' /var/ml-persistent/from-chroot.txt
else
    echo chroot=absent
fi
echo private=$(cat /var/ml-persistent/private.txt)
echo probe_done=verify
'''


def run(argv, timeout=40, stdin=None):
    started = time.perf_counter()
    proc = subprocess.run(
        argv, text=True, capture_output=True, timeout=timeout, input=stdin,
        encoding='utf-8', errors='replace')
    return {
        'argv': argv, 'exit_code': proc.returncode, 'stdout': proc.stdout,
        'stderr': proc.stderr, 'seconds': round(time.perf_counter() - started, 6),
    }


def ids(cmd):
    result = run(cmd)
    if result['exit_code'] != 0:
        raise SystemExit(result)
    return result, set(result['stdout'].splitlines())


def parse_kv(text):
    found = {}
    stats = []
    for line in text.splitlines():
        if line.startswith('stat '):
            stats.append(line)
        elif '=' in line:
            key, value = line.split('=', 1)
            found[key] = value
    return found, stats


def host_read(path):
    try:
        return {'text': Path(path).read_text(encoding='utf-8')}
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}


def one(image, mode, userns):
    token = uuid.uuid4().hex[:12]
    name = f'ml-viewsave-{token}'
    case = {'mode': mode, 'container': name, 'cleanup': []}
    created = False
    mount_path = ''
    try:
        argv = [
            'podman', 'create', '--name', name, '--pull=never', '--network=none',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--security-opt=label=disable', '--http-proxy=false',
            '--entrypoint', '/bin/sleep', *userns, image, '3600']
        case['create'] = run(argv)
        created = case['create']['exit_code'] == 0
        if not created:
            return case
        case['start'] = run(['podman', 'start', name])
        case['setup'] = run(['podman', 'exec', '-u', '0', '-i', name, '/bin/sh', '-s'], stdin=SETUP)
        case['stop'] = run(['podman', 'stop', '-t', '0', name])
        writer = r'''
set -eu
name=$1
root=$2
m=$(podman mount "$name")
printf 'mount=%s\n' "$m"
case "$m" in
    "$root"/overlay/*/merged) ;;
    *) echo 'mount_rejected=1'; exit 2 ;;
esac
printf '%s\n' from-namespace > "$m/var/ml-persistent/from-namespace.txt"
chroot "$m" /bin/sh -c 'printf "%s\n" from-chroot > /var/ml-persistent/from-chroot.txt'
stat -c 'stat_namespace uid=%u gid=%g mode=%a' "$m/var/ml-persistent/from-namespace.txt"
stat -c 'stat_chroot uid=%u gid=%g mode=%a' "$m/var/ml-persistent/from-chroot.txt"
echo write_done=1
'''
        case['write'] = run(
            ['podman', 'unshare', 'sh', '-s', name, str(STORAGE)], stdin=writer, timeout=40)
        case['write_kv'], case['write_stat'] = parse_kv(case['write']['stdout'])
        mount_path = case['write_kv'].get('mount', '')
        if mount_path:
            case['host_while_mounted'] = {
                'namespace': host_read(mount_path + '/var/ml-persistent/from-namespace.txt'),
                'chroot': host_read(mount_path + '/var/ml-persistent/from-chroot.txt'),
                'listing_error_or_count': None,
            }
            try:
                case['host_while_mounted']['listing_count'] = len(os.listdir(mount_path))
            except OSError as exc:
                case['host_while_mounted']['listing_error'] = {
                    'error': exc.__class__.__name__, 'errno': exc.errno}
        case['umount'] = run(['podman', 'unshare', 'podman', 'umount', name])
        case['restart'] = run(['podman', 'start', name])
        case['verify_cmd'] = run(
            ['podman', 'exec', '-u', '0', '-i', name, '/bin/sh', '-s'], stdin=VERIFY)
        case['verify_kv'], case['verify_stat'] = parse_kv(case['verify_cmd']['stdout'])
        case['stop_after'] = run(['podman', 'stop', '-t', '0', name])
        return case
    finally:
        if created:
            case['cleanup'].append(run(['podman', 'unshare', 'podman', 'umount', name]))
            case['cleanup'].append(run(['podman', 'umount', name]))
            case['cleanup'].append(run(['podman', 'stop', '-t', '0', name]))
            case['cleanup'].append(run(['podman', 'rm', '-f', name]))


def main():
    out = Path(__file__).resolve().parent / 'result-save.json'
    if out.exists():
        raise SystemExit('result-save.json already exists')
    image = json.loads((ROOT / 'config/images.lock.json').read_text())['targets']['debian13']['locked_reference']
    before_cmd, before = ids(['podman', 'ps', '-aq', '--no-trunc'])
    report = {
        'time': dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'purpose': 'save into a stopped merged root from the storage namespace',
        'product_acceptance': False,
        'image': image,
        'containers_before': before_cmd,
        'cases': [
            one(image, 'default-rootless', []),
            one(image, 'keep-id', ['--userns=keep-id']),
        ],
    }
    after_cmd, after = ids(['podman', 'ps', '-aq', '--no-trunc'])
    report['containers_after'] = after_cmd
    report['container_set_unchanged'] = before == after
    with out.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    brief = []
    for case in report['cases']:
        brief.append({
            'mode': case['mode'],
            'write_exit': (case.get('write') or {}).get('exit_code'),
            'write_kv': case.get('write_kv'),
            'write_stat': case.get('write_stat'),
            'host_while_mounted': case.get('host_while_mounted'),
            'verify': case.get('verify_kv'),
            'verify_stat': case.get('verify_stat'),
            'write_err': ((case.get('write') or {}).get('stderr') or '')[:400],
        })
    print(json.dumps({'unchanged': report['container_set_unchanged'], 'cases': brief},
                     ensure_ascii=False, indent=2))
    return 0 if report['container_set_unchanged'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
