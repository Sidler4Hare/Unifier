"""Construct the default service for installed and explicitly selected roots."""

from pathlib import Path


def service_for_root(root: str | Path | None = None):
    from unifier.install import load_catalog, state_root
    from unifier.runtime import PodmanRuntime
    from unifier.service import EnvironmentService

    config, lock = load_catalog()
    return EnvironmentService(state_root() if root is None else Path(root),
                              PodmanRuntime(), config, lock)
