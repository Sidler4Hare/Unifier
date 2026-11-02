"""Installation root and catalog loading without implicit repository state.

Two different roots matter here:

* the code root is this checkout (catalog, image lock and worker entry point);
* the installation root holds user-level configuration, records, containers and
  every ``.lab`` state directory used by a process.

The low-level CLI keeps its historical behaviour (code root) unless
``UNIFIER_HOME`` is set.  The user-facing shortcut CLI always uses the
user-level installation root.  Passing the root explicitly, and exporting the
same root to every detached worker, avoids chdir or process-global tricks.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import tomllib

from unifier.errors import UnifierError


ROOT_ENVIRONMENT = "UNIFIER_HOME"
DIGEST_TEXT = r"sha256:[0-9a-f]{64}"


def code_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _absolute_root(value: str, label: str) -> Path:
    value = os.path.expanduser(value)
    path = Path(value)
    if not path.is_absolute():
        raise UnifierError(f"{label} must be an absolute path: {value}")
    if "\x00" in value or "\n" in value:
        raise UnifierError(f"{label} contains an invalid character")
    return path.resolve(strict=False)


def environment_root(environ: dict | None = None) -> Path | None:
    """Return an explicitly requested isolated installation root, if any."""
    values = os.environ if environ is None else environ
    raw = values.get(ROOT_ENVIRONMENT)
    if raw is None or raw == "":
        return None
    return _absolute_root(raw, ROOT_ENVIRONMENT)


def installation_root(environ: dict | None = None) -> Path:
    """Default user-level root for the shortcut CLI and language preference."""
    explicit = environment_root(environ)
    if explicit is not None:
        return explicit
    values = os.environ if environ is None else environ
    data_home = values.get("XDG_DATA_HOME")
    if data_home:
        base = _absolute_root(data_home, "XDG_DATA_HOME")
    else:
        base = Path.home() / ".local/share"
    return (base / "unifier").resolve(strict=False)


def state_root(environ: dict | None = None) -> Path:
    """Root used by the legacy ``env/pkg/app/...`` commands.

    Existing deployments live under the checkout.  That remains the default so
    compatibility is not silently broken.  An explicit ``UNIFIER_HOME`` moves
    records, locks and worker state together.
    """
    explicit = environment_root(environ)
    if explicit is not None:
        return explicit
    return code_root()


def ensure_root(root: Path, *, mode: int = 0o700) -> Path:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        root.chmod(mode)
    except OSError:
        pass
    return root


def worker_environment(root: Path, environ: dict | None = None) -> dict[str, str]:
    """Environment for a detached worker owned by one installation root."""
    values = dict(os.environ if environ is None else environ)
    values[ROOT_ENVIRONMENT] = str(Path(root).resolve(strict=False))
    return values


def project_token(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:8]


def load_catalog(root: Path | None = None) -> tuple[dict, dict]:
    """Load ``targets.toml`` and the immutable image lock from a code root."""
    root = Path(root or code_root())
    target_path = root / "config/targets.toml"
    lock_path = root / "config/images.lock.json"
    try:
        data = tomllib.loads(target_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UnifierError(f"cannot read target catalog {target_path}: {exc}") from exc
    if (data.get("schema_version") != 1 or data.get("platform") != "linux/amd64"
            or data.get("runtime") != "podman"):
        raise UnifierError("unsupported target configuration")
    targets = data.get("targets")
    if not isinstance(targets, dict) or set(targets) != {"debian13", "fedora44", "arch"}:
        raise UnifierError(
            "the prepared catalog must contain exactly debian13, fedora44 and arch")
    try:
        from unifier.catalog import PREPARED
    except ImportError as exc:  # pragma: no cover - import failure is fatal
        raise UnifierError(f"cannot load the prepared catalog: {exc}") from exc
    for name, expected in PREPARED.items():
        target = targets.get(name)
        if not isinstance(target, dict):
            raise UnifierError(f"target catalog has no entry for {name}")
        for key, value in expected.items():
            if target.get(key) != value:
                raise UnifierError(
                    f"{name} catalog field {key} must be {value} for the prepared experiment")
    lock = _load_lock(lock_path, data)
    return data, lock


def _load_lock(path: Path, config: dict) -> dict:
    try:
        lock = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UnifierError(f"image lock is missing or unreadable: {path}: {exc}") from exc
    if lock.get("schema_version") != 1 or lock.get("platform") != config["platform"]:
        raise UnifierError("image lock schema/platform does not match targets.toml")
    if not isinstance(lock.get("targets"), dict) or set(lock["targets"]) != set(config["targets"]):
        raise UnifierError("image lock target set does not match targets.toml")
    for name, target in config["targets"].items():
        item = lock["targets"].get(name)
        if not isinstance(item, dict):
            raise UnifierError(f"image lock has no object for {name}")
        digest = item.get("manifest_digest", "")
        expected = repository(target["source"]) + "@" + digest
        if (not isinstance(digest, str) or not re.fullmatch(DIGEST_TEXT, digest)
                or item.get("source") != target["source"]
                or item.get("locked_reference") != expected
                or item.get("os") != "linux"
                or item.get("architecture") != "amd64"):
            raise UnifierError(f"invalid or stale image lock for {name}")
    return lock


def repository(reference: str) -> str:
    return reference.rsplit(":", 1)[0]


def locked_reference(lock: dict, target_name: str) -> str:
    try:
        item = lock["targets"][target_name]
    except KeyError as exc:
        raise UnifierError(f"image lock has no entry for {target_name}") from exc
    digest = item.get("manifest_digest", "")
    reference = item.get("locked_reference", "")
    if not isinstance(digest, str) or not reference.endswith("@" + digest):
        raise UnifierError(
            f"refusing to use {target_name} without a locked image digest")
    return reference
