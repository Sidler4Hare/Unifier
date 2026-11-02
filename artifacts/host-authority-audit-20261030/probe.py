"""Read-only policy audit plus isolated fake-runtime counterexamples.

Run from the repository root with:
  PYTHONDONTWRITEBYTECODE=1 python3 artifacts/host-authority-audit-20261030/probe.py
No real container is created, started, executed in, stopped or removed.
"""
import datetime
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import types
from unittest.mock import patch
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]

from unifier import compatibility, service as service_module
from unifier.runtime import ExecResult
from unifier.errors import UnifierError
from test_compatibility import host
from test_env import service_in
from test_cli_shortcuts import ready_context, run_ctx


def evaluate_policy(label, source):
    module = types.ModuleType("audit_policy")
    exec(compile(source, label, "exec"), module.__dict__)
    pairs = [("ubuntu", "debian13"), ("debian", "debian13"),
             ("fedora", "fedora44"), ("arch", "arch"), ("manjaro", "arch")]
    return module, {
        "label": label,
        "sha256": hashlib.sha256(source.encode()).hexdigest(),
        "policy_version": module.POLICY_VERSION,
        "same_family_pairs": [
            {"host": distro, "target": target,
             "allowed": module.relationship_plan(host(distro), target)["allowed"]}
            for distro, target in pairs
        ],
        "scope": "policy function evaluated locally; not deployed runtime acceptance",
    }


result = {"captured_at": datetime.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
          "scope": "read-only inventory; isolated fake-runtime counterexamples",
          "policies": []}
result["source_sha256"] = {
    str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
    for path in [ROOT / f"src/unifier/{name}.py" for name in
                 ("compatibility", "service", "shortcuts", "runtime", "cli")]
}
policy_path = ROOT / "src/unifier/compatibility.py"
_, current = evaluate_policy(str(policy_path), policy_path.read_text())
result["policies"].append(current)
staged_source = subprocess.check_output(
    ["git", "show", ":src/unifier/compatibility.py"], cwd=ROOT, text=True)
staged, index_result = evaluate_policy("git-index:src/unifier/compatibility.py", staged_source)
result["policies"].append(index_result)
for app in ("freecad", "wemeet"):
    path = Path.home() / f".local/share/multilinux/apps/{app}/src/multilinux/compatibility.py"
    if path.is_file():
        _, item = evaluate_policy(str(path), path.read_text())
        result["policies"].append(item)
for archive in [ROOT / "artifacts/unifier-ubuntu-server-20261028.tar.gz",
                ROOT / "artifacts/server-validation-20261030T204043+0800/unifier-current.tar.gz"]:
    if not archive.is_file():
        continue
    with tarfile.open(archive) as bundle:
        for member in bundle.getmembers():
            if member.isfile() and member.name.endswith("src/unifier/compatibility.py"):
                source = bundle.extractfile(member).read().decode()
                _, item = evaluate_policy(f"{archive}:{member.name}", source)
                result["policies"].append(item)
                break
            if member.isfile() and member.name.endswith("src/multilinux/compatibility.py"):
                source = bundle.extractfile(member).read().decode()
                if "def relationship_plan" in source:
                    _, item = evaluate_policy(f"{archive}:{member.name}", source)
                    result["policies"].append(item)
                    break

facts = compatibility.observe_host()
result["live_host"] = compatibility.classify_host(facts)
result["live_matrix"] = [
    {"target": target, "allowed": (p := compatibility.relationship_plan(facts, target))["allowed"],
     "relationship": p["relationship"], "policy_version": p["policy_version"]}
    for target in ("debian13", "fedora44", "arch")
]
result["container_inventory"] = subprocess.check_output(
    ["podman", "ps", "-a", "--format", "{{.ID}}\t{{.Names}}\t{{.Status}}"], text=True).splitlines()
matrix = subprocess.run([str(ROOT / "bin/unifier"), "compat", "matrix"],
                        text=True, capture_output=True)
