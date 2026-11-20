"""Durable agent request claims, execution bindings, and receipts."""

from __future__ import annotations

import os
from pathlib import Path
import time

from unifier.bridge_protocol import (
    ACTION_ID_RE, ERROR_MESSAGE_CHARS, BridgeError, copy_json,
    is_canonical_uuid, strict_dumps,
)
from unifier.errors import UnifierError
from unifier.json_records import JsonRecordCollection
from unifier.process_identity import process_stamp


SCHEMA_VERSION = 1
LEDGER_NAME = "agent-requests"
REQUEST_STATES = ("claimed", "completed", "failed", "unresolved")
MAX_RECEIPT_BYTES = 32768


def _json_safe(value, depth: int = 0) -> bool:
    if depth > 7:
        return False
    if value is None or isinstance(value, str):
        return True
    if isinstance(value, bool):
        return True
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return value == value and value not in (float("inf"), float("-inf"))
    if isinstance(value, list):
        return all(_json_safe(item, depth + 1) for item in value)
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            return False
        return all(_json_safe(item, depth + 1) for item in value.values())
    return False


def validate_request_record(record) -> dict:
    """Validate one persisted request/receipt record."""
    if not isinstance(record, dict):
        raise UnifierError("request record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise UnifierError(f"request schema_version {version!r} is not supported")
    for key, label in (("request_uuid", "request"), ("origin_session_uuid", "origin")):
        if not is_canonical_uuid(record.get(key)):
            raise UnifierError(f"request {label} must be a canonical lowercase UUID")
    action_id = record.get("action_id")
    if not isinstance(action_id, str) or ACTION_ID_RE.fullmatch(action_id) is None:
        raise UnifierError("request action_id is invalid")
    state = record.get("state")
    if state not in REQUEST_STATES:
        raise UnifierError(f"unknown request state: {state!r}")
    for key in ("claimed_at", "expires_at"):
        value = record.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise UnifierError(f"request {key} must be a number")
    if record["expires_at"] <= record["claimed_at"]:
        raise UnifierError("request expires_at must be after claimed_at")
    finished = record.get("finished_at")
    if finished is not None and (isinstance(finished, bool)
                                 or not isinstance(finished, (int, float))):
        raise UnifierError("request finished_at must be a number or null")
    receipt = record.get("receipt")
    if receipt is not None and (not isinstance(receipt, dict) or not _json_safe(receipt)):
        raise UnifierError("request receipt must be a bounded JSON object")
    error = record.get("error")
    if error is not None and (not isinstance(error, str) or not error
                              or len(error) > ERROR_MESSAGE_CHARS + 3):
        raise UnifierError("request error must be a bounded string or null")
    return record


class RequestLedger:
    """Durable claim + receipt store; the deduplication is the point.

    ``reserve`` writes the claim atomically before an action starts, so the
    same ``request_uuid`` is still refused after a crash or a restart.  A claim
    that is still open past its TTL means an execution whose end nobody can
    prove, so every later request fails closed until the host resolves the
    ledger; the same holds for an unreadable ledger.
    """

    def __init__(self, root: Path, *, name: str = LEDGER_NAME, clock=time.time):
        self.root = Path(root)
        self.collection = JsonRecordCollection(
            self.root, name, schema_version=SCHEMA_VERSION,
            validator=validate_request_record, unique_keys=("request_uuid",))
        self.clock = clock

    @property
    def path(self) -> Path:
        return self.collection.path

    def _read(self) -> list:
        try:
            return self.collection.read_unlocked()
        except UnifierError as exc:
            raise BridgeError("ledger-corrupt",
                              f"request ledger is unreadable: {exc}") from exc

    def records(self) -> list:
        with self.collection.locked(False):
            return self._read()

    def reserve(self, request_uuid: str, action_id: str, origin_session_uuid: str,
                *, ttl_seconds: int) -> dict:
        with self.collection.locked(True):
            records = self._read()
            now = float(self.clock())
            stale = sorted(item["request_uuid"] for item in records
                           if item["state"] == "claimed" and item["expires_at"] < now)
            if stale:
                raise BridgeError(
                    "expired-claim",
                    "request ledger holds an unresolved expired claim "
                    f"({', '.join(stale[:3])}); refusing to start anything else "
                    "until the host resolves it")
            if any(item["request_uuid"] == request_uuid for item in records):
                raise BridgeError("duplicate-request",
                                  f"request {request_uuid} was already received")
            claim = {
                "schema_version": SCHEMA_VERSION,
                "request_uuid": request_uuid,
                "action_id": action_id,
                "origin_session_uuid": origin_session_uuid,
                "state": "claimed",
                "claimed_at": now,
                "expires_at": now + float(ttl_seconds),
                "owner": {"pid": os.getpid(), "stamp": process_stamp(os.getpid()),
                          "boot_id": Path('/proc/sys/kernel/random/boot_id').read_text().strip()},
            }
            records.append(claim)
            try:
                validate_request_record(dict(claim))
                self.collection.write_unlocked(records)
            except (UnifierError, OSError) as exc:
                raise BridgeError("ledger-write",
                                  f"cannot reserve the request: {exc}") from exc
            return dict(claim)

    def bind_execution(self, request_uuid: str, pinned: dict) -> None:
        """Persist the exact session before execute can submit any guest work."""
        with self.collection.locked(True):
            records = self._read()
            for item in records:
                if item['request_uuid'] == request_uuid:
                    if item['state'] != 'claimed' or item.get('execution'):
                        raise BridgeError('ledger-state', 'request is not an unbound claim')
                    item['execution'] = copy_json(pinned)
                    self.collection.write_unlocked(records)
                    return
            raise BridgeError('ledger-lost', 'request disappeared before execution')

    def recover_record(self, expected: dict, proof: dict) -> dict:
        """Compare-and-set a terminal failure, preserving the original receipt."""
        with self.collection.locked(True):
            records = self._read()
            for index, item in enumerate(records):
                if item['request_uuid'] != expected['request_uuid']:
                    continue
                if item != expected:
                    raise BridgeError('ledger-changed', 'request changed during recovery')
                updated = dict(item, state='failed', finished_at=float(self.clock()),
                               recovery={'previous_state': item['state'], **proof})
                validate_request_record(updated)
                records[index] = updated
                self.collection.write_unlocked(records)
                return updated
            raise BridgeError('ledger-lost', 'request disappeared during recovery')

    def settle(self, request_uuid: str, state: str, *, receipt=None,
               error: str | None = None) -> dict:
        if state not in REQUEST_STATES or state == "claimed":
            raise BridgeError("ledger-state", f"cannot settle a request as {state!r}")
        if receipt is not None and (not isinstance(receipt, dict)
                                    or not _json_safe(receipt)):
            raise BridgeError("ledger-state", "a receipt must be a bounded JSON object")
        if receipt is not None and len(strict_dumps(receipt).encode("utf-8")) > MAX_RECEIPT_BYTES:
            raise BridgeError("oversized-receipt",
                              f"the receipt is above {MAX_RECEIPT_BYTES} bytes")
        with self.collection.locked(True):
            records = self._read()
            for index, existing in enumerate(records):
                if existing["request_uuid"] != request_uuid:
                    continue
                if existing["state"] != "claimed":
                    raise BridgeError(
                        "duplicate-request",
                        f"request {request_uuid} already settled as {existing['state']}")
                updated = dict(existing)
                updated["state"] = state
                updated["finished_at"] = float(self.clock())
                if receipt is not None:
                    updated["receipt"] = copy_json(receipt)
                if error is not None:
                    trimmed = error[:ERROR_MESSAGE_CHARS]
                    updated["error"] = trimmed
                try:
                    validate_request_record(updated)
                    records[index] = updated
                    self.collection.write_unlocked(records)
                except (UnifierError, OSError) as exc:
                    raise BridgeError("ledger-write",
                                      f"cannot record the receipt: {exc}") from exc
                return updated
            raise BridgeError("ledger-lost",
                              f"request {request_uuid} is no longer in the ledger")
