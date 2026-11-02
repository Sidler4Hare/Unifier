#!/usr/bin/env python3
"""Independent boundary and bounded performance retest; no product edits."""
import argparse
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import select
import statistics
import struct
import sys
import time
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'evidence/projection-objects-2026-10-26'))
from real_fuse_probe import Fixture


class Inotify:
    def __init__(self, path):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), 'inotify_init1')
        mask = 0x2 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200
        if self.libc.inotify_add_watch(self.fd, os.fsencode(path), mask) < 0:
            code = ctypes.get_errno()
            os.close(self.fd)
            raise OSError(code, 'inotify_add_watch')

    def read(self):
        events = []
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                return events
            cursor = 0
            while cursor < len(data):
                wd, mask, cookie, length = struct.unpack_from('iIII', data, cursor)
                name = data[cursor+16:cursor+16+length].split(b'\0', 1)[0].decode()
                events.append(dict(mask=mask, cookie=cookie, name=name))
                cursor += 16+length

    def close(self):
        os.close(self.fd)


def watcher(f):
    directory = f.back/'watch'
    directory.mkdir()
    path = directory/'watched.txt'
    path.write_bytes(b'old')
    host = Inotify(directory)
    projected = Inotify(f.view/'watch')
    try:
        path.write_bytes(b'inplace')
        temporary = directory/'replacement.tmp'
        temporary.write_bytes(b'replaced')
        os.replace(temporary, path)
        host_events, projected_events = [], []
        until = time.monotonic()+1
        while time.monotonic() < until:
            select.select([host.fd, projected.fd], [], [], .05)
            host_events.extend(host.read())
            projected_events.extend(projected.read())
        assert host_events, 'native control received no events'
        return dict(native_events=host_events, projected_events=projected_events,
                    projected_change_events_delivered=bool(projected_events),
                    fresh_open_bytes=(f.view/'watch/watched.txt').read_bytes().decode(),
                    declared_capability=f.status().get('consistency', {}).get('watcher'))
    finally:
        projected.close()
        host.close()


def preserved_mtime(f):
    path = f.back/'preserved-mtime'
    path.write_bytes(b'O'*4096)
    native = os.open(path, os.O_RDONLY)
    projected = os.open(f.view/path.name, os.O_RDONLY)
    try:
        assert os.pread(projected,8192,0)==b'O'*4096
        before = path.stat()
        with path.open('r+b') as stream:
            stream.write(b'N'*4096)
            stream.flush()
            os.fsync(stream.fileno())
        os.utime(path, ns=(before.st_atime_ns,before.st_mtime_ns))
        native_stat, projected_stat = os.fstat(native), os.fstat(projected)
        native_read, projected_read = os.pread(native,8192,0), os.pread(projected,8192,0)
        reopened = (f.view/path.name).read_bytes()
        assert native_read==b'N'*4096
        return dict(native_sha256=hashlib.sha256(native_read).hexdigest(),
                    held_projected_sha256=hashlib.sha256(projected_read).hexdigest(),
                    held_fd_fresh_after_fstat=projected_read==native_read,
                    reopened_fresh=reopened==native_read,
                    size=projected_stat.st_size,
                    preserved_mtime_ns=native_stat.st_mtime_ns==before.st_mtime_ns,
                    ctime_changed=native_stat.st_ctime_ns!=before.st_ctime_ns)
    finally:
        os.close(native)
        os.close(projected)


def symlink_dotdot(f):
    path = f.back/'link-context'
    (path/'a').mkdir(parents=True)
    (path/'b').mkdir()
    (path/'target').write_bytes(b'ROOT-CORRECT')
    (path/'a/target').write_bytes(b'A-WRONG')
    os.symlink('../b',path/'a/jump')
    os.symlink('jump/../target',path/'a/link')
    native = (path/'a/link').read_bytes()
    projected = (f.view/'link-context/a/link').read_bytes()
    assert native==b'ROOT-CORRECT'
    return dict(native=native.decode(),projected=projected.decode(),
                same_target=projected==native,
                raw_link=os.readlink(path/'a/link'),
                projected_link=os.readlink(f.view/'link-context/a/link'))


