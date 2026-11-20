"""Select, install and register an application in an existing target."""

from dataclasses import dataclass, field
import hashlib
import os
from pathlib import Path
import re

from unifier.desktop_vendor import application_exit_policy
from unifier.errors import UnifierError
from unifier.packages import validate_package_name
from unifier.routing import route_package


@dataclass
class InstallRequest:
    items: list[str]
    argv: list[str]
    target: str | None
    executable: str | None
    app_id: str | None
    package: str | None
    gui: bool
    register_only: bool
    package_options: dict


@dataclass
class InstallResult:
    exit_code: int
    target: str
    environment_id: str
    transaction: dict | None
    details: dict = field(default_factory=dict)
    announce_app: bool = False


def _app_id(executable: str, explicit: str | None,
            suggested: str | None = None) -> str:
    if explicit:
        return explicit
    name = (suggested or Path(executable).name).lower()
    # The record store keeps application ids to lowercase letters, digits and
    # hyphens.  A dotted desktop id such as com.alibabainc.dingtalk folds into
    # hyphens here instead of producing an id the store would reject later.
    name = re.sub(r"[^a-z0-9]+", "-", name).strip("-")
    if not name:
        name = "app"
    elif not "a" <= name[0] <= "z":
        name = f"app-{name}"
    return name[:32].rstrip("-") or "app"


def _internal_app_id(target: str, alias: str) -> str:
    digest = hashlib.sha1((target + ":" + alias).encode("utf-8")).hexdigest()[:6]
    prefix = re.sub(r"[^a-z0-9]+", "-", alias.lower()).strip("-") or "app"
    if not "a" <= prefix[0] <= "z":
        prefix = f"app-{prefix}"
    return f"{(prefix[:25].rstrip('-') or 'app')}-{digest}"


def _launch_matches(launch: dict, executable: str, argv: list[str]) -> bool:
    return (launch.get("executable") == executable
            and list(launch.get("argv") or []) == list(argv))


def _can_register_after_dependency_failure(txn: dict) -> bool:
    verification = txn.get("verification") or {}
    packages = verification.get("packages") or {}
    repair = txn.get("dependency_repair") or {}
    return (txn.get("phase") == "failed"
            and txn.get("failure_phase") == "dependency-repair"
            and txn.get("recovery_required") is False
            and txn.get("inventory_status") == "verified"
            and not verification.get("isolation")
            and repair.get("status") == "unresolved"
            and (bool(repair.get("unresolved"))
                 or (repair.get("scope") == "package-elf-needed" and bool(repair.get("error"))))
            and bool(packages) and all(value is True for value in packages.values()))


def _publish_installed_menu(ctx, service, launch: dict, alias: str,
                            menu: dict | None) -> dict | None:
    """Put the installed application in the host menu without a file association.

    A package that marks its own desktop entry hidden stays out of the menu.
    An existing menu entry is left in place. Failure here does not undo the
    package transaction or the launch record.
    """
    from unifier.app_entries import host_menu_icon
    exported = launch.get("export") or {}
    desktop_path = exported.get("desktop_path")
    if desktop_path and os.path.isfile(desktop_path):
        ctx.print(ctx.t("install.menu_kept", app=alias))
        return launch
    if menu and (menu.get("no_display") or menu.get("hidden")):
        ctx.print(ctx.t("install.menu_hidden", app=alias))
        return launch
    display_name = (menu or {}).get("name") or alias
    try:
        updated = service.export_app(
            launch["id"], menu_only=True, display_name=display_name,
            icon=host_menu_icon((menu or {}).get("icon")),
            wm_class=(menu or {}).get("wm_class"),
            terminal=bool((menu or {}).get("terminal")),
            extra_fields=(menu or {}).get("localized_names") or None)
    except UnifierError as exc:
        ctx.error(ctx.t("install.menu_failed", app=alias, detail=str(exc)))
        return None
    ctx.print(ctx.t("install.menu_added", app=display_name))
    return updated


def _resolve_target(ctx, request: InstallRequest, enabled: list[str], choose_target):
    if request.register_only or len(request.items) != 1:
        return choose_target(enabled, request.target), None
    first = Path(request.items[0]).expanduser()
    looks_local = ("/" in request.items[0]
                   or request.items[0].endswith((".deb", ".rpm", ".pkg.tar.zst",
                                              ".pkg.tar.xz", ".pkg.tar.gz")))
    if not first.is_file():
        if looks_local:
            raise UnifierError(ctx.t("install.local_missing", file=str(first)))
        return choose_target(enabled, request.target), None
    try:
        route = route_package(str(first), ctx.catalog()[0]["targets"])
    except UnifierError as exc:
        raise UnifierError(ctx.t("install.package_error", detail=str(exc))) from exc
    if request.target and request.target not in route["candidates"]:
        raise UnifierError(ctx.t("install.target_mismatch",
                                     format=route["format"], target=request.target,
                                     candidates=", ".join(route["candidates"])))
    candidates = [name for name in route["candidates"] if name in enabled]
    return choose_target(candidates, request.target), route["path"]


