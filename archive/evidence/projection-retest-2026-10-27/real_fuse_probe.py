#!/usr/bin/env python3
"""Controller-owned kernel FUSE acceptance, isolated temporary backing data.

The same assertions can run against the frozen pre-repair worker. Failures are
retained as evidence, never converted to skips. No containers or main state.
"""
import argparse
import contextlib
import ctypes
import errno
import hashlib
import json
import mmap
import os
from pathlib import Path
import resource
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import traceback
import uuid


ROOT = Path(__file__).resolve().parents[2]


def control(path):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.settimeout(3)
        peer.connect(str(path))
        peer.sendall(b'{"op":"status"}\n')
        data = b""
        while b"\n" not in data:
            data += peer.recv(65536)
    return json.loads(data)


def mounted(path):
    escaped = str(path).replace(" ", "\\040")
    return any(line.split()[4] == escaped for line in Path('/proc/self/mountinfo').read_text().splitlines())


class Fixture:
    def __init__(self, source, out):
        self.source, self.out = source, out
        self.tmp = tempfile.TemporaryDirectory(prefix='unifier-object-')
        self.root = Path(self.tmp.name)
        self.back = self.root / 'back'
        self.view = self.root / 'view'
        self.provider = self.root / 'provider'
        for p in (self.back, self.view, self.provider):
            p.mkdir()
        (self.provider / 'mounted.txt').write_bytes(b'MOUNTED')
        (self.back / 'mask').write_bytes(b'underlying-file')
        (self.back / 'file-mask').mkdir()
        self.file_provider = self.root / 'file-provider'
        self.file_provider.write_bytes(b'FILE-MOUNT')
        uid, gid = os.getuid(), os.getgid()
        self.spec = self.root / 'spec.json'
        self.spec.write_text(json.dumps({
            'id_map': dict(host_uid=uid, host_gid=gid, container_uid=uid,
                           container_gid=gid, subuid_start=0, subuid_count=0,
                           subgid_start=0, subgid_count=0, user_namespace='keep-id'),
            'mounts': [
                dict(destination='/missing/anc/child', source=str(self.provider)),
                dict(destination='/mask', source=str(self.provider)),
                dict(destination='/file-mask', source=str(self.file_provider)),
                dict(destination='/other-domain', source=str(self.provider)),
            ],
            'capabilities': ['/proc'],
        }))
        self.ready = self.root / 'ready.json'
        self.sock = self.root / 'control.sock'
        fd = os.open(self.back, os.O_PATH | os.O_DIRECTORY)
        self.log = open(out / 'worker.log', 'wb')
        env = dict(os.environ, PYTHONPATH=str(source))
        try:
            self.process = subprocess.Popen([
                sys.executable, '-m', 'multilinux.fuse_view',
                '--mountpoint', str(self.view), '--merged-fd', str(fd),
                '--spec', str(self.spec), '--control-socket', str(self.sock),
                '--view-uuid', str(uuid.uuid4()), '--ready-file', str(self.ready),
                '--nonce', uuid.uuid4().hex], env=env, pass_fds=(fd,),
                stdin=subprocess.DEVNULL, stdout=self.log, stderr=self.log)
        finally:
            os.close(fd)
        deadline = time.monotonic() + 15
        while not self.ready.exists():
            if self.process.poll() is not None or time.monotonic() > deadline:
                self.close()
                raise RuntimeError('real FUSE worker failed readiness; see worker.log')
            time.sleep(.03)
        assert mounted(self.view), 'mountinfo did not contain real FUSE mount'
        self.initial = self.status()

    def status(self):
        return control(self.sock)

    def close(self):
        if hasattr(self, 'process'):
            unmount = subprocess.run(['fusermount3', '-u', str(self.view)], capture_output=True)
            if unmount.returncode and mounted(self.view):
                raise RuntimeError('normal unmount failed: ' + unmount.stderr.decode())
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=8)
            self.cleanup = dict(not_mounted=not mounted(self.view), worker_exit=self.process.returncode,
                                normal_unmount=True)
            self.log.close()
        self.tmp.cleanup()


