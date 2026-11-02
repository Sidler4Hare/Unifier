#!/usr/bin/env python3
"""One offline repository check, optionally against the exact staged snapshot."""

from __future__ import annotations

import argparse
import ast
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]


def run(root: Path, environment: dict[str, str], command: list[str]) -> None:
    subprocess.run(command, cwd=root, env=environment, check=True)


def check(root: Path, *, quick: bool) -> None:
    environment = dict(os.environ)
    environment.update(PYTHONDONTWRITEBYTECODE="1", LC_ALL="C", LANG="C",
                       MULTILINUX_INTEGRATION="0", MULTILINUX_EVIDENCE="0",
                       PYTHONPATH=os.pathsep.join([str(root / "src"), str(root)]))
    with tempfile.TemporaryDirectory(prefix="unifier-check-home-") as temporary:
        environment["UNIFIER_HOME"] = str(Path(temporary) / "installation")
        sources = [p for name in ("src", "tests", "tools")
                   for p in (root / name).rglob("*.py") if "__pycache__" not in p.parts]
        for path in sources:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        print(f"PASS Python syntax: {len(sources)} files", flush=True)
        run(root, environment, [sys.executable, "-c", """
import importlib
from pathlib import Path
count = 0
for path in sorted(Path('src/multilinux').glob('*.py')):
    if path.stem in {'__init__', '__main__'}:
        continue
    old = importlib.import_module('multilinux.' + path.stem)
    new = importlib.import_module('unifier.' + path.stem)
    assert old is new, path
    count += 1
print(f'PASS legacy module aliases: {count}')
"""])
        for command in ([sys.executable, "tools/unifier.py", "--help"],
                        [sys.executable, "tools/ml.py", "--help"],
                        [sys.executable, "-m", "unifier", "--help"],
                        [sys.executable, "-m", "multilinux", "--help"],
                        [str(root / "bin/unifier"), "--help"]):
            subprocess.run(command, cwd=root, env=environment, check=True,
                           stdout=subprocess.DEVNULL)
        print("PASS five CLI entry points", flush=True)
        if not quick:
            run(root, environment,
                [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true",
                        help="export and check only the Git index, without untracked/unstaged files")
    parser.add_argument("--quick", action="store_true", help="skip the full offline unittest suite")
    args = parser.parse_args(argv)
    try:
        command = ["git", "diff", "--check"]
        if args.staged:
            command.append("--cached")
        subprocess.run(command, cwd=ROOT, check=True)
        if args.staged:
            with tempfile.TemporaryDirectory(prefix="unifier-staged-check-") as temporary:
                root = Path(temporary)
                subprocess.run(["git", "checkout-index", "--all", f"--prefix={root}/"],
                               cwd=ROOT, check=True)
                print("Checking staged snapshot (no working-tree additions)", flush=True)
                check(root, quick=args.quick)
        else:
            print("Checking working tree", flush=True)
            check(ROOT, quick=args.quick)
    except (OSError, ValueError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"repository check failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print("PASS repository check", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
