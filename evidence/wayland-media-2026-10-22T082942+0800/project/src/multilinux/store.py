"""Atomic environment records under .lab/state/environments/."""

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
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
