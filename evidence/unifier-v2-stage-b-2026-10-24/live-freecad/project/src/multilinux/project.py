"""Locate this checkout and load the prepared target lock."""

import sys
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_project():
    root = project_root()
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    from tools import lab

    config = lab.targets()
    lock = lab.read_lock(config)
    return config, lock, root
