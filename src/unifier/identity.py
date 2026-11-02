"""Versioned environment identity and conservative launch bindings.

The user-visible environment id is an alias. The durable identity is the pair
``environment_uuid`` + positive ``generation``. New records start at schema 2
and generation 1. Schema 1 records without identity stay readable and can be
migrated explicitly; nothing here writes to a record on its own.
"""

import re
import uuid

from unifier.errors import UnifierError
from unifier.share_label import effective_policy as effective_share_label_policy


CURRENT_SCHEMA_VERSION = 2
# Schema 1 records predate identity. Future schemas are refused rather than
# guessed at, because they may have a different identity meaning.
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_BINDING_FIELDS = (
    "environment_id",
    "environment_uuid",
    "generation",
    "container_id",
)


def schema_version(record: dict) -> int:
    """Return a supported record schema, refusing unknown/future versions."""
    value = record.get("schema_version", 1)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnifierError("environment schema_version must be an integer")
    if value < 1:
        raise UnifierError(f"environment schema_version {value} is invalid")
    if value > CURRENT_SCHEMA_VERSION:
        raise UnifierError(
            f"environment schema_version {value} is newer than this build "
            f"supports ({CURRENT_SCHEMA_VERSION}); refusing to migrate or modify it")
    return value


def canonical_uuid(value) -> str:
    if not isinstance(value, str):
        raise UnifierError("environment_uuid must be a string")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise UnifierError(f"environment_uuid is not a UUID: {value!r}") from exc
    if str(parsed) != value:
        raise UnifierError(
            "environment_uuid must be the canonical lowercase UUID form")
    return value


def identity(record: dict, *, required: bool = False):
    """Return ``(environment_uuid, generation)`` or None.

    A half-written identity is an error, never a reason to issue a new UUID.
    """
    has_uuid = "environment_uuid" in record
    has_generation = "generation" in record
    if not has_uuid and not has_generation:
        if required:
            raise UnifierError(
                "environment has no versioned identity; run 'env migrate' explicitly")
        return None
    missing = []
    if not has_uuid:
        missing.append("environment_uuid")
    if not has_generation:
        missing.append("generation")
    if missing:
        raise UnifierError(
            "environment identity is incomplete: missing "
            + ", ".join(missing) + "; refusing to issue a new identity")
    raw_uuid = record["environment_uuid"]
    raw_generation = record["generation"]
    if raw_uuid is None or raw_generation is None:
        nulls = [key for key in ("environment_uuid", "generation")
                 if record[key] is None]
        raise UnifierError(
            "environment identity is malformed: null value for " + ", ".join(nulls))
    environment_uuid = canonical_uuid(raw_uuid)
    if (isinstance(raw_generation, bool) or not isinstance(raw_generation, int)
            or raw_generation < 1):
        raise UnifierError("environment generation must be a positive integer")
    return environment_uuid, raw_generation


def has_identity(record: dict) -> bool:
    return identity(record) is not None


def stamp_new_identity(record: dict) -> dict:
    """Add a fresh identity to an otherwise validated new record."""
    record["environment_uuid"] = str(uuid.uuid4())
    record["generation"] = 1
    record["schema_version"] = CURRENT_SCHEMA_VERSION
    return record


def check_record_header(record: dict, env_id: str) -> None:
    """Checks shared by migration and launch binding."""
    if not isinstance(record, dict):
        raise UnifierError("environment record must be a JSON object")
    if record.get("id") != env_id:
        raise UnifierError(
            f"environment record {env_id} has mismatched id {record.get('id')!r}")
    schema_version(record)


def _require_string(record: dict, key: str, *, allow_empty: bool = False) -> str:
    value = record.get(key)
    if not isinstance(value, str) or (not allow_empty and not value):
        raise UnifierError(f"legacy environment field {key} must be a non-empty string")
    return value


def _optional_choice(record: dict, key: str, allowed: set[str], label: str) -> None:
    """Reject list/dict/None before any set membership comparison."""
    if key not in record:
        return
    value = record[key]
    if not isinstance(value, str):
        raise UnifierError(f"legacy environment {label} must be a string")
    if value not in allowed:
        raise UnifierError(f"legacy environment {label} is invalid")


def _require_int(record: dict, key: str, *, minimum: int | None = None) -> int:
    value = record.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise UnifierError(f"legacy environment field {key} must be an integer")
    if minimum is not None and value < minimum:
        raise UnifierError(f"legacy environment field {key} must be >= {minimum}")
    return value


