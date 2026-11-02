"""Isolated real Podman no-op repair and recorded-CID mismatch refusal."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from unifier.errors import UnifierError
from unifier.project import load_project
from unifier.runtime import PodmanRuntime, container_name
from unifier.service import EnvironmentService

BASE = Path(__file__).resolve().parent
TEST_ROOT = ROOT / ".lab/agents/continued-repair-20261101T154240+0800/identity-root"
if TEST_ROOT.exists():
    raise RuntimeError("refusing to reuse existing test state")
TEST_ROOT.mkdir()
config, lock, _ = load_project()
runtime = PodmanRuntime()
service = EnvironmentService(TEST_ROOT, runtime, config, lock)
result = {"passed": False, "scope": "real Debian no-op dependency repair; simulated stored-CID mismatch; no package installation"}
record = None
try:
    record = service.create("debian13", "identity-check", [], network="pasta")
    cid = record["runtime"]["container_id"]
    calls = []
    execute = runtime.exec
    def traced(name, argv, **kwargs):
        calls.append({"container": name, "argv_head": argv[:2]})
        return execute(name, argv, **kwargs)
    runtime.exec = traced
    txn = service.repair_dependencies("identity-check", [], libraries=["libc.so.6"])
    assert txn["phase"] == "completed", txn
    assert txn["dependency_repair"]["status"] == "no-missing-elf-dependencies", txn
    assert txn["verification"]["ok"], txn
    after = service.store.load("identity-check")
    assert after["package_inventory_sha256"] == record["package_inventory_sha256"]
    pinned = [c for c in calls if c["argv_head"] in (["dpkg", "--audit"], ["dpkg-query", "-W"])]
    assert pinned and all(c["container"] == cid for c in pinned), pinned
    valid = dict(after)
    wrong = {**after, "runtime": {**after["runtime"], "container_id": "0" * 64}}
    service.store.save(wrong, None)
    count = len(calls)
    try:
        service.install_packages("identity-check", ["hello"])
    except UnifierError as exc:
        assert "identity changed" in str(exc), str(exc)
        refusal = str(exc)
    else:
        raise AssertionError("CID mismatch was accepted")
    finally:
        service.store.save(valid, None)
    assert len(calls) == count, "guest execution happened despite mismatched CID"
    result.update(passed=True, container_id=cid, transaction_id=txn["id"],
                  transaction_phase=txn["phase"], dependency_repair=txn["dependency_repair"],
                  verification_ok=txn["verification"]["ok"], inventory_unchanged=True,
                  cid_pinned_audit_and_inventory=pinned, mismatch_refusal=refusal,
                  mismatch_guest_execution_count=len(calls) - count)
finally:
    if record is not None:
        current = runtime.inspect(container_name(TEST_ROOT, "identity-check"))
        if current is not None:
            assert current.container_id == record["runtime"]["container_id"], "refusing cleanup of replacement"
            service.remove("identity-check")
        result["test_container_removed"] = runtime.inspect(container_name(TEST_ROOT, "identity-check")) is None
    (BASE / "real-package-identity.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
print(json.dumps(result, ensure_ascii=False, indent=2))
