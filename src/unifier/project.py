"""Locate this checkout and load the prepared target lock."""

from pathlib import Path

from unifier.install import load_catalog


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_project():
    root = project_root()
    config, lock = load_catalog(root)
    return config, lock, root
