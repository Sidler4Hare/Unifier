#!/usr/bin/env python3
"""Reuse previous 26-case broker matrix and add low-limit large readdir."""
from pathlib import Path
import resource
import sys
ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'evidence/projection-boundaries-2026-10-27'),str(Path(__file__).parent)]
import real_broker_boundaries as broker
from real_fd_probe import large_directories

def limited_large(f):
    initial=f.status();pid=initial['worker_pid']
    old=resource.prlimit(pid,resource.RLIMIT_NOFILE)
    resource.prlimit(pid,resource.RLIMIT_NOFILE,(256,old[1]))
    try:
        assert f.status()['counters']['fd_soft_limit']==256
        return dict(worker_soft_limit=256, directories=large_directories(f))
    finally:resource.prlimit(pid,resource.RLIMIT_NOFILE,old)

broker.CASES=[*broker.CASES,('low-nofile-large-directories',limited_large)]
if __name__=='__main__':raise SystemExit(broker.main())