def replace_case(f, old_size, new_size, primed):
    name = 'replace-%s-%s-%s' % (old_size, new_size, primed)
    path = f.back / name
    old, new = b'O' * old_size, b'N' * new_size
    path.write_bytes(old)
    host = os.open(path, os.O_RDONLY)
    fd = os.open(f.view / name, os.O_RDONLY)
    try:
        old_stat = os.fstat(fd)
        if primed:
            assert os.pread(fd, old_size + 1024, 0) == old
        replacement = f.back / 'replacement.tmp'
        replacement.write_bytes(new)
        os.replace(replacement, path)
        # No TTL sleeps: the new binding and attributes must be coherent now.
        fresh = (f.view / name).read_bytes()
        fresh_stat = os.stat(f.view / name)
        held = os.pread(fd, old_size + new_size + 1024, 0)
        held_stat = os.fstat(fd)
        observed = dict(old_size=old_size, new_size=new_size, primed=primed,
                        new_read_size=len(fresh), held_read_size=len(held),
                        old_ino=old_stat.st_ino, new_ino=fresh_stat.st_ino,
                        held_stat_size=held_stat.st_size)
        assert fresh == new, observed
        assert held == os.pread(host, old_size + new_size + 1024, 0) == old, observed
        assert fresh_stat.st_size == new_size and held_stat.st_size == old_size, observed
        assert old_stat.st_ino == held_stat.st_ino != fresh_stat.st_ino, observed
        return observed
    finally:
        os.close(fd)
        os.close(host)


def unlink_case(f):
    p = f.back / 'unlink-open'
    p.write_bytes(b'old unlinked object')
    fd = os.open(f.view / p.name, os.O_RDONLY)
    try:
        ino = os.fstat(fd).st_ino
        p.unlink()
        assert not (f.view / p.name).exists()
        assert os.pread(fd, 999, 0) == b'old unlinked object'
        assert os.fstat(fd).st_size == 19
        p.write_bytes(b'new incarnation')
        assert (f.view / p.name).read_bytes() == b'new incarnation'
        assert os.stat(f.view / p.name).st_ino != ino
        assert os.pread(fd, 999, 0) == b'old unlinked object'
        return dict(old_fd_preserved=True, recreated_path_new_identity=True)
    finally:
        os.close(fd)


def hardlinks_case(f):
    (f.back / 'link-a').write_bytes(b'abcdef')
    os.link(f.back / 'link-a', f.back / 'link-b')
    a, b = (os.open(f.view / n, os.O_RDONLY) for n in ('link-a', 'link-b'))
    try:
        assert os.fstat(a).st_ino == os.fstat(b).st_ino
        assert os.read(a, 2) == os.read(b, 2) == b'ab'
        assert os.read(a, 2) == b'cd'
        return dict(alias_identity=True, independent_file_offsets=True)
    finally:
        os.close(a)
        os.close(b)


def symlink_alias_case(f):
    directory = f.back / 'hardlink-symlink'
    directory.mkdir()
    for name in ('a', 'b'):
        (directory/name).mkdir()
        (directory/name/'target').write_bytes(name.encode())
    os.symlink('target', directory/'a/link')
    os.link(directory/'a/link',directory/'b/link',follow_symlinks=False)
    for name in ('a','b'):
        try:
            os.readlink(f.view/'hardlink-symlink'/name/'link')
        except OSError as exc:
            assert exc.errno==errno.EOPNOTSUPP, exc
        else:
            raise AssertionError('ambiguous relative symlink was silently projected')
    return dict(relative_alias_projection_explicitly_unsupported=True,errno=errno.EOPNOTSUPP)


def inplace_case(f):
    p = f.back / 'inplace'
    p.write_bytes(b'X' * 32)
    fd = os.open(f.view / p.name, os.O_RDONLY)
    try:
        assert os.pread(fd, 1024, 0) == b'X' * 32
        ino = os.fstat(fd).st_ino
        observations = []
        for data in (b'Y' * 128, b'Z' * 32, b'Q' * 32, b'R' * 7):
            with open(p, 'r+b') as stream:
                stream.write(data)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            st = os.fstat(fd)
            read = os.pread(fd, 4096, 0)
            assert st.st_size == len(data) and read == data, (st.st_size, len(read), len(data))
            assert st.st_ino == ino
            observations.append(dict(size=st.st_size, sha256=hashlib.sha256(read).hexdigest()))
        return dict(observations=observations, same_inode_live_not_snapshot=True)
    finally:
        os.close(fd)


