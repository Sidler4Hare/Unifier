"""User-facing shortcut CLI: one container per selected distribution.

This layer is deliberately thin above the existing EnvironmentService,
ViewManager and native package transactions.  It never invents a container
backend, never starts a container for help/language/path/dirs, and never falls
back to another target or to the host when something is unavailable.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import shlex
import sys

from unifier import i18n
from unifier.application_install import InstallRequest, _internal_app_id, install_application
from unifier.catalog import PREPARED
from unifier.errors import UnifierError
from unifier.compatibility import observe_host, relationship_plan, require_relationship
from unifier.desktop_clients import desktop_provisioning_failure, desktop_repair_command
from unifier.guest_paths import list_guest_directories, open_guest_directory
from unifier.install import (
    installation_root,
    load_catalog,
    locked_reference,
    worker_environment,
)
from unifier.install_state import InstallState
from unifier.routing import route_package
from unifier.native_io import add_package_arguments, package_options
from unifier.package_offer import (
    DESKTOP_ID,
    admission,
    host_launch_argv,
    load_state,
    parse_choice,
    remove_registration,
    select_host_desktop,
    whitelist_formats,
    write_registration,
)


SHORTCUTS = (
    "init", "configure", "list", "status", "dirs", "path", "ls",
    "install", "offer", "run", "start", "stop", "adopt", "language", "help",
)
GUEST_LIST_SCRIPT = r'''
set -u
target=$1
cd -- "$target" 2>/dev/null || { printf 'ERROR chdir\n' >&2; exit 66; }
printf '%s\0' "$(pwd -P)"
for entry in * .[!.]* ..?*; do
    [ -e "$entry" ] || [ -L "$entry" ] || continue
    [ -d "$entry" ] || continue
    printf '%s\0' "$entry"
done
'''


class _Parser(argparse.ArgumentParser):
    def __init__(self, *args, **kwargs):
        kwargs.setdefault("add_help", False)
        kwargs.setdefault("allow_abbrev", False)
        super().__init__(*args, **kwargs)

    def error(self, message):  # argparse normally exits and prints English
        raise UnifierError("\x00argument\x00" + message)


@dataclass
class Context:
    root: Path
    config: dict | None = None
    lock: dict | None = None
    runtime: object | None = None
    environ: dict | None = None
    locale_files: tuple[str, ...] | None = None
    stdin_isatty: bool | None = None
    input_fn: object | None = None
    out: object = field(default_factory=lambda: sys.stdout)
    err: object = field(default_factory=lambda: sys.stderr)
    services: dict = field(default_factory=dict)
    view_managers: dict = field(default_factory=dict)
    service_kwargs: dict = field(default_factory=dict)
    json_mode: bool = False

    def __post_init__(self):
        self.root = Path(self.root).resolve(strict=False)
        self.state = InstallState(self.root, target_names())
        self._language = None

    def catalog(self):
        if self.config is None or self.lock is None:
            self.config, self.lock = load_catalog()
        return self.config, self.lock

    def target_config(self, name: str) -> dict:
        config, _lock = self.catalog()
        try:
            return config["targets"][name]
        except KeyError as exc:
            raise UnifierError(self.t("dirs.unknown", target=name,
                                         choices=", ".join(target_names()))) from exc

    def language(self) -> str:
        if self._language is None:
            self._language = i18n.resolve(
                self.root, self.environ, self.locale_files)
        return self._language["language"]

    def language_info(self) -> dict:
        self.language()
        return self._language

    def t(self, key: str, **values) -> str:
        return i18n.translate(self.language(), key, **values)

    def print(self, message: str = "") -> None:
        # In machine mode stdout contains exactly one JSON document; progress
        # and human summaries move to stderr.
        print(message, file=self.err if self.json_mode else self.out)

    def error(self, message: str) -> None:
        print(message, file=self.err)

    def get_runtime(self):
        if self.runtime is None:
            from unifier.runtime import PodmanRuntime
            self.runtime = PodmanRuntime()
        return self.runtime

    def service_for(self, target: str, state: dict | None = None):
        state = state or self.state.load()
        item = state["targets"].get(target) or {}
        root = self.root
        adopted = item.get("source") == "adopted"
        if adopted:
            source_root = item.get("source_root")
            if not source_root:
                raise UnifierError(self.t("adopt.stale", target=target,
                                             detail="missing source root"))
            root = Path(source_root)
        key = str(root)
        if key not in self.services:
            from unifier.service import EnvironmentService
            config, lock = self.catalog()
            self.services[key] = EnvironmentService(
                root, self.get_runtime(), config, lock,
                **(self.service_kwargs or {}))
        service = self.services[key]
        if adopted:
            _validate_adopted(self, target, item, service)
        return service

    def view_manager_for(self, target: str, state: dict | None = None):
        service = self.service_for(target, state)
        key = str(service.root)
        if key not in self.view_managers:
            from unifier.views import ViewManager
            self.view_managers[key] = ViewManager(service.root, service)
        return self.view_managers[key]

    def is_tty(self) -> bool:
        if self.stdin_isatty is not None:
            return bool(self.stdin_isatty)
        try:
            return bool(sys.stdin.isatty())
        except (AttributeError, OSError):
            return False

    def ask(self, prompt: str) -> str:
        if self.input_fn is not None:
            return self.input_fn(prompt)
        return input(prompt)


def target_names() -> list[str]:
    return list(PREPARED)


def _display(config: dict, target: str) -> str:
    return (config.get("targets", {}).get(target) or {}).get("display_name", target)


def _env_id(state: dict, target: str) -> str:
    item = state["targets"].get(target) or {}
    return item.get("environment_id") or target


def _target_dir(ctx: Context, target: str) -> Path:
    return ctx.root / "containers" / target


def _files_dir(ctx: Context, target: str) -> Path:
    return _target_dir(ctx, target) / "files"


def _ensure_files_dir(ctx: Context, target: str) -> Path:
    files = _files_dir(ctx, target)
    if files.resolve(strict=False) != files.absolute():
        raise UnifierError("managed private directory must not follow symbolic links")
    files.mkdir(parents=True, exist_ok=True, mode=0o700)
    return files


def _private_share_policy(ctx: Context, target: str, files: Path, *,
                          selinux_enabled: bool | None = None) -> str:
    """Label only the installation-owned private area, never a project root."""
    expected = _files_dir(ctx, target).absolute()
    if (files.absolute() != expected or expected.resolve() != expected
            or not expected.is_dir() or expected.stat().st_uid != os.getuid()):
        raise UnifierError("private share is not an owned installation directory")
    if selinux_enabled is None:
        selinux_enabled = Path("/sys/fs/selinux/enforce").is_file()
    return "shared-relabel" if selinux_enabled else "preserve-host"


def _files_mounted(record: dict, files: str) -> bool:
    wanted = str(Path(files).resolve(strict=False))
    for mount in record.get("shared_mounts") or []:
        if not isinstance(mount, dict):
            continue
        host = mount.get("host")
        if isinstance(host, str) and str(Path(host).resolve(strict=False)) == wanted:
            return True
    return False


def _shared_directories(record: dict, binds) -> list[dict]:
    """List the declared user directories, checked against Podman inspect."""
    shares = []
    for mount in record.get("shared_mounts") or []:
        host, guest = mount["host"], mount["container"]
        path = Path(host)
        matching = [rw for source, destination, rw in (binds or ())
                    if source == host and destination == guest]
        status = "unverified" if binds is None else "not-bound"
        if len(matching) == 1:
            status = ("available" if path.is_dir() and path.resolve() == path
                      and os.access(path, os.R_OK | os.X_OK)
                      else "unavailable")
        shares.append({"host": host, "guest": guest, "status": status,
                       "writable": (status == "available" and matching[0]
                                    and "ro" not in (mount.get("options") or "").split(",")
                                    and os.access(path, os.W_OK | os.X_OK))})
    return shares


def _select_shared_directory(shares: list[dict], private: Path, *,
                             writable: bool = False) -> tuple[str | None, bool]:
    paths = {share["host"] for share in shares if share["status"] == "available"
             and (not writable or share["writable"])}
    if str(private) in paths:
        return str(private), False
    if len(paths) == 1:
        return next(iter(paths)), False
    return None, len(paths) > 1


def _load_state(ctx: Context) -> dict:
    try:
        return ctx.state.load()
    except UnifierError:
        raise


def _save_state(ctx: Context, state: dict) -> None:
    ctx.state.save(state)


def _state_label(ctx: Context, item: dict) -> str:
    state = item.get("state")
    if state == "adopted":
        return ctx.t("status.adopted")
    if state == "ready":
        return ctx.t("status.enabled") if item.get("selected") else ctx.t("status.disabled")
    if state == "failed":
        return ctx.t("status.failed")
    if state == "pending":
        return ctx.t("status.pending")
    if state == "missing":
        return ctx.t("status.environment_missing")
    return ctx.t("status.not_installed")


def _container_label(ctx: Context, service, env_id: str) -> tuple[str, str | None]:
    if not service.store.exists(env_id):
        return ctx.t("status.none"), None
    try:
        view = service.inspect(env_id)
    except Exception as exc:
        return f"{ctx.t('status.failed')}: {exc}", None
    runtime_state = view.get("runtime_state")
    if runtime_state == "running":
        return ctx.t("status.running"), view
    if runtime_state == "stopped":
        return ctx.t("status.stopped"), view
    if runtime_state == "missing":
        return ctx.t("status.missing"), view
    return ctx.t("status.foreign"), view


def _target_runtime_facts(ctx: Context, target: str, state: dict) -> dict:
    """Derive one target's usable identity from the environment record.

    The installation selection file keeps the user's choice and the last
    outcome, but the environment record and its live container identity are
    authoritative.  A deleted record or a changed container ID must never be
    presented as ``ready`` or as having the private files mounted.  This
    function only inspects; it never creates, starts or adopts a container.
    """
    item = state["targets"].get(target) or {}
    configured = _configured(item)
    env_id = _env_id(state, target)
    service, service_error = _safe_service(ctx, target, state)
    missing = False
    record = None
    record_valid = False

    # For an adopted target the source store itself is authoritative.  Check
    # it before trusting service construction so a deleted legacy record is
    # reported as missing, not as a generic failure.
    if configured and item.get("source") == "adopted":
        source_root = item.get("source_root")
        if not source_root:
            missing = True
        else:
            try:
                from unifier.store import EnvironmentStore
                if not EnvironmentStore(Path(source_root)).exists(env_id):
                    missing = True
            except (OSError, UnifierError) as exc:
                service_error = service_error or str(exc)

    if service is not None and not missing:
        try:
            exists = service.store.exists(env_id)
        except (OSError, UnifierError) as exc:
            service_error = service_error or str(exc)
            exists = False
        if not exists:
            missing = configured
        elif item.get("source") == "adopted":
            try:
                record = service.store.load(env_id)
                record_valid = True
            except (OSError, UnifierError) as exc:
                service_error = str(exc)
        else:
            try:
                record = _validate_new_record(ctx, service, target, env_id, item)
                record_valid = True
            except UnifierError as exc:
                service_error = str(exc)
    elif configured and not missing and service_error is None:
        service_error = "environment service is unavailable"

    if missing:
        service_error = "environment record is missing"

    container = None
    view = None
    if record_valid and service is not None:
        container, view = _container_label(ctx, service, env_id)
        if view is None:
            record_valid = False
    elif missing:
        container = ctx.t("status.none")
        view = {"runtime_state": "missing"}
    elif service_error:
        container = f"{ctx.t('status.failed')}: {service_error}"

    runtime_state = (view or {}).get("runtime_state")
    if not configured:
        derived_state = item.get("state")
    elif missing or runtime_state == "missing":
        derived_state = "failed" if item.get("state") == "failed" else "missing"
    elif service_error or not record_valid or runtime_state not in {"running", "stopped"}:
        derived_state = "failed"
    else:
        derived_state = item.get("state")

    files = _files_dir(ctx, target)
    shares = _shared_directories(record or {}, (view or {}).get("runtime_bind_mounts"))
    mounted = any(share["host"] == str(files)
                  and share["status"] in {"available", "unavailable"} for share in shares)
    failure = item.get("failure")
    if failure is None and configured and derived_state in {"missing", "failed"}:
        failure = {"phase": "record", "detail": service_error or "environment is missing"}
    return {
        "item": item,
        "configured": configured,
        "environment_id": env_id,
        "service": service,
        "record": record,
        "record_valid": record_valid,
        "missing": missing,
        "runtime_state": runtime_state,
        "state": derived_state,
        "container_label": container,
        "files_mounted": mounted,
        "shared_directories": shares,
        "failure": failure,
        "error": service_error,
    }


def _configured(item: dict) -> bool:
    return item.get("state") in {"ready", "failed", "pending", "adopted"}


def _active_views(manager, env_id: str) -> list[dict]:
    from unifier.views import is_mounted, view_active
    found = []
    for view in manager.list():
        if view.get("environment_id") != env_id:
            continue
        if view_active(view) and is_mounted(view.get("mountpoint", "")):
            found.append(view)
    return found


def _recovery_text(manager, env_id: str) -> str:
    try:
        views = [v for v in manager.list() if v.get("environment_id") == env_id]
    except Exception:
        return ""
    if not views:
        return ""
    latest = views[-1]
    return (f"view={latest.get('view_uuid')} state={latest.get('state')} "
            f"mountpoint={latest.get('mountpoint')}")


def _image_missing(exc: BaseException) -> bool:
    text = str(exc).lower()
    return ("no such image" in text or "image not known" in text
            or "not found" in text or "does not exist" in text)


def _assert_target_unique(ctx: Context, service, target: str, env_id: str,
                          state: dict) -> None:
    item = state["targets"].get(target) or {}
    if item.get("source") == "adopted":
        return
    others = [record.get("id") for record in _records_for_target(service, target)
              if record.get("id") != env_id]
    if others:
        raise UnifierError(ctx.t("install.target_conflict", target=target,
                                     others=", ".join(str(item) for item in others)))


def _records_for_target(service, target: str) -> list[dict]:
    try:
        return [record for record in service.store.all()
                if record.get("target") == target]
    except (OSError, ValueError, UnifierError) as exc:
        raise UnifierError(
            f"cannot establish the container inventory: {exc}") from exc


def _expected_target(ctx: Context, target: str) -> dict:
    config, _lock = ctx.catalog()
    try:
        return config["targets"][target]
    except KeyError as exc:
        raise UnifierError(ctx.t("dirs.unknown", target=target,
                                     choices=", ".join(target_names()))) from exc


def _validate_new_record(ctx: Context, service, target: str, env_id: str,
                         expected: dict | None = None) -> dict:
    """Validate a shortcut-created record against its target and live CID."""
    from unifier.identity import identity
    record = service.store.load(env_id)
    target_spec = _expected_target(ctx, target)
    if record.get("target") != target:
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail=f"record target {record.get('target')!r}"))
    if record.get("distribution_id") != target_spec.get("expected_id"):
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail=("distribution "
                                             f"{record.get('distribution_id')!r}")))
    if ("expected_version_id" in target_spec
            and record.get("version_track") != target_spec["expected_version_id"]):
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail=f"version {record.get('version_track')!r}"))
    try:
        ident = identity(record, required=True)
    except UnifierError as exc:
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail=str(exc))) from exc
    stored = (expected or {}).get("identity") or {}
    if stored:
        if (stored.get("environment_uuid") != ident[0]
                or stored.get("generation") != ident[1]):
            raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                         detail="UUID/generation changed"))
    runtime = record.get("runtime") or {}
    cid = runtime.get("container_id")
    name = runtime.get("container_name")
    if not cid or not name or not runtime.get("project"):
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail="incomplete runtime identity"))
    if stored and stored.get("container_id") not in {None, cid}:
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail="container id changed"))
    info = service.runtime.inspect(name)
    if (info is None or info.container_id != cid
            or not service._matches(info, record)):
        raise UnifierError(ctx.t("target.record_mismatch", target=target,
                                     detail="container UUID/CID does not match the record"))
    return record


def _validate_adopted(ctx: Context, target: str, item: dict, service) -> dict:
    from unifier.identity import identity
    env_id = item.get("environment_id") or item.get("source_environment")
    if item.get("source_environment") and env_id != item["source_environment"]:
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail="source environment changed"))
    if not env_id or not service.store.exists(env_id):
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail="legacy environment record is missing"))
    record = service.store.load(env_id)
    if record.get("target") != target:
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail=f"record target {record.get('target')!r}"))
    try:
        ident = identity(record, required=True)
    except UnifierError as exc:
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail=str(exc))) from exc
    stored = item.get("identity") or {}
    if (stored.get("environment_uuid") != ident[0]
            or stored.get("generation") != ident[1]):
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail="UUID/generation changed"))
    runtime = record.get("runtime") or {}
    cid = runtime.get("container_id")
    if not cid or stored.get("container_id") != cid:
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail="container id changed"))
    info = service.runtime.inspect(runtime.get("container_name"))
    if (info is None or info.container_id != cid
            or not service._matches(info, record)):
        raise UnifierError(ctx.t("adopt.stale", target=target,
                                     detail="runtime container identity changed"))
    return record


def _retain_incomplete_desktop(ctx: Context, state: dict, target: str, record: dict,
                               failure: dict) -> bool:
    from unifier.identity import identity

    uuid, generation = identity(record, required=True)
    env_id = record["id"]
    detail = failure.get("detail") or failure["phase"]
    ctx.state.mark(
        state, target, state="failed", container_retained=True, pending_phase=None,
        identity={"environment_uuid": uuid, "generation": generation,
                  "container_id": record["runtime"]["container_id"]},
        failure={"phase": "desktop-clients", "detail": detail,
                 "transaction_id": failure.get("transaction_id")})
    ctx.error(ctx.t("init.desktop_clients_incomplete", target=target, detail=detail,
                    command=desktop_repair_command(env_id)))
    if failure.get("recovery_required") or record.get("package_recovery_required"):
        ctx.error(ctx.t("init.desktop_clients_recover", environment=env_id))
    return False


def _prepare_target(ctx: Context, state: dict, target: str, *,
                    network: str, desktop: bool, no_portals: bool = False) -> bool:
    """Prepare one selected target under the caller's installation lock."""
    require_relationship(_target_relationship(ctx, target))
    from unifier.identity import identity
    item = state["targets"].setdefault(target, {})
    item["selected"] = True
    # Source identity survives transient failures; state is only an outcome.
    if item.get("source") == "adopted":
        try:
            ctx.service_for(target, state)
        except UnifierError as exc:
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "adopt", "detail": str(exc)[-2000:]})
            ctx.error(str(exc))
            return False
        ctx.state.mark(state, target, state="adopted", container_retained=True,
                       failure=None)
        ctx.print(ctx.t("init.env_reuse", target=target,
                        environment=_env_id(state, target)))
        return True
    item["source"] = "new"
    item["environment_id"] = _env_id(state, target)
    item["failure"] = None
    files = _ensure_files_dir(ctx, target)
    item["files_dir"] = str(files)
    service = ctx.service_for(target, state)
    env_id = _env_id(state, target)

    _config, lock = ctx.catalog()
    reference = locked_reference(lock, target)
    digest = lock["targets"][target]["manifest_digest"]
    if service.store.exists(env_id):
        record = service.store.load(env_id)
        try:
            _validate_new_record(ctx, service, target, env_id, item)
        except UnifierError as exc:
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "record",
                                    "detail": str(exc)[-2000:]})
            ctx.error(str(exc))
            return False
        try:
            view = service.inspect(env_id)
        except Exception as exc:
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "reuse", "detail": str(exc)[-2000:]})
            ctx.error(ctx.t("init.failed", target=target, phase="reuse",
                            detail=str(exc)[-2000:]))
            return False
        if view.get("runtime_state") in {"missing", "foreign"}:
            ctx.state.mark(state, target, state="failed", container_retained=False,
                           failure={"phase": "reuse",
                                    "detail": f"runtime state {view.get('runtime_state')}"})
            ctx.error(ctx.t("init.conflict", target=target))
            return False
        locked = (record.get("base_image") or {}).get("locked_reference")
        if locked != reference:
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "image-lock",
                                    "detail": f"record image {locked!r} does not match "
                                              f"locked reference {reference!r}"})
            ctx.error(ctx.t("init.failed", target=target, phase="image-lock",
                            detail=f"record image {locked!r} does not match locked "
                                   f"reference {reference!r}"))
            return False
        if record.get("target") != target:
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "reuse",
                                    "detail": f"record target {record.get('target')}"})
            ctx.error(ctx.t("init.conflict", target=target))
            return False
        others = [item.get("id") for item in _records_for_target(service, target)
                  if item.get("id") != env_id]
        if others:
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "conflict",
                                    "detail": "multiple environments for target: "
                                              + ", ".join(str(item) for item in others)})
            ctx.error(ctx.t("init.conflict", target=target))
            return False
        if desktop and not record.get("desktop"):
            ctx.state.mark(state, target, state="failed", container_retained=True,
                           failure={"phase": "desktop",
                                    "detail": "existing environment is headless; "
                                              "recreate explicitly to add a desktop"})
            ctx.error(ctx.t("init.failed", target=target, phase="desktop",
                            detail="existing environment is headless; recreate "
                                   "explicitly to add a desktop"))
            return False
        if failure := desktop_provisioning_failure(record):
            return _retain_incomplete_desktop(ctx, state, target, record, failure)
        mounted = _files_mounted(record, str(files))
        ident = identity(record, required=True)
        ctx.state.mark(
            state, target, state="ready", container_retained=True,
            files_mounted=mounted, image_digest=(record.get("base_image") or {}).get("manifest_digest"),
            image_reference=(record.get("base_image") or {}).get("locked_reference"),
            identity={"environment_uuid": ident[0], "generation": ident[1],
                      "container_id": record["runtime"]["container_id"]},
            real_runtime_state=view.get("runtime_state"), failure=None)
        ctx.print(ctx.t("init.env_reuse", target=target, environment=env_id))
        return True

    runtime = ctx.get_runtime()
    try:
        info = runtime.image_info(reference)
        ctx.print(ctx.t("init.reuse", target=target, digest=digest))
    except Exception as exc:
        if not _image_missing(exc):
            ctx.state.mark(state, target, state="failed", failure={
                "phase": "image-inspect", "detail": str(exc)[-2000:]})
            ctx.error(ctx.t("init.failed", target=target, phase="image-inspect",
                            detail=str(exc)[-2000:]))
            return False
        ctx.print(ctx.t("init.pull", target=target, reference=reference))
        try:
            info = runtime.pull_image(reference)
        except Exception as exc:
            ctx.state.mark(state, target, state="failed", failure={
                "phase": "image-pull", "detail": str(exc)[-2000:]})
            ctx.error(ctx.t("init.failed", target=target, phase="image-pull",
                            detail=str(exc)[-2000:]))
            return False
    ctx.state.mark(state, target, image_digest=digest, image_reference=reference,
                   image_id=(info or {}).get("id"))
    ctx.print(ctx.t("init.image_ready", target=target, digest=digest))

    ctx.state.mark(state, target, state="pending", pending_phase="container-create")
    ctx.print(ctx.t("init.env_create", target=target, environment=env_id,
                    network=network, desktop=str(bool(desktop)).lower()))
    try:
        private_policy = _private_share_policy(ctx, target, files)
        if private_policy == "shared-relabel":
            ctx.error(ctx.t("init.private_label", path=str(files)))
        portal_options = {"portals": None} if desktop and not no_portals else {}
        if portal_options and network == "pasta":
            portal_options["provision_desktop_clients"] = True
        record = service.create(
            target, env_id, [str(files)], network=network, desktop=desktop,
            user_namespace="keep-id", share_label_policy=private_policy, **portal_options)
        if failure := desktop_provisioning_failure(record):
            return _retain_incomplete_desktop(ctx, state, target, record, failure)
        live = service.inspect(env_id)
        if live.get("runtime_state") != "stopped":
            raise UnifierError(
                f"environment {env_id} was not stopped after creation: "
                f"{live.get('runtime_state')}")
    except Exception as exc:
        ctx.state.mark(state, target, state="failed", container_retained=False,
                       failure={"phase": "container-create",
                                "detail": str(exc)[-2000:]})
        ctx.error(ctx.t("init.failed", target=target, phase="container-create",
                        detail=str(exc)[-2000:]))
        return False
    if not service.store.exists(env_id):
        ctx.state.mark(state, target, state="failed", container_retained=False,
                       failure={"phase": "record", "detail": "environment record missing"})
        ctx.error(ctx.t("init.record_failed", target=target,
                        detail="environment record missing"))
        return False
    ident = identity(record, required=True)
    ctx.state.mark(
        state, target, state="ready", container_retained=True,
        files_mounted=_files_mounted(record, str(files)),
        identity={"environment_uuid": ident[0], "generation": ident[1],
                  "container_id": record["runtime"]["container_id"]},
        real_runtime_state="stopped", failure=None)
    ctx.print(ctx.t("init.ready", target=target))
    return True