result["live_matrix_exit_code"] = matrix.returncode

# The same real service implementation reaches create with policy v1, and
# rejects before any fake runtime event with policy v2.
result["same_family_create_replay"] = []
for policy in (staged, compatibility):
    for distro, target in (("ubuntu", "debian13"), ("fedora", "fedora44"), ("arch", "arch")):
        with tempfile.TemporaryDirectory(prefix="unifier-authority-") as temporary:
            service, runtime = service_in(Path(temporary))
            service.host_facts = host(distro)
            error = None
            with patch.object(service_module, "relationship_plan", policy.relationship_plan):
                try:
                    service.create(target, None, [])
                except UnifierError as exc:
                    error = str(exc)
            result["same_family_create_replay"].append({
                "host": distro, "target": target, "policy_version": policy.POLICY_VERSION,
                "accepted": error is None, "error": error,
                "runtime_create_events": sum(e[0] == "create" for e in runtime.events),
                "runtime_event_count": len(runtime.events),
            })

# Legacy same-family environment already running after a host/policy change:
# ordinary run refuses, while the full shortcut ls command reaches guest sh.
with tempfile.TemporaryDirectory(prefix="unifier-authority-ls-") as temporary:
    ctx, runtime = ready_context(Path(temporary))
    service = ctx.service_for("debian13")
    service.start("debian13")
    service.host_facts = host("ubuntu")
    ctx.service_kwargs = {**ctx.service_kwargs, "host_facts": host("ubuntu")}
    reached = []
    def handle_exec(name, argv, env_id):
        reached.append(argv)
        return ExecResult(0, "/\x00usr\x00var\x00", "")
    runtime.exec_handler = handle_exec
    try:
        service.run("debian13", ["true"], workdir=None, env_keys=[])
        run_refusal = None
    except UnifierError as exc:
        run_refusal = str(exc)
    code, stdout, stderr = run_ctx(ctx, ["ls", "debian13", "/", "--json"])
    result["running_ls_bypass"] = {
        "relationship_allowed": service.compatibility_plan("debian13")["allowed"],
        "ordinary_run_error": run_refusal, "ls_exit_code": code,
        "ls_stdout": stdout, "ls_stderr": stderr,
        "guest_exec_count": len(reached),
        "guest_executables": [argv[:2] for argv in reached],
        "scope": "full shortcut CLI and real service with injected host facts and fake runtime",
    }
    assert run_refusal and code == 0 and reached[0][:2] == ["/bin/sh", "-c"]

# The diagnostic API enforces env-id uniqueness, not target uniqueness.
with tempfile.TemporaryDirectory(prefix="unifier-authority-duplicates-") as temporary:
    service, runtime = service_in(Path(temporary))
    for env_id in ("debian-one", "debian-two"):
        runtime.identities[env_id] = runtime.identities["debian13"]
        service.create("debian13", env_id, [])
    result["diagnostic_duplicate_target"] = {
        "target": "debian13", "environment_count": len(runtime.containers),
        "scope": "cross-family host and fake runtime; shortcut uniqueness is separate",
    }

result["server_receipts"] = []
receipt_root = ROOT / "artifacts/server-validation-20261030T204043+0800"
for path in sorted(receipt_root.glob("*/live-*/headless.json")):
    relative = str(path.relative_to(ROOT))
    receipt = json.loads(path.read_text())
    for target, item in receipt.get("targets", {}).items():
        plan = item.get("compatibility", {})
        result["server_receipts"].append({
            "path": relative, "captured_at": receipt.get("captured_at"),
            "passed": receipt.get("passed"), "target": target,
            "container_id": item.get("container_id"),
            "host": plan.get("host", {}).get("id"),
            "policy_version": plan.get("policy_version"), "allowed": plan.get("allowed"),
            "relationship": plan.get("relationship"),
            "scope": "existing remote receipt read locally; remote host not rechecked",
        })

print(json.dumps(result, ensure_ascii=False, indent=2))
