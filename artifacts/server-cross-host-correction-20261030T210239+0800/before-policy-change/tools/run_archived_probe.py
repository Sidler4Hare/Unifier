#!/usr/bin/env python3
"""Replay an archived driver in its original directory layout using current code.

Archive bytes are copied, never edited. The fresh workspace has independent
state; it is not a sandbox for drivers that explicitly invoke Podman or the GUI.
Historical drivers can still require an older API or additional input data.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
SOURCE_TREES = ("src", "tests", "tools", "config", "fixtures", "bin")


def archived_script(root: Path, value: str) -> Path:
    archive = (root / "archive/evidence").resolve(strict=True)
    script = (root / value).resolve(strict=True)
    relative = script.relative_to(archive)
    if len(relative.parts) != 2 or script.suffix != ".py" or not script.is_file():
        raise ValueError("choose a Python driver directly inside an archived run directory")
    return relative


def replay(root: Path, script: Path, workspace: Path, arguments: list[str], *,
           include_runs: Sequence[str] = ()) -> int:
    root = root.resolve(strict=True)
    workspace = workspace.resolve(strict=False)
    if workspace.is_relative_to(root):
        raise ValueError("the replay workspace must be outside the repository")
    archive = root / "archive/evidence"
    runs = list(dict.fromkeys([script.parts[0], *include_runs]))
    for name in runs:
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"invalid archived run name: {name}")
        if not (archive / name).is_dir():
            raise ValueError(f"archived run is missing: {name}")
    workspace.mkdir()  # An existing output is never overwritten.
    for name in SOURCE_TREES:
        if (root / name).is_dir():
            shutil.copytree(root / name, workspace / name, symlinks=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    for name in runs:
        shutil.copytree(archive / name, workspace / "evidence" / name, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    environment = dict(os.environ)
    environment.update(
        PYTHONDONTWRITEBYTECODE="1",
        PYTHONPATH=os.pathsep.join(str(workspace / name) for name in ("src", "tests", "")),
        UNIFIER_HOME=str(workspace / ".unifier-home"),
    )
    print(f"replay: current source copied to {workspace}; driver exit code is preserved",
          file=sys.stderr, flush=True)
    result = subprocess.run(
        [sys.executable, str(workspace / "evidence" / script), *arguments],
        cwd=workspace, env=environment)
    return result.returncode if result.returncode >= 0 else 128 - result.returncode


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path,
                        help="retain a new workspace outside the repository; default temporary")
    parser.add_argument("--include-run", action="append", default=[], metavar="NAME",
                        help="also copy another archived run needed by this driver; repeatable")
    parser.add_argument("script", help="evidence/<run>/<driver>.py or archive/evidence/... path")
    parser.add_argument("arguments", nargs=argparse.REMAINDER,
                        help="driver arguments, optionally preceded by --")
    args = parser.parse_args(argv)
    try:
        script = archived_script(ROOT, args.script)
        arguments = args.arguments[1:] if args.arguments[:1] == ["--"] else args.arguments
        if args.workspace is not None:
            return replay(ROOT, script, args.workspace, arguments, include_runs=args.include_run)
        with tempfile.TemporaryDirectory(prefix="unifier-archive-replay-") as temporary:
            return replay(ROOT, script, Path(temporary) / "checkout", arguments,
                          include_runs=args.include_run)
    except (OSError, ValueError, shutil.Error) as exc:
        print(f"replay: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