def _validate_targets(ctx: Context, values: list[str]) -> list[str]:
    names = target_names()
    result = []
    for value in values:
        if value not in names:
            raise UnifierError(ctx.t("init.invalid_target", target=value,
                                         choices=", ".join(names)))
        if value not in result:
            result.append(value)
    return result


def _target_relationship(ctx: Context, target: str) -> dict:
    facts = (ctx.service_kwargs or {}).get("host_facts")
    if facts is None:
        facts = observe_host()
    return relationship_plan(facts, target, ctx.target_config(target))


def _interactive_selection(ctx: Context, current: list[str]) -> list[str]:
    names = [name for name in target_names()
             if _target_relationship(ctx, name)["allowed"]]
    current = [name for name in current if name in names]
    config, _lock = ctx.catalog()
    ctx.print(ctx.t("init.summary"))
    for index, name in enumerate(names, 1):
        mark = "x" if name in current else " "
        ctx.print(f"  [{mark}] {index}) {_display(config, name)}")
    ctx.print(ctx.t("init.option_none"))
    if current:
        ctx.print(ctx.t("init.current", targets=", ".join(
            _display(config, name) for name in current)))
    ctx.print(ctx.t("init.prompt"))
    try:
        raw = ctx.ask("> ").strip()
    except EOFError as exc:
        raise UnifierError(ctx.t("error.eof")) from exc
    if not raw:
        return list(current)
    chosen = []
    for token in re.split(r"[\s,]+", raw):
        if not token:
            continue
        if not token.isdigit():
            raise UnifierError(ctx.t("error.arguments", detail=token))
        number = int(token)
        if number == 0:
            return []
        if number < 1 or number > len(names):
            raise UnifierError(ctx.t("error.arguments", detail=token))
        name = names[number - 1]
        if name not in chosen:
            chosen.append(name)
    return chosen


