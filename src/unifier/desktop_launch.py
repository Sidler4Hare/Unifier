"""Desktop launcher journal records and authorization shared with sessions."""

import hashlib
from pathlib import Path
import uuid

from unifier.errors import UnifierError
from unifier.json_records import JsonRecordCollection


SCHEMA_VERSION = 1
STATES = {"planned", "installing", "installed", "revoking", "revoked", "conflict"}


def _sha(blob: bytes | None) -> str | None:
    return None if blob is None else hashlib.sha256(blob).hexdigest()


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def validate_integration_record(record) -> dict:
    if not isinstance(record, dict):
        raise UnifierError("desktop integration record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise UnifierError(f"desktop integration schema_version {version!r} is not supported")
    for key in ("integration_uuid", "project_uuid"):
        value = record.get(key)
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise UnifierError(f"desktop integration {key} is not a UUID") from exc
        if str(parsed) != value:
            raise UnifierError(f"desktop integration {key} is not canonical")
    if record.get("state") not in STATES:
        raise UnifierError(f"unknown desktop integration state: {record.get('state')!r}")
    files = record.get("files")
    if not isinstance(files, list) or not files:
        raise UnifierError("desktop integration files must be a non-empty list")
    for item in files:
        if not isinstance(item, dict):
            raise UnifierError("desktop integration file entry must be an object")
        for key in ("role", "path", "after", "after_sha256"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise UnifierError(f"desktop integration file {key} is missing")
        if item["before"] is not None and not isinstance(item["before"], str):
            raise UnifierError("desktop integration before bytes are invalid")
        if item["before_sha256"] is not None and not isinstance(item["before_sha256"], str):
            raise UnifierError("desktop integration before hash is invalid")
        if not item["path"].startswith("/"):
            raise UnifierError("desktop integration paths must be absolute")
    for key in ("created_at", "updated_at"):
        if not isinstance(record.get(key), str) or not record.get(key):
            raise UnifierError(f"desktop integration {key} must be a string")
    return record


def integration_collection(root):
    return JsonRecordCollection(
        root, "desktop-integrations", schema_version=SCHEMA_VERSION,
        validator=validate_integration_record, unique_keys=("integration_uuid",))


def show_integration(collection, identifier: str) -> dict:
    for record in collection.list():
        if record["integration_uuid"] == identifier:
            return dict(record)
    raise UnifierError(f"desktop integration {identifier} is not recorded")


def validate_launcher(collection, show_project, service, integration_uuid: str,
                      *, project_uuid=None, app_id=None, app_instance_uuid=None,
                      execution_fingerprint=None) -> dict:
    """Authorize a launcher from the journal, not from a name or env alone.

    The journal is authoritative even when the entry was installed through
    an injected HostDesktop tree. Revoked, missing or externally changed
    entries are refused, and an application launch must still name the
    exact app instance and execution configuration it was exported for.
    """
    record = show_integration(collection, integration_uuid)
    if record["state"] != "installed":
        raise UnifierError(
            f"desktop integration {integration_uuid} is {record['state']}; "
            "a cached launcher is not authorized")
    project = show_project(record["project_uuid"])
    if project_uuid is not None and record["project_uuid"] != project_uuid:
        raise UnifierError(
            "desktop entry project pin does not match its integration journal")
    if record["environment_binding"] != project["environment_binding"]:
        raise UnifierError(
            "desktop integration environment instance changed; re-export it")
    recorded_app = record.get("app_id")
    if recorded_app is None:
        if app_id is not None:
            raise UnifierError(
                "desktop integration is a project entry, not an application entry")
    else:
        if app_id != recorded_app:
            raise UnifierError(
                "desktop entry application differs from its integration journal")
        if app_instance_uuid is not None and (
                app_instance_uuid != record.get("app_instance_uuid")):
            raise UnifierError(
                "desktop entry names a different application instance; "
                "re-export the current application")
        if execution_fingerprint is not None and (
                execution_fingerprint != record.get("execution_fingerprint")):
            raise UnifierError(
                "desktop entry execution configuration changed; "
                "re-export the current application")
        launch = service.show_app(app_id)
        service._require_app_execution(
            launch, app_instance_uuid=record.get("app_instance_uuid"),
            execution_fingerprint=record.get("execution_fingerprint"))
        service._validate_launch_binding(launch)
        if launch.get("environment_binding") != record["environment_binding"]:
            raise UnifierError(
                "application environment instance differs from the desktop journal")
    entry = next(
        (item for item in record["files"] if item.get("role") == "desktop-entry"),
        None)
    if entry is None:
        raise UnifierError("desktop integration journal has no desktop entry")
    if _sha(_read(Path(entry["path"]))) != entry["after_sha256"]:
        raise UnifierError(
            f"desktop entry {entry['path']} changed after export; refusing launch")
    return record
