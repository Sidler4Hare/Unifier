"""Reversible user-level desktop entries and file associations.

Every integration is journaled before anything is published.  The journal keeps
the exact before/after bytes and hashes for each file, so revoke restores the
original association only when our installed state is still untouched.  A
later external edit is a conflict, never an instruction to overwrite the user's
choice or delete a replaced desktop file.
"""

from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
import sys
import tempfile
import uuid

from multilinux.desktop import (
    HostDesktop,
    add_mime_association,
    desktop_id,
    render_desktop_entry,
    render_mime_xml,
    validate_app_id,
)
from multilinux.errors import MultilinuxError
from multilinux.handlers import execution_fingerprint
from multilinux.json_records import JsonRecordCollection, fsync_directory
from multilinux.project_sessions import ProjectSessions


SCHEMA_VERSION = 1
STATES = {"planned", "installing", "installed", "revoking", "revoked", "conflict"}


def _sha(blob: bytes | None) -> str | None:
    return None if blob is None else hashlib.sha256(blob).hexdigest()


def _b64(blob: bytes | None) -> str | None:
    return None if blob is None else base64.b64encode(blob).decode("ascii")


def _unb64(value: str | None) -> bytes | None:
    return None if value is None else base64.b64decode(value.encode("ascii"), validate=True)


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".ml-desktop-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def _remove(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    fsync_directory(path.parent)


def validate_integration_record(record) -> dict:
    if not isinstance(record, dict):
        raise MultilinuxError("desktop integration record must be a JSON object")
    version = record.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version != SCHEMA_VERSION:
        raise MultilinuxError(f"desktop integration schema_version {version!r} is not supported")
    for key in ("integration_uuid", "project_uuid"):
        value = record.get(key)
        try:
            parsed = uuid.UUID(value)
        except (ValueError, AttributeError, TypeError) as exc:
            raise MultilinuxError(f"desktop integration {key} is not a UUID") from exc
        if str(parsed) != value:
            raise MultilinuxError(f"desktop integration {key} is not canonical")
    if record.get("state") not in STATES:
        raise MultilinuxError(f"unknown desktop integration state: {record.get('state')!r}")
    files = record.get("files")
    if not isinstance(files, list) or not files:
        raise MultilinuxError("desktop integration files must be a non-empty list")
    for item in files:
        if not isinstance(item, dict):
            raise MultilinuxError("desktop integration file entry must be an object")
        for key in ("role", "path", "after", "after_sha256"):
            if not isinstance(item.get(key), str) or not item[key]:
                raise MultilinuxError(f"desktop integration file {key} is missing")
        if item["before"] is not None and not isinstance(item["before"], str):
            raise MultilinuxError("desktop integration before bytes are invalid")
        if item["before_sha256"] is not None and not isinstance(item["before_sha256"], str):
            raise MultilinuxError("desktop integration before hash is invalid")
        if not item["path"].startswith("/"):
            raise MultilinuxError("desktop integration paths must be absolute")
    for key in ("created_at", "updated_at"):
        if not isinstance(record.get(key), str) or not record.get(key):
            raise MultilinuxError(f"desktop integration {key} must be a string")
    return record


