"""Stable application handlers bound to one recorded environment application.

A handler records the app instance and a fingerprint of its execution
configuration. Recreating the same app id or editing its argv/env/workdir does
not silently inherit the old handler authorization.
"""

import hashlib
import json
import re
import uuid

from multilinux.errors import MultilinuxError
from multilinux.identity import validate_binding
from multilinux.json_records import JsonRecordCollection
from multilinux.projects import INTENTS


SCHEMA_VERSION = 1
PROVIDERS = {"environment-app"}
SAVE_CAPABILITIES = {"explicit-path", "dialog-adapter", "unknown"}
FILE_MODES = {"append", "template"}
ALIAS = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
HEX64 = re.compile(r"[0-9a-f]{64}\Z")
FILE_TOKEN = "{file}"


def _canonical_uuid(value, label: str) -> str:
    if not isinstance(value, str):
        raise MultilinuxError(f"handler {label} must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise MultilinuxError(f"handler {label} is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise MultilinuxError(f"handler {label} must be the canonical lowercase UUID form")
    return value


def _validate_alias(alias) -> str | None:
    if alias is None:
        return None
    if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
        raise MultilinuxError(
            "handler alias must be 1-32 characters, start with a lowercase letter, "
            "and contain only lowercase letters, digits and hyphens")
    return alias


def _choice(value, allowed, label: str) -> str:
    if not isinstance(value, str):
        raise MultilinuxError(f"handler {label} must be a string")
    if value not in allowed:
        raise MultilinuxError(f"unknown handler {label}: {value!r}")
    return value


def execution_fingerprint(launch: dict) -> str:
    """Fingerprint the app fields that make one execution authorization."""
    payload = {
        "schema_version": launch.get("schema_version"),
        "id": launch.get("id"),
        "app_instance_uuid": launch.get("app_instance_uuid"),
        "environment_id": launch.get("environment_id"),
        "environment_binding": launch.get("environment_binding"),
        "executable": launch.get("executable"),
        "argv": launch.get("argv"),
        "env": launch.get("env") or {},
        "user": launch.get("user"),
        "workdir": launch.get("workdir"),
        "on_demand": bool(launch.get("on_demand")),
        "mounts": launch.get("mounts") or [],
        "package_origin": launch.get("package_origin"),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def validate_file_argument(value) -> dict:
    if not isinstance(value, dict):
        raise MultilinuxError("handler file_argument must be an object")
    mode = value.get("mode")
    if not isinstance(mode, str):
        raise MultilinuxError("handler file_argument mode must be a string")
    if mode not in FILE_MODES:
        raise MultilinuxError(f"unknown handler file_argument mode: {mode!r}")
    if mode == "append":
        return {"mode": "append"}
    template = value.get("argv")
    if not isinstance(template, list) or not template:
        raise MultilinuxError("handler template argv must be a non-empty list")
    if any(not isinstance(item, str) or not item for item in template):
        raise MultilinuxError("handler template argv entries must be non-empty strings")
    # First version: {file} is one complete argv element, never concatenated
    # into a flag, shell command or larger string.
    token_elements = [item for item in template if item == FILE_TOKEN]
    occurrences = sum(item.count(FILE_TOKEN) for item in template)
    if len(token_elements) != 1 or occurrences != 1:
        raise MultilinuxError(
            "handler template argv must contain one standalone {file} element")
    return {"mode": "template", "argv": list(template)}


def _file_argument(argv_template) -> dict:
    if argv_template is None:
        return {"mode": "append"}
    if not isinstance(argv_template, list) or not argv_template:
        raise MultilinuxError("--argv-template requires at least one argv entry")
    return validate_file_argument({"mode": "template", "argv": argv_template})


def validate_handler_record(record) -> dict:
    if not isinstance(record, dict):
        raise MultilinuxError("handler record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise MultilinuxError(f"handler schema_version {version!r} is not supported")
    _canonical_uuid(record.get("handler_uuid"), "handler_uuid")
    _validate_alias(record.get("alias"))
    _choice(record.get("intent"), INTENTS, "intent")
    _choice(record.get("provider"), PROVIDERS, "provider")
    if not isinstance(record.get("app_id"), str) or not record["app_id"]:
        raise MultilinuxError("handler app_id must be a non-empty string")
    _canonical_uuid(record.get("app_instance_uuid"), "app_instance_uuid")
    validate_binding(record.get("app_binding"))
    fingerprint = record.get("execution_fingerprint")
    if not isinstance(fingerprint, str) or not HEX64.fullmatch(fingerprint):
        raise MultilinuxError("handler execution_fingerprint must be 64 lowercase hex digits")
    validate_file_argument(record.get("file_argument"))
    _choice(record.get("save_capability"), SAVE_CAPABILITIES, "save_capability")
    if not isinstance(record.get("created_at"), str) or not isinstance(record.get("updated_at"), str):
        raise MultilinuxError("handler timestamps must be strings")
    return record


class HandlerRegistry:
    def __init__(self, root):
        self.collection = JsonRecordCollection(
            root, "handlers", schema_version=SCHEMA_VERSION,
            validator=validate_handler_record, unique_keys=("handler_uuid", "alias"))

    def list(self) -> list[dict]:
        return self.collection.list()

    def show(self, identifier: str) -> dict:
        for record in self.list():
            if record["handler_uuid"] == identifier or record.get("alias") == identifier:
                return dict(record)
        raise MultilinuxError(f"handler {identifier} is not registered")

    def add(self, record: dict) -> dict:
        validate_handler_record(record)
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            for existing in records:
                if existing["handler_uuid"] == record["handler_uuid"]:
                    raise MultilinuxError("handler UUID already registered")
                if record.get("alias") and existing.get("alias") == record["alias"]:
                    raise MultilinuxError(f"handler alias {record['alias']} is already registered")
            records.append(record)
            self.collection.write_unlocked(records)
        return record

    def remove(self, identifier: str) -> dict:
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            found = None
            for record in records:
                if record["handler_uuid"] == identifier or record.get("alias") == identifier:
                    found = record
                    break
            if found is None:
                raise MultilinuxError(f"handler {identifier} is not registered")
            self.collection.write_unlocked(
                [record for record in records
                 if record["handler_uuid"] != found["handler_uuid"]])
            return found


def build_handler(launch: dict, *, intent: str, save_capability: str,
                  provider: str = "environment-app", alias=None,
                  argv_template=None, timestamp: str) -> dict:
    _choice(intent, INTENTS, "intent")
    _choice(provider, PROVIDERS, "provider")
    _choice(save_capability, SAVE_CAPABILITIES, "save_capability")
    binding = launch.get("environment_binding")
    validate_binding(binding)
    app_id = launch.get("id")
    if not isinstance(app_id, str) or not app_id:
        raise MultilinuxError("application record has no id")
    instance = launch.get("app_instance_uuid")
    try:
        _canonical_uuid(instance, "app_instance_uuid")
    except MultilinuxError as exc:
        raise MultilinuxError(
            "application launch has no app instance UUID; remove and re-add the "
            "application before registering a handler") from exc
    file_argument = _file_argument(argv_template)
    if file_argument["mode"] == "template":
        executable = launch.get("executable")
        if not isinstance(executable, str) or file_argument["argv"][0] != executable:
            raise MultilinuxError(
                "handler argv template must start with the application executable")
    return {
        "schema_version": SCHEMA_VERSION,
        "handler_uuid": str(uuid.uuid4()),
        "alias": _validate_alias(alias),
        "intent": intent,
        "provider": provider,
        "app_id": app_id,
        "app_instance_uuid": instance,
        "app_binding": dict(binding),
        "execution_fingerprint": execution_fingerprint(launch),
        "file_argument": file_argument,
        "save_capability": save_capability,
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def render_argv(handler: dict, launch: dict, file_path: str) -> list[str]:
    """Build a literal argv array; no shell or PATH lookup is involved."""
    if not isinstance(file_path, str) or not file_path.startswith("/"):
        raise MultilinuxError("file argument must be an absolute host path")
    argument = validate_file_argument(handler.get("file_argument"))
    if argument["mode"] == "append":
        return [*launch["argv"], file_path]
    template = argument["argv"]
    if template[0] != launch.get("executable"):
        raise MultilinuxError("handler template no longer matches the application executable")
    rendered = list(template)
    rendered[rendered.index(FILE_TOKEN)] = file_path
    return rendered