def released_dirent(f):
    directory = f.back/'released-inode'
    directory.mkdir()
    for n in ('a','b','c'):
        (directory/n).touch()
    with os.scandir(directory) as it:
        native_entries=list(it)
    native=[dict(name=x.name,listed=x.inode(),stat=os.lstat(x.path).st_ino) for x in native_entries]
    with os.scandir(f.view/directory.name) as it:
        entries=list(it)
    deadline=time.monotonic()+3
    while f.status()['counters']['dir_handles']:
        if time.monotonic()>deadline:
            raise RuntimeError('directory handle did not release')
        time.sleep(.01)
    projected=[dict(name=x.name,listed=x.inode(),stat=os.lstat(x.path).st_ino) for x in entries]
    assert all(x['listed']==x['stat'] for x in native)
    return dict(native=native,projected=projected,
                projected_dirent_matches_later_stat=all(x['listed']==x['stat'] for x in projected))


def benchmark(f):
    native = f.provider/'bench'
    small = native/'small'
    small.mkdir(parents=True)
    payload=b'p'*4096
    for i in range(2000):
        (small/f'f{i:04d}').write_bytes(payload)
    (native/'sequential.bin').write_bytes(b'q'*(16*1024*1024))
    projected=f.view/'mask/bench'

    def workload(path, name):
        if name=='stat_2000':
            return sum(os.stat(path/'small'/f'f{i:04d}').st_size for i in range(2000))
        if name=='read_hash_2000x4KiB':
            digest=hashlib.sha256()
            for i in range(2000):
                digest.update((path/'small'/f'f{i:04d}').read_bytes())
            return digest.hexdigest()
        return hashlib.sha256((path/'sequential.bin').read_bytes()).hexdigest()

    result={}
    for name in ('stat_2000','read_hash_2000x4KiB','read_hash_16MiB'):
        rows=[]
        for wave in range(5):
            row={}
            for label,path in ((('native',native),('fuse',projected)) if wave%2==0 else
                               (('fuse',projected),('native',native))):
                start=time.perf_counter_ns()
                value=workload(path,name)
                row[label]=dict(ms=(time.perf_counter_ns()-start)/1e6,value=value)
            assert row['native']['value']==row['fuse']['value'],row
            rows.append(row)
        summary={}
        for label in ('native','fuse'):
            values=sorted(x[label]['ms'] for x in rows)
            summary[label]=dict(count=len(values),min_ms=min(values),p50_ms=statistics.median(values),
                                max_ms=max(values))
        result[name]=dict(samples=rows,summary=summary,
                          ratio_of_p50=summary['fuse']['p50_ms']/summary['native']['p50_ms'])
        print(json.dumps(dict(benchmark=name,ratio_of_p50=result[name]['ratio_of_p50'])),flush=True)
    return dict(workloads=result,note='5 alternating native/FUSE pairs per workload; no dropped caches, uncontrolled desktop; current fixture only, not directly comparable to Stage F')


def main():
    p=argparse.ArgumentParser()
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--out',type=Path,required=True)
    p.add_argument('--skip-benchmark',action='store_true')
    args=p.parse_args()
    args.out.mkdir(exist_ok=False)
    result=dict(started_at=time.time(),observations={},source=str(args.source.resolve()))
    fixture=None
    try:
        fixture=Fixture(args.source.resolve(),args.out)
        result['initial_status']=fixture.initial
        cases=[('external_watcher',watcher),('preserved_mtime',preserved_mtime),
                          ('symlink_component_then_dotdot',symlink_dotdot),
                          ('dirent_after_releasedir',released_dirent)]
        if not args.skip_benchmark:
            cases.append(('bounded_performance',benchmark))
        for name,call in cases:
            try:
                result['observations'][name]=dict(executed=True,data=call(fixture))
            except Exception:
                result['observations'][name]=dict(executed=False,error=traceback.format_exc())
            print(json.dumps(dict(case=name,executed=result['observations'][name]['executed'])),flush=True)
            (args.out/'progress.json').write_text(json.dumps(result,indent=2))
        result['final_status']=fixture.status()
    finally:
        if fixture:
            fixture.close()
            result['cleanup']=fixture.cleanup
        result['finished_at']=time.time()
        result['execution_completed']=len(result['observations'])==len(cases) and all(v['executed'] for v in result['observations'].values())
        fields={'external_watcher':'projected_change_events_delivered','preserved_mtime':'held_fd_fresh_after_fstat',
                'symlink_component_then_dotdot':'same_target','dirent_after_releasedir':'projected_dirent_matches_later_stat'}
        result['native_equivalence']={name:result['observations'].get(name,{}).get('data',{}).get(field,False)
                                      for name,field in fields.items()}
        result['native_equivalence_passed']=all(result['native_equivalence'].values())
        (args.out/'result.json').write_text(json.dumps(result,indent=2))
    return 0 if result['execution_completed'] and result['native_equivalence_passed'] else 1


if __name__=='__main__':
    raise SystemExit(main())
