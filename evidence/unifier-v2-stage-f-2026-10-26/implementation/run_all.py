#!/usr/bin/env python3
"""Run the focused Stage F local probe sequence, stopping on first failure.

This is a convenience orchestrator, not an acceptance decision.  It never
cleans containers.  Run ``cleanup_fixtures.py`` separately after inspecting
the evidence.  Each probe still creates its own exclusive run directory.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def child_command(script: str, fixtures: Path, run_dir: Path, extra=None):
    return [sys.executable, str(HERE / script), "--fixtures", str(fixtures),
            "--run-dir", str(run_dir), *(extra or [])]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run prepare (optional), sessions, projection and bounded "
                    "soak in order. Stops on first failure; never cleans.")
    parser.add_argument("--fixtures", type=Path, default=common.FIXTURES_DEFAULT)
    parser.add_argument("--run-root", type=Path, required=True,
                        help="new parent directory for the stage run directories")
    parser.add_argument("--prepare", action="store_true",
                        help="run prepare_fixtures.py first (fixtures must not exist)")
    parser.add_argument("--skip-soak", action="store_true")
    parser.add_argument("--soak-seconds", type=int, default=180)
    args = parser.parse_args(argv)
    root = common.create_run_dir(args.run_root)
    logs = root / "logs"
    logs.mkdir()
    summary = {
        "schema_version": 1,
        "status": "running",
        "acceptance": "pending-primary-review",
        "started_at": common.now_iso(),
        "run_root": str(root),
        "stages": [],
    }
    common.atomic_write_json(root / "run-all-summary.json", summary)

    def record_stage(name: str, command, returncode: int, started: float, log_path: Path):
        summary["stages"].append({
            "name": name, "command": [str(item) for item in command],
            "exit": returncode, "wall_seconds": round(time.monotonic() - started, 3),
            "log": str(log_path),
        })
        common.atomic_write_json(root / "run-all-summary.json", summary)

    try:
        if args.prepare:
            started = time.monotonic()
            log_path = logs / "prepare.log"
            command = [sys.executable, str(HERE / "prepare_fixtures.py"),
                       "--output", str(args.fixtures)]
            with open(log_path, "xb") as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                         text=True)
            record_stage("prepare", command, process.returncode, started, log_path)
            if process.returncode != 0:
                summary["status"] = "failed"
                summary["failed_stage"] = "prepare"
                common.atomic_write_json(root / "run-all-summary.json", summary)
                return 1
        fixtures = common.load_fixtures(args.fixtures)
        stamp = time.strftime("%Y%m%dT%H%M%S")
        stages = [("sessions", "probe_sessions.py", [])]
        stages.append(("projection", "probe_projection.py", []))
        if not args.skip_soak:
            stages.append(("soak", "probe_soak.py",
                           ["--seconds", str(args.soak_seconds)]))
        for name, script, extra in stages:
            run_dir = root / f"{name}-{stamp}"
            command = child_command(script, Path(args.fixtures), run_dir, extra)
            log_path = logs / f"{name}.log"
            started = time.monotonic()
            with open(log_path, "xb") as log:
                process = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT,
                                         text=True)
            record_stage(name, command, process.returncode, started, log_path)
            if process.returncode != 0:
                summary["status"] = "failed"
                summary["failed_stage"] = name
                summary["finished_at"] = common.now_iso()
                common.atomic_write_json(root / "run-all-summary.json", summary)
                return 1
        summary["status"] = "probes-complete"
        summary["finished_at"] = common.now_iso()
        common.atomic_write_json(root / "run-all-summary.json", summary)
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except BaseException as exc:
        summary["status"] = "failed"
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()[-12000:]
        summary["finished_at"] = common.now_iso()
        common.atomic_write_json(root / "run-all-summary.json", summary)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