def mmap_case(f, primed=True):
    p = f.back / ('mmap-file-' + str(primed))
    p.write_bytes(b'A' * 8192)
    fd = os.open(f.view / p.name, os.O_RDONLY)
    try:
        with mmap.mmap(fd, 0, access=mmap.ACCESS_READ) as old:
            if primed:
                assert old[:] == b'A' * 8192
            replacement = f.back / 'mmap-temp'
            replacement.write_bytes(b'B' * 16384)
            os.replace(replacement, p)
            newfd = os.open(f.view / p.name, os.O_RDONLY)
            try:
                with mmap.mmap(newfd, 0, access=mmap.ACCESS_READ) as new:
                    assert new[:] == b'B' * 16384
                assert old[:] == b'A' * 8192
            finally:
                os.close(newfd)
        return dict(readonly_mmap_supported=True, old_new_object_isolation=True, primed=primed)
    finally:
        os.close(fd)


def concurrent_case(f):
    p = f.back / 'concurrent-replace'
    versions = (b'alpha' * 701, b'beta' * 4103, b'gamma' * 173)
    p.write_bytes(versions[0])
    stop = threading.Event()
    errors = []
    counts = [0]

    def writer():
        try:
            index = 0
            while not stop.is_set():
                tmp = f.back / 'concurrent-temp'
                tmp.write_bytes(versions[index % len(versions)])
                os.replace(tmp, p)
                index += 1
                counts[0] = index
                time.sleep(.0005)
        except Exception:
            errors.append(traceback.format_exc())

    worker = threading.Thread(target=writer)
    worker.start()
    try:
        for _ in range(250):
            fd = os.open(f.view / p.name, os.O_RDONLY)
            try:
                st = os.fstat(fd)
                data = os.pread(fd, 100000, 0)
                assert data in versions and st.st_size == len(data), (st.st_size, len(data))
                # Re-read must stay on the opened object despite a racing rename.
                assert os.pread(fd, 100000, 0) == data
            finally:
                os.close(fd)
        assert not errors, errors
        assert counts[0] > 0
        return dict(open_read_replays=250, atomic_replacements=counts[0], coherent_objects=True)
    finally:
        stop.set()
        worker.join(timeout=5)
        assert not worker.is_alive()


def topology_case(f):
    assert (f.view / 'missing/anc/child/mounted.txt').read_bytes() == b'MOUNTED'
    assert 'missing' in os.listdir(f.view)
    assert 'anc' in os.listdir(f.view / 'missing')
    assert 'child' in os.listdir(f.view / 'missing/anc')
    kinds = {e.name: e.is_dir(follow_symlinks=False) for e in os.scandir(f.view)}
    assert kinds['mask'] is True and kinds['file-mask'] is False, kinds
    assert (f.view / 'mask/mounted.txt').read_bytes() == b'MOUNTED'
    assert (f.view / 'file-mask').read_bytes() == b'FILE-MOUNT'
    assert os.stat(f.view / 'mask/mounted.txt').st_ino != os.stat(f.view / 'other-domain/mounted.txt').st_ino
    return dict(missing_ancestors=True, overlay_types=True, provider_identity_separated=True)


def dirent_identity_case(f):
    p = f.back / 'inode-enumeration'
    p.mkdir()
    for name in ('one', 'two'):
        (p / name).write_bytes(name.encode())
    seen = {}
    with os.scandir(f.view / p.name) as entries:
        for entry in entries:
            st = os.lstat(entry.path)
            assert entry.inode() == st.st_ino, (entry.name, entry.inode(), st.st_ino)
            seen[entry.name] = st.st_ino
    assert len(set(seen.values())) == 2, seen
    return seen


def read_dir_page(fd, size=96):
    libc = ctypes.CDLL(None, use_errno=True)
    buf = ctypes.create_string_buffer(size)
    count = libc.getdents64(fd, buf, size)
    if count < 0:
        raise OSError(ctypes.get_errno(), 'getdents64')
    data, out, pos = buf.raw[:count], [], 0
    while pos < len(data):
        ino, cookie, length, kind = struct.unpack_from('<QqHB', data, pos)
        name = data[pos + 19:pos + length].split(b'\0')[0].decode()
        out.append(dict(name=name, cookie=cookie, ino=ino, kind=kind))
        pos += length
    return out


