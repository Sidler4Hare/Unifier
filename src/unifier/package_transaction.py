"""Prepare, execute, and finalize one package transaction under service-owned locks."""

from dataclasses import asdict
from pathlib import Path
import uuid

from unifier import package_journal
from unifier.catalog import plan_change
from unifier.desktop_clients import application_clients, client_packages
from unifier.desktop_vendor import local_install_prerequisites
from unifier.environment_contract import validate_env_id
from unifier.errors import UnifierError
from unifier.identity import binding_for
from unifier.package_observation import unavailable_host
from unifier.package_recovery import (
    arch_post_transaction_recovery, arch_transaction_marker, output_tail,
)
from unifier.packages import (
    TIMEOUT_PACMAN_KEY,
    driver_for,
    explain_native_error,
    inventory_has,
    local_package_path,
    maintenance_findings,
    merge_maintenance_findings,
    package_command,
    package_environment,
    transaction_verdict,
    validate_package_name,
    validate_use_argv,
)
from unifier.runtime import PodmanRuntime, decode_output


def syu_succeeded(native: list) -> bool:
    return any(
        (step.get("argv") or [])[:2] == ["pacman", "-Syu"] and step.get("exit_code") == 0
        for step in native)


def _keyring_ready(native: list) -> bool:
    """True only after the keyring commands queued for this transaction succeeded."""
    init = [step for step in native if (step.get("argv") or [])[:2] == ["pacman-key", "--init"]]
    if not init or init[-1].get("exit_code") != 0:
        return False
    populate = [step for step in native
                if (step.get("argv") or [])[:2] == ["pacman-key", "--populate"]]
    return not populate or populate[-1].get("exit_code") == 0


def _record_finalize_error(txn: dict, step: str, exc: BaseException) -> None:
    """Record a finalization step failure without aborting the remaining steps."""
    txn.setdefault("finalize_errors", []).append(
        {"step": step, "error": f"{type(exc).__name__}: {exc}"[-1500:]})
    txn["recovery_required"] = True


