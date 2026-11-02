"""Combine E native proof with the previously accepted C/D real regression."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())
D = ROOT / "evidence/unifier-v2-stage-d-complete-2026-10-25"


def hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "src/multilinux").glob("*.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    output = HERE / (args.prefix + "-acceptance.json")
    assert not output.exists()
    run = ROOT / ".lab/agents/unifier-v2-stage-e-20261025"
    assert json.loads((run / "verify_fix-run.json").read_text())["exit_code"] == 0
    before = hashes()
    d = json.loads((D / "acceptance-result.json").read_text())
    for name in ["file_operations.py", "lease.py", "registry.py"]:
        path = "src/multilinux/" + name
        assert before[path] == d["source_sha256"][path], "frozen D source changed"
    report = {"passed": False, "source_sha256": before, "started_at": time.time(), "steps": {}}

    def result(name):
        return HERE / (args.prefix + "-" + name + ".json")

    def step(name, script, *arguments):
        child = subprocess.run([sys.executable, str(script), *map(str, arguments)],
                               cwd=ROOT, text=True, capture_output=True, timeout=240)
        report["steps"][name] = {"exit": child.returncode, "stdout": child.stdout[-4500:], "stderr": child.stderr[-4500:]}
        print(json.dumps({"step": name, "exit": child.returncode}), flush=True)
        assert child.returncode == 0, (name, child.stderr[-2500:])

    try:
        step("C-regression", HERE / "projection-fixtures/run_gate.py", "--out", HERE, "--prefix", args.prefix + "-C")
        for filename, label in [("probe_cleanup_native.py", "D-controlled"),
                                ("probe_cleanup_windows.py", "D-windows"),
                                ("legacy-probe_native.py", "D-legacy"),
                                ("probe_enospc.py", "D-ENOSPC")]:
            step(label, D / filename, "--output", result(label))
        step("D-CLI", D / "probe_cleanup_cli.py", "--workdir", HERE / (args.prefix + "-D-CLI"), "--output", result("D-CLI"))
        step("D-legacy-CLI", D / "probe_cli.py", "--workdir", HERE / (args.prefix + "-D-legacy-CLI"),
             "--output", result("D-legacy-CLI"))
        step("C-D-copy", D / "probe_view_copy.py", "--fixtures", HERE / "projection-fixtures/fixtures.json",
             "--output", result("C-D-copy"))
        step("E-sessions", HERE / "probe_native_sessions.py", "--refresh-code", "--reset-settled-sessions",
             "--output", result("E-sessions"))
        step("E-desktop", HERE / "probe_native_desktop.py", "--workdir", HERE / (args.prefix + "-E-desktop"),
             "--output", result("E-desktop"))
        step("E-revoke-windows", HERE / "probe_desktop_revoke_windows.py", "--desktop-proof", result("E-desktop"),
             "--workdir", HERE / (args.prefix + "-E-windows"), "--output", result("E-windows"))
        report["source_unchanged"] = before == hashes()
        assert report["source_unchanged"]
        report["passed"] = True
    except Exception as exc:
        report["error"] = str(exc)
    finally:
        report["finished_at"] = time.time()
        with output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": report["passed"], "output": str(output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
