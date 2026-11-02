"""Real interruption between cleanup filesystem effects and journal writes."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import traceback

ROOT = next(p for p in Path(__file__).resolve().parents if (p / "src/multilinux").is_dir())
sys.path.insert(0, str(ROOT / "src"))
from multilinux.control import Control


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path)
    p.add_argument("--worker", action="store_true")
    p.add_argument("--project", type=Path)
    p.add_argument("--source-root")
    p.add_argument("--target-root")
    p.add_argument("--phase")
    p.add_argument("--recover")
    a = p.parse_args()
    if a.worker:
        c = Control(a.project)
        write = c.operations._write_journal
        def interrupted(record):
            if record["state"] == a.phase:
                os.kill(os.getpid(), signal.SIGKILL)
            return write(record)
        c.operations._write_journal = interrupted
        if a.recover:
            c.recover_file_operations(a.recover)
        else:
            c.move_file(a.source_root, "source.bin", a.target_root, "target.bin", cleanup_source=True)
        raise RuntimeError("crash window not reached: " + a.phase)
    assert a.output and not a.output.exists()
    before = {str(f.relative_to(ROOT)): sha(f) for f in (ROOT / "src/multilinux").glob("*.py")}
    report = {"passed": False, "source_sha256": before, "cases": {},
              "scope": "real private files, SIGKILL before journal persist; unproven ownership remains reconciliation"}
    payload = os.urandom(64 * 1024)
    wanted = hashlib.sha256(payload).hexdigest()
    private = ROOT / ".lab/agents/unifier-v2-stage-d2-20261025"
    try:
        with tempfile.TemporaryDirectory(prefix="windows-", dir=private) as left, \
                tempfile.TemporaryDirectory(prefix="unifier-windows-", dir="/dev/shm") as right:
            left, right = Path(left), Path(right)
            def prepare(name):
                project, src, dst = left / name, left / (name + "-src"), right / name
                for f in [project, src, dst]:
                    f.mkdir(mode=0o700)
                (src / "source.bin").write_bytes(payload)
                c = Control(project)
                sr = c.add_root(str(src), cleanup_policy="controlled")
                tr = c.add_root(str(dst))
                return project, src, dst, c, sr, tr

            def crash(project, sr, tr, phase, operation=None):
                command = [sys.executable, str(Path(__file__).resolve()), "--worker", "--project", str(project),
                           "--source-root", sr["root_uuid"], "--target-root", tr["root_uuid"], "--phase", phase]
                if operation:
                    command.extend(["--recover", operation])
                child = subprocess.run(command, capture_output=True, text=True, timeout=15)
                assert child.returncode == -signal.SIGKILL, (phase, child.returncode, child.stderr[-1500:])

            phases = ["SOURCE_QUARANTINE_READY", "SOURCE_CAPTURED", "SOURCE_ISOLATED", "SOURCE_REMOVED", "DONE"]
            for index, phase in enumerate(phases):
                row = {"passed": False}
                report["cases"]["before-journal:" + phase] = row
                project, src, dst, c, sr, tr = prepare("gap-" + str(index))
                crash(project, sr, tr, phase)
                record = c.list_file_operations()[0]
                row["persisted_state"] = record["state"]
                c.recover_file_operations(record["operation_uuid"])
                c.recover_file_operations(record["operation_uuid"])
                result = c.file_operation(record["operation_uuid"])
                assert sha(dst / "target.bin") == wanted
                assert len(c.operations.registry_collection.list()) == 1
                if phase == "SOURCE_QUARANTINE_READY":
                    # mkdir happened but its inode identity was not journaled.
                    # Treat this unknown directory conservatively, not as ours.
                    assert not result["complete"] and result["source_retained"]
                    assert sha(src / "source.bin") == wanted
                    assert result["state"] == "NEEDS_RECONCILIATION"
                    row["unproven_directory_retained"] = True
                else:
                    assert result["complete"] and not result["source_retained"], result
                    assert not list(src.iterdir())
                row.update(passed=True, final_state=result["state"], one_registration=True,
                           recovered_twice=True, target_bytes_valid=True)

            # Crash recovery itself after the unlink but before its journal,
            # then interrupt recovery once more before DONE is durable.
            project, src, dst, c, sr, tr = prepare("repeated-recovery")
            crash(project, sr, tr, "SOURCE_ISOLATED")
            operation = c.list_file_operations()[0]["operation_uuid"]
            crash(project, sr, tr, "SOURCE_REMOVED", operation)
            crash(project, sr, tr, "DONE", operation)
            c.recover_file_operations(operation)
            c.recover_file_operations(operation)
            result = c.file_operation(operation)
            assert result["complete"] and not result["source_retained"] and not list(src.iterdir()), result
            assert sha(dst / "target.bin") == wanted and len(c.operations.registry_collection.list()) == 1
            report["cases"]["recovery-SIGKILL-twice"] = {
                "passed": True, "final_state": result["state"], "recovery_interrupted_twice": True,
                "source_removed": True, "one_registration": True}

            # An unknown same-name directory must not be adopted or chmodded.
            project, src, dst, c, sr, tr = prepare("unknown-quarantine")
            changed = False
            def place_unknown(point):
                nonlocal changed
                if point == "SOURCE_QUARANTINE_INTENT":
                    record = c.list_file_operations()[0]
                    q = src / record["source_cleanup"]["quarantine"]["dir_name"]
                    q.mkdir(mode=0o755)
                    (q / "foreign.txt").write_text("foreign directory marker")
                    changed = True
            result = c.move_file(sr["root_uuid"], "source.bin", tr["root_uuid"], "target.bin",
                                 cleanup_source=True, fault_hook=place_unknown)
            assert changed and not result["complete"] and result["source_retained"]
            q = next(src.glob(".ml-quarantine-*"))
            assert q.stat().st_mode & 0o777 == 0o755
            assert (q / "foreign.txt").read_text() == "foreign directory marker"
            assert sha(src / "source.bin") == wanted
            c.recover_file_operations(result["operation_uuid"])
            c.recover_file_operations(result["operation_uuid"])
            assert q.stat().st_mode & 0o777 == 0o755
            report["cases"]["unknown-quarantine-no-adoption-or-chmod"] = {
                "passed": True, "state": result["state"], "foreign_mode_and_bytes_preserved": True}

        report["source_unchanged"] = all(sha(ROOT / n) == h for n, h in before.items())
        assert report["source_unchanged"]
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()[-7000:]
    with a.output.open("x") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]), "output": str(a.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
