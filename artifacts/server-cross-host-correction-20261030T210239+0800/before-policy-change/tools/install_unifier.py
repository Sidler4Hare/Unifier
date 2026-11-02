#!/usr/bin/env python3
"""Install the user-level ``unifier`` launcher without overwriting unknowns.

The script only creates a symlink in a user bin directory.  It never modifies
the checkout, global dsh configuration, shell profiles, containers or any
system package.  If the destination exists and is not our launcher, the script
refuses and leaves it untouched.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = ROOT / "bin/unifier"
MARKER = "Unifier launcher"


def _is_our_launcher(path: Path) -> bool:
    if path.is_symlink():
        try:
            return path.resolve(strict=True) == LAUNCHER.resolve(strict=True)
        except OSError:
            return False
    if path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False
        return MARKER in text and str(LAUNCHER) in text
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install the user-level unifier launcher (symlink only).")
    parser.add_argument("--bin-dir", default=str(Path.home() / ".local/bin"),
                        help="destination directory; default ~/.local/bin")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print("unifier requires Python 3.11 or newer", file=sys.stderr)
        return 1
    if not LAUNCHER.is_file():
        print(f"launcher source is missing: {LAUNCHER}", file=sys.stderr)
        return 1
    bin_dir = Path(args.bin_dir).expanduser()
    destination = bin_dir / "unifier"
    if destination.exists() or destination.is_symlink():
        if not _is_our_launcher(destination):
            print(f"refusing to overwrite an unknown file: {destination}", file=sys.stderr)
            print("remove or rename it explicitly, then rerun this installer",
                  file=sys.stderr)
            return 1
        if args.dry_run:
            print(f"would keep existing unifier launcher: {destination}")
            return 0
        print(f"unifier launcher already installed: {destination}")
    if args.dry_run:
        print(f"would create symlink: {destination} -> {LAUNCHER}")
        return 0
    bin_dir.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".unifier-", dir=bin_dir)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        temporary.unlink()
        os.symlink(str(LAUNCHER), temporary)
        os.replace(temporary, destination)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        print(f"cannot install launcher: {exc}", file=sys.stderr)
        return 1
    print(f"installed {destination} -> {LAUNCHER}")
    if str(bin_dir) not in os.environ.get("PATH", "").split(os.pathsep):
        print(f"note: add {bin_dir} to PATH to run 'unifier'", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
