"""Read-only audit after the timed-out toolchain install."""
import json, os
from pathlib import Path
import sys
ROOT = Path("/home/omg/文档/多兼容linux机")
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from tools.reuse_test_guard import make_service, get

spec = Path(os.environ["UNIFIER_TEST_SPEC"])
config = get(spec)
out = spec.parent
service = make_service(spec)
service.start(config["env_id"])
try:
    audit = service.runtime.exec(
        config["cid"], ["dpkg", "--audit"], project=config["project"], env_id=config["env_id"],
        workdir="/", env={"LC_ALL": "C"}, capture=True, timeout=60, user="0")
    query = service.runtime.exec(
        config["cid"], ["dpkg-query", "-W", "-f", "${Package}\t${db:Status-Abbrev}\t${Version}\n",
                        "gcc", "binutils", "dpkg-dev", "apt-file", "libc6-dev"],
        project=config["project"], env_id=config["env_id"], workdir="/",
        env={"LC_ALL": "C"}, capture=True, timeout=30, user="0")
    result = {"audit_exit": audit.returncode, "audit": audit.stdout, "audit_err": audit.stderr[-1000:],
              "query_exit": query.returncode, "query": query.stdout, "query_err": query.stderr[-1000:]}
finally:
    service.stop(config["env_id"])
(out / "result.json").write_text(json.dumps(result, indent=2))
print(json.dumps(result))
