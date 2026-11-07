"""Real CLI and real detached APP workers, with the same fixed-CID test runtime."""
import os
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'src'),str(ROOT)]
from unifier import cli,on_demand
from tools.reuse_test_guard import make_service

service=make_service(Path(os.environ['UNIFIER_TEST_SPEC']))
cli.service_factory=lambda:service
original=on_demand.worker_command
def guarded_worker(*args,**kwargs):
    command=original(*args,**kwargs)
    command[1]=str(Path(__file__).resolve())
    return command
on_demand.worker_command=guarded_worker
raise SystemExit(cli.main())
