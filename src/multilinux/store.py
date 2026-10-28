"""Atomic environment records under .lab/state/environments/."""

import hashlib
import json
import os
from pathlib import Path
import tempfile

from multilinux.errors import MultilinuxError


class EnvironmentStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.directory = self.root / ".lab/state/environments"

    def path(self, env_id: str) -> Path:
        return self.directory / f"{env_id}.json"

    def inventory_path(self, env_id: str) -> Path:
        return self.directory / f"{env_id}.packages.txt"

    def identity_lock_path(self, env_id: str) -> Path:
        """Per-record lock used only by explicit identity migration."""
        return self.directory / ".identity-locks" / f"{env_id}.lock"

    def identity_backup_path(self, env_id: str, source_sha256: str) -> Path:
        """A durable copy named by the exact pre-migration source bytes.

        Naming by content digest means a later, different legacy record for the
        same alias cannot overwrite or be mistaken for the first original.
        """
        if (not isinstance(source_sha256, str) or len(source_sha256) != 64
                or any(char not in "0123456789abcdef" for char in source_sha256)):
            raise MultilinuxError("identity backup digest must be 64 lowercase hex digits")
        return (self.directory / "backups"
                / f"{env_id}.pre-identity-{source_sha256}.json")

    def exists(self, env_id: str) -> bool:
        return self.path(env_id).is_file()

    def load(self, env_id: str) -> dict:
        path = self.path(env_id)
        if not path.is_file():
            raise MultilinuxError(f"environment {env_id} does not exist")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MultilinuxError(f"environment record {env_id} is unreadable: {exc}") from exc

    def all(self) -> list[dict]:
        if not self.directory.is_dir():
            return []
        records = []
        for path in sorted(self.directory.glob("*.json")):
            records.append(json.loads(path.read_text(encoding="utf-8")))
        return records

    def save(self, record: dict, inventory: str | None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        if inventory is not None:
            relative = self._write_text(self.inventory_path(record["id"]), inventory)
            record["package_inventory_path"] = relative
        self._write_json(self.path(record["id"]), record)

    def delete(self, env_id: str) -> None:
        self.path(env_id).unlink(missing_ok=True)
        self.inventory_path(env_id).unlink(missing_ok=True)

    def transaction_path(self, txn_id: str) -> Path:
        return self.root / ".lab/state/transactions" / f"{txn_id}.json"

    def save_transaction(self, record: dict) -> None:
        self._write_json(self.transaction_path(record["id"]), record)

    def load_transaction(self, txn_id: str) -> dict:
        path = self.transaction_path(txn_id)
        if not path.is_file():
            raise MultilinuxError(f"transaction {txn_id} does not exist")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MultilinuxError(f"transaction {txn_id} is unreadable: {exc}") from exc

    def launch_path(self, app_id: str) -> Path:
        return self.root / ".lab/state/launches" / f"{app_id}.json"

    def save_launch(self, record: dict) -> None:
        self._write_json(self.launch_path(record["id"]), record)

    def load_launch(self, app_id: str) -> dict:
        path = self.launch_path(app_id)
        if not path.is_file():
            raise MultilinuxError(f"application {app_id} does not exist")
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise MultilinuxError(f"application record {app_id} is unreadable: {exc}") from exc

    def delete_launch(self, app_id: str) -> None:
        self.launch_path(app_id).unlink(missing_ok=True)

    def transactions_for(self, env_id: str) -> list[dict]:
        directory = self.root / ".lab/state/transactions"
        if not directory.is_dir():
            return []
        found = []
        for path in sorted(directory.glob("*.json")):
            record = json.loads(path.read_text(encoding="utf-8"))
            if record.get("environment_id") == env_id:
                found.append(record)
        found.sort(key=lambda item: item.get("started_at") or "")
        return found

    def backup_environment(self, env_id: str) -> Path:
        """Copy the original record bytes once, before the explicit migration.

        The caller holds the per-record migration lock. An existing backup is
        left untouched: it is the original record, not a scratch file.
        """
        source = self.path(env_id)
        if not source.is_file():
            raise MultilinuxError(f"environment {env_id} does not exist")
        payload = source.read_bytes()
        digest = hashlib.sha256(payload).hexdigest()
        destination = self.identity_backup_path(env_id, digest)
        if destination.exists():
            if destination.read_bytes() != payload:
                raise MultilinuxError(
                    f"identity backup for {env_id} does not match the current record; "
                    "refusing to migrate over an earlier backup")
            return destination
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            destination.unlink(missing_ok=True)
            raise
        _fsync_directory(destination.parent)
        return destination

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def _write_text(self, path: Path, text: str) -> str:
        self._replace(path, text.encode())
        return self._relative(path)

    def _write_json(self, path: Path, value: dict) -> None:
        payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        self._replace(path, payload.encode())

    def _replace(self, path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(prefix=".ml-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
            _fsync_directory(path.parent)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise


def _fsync_directory(path: Path) -> None:
    """Persist a rename/create in its parent directory."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