def run_locked(service, env_id: str, operation: str, packages: list[str], *,
               use_argv=None, local_file: str | None = None, interactive=False,
               stream=False, repair_dependencies=False, libraries=(),
               dependency_input=None, application_runtime=False, desktop_clients=()) -> dict:
    record = service.store.load(validate_env_id(env_id))
    service._require_host_compatibility(record)
    service._package_container_ref(record)
    decision = plan_change(record, operation="package-change")
    if decision["action"] != "upgrade":
        raise UnifierError(decision.get("reason") or "this environment cannot be upgraded in place")
    names = [validate_package_name(name) for name in packages]
    if desktop_clients and not record.get("desktop"):
        raise UnifierError("desktop clients require an environment created with --desktop")
    clients = desktop_clients or (application_clients(record, names) if application_runtime else ())
    providers = client_packages(record["target"], clients)
    if desktop_clients:
        names = list(providers.values())
    use_argv = validate_use_argv(use_argv)
    if operation in {"install", "remove"} and not names:
        raise UnifierError(f"{operation} requires at least one package name")
    if operation == "update" and names:
        raise UnifierError("update upgrades the whole environment and does not take package names")
    network = record["runtime"].get("network", "none")
    if operation in {"install", "update", "install-local", "repair-dependencies"} and network != "pasta":
        raise UnifierError(
            f"environment {record['id']} was created with network {network}; "
            "install and update need --network pasta at create time")
    local_path = None
    if operation == "install-local":
        if not local_file:
            raise UnifierError("install-local requires a package file")
        local_path = local_package_path(record["target"], local_file)

    txn = {
        "schema_version": 1,
        "id": "txn" + uuid.uuid4().hex[:12],
        "environment_id": record["id"],
        "operation": operation,
        "packages": names,
        "interactive": bool(interactive),
        "stream": bool(stream),
        "dependency_repair": {"status": "not-requested"},
        "use_command": use_argv,
        "local_file": None if local_path is None else str(local_path),
        "revision_before": {
            "writable_revision": record["writable_revision"],
            "content_revision": int(record.get("content_revision") or 1),
            "package_inventory_sha256": record["package_inventory_sha256"],
        },
        "native": [],
        "native_error": None,
        # Hooks run after the package files are written. Their failures do
        # not uninstall the package, so they are recorded beside the phase
        # and summarized by the top-level verdict.
        "maintenance": {
            "status": "ok",
            "items": [],
            "system_integration": "unverified",
            "full_init": False,
        },
        "verdict": None,
        "verification": None,
        "host_entries": None,
        "host_packages": None,
        "other_environments": None,
        "revision_after": None,
        "phase": "running",
        "environment_binding": binding_for(record),
        "recovery_required": False,
        "inventory_status": "checking",
        "failure_phase": None,
        "started_at": service.clock(),
        "finished_at": None,
    }
    inventory_text = None
    if dependency_input is not None:
        txn["dependency_input"] = dependency_input
    if providers:
        txn["desktop_clients"] = {"source": "explicit" if desktop_clients else "application",
                                  "packages": providers}
    was_running = None
    other_before = None
    host_before = None
    service.store.save_transaction(txn)
    try:
        try:
            other_before = service._other_fingerprints(record["id"])
        except Exception as exc:
            txn['observation_preflight_error'] = str(exc)[-500:]
        try:
            host_before = service.host_observer.snapshot()
        except Exception as exc:
            host_before = unavailable_host(exc)
        service._require_record_binding(service.store.load(record['id']), txn['environment_binding'])
        service._package_container_ref(record)
        was_running = service._ensure_running(record["id"])
        record = service.store.load(record["id"])
        service._require_record_binding(record, txn['environment_binding'])
        runtime_packages = []
        desktop_install = (application_runtime and bool(record.get("desktop"))
                           and operation in {"install", "install-local"})
        needs_python = application_runtime and not service._application_python_ready(record)
        minimal_dependencies = ()
        missing_clients = []
        if providers or (needs_python and operation == "repair-dependencies"):
            installed = service._read_inventory_record(record)
            if operation == "repair-dependencies":
                missing = [name for name in names if not inventory_has(installed, name)]
                if missing:
                    raise UnifierError(
                        "dependency repair requires installed packages: " + ", ".join(missing))
            missing_clients = [name for name in providers.values() if not inventory_has(installed, name)]
            if record["target"] == "debian13":
                minimal_dependencies = tuple(missing_clients)
            else:
                runtime_packages.extend(missing_clients)
        if needs_python:
            runtime_packages.append(driver_for(record).application_python_package)
        if desktop_install and not service._desktop_fonts_ready(record):
            runtime_packages.extend(("fontconfig", driver_for(record).application_font_package))
        if not desktop_clients or missing_clients:
            keyring_populate, container_path = _prepare_native_steps(
                service, record, operation, local_path)
            steps = plan_native_steps(
                record, operation, [] if desktop_clients else names,
                keyring_populate=keyring_populate, container_path=container_path,
                runtime_packages=runtime_packages, minimal_dependencies=minimal_dependencies,
                application_runtime=application_runtime)
            service._run_native(record, txn, steps)
        if (application_runtime and txn["failure_phase"] is None
                and not service._application_python_ready(record)):
            txn["failure_phase"] = "application-runtime"
            txn["native_error"] = "guest Python 3 is still missing after installation"
        if desktop_install and txn["failure_phase"] is None and not service._desktop_fonts_ready(record):
            txn["failure_phase"] = "application-runtime"
            txn["native_error"] = "guest fontconfig cannot find a usable sans-serif font after installation"
        if repair_dependencies and txn["failure_phase"] is None:
            from unifier.dependencies import DependencyRepair
            try:
                DependencyRepair(service, record, txn).run(names, libraries)
            except Exception as exc:
                txn["failure_phase"] = txn["failure_phase"] or "dependency-repair"
                txn["native_error"] = str(exc)[-1500:]
        if _keyring_ready(txn["native"]):
            service._mark_keyring(record["id"])
            record = service.store.load(record["id"])
        inventory_text = service._read_inventory_record(record)
        if providers:
            present = {client: inventory_has(inventory_text, name) for client, name in providers.items()}
            txn["desktop_clients"]["inventory"] = present
            absent = [providers[client] for client, found in present.items() if not found]
            if absent and txn["failure_phase"] is None:
                txn["failure_phase"] = "application-runtime"
                txn["native_error"] = "guest desktop client packages are missing: " + ", ".join(absent)
            if desktop_clients and not missing_clients and not absent:
                txn["desktop_clients"]["status"] = "already-installed"
        txn["verification"] = service._verify(record, operation, names, use_argv, inventory_text, txn)
        if txn["failure_phase"] is None and not txn["verification"]["ok"]:
            txn["failure_phase"] = "verification"
    except Exception as exc:
        if txn["failure_phase"] is None:
            txn["failure_phase"] = "runtime"
            txn["native_error"] = str(exc)[-1500:]
    finally:
        service._finalize_package_transaction(
            record, txn, inventory_text, was_running, other_before, host_before)
    return txn


