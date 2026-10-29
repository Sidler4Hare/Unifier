#!/usr/bin/env python3
"""Isolated real-kernel low-NOFILE acceptance; only test workers are limited."""
import argparse
import errno
import json
import mmap
import os
from pathlib import Path
import resource
import subprocess
import sys
import time
import traceback
from unittest.mock import patch
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'evidence/projection-objects-2026-10-26'))
import real_fuse_probe as probe


def large_directories(f):
    rows = []
    for count in (300, 3000, 10000):
        directory = f.back / f'large-{count}'
        directory.mkdir()
        for index in range(count):
            (directory / f'f{index:05d}').touch()
        before = f.status()['counters']
        fd = os.open(f.view / directory.name, os.O_RDONLY | os.O_DIRECTORY)
        second = None
        try:
            names = os.listdir(fd)
            assert len(names) == count and len(set(names)) == count
            during = f.status()['counters']
            assert during['anchor_fds'] - before['anchor_fds'] <= 1
            assert during['process_fds'] - before['process_fds'] <= 3
            assert during['snapshot_identity_refs'] >= count
            assert during['snapshot_anchor_fds'] == 0
            second = os.open(f.view / directory.name, os.O_RDONLY | os.O_DIRECTORY)
            assert set(os.listdir(second)) == set(names)
            # Public plain ls is a distinct pure-readdir caller.
            command = subprocess.run(['ls', '-U', '--color=never', str(f.view/directory.name)],
                                     capture_output=True, timeout=30)
            assert command.returncode == 0, command.stderr.decode()
            assert len(command.stdout.splitlines()) == count
            rows.append(dict(files=count, fd_delta=during['process_fds']-before['process_fds'],
                             nodes=during['nodes'], held_directory_status=during))
        finally:
            if second is not None: os.close(second)
            os.close(fd)
    deadline = time.monotonic()+5
    while f.status()['counters']['dir_handles']:
        assert time.monotonic() < deadline
        time.sleep(.01)
    assert f.status()['counters']['snapshot_identity_refs'] == 0
    return rows


def lookup_pressure(f):
    for index in range(350): (f.back/f'p{index:04d}').write_bytes(b'old')
    fd = os.open(f.view/'p0000', os.O_RDONLY)
    errors = []
    try:
        for index in range(1,350):
            try: os.stat(f.view/f'p{index:04d}')
            except OSError as exc:
                assert exc.errno == errno.EMFILE, exc
                errors.append(index)
        assert errors
        started = time.monotonic()
        status = f.status()
        latency = time.monotonic()-started
        counts = status['counters']
        assert counts['fd_budget_rejections'] > 0
        assert counts['process_fds'] <= counts['fd_soft_limit']-16
        assert latency < 3
        replacement=f.back/'new';replacement.write_bytes(b'new');replacement.replace(f.back/'p0000')
        assert os.pread(fd,3,0) == b'old'
        listed = subprocess.run(['ls','-l','--color=never',str(f.view)], capture_output=True, timeout=30)
        assert listed.returncode != 0 and b'Too many open files' in listed.stderr
        return dict(control_latency_seconds=latency, controlled_errors=len(errors),
                    ls_long_exit=listed.returncode, old_handle_preserved=True, status=status)
    finally: os.close(fd)


def open_pressure(f):
    for index in range(350): (f.back/f'o{index:04d}').write_bytes(b'old')
    fds=[]; mapping=None
    try:
        for index in range(350):
            try: fds.append(os.open(f.view/f'o{index:04d}',os.O_RDONLY))
            except OSError as exc:
                assert exc.errno==errno.EMFILE,exc
                break
        assert 0<len(fds)<350
        status=f.status();counts=status['counters']
        assert counts['fd_budget_rejections']>0
        assert counts['process_fds']<=counts['fd_soft_limit']-16
        mapping=mmap.mmap(fds[0],0,access=mmap.ACCESS_READ)
        (f.back/'o0000').unlink()
        assert mapping[:]==os.pread(fds[0],3,0)==b'old'
        for fd in fds[1:]:os.close(fd)
        fds=fds[:1]
        deadline=time.monotonic()+5
        while f.status()['counters']['file_handles']>1:
            assert time.monotonic()<deadline
            time.sleep(.01)
        assert (f.view/'o0349').read_bytes()==b'old'
        return dict(status_at_rejection=status, release_recovers_admission=True,
                    unlink_old_fd_and_mmap_preserved=True, after=f.status())
    finally:
        if mapping is not None:mapping.close()
        for fd in fds:os.close(fd)


def main():
    p=argparse.ArgumentParser();p.add_argument('--out',type=Path,required=True);a=p.parse_args()
    a.out.mkdir(parents=True,exist_ok=False)
    r={'cases':[], 'scope':'real FUSE worker soft NOFILE=256; host unchanged'}
    original_popen=subprocess.Popen
    def limited(*args,**kwargs):
        def limit():
            _soft,hard=resource.getrlimit(resource.RLIMIT_NOFILE)
            resource.setrlimit(resource.RLIMIT_NOFILE,(256,hard))
        kwargs['preexec_fn']=limit
        return original_popen(*args,**kwargs)
    for name,call in [('large-directories',large_directories),('lookup-pressure',lookup_pressure),('open-pressure',open_pressure)]:
        out=a.out/name;out.mkdir();f=None;case={'name':name,'passed':False}
        try:
            with patch.object(probe.subprocess,'Popen',side_effect=limited):
                f=probe.Fixture(ROOT/'src',out)
            assert f.initial['counters']['fd_soft_limit']==256
            case['initial']=f.initial;case['data']=call(f);case['final']=f.status();case['passed']=True
        except Exception:case['error']=traceback.format_exc()
        finally:
            if f:
                try:f.close();case['cleanup']=f.cleanup
                except Exception:case['cleanup_error']=traceback.format_exc();case['passed']=False
        r['cases'].append(case)
        (a.out/'result.json').write_text(json.dumps(r,indent=2))
        print(json.dumps({'case':name,'passed':case['passed']}),flush=True)
    r['passed']=all(c['passed'] for c in r['cases'])
    (a.out/'result.json').write_text(json.dumps(r,indent=2))
    return 0 if r['passed'] else 1
if __name__=='__main__':raise SystemExit(main())