def _selection(ctx: Context, args, state: dict) -> list[str]:
    explicit = _validate_targets(ctx, getattr(args, "target", []) or [])
    if getattr(args, "none", False):
        if explicit:
            raise UnifierError(ctx.t("error.none_target"))
        return []
    if explicit:
        for target in explicit:
            require_relationship(_target_relationship(ctx, target))
        return explicit
    current = [name for name in target_names()
               if (state["targets"].get(name) or {}).get("selected")]
    if not ctx.is_tty():
        raise UnifierError(ctx.t("error.noninteractive"))
    return _interactive_selection(ctx, current)


def _confirm(ctx: Context, selected: list[str]) -> bool:
    if not selected or not ctx.is_tty():
        return True
    config, _lock = ctx.catalog()
    labels = ", ".join(_display(config, name) for name in selected)
    ctx.print(ctx.t("init.confirm", count=len(selected), targets=labels))
    try:
        answer = ctx.ask("").strip().lower()
    except EOFError as exc:
        raise UnifierError(ctx.t("error.eof")) from exc
    return answer in {"", "y", "yes"}


def _retained_count(ctx: Context, state: dict) -> int:
    count = 0
    for target in target_names():
        item = state["targets"].get(target) or {}
        if item.get("container_retained"):
            count += 1
            continue
        if item.get("state") in {"ready", "adopted"}:
            count += 1
    return count


def cmd_init(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier init")
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--none", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--desktop", dest="desktop", action="store_true", default=None)
    parser.add_argument("--no-desktop", dest="desktop", action="store_false")
    parser.add_argument("--no-portals", action="store_true",
                        help="skip the managed Portal bridge for newly created desktops")
    parser.add_argument("--network", choices=("pasta", "none"), default="pasta")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    desktop = bool(args.desktop)
    with ctx.state.locked():
        state = _load_state(ctx)
        selected = _selection(ctx, args, state)
        if not args.yes and not _confirm(ctx, selected):
            return 0
        state = ctx.state.set_selection(state, selected)
        if selected:
            config, _lock = ctx.catalog()
            ctx.print(ctx.t("init.plan", targets=", ".join(
                _display(config, name) for name in selected)))
        else:
            ctx.print(ctx.t("init.none"))
        failures = 0
        for target in selected:
            ok = _prepare_target(ctx, state, target, network=args.network,
                                 desktop=desktop, no_portals=args.no_portals)
            _save_state(ctx, state)
            state = _load_state(ctx)
            state["targets"][target]["selected"] = True
            if not ok:
                failures += 1
        _save_state(ctx, state)
        state = _load_state(ctx)
        if args.json:
            _emit(ctx, _list_payload(ctx, state))
        else:
            ctx.print(ctx.t("init.done", enabled=len(selected),
                            retained=_retained_count(ctx, state)))
            if failures:
                ctx.print(ctx.t("init.continued"))
        return 0 if failures == 0 else 1