def finalize(service, record, txn, inventory_text, was_running, other_before, host_before):
    """Attempt independent cleanup stages, preserving a durable failure journal.

    A failed stage cannot prove reconciliation. Keep subsequent package
    writes blocked until explicit recovery, without masking the primary
    native error. Persistent journal write failure is raised to the caller;
    the already-saved running journal remains the recovery barrier.
    """
    def attempt(stage, action, default=None):
        try:
            return action()
        except Exception as exc:
            _record_finalize_error(txn, stage, exc)
            legacy_field = {"inventory-read": "inventory_error",
                            "inventory-verify": "inventory_error",
                            "database-audit": "recovery_error", "stop": "stop_error"}.get(stage)
            if legacy_field:
                txn[legacy_field] = str(exc)[-1500:]
            txn["inventory_status"] = "unknown"
            if txn["failure_phase"] is None:
                txn["failure_phase"] = "finalization"
            return default

    finalization_finished = False
    try:
        uncertain = any(
            step.get("state") in {"starting", "running", "interrupted"}
            and step.get("cleanup") not in {"gone", "container-stopped"}
            for step in txn["native"])
        post_transaction = (record["target"] == "arch"
                            and arch_post_transaction_recovery(txn))
        if post_transaction:
            txn["post_transaction_recovery"] = post_transaction
        dirty_journal = False
        if not uncertain and record["target"] in package_journal.TARGETS and any(
                step.get("state") == "interrupted" for step in txn["native"]):
            journal = attempt("database-journal", lambda: service._native_package_journal(record, "probe", txn))
            dirty_journal = journal != "clean"
        if uncertain or post_transaction or dirty_journal:
            txn["recovery_required"] = True
            txn["inventory_status"] = "unknown"
        elif inventory_text is None and txn["native"]:
            inventory_text = attempt(
                "inventory-read", lambda: service._read_inventory_record(
                    service.store.load(record["id"])))
            if inventory_text is not None:
                txn["verification"] = attempt(
                    "inventory-verify", lambda: service._verify(
                        record, txn["operation"], txn["packages"], [], inventory_text, txn))
        if txn["failure_phase"] is not None and txn["native"] and not uncertain and not dirty_journal:
            attempt("database-audit", lambda: service._audit_package_database(record, txn))
        if (inventory_text is not None and not uncertain and not post_transaction and not dirty_journal
                and not txn.get("finalize_errors")):
            txn["inventory_status"] = "verified"
        elif txn["native"]:
            txn["inventory_status"] = "unknown"
        if was_running is False and not uncertain:
            def stop_owned():
                service._package_container_ref(record)
                service.stop(record["id"], package_lock_held=True)
            attempt("stop", stop_owned)
        record = attempt("record-read", lambda: service.store.load(record["id"]), record)
        if (not uncertain and not post_transaction and not dirty_journal
                and (inventory_text is not None or syu_succeeded(txn["native"]))):
            record = attempt(
                "inventory-commit", lambda: service._commit_inventory(record, inventory_text, txn), record)
        try:
            service._finish_isolation(txn, other_before, host_before, record["id"])
        except Exception as exc:
            txn['isolation_observation'] = {
                'schema_version': 1, 'scope': 'non-target-audit', 'status': 'unavailable',
                'attribution': 'unattributed', 'problems': [str(exc)[-500:]]}
        if txn.get("verification") and not txn["verification"].get("ok") and txn["failure_phase"] is None:
            txn["failure_phase"] = "verification"
        if txn.get("verification") is None and txn["failure_phase"] is None:
            txn["failure_phase"] = "runtime"
        clients_present = txn.get("desktop_clients", {}).get("status") == "already-installed"
        if (not txn["native"] and txn["failure_phase"] is None
                and txn["operation"] != "repair-dependencies" and not clients_present):
            txn["failure_phase"] = "runtime"
            txn["native_error"] = "no native package command was run"
        txn["revision_after"] = {
            "writable_revision": record.get("writable_revision"),
            "content_revision": int(record.get("content_revision") or 1),
            "package_inventory_sha256": record.get("package_inventory_sha256"),
        }
        finalization_finished = True
    finally:
        # Always attempt the journal, even if an unexpected error interrupts
        # the coordinator itself. Never turn an incomplete cleanup into PASS.
        if not finalization_finished:
            txn["recovery_required"] = True
            txn["inventory_status"] = "unknown"
            txn["failure_phase"] = txn["failure_phase"] or "finalization"
        txn["finished_at"] = attempt("timestamp", service.clock, txn.get("started_at"))
        if txn["native"]:
            attempt("record-save", lambda: service._save_package_recovery_state(
                txn["environment_binding"], status=txn["inventory_status"],
                required=txn["recovery_required"]))
        txn["phase"] = ("needs-recovery" if txn["recovery_required"] else
                        "completed" if txn["failure_phase"] is None else "failed")
        maintenance = txn.get("maintenance") or {}
        maintenance.update(status="warnings" if maintenance.get("items") else "ok",
                           system_integration="unverified", full_init=False)
        txn["maintenance"] = maintenance
        txn["verdict"] = transaction_verdict(txn["phase"], maintenance)
        if txn["operation"] in {"install", "install-local", "repair-dependencies"}:
            from unifier.package_install_diagnostics import installation_guidance
            if guidance := installation_guidance(txn, record["target"]):
                txn["dependency_guidance"] = guidance
        try:
            service.store.save_transaction(txn)
        except Exception as exc:
            raise UnifierError(
                f"cannot persist final package transaction {txn['id']}; "
                "recovery is required; inspect the existing transaction journal") from exc
    service.log(f"{txn['environment_id']}: {txn['operation']} {txn['phase']} {txn['id']}")


