"""Small atomic JSON collection shared by B-stage registries.

Each collection has one JSON document and one flock. Writes use a temporary
file, fsync and atomic rename, then fsync the directory. The collection only
stores and validates records; callers keep policy in their own modules.
"""

from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path

from unifier.atomic_io import atomic_replace, fsync_directory
from unifier.errors import UnifierError


class JsonRecordCollection:
    def __init__(self, root: Path, name: str, *, schema_version: int, validator,
                 unique_keys=()):
        self.root = Path(root)
        self.directory = self.root / ".lab/state" / name
        self.path = self.directory / "registry.json"
        self.lock_path = self.directory / "registry.lock"
        self.schema_version = schema_version
        self.validator = validator
        self.unique_keys = tuple(unique_keys)

    @contextmanager
    def locked(self, exclusive: bool):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    def read_unlocked(self) -> list[dict]:
        if not self.path.is_file():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UnifierError(f"{self.directory.name} registry is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise UnifierError(f"{self.directory.name} registry must be a JSON object")
        version = payload.get("schema_version")
        if (isinstance(version, bool) or not isinstance(version, int)
                or version != self.schema_version):
            raise UnifierError(
                f"{self.directory.name} registry schema_version {version!r} is not supported")
        records = payload.get("records")
        if not isinstance(records, list):
            raise UnifierError(f"{self.directory.name} registry records must be a list")
        validated = [self.validator(record) for record in records]
        for key in self.unique_keys:
            seen = set()
            for record in validated:
                value = record.get(key)
                if value is None:
                    continue
                if value in seen:
                    raise UnifierError(
                        f"{self.directory.name} registry contains duplicate {key}: {value!r}")
                seen.add(value)
        return validated

    def write_unlocked(self, records: list[dict]) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = json.dumps(
            {"schema_version": self.schema_version, "records": records},
            ensure_ascii=False, indent=2, sort_keys=True).encode() + b"\n"
        atomic_replace(self.path, payload, prefix=f".ml-{self.directory.name}-")

    def list(self) -> list[dict]:
        if not self.path.is_file():
            return []
        with self.locked(False):
            return self.read_unlocked()

    def find(self, identifier: str, *, key: str) -> dict:
        if not isinstance(identifier, str) or not identifier:
            raise UnifierError(f"{self.directory.name} identifier must be a non-empty string")
        for record in self.list():
            if record.get(key) == identifier:
                return dict(record)
        raise UnifierError(f"{self.directory.name} record {identifier} is not registered")