def cmd_configure(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier configure")
    parser.add_argument("--target", action="append", default=[])
    parser.add_argument("--none", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--desktop", dest="desktop", action="store_true", default=None)
    parser.add_argument("--no-desktop", dest="desktop", action="store_false")
    parser.add_argument("--no-portals", action="store_true",
                        help="skip the managed Portal bridge for newly created desktops")
    parser.add_argument("--network", choices=("pasta", "none"), default="pasta")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    desktop = bool(args.desktop)
    with ctx.state.locked():
        state = _load_state(ctx)
        selected = _selection(ctx, args, state)
        if not args.yes and not _confirm(ctx, selected):
            return 0
        previous = {name for name in target_names()
                    if (state["targets"].get(name) or {}).get("selected")}
        state = ctx.state.set_selection(state, selected)
        failures = 0
        for target in selected:
            if not _prepare_target(ctx, state, target, network=args.network,
                                   desktop=desktop, no_portals=args.no_portals):
                failures += 1
            state["targets"][target]["selected"] = True
            _save_state(ctx, state)
            state = _load_state(ctx)
        for target in target_names():
            item = state["targets"].get(target) or {}
            if target not in selected:
                item["selected"] = False
                if item.get("state") in {"ready", "adopted"}:
                    item["container_retained"] = True
        _save_state(ctx, state)
        state = _load_state(ctx)
        if args.json:
            _emit(ctx, _list_payload(ctx, state))
            return 0 if failures == 0 else 1
        enabled = sum(1 for name in target_names()
                      if (state["targets"].get(name) or {}).get("selected"))
        retained = _retained_count(ctx, state)
        ctx.print(ctx.t("configure.done", enabled=enabled, retained=retained,
                        failed=failures))
        config, _lock = ctx.catalog()
        for target in target_names():
            item = state["targets"].get(target) or {}
            if target in selected:
                state_label = (ctx.t("configure.failed", target=target)
                               if item.get("state") == "failed"
                               else ctx.t("configure.enabled", target=target))
            elif item.get("state") in {"ready", "adopted"}:
                state_label = ctx.t("configure.disabled_retained", target=target)
            else:
                state_label = ctx.t("configure.not_selected", target=target)
            ctx.print(f"{_display(config, target)}: {state_label}")
        return 0 if failures == 0 else 1


def _safe_service(ctx: Context, target: str, state: dict):
    try:
        return ctx.service_for(target, state), None
    except UnifierError as exc:
        return None, str(exc)


def _list_payload(ctx: Context, state: dict) -> dict:
    config, _lock = ctx.catalog()
    targets = []
    enabled = []
    retained = []
    failed = []
    missing = []
    blocked = []
    for target in target_names():
        item = state["targets"].get(target) or {}
        compatibility = _target_relationship(ctx, target)
        facts = _target_runtime_facts(ctx, target, state)
        policy_blocked = facts["configured"] and not compatibility["allowed"]
        policy_error = "; ".join(compatibility["reasons"]) if policy_blocked else None
        error = facts["error"] or policy_error
        if item.get("selected") and compatibility["allowed"]:
            enabled.append(target)
        if policy_blocked:
            blocked.append(target)
        if facts["missing"]:
            missing.append(target)
        if facts["record_valid"] and facts["runtime_state"] in {"running", "stopped"}:
            retained.append(target)
        if (item.get("state") == "failed"
                or facts["state"] in {"failed", "missing"} or facts["error"]):
            failed.append(target)
        targets.append({
            "target": target,
            "display_name": _display(config, target),
            "selected": bool(item.get("selected")),
            "configured": facts["configured"],
            "state": facts["state"],
            "environment_id": facts["environment_id"],
            "container_state": facts["runtime_state"],
            "container_label": facts["container_label"] or ctx.t("status.none"),
            "directory": str(_target_dir(ctx, target)),
            "files_dir": str(_files_dir(ctx, target)),
            "files_mounted": facts["files_mounted"],
            "source": item.get("source"),
            "source_root": item.get("source_root"),
            "source_environment": item.get("source_environment"),
            "failure": facts["failure"],
            "error": error,
            "policy_blocked": policy_blocked,
            "compatibility": compatibility,
        })
    return {"schema_version": 1, "enabled": enabled, "retained": retained,
            "failed": failed, "missing": missing, "blocked": blocked,
            "targets": targets}


def cmd_list(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier list")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state = _load_state(ctx)
    payload = _list_payload(ctx, state)
    if args.json:
        _emit(ctx, payload)
        return 0
    if not any(item["configured"] for item in payload["targets"]):
        ctx.print(ctx.t("list.none"))
        return 0
    ctx.print(ctx.t("list.header"))
    for item in payload["targets"]:
        item_state = state["targets"].get(item["target"]) or {}
        if item["policy_blocked"]:
            state_label = f"{ctx.t('status.blocked')}: {item['error']}"
        elif item["state"] == "missing":
            state_label = ctx.t("status.environment_missing")
        elif item["state"] == "failed" and item["error"]:
            state_label = f"{ctx.t('status.failed')}: {item['error']}"
        else:
            state_label = _state_label(ctx, {**item_state, "state": item["state"]})
        ctx.print(f"{item['display_name']}\t{state_label}"
                  f"\t{item['container_label']}\t{item['directory']}")
    ctx.print(ctx.t("list.footer", enabled=len(payload["enabled"]),
                    retained=len(payload["retained"])))
    if payload["failed"]:
        ctx.print(ctx.t("list.failed", count=len(payload["failed"])))
    return 0


def cmd_status(ctx: Context, argv: list[str]) -> int:
    return cmd_list(ctx, argv)


def _dirs_payload(ctx: Context, state: dict, targets: list[str]) -> list[dict]:
    config, _lock = ctx.catalog()
    rows = []
    for target in targets:
        item = state["targets"].get(target) or {}
        facts = _target_runtime_facts(ctx, target, state)
        files = _files_dir(ctx, target)
        shared_files_dir, _ = _select_shared_directory(facts["shared_directories"], files)
        view_state = ctx.t("dirs.view_unmounted")
        view_path = None
        if (facts["record_valid"] and facts["service"] is not None
                and facts["runtime_state"] in {"running", "stopped"}):
            try:
                manager = ctx.view_manager_for(target, state)
                active = _active_views(manager, facts["environment_id"])
                if active:
                    view_path = active[0].get("mountpoint")
                    view_state = ctx.t("dirs.view_mounted", path=view_path)
            except Exception:
                view_state = ctx.t("dirs.view_unmounted")
        rows.append({
            "target": target,
            "display_name": _display(config, target),
            "configured": facts["configured"],
            "state": facts["state"],
            "container_state": facts["runtime_state"],
            "container_label": facts["container_label"] or ctx.t("status.none"),
            "directory": str(_target_dir(ctx, target)),
            "files_dir": str(files),
            "files_mounted": facts["files_mounted"],
            "shared_directories": facts["shared_directories"],
            "shared_files_dir": shared_files_dir,
            "view_state": view_state,
            "view_path": view_path,
            "source": item.get("source"),
            "source_root": item.get("source_root"),
            "source_environment": item.get("source_environment"),
            "error": facts["error"],
        })
    return rows

def cmd_dirs(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier dirs")
    parser.add_argument("target", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state = _load_state(ctx)
    if args.target:
        if args.target not in target_names():
            raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                         choices=", ".join(target_names())))
        item = state["targets"].get(args.target) or {}
        if not _configured(item):
            raise UnifierError(ctx.t("dirs.not_configured", target=args.target))
        targets = [args.target]
    else:
        targets = [name for name in target_names()
                   if _configured(state["targets"].get(name) or {})]
    rows = _dirs_payload(ctx, state, targets)
    if args.json:
        _emit(ctx, {"schema_version": 1, "targets": rows})
        return 0
    for row in rows:
        state_label = row["container_label"]
        if row["state"] == "missing":
            state_label = ctx.t("status.environment_missing")
        elif row["state"] == "failed" and row["error"]:
            state_label = f"{ctx.t('status.failed')}: {row['error']}"
        ctx.print(ctx.t("dirs.target", target=row["target"],
                        path=row["directory"], state=state_label))
        if not row["files_mounted"]:
            ctx.print(ctx.t("dirs.files_unmounted"))
        for share in row["shared_directories"]:
            status = share["status"]
            if status == "available":
                status = "writable" if share["writable"] else "read-only"
            ctx.print(ctx.t("dirs.share", host=share["host"], guest=share["guest"],
                            status=ctx.t(f"dirs.share.{status}")))
        if not row["shared_directories"]:
            ctx.print(ctx.t("dirs.share.none"))
        if row["shared_files_dir"]:
            ctx.print(ctx.t("dirs.share.selected", path=row["shared_files_dir"]))
        ctx.print(ctx.t("dirs.rootfs", state=row["view_state"]))
    return 0


def cmd_path(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier path")
    parser.add_argument("target")
    parser.add_argument("--files", action="store_true", required=True)
    parser.add_argument("--writable", action="store_true")
    args = parser.parse_args(argv)
    if args.target not in target_names():
        raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                     choices=", ".join(target_names())))
    state = _load_state(ctx)
    item = state["targets"].get(args.target) or {}
    if not _configured(item):
        raise UnifierError(ctx.t("path.not_configured", target=args.target))
    facts = _target_runtime_facts(ctx, args.target, state)
    if facts["missing"]:
        raise UnifierError(ctx.t("path.not_configured", target=args.target))
    path, ambiguous = _select_shared_directory(
        facts["shared_directories"], _files_dir(ctx, args.target), writable=args.writable)
    if path is None:
        key = "path.ambiguous" if ambiguous else "path.no_share"
        raise UnifierError(ctx.t(key, target=args.target))
    ctx.print(path)
    return 0


def _guest_list_running(ctx: Context, service, env_id: str, target: str,
                        guest_path: str) -> dict:
    record = service.store.load(env_id)
    service._require_host_compatibility(record)
    runtime = service.runtime
    info = service._require_owned(record)
    if not info.running:
        raise UnifierError(ctx.t("ls.stopped", target=target, path=guest_path))
    result = runtime.exec(
        record["runtime"]["container_name"],
        ["/bin/sh", "-c", GUEST_LIST_SCRIPT, "unifier-ls", guest_path],
        project=record["runtime"]["project"], env_id=env_id, workdir=None,
        env={}, capture=True, timeout=30)
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()[-2000:]
        raise UnifierError(ctx.t("ls.runtime_failed", target=target,
                                     path=guest_path, detail=detail))
    from unifier.guest_paths import parse_nul_listing
    return {"via": "runtime", **parse_nul_listing(result.stdout or "")}


def _list_view_directories(root: Path, guest_path: str) -> dict:
    """List a stopped view with the FUSE projected-link semantics."""
    return list_guest_directories(root, guest_path, projected=True)


def _guest_list_stopped(ctx: Context, target: str, state: dict,
                        guest_path: str) -> dict:
    manager = ctx.view_manager_for(target, state)
    env_id = _env_id(state, target)
    active = _active_views(manager, env_id)
    opened = None
    if active:
        ready = [view for view in active if view.get("state") == "ready"]
        if not ready:
            detail = _recovery_text(manager, env_id) or "view not ready"
            raise UnifierError(ctx.t("ls.view_busy", target=target, detail=detail))
        view = ready[0]
        ctx.error(ctx.t("ls.reuse_view", target=target, view=view.get("view_uuid")))
    else:
        try:
            view = manager.open(env_id)
            opened = view.get("view_uuid")
        except (UnifierError, OSError) as exc:
            detail = _recovery_text(manager, env_id) or str(exc)
            raise UnifierError(ctx.t("ls.view_failed", target=target,
                                         detail=detail)) from exc
        ctx.error(ctx.t("ls.open_view", target=target, view=view.get("view_uuid")))
    try:
        payload = {"via": "view", "view_uuid": view.get("view_uuid"),
                   "mountpoint": view.get("mountpoint")}
        payload.update(_list_view_directories(Path(view["mountpoint"]), guest_path))
        return payload
    except (UnifierError, OSError) as exc:
        detail = _recovery_text(manager, env_id) or str(exc)
        raise UnifierError(ctx.t("ls.view_failed", target=target,
                                     detail=detail)) from exc
    finally:
        if opened is not None:
            try:
                manager.revoke(opened)
            except (UnifierError, OSError) as exc:
                detail = _recovery_text(manager, env_id) or str(exc)
                raise UnifierError(ctx.t("ls.view_failed", target=target,
                                             detail=detail)) from exc


def cmd_ls(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier ls")
    parser.add_argument("target")
    parser.add_argument("guest_path", nargs="?", default="/")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if not args.guest_path.startswith("/"):
        raise UnifierError(ctx.t("ls.relative", path=args.guest_path))
    if args.target not in target_names():
        raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                     choices=", ".join(target_names())))
    state = _load_state(ctx)
    item = state["targets"].get(args.target) or {}
    if not _configured(item):
        raise UnifierError(ctx.t("ls.not_configured", target=args.target))
    service = ctx.service_for(args.target, state)
    env_id = _env_id(state, args.target)
    if not service.store.exists(env_id):
        raise UnifierError(ctx.t("ls.not_configured", target=args.target))
    if item.get("source") != "adopted":
        _validate_new_record(ctx, service, args.target, env_id, item)
    try:
        info = service.inspect(env_id)
    except Exception as exc:
        raise UnifierError(ctx.t("ls.runtime_failed", target=args.target,
                                     path=args.guest_path,
                                     detail=str(exc)[-2000:])) from exc
    runtime_state = info.get("runtime_state")
    if runtime_state == "running":
        ctx.error(ctx.t("ls.running", target=args.target, path=args.guest_path))
        payload = _guest_list_running(ctx, service, env_id, args.target,
                                      args.guest_path)
    elif runtime_state == "stopped":
        ctx.error(ctx.t("ls.stopped", target=args.target, path=args.guest_path))
        payload = _guest_list_stopped(ctx, args.target, state, args.guest_path)
    else:
        raise UnifierError(ctx.t("ls.runtime_failed", target=args.target,
                                     path=args.guest_path,
                                     detail=f"runtime state {runtime_state}"))
    names = payload.get("entries") or []
    if args.json:
        _emit(ctx, {"schema_version": 1, "target": args.target, **payload})
    else:
        ctx.print("  ".join(names))
    return 0


def _enabled_targets(state: dict) -> list[str]:
    return [name for name in target_names()
            if (state["targets"].get(name) or {}).get("selected")]


def _choose_target(ctx: Context, candidates: list[str], flag: str | None) -> str:
    if not candidates:
        raise UnifierError(ctx.t("install.no_target"))
    if flag:
        if flag not in candidates:
            raise UnifierError(ctx.t("install.target_disabled", target=flag))
        return flag
    if len(candidates) == 1:
        return candidates[0]
    if not ctx.is_tty():
        raise UnifierError(ctx.t("install.noninteractive_target",
                                     targets=", ".join(candidates)))
    config, _lock = ctx.catalog()
    ctx.print(ctx.t("install.choose_target"))
    for index, name in enumerate(candidates, 1):
        ctx.print(f"  {index}) {_display(config, name)}")
    try:
        raw = ctx.ask("> ").strip()
    except EOFError as exc:
        raise UnifierError(ctx.t("error.eof")) from exc
    if not raw.isdigit() or not 1 <= int(raw) <= len(candidates):
        raise UnifierError(ctx.t("error.arguments", detail=raw))
    return candidates[int(raw) - 1]


def _entry_label(item: dict) -> str:
    argv = item.get("argv") or [item.get("executable") or item.get("path") or "?"]
    return shlex.join(str(part) for part in argv)


def _entry_choice(ctx: Context, candidates: list[dict]) -> dict:
    if not candidates:
        raise UnifierError(ctx.t("install.entry_required",
                                     candidates="none"))
    if len(candidates) == 1:
        return candidates[0]
    if not ctx.is_tty():
        names = ", ".join(_entry_label(item) for item in candidates)
        raise UnifierError(ctx.t("install.entry_required", candidates=names))
    ctx.print(ctx.t("install.choose_entry"))
    for index, item in enumerate(candidates, 1):
        ctx.print(f"  {index}) {_entry_label(item)}"
                  f" ({item.get('source') or 'discovered'})")
    try:
        raw = ctx.ask("> ").strip()
    except EOFError as exc:
        raise UnifierError(ctx.t("error.eof")) from exc
    if not raw.isdigit() or not 1 <= int(raw) <= len(candidates):
        raise UnifierError(ctx.t("error.arguments", detail=raw))
    return candidates[int(raw) - 1]


def _emit(ctx: Context, payload: dict | list) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2), file=ctx.out)


