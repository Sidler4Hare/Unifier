#!/usr/bin/env python3
"""Bounded paired timings after all other test processes have finished."""
import json
import argparse
import platform
import subprocess
from pathlib import Path
import time

from extra_probe import Fixture, benchmark

HERE=Path(__file__).resolve().parent


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--source',type=Path,default=HERE/'snapshot/src')
    parser.add_argument('--out',type=Path,default=HERE/'performance-isolated')
    args=parser.parse_args()
    out=args.out
    out.mkdir(exist_ok=False)
    result={'started_at':time.time(),'other_retest_workloads_finished':True,
            'source':str(args.source.resolve()),
            'uname':platform.uname()._asdict(),
            'podman':subprocess.check_output(['podman','--version'],text=True).strip(),
            'scope':'bounded local warm/uncontrolled-cache native vs persistent-provider FUSE; no benchmark SLO'}
    fixture=None
    try:
        fixture=Fixture(args.source.resolve(),out)
        result['initial_status']=fixture.initial
        result['measurement']=benchmark(fixture)
        result['final_status']=fixture.status()
    finally:
        if fixture:
            fixture.close()
            result['cleanup']=fixture.cleanup
        result['finished_at']=time.time()
        (out/'result.json').write_text(json.dumps(result,indent=2))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
