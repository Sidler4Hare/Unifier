"""Confirm a host-built glibc 2.34 binary runs in the fixed Debian container."""
import os
from pathlib import Path
import sys
ROOT = Path("/home/omg/文档/多兼容linux机")
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from tools.reuse_test_guard import make_service, get
spec = Path(os.environ["UNIFIER_TEST_SPEC"])
config = get(spec)
service = make_service(spec)
service.start(config["env_id"])
try:
    copied = service.runtime.runner(
        ["podman", "cp", "/tmp/host-built-ok", config["cid"] + ":/tmp/host-built-ok"],
        capture=True, timeout=30)
    if copied.returncode:
        raise SystemExit(copied.stderr[-500:])
    value = service.runtime.exec(
        config["cid"], ["/tmp/host-built-ok"], project=config["project"], env_id=config["env_id"],
        workdir="/tmp", env={"LC_ALL": "C"}, capture=True, timeout=20, user="1000")
    print(value.returncode)
    print(value.stdout)
    print(value.stderr)
finally:
    service.stop(config["env_id"])
