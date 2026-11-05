"""Run the real app CLI with the test runtime in this worker as well."""
import os,sys,json
from pathlib import Path
REPO=Path('/home/omg/文档/多兼容linux机');sys.path[:0]=[str(REPO/'src'),str(REPO)]
from unifier import cli
from unifier.project import load_project
from unifier.service import EnvironmentService
from unifier.on_demand import WORKER_MARKER
from tools.guarded_test import GuardedPodmanRuntime
CTL=Path(__file__).resolve().parent;root=REPO/'.lab/guarded-expansion-20261103/environment';config,_,_=load_project();lock=json.loads((CTL/'guarded-images.lock.json').read_text())
service=EnvironmentService(root,GuardedPodmanRuntime(root),config,lock)
cli.service_factory=lambda: service
# This entry is itself the controlled worker: no detached normal-runtime worker.
os.environ[WORKER_MARKER]='1'
raise SystemExit(cli.main())
