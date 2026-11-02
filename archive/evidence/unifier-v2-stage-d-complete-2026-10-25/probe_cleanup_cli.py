"""Real public CLI controlled move, status and repeat recovery on private data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/multilinux").is_dir())


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--workdir", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    assert not a.workdir.exists() and not a.output.exists()
    a.workdir.mkdir(mode=0o700)
    project = a.workdir / "project"
    project.mkdir()
    for name in ["src", "config"]:
        shutil.copytree(ROOT / name, project / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (project / "tools").mkdir()
    for name in ["ml.py", "unifier.py", "lab.py"]:
        shutil.copy2(ROOT / "tools" / name, project / "tools" / name)
    src = a.workdir / "source"
    src.mkdir(mode=0o700)
    name, target_name = "待移动 中文';%.bin", "已移动 中文';%.bin"
    payload = os.urandom(96 * 1024)
    (src / name).write_bytes(payload)
    report = {"passed": False, "source_sha256": {
        str(f.relative_to(ROOT)): hashlib.sha256(f.read_bytes()).hexdigest()
        for f in (ROOT / "src/multilinux").glob("*.py")}, "commands": [], "project": str(project)}
    def cli(*argv, code=0):
        result = subprocess.run([sys.executable, str(project / "tools/unifier.py"), *argv],
                                text=True, capture_output=True, timeout=15)
        row = {"args": list(argv), "exit": result.returncode, "stderr": result.stderr}
        try:
            row["json"] = json.loads(result.stdout)
        except ValueError:
            row["stdout"] = result.stdout
        report["commands"].append(row)
        assert result.returncode == code, row
        return row.get("json")
    try:
        with tempfile.TemporaryDirectory(prefix="unifier-cleanup-cli-", dir="/dev/shm") as remote:
            dst = Path(remote)
            sr = cli("root", "add", str(src.resolve()), "--cleanup-policy", "controlled")
            tr = cli("root", "add", str(dst))
            record = cli("file", "move", sr["root_uuid"], name, tr["root_uuid"], target_name, "--cleanup-source")
            assert record["complete"] and not record["source_retained"] and record["state"] == "DONE"
            assert (dst / target_name).read_bytes() == payload and not (src / name).exists()
            assert not list(src.iterdir()) and (dst / target_name).stat().st_nlink == 1
            assert cli("file", "status", record["operation_uuid"])["state"] == "DONE"
            cli("file", "recover", record["operation_uuid"])
            cli("file", "recover", record["operation_uuid"])
            cli("file", "resolve", tr["root_uuid"], target_name)
            report.update(passed=True, full_move_exit=0, source_removed=True,
                          target_sha256=hashlib.sha256(payload).hexdigest(),
                          source_and_target_different_devices=src.stat().st_dev != dst.stat().st_dev,
                          source_stage_quarantine_empty=True, path_characters_preserved=True)
    finally:
        with a.output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": report["passed"], "commands": len(report["commands"]), "full_move_exit": 0}))


if __name__ == "__main__":
    main()
