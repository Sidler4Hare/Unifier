"""Real-kernel FUSE regressions; temporary backing only, no containers."""
import argparse
import errno
import json
import mmap
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import uuid


def mounted(path):
    value = str(path).replace(' ', '\\040')
    return any(row.split()[4] == value for row in Path('/proc/self/mountinfo').read_text().splitlines())


def status(path):
    with socket.socket(socket.AF_UNIX) as peer:
        peer.settimeout(5)
        peer.connect(str(path))
        peer.sendall(b'{"op":"status"}\n')
        payload = b''
        while b'\n' not in payload:
            part = peer.recv(65536)
            if not part:
                raise RuntimeError('short control reply')
            payload += part
    return json.loads(payload)


def run(source, out):
    sys.path.insert(0, str(source / 'src'))
    from unifier.guest_paths import list_guest_directories
    from unifier import __version__
    report = {'version': __version__, 'python': platform.python_version(),
              'kernel': platform.release(), 'scope': 'real kernel FUSE, temporary backing; no containers', 'cases': {}}
    with tempfile.TemporaryDirectory(prefix='unifier-v001-fuse-') as tmp:
        root = Path(tmp)
        back, view, provider = [root / x for x in ('back', 'view', 'provider')]
        for path in (back, view, provider):
            path.mkdir()
        for name in ('a', 'b', 'etc/readable'):
            (back / name).mkdir(parents=True)
        (provider / 'payload').write_bytes(b'MOUNTED')
        (back / 'etc/mtab').symlink_to('/proc/mounts')
        (back / 'a/target').write_bytes(b'A')
        (back / 'b/target').write_bytes(b'B')
        (back / 'a/link').symlink_to('target')
        (back / 'file').write_bytes(b'O' * 4096)
        spec = root / 'spec.json'
        spec.write_text(json.dumps({'id_map': dict(host_uid=os.getuid(), host_gid=os.getgid(),
            container_uid=os.getuid(), container_gid=os.getgid(), subuid_start=0, subuid_count=0,
            subgid_start=0, subgid_count=0, user_namespace='keep-id'),
            'mounts': [{'destination': '/a/nested', 'source': str(provider)}], 'capabilities': ['/proc']}))
        ready, control = root / 'ready.json', root / 'control.sock'
        descriptor = os.open(back, os.O_PATH | os.O_DIRECTORY)
        log = (out / 'worker.log').open('wb')
        environment = {**os.environ, 'PYTHONPATH': str(source / 'src'), 'PYTHONDONTWRITEBYTECODE': '1'}
        try:
            worker = subprocess.Popen([sys.executable, '-m', 'unifier.fuse_view',
                '--mountpoint', str(view), '--merged-fd', str(descriptor), '--spec', str(spec),
                '--control-socket', str(control), '--view-uuid', str(uuid.uuid4()),
                '--ready-file', str(ready), '--nonce', uuid.uuid4().hex], env=environment,
                pass_fds=(descriptor,), stdin=subprocess.DEVNULL, stdout=log, stderr=log)
        finally:
            os.close(descriptor)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists():
                if worker.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError('worker not ready; see worker.log')
                time.sleep(.03)
            assert mounted(view)
            report['initial'] = status(control)
            listing = list_guest_directories(view, '/etc', projected=True)
            assert listing['entries'] == ['readable'], listing
            try:
                list_guest_directories(view, '/etc/mtab', projected=True)
            except OSError as error:
                assert error.errno in (errno.EACCES, errno.EPERM, errno.EOPNOTSUPP), error
            else:
                raise AssertionError('restricted proc link unexpectedly exported')
            report['cases']['etc_restricted_link'] = listing

            assert (view / 'a/link').read_bytes() == b'A'
            assert (view / 'a/nested/payload').read_bytes() == b'MOUNTED'
            held = os.open(view / 'a/nested/payload', os.O_RDONLY)
            try:
                (back / 'a/link').rename(back / 'b/link')
                assert (view / 'b/link').read_bytes() == b'B'
                (back / 'a').rename(back / 'renamed')
                assert 'nested' not in os.listdir(view / 'renamed')
                assert os.pread(held, 100, 0) == b'MOUNTED'
                report['cases']['rename_link_mount_and_old_fd'] = True
            finally:
                os.close(held)

            fd = os.open(view / 'file', os.O_RDONLY)
            mapping = mmap.mmap(fd, 4096, access=mmap.ACCESS_READ)
            collisions = 0
            try:
                assert mapping[:] == b'O' * 4096
                for i in range(50):
                    before = (back / 'file').stat()
                    payload = bytes([65 + i % 26]) * 4096
                    with (back / 'file').open('r+b') as stream:
                        stream.write(payload)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.utime(back / 'file', ns=(before.st_atime_ns, before.st_mtime_ns))
                    after = (back / 'file').stat()
                    collisions += (before.st_ctime_ns == after.st_ctime_ns)
                    os.fstat(fd)  # explicit kernel attribute revalidation
                    assert os.pread(fd, 4096, 0) == payload, i
                    assert mapping[:] == payload, i
                report['cases']['rapid_same_size_same_mtime'] = {'trials': 50,
                    'raw_ctime_collisions': collisions, 'pread_and_mmap_correct_after_revalidation': True}
                old = os.pread(fd, 4096, 0)
                (back / 'replacement').write_bytes(b'NEW-OBJECT')
                os.replace(back / 'replacement', back / 'file')
                assert (view / 'file').read_bytes() == b'NEW-OBJECT'
                assert os.pread(fd, 4096, 0) == old
                assert mapping[:] == old
                report['cases']['replace_preserves_old_fd_and_mmap'] = True
            finally:
                mapping.close()
                os.close(fd)
            try:
                (view / 'file').write_bytes(b'WRONG')
            except OSError as error:
                assert error.errno == errno.EROFS, error
                report['cases']['readonly'] = True
            else:
                raise AssertionError('view accepted a write')
            report['final'] = status(control)
        except Exception:
            report['error'] = traceback.format_exc()
        finally:
            unmount = subprocess.run(['fusermount3', '-u', str(view)], capture_output=True, text=True, timeout=10)
            report['cleanup'] = {'unmount_returncode': unmount.returncode, 'not_mounted': not mounted(view)}
            if mounted(view):
                raise RuntimeError('normal unmount failed; backing retained until recovery: ' + unmount.stderr)
            try:
                worker.wait(timeout=8)
            except subprocess.TimeoutExpired:
                worker.terminate()
                worker.wait(timeout=8)
            log.close()
            report['cleanup']['worker_returncode'] = worker.returncode
    report['passed'] = not report.get('error') and len(report['cases']) == 5 and report['cleanup']['not_mounted']
    (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'passed': report['passed'], 'cases': report['cases'], 'cleanup': report['cleanup'], 'error': report.get('error')}, ensure_ascii=False, indent=2))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    raise SystemExit(run(args.source.resolve(), args.out.resolve()))
