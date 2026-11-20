#!/usr/bin/env python3
"""One offline repository check, optionally against the exact staged snapshot."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.repo_contract import load_contract, verify_entrypoints, verify_import_directions
from tools.test_runtime import offline_environment, short_temporary_directory


def verify_bundle(root: Path) -> None:
    """A standalone source bundle has no Git index; verify its file manifest."""
    manifest = json.loads((root / "BUNDLE-MANIFEST.json").read_text(encoding="utf-8"))
    if 'source_tree_sha256' in manifest:
        content = json.dumps(manifest['files'], sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
        if hashlib.sha256(content).hexdigest() != manifest['source_tree_sha256']:
            raise ValueError('bundle source tree digest differs')
    for name, record in manifest["files"].items():
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"invalid bundle path: {name}")
        path = root / relative
        if record["kind"] == "symlink":
            if not path.is_symlink() or os.readlink(path) != record["target"]:
                raise ValueError(f"bundle symlink differs: {name}")
        elif record["kind"] == "file":
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() != record["sha256"]:
                raise ValueError(f"bundle checksum differs: {name}")
        else:
            raise ValueError(f"unknown bundle entry kind: {name}")
        if 'mode' in record and path.lstat().st_mode & 0o777 != record['mode']:
            raise ValueError(f"bundle mode differs: {name}")
    print(f"PASS bundle manifest: {len(manifest['files'])} entries", flush=True)


def run(root: Path, environment: dict[str, str], command: list[str]) -> None:
    subprocess.run(command, cwd=root, env=environment, check=True)


def check(root: Path, *, quick: bool) -> None:
    with short_temporary_directory() as temporary:
        private = Path(temporary)
        environment = offline_environment(root, private, private / 'tmp')
        sources = [p for name in ("src", "tests", "tools")
                   for p in (root / name).rglob("*.py") if "__pycache__" not in p.parts]
        product = root / "src/unifier"
        trees = {}
        for path in sources:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            if path.is_relative_to(product):
                name = ".".join(path.relative_to(product.parent).with_suffix("").parts)
                trees[name] = tree
        print(f"PASS Python syntax: {len(sources)} files", flush=True)
        contract = load_contract(root)
        verify_entrypoints(root, contract)
        verify_import_directions(trees, contract)
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
        if (ROOT / ".git").exists():
            command = ["git", "diff", "--check"]
            if args.staged:
                command.append("--cached")
            subprocess.run(command, cwd=ROOT, check=True)
        else:
            if args.staged:
                raise ValueError("--staged requires a Git checkout")
            verify_bundle(ROOT)
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
    except (OSError, ValueError, KeyError, ImportError, SyntaxError, subprocess.CalledProcessError) as exc:
        print(f"repository check failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    print("PASS repository check", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