def install_application(ctx, request: InstallRequest, *, enabled: list[str],
                        choose_target, ready_target, choose_entry,
                        internal_app_id=_internal_app_id) -> InstallResult:
    target, local_file = _resolve_target(ctx, request, enabled, choose_target)
    service, env_id = ready_target(target)
    package_name = request.package

    if request.register_only:
        package_name = validate_package_name(request.items[0])
        with service.application_registration_session(env_id):
            query = service.query_package(env_id, package_name, require_installed=True)
            if not query["present"]:
                message = ctx.t("install.package_not_installed", target=target,
                                package=package_name)
                ctx.error(message)
                return InstallResult(1, target, env_id, None, {"error": message})
            return _register(ctx, request, service, env_id, target, local_file,
                             package_name, None, 0, choose_entry, internal_app_id)

    try:
        if local_file:
            if package_name is None:
                package_name = service.local_package_name(env_id, local_file)
            ctx.print(ctx.t("install.local", target=target, file=local_file))
            txn = service.install_local(
                env_id, local_file, package_name=package_name, **request.package_options)
        else:
            ctx.print(ctx.t("install.packages", target=target,
                            packages=", ".join(request.items)))
            txn = service.install_packages(env_id, list(request.items), **request.package_options)
    except UnifierError as exc:
        raise UnifierError(ctx.t("install.failed", target=target,
                                     detail=str(exc))) from exc
    deferred = 0
    from unifier.package_observation import warning
    if message := warning(txn):
        ctx.error(message)
    if txn.get("phase") != "completed":
        detail = txn.get("native_error") or txn.get("failure_phase") or "failed"
        ctx.error(ctx.t("install.transaction_failed",
                        phase=txn.get("failure_phase") or "unknown",
                        detail=detail))
        if guidance := txn.get("dependency_guidance"):
            from unifier.package_install_diagnostics import guidance_messages
            for message in guidance_messages(ctx.t, guidance, service.root):
                ctx.error(message)
        if not _can_register_after_dependency_failure(txn):
            return InstallResult(int(txn.get("exit_code") or 1), target, env_id, txn)
        # The package transaction itself succeeded and only the automatic
        # runtime-library repair failed to converge, so the entry is still
        # registered and published below while the command keeps failing.
        deferred = int(txn.get("exit_code") or 1)
        unresolved = (txn.get("dependency_repair") or {}).get("unresolved") or []
        ctx.print(ctx.t("install.deps_unresolved",
                        libraries=", ".join(unresolved) or detail))
    else:
        ctx.print(ctx.t("install.transaction_ok", transaction=txn.get("id")))
    if (txn.get('dependency_repair') or {}).get('status') == 'bundled-candidates-unverified':
        ctx.print(ctx.t('install.bundled_unverified'))

    return _register(ctx, request, service, env_id, target, local_file,
                     package_name, txn, deferred, choose_entry, internal_app_id)


def _register(ctx, request, service, env_id, target, local_file, package_name,
              txn, deferred, choose_entry, internal_app_id) -> InstallResult:
    record = service.store.load(env_id)
    desktop = bool(record.get("desktop"))
    if request.gui and not desktop:
        message = ctx.t("install.gui_no_desktop", target=target)
        ctx.error(message)
        return InstallResult(1, target, env_id, txn, {"error": message})

    executable = request.executable
    source = "explicit"
    base_argv = None
    suggested = None
    menu = None
    package_desktop_id = None
    if executable:
        executable = service.resolve_executable(env_id, executable)
        base_argv = [executable]
    else:
        package_names = [package_name] if package_name else (
            [] if local_file else list(request.items))
        package_names = [name for name in package_names if name]
        discovered = service.discover_app_entries(env_id, package_names)
        choice = choose_entry(discovered)
        executable = choice.get("executable")
        base_argv = list(choice.get("argv") or [executable])
        suggested = choice.get("desktop_id")
        menu = choice.get("menu")
        package_desktop_id = choice.get("menu_desktop_id", suggested)
        source = choice.get("source") or "discovered"
        if not executable:
            raise UnifierError(ctx.t("install.entry_required",
                                         candidates=str(discovered)))
    if source == "explicit":
        ctx.print(ctx.t("install.entry_explicit", target=target,
                        executable=executable))
    else:
        ctx.print(ctx.t("install.entry_discovered", target=target,
                        executable=executable))
    alias = _app_id(executable, request.app_id, suggested)
    internal_id = internal_app_id(target, alias)
    full_argv = [*base_argv, *request.argv]
    try:
        existing = service.app_by_alias(env_id, alias)
    except UnifierError:
        existing = None
    if existing is not None:
        if request.register_only:
            service._validate_launch_binding(existing, record=record)
        if not _launch_matches(existing, executable, full_argv):
            raise UnifierError(ctx.t("install.app_conflict", app=alias))
        launch = existing
        ctx.print(ctx.t("install.app_reused", app=alias))
    else:
        from unifier.file_chooser import default_portal_profile

        as_host_user = record.get("runtime", {}).get("user_namespace") == "keep-id"
        graphical = request.gui or (menu is not None and not menu.get("terminal"))
        profile = default_portal_profile(
            record, executable, desktop_id=package_desktop_id) if graphical else {}
        execution = {}
        if graphical and (policy := application_exit_policy(target, package_name, full_argv)):
            execution["exit_policy"] = policy
        try:
            launch = service.add_app(
                env_id, internal_id, executable, full_argv,
                alias=alias, as_host_user=as_host_user,
                on_demand=True, desktop_required=False, env_overlay=profile, **execution)
        except UnifierError as exc:
            ctx.error(ctx.t("install.entry_missing", target=target,
                                executable=executable, detail=str(exc)))
            return InstallResult(1, target, env_id, txn, {"error": str(exc)})
        ctx.print(ctx.t("install.app_added", app=alias, target=target,
                        environment=env_id))
    published = _publish_installed_menu(ctx, service, launch, alias, menu)
    details = {
        "app": alias, "app_id": launch.get("id"),
        "executable": executable, "argv": full_argv, "entry_source": source,
        "menu": ((published or {}).get("export") or {}).get("desktop_path"),
    }
    if published is None:
        return InstallResult(1, target, env_id, txn, details)
    return InstallResult(deferred, target, env_id, txn, details, announce_app=True)
