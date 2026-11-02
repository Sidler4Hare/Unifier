#!/usr/bin/env python3
"""Entry point for the environment lifecycle CLI.

Usage from the project root: python3 tools/ml.py env --help
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