def _prepare_native_steps(service, record: dict, operation: str,
                          local_path: Path | None) -> tuple[bool | None, str | None]:
    """Probe before staging and finish both before the first native write."""
    keyring_populate = None
    if (driver_for(record).needs_keyring
            and operation in {"install", "update", "install-local", "repair-dependencies"}
            and not (record.get("arch") or {}).get("keyring_initialized_at")):
        keyring_populate = _arch_needs_populate(service, record)
    container_path = service._stage_local(record, local_path) if operation == "install-local" else None
    return keyring_populate, container_path


def _arch_needs_populate(service, record: dict) -> bool:
    result = service._exec_record(
        record, ["pacman-key", "--list-keys"], timeout=TIMEOUT_PACMAN_KEY, native=True)
    if result.returncode != 0:
        return True
    return not result.stdout.strip()


def run_native(service, record: dict, txn: dict, steps: list[tuple[list[str], int]]) -> None:
    for argv, timeout in steps:
        container = service._package_container_ref(record)
        interactive = bool(txn.get("interactive"))
        argv = package_command(argv, interactive=interactive)
        service.log(f"{record['id']}: {' '.join(argv)}")
        step = {"argv": list(argv), "timeout": timeout, "state": "starting",
                "exit_code": None, "stdout_tail": "", "stderr_tail": ""}
        txn["native"].append(step)
        service.store.save_transaction(txn)

        def ready(guest):
            step.update(state="running", guest=asdict(guest))
            service.store.save_transaction(txn)

        try:
            if hasattr(service.runtime, "package_exec"):
                runtime = record["runtime"]
                tracking = {"on_guest": ready} if isinstance(service.runtime, PodmanRuntime) else {}
                result = service.runtime.package_exec(
                    container, argv, project=runtime["project"],
                    env_id=record["id"], env=package_environment(record, interactive=interactive),
                    interactive=interactive, stream=bool(txn.get("stream")), timeout=timeout,
                    **tracking)
            elif interactive:
                raise UnifierError("runtime does not support native interactive package commands")
            else:
                result = service._exec_record(record, argv, timeout=timeout,
                                           native=True, pin_package=True)
        except BaseException as exc:
            stdout = decode_output(getattr(exc, "stdout", ""))
            stderr = decode_output(getattr(exc, "stderr", ""))
            step.update(state="interrupted", cleanup=getattr(exc, "cleanup", "unknown"),
                        exit_code=getattr(exc, "returncode", None),
                        stdout_tail=output_tail(stdout), stderr_tail=output_tail(stderr), error=str(exc))
            if record["target"] == "arch" and arch_transaction_marker(stdout, stderr):
                step["transaction_marker"] = True
            if getattr(exc, "signum", None):
                txn["exit_code"] = 128 + exc.signum
            service.store.save_transaction(txn)
            raise
        step.update(state="finished", exit_code=result.returncode,
                    stdout_tail=output_tail(result.stdout or ""), stderr_tail=output_tail(result.stderr or ""))
        if (record["target"] == "arch"
                and arch_transaction_marker(result.stdout or "", result.stderr or "")):
            step["transaction_marker"] = True
        service.store.save_transaction(txn)
        # The saved tails are for reading; the hook scan uses the complete
        # output because a failure line can sit in the leading part.
        findings = maintenance_findings(
            record["target"], result.stdout or "", result.stderr or "")
        if findings:
            maintenance = txn["maintenance"]
            maintenance["items"] = merge_maintenance_findings(
                maintenance.get("items"), findings)
            maintenance["status"] = "warnings"
        if result.returncode != 0:
            txn["native_error"] = explain_native_error(
                (result.stderr or "") + "\n" + (result.stdout or ""))
            from unifier.package_install_diagnostics import native_findings
            if findings := native_findings(
                    record["target"], result.stdout or "", result.stderr or ""):
                txn["native_dependencies"] = findings
            txn["failure_phase"] = "native-command"
            return


