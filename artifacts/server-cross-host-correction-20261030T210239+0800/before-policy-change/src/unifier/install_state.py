"""Short, recoverable per-installation target record and its lock.

Only the shortcut layer writes this file.  It records the selected set, the
unique environment id per target and the last failure phase.  The environment
records themselves remain authoritative for identity and container state;
this file never claims "ready" before the service returned a record and the
runtime confirmed the container.

Writes replace the file atomically and are serialized by one installation-wide
flock.  A failed target keeps its last error and is retried by the next init.
"""

from __future__ import annotations

from contextlib import contextmanager
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import tempfile

from unifier.errors import UnifierError


SCHEMA_VERSION = 1
STATE_NAME = "install-state.json"
LOCK_NAME = ".lab/install.lock"
TARGET_STATES = {"not_configured", "pending", "ready", "failed", "adopted"}


def now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class InstallState:
    def __init__(self, root: Path, targets, *, clock=None):
        self.root = Path(root)
        self.target_names = list(targets)
        self.path = self.root / STATE_NAME
        self.clock = clock or now

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = self.root / LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _empty(self) -> dict:
        return {
            "schema_version": SCHEMA_VERSION,
            "initialized": False,
            "updated_at": None,
            "enabled": [],
            "targets": {
                name: {
                    "selected": False,
                    "state": "not_configured",
                    "environment_id": name,
                    "container_retained": False,
                    "files_mounted": False,
                    "source": "new",
                    "failure": None,
                }
                for name in self.target_names
            },
        }

    def load(self) -> dict:
        if not self.path.is_file():
            return self._empty()
        try:
            record = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnifierError(
                f"installation record is corrupt: {self.path}: {exc}") from exc
        if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
            raise UnifierError(
                f"installation record has an unsupported schema: {self.path}")
        targets = record.get("targets")
        if not isinstance(targets, dict):
            raise UnifierError(f"installation record has no targets object: {self.path}")
        base = self._empty()
        for name in self.target_names:
            item = targets.get(name)
            if isinstance(item, dict):
                merged = base["targets"][name]
                merged.update(item)
                if merged.get("state") not in TARGET_STATES:
                    merged["state"] = "failed"
                    merged["failure"] = {
                        "phase": "record",
                        "detail": "invalid state in installation record",
                    }
                base["targets"][name] = merged
        base["initialized"] = bool(record.get("initialized"))
        base["updated_at"] = record.get("updated_at")
        enabled = record.get("enabled")
        base["enabled"] = [name for name in enabled if name in self.target_names] \
            if isinstance(enabled, list) else []
        for name, item in base["targets"].items():
            item["selected"] = bool(item.get("selected"))
        return base

    def save(self, record: dict) -> None:
        record = dict(record)
        record["schema_version"] = SCHEMA_VERSION
        record["updated_at"] = self.clock()
        record["enabled"] = [name for name in self.target_names
                             if (record.get("targets", {}).get(name) or {}).get("selected")]
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".unifier-state-", dir=self.root)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            temporary.replace(self.path)
            _fsync_directory(self.root)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def set_selection(self, record: dict, selected: list[str]) -> dict:
        selected_set = set(selected)
        for name in self.target_names:
            item = record["targets"].setdefault(name, {})
            item["selected"] = name in selected_set
            if not item["selected"] and item.get("state") in {"pending", "failed"}:
                # A failed/partial target that the user unchecked is retained,
                # not silently deleted, but no longer counts as enabled.
                item["state"] = "failed" if item.get("failure") else "not_configured"
        record["initialized"] = True
        record["enabled"] = [name for name in self.target_names if name in selected_set]
        return record

    def mark(self, record: dict, name: str, **values) -> dict:
        item = record["targets"].setdefault(name, {})
        for key, value in values.items():
            item[key] = value
        item["updated_at"] = self.clock()
        return record


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