def directory_case(f):
    path = f.back / 'paged'
    path.mkdir()
    for name in ('a', 'b', 'c', 'd'):
        (path / name).touch()
    fd = os.open(f.view / 'paged', os.O_RDONLY | os.O_DIRECTORY)
    try:
        first = read_dir_page(fd)
        (path / 'a').unlink()
        (path / 'z').touch()
        rest = []
        for _ in range(20):
            page = read_dir_page(fd)
            if not page:
                break
            rest.extend(page)
        else:
            raise AssertionError('directory did not reach EOF')
        names = [e['name'] for e in first + rest]
        assert all(names.count(n) == 1 for n in ('b', 'c', 'd')), names
        assert len(names) == len(set(names)), names
        os.lseek(fd, 0, os.SEEK_SET)
        replay = []
        while True:
            page = read_dir_page(fd)
            if not page:
                break
            replay.extend(page)
        assert [e['name'] for e in replay] == names
        return dict(page1=first, rest=rest, stable_seek_replay=True)
    finally:
        os.close(fd)


def directory_identity_case(f):
    d = f.back / 'dir-binding'
    d.mkdir()
    (d / 'child').write_bytes(b'original-dir')
    fd = os.open(f.view / d.name, os.O_RDONLY | os.O_DIRECTORY)
    try:
        d.rename(f.back / 'renamed-dir')
        d.mkdir()
        (d / 'child').write_bytes(b'replacement-dir')
        child = os.open('child', os.O_RDONLY, dir_fd=fd)
        try:
            assert os.read(child, 999) == b'original-dir'
        finally:
            os.close(child)
        assert (f.view / 'dir-binding/child').read_bytes() == b'replacement-dir'
        return dict(held_directory_identity=True, new_path_new_directory=True)
    finally:
        os.close(fd)


def readonly_case(f):
    p = f.view / 'write-denied'
    try:
        p.write_bytes(b'forbidden')
    except OSError as exc:
        assert exc.errno == errno.EROFS, exc
    else:
        raise AssertionError('write succeeded')
    assert not (f.back / 'write-denied').exists()
    return dict(erofs=True)


def permissions_case(f):
    p = f.back / 'permissions'
    p.write_bytes(b'held capability')
    fd = os.open(f.view / p.name, os.O_RDONLY)
    try:
        p.chmod(0)
        mode = os.stat(f.view / p.name).st_mode & 0o777
        assert mode == 0, oct(mode)
        try:
            unexpected = os.open(f.view / p.name, os.O_RDONLY)
        except OSError as exc:
            assert exc.errno == errno.EACCES, exc
        else:
            os.close(unexpected)
            raise AssertionError('unreadable backing file opened successfully')
        assert os.pread(fd, 999, 0) == b'held capability'
        return dict(live_mode=mode, open_eacces=True, held_fd_preserved=True)
    finally:
        p.chmod(0o600)
        os.close(fd)


def unavailable_provider_case(f):
    fd = os.open(f.view / 'mask/mounted.txt', os.O_RDONLY)
    directory = os.open(f.view / 'mask', os.O_RDONLY | os.O_DIRECTORY)
    old = f.root / 'provider-original'
    try:
        f.provider.rename(old)
        f.provider.mkdir()
        (f.provider / 'mounted.txt').write_bytes(b'REPLACED-PROVIDER')
        for call in (lambda: (f.view / 'mask/mounted.txt').read_bytes(),
                     lambda: list(os.scandir(f.view))):
            try:
                call()
            except OSError as exc:
                assert exc.errno == errno.EIO, exc
            else:
                raise AssertionError('invalid provider was accepted')
        assert os.pread(fd, 999, 0) == b'MOUNTED'
        names = [entry['name'] for entry in read_dir_page(directory, 4096)]
        assert 'mounted.txt' in names, names
        return dict(new_lookups_and_directory_fail_closed=True, held_objects_preserved=True)
    finally:
        os.close(fd)
        os.close(directory)
        if old.exists():
            (f.provider / 'mounted.txt').unlink()
            f.provider.rmdir()
            old.rename(f.provider)