def _ready_install_target(ctx: Context, state: dict, target: str):
    require_relationship(_target_relationship(ctx, target))
    item = state["targets"].get(target) or {}
    if not item.get("selected"):
        raise UnifierError(ctx.t("install.target_disabled", target=target))
    if item.get("state") not in {"ready", "adopted"}:
        raise UnifierError(ctx.t("install.not_ready", target=target))
    service = ctx.service_for(target, state)
    env_id = _env_id(state, target)
    if not service.store.exists(env_id):
        raise UnifierError(ctx.t("install.not_ready", target=target))
    _assert_target_unique(ctx, service, target, env_id, state)
    if item.get("source") != "adopted":
        _validate_new_record(ctx, service, target, env_id, item)
    return service, env_id


def cmd_install(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier install")
    parser.add_argument("--target")
    parser.add_argument("--exec", dest="executable")
    parser.add_argument("--app", dest="app_id")
    parser.add_argument("--package")
    parser.add_argument("--register-only", action="store_true")
    parser.add_argument("--gui", "--desktop-app", dest="gui", action="store_true")
    parser.add_argument("--json", action="store_true")
    add_package_arguments(parser, repair=True, json_option=False)
    parser.add_argument("items", nargs="+")
    head, extra = _split_double_dash(argv)
    args = parser.parse_args(head)
    if args.register_only:
        if (len(args.items) != 1 or args.package is not None
                or args.interactive or args.yes or args.no_repair_deps):
            raise UnifierError(ctx.t("install.register_usage"))
        options = {}
    else:
        options = package_options(args, repair=True)
        options["application_runtime"] = True
    state = _load_state(ctx)
    request = InstallRequest(
        items=args.items, argv=extra, target=args.target, executable=args.executable,
        app_id=args.app_id, package=args.package, gui=args.gui,
        register_only=args.register_only, package_options=options)
    result = install_application(
        ctx, request, enabled=_enabled_targets(state),
        choose_target=lambda candidates, flag: _choose_target(ctx, candidates, flag),
        ready_target=lambda target: _ready_install_target(ctx, state, target),
        choose_entry=lambda candidates: _entry_choice(ctx, candidates),
        internal_app_id=_internal_app_id)
    if args.json:
        _emit(ctx, {"schema_version": 1, "target": result.target,
                    "environment_id": result.environment_id,
                    "transaction": result.transaction, **result.details})
    elif result.announce_app:
        ctx.print(ctx.t("install.app_name", app=result.details["app"]))
    return result.exit_code


def _launch_targets(ctx: Context, state: dict, app_id: str) -> list[str]:
    found = []
    for target in _enabled_targets(state):
        if not _target_relationship(ctx, target)["allowed"]:
            continue
        service = ctx.service_for(target, state)
        env_id = _env_id(state, target)
        if not service.store.exists(env_id):
            continue
        try:
            service.app_by_alias(env_id, app_id)
        except UnifierError:
            continue
        found.append(target)
    return found


def cmd_run(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier run")
    parser.add_argument("--target")
    parser.add_argument("app")
    head, extra = _split_double_dash(argv)
    args = parser.parse_args(head)
    state = _load_state(ctx)
    enabled = _enabled_targets(state)
    if not enabled:
        raise UnifierError(ctx.t("run.no_target"))
    if args.target:
        if args.target not in target_names():
            raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                         choices=", ".join(target_names())))
        target = args.target
        item = state["targets"].get(target) or {}
        if not item.get("selected"):
            raise UnifierError(ctx.t("start.disabled", target=target))
    else:
        found = _launch_targets(ctx, state, args.app)
        if not found:
            raise UnifierError(ctx.t("run.app_missing", app=args.app))
        if len(found) > 1:
            raise UnifierError(ctx.t("run.ambiguous", app=args.app))
        target = found[0]
    require_relationship(_target_relationship(ctx, target))
    service = ctx.service_for(target, state)
    env_id = _env_id(state, target)
    if not service.store.exists(env_id):
        raise UnifierError(ctx.t("run.app_missing", app=args.app))
    if (state["targets"].get(target) or {}).get("source") != "adopted":
        _validate_new_record(ctx, service, target, env_id,
                             state["targets"].get(target) or {})
    try:
        launch = service.app_by_alias(env_id, args.app)
    except UnifierError as exc:
        raise UnifierError(ctx.t("run.app_missing", app=args.app)) from exc
    if launch.get("environment_id") != env_id:
        raise UnifierError(ctx.t("run.target_missing", app=args.app,
                                     target=target))
    try:
        result = service.open_app(launch["id"], extra)
    except UnifierError as exc:
        raise UnifierError(ctx.t("run.failed", app=args.app,
                                     detail=str(exc))) from exc
    code = getattr(result, "returncode", result if isinstance(result, int) else 1)
    return int(code)


def cmd_start(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier start")
    parser.add_argument("target")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    state = _load_state(ctx)
    item = state["targets"].get(args.target) or {}
    if args.target not in target_names():
        raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                     choices=", ".join(target_names())))
    require_relationship(_target_relationship(ctx, args.target))
    if not item.get("selected"):
        raise UnifierError(ctx.t("start.disabled", target=args.target))
    if item.get("state") not in {"ready", "adopted"}:
        raise UnifierError(ctx.t("start.not_ready", target=args.target))
    service = ctx.service_for(args.target, state)
    env_id = _env_id(state, args.target)
    if not service.store.exists(env_id):
        raise UnifierError(ctx.t("start.not_ready", target=args.target))
    _assert_target_unique(ctx, service, args.target, env_id, state)
    if item.get("source") != "adopted":
        _validate_new_record(ctx, service, args.target, env_id, item)
    view = service.inspect(env_id)
    if view.get("runtime_state") == "running":
        ctx.print(ctx.t("start.running", target=args.target))
    else:
        try:
            service.start(env_id)
        except UnifierError as exc:
            raise UnifierError(ctx.t("start.failed", target=args.target,
                                         detail=str(exc))) from exc
        ctx.print(ctx.t("start.started", target=args.target, environment=env_id))
    if args.json:
        _emit(ctx, {"schema_version": 1, "target": args.target,
                    "environment_id": env_id, "runtime_state": "running"})
    return 0


