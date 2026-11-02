#!/usr/bin/env python3
"""Throwaway rootless environment-view probe.

Checks the first prototype gate from the 2026-10-23 file-layer study:
browsing a stopped container root, absolute versus relative symlinks,
a separate volume, UID/GID, a host write, and cleanup.

Uses only the locked Debian image already on this machine. Does not pull,
install packages, relabel host directories, or touch containers that
already existed. label=disable avoids SELinux relabel of this temporary
container; it is not a product default.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import pwd
import subprocess
import time
import uuid
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
STORAGE = Path.home() / '.local/share/containers/storage'


def run(argv, timeout=60, stdin=None):
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            argv, text=True, capture_output=True, timeout=timeout, input=stdin,
            encoding='utf-8', errors='replace')
        error = None
        code = proc.returncode
        stdout, stderr = proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as exc:
        error = 'timeout'
        code = None
        stdout = exc.stdout or ''
        stderr = exc.stderr or ''
        if isinstance(stdout, bytes):
            stdout = stdout.decode('utf-8', 'replace')
        if isinstance(stderr, bytes):
            stderr = stderr.decode('utf-8', 'replace')
    return {
        'argv': argv, 'exit_code': code, 'stdout': stdout, 'stderr': stderr,
        'seconds': round(time.perf_counter() - started, 6), 'error': error,
    }


def lines_of(name_cmd):
    result = run(name_cmd)
    if result['exit_code'] != 0:
        return result, None
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


def first_line(path):
    try:
        with path.open('r', encoding='utf-8', errors='replace') as handle:
            return {'text': handle.readline().rstrip('\n')}
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}


def read_follow(path):
    try:
        data = path.read_text(encoding='utf-8', errors='replace')
        return {'text': data}
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}


def link_target(path):
    try:
        return {'target': os.readlink(path)}
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}


def file_stat(path):
    try:
        st = path.lstat()
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}
    info = {
        'uid': st.st_uid, 'gid': st.st_gid, 'mode': oct(st.st_mode),
        'is_symlink': path.is_symlink(), 'is_dir': stat_isdir(st),
    }
    try:
        info['selinux'] = os.getxattr(path, 'security.selinux').decode('utf-8', 'replace').rstrip('\x00')
    except OSError as exc:
        info['selinux_error'] = exc.__class__.__name__
    return info


def stat_isdir(st):
    return (st.st_mode & 0o170000) == 0o040000


def under_storage(path):
    try:
        Path(path).resolve().relative_to(STORAGE.resolve())
    except (OSError, ValueError):
        return False
    return True


def exclusive_write(directory, name, payload):
    """Create one file inside an already-mounted throwaway root."""
    if not under_storage(directory):
        return {'refused': 'directory is outside container storage'}
    try:
        dirfd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}
    try:
        fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644, dir_fd=dirfd)
    except OSError as exc:
        os.close(dirfd)
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}
    try:
        os.write(fd, payload)
        os.fsync(fd)
    finally:
        os.close(fd)
        os.close(dirfd)
    written = Path(directory) / name
    return {'written': True, 'stat': file_stat(written), 'readback': read_follow(written)}


def list_names(path):
    try:
        return {'names': sorted(os.listdir(path))[:40], 'count': len(os.listdir(path))}
    except OSError as exc:
        return {'error': exc.__class__.__name__, 'errno': exc.errno, 'message': os.strerror(exc.errno)}


def command_brief(result):
    return {key: result[key] for key in ('exit_code', 'seconds', 'stdout', 'stderr', 'error')}


def mount_path_of(stdout):
    paths = [line.strip() for line in stdout.splitlines() if line.strip().startswith('/')]
    return paths[-1] if paths else ''


def mount_container(name):
    """Direct mount is expected to fail rootless. The supported entry is unshare."""
    direct = run(['podman', 'mount', name], timeout=40)
    unshare = run(['podman', 'unshare', 'podman', 'mount', name], timeout=40)
    path = mount_path_of(unshare['stdout']) or mount_path_of(direct['stdout'])
    result = {
        'exit_code': unshare['exit_code'],
        'seconds': round(direct['seconds'] + unshare['seconds'], 6),
        'direct_seconds': direct['seconds'],
        'unshare_seconds': unshare['seconds'],
        'mount_path': path,
        'under_storage': bool(path and under_storage(path)),
        'stderr': unshare['stderr'],
        'error': unshare['error'],
        'direct': command_brief(direct),
        'unshare': command_brief(unshare),
    }
    if path:
        result['findmnt'] = run(
            ['findmnt', '-n', '-o', 'FSTYPE,SOURCE,TARGET', '-T', path], timeout=15)
    return result


def mount_brief(result):
    keys = ('exit_code', 'seconds', 'direct_seconds', 'unshare_seconds', 'mount_path',
            'under_storage', 'stderr', 'error', 'direct', 'unshare', 'findmnt')
    return {key: result.get(key) for key in keys}


def umount_container(name):
    return {
        'unshare': command_brief(run(['podman', 'unshare', 'podman', 'umount', name], timeout=40)),
        'direct': command_brief(run(['podman', 'umount', name], timeout=40)),
    }


def observe_mount(mount_path, base):
    root = Path(mount_path)
    view = root / base.lstrip('/')
    os_link = view / 'os-link'
    observed = {
        'root_listing': list_names(root),
        'base_listing': list_names(view),
        'private': read_follow(view / 'private.txt'),
        'private_stat': file_stat(view / 'private.txt'),
        'relative_link': link_target(view / 'rel-link'),
        'relative_read': read_follow(view / 'rel-link'),
        'absolute_link': link_target(view / 'abs-link'),
        'absolute_read': read_follow(view / 'abs-link'),
        'absolute_realpath': None,
        'os_link': link_target(os_link),
        'os_link_read': read_follow(os_link),
        'rootfs_os_release': first_line(root / 'etc/os-release'),
        'merged_volume_listing': list_names(root / 'var/ml-volume'),
        'merged_volume_read': read_follow(root / 'var/ml-volume/payload.txt'),
        'volume_link_read': read_follow(view / 'vol-link'),
    }
    try:
        observed['absolute_realpath'] = os.path.realpath(view / 'abs-link')
    except OSError as exc:
        observed['absolute_realpath'] = {'error': exc.__class__.__name__, 'errno': exc.errno}
    observed['unshare_os_link'] = run(['podman', 'unshare', 'cat', str(os_link)], timeout=30)
    observed['unshare_chroot_os_link'] = run(
        ['podman', 'unshare', 'chroot', mount_path, 'cat', f'{base}/os-link'], timeout=30)
    observed['unshare_chroot_abs'] = run(
        ['podman', 'unshare', 'chroot', mount_path, 'cat', f'{base}/abs-link'], timeout=30)
    observed['unshare_chroot_rel'] = run(
        ['podman', 'unshare', 'chroot', mount_path, 'cat', f'{base}/rel-link'], timeout=30)
    observed['unshare_chroot_volume'] = run(
        ['podman', 'unshare', 'chroot', mount_path, 'cat', '/var/ml-volume/payload.txt'], timeout=30)
    observed['unshare_root_list'] = run(['podman', 'unshare', 'ls', mount_path], timeout=30)
    observed['unshare_private_stat'] = run(
        ['podman', 'unshare', 'stat', '-c', 'uid=%u gid=%g mode=%a',
         str(root / 'var/ml-persistent/private.txt')], timeout=30)
    return observed


def observe_upper(upper):
    root = Path(upper)
    private = root / 'var/ml-persistent/private.txt'
    return {
        'host_list': list_names(root),
        'host_private': read_follow(private),
        'host_private_stat': file_stat(private),
        'host_volume_dir': list_names(root / 'var/ml-volume'),
        'unshare_private': run(['podman', 'unshare', 'cat', str(private)], timeout=30),
        'unshare_private_stat': run(
            ['podman', 'unshare', 'stat', '-c', 'uid=%u gid=%g mode=%a', str(private)], timeout=30),
        'unshare_volume_dir': run(['podman', 'unshare', 'ls', '-la', str(root / 'var/ml-volume')], timeout=30),
    }


def observe_volume_source(source):
    root = Path(source)
    payload = root / 'payload.txt'
    user_payload = root / 'user-payload.txt'
    return {
        'host_list': list_names(root),
        'host_dir_stat': file_stat(root),
        'host_payload': read_follow(payload),
        'host_payload_stat': file_stat(payload),
        'host_user_payload': read_follow(user_payload),
        'host_user_payload_stat': file_stat(user_payload),
        'unshare_payload': run(['podman', 'unshare', 'cat', str(payload)], timeout=30),
        'unshare_payload_stat': run(
            ['podman', 'unshare', 'stat', '-c', 'uid=%u gid=%g mode=%a', str(payload)], timeout=30),
    }


ROOT_SETUP = r'''
mkdir -p /var/ml-persistent/target
echo container-private > /var/ml-persistent/private.txt
echo container-target > /var/ml-persistent/target/payload.txt
ln -sfn /var/ml-persistent/target/payload.txt /var/ml-persistent/abs-link
ln -sfn target/payload.txt /var/ml-persistent/rel-link
ln -sfn /etc/os-release /var/ml-persistent/os-link
if echo volume-payload > /var/ml-volume/payload.txt; then
    ln -sfn /var/ml-volume/payload.txt /var/ml-persistent/vol-link
    echo volume_write=ok
else
    echo volume_write=fail
fi
echo root_euid=$(id -u)
echo root_egid=$(id -g)
stat -c 'stat %n uid=%u gid=%g mode=%a' /var/ml-persistent/private.txt /var/ml-volume /var/ml-volume/payload.txt
while IFS= read -r line; do
    case "$line" in
        *' /var/ml-volume '*) printf 'mountinfo=%s\n' "$line" ;;
    esac
done < /proc/self/mountinfo
echo probe_done=root
'''

USER_SETUP = r'''
echo euid=$(id -u)
echo egid=$(id -g)
if mkdir -p /home/mlview/target; then
    echo container-private > /home/mlview/private.txt
    echo user-target > /home/mlview/target/payload.txt
    ln -sfn /home/mlview/target/payload.txt /home/mlview/abs-link
    ln -sfn target/payload.txt /home/mlview/rel-link
    ln -sfn /etc/os-release /home/mlview/os-link
    echo user_home_write=ok
    stat -c 'stat %n uid=%u gid=%g mode=%a' /home/mlview/private.txt
else
    echo user_home_write=fail
fi
if echo user-volume-payload > /var/ml-volume/user-payload.txt; then
    echo user_volume_write=ok
    stat -c 'stat %n uid=%u gid=%g mode=%a' /var/ml-volume/user-payload.txt
else
    echo user_volume_write=fail
fi
echo probe_done=user
'''

VERIFY = r'''
echo euid=$(id -u)
if test -f /var/ml-persistent/private.txt; then echo persistent=$(cat /var/ml-persistent/private.txt); else echo persistent=absent; fi
if test -f /var/ml-persistent/host-written.txt; then echo host_written=$(cat /var/ml-persistent/host-written.txt); else echo host_written=absent; fi
if test -f /home/mlview/host-written.txt; then echo home_host_written=$(cat /home/mlview/host-written.txt); else echo home_host_written=absent; fi
if test -f /var/ml-volume/payload.txt; then echo volume=$(cat /var/ml-volume/payload.txt); else echo volume=absent; fi
if test -f /var/ml-volume/user-payload.txt; then echo user_volume=$(cat /var/ml-volume/user-payload.txt); else echo user_volume=absent; fi
if test -f /var/ml-volume/host-volume-write.txt; then echo host_volume=$(cat /var/ml-volume/host-volume-write.txt); else echo host_volume=absent; fi
if test -L /var/ml-persistent/os-link; then IFS= read -r line < /var/ml-persistent/os-link; echo os_inside="$line"; else echo os_inside=absent; fi
echo probe_done=verify
'''


def exec_sh(name, script, user=None):
    argv = ['podman', 'exec']
    if user is not None:
        argv.extend(['-u', user])
    argv.extend(['-i', name, '/bin/sh', '-s'])
    return run(argv, stdin=script, timeout=40)


def subset_info(raw):
    if raw['exit_code'] != 0:
        return raw
    try:
        info = json.loads(raw['stdout'])
    except json.JSONDecodeError as exc:
        return {'error': f'json: {exc}'}
    store = info.get('store') or {}
    host = info.get('host') or {}
    security = (host.get('security') or {})
    return {
        'rootless': (host.get('security') or {}).get('rootless'),
        'selinux_enabled': security.get('selinuxEnabled'),
        'graph_driver': store.get('graphDriverName'),
        'graph_root': store.get('graphRoot'),
        'graph_status': store.get('graphStatus'),
        'run_root': store.get('runRoot'),
    }


def subset_inspect(raw):
    if raw['exit_code'] != 0:
        return raw
    try:
        item = json.loads(raw['stdout'])[0]
    except (json.JSONDecodeError, IndexError, TypeError) as exc:
        return {'error': repr(exc)}
    mounts = []
    for mount in item.get('Mounts') or []:
        mounts.append({key: mount.get(key) for key in ('Type', 'Name', 'Source', 'Destination', 'Driver', 'Mode', 'RW')})
    graph = item.get('GraphDriver') or {}
    return {
        'status': (item.get('State') or {}).get('Status'),
        'userns_mode': (item.get('HostConfig') or {}).get('UsernsMode'),
        'mounts': mounts,
        'graph_driver': graph.get('Name'),
        'graph_data': graph.get('Data') or {},
    }


def text_of(command):
    if not isinstance(command, dict):
        return ''
    if command.get('exit_code') != 0:
        return ''
    return command.get('stdout') or ''


def judge(case, host_os):
    stopped = case.get('stopped_view') or {}
    upper = case.get('upper_stopped') or {}
    volume = case.get('volume_stopped') or {}
    verified = ((case.get('verify') or {}).get('kv') or {})
    os_read = (stopped.get('os_link_read') or {}).get('text')
    os_line = os_read.splitlines()[0] if isinstance(os_read, str) and os_read else None
    chroot = text_of(stopped.get('unshare_chroot_os_link')).splitlines()
    chroot_line = chroot[0] if chroot else None
    unshare_only = text_of(stopped.get('unshare_os_link')).splitlines()
    unshare_line = unshare_only[0] if unshare_only else None
    inside = verified.get('os_inside')
    direct_err = ((case.get('cold_mount') or {}).get('direct') or {}).get('stderr') or ''
    return {
        'direct_mount_refused': 'podman unshare' in direct_err,
        'unshare_mount_ok': (case.get('cold_mount') or {}).get('exit_code') == 0,
        'host_can_list_stopped_root': 'names' in (stopped.get('root_listing') or {}),
        'unshare_can_list_stopped_root': (stopped.get('unshare_root_list') or {}).get('exit_code') == 0,
        'host_direct_absolute_symlink_matches_host': os_line == host_os,
        'user_namespace_without_chroot_matches_host': unshare_line == host_os,
        'chroot_absolute_symlink_matches_container': bool(inside) and chroot_line == inside,
        'relative_symlink_reads_container_target': (stopped.get('relative_read') or {}).get('text') == 'container-target\n',
        'volume_file_visible_in_stopped_merged_root': (stopped.get('merged_volume_read') or {}).get('text') == 'volume-payload\n',
        'host_can_read_upper_private': (upper.get('host_private') or {}).get('text') == 'container-private\n',
        'unshare_can_read_upper_private': text_of(upper.get('unshare_private')) == 'container-private\n',
        'volume_payload_visible_on_host_path': (volume.get('host_payload') or {}).get('text') == 'volume-payload\n',
        'user_volume_visible_on_host_path': (volume.get('host_user_payload') or {}).get('text') == 'user-volume-payload\n',
        'root_host_write': (case.get('root_host_write') or {}).get('written') is True,
        'home_host_write': (case.get('home_host_write') or {}).get('written') is True,
        'volume_host_write': (case.get('volume_host_write') or {}).get('written') is True,
        'restart_sees_root_host_write': verified.get('host_written') == 'host-view-write',
        'restart_sees_volume': verified.get('volume') == 'volume-payload',
        'restart_sees_user_volume': verified.get('user_volume') == 'user-volume-payload',
        'restart_sees_host_volume_write': verified.get('host_volume') == 'host-volume-write',
        'user_home_write': (case.get('user_setup_kv') or {}).get('user_home_write'),
        'user_volume_write': (case.get('user_setup_kv') or {}).get('user_volume_write'),
        'root_volume_write': (case.get('root_setup_kv') or {}).get('volume_write'),
    }


def one_mode(image, mode, userns_args, host_os):
    token = uuid.uuid4().hex[:12]
    name = f'ml-viewproto-{token}'
    volume = f'ml-viewproto-{token}'
    case = {'mode': mode, 'container': name, 'volume': volume, 'cleanup': []}
    created = {'container': False, 'volume': False}
    try:
        case['volume_create'] = run(['podman', 'volume', 'create', volume], timeout=30)
        created['volume'] = case['volume_create']['exit_code'] == 0
        argv = [
            'podman', 'create', '--name', name, '--pull=never', '--network=none',
            '--cap-drop=ALL', '--security-opt=no-new-privileges',
            '--security-opt=label=disable', '--http-proxy=false',
            '--volume', f'{volume}:/var/ml-volume', '--entrypoint', '/bin/sleep',
            *userns_args, image, '3600']
        case['create'] = run(argv, timeout=40)
        created['container'] = case['create']['exit_code'] == 0
        if not created['container']:
            return case
        case['start'] = run(['podman', 'start', name], timeout=40)
        if case['start']['exit_code'] != 0:
            return case
        case['user_setup'] = exec_sh(name, USER_SETUP)
        case['user_setup_kv'], case['user_setup_stat'] = parse_kv(case['user_setup']['stdout'])
        case['root_setup'] = exec_sh(name, ROOT_SETUP, user='0')
        case['root_setup_kv'], case['root_setup_stat'] = parse_kv(case['root_setup']['stdout'])
        case['stop'] = run(['podman', 'stop', '-t', '0', name], timeout=30)
        case['inspect_stopped'] = subset_inspect(run(['podman', 'inspect', name], timeout=30))
        upper = (case['inspect_stopped'].get('graph_data') or {}).get('UpperDir')
        if upper and under_storage(upper):
            case['upper_stopped'] = observe_upper(upper)
        volume_source = None
        for mount in (case['inspect_stopped'].get('mounts') or []):
            if mount.get('Destination') == '/var/ml-volume':
                volume_source = mount.get('Source')
        case['volume_source'] = volume_source
        if volume_source and under_storage(volume_source):
            case['volume_stopped'] = observe_volume_source(volume_source)
            case['volume_host_write'] = exclusive_write(
                Path(volume_source), 'host-volume-write.txt', b'host-volume-write\n')

        cold = mount_container(name)
        case['cold_mount'] = mount_brief(cold)
        if cold['exit_code'] == 0 and cold['under_storage']:
            case['stopped_view'] = observe_mount(cold['mount_path'], '/var/ml-persistent')
            case['root_host_write'] = exclusive_write(
                Path(cold['mount_path']) / 'var/ml-persistent', 'host-written.txt', b'host-view-write\n')
            home = Path(cold['mount_path']) / 'home/mlview'
            if home.is_dir():
                case['home_host_write'] = exclusive_write(home, 'host-written.txt', b'home-host-write\n')
            else:
                case['home_host_write'] = {'skipped': 'app home was not created'}
        case['cold_umount'] = umount_container(name)

        warm = mount_container(name)
        case['warm_mount'] = mount_brief(warm)
        if warm['exit_code'] == 0 and warm['under_storage']:
            view = Path(warm['mount_path']) / 'var/ml-persistent'
            case['warm_reread'] = {
                'private': read_follow(view / 'private.txt'),
                'host_written': read_follow(view / 'host-written.txt'),
                'os_link_read': read_follow(view / 'os-link'),
                'unshare_private': run(
                    ['podman', 'unshare', 'cat', str(view / 'private.txt')], timeout=30),
            }
        case['warm_umount'] = umount_container(name)

        case['restart'] = run(['podman', 'start', name], timeout=40)
        if case['restart']['exit_code'] == 0:
            running = mount_container(name)
            case['running_mount'] = mount_brief(running)
            if running['exit_code'] == 0 and running['under_storage']:
                view = Path(running['mount_path']) / 'var/ml-persistent'
                case['running_view_read'] = {
                    'private': read_follow(view / 'private.txt'),
                    'os_link_read': read_follow(view / 'os-link'),
                    'note': 'read only; this probe does not write a live overlay',
                }
                case['running_umount'] = umount_container(name)
            case['verify_cmd'] = exec_sh(name, VERIFY, user='0')
            case['verify'] = {}
            case['verify']['kv'], case['verify']['stat'] = parse_kv(case['verify_cmd']['stdout'])
            case['stop_after_verify'] = run(['podman', 'stop', '-t', '0', name], timeout=30)
        case['judgment'] = judge(case, host_os)
        return case
    except Exception as exc:
        case['error'] = repr(exc)
        return case
    finally:
        if created['container']:
            case['cleanup'].append(umount_container(name))
            case['cleanup'].append(run(['podman', 'stop', '-t', '0', name], timeout=30))
            case['cleanup'].append(run(['podman', 'rm', '-f', name], timeout=40))
        if created['volume']:
            case['cleanup'].append(run(['podman', 'volume', 'rm', '-f', volume], timeout=40))
        mounted = run(['findmnt', '-n', '-o', 'TARGET,SOURCE'], timeout=20)
        upper = ((case.get('inspect_stopped') or {}).get('graph_data') or {}).get('UpperDir') or ''
        needles = [item for item in (name, volume, upper) if item]
        if mounted['exit_code'] == 0:
            case['leftover_mounts'] = [
                line for line in mounted['stdout'].splitlines()
                if any(needle and needle in line for needle in needles)]
        else:
            case['leftover_mounts'] = {'error': mounted}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error('output already exists; use a new name')
    locks = json.loads((ROOT / 'config/images.lock.json').read_text())['targets']
    image = locks['debian13']['locked_reference']
    host_os = first_line(Path('/etc/os-release')).get('text')
    report = {
        'time': dt.datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
        'purpose': 'stopped rootless rootfs view, symlink scope, volume, uid, host write',
        'product_acceptance': False,
        'image': image,
        'label_disable_scope': 'temporary probe containers only; not a product default',
        'host_uid': os.getuid(),
        'host_gid': os.getgid(),
        'host_user': pwd.getpwuid(os.getuid()).pw_name,
        'host_os_release_first': host_os,
        'podman': run(['podman', '--version']),
        'kernel': os.uname().release,
        'store': subset_info(run(['podman', 'info', '--format', 'json'], timeout=40)),
    }
    report['subuid'] = None
    subuid_path = Path('/etc/subuid')
    if subuid_path.exists():
        matches = []
        for line in subuid_path.read_text().splitlines():
            owner = line.split(':', 1)[0]
            if owner in {report['host_user'], str(report['host_uid'])}:
                matches.append(line)
        report['subuid'] = matches
    before_cmd, before = lines_of(['podman', 'ps', '-aq', '--no-trunc'])
    volumes_before_cmd, volumes_before = lines_of(['podman', 'volume', 'ls', '-q'])
    report['containers_before'] = before_cmd
    report['volumes_before'] = volumes_before_cmd
    report['image_exists'] = run(['podman', 'image', 'exists', image])
    report['unshare_id'] = run(
        ['podman', 'unshare', 'sh', '-c',
         'id; printf "uid_map="; cat /proc/self/uid_map; printf "gid_map="; cat /proc/self/gid_map'],
        timeout=30)
    report['cases'] = []
    if report['image_exists']['exit_code'] != 0 or before is None or volumes_before is None:
        report['error'] = 'locked image missing or inventory failed'
    else:
        try:
            report['cases'].append(one_mode(image, 'default-rootless', [], host_os))
            report['cases'].append(one_mode(image, 'keep-id', ['--userns=keep-id'], host_os))
        except Exception as exc:
            report['error'] = repr(exc)
    after_cmd, after = lines_of(['podman', 'ps', '-aq', '--no-trunc'])
    volumes_after_cmd, volumes_after = lines_of(['podman', 'volume', 'ls', '-q'])
    report['containers_after'] = after_cmd
    report['volumes_after'] = volumes_after_cmd
    report['container_set_unchanged'] = before == after and before is not None
    report['volume_set_unchanged'] = volumes_before == volumes_after and volumes_before is not None
    leftover = []
    for case in report['cases']:
        for key in ('container', 'volume'):
            if case.get(key):
                leftover.append(case[key])
    report['names_created'] = leftover
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    brief = {
        'time': report['time'],
        'error': report.get('error'),
        'container_set_unchanged': report['container_set_unchanged'],
        'volume_set_unchanged': report['volume_set_unchanged'],
        'cases': [{
            'mode': case['mode'],
            'cold_mount_seconds': (case.get('cold_mount') or {}).get('seconds'),
            'warm_mount_seconds': (case.get('warm_mount') or {}).get('seconds'),
            'mount_fstype': (((case.get('cold_mount') or {}).get('findmnt') or {}).get('stdout') or '').strip(),
            'judgment': case.get('judgment'),
            'user_home_write': (case.get('user_setup_kv') or {}).get('user_home_write'),
            'root_euid': (case.get('root_setup_kv') or {}).get('root_euid'),
            'user_euid': (case.get('user_setup_kv') or {}).get('euid'),
        } for case in report['cases']],
    }
    print(json.dumps(brief, ensure_ascii=False, indent=2))
    leaked = [case.get('leftover_mounts') for case in report['cases'] if case.get('leftover_mounts')]
    ok = (not report.get('error') and report['container_set_unchanged']
          and report['volume_set_unchanged'] and len(report['cases']) == 2 and not leaked)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
