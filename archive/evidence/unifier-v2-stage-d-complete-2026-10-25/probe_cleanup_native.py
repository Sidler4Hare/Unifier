"""Independent real-filesystem acceptance for explicitly controlled source cleanup.

Only fresh private Btrfs/tmpfs fixtures are mutated. Every reported crash is a
real SIGKILL of a worker; recovery is invoked twice in a fresh process context.
This tests process interruption, not physical disk or power-loss durability.
"""
import argparse
from contextlib import contextmanager
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
sys.path[:0] = [str(ROOT / "src")]
from multilinux.control import Control


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sources():
    return {str(p.relative_to(ROOT)): digest(p) for p in (ROOT / "src/multilinux").glob("*.py")}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--project", type=Path)
    parser.add_argument("--source-root")
    parser.add_argument("--target-root")
    parser.add_argument("--phase")
    args = parser.parse_args()
    if args.worker:
        c = Control(args.project)
        def stop(point):
            if point == args.phase:
                os.kill(os.getpid(), signal.SIGKILL)
        c.move_file(args.source_root, "payload.bin", args.target_root, "published.bin",
                    cleanup_source=True, fault_hook=stop)
        raise RuntimeError("crash point was not reached: " + args.phase)
    assert args.output and not args.output.exists()
    private = ROOT / ".lab/agents/unifier-v2-stage-d2-20261025"
    private.mkdir(parents=True, exist_ok=True, mode=0o700)
    report = {"passed": False, "source_sha256": sources(), "cases": {},
              "scope": "actual host Btrfs/tmpfs ordinary files and process SIGKILL; no power-loss claim"}
    payload = os.urandom(96 * 1024)
    expected = hashlib.sha256(payload).hexdigest()
    try:
        with tempfile.TemporaryDirectory(prefix="cleanup-native-", dir=private) as local_name, \
                tempfile.TemporaryDirectory(prefix="unifier-cleanup-", dir="/dev/shm") as remote_name:
            local, remote = Path(local_name), Path(remote_name)

            @contextmanager
            def fixture(label, *, source_base=None, target_base=None, policy="controlled"):
                project = local / (label + "-project")
                project.mkdir()
                src = (source_base or local) / (label + "-source")
                dst = (target_base or remote) / (label + "-target")
                src.mkdir(mode=0o700)
                dst.mkdir(mode=0o700)
                (src / "payload.bin").write_bytes(payload)
                with (src / "payload.bin").open("rb") as stream:
                    os.fsync(stream.fileno())
                c = Control(project)
                sr = c.add_root(str(src), cleanup_policy=policy)
                tr = c.add_root(str(dst))
                yield project, src, dst, c, sr, tr

            def full(c, dst, source_root, record):
                assert record["state"] == "DONE" and record["complete"] and not record["source_retained"], record
                assert digest(dst / "published.bin") == expected
                assert (dst / "published.bin").stat().st_nlink == 1
                assert len(c.operations.registry_collection.list()) == 1
                assert not list(dst.glob(".ml-stage-*"))
                # An unoccupied original source and no private capture remain.
                assert not list(source_root.iterdir()), list(source_root.iterdir())
                uid = record["operation_uuid"]
                c.recover_file_operations(uid)
                c.recover_file_operations(uid)
                assert c.file_operation(uid)["complete"]

            def run_case(label, function):
                result = {"passed": False}
                report["cases"][label] = result
                try:
                    result.update(function() or {})
                    result["passed"] = True
                except Exception:
                    result["error"] = traceback.format_exc()[-5000:]
                    raise

            trace = []
            def normal_cross():
                with fixture("normal-cross") as (_, src, dst, c, sr, tr):
                    r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                    cleanup_source=True, fault_hook=trace.append)
                    full(c, dst, src, r)
                    assert src.stat().st_dev != dst.stat().st_dev
                    return {"source_dev": src.stat().st_dev, "target_dev": dst.stat().st_dev,
                            "state": r["state"], "source_removed": True, "one_registration": True}
            run_case("normal-btrfs-to-tmpfs", normal_cross)
            for label, src_base, dst_base in [("normal-same-fs", local, local),
                                               ("normal-tmpfs-to-btrfs", remote, local)]:
                def action(label=label, src_base=src_base, dst_base=dst_base):
                    with fixture(label, source_base=src_base, target_base=dst_base) as (_, src, dst, c, sr, tr):
                        r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                        cleanup_source=True)
                        full(c, dst, src, r)
                        return {"state": r["state"], "source_removed": True}
                run_case(label, action)

            # Every exposed checkpoint is crashed after its own effects, using
            # SIGKILL so neither Python finally blocks nor explicit cleanup run.
            report["observed_checkpoints"] = list(dict.fromkeys(trace))
            for index, phase in enumerate(dict.fromkeys(trace)):
                def interrupted(index=index, phase=phase):
                    with fixture("crash-" + str(index)) as (project, src, dst, c, sr, tr):
                        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker",
                            "--project", str(project), "--source-root", sr["root_uuid"],
                            "--target-root", tr["root_uuid"], "--phase", phase],
                            capture_output=True, text=True, timeout=35)
                        assert child.returncode == -signal.SIGKILL, (phase, child.returncode, child.stderr[-1500:])
                        initial = c.list_file_operations()
                        assert len(initial) == 1
                        uid = initial[0]["operation_uuid"]
                        first = c.recover_file_operations(uid)
                        second = c.recover_file_operations(uid)
                        r = c.file_operation(uid)
                        full(c, dst, src, r)
                        return {"worker_signal": "SIGKILL", "before_state": initial[0]["state"],
                                "final_state": r["state"], "recovered_twice": True,
                                "first_action": first[0].get("recovery"),
                                "second_action": second[0].get("recovery")}
                run_case("SIGKILL:" + phase, interrupted)

            def retained_default():
                with fixture("default-retain") as (_, src, dst, c, sr, tr):
                    r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin")
                    c.recover_file_operations(r["operation_uuid"])
                    assert digest(src / "payload.bin") == digest(dst / "published.bin") == expected
                    assert not r["complete"] and r["source_retained"]
                    return {"state": r["state"], "source_preserved": True}
            run_case("no-operation-delete-authorization", retained_default)

            for label, mode in [("held-reader", os.O_RDONLY), ("held-writer", os.O_RDWR)]:
                def held(label=label, mode=mode):
                    with fixture(label) as (_, src, dst, c, sr, tr):
                        fd = os.open(src / "payload.bin", mode)
                        try:
                            r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                            cleanup_source=True)
                            assert not r["complete"] and r["source_retained"]
                            assert digest(src / "payload.bin") == digest(dst / "published.bin") == expected
                        finally:
                            os.close(fd)
                        return {"state": r["state"], "source_preserved": True}
                run_case(label, held)

            # Change the source, registration policy or published target after
            # the earlier copy has succeeded, before source cleanup can begin.
            for label in ["source-in-place-change", "source-name-replacement", "target-name-replacement", "policy-revocation"]:
                def changed(label=label):
                    with fixture(label) as (_, src, dst, c, sr, tr):
                        injected = False
                        foreign = b"foreign-object-must-survive"
                        def mutate(point):
                            nonlocal injected
                            if point != "STAGE_CLEANED" or injected:
                                return
                            injected = True
                            if label == "source-in-place-change":
                                (src / "payload.bin").write_bytes(foreign)
                            elif label == "source-name-replacement":
                                (src / "payload.bin").rename(src / "original-retained.bin")
                                (src / "payload.bin").write_bytes(foreign)
                            elif label == "target-name-replacement":
                                (dst / "published.bin").rename(dst / "our-target-retained.bin")
                                (dst / "published.bin").write_bytes(payload)
                            else:
                                registry = c.registry.path
                                data = json.loads(registry.read_text())
                                next(item for item in data["roots"] if item["root_uuid"] == sr["root_uuid"])["cleanup_policy"] = "retain"
                                registry.write_text(json.dumps(data))
                        r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                        cleanup_source=True, fault_hook=mutate)
                        assert injected and not r["complete"] and r["source_retained"], r
                        if label in {"source-in-place-change", "source-name-replacement"}:
                            assert (src / "payload.bin").read_bytes() == foreign
                        else:
                            assert digest(src / "payload.bin") == expected
                        c.recover_file_operations(r["operation_uuid"])
                        c.recover_file_operations(r["operation_uuid"])
                        assert (src / "payload.bin").exists()
                        return {"state": r["state"], "source_preserved": True, "foreign_bytes_preserved": True}
                run_case(label, changed)

            # Exercise the last durable boundary as well; validating before
            # capture is insufficient when the namespace changes afterwards.
            for label in ["late-policy-revocation", "late-source-root-replacement",
                          "late-target-replacement", "late-target-root-replacement",
                          "late-source-hardlink", "late-target-hardlink", "late-source-lease-break"]:
                def late(label=label):
                    with fixture(label) as (_, src, dst, c, sr, tr):
                        injected = False
                        auxiliary = {}
                        parked = src.with_name(src.name + "-parked")
                        def mutate(point):
                            nonlocal injected
                            if point != "SOURCE_DELETION_INTENT" or injected:
                                return
                            injected = True
                            q = next(src.glob(".ml-quarantine-*/source"))
                            if label == "late-policy-revocation":
                                data = json.loads(c.registry.path.read_text())
                                next(x for x in data["roots"] if x["root_uuid"] == sr["root_uuid"])["cleanup_policy"] = "retain"
                                c.registry.path.write_text(json.dumps(data))
                            elif label == "late-source-root-replacement":
                                src.rename(parked)
                                src.mkdir()
                                (src / "foreign.bin").write_bytes(b"new-root-bytes")
                            elif label == "late-target-replacement":
                                (dst / "published.bin").rename(dst / "our-target.bin")
                                (dst / "published.bin").write_bytes(payload)
                            elif label == "late-target-root-replacement":
                                dst.rename(dst.with_name(dst.name + "-parked"))
                                dst.mkdir()
                                (dst / "foreign.bin").write_bytes(b"new-target-root")
                            elif label == "late-source-hardlink":
                                os.link(q, src / "new-alias.bin")
                            elif label == "late-target-hardlink":
                                os.link(dst / "published.bin", dst / "new-alias.bin")
                            else:
                                code = ('import os,sys,json;\ntry:\n '
                                        'fd=os.open(sys.argv[1],os.O_WRONLY|os.O_NONBLOCK);os.close(fd);'
                                        'print(json.dumps({"opened":True}))\n'
                                        'except OSError as e: print(json.dumps({"errno":e.errno}))')
                                auxiliary.update(json.loads(subprocess.check_output(
                                    [sys.executable, "-c", code, str(q)], text=True, timeout=5)))
                        r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                        cleanup_source=True, fault_hook=mutate)
                        assert injected and not r["complete"] and r["source_retained"], r
                        scopes = [src] + ([parked] if parked.exists() else [])
                        assert any(f.is_file() and digest(f) == expected
                                   for scope in scopes for f in scope.rglob("*")), "captured source bytes lost"
                        if label == "late-source-lease-break":
                            assert auxiliary.get("errno") == 11, auxiliary
                        c.recover_file_operations(r["operation_uuid"])
                        c.recover_file_operations(r["operation_uuid"])
                        return {"state": r["state"], "source_bytes_preserved": True,
                                "late_guard_rejected": True, "auxiliary": auxiliary}
                run_case(label, late)

            def repopulated():
                with fixture("repopulated") as (_, src, dst, c, sr, tr):
                    marker = b"a new unrelated original-name object"
                    def refill(point):
                        if point == "SOURCE_DELETION_INTENT":
                            (src / "payload.bin").write_bytes(marker)
                    r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                    cleanup_source=True, fault_hook=refill)
                    assert r["complete"] and not r["source_retained"], r
                    assert (src / "payload.bin").read_bytes() == marker
                    assert digest(dst / "published.bin") == expected
                    assert not list(src.glob(".ml-quarantine-*"))
                    c.recover_file_operations(r["operation_uuid"])
                    c.recover_file_operations(r["operation_uuid"])
                    assert (src / "payload.bin").read_bytes() == marker
                    return {"complete": True, "repopulated_foreign_source_preserved": True}
            run_case("original-name-repopulated", repopulated)

            def raced_capture():
                with fixture("foreign-capture") as (_, src, dst, c, sr, tr):
                    marker = b"foreign capture must survive"
                    injected = False
                    def replace(point):
                        nonlocal injected
                        if point == "SOURCE_CAPTURE_INTENT" and not injected:
                            injected = True
                            (src / "payload.bin").rename(src / "original-parked.bin")
                            (src / "payload.bin").write_bytes(marker)
                    r = c.move_file(sr["root_uuid"], "payload.bin", tr["root_uuid"], "published.bin",
                                    cleanup_source=True, fault_hook=replace)
                    assert injected and not r["complete"] and r["source_retained"], r
                    assert digest(src / "original-parked.bin") == expected
                    assert any(f.is_file() and f.read_bytes() == marker for f in src.rglob("*"))
                    c.recover_file_operations(r["operation_uuid"])
                    c.recover_file_operations(r["operation_uuid"])
                    assert digest(src / "original-parked.bin") == expected
                    assert any(f.is_file() and f.read_bytes() == marker for f in src.rglob("*"))
                    return {"state": r["state"], "foreign_bytes_and_original_preserved": True}
            run_case("capture-raced-foreign-inode", raced_capture)
        report["source_unchanged"] = report["source_sha256"] == sources()
        assert report["source_unchanged"]
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()[-5000:]
    finally:
        with args.output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": report["passed"], "cases": len(report["cases"]), "output": str(args.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
