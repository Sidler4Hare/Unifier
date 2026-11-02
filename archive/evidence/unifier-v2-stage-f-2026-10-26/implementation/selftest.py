#!/usr/bin/env python3
"""Pure-helper and workload-script self-test; uses temporary directories only.

This test never imports the product, never starts a container, never opens a
FUSE view and never touches desktop state.  It is the only test run by the
implementation agent before primary execution.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
from probe_projection import Inotify, atomic_replace, parse_workload  # noqa: E402
from probe_sessions import guest_command, parse_result  # noqa: E402
from resource_sampler import cgroup_metrics, parse_smaps_rollup  # noqa: E402


def check(name, condition, detail=None):
    if not condition:
        raise AssertionError(f"{name} failed: {detail!r}")
    print(f"ok  {name}")


def test_stats():
    values = common.stats([5, 1, 3, 2, 4])
    check("stats count", values["count"] == 5)
    check("stats p50 nearest-rank", values["p50"] == 3, values)
    check("stats p95 nearest-rank", values["p95"] == 5, values)
    check("stats None empty", common.stats([]) is None)


def make_proc(root: Path, available: int, full: float, some: float):
    proc = root / "proc"
    pressure = proc / "pressure"
    pressure.mkdir(parents=True)
    (proc / "meminfo").write_text(
        "MemTotal:       8000000 kB\n"
        "MemFree:         500000 kB\n"
        f"MemAvailable:   {available // 1024} kB\n"
        "SwapTotal:      8000000 kB\n"
        "SwapFree:       7000000 kB\n")
    for kind in ("memory", "cpu", "io"):
        (pressure / kind).write_text(
            "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n"
            f"full avg10={full if kind == 'memory' else 0.0:.2f} "
            f"avg60=0.00 avg300=0.00 total=1\n")
    return proc


def test_guard():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        proc = make_proc(root, 2 * 1024 ** 3, 1.0, 2.0)
        state = common.memory_guard_state(proc_root=proc)
        check("guard headroom ok", state["ok"], state)
        proc_low = make_proc(root / "low", 100 * 1024 ** 2, 1.0, 2.0)
        state = common.memory_guard_state(proc_root=proc_low)
        check("guard low memory refuses", not state["ok"], state)
        proc_psi = make_proc(root / "psi", 2 * 1024 ** 3, 60.0, 95.0)
        state = common.memory_guard_state(proc_root=proc_psi)
        check("guard catastrophic PSI refuses", not state["ok"], state)


def test_run_dir_and_json():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        run = common.create_run_dir(root / "run")
        check("run dir created", run.is_dir())
        try:
            common.create_run_dir(run)
        except common.ProbeError:
            pass
        else:
            raise AssertionError("create_run_dir reused an existing directory")
        record = run / "record.json"
        common.write_new_json(record, {"a": 1})
        try:
            common.write_new_json(record, {"a": 2})
        except FileExistsError:
            pass
        else:
            raise AssertionError("write_new_json replaced existing evidence")
        common.atomic_write_json(run / "progress.json", {"phase": 1})
        common.atomic_write_json(run / "progress.json", {"phase": 2})
        check("atomic status update", common.read_json(run / "progress.json")["phase"] == 2)
        common.append_jsonl(run / "failures.jsonl", {"error": "kept"})
        common.append_jsonl(run / "failures.jsonl", {"error": "kept2"})
        check("append-only failure evidence",
              len((run / "failures.jsonl").read_text().splitlines()) == 2)


def test_proc_stat():
    with tempfile.TemporaryDirectory() as temporary:
        proc = Path(temporary) / "proc"
        directory = proc / "1234"
        directory.mkdir(parents=True)
        # Fields after the comm: state ppid ... starttime is field 22.
        fields = ["S", "1"] + ["0"] * 17 + ["987654", "0", "0", "0"]
        (directory / "stat").write_text("1234 (name with space) " + " ".join(fields))
        parsed = common.parse_proc_stat(1234, proc)
        check("proc stat identity parser",
              parsed is not None and parsed["starttime"] == 987654, parsed)


def build_workload_dataset(directory: Path, count: int = 20):
    dataset = directory / "dataset"
    small = dataset / "small"
    small.mkdir(parents=True)
    import hashlib
    digest = hashlib.sha256()
    for index in range(count):
        payload = hashlib.sha256(f"selftest:{index}".encode()).digest() * 128
        (small / f"f{index:04d}").write_bytes(payload)
        digest.update(payload)
    sequential = hashlib.sha256(b"selftest-sequential").digest() * 32768
    (dataset / "sequential.bin").write_bytes(sequential)
    shutil.copy2(HERE / "workload.sh", dataset / "workload.sh")
    return dataset, digest.hexdigest(), hashlib.sha256(sequential).hexdigest()


def test_workload_script():
    with tempfile.TemporaryDirectory() as temporary:
        dataset, small_hash, seq_hash = build_workload_dataset(Path(temporary))
        result = subprocess.run(
            ["/bin/sh", str(dataset / "workload.sh"), str(dataset), "2", "20",
             small_hash, seq_hash],
            capture_output=True, text=True, timeout=60)
        check("workload script exit", result.returncode == 0, result.stderr)
        rows = parse_workload(result.stdout)
        check("workload rows", len(rows) == 2, rows)
        check("workload hashes",
              all(row["small_sha"] == small_hash and row["seq_sha"] == seq_hash
                  for row in rows), rows)
        check("workload timings",
              all(isinstance(row[key], int) and row[key] >= 0
                  for row in rows for key in ("stat_ns", "read_ns", "seq_ns")))
        wrong = subprocess.run(
            ["/bin/sh", str(dataset / "workload.sh"), str(dataset), "1", "20",
             "0" * 64, seq_hash], capture_output=True, text=True, timeout=60)
        check("workload wrong hash propagates", wrong.returncode != 0, wrong.returncode)


def test_inotify():
    try:
        watch = Inotify()
    except OSError as exc:
        print(f"skip inotify self-test: {exc}")
        return
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        try:
            watch.add(directory)
            (directory / "event.txt").write_text("x")
            events = []
            deadline = time.monotonic() + 2
            while not events and time.monotonic() < deadline:
                events.extend(watch.read())
                time.sleep(0.02)
            check("inotify host control event", bool(events), events)
        finally:
            watch.close()


def test_session_helpers():
    with tempfile.TemporaryDirectory() as temporary:
        marker = Path(temporary)
        script = guest_command("fedora44", marker, 7, 2.1)
        check("guest result parser",
              parse_result("noise\nRESULT id=fedora uid=1000 hash=abc\n")
              == {"id": "fedora", "uid": "1000", "hash": "abc"})
        syntax = subprocess.run(["/bin/sh", "-n", "-c", script],
                                capture_output=True, text=True, timeout=10)
        check("guest command shell syntax", syntax.returncode == 0, syntax.stderr)


def test_atomic_replace_and_cgroup_parsing():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        target = root / "file.txt"
        target.write_bytes(b"old")
        outcome = atomic_replace(target, b"new")
        check("atomic replace new bytes", target.read_bytes() == b"new", outcome)
        smaps = ("Rss: 100 kB\nPss: 40 kB\nPss_Anon: 20 kB\n"
                 "Pss_File: 10 kB\nPss_Shmem: 10 kB\n")
        parsed = parse_smaps_rollup(smaps)
        check("smaps parser", parsed["pss_bytes"] == 40 * 1024, parsed)
        cgroup = root / "cg"
        cgroup.mkdir()
        (cgroup / "memory.current").write_text("1234\n")
        (cgroup / "memory.peak").write_text("2000\n")
        (cgroup / "memory.events").write_text("oom_kill 0\n")
        (cgroup / "memory.stat").write_text("anon 100\nfile 200\n")
        metrics = cgroup_metrics(cgroup)
        check("cgroup parser", metrics["memory_current_bytes"] == 1234
              and metrics["memory_peak_bytes"] == 2000, metrics)


def test_fixture_manifest_shape():
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "fixtures.json"
        common.write_new_json(path, {
            "prepared": True,
            "project": str(Path(temporary) / "project"),
            "targets": {target: {} for target in common.TARGETS},
        })
        loaded = common.load_fixtures(path)
        check("fixture manifest loads", set(loaded["targets"]) == set(common.TARGETS))
        common.write_new_json(Path(temporary) / "bad.json",
                              {"prepared": True, "targets": {"debian13": {}}})
        try:
            common.load_fixtures(Path(temporary) / "bad.json")
        except common.ProbeError:
            pass
        else:
            raise AssertionError("incomplete fixture manifest was accepted")


def main() -> int:
    test_stats()
    test_guard()
    test_run_dir_and_json()
    test_proc_stat()
    test_workload_script()
    test_session_helpers()
    test_atomic_replace_and_cgroup_parsing()
    test_inotify()
    test_fixture_manifest_shape()
    print(json.dumps({"status": "passed", "tests": 9}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