def resource_case(f):
    p = f.back / 'pressure-file'
    p.write_bytes(b'pressure-object')
    fd_dir = Path('/proc') / str(f.process.pid) / 'fd'
    baseline = len(list(fd_dir.iterdir()))
    before = f.status()
    for _ in range(200):
        with open(f.view / p.name, 'rb') as stream:
            assert stream.read() == b'pressure-object'
    for i in range(1200):
        q = f.back / ('churn-%d' % i)
        q.touch()
        os.stat(f.view / q.name)
        q.unlink()
        assert not (f.view / q.name).exists()
    time.sleep(.15)  # drain asynchronous RELEASE/FORGET, not a cache correctness delay
    after = f.status()
    after_fds = len(list(fd_dir.iterdir()))
    assert after_fds <= baseline + 8, (baseline, after_fds, after)
    # Real worker descriptor exhaustion, then recovery while live handles remain.
    limits = resource.prlimit(f.process.pid, resource.RLIMIT_NOFILE)
    held = []
    exhausted = None
    try:
        resource.prlimit(f.process.pid, resource.RLIMIT_NOFILE,
                         (after_fds + 48, limits[1]))
        for _ in range(128):
            try:
                held.append(os.open(f.view / p.name, os.O_RDONLY))
            except OSError as exc:
                exhausted = exc.errno
                break
        assert exhausted in (errno.EMFILE, errno.ENFILE), (exhausted, len(held))
        assert os.pread(held[0], 999, 0) == b'pressure-object'
    finally:
        resource.prlimit(f.process.pid, resource.RLIMIT_NOFILE, limits)
        for fd in held:
            os.close(fd)
    assert (f.view / p.name).read_bytes() == b'pressure-object'
    return dict(before=before, after=after, baseline_fds=baseline, after_fds=after_fds,
                exhaustion_errno=exhausted, held_count=len(held), recovered=True)


def provider_resource_case(f):
    class ProviderFixture:
        back = f.provider
        view = f.view / 'mask'
        process = f.process

        def status(self):
            return f.status()

    return resource_case(ProviderFixture())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--source', type=Path, default=ROOT / 'src')
    p.add_argument('--out', type=Path, required=True)
    args = p.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    result = dict(source=str(args.source.resolve()), scope='real kernel FUSE, isolated local backends',
                  cases=[], started_at=time.time())
    fixture = None
    try:
        fixture = Fixture(args.source.resolve(), args.out)
        result['initial_status'] = fixture.initial
        cases = [(f'replace-{a}-{b}-primed-{c}', lambda f, a=a,b=b,c=c: replace_case(f,a,b,c))
                 for a,b in ((32,128),(128,32),(32,32)) for c in (False,True)]
        cases += [('unlink', unlink_case), ('hardlinks', hardlinks_case), ('relative-symlink-alias',symlink_alias_case),
                  ('inplace', inplace_case), ('mmap', mmap_case), ('mmap-unprimed', lambda f:mmap_case(f,False)),
                  ('concurrent-replace', concurrent_case), ('topology', topology_case), ('dirent-identity', dirent_identity_case),
                  ('directory-pagination', directory_case), ('directory-identity', directory_identity_case),
                  ('readonly', readonly_case), ('permissions', permissions_case), ('resources', resource_case),
                  ('provider-resources', provider_resource_case),
                  ('unavailable-provider', unavailable_provider_case)]
        for name, call in cases:
            try:
                data = call(fixture)
                record = dict(name=name, passed=True, data=data)
            except Exception as exc:
                record = dict(name=name, passed=False, error=repr(exc), traceback=traceback.format_exc())
            result['cases'].append(record)
            (args.out / 'progress.json').write_text(json.dumps(result, indent=2))
            print(json.dumps({k:record[k] for k in ('name','passed')}, ensure_ascii=False), flush=True)
        result['final_status'] = fixture.status()
    except Exception:
        result['fatal'] = traceback.format_exc()
    finally:
        if fixture:
            try:
                fixture.close()
                result['cleanup'] = fixture.cleanup
            except Exception:
                result['cleanup_error'] = traceback.format_exc()
        result['finished_at'] = time.time()
        result['passed'] = (not result.get('fatal') and not result.get('cleanup_error') and
                            bool(result['cases']) and all(c['passed'] for c in result['cases']))
        (args.out / 'result.json').write_text(json.dumps(result, indent=2))
    print(json.dumps(dict(passed=result['passed'], out=str(args.out)), ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
