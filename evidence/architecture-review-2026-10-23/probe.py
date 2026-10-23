#!/usr/bin/env python3
"""Isolated filesystem and catalog review; no product or container mutation.

Crash recovery deliberately retains the source. This is a copy/publication
prototype, not an atomic cross-filesystem move implementation.
"""
import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def syncdir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def write_json(path, value):
    temp = path.with_suffix(".tmp")
    with temp.open("w") as f:
        json.dump(value, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    syncdir(path.parent)


def identity(path):
    st = path.stat()
    return {"device": st.st_dev, "inode": st.st_ino,
            "size": st.st_size, "mtime_ns": st.st_mtime_ns,
            "ctime_ns": st.st_ctime_ns, "sha256": digest(path)}


def worker(source, folder, phase):
    folder = Path(folder)
    source = Path(source)
    journal = {"op_id": "test-op", "source": str(source),
               "source_identity": identity(source), "state": "intent"}
    write_json(folder / "journal.json", journal)
    if phase == "intent":
        os._exit(77)
    stage = folder / "stage"
    with source.open("rb") as src, stage.open("xb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    syncdir(folder)
    if phase == "staged":
        os._exit(77)
    # Same-filesystem, no-clobber publication of a regular file.
    os.link(stage, folder / "published")
    syncdir(folder)
    if phase == "published":
        os._exit(77)
    raise ValueError(phase)


def recover(folder):
    journal = json.loads((folder / "journal.json").read_text())
    source = Path(journal["source"])
    stage, dest = folder / "stage", folder / "published"
    original = journal["source_identity"]
    if not source.exists() or identity(source) != original:
        return "conflict-source-retained"
    if dest.exists():
        # A matching hash alone is insufficient: require this operation's
        # retained staging inode. An unrelated same-content target is conflict.
        if not stage.exists() or not os.path.samefile(stage, dest):
            return "conflict-target-retained"
        if digest(dest) != original["sha256"]:
            return "conflict-target-retained"
    elif not stage.exists():
        return "source-retained-no-copy"
    else:
        if digest(stage) != original["sha256"]:
            return "conflict-stage-retained"
        try:
            os.link(stage, dest)
        except FileExistsError:
            return "conflict-target-retained"
        syncdir(folder)
    with sqlite3.connect(folder / "registry.sqlite") as db:
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS operations (id TEXT PRIMARY KEY, destination TEXT, state TEXT)")
        db.execute("INSERT OR IGNORE INTO operations VALUES (?, ?, ?)",
                   (journal["op_id"], str(dest), "source-cleanup-pending"))
    syncdir(folder)
    return "source-cleanup-pending"


def run(output):
    from multilinux.catalog import PREPARED, describe, require_runnable, plan_change
    from multilinux.packages import driver_for
    report = {"time": datetime.now(timezone(timedelta(hours=8))).isoformat(),
              "product_acceptance": False, "kernel": os.uname().release,
              "source_sha256": digest(Path(__file__)), "checks": {},
              "observations": {}, "limits": [
                  "Local Btrfs and tmpfs regular-file mechanism only; no containers or GUI.",
                  "External operations run while no watcher exists; no real IN_Q_OVERFLOW injection.",
                  "os._exit is process interruption, not host power loss; tmpfs is not power-durable.",
                  "Recovery retains source; source deletion and concurrent-writer CAS are not implemented.",
                  "No inode reuse, ACL, SELinux, mmap, quota, ENOSPC or arbitrary tree recovery coverage."]}
    checks, observations = report["checks"], report["observations"]
    with tempfile.TemporaryDirectory(prefix="ml-review-", dir=ROOT / ".lab") as local, \
            tempfile.TemporaryDirectory(prefix="ml-review-", dir="/dev/shm") as remote:
        base, remote = Path(local), Path(remote)
        host, env = base / "host", base / "env"
        host.mkdir(); env.mkdir()
        def domain(path):
            resolved = path.resolve(strict=True)
            for root, label in ((host, "host"), (env, "env:fixture")):
                if resolved.is_relative_to(root):
                    return label
            return "UNKNOWN"
        src = host / "源.txt"
        src.write_text("initial\n")
        os.setxattr(src, "user.multilinux-test-origin", b"host")
        copied = env / "副本.txt"
        subprocess.run(["cp", "--preserve=xattr", "--", str(src), str(copied)], check=True)
        checks["copy_follows_destination_despite_copied_xattr"] = (
            domain(copied) == "env:fixture" and
            os.getxattr(copied, "user.multilinux-test-origin") == b"host" and
            src.stat().st_ino != copied.stat().st_ino)
        old = identity(src)
        moved = env / "外部移动.txt"
        subprocess.run(["mv", "--", str(src), str(moved)], check=True)
        checks["offline_move_domain_recovered_from_destination"] = (
            domain(moved) == "env:fixture" and moved.stat().st_ino == old["inode"] and not src.exists())
        observations["offline_move"] = {"current_domain": domain(moved),
                                        "historical_provenance_without_log": "UNKNOWN"}
        replace = env / "replacement"
        replace.write_text("saved by editor\n")
        previous = moved.stat().st_ino
        os.replace(replace, moved)
        checks["editor_replace_changes_inode_not_domain"] = (
            moved.stat().st_ino != previous and domain(moved) == "env:fixture")
        linked = host / "hardlink"
        os.link(moved, linked)
        checks["one_inode_can_have_two_domain_paths"] = (
            os.path.samefile(moved, linked) and domain(moved) != domain(linked))
        link = host / "shortcut"
        link.symlink_to(moved)
        checks["symlink_target_resolves_to_environment_domain"] = domain(link) == "env:fixture"
        observations["hardlink"] = {"action": "ambiguous-alias; do not assign a unique inode owner",
                                     "link_count": moved.stat().st_nlink}
        xsource = host / "cross-source"
        xsource.write_text("cross-filesystem bytes\n")
        report["devices"] = {"local": base.stat().st_dev, "remote": remote.stat().st_dev}
        try:
            os.rename(xsource, remote / "rename")
            checks["cross_filesystem_rename_EXDEV"] = False
        except OSError as exc:
            checks["cross_filesystem_rename_EXDEV"] = exc.errno == errno.EXDEV and xsource.exists()
        recovery = []
        for phase in ("intent", "staged", "published"):
            folder = remote / phase
            folder.mkdir()
            proc = subprocess.run([sys.executable, __file__, "--worker", str(xsource), str(folder), phase])
            first = recover(folder)
            second = recover(folder)
            expected = "source-retained-no-copy" if phase == "intent" else "source-cleanup-pending"
            row = {"phase": phase, "worker_exit": proc.returncode, "first": first, "second": second,
                   "source_retained": xsource.exists()}
            if phase != "intent":
                with sqlite3.connect(folder / "registry.sqlite") as db:
                    row["registry_rows"] = db.execute("SELECT count(*) FROM operations").fetchone()[0]
                row["hash_equal"] = digest(xsource) == digest(folder / "published")
            checks[f"recover_{phase}_twice"] = (
                proc.returncode == 77 and first == second == expected and xsource.exists()
                and row.get("registry_rows", 1) == 1 and row.get("hash_equal", True))
            recovery.append(row)
        for conflict in ("source", "target", "same-content-target"):
            folder = remote / conflict
            folder.mkdir()
            proc = subprocess.run([sys.executable, __file__, "--worker", str(xsource), str(folder), "staged"])
            if conflict == "source":
                xsource.write_text("external writer's new bytes\n")
            else:
                (folder / "published").write_bytes(
                    xsource.read_bytes() if conflict == "same-content-target" else b"someone else's target\n")
            before = xsource.read_bytes()
            before_dest = (folder / "published").read_bytes() if (folder / "published").exists() else None
            result = recover(folder)
            expected = "conflict-source-retained" if conflict == "source" else "conflict-target-retained"
            checks[f"conflict_{conflict}_preserves_bytes"] = (
                proc.returncode == 77 and result == expected and xsource.read_bytes() == before and
                (before_dest is None or (folder / "published").read_bytes() == before_dest))
            recovery.append({"case": conflict, "worker_exit": proc.returncode, "result": result})
        observations["recovery"] = recovery
        spec = dict(PREPARED["debian13"])
        allowed = require_runnable("future-debian", describe("future-debian", spec))
        try:
            driver_for("future-debian")
            driver_error = None
        except Exception as exc:
            driver_error = str(exc)
        contradictory = {"id": "fixture", "target": "debian13",
                         "system": {**spec, "package_driver": "dnf"}}
        observations["catalog"] = {
            "unprepared_apt_contract_accepted": bool(allowed),
            "actual_driver_lookup_error": driver_error,
            "inconsistent_record_plan": plan_change(contradictory, operation="package-change"),
            "actual_target_driver": driver_for("debian13").name,
            "scope": "synthetic contract boundaries, not a corrupt live environment"}
    checks["temporary_paths_removed"] = not Path(local).exists() and not remote.exists()
    report["all_mechanism_checks_passed"] = all(checks.values())
    with output.open("x") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({"checks": checks, "observations": observations}, ensure_ascii=False, indent=2))
    return 0 if report["all_mechanism_checks_passed"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worker", nargs=3)
    args = parser.parse_args()
    if args.worker:
        worker(*args.worker)
    elif args.output:
        if args.output.exists():
            parser.error("output already exists")
        sys.exit(run(args.output))
    else:
        parser.error("--output required")
