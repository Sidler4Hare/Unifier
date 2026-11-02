"""Backport only the admission repair to stopped daily application deployments.

Requires the pre-repair backup recorded in /tmp/unifier-host-authority-fix-backup-path.
Prepares and validates complete temporary code copies before changing live files.
Does not change config, container records, launchers, app data or container state.
"""
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
BACKUP = Path(Path("/tmp/unifier-host-authority-fix-backup-path").read_text())
BEFORE = json.loads((BACKUP / "files.json").read_text())
PREVIOUS = json.loads((OUT / "deployment.json").read_text()) if (OUT / "deployment.json").exists() else None


def digest(data):
    return hashlib.sha256(data).hexdigest()


def methods(source):
    klass = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.ClassDef) and node.name == "EnvironmentService")
    return {node.name: node for node in klass.body if isinstance(node, ast.FunctionDef)}


def method_source(source, name):
    node = methods(source)[name]
    start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
    return "".join(source.splitlines(keepends=True)[start:node.end_lineno])


def legacy_names(source):
    return source.replace("from unifier.", "from multilinux.").replace("UnifierError", "MultilinuxError")


def atomic_write(path, data):
    fd, name = tempfile.mkstemp(prefix=".host-authority-", dir=path.parent)
    try:
        os.fchmod(fd, path.stat().st_mode & 0o777)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


current = (ROOT / "src/unifier/service.py").read_text()
replacements = {}


def expected_hash(app, deployment, relative):
    if PREVIOUS and relative in PREVIOUS["deployments"][app]["sha256"]:
        return PREVIOUS["deployments"][app]["sha256"][relative]
    return BEFORE[str(deployment / relative)]["sha256"]


for app in ("freecad", "wemeet"):
    deployment = Path.home() / ".local/share/multilinux/apps" / app
    source = (deployment / "src/multilinux/service.py").read_text()
    tree = methods(source)
    create = "\n".join(method_source(current, name).rstrip() for name in
                        ("create", "_create_locked", "_require_unique_target", "_creation_lock")) + "\n"
    lines = source.splitlines(keepends=True)
    updates = [("create", create), ("_exec_record", method_source(current, "_exec_record"))]
    if "_create_locked" in tree:
        updates = [(name, method_source(current, name)) for name in
                   ("create", "_create_locked", "_require_unique_target", "_creation_lock", "_exec_record")]
    for name, replacement in sorted(
        updates,
        key=lambda item: tree[item[0]].lineno, reverse=True,
    ):
        node = tree[name]
        start = min([node.lineno] + [d.lineno for d in node.decorator_list]) - 1
        lines[start:node.end_lineno] = [legacy_names(replacement)]
    changed = {
        "src/multilinux/service.py": "".join(lines),
        "src/multilinux/compatibility.py": legacy_names((ROOT / "src/unifier/compatibility.py").read_text()),
        "tools/lab.py": legacy_names((ROOT / "tools/lab.py").read_text()),
    }
    cli_path = deployment / "src/multilinux/cli.py"
    cli_source = cli_path.read_text()
    old = 'return 0 if all(plan["allowed"] for plan in plans) else 1'
    assert cli_source.count(old) == 1 or (PREVIOUS and old.replace("all(", "any(") in cli_source)
    changed["src/multilinux/cli.py"] = cli_source.replace(old, old.replace("all(", "any("))
    probe = deployment / "tools/probe_headless.py"
    if probe.exists():
        changed["tools/probe_headless.py"] = legacy_names((ROOT / "tools/probe_headless.py").read_text())
    for relative, text in changed.items():
        ast.parse(text, filename=str(deployment / relative))
        assert digest((deployment / relative).read_bytes()) == expected_hash(app, deployment, relative)
    with tempfile.TemporaryDirectory(prefix=f"unifier-deploy-{app}-") as temporary:
        stage = Path(temporary)
        for name in ("src", "tools", "config"):
            shutil.copytree(deployment / name, stage / name, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        for relative, text in changed.items():
            (stage / relative).write_text(text)
        completed = subprocess.run([sys.executable, str(OUT / "deployment_probe.py"), str(stage)],
                                   text=True, capture_output=True)
        (OUT / f"{app}-predeploy.log").write_text(completed.stdout + completed.stderr)
        completed.check_returncode()
    replacements[app] = (deployment, changed)

# All candidate deployments passed before the first live mutation.
receipt = {"deployed_at": datetime.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
           "backup": str(BACKUP), "policy_version": 2, "deployments": {}}
if PREVIOUS and not (OUT / "deployment-initial.json").exists():
    shutil.copy2(OUT / "deployment.json", OUT / "deployment-initial.json")
for app, (deployment, changed) in replacements.items():
    hashes = {}
    for relative, text in changed.items():
        path = deployment / relative
        assert digest(path.read_bytes()) == expected_hash(app, deployment, relative)
        atomic_write(path, text.encode())
        hashes[relative] = digest(path.read_bytes())
    metadata = deployment / "deployment-current.json"
    state = json.loads(metadata.read_text())
    if PREVIOUS:
        assert state["host_authority_repair"]["deployed_at"] == PREVIOUS["deployed_at"]
    else:
        assert digest(metadata.read_bytes()) == BEFORE[str(metadata)]["sha256"]
    state.setdefault("source_sha256", {}).update(hashes)
    state["host_authority_repair"] = {
        "policy_version": 2, "source": str(ROOT), "source_sha256": hashes,
        "backup": str(BACKUP), "deployed_at": receipt["deployed_at"],
        "evidence": str(OUT), "scope": "admission and unique-target backport; original deployment retained",
    }
    atomic_write(metadata, (json.dumps(state, ensure_ascii=False, indent=2) + "\n").encode())
    receipt["deployments"][app] = {"root": str(deployment), "sha256": hashes,
                                    "metadata_sha256": digest(metadata.read_bytes())}
(OUT / "deployment.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + "\n")
print(json.dumps(receipt, ensure_ascii=False, indent=2))
