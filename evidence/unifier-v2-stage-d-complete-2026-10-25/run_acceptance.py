"""Independent combination gate against an unchanged D-completion snapshot."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())


def source_hashes():
    return {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (ROOT / "src/multilinux").glob("*.py")}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--prefix", required=True)
    a = p.parse_args()
    output = HERE / (a.prefix + "-acceptance.json")
    assert not output.exists()
    run = ROOT / ".lab/agents/unifier-v2-stage-d2-20261025"
    assert json.loads((run / "finish-run.json").read_text())["exit_code"] == 0
    report = {"passed": False, "started_at": time.time(), "source_sha256": source_hashes(), "steps": {}}
    def step(name, script, *args):
        child = subprocess.run([sys.executable, str(script), *map(str, args)],
                               cwd=ROOT, text=True, capture_output=True, timeout=100)
        report["steps"][name] = {"exit": child.returncode, "stdout": child.stdout[-3500:], "stderr": child.stderr[-3500:]}
        print(json.dumps({"step": name, "exit": child.returncode}), flush=True)
        assert child.returncode == 0, (name, child.stderr[-2500:])
    def result(label):
        return HERE / (a.prefix + "-" + label + ".json")
    try:
        step("C-regression", HERE / "projection-fixtures/run_gate.py", "--out", HERE, "--prefix", a.prefix + "-C")
        for script, name in [("probe_cleanup_native.py", "controlled-native"),
                             ("probe_cleanup_windows.py", "cleanup-windows"),
                             ("legacy-probe_native.py", "legacy-native"),
                             ("probe_enospc.py", "ENOSPC")]:
            step(name, HERE / script, "--output", result(name))
        step("controlled-cli", HERE / "probe_cleanup_cli.py", "--workdir", HERE / (a.prefix + "-controlled-cli"),
             "--output", result("controlled-cli"))
        step("legacy-cli", HERE / "probe_cli.py", "--workdir", HERE / (a.prefix + "-legacy-cli"),
             "--output", result("legacy-cli"))
        step("view-copy", HERE / "probe_view_copy.py", "--fixtures", HERE / "projection-fixtures/fixtures.json",
             "--output", result("view-copy"))
        report["source_unchanged"] = report["source_sha256"] == source_hashes()
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