def plan_native_steps(record: dict, operation: str, names: list[str], *,
                      keyring_populate: bool | None, container_path: str | None,
                      runtime_packages=(), minimal_dependencies=(),
                      application_runtime=False) -> list[tuple[list[str], int]]:
    """Build native argv using the evidence and guest path prepared by the caller."""
    driver = driver_for(record)
    steps: list[tuple[list[str], int]] = []
    if keyring_populate is not None:
        steps.extend(driver.bootstrap(populate=keyring_populate))
    # Only the Debian driver accepts an APT no-recommends install step.
    options = {"minimal_dependencies": minimal_dependencies} if minimal_dependencies else {}
    if operation == "install":
        extra = [package for package in runtime_packages if package not in names]
        steps.extend(driver.install([*names, *extra], **options))
    elif operation == "update":
        steps.extend(driver.update())
    elif operation == "remove":
        steps.extend(driver.remove(names))
    elif operation == "install-local":
        options["dependencies"] = runtime_packages
        if application_runtime and (required := local_install_prerequisites(record["target"], names)):
            options["prerequisites"] = required
        steps.extend(driver.local(container_path, **options))
    elif operation == "repair-dependencies" and (runtime_packages or minimal_dependencies):
        steps.extend(driver.install(runtime_packages, **options))
    return steps
