"""Registered host projects and their accepted toolchain/handler bindings.

A project is an explicit registration over one data root. It never gets
execution authority from a copied configuration file, and removing it never
deletes the root, package state, container or application records.
"""

import os
from pathlib import Path
import re
import uuid

from multilinux.errors import MultilinuxError
from multilinux.identity import binding_for, validate_binding
from multilinux.json_records import JsonRecordCollection


SCHEMA_VERSION = 1
INTENTS = {"edit", "view"}
ACCESS_POLICIES = {"read-only", "read-write"}
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")


def _canonical_uuid(value, label: str) -> str:
    if not isinstance(value, str):
        raise MultilinuxError(f"project {label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MultilinuxError(f"project {label} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise MultilinuxError(f"project {label} must be the canonical lowercase UUID form")
    return value


def _validate_alias(alias) -> str | None:
    if alias is None:
        return None
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
        raise MultilinuxError(
            "project alias must be 1-32 characters, start with a lowercase letter, "
            "and contain only lowercase letters, digits and hyphens")
    return alias


def _validate_intent_handlers(value) -> dict:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise MultilinuxError("project intent_handlers must be an object")
    handlers = {}
    for intent, handler_uuid in value.items():
        if intent not in INTENTS:
            raise MultilinuxError(f"unknown project intent: {intent!r}")
        handlers[intent] = _canonical_uuid(handler_uuid, "handler_uuid")
    return handlers


def _path_inside(path: str, root: str) -> bool:
    try:
        candidate = Path(path).resolve(strict=True)
        root_path = Path(root).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise MultilinuxError(f"project path is unavailable: {exc}") from exc
    return candidate.is_dir() and candidate.is_relative_to(root_path)


def validate_default_save_root(root_record: dict, path: str) -> str:
    """The default save directory must be an existing directory in the root."""
    if (not isinstance(path, str) or not path.startswith("/")
            or "\x00" in path or "\n" in path):
        raise MultilinuxError("default_save_root must be an absolute host directory")
    if os.path.normpath(path) != path:
        raise MultilinuxError("default_save_root must be a normalized absolute path")
    locator = root_record.get("locator")
    if not isinstance(locator, str):
        raise MultilinuxError("data root locator is missing")
    try:
        observed = os.stat(locator, follow_symlinks=False)
    except OSError as exc:
        raise MultilinuxError(f"data root path is unavailable: {exc}") from exc
    if (observed.st_dev, observed.st_ino) != (
            root_record.get("root_dev"), root_record.get("root_ino")):
        raise MultilinuxError("data root path was replaced")
    if not _path_inside(path, locator):
        raise MultilinuxError("default_save_root must stay inside the project root")
    return path


def validate_project_record(record) -> dict:
    if not isinstance(record, dict):
        raise MultilinuxError("project record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise MultilinuxError(f"project schema_version {version!r} is not supported")
    _canonical_uuid(record.get("project_uuid"), "project_uuid")
    _validate_alias(record.get("alias"))
    _canonical_uuid(record.get("root_uuid"), "root_uuid")
    path = record.get("default_save_root")
    if (not isinstance(path, str) or not path.startswith("/")
            or "\x00" in path or "\n" in path or os.path.normpath(path) != path):
        raise MultilinuxError("project default_save_root must be a normalized absolute path")
    policy = record.get("access_policy")
    if not isinstance(policy, str):
        raise MultilinuxError("project access_policy must be a string")
    if policy not in ACCESS_POLICIES:
        raise MultilinuxError(f"unknown project access policy: {policy!r}")
    binding = record.get("environment_binding")
    validate_binding(binding)
    toolchain = record.get("toolchain")
    if not isinstance(toolchain, dict) or toolchain.get("provider") != "environment":
        raise MultilinuxError("project toolchain must be an environment binding")
    if not isinstance(toolchain.get("target"), str) or not toolchain["target"]:
        raise MultilinuxError("project toolchain target is missing")
    _validate_intent_handlers(record.get("intent_handlers"))
    if not isinstance(record.get("created_at"), str) or not isinstance(record.get("updated_at"), str):
        raise MultilinuxError("project timestamps must be strings")
    return record


class ProjectRegistry:
    def __init__(self, root: Path):
        self.collection = JsonRecordCollection(
            Path(root), "projects", schema_version=SCHEMA_VERSION,
            validator=validate_project_record, unique_keys=("project_uuid", "alias"))

    def list(self) -> list[dict]:
        return self.collection.list()

    def show(self, identifier: str) -> dict:
        for record in self.list():
            if record["project_uuid"] == identifier or record.get("alias") == identifier:
                return dict(record)
        raise MultilinuxError(f"project {identifier} is not registered")

    def for_root(self, root_uuid: str) -> list[dict]:
        return [record for record in self.list() if record["root_uuid"] == root_uuid]

    def add(self, record: dict) -> dict:
        validate_project_record(record)
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            for existing in records:
                if existing["project_uuid"] == record["project_uuid"]:
                    raise MultilinuxError("project UUID already registered")
                if record.get("alias") and existing.get("alias") == record["alias"]:
                    raise MultilinuxError(f"project alias {record['alias']} is already registered")
                if existing["root_uuid"] == record["root_uuid"]:
                    raise MultilinuxError(
                        f"data root {record['root_uuid']} already has project "
                        f"{existing['project_uuid']}; one root has one project registration")
            records.append(record)
            self.collection.write_unlocked(records)
        return record

    def remove(self, identifier: str) -> dict:
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            found = None
            for record in records:
                if record["project_uuid"] == identifier or record.get("alias") == identifier:
                    found = record
                    break
            if found is None:
                raise MultilinuxError(f"project {identifier} is not registered")
            self.collection.write_unlocked(
                [record for record in records
                 if record["project_uuid"] != found["project_uuid"]])
            return found


def build_project(root_record: dict, environment_record: dict, *, alias=None,
                  default_save_root: str | None = None,
                  access_policy: str = "read-write",
                  intent_handlers: dict | None = None,
                  timestamp: str) -> dict:
    if not isinstance(access_policy, str):
        raise MultilinuxError("project access_policy must be a string")
    if access_policy not in ACCESS_POLICIES:
        raise MultilinuxError(f"unknown project access policy: {access_policy!r}")
    binding = binding_for(environment_record)
    target = environment_record.get("target")
    if not isinstance(target, str) or not target:
        raise MultilinuxError("environment target is missing")
    path = default_save_root if default_save_root is not None else root_record["locator"]
    validate_default_save_root(root_record, path)
    return {
        "schema_version": SCHEMA_VERSION,
        "project_uuid": str(uuid.uuid4()),
        "alias": _validate_alias(alias),
        "root_uuid": _canonical_uuid(root_record["root_uuid"], "root_uuid"),
        "default_save_root": path,
        "access_policy": access_policy,
        "environment_binding": binding,
        "toolchain": {"provider": "environment", "target": target, **binding},
        "intent_handlers": _validate_intent_handlers(intent_handlers),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