def validate_migratable_record(record: dict, env_id: str) -> None:
    """Validate the whole legacy record before giving it a durable identity.

    A few target/id fields are not enough: a migratable record must still show
    the runtime instance, package inventory and frozen base-image fields that an
    environment record owns. Type checks keep malformed JSON from becoming a
    "successful" migration, and optional fields added by later schema-1 code
    remain optional so existing records stay compatible.
    """
    check_record_header(record, env_id)
    for key in ("target", "distribution_id", "version_track", "update_policy",
                "architecture", "writable_revision", "runtime_state",
                "package_inventory_sha256", "package_inventory_path",
                "created_at", "updated_at"):
        _require_string(record, key)
    _require_int(record, "content_revision", minimum=1)
    _require_int(record, "package_inventory_count", minimum=1)
    _require_int(record, "created_by_uid", minimum=0)
    _require_int(record, "created_by_gid", minimum=0)
    if not _SHA256.fullmatch(record["package_inventory_sha256"]):
        raise UnifierError(
            "legacy environment package_inventory_sha256 must be 64 lowercase hex digits")

    base_image = record.get("base_image")
    if not isinstance(base_image, dict):
        raise UnifierError("legacy environment base_image must be an object")
    _require_string(base_image, "source")
    digest = _require_string(base_image, "manifest_digest")
    if not digest.startswith("sha256:") or not _SHA256.fullmatch(digest[7:]):
        raise UnifierError(
            "legacy environment base_image.manifest_digest must be sha256:<64 hex>")
    reference = _require_string(base_image, "locked_reference")
    if not reference.endswith("@" + digest):
        raise UnifierError(
            "legacy environment base_image.locked_reference does not pin manifest_digest")

    runtime = record.get("runtime")
    if not isinstance(runtime, dict):
        raise UnifierError("legacy environment runtime must be an object")
    for key in ("container_id", "container_name", "project"):
        _require_string(runtime, key)
    if runtime.get("backend") != "podman":
        raise UnifierError("legacy environment runtime.backend must be podman")
    if runtime["container_id"] != record["writable_revision"]:
        raise UnifierError(
            "legacy environment writable_revision does not match runtime.container_id")
    _optional_choice(runtime, "network", {"none", "pasta"},
                     "runtime.network")
    _optional_choice(runtime, "selinux_label", {"default", "disable"},
                     "runtime.selinux_label")
    _optional_choice(runtime, "user_namespace", {"rootless-default", "keep-id"},
                     "runtime.user_namespace")
    _optional_choice(runtime, "ipc", {"private", "host"},
                     "runtime.ipc")
    if "devices" in runtime:
        devices = runtime["devices"]
        if not isinstance(devices, list) or any(not isinstance(item, str) for item in devices):
            raise UnifierError("legacy environment runtime.devices must be a list of strings")
    for key in ("rootless", "full_init"):
        if key in runtime and not isinstance(runtime[key], bool):
            raise UnifierError(f"legacy environment runtime.{key} must be a boolean")

    shares = record.get("shared_mounts")
    if not isinstance(shares, list):
        raise UnifierError("legacy environment shared_mounts must be a list")
    for share in shares:
        if not isinstance(share, dict):
            raise UnifierError("legacy environment shared_mounts entries must be objects")
        for key in ("host", "container", "options"):
            if not isinstance(share.get(key), str):
                raise UnifierError(
                    f"legacy environment shared_mounts entry {key} must be a string")
        if not share["host"].startswith("/") or not share["container"].startswith("/"):
            raise UnifierError(
                "legacy environment shared_mounts paths must be absolute")

    observed = record.get("observed")
    if not isinstance(observed, dict):
        raise UnifierError("legacy environment observed must be an object")
    for key in ("kernel", "machine", "os_release_id"):
        _require_string(observed, key)
    if observed["machine"] != "x86_64":
        raise UnifierError("legacy environment observed.machine must be x86_64")

    # This checks a persisted policy and its consistency with mount options;
    # old records without the field are inferred from their historical options.
    effective_share_label_policy(record)

    if record.get("desktop") is not None:
        desktop = record["desktop"]
        if not isinstance(desktop, dict):
            raise UnifierError("legacy environment desktop must be an object or null")
        if not isinstance(desktop.get("env"), dict):
            raise UnifierError("legacy environment desktop.env must be an object")
        if not isinstance(desktop.get("mounts"), list):
            raise UnifierError("legacy environment desktop.mounts must be a list")
    if record.get("arch") is not None and not isinstance(record["arch"], dict):
        raise UnifierError("legacy environment arch must be an object or null")
    if "scope" in record and not isinstance(record["scope"], str):
        raise UnifierError("legacy environment scope must be a string")


def binding_for(record: dict) -> dict:
    """Pin the current environment instance for a new launch record."""
    environment_uuid, generation = identity(record, required=True)
    runtime = record.get("runtime")
    if not isinstance(runtime, dict):
        raise UnifierError("environment runtime record is missing")
    container_id = runtime.get("container_id")
    if not isinstance(container_id, str) or not container_id:
        raise UnifierError("environment has no container_id; cannot bind an application")
    return {
        "environment_id": record["id"],
        "environment_uuid": environment_uuid,
        "generation": generation,
        "container_id": container_id,
    }


def validate_binding(binding) -> dict:
    """Validate the shape of a stored launch binding without consulting a record."""
    if not isinstance(binding, dict):
        raise UnifierError("launch record has no versioned environment binding")
    missing = [key for key in _BINDING_FIELDS if binding.get(key) in (None, "")]
    if missing:
        raise UnifierError(
            "launch environment binding is incomplete: " + ", ".join(missing))
    if not isinstance(binding["environment_id"], str):
        raise UnifierError("launch binding environment_id must be a string")
    canonical_uuid(binding["environment_uuid"])
    generation = binding["generation"]
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
        raise UnifierError("launch binding generation must be a positive integer")
    if not isinstance(binding["container_id"], str):
        raise UnifierError("launch binding container_id must be a string")
    return binding