class DesktopIntegrationManager:
    def __init__(self, root: Path, service, host_desktop: HostDesktop | None = None):
        self.root = Path(root)
        self.service = service
        self.host = host_desktop or HostDesktop()
        self.sessions = ProjectSessions(self.root, service)
        self.collection = JsonRecordCollection(
            self.root, "desktop-integrations", schema_version=SCHEMA_VERSION,
            validator=validate_integration_record,
            unique_keys=("integration_uuid",))

    def _clock(self) -> str:
        return self.service.clock()

    def _records(self) -> list[dict]:
        return self.collection.list()

    def _save(self, record: dict) -> dict:
        validate_integration_record(record)
        with self.collection.locked(True):
            records = self.collection.read_unlocked()
            for index, existing in enumerate(records):
                if existing["integration_uuid"] == record["integration_uuid"]:
                    records[index] = dict(record)
                    break
            else:
                records.append(dict(record))
            self.collection.write_unlocked(records)
        return dict(record)

    def show(self, identifier: str) -> dict:
        for record in self._records():
            if record["integration_uuid"] == identifier:
                return dict(record)
        raise MultilinuxError(f"desktop integration {identifier} is not recorded")

    def list(self) -> list[dict]:
        return self._records()

    def validate_launcher(self, integration_uuid: str, *, project_uuid=None,
                          app_id=None, app_instance_uuid=None,
                          execution_fingerprint=None) -> dict:
        """Authorize a launcher from the journal, not from a name or env alone.

        The journal is authoritative even when the entry was installed through
        an injected HostDesktop tree. Revoked, missing or externally changed
        entries are refused, and an application launch must still name the
        exact app instance and execution configuration it was exported for.
        """
        record = self.show(integration_uuid)
        if record["state"] != "installed":
            raise MultilinuxError(
                f"desktop integration {integration_uuid} is {record['state']}; "
                "a cached launcher is not authorized")
        project = self.sessions.projects.show(record["project_uuid"])
        if project_uuid is not None and record["project_uuid"] != project_uuid:
            raise MultilinuxError(
                "desktop entry project pin does not match its integration journal")
        if record["environment_binding"] != project["environment_binding"]:
            raise MultilinuxError(
                "desktop integration environment instance changed; re-export it")
        recorded_app = record.get("app_id")
        if recorded_app is None:
            if app_id is not None:
                raise MultilinuxError(
                    "desktop integration is a project entry, not an application entry")
        else:
            if app_id != recorded_app:
                raise MultilinuxError(
                    "desktop entry application differs from its integration journal")
            if app_instance_uuid is not None and (
                    app_instance_uuid != record.get("app_instance_uuid")):
                raise MultilinuxError(
                    "desktop entry names a different application instance; "
                    "re-export the current application")
            if execution_fingerprint is not None and (
                    execution_fingerprint != record.get("execution_fingerprint")):
                raise MultilinuxError(
                    "desktop entry execution configuration changed; "
                    "re-export the current application")
            launch = self.service.show_app(app_id)
            self.service._require_app_execution(
                launch, app_instance_uuid=record.get("app_instance_uuid"),
                execution_fingerprint=record.get("execution_fingerprint"))
            self.service._validate_launch_binding(launch)
            if launch.get("environment_binding") != record["environment_binding"]:
                raise MultilinuxError(
                    "application environment instance differs from the desktop journal")
        entry = next(
            (item for item in record["files"] if item.get("role") == "desktop-entry"),
            None)
        if entry is None:
            raise MultilinuxError("desktop integration journal has no desktop entry")
        if _sha(_read(Path(entry["path"]))) != entry["after_sha256"]:
            raise MultilinuxError(
                f"desktop entry {entry['path']} changed after export; refusing launch")
        return record

    def _project_launcher(self, project_uuid: str,
                          integration_uuid: str) -> list[str]:
        entry = Path(__file__).resolve().parents[2] / "tools/ml.py"
        return [sys.executable, str(entry), "project", "shell", project_uuid,
                "--integration", integration_uuid]

    def _app_launcher(self, project_uuid: str, app_id: str, *,
                      integration_uuid: str, app_instance_uuid: str,
                      execution_sha256: str) -> list[str]:
        entry = Path(__file__).resolve().parents[2] / "tools/ml.py"
        return [sys.executable, str(entry), "app", "open", app_id,
                "--project", project_uuid,
                "--integration", integration_uuid,
                "--app-instance", app_instance_uuid,
                "--execution-fingerprint", execution_sha256, "--"]

    def _file_plan(self, path: Path, role: str, after: bytes | None) -> dict:
        before = _read(path)
        return {
            "role": role,
            "path": str(path),
            "before": _b64(before),
            "before_sha256": _sha(before),
            "after": _b64(after),
            "after_sha256": _sha(after),
        }

    def install(self, project_identifier: str, *, session_uuid=None, app_id=None,
                name: str | None = None, icon: str | None = None,
                wm_class: str | None = None, mime_type: str | None = None,
                extension: str | None = None, desktop_alias: str | None = None,
                menu_only: bool = False, terminal: bool = False) -> dict:
        project = self.sessions.projects.show(project_identifier)
        project_uuid = project["project_uuid"]
        launch = None
        if app_id is not None:
            launch = self.service.show_app(app_id)
            self.sessions.validate_app_project(project_uuid, app_id)
            if launch["environment_id"] != project["environment_binding"]["environment_id"]:
                raise MultilinuxError("application belongs to another project environment")
        else:
            self.sessions.prepare(project_uuid, cwd=project.get("default_save_root"),
                                  argv=[], env_keys=[])
            if mime_type and not menu_only:
                raise MultilinuxError(
                    "a file association needs an existing application record; "
                    "export the project terminal entry with --menu-only")
        if session_uuid is not None:
            session = self.sessions.store.read(session_uuid)
            if session["project_uuid"] != project_uuid:
                raise MultilinuxError("session does not belong to this project")
            if session["state"] in {"retired", "failed"}:
                raise MultilinuxError(f"session is {session['state']}; refusing to export it")
            if session["environment_binding"] != project["environment_binding"]:
                raise MultilinuxError(
                    "session environment instance differs from the project; re-plan it")
            if session["root_uuid"] != project["root_uuid"]:
                raise MultilinuxError("session root differs from the project")
        if mime_type and menu_only:
            raise MultilinuxError("--menu-only cannot be combined with a MIME association")
        if mime_type and extension is None:
            raise MultilinuxError("a MIME association requires --extension")
        if extension and not mime_type:
            raise MultilinuxError("--extension requires --mime-type")

        if desktop_alias is not None:
            desktop_name = validate_app_id(desktop_alias)
        elif app_id is not None:
            desktop_name = desktop_id(self.root, app_id)
        else:
            desktop_name = desktop_id(self.root, "project-" + project_uuid[:8])
        desktop_path = self.host.applications / (desktop_name + ".desktop")
        options = {
            "display_name": name,
            "icon": icon,
            "wm_class": wm_class,
            "extension": extension,
            "terminal": bool(terminal),
            "menu_only": bool(menu_only),
        }

        # Repeated export of the same installed target is idempotent. A change
        # to name/icon/terminal/extension is not silently ignored: the previous
        # journal must be revoked before it can be replaced honestly.
        for existing in self._records():
            if existing.get("desktop_path") == str(desktop_path):
                if (existing["state"] in {"installed", "installing", "planned"}
                        and existing["project_uuid"] == project_uuid
                        and existing.get("app_id") == app_id
                        and existing.get("session_uuid") == session_uuid
                        and existing.get("mime_type") == mime_type):
                    if existing.get("options") not in (None, options):
                        raise MultilinuxError(
                            f"desktop path {desktop_path} is already installed with "
                            "different options; revoke that integration first")
                    return dict(existing)
                if existing["state"] != "revoked":
                    raise MultilinuxError(
                        f"desktop path {desktop_path} already belongs to integration "
                        f"{existing['integration_uuid']} ({existing['state']})")

        foreign_existing = (
            desktop_path.exists()
            and not any(record.get("desktop_path") == str(desktop_path)
                        and record["state"] != "revoked"
                        for record in self._records()))
        integration_uuid = str(uuid.uuid4())
        app_instance_uuid = None
        execution_sha256 = None
        if app_id is not None:
            app_instance_uuid = launch.get("app_instance_uuid")
            if not isinstance(app_instance_uuid, str) or not app_instance_uuid:
                raise MultilinuxError(
                    "application has no app_instance_uuid; re-register it before export")
            execution_sha256 = execution_fingerprint(launch)
            launcher = self._app_launcher(
                project_uuid, app_id, integration_uuid=integration_uuid,
                app_instance_uuid=app_instance_uuid,
                execution_sha256=execution_sha256)
        else:
            launcher = self._project_launcher(project_uuid, integration_uuid)
        extra_fields = {}
        if app_instance_uuid is not None:
            extra_fields["X-Multilinux-App-Instance"] = app_instance_uuid
        if execution_sha256 is not None:
            extra_fields["X-Multilinux-Execution-Fingerprint"] = execution_sha256
        entry_text = render_desktop_entry(
            launcher=launcher, app_id=app_id or ("project-" + project_uuid[:8]),
            environment_id=project["environment_binding"]["environment_id"],
            project=project_uuid, mime_type=mime_type,
            name=name or project.get("alias") or (
                launch.get("id") if launch else project_uuid),
            icon=icon, wm_class=wm_class, terminal=terminal,
            integration_uuid=integration_uuid, session_uuid=session_uuid,
            extra_fields=extra_fields)
        files = [self._file_plan(desktop_path, "desktop-entry",
                                 entry_text.encode("utf-8"))]
        if mime_type:
            mimeapps_before = _read(self.host.mimeapps)
            try:
                before_text = (None if mimeapps_before is None
                               else mimeapps_before.decode("utf-8"))
            except UnicodeDecodeError as exc:
                raise MultilinuxError(f"mimeapps.list is not UTF-8: {exc}") from exc
            after_text = add_mime_association(
                before_text, mime_type, desktop_path.name)
            files.append(self._file_plan(
                self.host.mimeapps, "mime-default",
                after_text.encode("utf-8")))
            mime_path = self.host.mime_packages / (integration_uuid + ".xml")
            files.append(self._file_plan(
                mime_path, "mime-package",
                render_mime_xml(mime_type, extension).encode("utf-8")))
        stamp = self._clock()
        record = {
            "schema_version": SCHEMA_VERSION,
            "integration_uuid": integration_uuid,
            "project_uuid": project_uuid,
            "session_uuid": session_uuid,
            "app_id": app_id,
            "app_instance_uuid": app_instance_uuid,
            "execution_fingerprint": execution_sha256,
            "environment_id": project["environment_binding"]["environment_id"],
            "environment_binding": dict(project["environment_binding"]),
            "desktop_id": desktop_name,
            "desktop_path": str(desktop_path),
            "mime_type": mime_type,
            "extension": extension,
            "terminal": bool(terminal),
            "options": options,
            "state": "planned",
            "files": files,
            "conflict": None,
            "created_at": stamp,
            "updated_at": stamp,
        }
        record = self._save(record)
        if foreign_existing:
            self._save({
                **record, "state": "conflict",
                "conflict": {"path": str(desktop_path),
                             "reason": "an externally owned desktop file already exists; "
                                       "refusing to replace it"},
                "updated_at": self._clock()})
            raise MultilinuxError(
                f"refusing to replace existing desktop file {desktop_path}")
        try:
            self._publish(record)
        except MultilinuxError:
            raise
        return self.show(integration_uuid)

    def _conflict(self, record: dict, path: Path, reason: str) -> dict:
        conflict = {"path": str(path), "reason": reason,
                    "observed_sha256": _sha(_read(path)),
                    "updated_at": self._clock()}
        return self._save({**record, "state": "conflict", "conflict": conflict,
                           "updated_at": self._clock()})

    def _preflight(self, record: dict) -> list[dict]:
        actions = []
        for item in record["files"]:
            path = Path(item["path"])
            current = _read(path)
            current_sha = _sha(current)
            if current_sha == item["after_sha256"]:
                actions.append({"item": item, "state": "after"})
            elif current_sha == item["before_sha256"]:
                actions.append({"item": item, "state": "before"})
            else:
                actions.append({"item": item, "state": "conflict",
                                "observed": current_sha})
        return actions

    def _publish(self, record: dict) -> dict:
        actions = self._preflight(record)
        for action in actions:
            if action["state"] != "before":
                path = Path(action["item"]["path"])
                self._conflict(
                    record, path,
                    "file changed after the integration journal was written; "
                    "refusing to replace it")
                raise MultilinuxError(
                    f"desktop integration conflict at {path}; evidence was journaled")
        record = self._save({**record, "state": "installing",
                             "updated_at": self._clock()})
        try:
            for item in record["files"]:
                _atomic_write(Path(item["path"]), _unb64(item["after"]))
        except BaseException as exc:
            self._rollback(record)
            self._save({**record, "state": "conflict",
                        "conflict": {"path": None, "reason": f"publish failed: {exc}"},
                        "updated_at": self._clock()})
            raise
        return self._save({**record, "state": "installed", "conflict": None,
                           "installed_at": self._clock(), "updated_at": self._clock()})

    def _rollback(self, record: dict) -> None:
        for item in reversed(record["files"]):
            path = Path(item["path"])
            current = _sha(_read(path))
            if current != item["after_sha256"]:
                continue
            before = _unb64(item["before"])
            try:
                if before is None:
                    _remove(path)
                else:
                    _atomic_write(path, before)
            except OSError:
                pass

    def revoke(self, identifier: str) -> dict:
        for record in self._records():
            if record["integration_uuid"] == identifier:
                return self._revoke_one(record)
        project = self.sessions.projects.show(identifier)
        matches = [record for record in self._records()
                   if record["project_uuid"] == project["project_uuid"]
                   and record["state"] in {"installed", "planned", "installing", "conflict"}]
        if not matches:
            raise MultilinuxError(
                f"project {project['project_uuid']} has no active desktop integration")
        return {"project_uuid": project["project_uuid"],
                "integrations": [self._revoke_one(record) for record in matches]}

    def _revoke_one(self, record: dict) -> dict:
        record = self.show(record["integration_uuid"])
        if record["state"] == "revoked":
            return record
        if record["state"] == "conflict":
            # A conflict journal may describe a foreign file that we never
            # owned. Do not turn that evidence into a clean revoke.
            raise MultilinuxError(
                f"desktop integration {record['integration_uuid']} is in conflict; "
                "recover or resolve it before revoking")
        actions = self._preflight(record)
        for action in actions:
            if action["state"] == "conflict":
                path = Path(action["item"]["path"])
                self._conflict(
                    record, path,
                    "installed bytes no longer match the journal; preserving the "
                    "external change and refusing to restore")
                raise MultilinuxError(
                    f"desktop integration {record['integration_uuid']} needs reconciliation "
                    f"at {path}")
            if action["state"] == "before" and record["state"] in {
                    "installed", "installing", "revoking"}:
                path = Path(action["item"]["path"])
                self._conflict(
                    record, path,
                    "an installed file was deleted or reverted externally; "
                    "refusing to record this as our own clean revoke")
                raise MultilinuxError(
                    f"desktop integration {record['integration_uuid']} needs reconciliation "
                    f"at {path}")
        # Persist the intent before touching the user's files. A crash mid-way
        # is then a mixed state for recover() to report honestly.
        record = self._save({**record, "state": "revoking",
                             "updated_at": self._clock()})
        try:
            for action in reversed(actions):
                item = action["item"]
                if action["state"] == "before":
                    continue
                before = _unb64(item["before"])
                path = Path(item["path"])
                if before is None:
                    _remove(path)
                else:
                    _atomic_write(path, before)
        except BaseException as exc:
            self._save({
                **record, "state": "conflict",
                "conflict": {"path": None, "reason": f"revoke failed: {exc}"},
                "updated_at": self._clock()})
            raise
        return self._save({**record, "state": "revoked", "conflict": None,
                           "revoked_at": self._clock(), "updated_at": self._clock()})

    def recover(self) -> list[dict]:
        results = []
        for record in self._records():
            actions = self._preflight(record)
            states = [action["state"] for action in actions]
            current = record["state"]

            # A durable revoke intent is never rolled back into an install.
            # Finish only the files that still hold our after bytes; known
            # before bytes are already restored and unknown bytes are a
            # conflict that must not be overwritten.
            if current == "revoking":
                if "conflict" in states:
                    first = next(action for action in actions
                                 if action["state"] == "conflict")
                    record = self._conflict(
                        record, Path(first["item"]["path"]),
                        "external change detected while finishing a revoke")
                    results.append({"integration_uuid": record["integration_uuid"],
                                    "action": "conflict"})
                    continue
                try:
                    for action in reversed(actions):
                        if action["state"] != "after":
                            continue
                        item = action["item"]
                        before = _unb64(item["before"])
                        path = Path(item["path"])
                        if before is None:
                            _remove(path)
                        else:
                            _atomic_write(path, before)
                except BaseException as exc:
                    record = self._save({
                        **record, "state": "conflict",
                        "conflict": {"path": None,
                                     "reason": f"recovery revoke failed: {exc}"},
                        "updated_at": self._clock()})
                    results.append({"integration_uuid": record["integration_uuid"],
                                    "action": "conflict"})
                    continue
                record = self._save({
                    **record, "state": "revoked", "conflict": None,
                    "revoked_at": self._clock(), "updated_at": self._clock()})
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "revoked"})
                continue

            # Conflict evidence is sticky: a later file coincidence cannot
            # prove that the user's choice was ours to replace or revoke.
            if current == "conflict":
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "conflict"})
                continue

            # A recorded revoke never becomes an install again, even if old
            # installed bytes were copied back outside our control.
            if current == "revoked":
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "revoked"})
                continue

            if "conflict" in states:
                first = next(action for action in actions if action["state"] == "conflict")
                record = self._conflict(
                    record, Path(first["item"]["path"]),
                    "external change detected during recovery")
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "conflict"})
                continue
            if all(state == "after" for state in states):
                if current != "installed":
                    record = self._save({**record, "state": "installed",
                                         "conflict": None,
                                         "updated_at": self._clock()})
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "installed"})
            elif all(state == "before" for state in states):
                if current == "installed":
                    record = self._conflict(
                        record, Path(record.get("desktop_path") or ""),
                        "installed files disappeared or were reverted externally; "
                        "refusing to report a successful install or clean revoke")
                    results.append({"integration_uuid": record["integration_uuid"],
                                    "action": "conflict"})
                    continue
                if current != "revoked":
                    record = self._save({**record, "state": "revoked",
                                         "conflict": None,
                                         "revoked_at": self._clock(),
                                         "updated_at": self._clock()})
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "revoked"})
            else:
                record = self._save({
                    **record, "state": "conflict",
                    "conflict": {"path": None,
                                 "reason": "recovery found a mixed before/after state"},
                    "updated_at": self._clock()})
                results.append({"integration_uuid": record["integration_uuid"],
                                "action": "conflict"})
        return results