@contextmanager
def _stop_guard(ctx: Context, service, env_id: str):
    """Hold lease -> package -> lifecycle lock order while stopping.

    An unreadable lease file is unknown occupancy and refuses.  A live lease
    or an unresolved project session also refuses.  The package lock is taken
    non-blocking so a concurrent native install is never interrupted; the
    lifecycle lock is acquired by service.stop only after both checks.
    """
    from unifier.on_demand import LaunchGroup
    group = LaunchGroup(service, env_id)
    with group.locked():
        path = group.path
        if path.exists():
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise UnifierError(ctx.t("stop.unknown", target=env_id,
                                             detail=f"lease file: {exc}")) from exc
            if not isinstance(state, dict):
                raise UnifierError(ctx.t("stop.unknown", target=env_id,
                                             detail="lease record is not an object"))
            leases = state.get("leases", {})
            if not isinstance(leases, dict):
                raise UnifierError(ctx.t("stop.unknown", target=env_id,
                                             detail="leases is not an object"))
            for key, item in leases.items():
                if not isinstance(item, dict):
                    raise UnifierError(ctx.t("stop.unknown", target=env_id,
                                                 detail=f"malformed lease {key}"))
                if (item.get("kind") != "project-session"
                        and (item.get("pid") is None
                             or item.get("stamp") is None)):
                    raise UnifierError(ctx.t("stop.unknown", target=env_id,
                                                 detail=f"lease {key} lacks pid/stamp"))
            live = {}
            for key, item in leases.items():
                if item.get("kind") == "project-session":
                    live[key] = item
                    continue
                pid, stamp = item["pid"], item["stamp"]
                if (type(pid) is not int or pid <= 0
                        or type(stamp) is not int or stamp <= 0):
                    raise UnifierError(ctx.t(
                        "stop.unknown", target=env_id,
                        detail=f"lease {key} has invalid process identity"))
                try:
                    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                    actual = int(fields[19])
                    zombie = fields[0] == "Z"
                except (FileNotFoundError, ProcessLookupError):
                    continue
                except (OSError, ValueError, IndexError) as exc:
                    raise UnifierError(ctx.t(
                        "stop.unknown", target=env_id,
                        detail=f"cannot inspect lease process {pid}: {exc}")) from exc
                if not zombie and actual == stamp:
                    live[key] = item
            if live:
                detail = ", ".join(sorted(
                    str(item.get("app_id") or item.get("kind") or key)
                    for key, item in live.items()))
                raise UnifierError(ctx.t("stop.busy", target=env_id,
                                             detail=detail))
        try:
            from unifier.project_sessions import assert_environment_not_live
            assert_environment_not_live(service.root, env_id, service)
        except UnifierError as exc:
            raise UnifierError(ctx.t("stop.busy", target=env_id,
                                         detail=str(exc))) from exc
        except Exception as exc:
            raise UnifierError(ctx.t("stop.unknown", target=env_id,
                                         detail=str(exc))) from exc
        descriptor = service.try_package_operation_lock(env_id)
        if descriptor is None:
            raise UnifierError(ctx.t("stop.package_busy", target=env_id))
        try:
            yield
        finally:
            service.release_package_operation_lock(descriptor)


def cmd_stop(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier stop")
    parser.add_argument("target")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.target not in target_names():
        raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                     choices=", ".join(target_names())))
    state = _load_state(ctx)
    item = state["targets"].get(args.target) or {}
    if item.get("state") not in {"ready", "adopted"}:
        raise UnifierError(ctx.t("stop.not_ready", target=args.target))
    service = ctx.service_for(args.target, state)
    env_id = _env_id(state, args.target)
    if not service.store.exists(env_id):
        raise UnifierError(ctx.t("stop.not_ready", target=args.target))
    _assert_target_unique(ctx, service, args.target, env_id, state)
    if item.get("source") != "adopted":
        _validate_new_record(ctx, service, args.target, env_id, item)
    view = service.inspect(env_id)
    if view.get("runtime_state") != "running":
        ctx.print(ctx.t("stop.already", target=args.target))
    else:
        with _stop_guard(ctx, service, env_id):
            # Re-read under the lease lock; a start may have won the race.
            view = service.inspect(env_id)
            if view.get("runtime_state") != "running":
                ctx.print(ctx.t("stop.already", target=args.target))
            else:
                try:
                    service.stop(env_id, package_lock_held=True)
                except UnifierError as exc:
                    raise UnifierError(ctx.t("stop.failed", target=args.target,
                                                 detail=str(exc))) from exc
                ctx.print(ctx.t("stop.stopped", target=args.target,
                                environment=env_id))
    if args.json:
        _emit(ctx, {"schema_version": 1, "target": args.target,
                    "environment_id": env_id, "runtime_state": "stopped"})
    return 0


def cmd_adopt(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier adopt")
    parser.add_argument("--target", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.target not in target_names():
        raise UnifierError(ctx.t("dirs.unknown", target=args.target,
                                     choices=", ".join(target_names())))
    require_relationship(_target_relationship(ctx, args.target))
    source_root = Path(args.root).expanduser().resolve(strict=False)
    if not source_root.is_dir():
        raise UnifierError(ctx.t("adopt.invalid", detail=str(source_root)))
    from unifier.store import EnvironmentStore
    store = EnvironmentStore(source_root)
    if not store.exists(args.environment):
        raise UnifierError(ctx.t("adopt.invalid",
                                     detail=f"environment {args.environment} not found"))
    try:
        record = store.load(args.environment)
    except UnifierError as exc:
        raise UnifierError(ctx.t("adopt.invalid", detail=str(exc))) from exc
    if record.get("target") != args.target:
        raise UnifierError(ctx.t("adopt.invalid",
                                     detail=f"target {record.get('target')} != {args.target}"))
    conflicts = [item.get("id") for item in store.all()
                 if item.get("target") == args.target
                 and item.get("id") != args.environment]
    from unifier.identity import identity
    runtime = record.get("runtime") or {}
    try:
        ident = identity(record, required=True)
    except UnifierError as exc:
        raise UnifierError(ctx.t("adopt.invalid", detail=str(exc))) from exc
    # Report a pre-existing different target selection before probing the
    # legacy runtime; the runtime check must not hide the conflict.
    snapshot = _load_state(ctx)
    existing_item = snapshot["targets"].get(args.target) or {}
    same_snapshot = (existing_item.get("source") == "adopted"
                     and existing_item.get("source_root") == str(source_root)
                     and existing_item.get("source_environment") == args.environment)
    if (existing_item.get("state") in {"ready", "pending", "adopted"}
            and not same_snapshot):
        raise UnifierError(ctx.t("adopt.conflict", target=args.target))
    config, lock = ctx.catalog()
    from unifier.service import EnvironmentService
    source_service = EnvironmentService(
        source_root, ctx.get_runtime(), config, lock,
        **(ctx.service_kwargs or {}))
    _validate_adopted(ctx, args.target, {
        "source_root": str(source_root),
        "source_environment": args.environment,
        "environment_id": args.environment,
        "identity": {
            "environment_uuid": ident[0],
            "generation": ident[1],
            "container_id": runtime.get("container_id"),
        },
    }, source_service)
    files = _files_dir(ctx, args.target)
    mounted = _files_mounted(record, str(files))
    with ctx.state.locked():
        state = _load_state(ctx)
        item = state["targets"].get(args.target) or {}
        same = (item.get("source") == "adopted"
                and item.get("source_root") == str(source_root)
                and item.get("source_environment") == args.environment)
        if item.get("state") in {"ready", "pending", "adopted"} and not same:
            raise UnifierError(ctx.t("adopt.conflict", target=args.target))
        # Create the management-side directory without changing the legacy
        # container's frozen mounts.  files_mounted remains truthful below.
        files = _ensure_files_dir(ctx, args.target)
        item.update({
            "selected": True,
            "state": "adopted",
            "source": "adopted",
            "source_root": str(source_root),
            "source_environment": args.environment,
            "environment_id": args.environment,
            "files_dir": str(files),
            "files_mounted": mounted,
            "container_retained": True,
            "identity": {"environment_uuid": ident[0] if ident else None,
                         "generation": ident[1] if ident else None,
                         "container_id": runtime.get("container_id")},
            "failure": None,
            "conflicts": conflicts,
        })
        state["targets"][args.target] = item
        state["initialized"] = True
        _save_state(ctx, state)
    payload = {
        "schema_version": 1,
        "target": args.target,
        "environment_id": args.environment,
        "source_root": str(source_root),
        "environment_uuid": ident[0] if ident else None,
        "generation": ident[1] if ident else None,
        "container_id": runtime.get("container_id"),
        "files_dir": str(files),
        "files_mounted": mounted,
        "conflicts": conflicts,
    }
    if args.json:
        _emit(ctx, payload)
    else:
        ctx.print(ctx.t("adopt.title"))
        ctx.print(ctx.t("adopt.done", target=args.target, environment=args.environment,
                        root=source_root, uuid=payload["environment_uuid"],
                        cid=payload["container_id"]))
        if not mounted:
            ctx.print(ctx.t("adopt.files_unmounted", files=files))
    return 0


def cmd_language(ctx: Context, argv: list[str]) -> int:
    parser = _Parser(prog="unifier language")
    parser.add_argument("value", nargs="?")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.value is not None:
        value = args.value.strip()
        lowered = value.lower()
        if lowered in {"auto"}:
            i18n.save_preference(ctx.root, None)
        elif lowered in {"zh", "zh-cn", "zh_hans", "zh-hans"}:
            i18n.save_preference(ctx.root, "zh-CN")
        elif lowered in {"en", "en-us", "en-gb"}:
            i18n.save_preference(ctx.root, "en")
        else:
            raise UnifierError(ctx.t("language.invalid"))
        ctx._language = None
    info = ctx.language_info()
    if args.json:
        _emit(ctx, {"schema_version": 1, "language": info["language"],
                    "source": info["source"],
                    "preference": info.get("preference"),
                    "locale_value": info.get("locale_value")})
        return 0
    ctx.print(ctx.t("language.current", language=info["language"]))
    ctx.print(ctx.t("language.source", source=i18n.source_label(
        info["language"], info["source"])))
    if info.get("preference"):
        ctx.print(ctx.t("language.preference", preference=info["preference"]))
    else:
        ctx.print(ctx.t("language.preference", preference=ctx.t("language.auto")))
    if args.value is not None:
        ctx.print(ctx.t("language.auto_changed", language=info["language"],
                        source=i18n.source_label(info["language"], info["source"]))
                  if args.value.strip().lower() == "auto"
                  else ctx.t("language.changed", language=info["language"]))
    return 0


def _offer_dirs(ctx: Context) -> tuple[Path, Path]:
    values = ctx.environ if ctx.environ is not None else os.environ
    data_home = Path(values.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
    return data_home / "applications", data_home / "mime"


def _query_default(mime: str):
    import subprocess
    try:
        result = subprocess.run(["xdg-mime", "query", "default", mime],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    name = (result.stdout or "").strip().split(";", 1)[0].strip()
    from unifier.package_offer import DESKTOP_NAME
    if result.returncode or not name or name == DESKTOP_ID or not DESKTOP_NAME.fullmatch(name):
        return None
    return name


def _host_desktop_ids(directories: list[Path]) -> set[str]:
    found = set()
    for directory in directories:
        if not directory.is_dir():
            continue
        for path in directory.glob("*.desktop"):
            if path.is_file():
                found.add(path.name)
    return found


def _handoff(ctx: Context, path: str, mime: str | None) -> int:
    import shutil
    import subprocess
    state = load_state(ctx.root)
    saved = (state.get("previous") or {}).get(mime) if mime else None
    applications, _mime = _offer_dirs(ctx)
    available = _host_desktop_ids([
        applications, Path("/usr/local/share/applications"), Path("/usr/share/applications"),
    ])
    chosen = select_host_desktop(saved, available)
    if not chosen:
        ctx.error(ctx.t("offer.no_handler"))
        return 1
    tool = "gio" if shutil.which("gio") else "gtk-launch" if shutil.which("gtk-launch") else None
    if tool is None:
        ctx.error(ctx.t("offer.no_handler"))
        return 1
    argv = host_launch_argv(chosen, path, tool=tool)
    subprocess.Popen(argv, start_new_session=True)
    ctx.print(ctx.t("offer.handoff", handler=chosen))
    return 0


def _refresh_desktop(applications: Path, mime_root: Path) -> None:
    import subprocess
    for argv in (["update-desktop-database", str(applications)],
                 ["update-mime-database", str(mime_root)]):
        try:
            subprocess.run(argv, capture_output=True, text=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired):
            continue


def cmd_offer(ctx: Context, argv: list[str], *, facts=None, handoff=None) -> int:
    parser = _Parser(prog="unifier offer")
    parser.add_argument("file", nargs="?")
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--host", action="store_true")
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--keep-open", action="store_true")
    args = parser.parse_args(argv)
    modes = [args.yes, args.host, args.setup, args.remove]
    if sum(bool(item) for item in modes) > 1:
        raise UnifierError(ctx.t("error.arguments", detail="choose only one of --yes, --host, --setup, --remove"))
    from unifier.compatibility import observe_host
    facts = facts or observe_host()
    handoff = handoff or _handoff
    applications, mime_root = _offer_dirs(ctx)
    if args.setup or args.remove:
        if args.remove:
            remove_registration(ctx.root, applications, mime_root)
            _refresh_desktop(applications, mime_root)
            ctx.print(ctx.t("offer.removed"))
            return 0
        formats = whitelist_formats(facts)
        if not formats:
            ctx.print(ctx.t("offer.nothing"))
            return 0
        write_registration(ctx.root, applications, mime_root, facts=facts,
                           launcher=str(launcher_path()), query_default=_query_default)
        _refresh_desktop(applications, mime_root)
        ctx.print(ctx.t("offer.registered", formats=", ".join(formats)))
        return 0
    if not args.file:
        raise UnifierError(ctx.t("error.arguments", detail="a package file is required"))
    try:
        route = route_package(args.file, ctx.catalog()[0]["targets"])
    except UnifierError as exc:
        raise UnifierError(ctx.t("install.package_error", detail=str(exc))) from exc
    decision = admission(route, facts)
    mime = None
    from unifier.package_offer import FORMAT_MIMES
    if route["format"] in FORMAT_MIMES:
        mime = FORMAT_MIMES[route["format"]][0]
    if not decision["enter"]:
        key = "offer.host_native" if decision["reason"] == "host-native" else "offer.outside"
        ctx.print(ctx.t(key))
        if args.json:
            _emit(ctx, {"schema_version": 1, "enter": False, **decision, "path": route["path"]})
        return handoff(ctx, route["path"], mime)
    if args.host:
        return handoff(ctx, route["path"], mime)
    if args.yes:
        return _install_from_offer(ctx, route["path"], keep_open=args.keep_open and not args.json)
    if not ctx.is_tty():
        raise UnifierError(ctx.t("offer.noninteractive"))
    ctx.print(ctx.t("offer.title"))
    ctx.print(ctx.t("offer.file", name=Path(route["path"]).name))
    ctx.print(ctx.t("offer.format", format=route["format"]))
    ctx.print(ctx.t("offer.target", targets=", ".join(decision["targets"])))
    ctx.print(ctx.t("offer.choice_y"))
    ctx.print(ctx.t("offer.choice_n"))
    ctx.print(ctx.t("offer.prompt"))
    try:
        answer = ctx.ask("")
    except EOFError as exc:
        raise UnifierError(ctx.t("error.eof")) from exc
    try:
        choice = parse_choice(answer)
    except UnifierError as exc:
        raise UnifierError(ctx.t("offer.bad_choice")) from exc
    if choice == "host":
        return handoff(ctx, route["path"], mime)
    return _install_from_offer(ctx, route["path"], keep_open=args.keep_open and not args.json)


def _install_from_offer(ctx: Context, path: str, *, keep_open: bool) -> int:
    if not keep_open or not ctx.is_tty():
        return cmd_install(ctx, [path])
    code = _invoke_shortcut(ctx, cmd_install, [path])
    ctx.print(ctx.t("offer.install_failed" if code else "offer.install_completed", code=code))
    try:
        ctx.ask(ctx.t("offer.install_close"))
    except (EOFError, KeyboardInterrupt):
        pass
    return code


def launcher_path():
    return Path(__file__).resolve().parents[2] / "bin" / "unifier"


def _wants_help(argv: list[str]) -> bool:
    head = argv[:argv.index("--")] if "--" in argv else argv
    return any(item in {"-h", "--help"} for item in head[1:])


def _split_double_dash(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" not in argv:
        return list(argv), []
    index = argv.index("--")
    return list(argv[:index]), list(argv[index + 1:])


def _invoke_shortcut(context, handler, argv):
    try:
        return handler(context, argv)
    except UnifierError as exc:
        text = str(exc)
        if text.startswith("\x00argument\x00"):
            text = context.t("error.arguments", detail=text[len("\x00argument\x00"):])
        print(f"unifier: {text}", file=context.err)
        return 1
    except Exception as exc:  # runtime adapters may raise LabError or other RuntimeError
        print(f"unifier: {exc}", file=context.err)
        return 1


def build_context(root: Path | None = None, *, out=None, err=None,
                  runtime=None, config=None, lock=None, input_fn=None,
                  stdin_isatty=None, environ=None,
                  locale_files=None, service_kwargs=None) -> Context:
    return Context(
        root=Path(root or installation_root()),
        config=config, lock=lock, runtime=runtime,
        environ=environ, locale_files=locale_files,
        stdin_isatty=stdin_isatty, input_fn=input_fn,
        service_kwargs=service_kwargs or {},
        out=out or sys.stdout, err=err or sys.stderr)


def main(argv: list[str], *, context: Context | None = None,
         context_factory=None, out=None, err=None) -> int:
    argv = list(argv)
    if not argv:
        raise UnifierError("a command is required")
    command = argv[0]
    handlers = {
        "init": cmd_init,
        "configure": cmd_configure,
        "list": cmd_list,
        "status": cmd_status,
        "dirs": cmd_dirs,
        "path": cmd_path,
        "ls": cmd_ls,
        "install": cmd_install,
        "offer": cmd_offer,
        "run": cmd_run,
        "start": cmd_start,
        "stop": cmd_stop,
        "adopt": cmd_adopt,
        "language": cmd_language,
    }
    if command == "help":
        if context is not None:
            return help_command(argv[1:], out=context.out, err=context.err,
                                root=context.root,
                                language=context.language())
        return help_command(argv[1:], out=out or sys.stdout, err=err or sys.stderr)
    if _wants_help(argv):
        if context is not None:
            return help_command([command], out=context.out, err=context.err,
                                root=context.root,
                                language=context.language())
        return help_command([command], out=out or sys.stdout,
                            err=err or sys.stderr)
    if command not in handlers:
        raise UnifierError(f"unknown command {command}")
    if context is None:
        context = (context_factory or build_context)(out=out, err=err)
    head = argv[:argv.index("--")] if "--" in argv else argv
    context.json_mode = "--json" in head
    if context.out is sys.stdout and out is not None:
        context.out = out
    if context.err is sys.stderr and err is not None:
        context.err = err
    return _invoke_shortcut(context, handlers[command], argv[1:])


# --------------------------------------------------------------------------
# Help

_HELP = {
    "init": {
        "zh-CN": (
            "unifier init [--target TARGET ...] [--none] [--desktop] [--network pasta|none] [--yes]",
            [
                ("--target TARGET", "选择发行版；可重复。只准备所选项，不拉取未选项。"),
                ("--none", "明确选择空集合：只安装 CLI，容器数为 0。"),
                ("--desktop", "显式创建桌面环境；默认无桌面，pasta + keep-id。"),
                ("--network pasta|none", "默认 pasta，安装软件需要网络。"),
                ("--yes", "跳过确认（非交互模式必须显式选择）。"),
            ],
            ["unifier init", "unifier init --target debian13 --target arch --yes"],
        ),
        "en": (
            "unifier init [--target TARGET ...] [--none] [--desktop] [--network pasta|none] [--yes]",
            [
                ("--target TARGET", "select a distribution; repeatable. Only selected images are prepared."),
                ("--none", "explicitly select nothing: CLI only, zero containers."),
                ("--desktop", "explicitly create a desktop environment; default is headless pasta + keep-id."),
                ("--network pasta|none", "defaults to pasta; package install needs network."),
                ("--yes", "skip confirmation (non-interactive use still needs an explicit selection)."),
            ],
            ["unifier init", "unifier init --target debian13 --target arch --yes"],
        ),
    },
    "configure": {
        "zh-CN": (
            "unifier configure [--target TARGET ...] [--none] [--desktop] [--yes]",
            [
                ("--target TARGET", "新的启用集合；取消勾选不停止、不删除已有容器。"),
                ("--none", "停用全部快捷入口，但保留已有容器和数据。"),
                ("--desktop", "新增项是否创建桌面环境；已存在项不会重建。"),
            ],
            ["unifier configure --target debian13", "unifier configure --none"],
        ),
        "en": (
            "unifier configure [--target TARGET ...] [--none] [--desktop] [--yes]",
            [
                ("--target TARGET", "the new enabled set; unchecking never stops or deletes a container."),
                ("--none", "disable all shortcuts while retaining existing containers and data."),
                ("--desktop", "desktop choice for newly prepared items; existing items are not rebuilt."),
            ],
            ["unifier configure --target debian13", "unifier configure --none"],
        ),
    },
    "list": {
        "zh-CN": ("unifier list [--json]",
                  [("--json", "输出稳定的机器字段。")],
                  ["unifier list", "unifier list --json"]),
        "en": ("unifier list [--json]",
               [("--json", "emit stable machine fields.")],
               ["unifier list", "unifier list --json"]),
    },
    "dirs": {
        "zh-CN": ("unifier dirs [TARGET] [--json]",
                  [("--json", "输出目标、目录、files 绑定和视图状态。")],
                  ["unifier dirs", "unifier dirs debian13 --json"]),
        "en": ("unifier dirs [TARGET] [--json]",
               [("--json", "emit target, directories, files mount and view state.")],
               ["unifier dirs", "unifier dirs debian13 --json"]),
    },
    "path": {
        "zh-CN": ("unifier path TARGET --files [--writable]",
                  [("--files", "只输出已核验的宿主共享目录。优先专用目录，其次唯一可用共享目录。"),
                   ("--writable", "仅选择可写目录。")],
                  ["unifier path debian13 --files"]),
        "en": ("unifier path TARGET --files [--writable]",
               [("--files", "print the verified private share or the only available shared directory."),
                ("--writable", "select a writable directory only.")],
               ["unifier path debian13 --files"]),
    },
    "ls": {
        "zh-CN": ("unifier ls TARGET [GUEST_PATH] [--json]",
                  [("GUEST_PATH", "客体绝对路径，默认 /；按客体语义解析链接和 ..。"),
                   ("--json", "输出 path/entries/via 等稳定字段。")],
                  ["unifier ls debian13", "unifier ls debian13 /usr"]),
        "en": ("unifier ls TARGET [GUEST_PATH] [--json]",
               [("GUEST_PATH", "absolute guest path, default /; symlinks and .. use guest semantics."),
                ("--json", "emit stable path/entries/via fields.")],
               ["unifier ls debian13", "unifier ls debian13 /usr"]),
    },
    "install": {
        "zh-CN": ("unifier install [--target TARGET] [--exec PATH] [--app ID] [--package NAME] [--gui] [--register-only] FILE|PACKAGE ... [-- ARGS]",
                  [
                      ("--target", "只在已启用发行版中选择；未启用时提示 configure，不自动建实例。"),
                      ("--exec PATH", "显式应用入口；可用路径或客体命令名。"),
                      ("--app ID", "应用登记标识；默认取入口 basename。"),
                      ("--package NAME", "本地包的已安装包名，用于从包文件发现入口。"),
                      ("--register-only PACKAGE", "从一个已安装的软件包登记入口并发布菜单，适用于补发菜单。"),
                      ("--gui", "声明桌面应用；无桌面容器会拒绝而不是静默运行。"),
                      ("软件列表", "安装成功后写入宿主应用菜单。不改文件的默认打开方式；安装包标明不显示的入口不会写入。只有自动补运行库没有收敛时，包仍会登记并写入软件列表，但命令仍以失败退出。"),
                  ],
                  ["unifier install ./软件.deb --exec /usr/bin/freecad",
                   "unifier install --target arch freecad --exec /usr/bin/freecad",
                   "unifier install --register-only --target arch freecad"]),
        "en": ("unifier install [--target TARGET] [--exec PATH] [--app ID] [--package NAME] [--gui] [--register-only] FILE|PACKAGE ... [-- ARGS]",
               [
                   ("--target", "only an enabled distribution is used; otherwise configure is required and no instance is created."),
                   ("--exec PATH", "explicit application entry; a path or guest command name."),
                   ("--app ID", "registered application id; defaults to the entry basename."),
                   ("--package NAME", "installed package name for a local file, used for entry discovery."),
                   ("--register-only PACKAGE", "register the entry and publish the menu for one installed package."),
                   ("--gui", "declare a desktop application; a headless container is refused rather than run silently."),
                   ("application menu", "a successful install adds a host menu entry. File associations stay unchanged, and an entry the package marks hidden is not added. When only the automatic runtime-library repair fails to converge, the package is still registered and added to the menu, while the command keeps its failing exit status."),
               ],
               ["unifier install ./software.deb --exec /usr/bin/freecad",
                "unifier install --target arch freecad --exec /usr/bin/freecad",
                "unifier install --register-only --target arch freecad"]),
    },
    "offer": {
        "zh-CN": ("unifier offer FILE | --setup | --remove",
                  [("--yes", "不询问，直接用 Unifier 安装。仅白名单跨派系包有效。"),
                   ("--host", "不询问，交给宿主原来的打开方式。"),
                   ("--keep-open", "在专用安装终端中保留安装结果，等回车关闭。"),
                   ("--setup", "登记外来包的双击入口。宿主自己的包格式不登记。"),
                   ("--remove", "恢复登记前的宿主打开方式。")],
                  ["unifier offer ./软件.deb", "unifier offer --setup"]),
        "en": ("unifier offer FILE | --setup | --remove",
               [("--yes", "skip the question and install with Unifier. Only a cross-family whitelist package is accepted."),
                ("--host", "skip the question and hand the file to the host opener."),
                ("--keep-open", "keep the result in the installer terminal until Enter is pressed."),
                ("--setup", "register the double-click entry for foreign packages. The host's own format is not registered."),
                ("--remove", "restore the host openers saved before registration.")],
               ["unifier offer ./software.deb", "unifier offer --setup"]),
    },
    "run": {
        "zh-CN": ("unifier run [--target TARGET] APP [-- ARGS]",
                  [("--target", "指定发行版；省略时要求应用只属于一个已启用发行版。")],
                  ["unifier run --target arch freecad -- --help"]),
        "en": ("unifier run [--target TARGET] APP [-- ARGS]",
               [("--target", "choose a distribution; without it the app must exist in exactly one enabled distribution.")],
               ["unifier run --target arch freecad -- --help"]),
    },
    "start": {
        "zh-CN": ("unifier start TARGET",
                  [], ["unifier start arch"]),
        "en": ("unifier start TARGET", [], ["unifier start arch"]),
    },
    "stop": {
        "zh-CN": ("unifier stop TARGET",
                  [], ["unifier stop arch"]),
        "en": ("unifier stop TARGET", [], ["unifier stop arch"]),
    },
    "adopt": {
        "zh-CN": ("unifier adopt --target TARGET --root LEGACY_ROOT --environment ID [--json]",
                  [("--root", "旧部署的安装根；只读取记录，不修改旧状态/挂载/菜单。"),
                   ("--environment", "旧环境 ID，作为内部身份引用；用户面仍只按 target。")],
                  ["unifier adopt --target debian13 --root /home/user/.local/share/multilinux --environment freecad"]),
        "en": ("unifier adopt --target TARGET --root LEGACY_ROOT --environment ID [--json]",
               [("--root", "legacy installation root; records are read only, old state/mounts/menus are not modified."),
                ("--environment", "legacy environment id used as an internal identity reference; users still select only target.")],
               ["unifier adopt --target debian13 --root /home/user/.local/share/multilinux --environment freecad"]),
    },
    "language": {
        "zh-CN": ("unifier language [zh-CN|zh|en|auto] [--json]",
                  [("zh-CN|zh", "持久选择中文。"), ("en", "持久选择英文。"),
                   ("auto", "清除显式偏好，按 LC_ALL→LC_MESSAGES→LANG→locale 文件→英文 判断。")],
                  ["unifier language", "unifier language zh-CN", "unifier language auto"]),
        "en": ("unifier language [zh-CN|zh|en|auto] [--json]",
               [("zh-CN|zh", "persist Chinese."), ("en", "persist English."),
                ("auto", "clear the explicit preference and re-detect via LC_ALL→LC_MESSAGES→LANG→locale files→English.")],
               ["unifier language", "unifier language en", "unifier language auto"]),
    },
}


def help_command(argv: list[str], *, out=None, err=None, root: Path | None = None,
                 language: str | None = None, parser_factory=None) -> int:
    out = out or sys.stdout
    err = err or sys.stderr
    if language is None:
        info = i18n.resolve(Path(root) if root is not None else installation_root())
        language = info["language"]
    command = argv[0] if argv else None
    if command is None:
        print(i18n.full_help(language), file=out)
        return 0
    if command in {"help", "language"}:
        if command == "help":
            print(i18n.full_help(language), file=out)
            return 0
        usage = _HELP["language"][language]
        return _print_help(language, command, usage, out)
    if command not in _HELP:
        if parser_factory is None:
            print(i18n.translate(language, "help.unknown", command=command), file=err)
            return 2
        try:
            import contextlib
            parser = parser_factory()
            with contextlib.redirect_stdout(out):
                parser.parse_args([*argv, "--help"])
            return 0
        except SystemExit as exc:
            if exc.code in (0, None):
                return 0
            print(i18n.translate(language, "help.unknown", command=command), file=err)
            return 2
        except Exception:
            print(i18n.translate(language, "help.unknown", command=command), file=err)
            return 2
    return _print_help(language, command, _HELP[command][language], out)


def _print_help(language: str, command: str, spec, out) -> int:
    usage, options, examples = spec
    print(i18n.translate(language, "help.title", command=command), file=out)
    print("", file=out)
    print(i18n.translate(language, "help.usage_prefix") + usage, file=out)
    if options:
        print("", file=out)
        print(i18n.translate(language, "help.options"), file=out)
        for flag, detail in options:
            print(f"  {flag:<18} {detail}", file=out)
    if examples:
        print("", file=out)
        print(i18n.translate(language, "help.examples"), file=out)
        for example in examples:
            print(f"  {example}", file=out)
    return 0
