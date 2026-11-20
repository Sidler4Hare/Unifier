"""Reconcile interrupted package writes against their pinned native database."""

import hashlib
import json
import re

from unifier import package_journal
from unifier.errors import UnifierError
from unifier.identity import binding_for, validate_binding
from unifier.packages import driver_for
from unifier.runtime import PodmanRuntime
from unifier.view_state import assert_no_active_views


def output_tail(text: str, limit: int = 4000) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def arch_transaction_marker(stdout: str, stderr: str) -> bool:
    return any(marker in output.lower() for output in (stdout, stderr)
               for marker in ("running post-transaction hooks", "transaction completed"))


def arch_post_transaction_recovery(txn: dict) -> dict | None:
    for index, step in enumerate(txn.get("native", [])):
        if (step.get("argv") or [])[:2] not in (
                ["pacman", "-Syu"], ["pacman", "-U"], ["pacman", "-R"]):
            continue
        code = step.get("exit_code")
        state = step.get("state")
        if state == "finished" and code == 0:
            continue
        marker = step.get("transaction_marker") or arch_transaction_marker(
            step.get("stdout_tail") or "", step.get("stderr_tail") or "")
        if (marker or state in {"starting", "running", "interrupted"}
                or isinstance(code, int) and (code < 0 or 129 <= code <= 192)):
            return {"reason": "post-transaction-unverified", "native_step": index,
                    "commit_evidence": "marker-observed" if marker else "indeterminate"}
    return None


def _canonical_json_sha256(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


def _validate_arch_maintenance_receipt(receipt, binding: dict,
                                      blockers: list[dict]) -> tuple[dict, str]:
    if (not isinstance(receipt, dict)
            or set(receipt) != {"schema_version", "environment_binding", "inventory_sha256",
                                "transactions"}
            or type(receipt["schema_version"]) is not int or receipt["schema_version"] != 1):
        raise UnifierError("invalid package maintenance receipt schema")
    if validate_binding(receipt["environment_binding"]) != binding:
        raise UnifierError("package maintenance receipt environment binding does not match")
    inventory_hash = receipt["inventory_sha256"]
    if not isinstance(inventory_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", inventory_hash):
        raise UnifierError("invalid package maintenance receipt inventory SHA-256")
    expected = {txn["id"]: txn for txn in blockers}
    entries = receipt["transactions"]
    if not expected or not isinstance(entries, list) or len(entries) != len(expected):
        raise UnifierError("package maintenance receipt must cover every post-transaction blocker")
    covered = {}
    for entry in entries:
        if (not isinstance(entry, dict) or set(entry) != {"id", "native_sha256", "hooks"}
                or not isinstance(entry["id"], str) or entry["id"] not in expected
                or entry["id"] in covered):
            raise UnifierError("invalid or duplicate package maintenance receipt transaction")
        txn_id = entry["id"]
        if entry["native_sha256"] != _canonical_json_sha256(expected[txn_id]["native"]):
            raise UnifierError(f"package maintenance receipt native SHA-256 does not match {txn_id}")
        hooks = entry["hooks"]
        if not isinstance(hooks, list) or not 1 <= len(hooks) <= 256:
            raise UnifierError(f"package maintenance receipt needs hook attestations for {txn_id}")
        names = set()
        for hook in hooks:
            if not isinstance(hook, dict) or set(hook) != {"name", "status", "detail"}:
                raise UnifierError(f"invalid package maintenance receipt hook for {txn_id}")
            name, status, detail = hook["name"], hook["status"], hook["detail"]
            if (not isinstance(name, str) or len(name) > 255
                    or not re.fullmatch(r"[^/\\\x00-\x1f\x7f]+\.hook", name)
                    or name in names or status not in ("applied", "verified", "not-applicable")
                    or not isinstance(detail, str) or not 1 <= len(detail) <= 2048
                    or not detail.strip() or "\x00" in detail):
                raise UnifierError(f"invalid or duplicate package maintenance receipt hook for {txn_id}")
            names.add(name)
        covered[txn_id] = entry
    try:
        return covered, _canonical_json_sha256(receipt)
    except UnicodeEncodeError as exc:
        raise UnifierError("package maintenance receipt contains invalid Unicode") from exc


def maintenance_reconciled(record: dict, txn: dict) -> bool:
    evidence = txn.get("maintenance_reconciliation")
    return (record["target"] == "arch" and isinstance(evidence, dict)
            and evidence.get("receipt_sha256") in record.get("package_maintenance_receipts", [])
            and txn.get("environment_binding") == binding_for(record)
            and evidence.get("native_sha256") == _canonical_json_sha256(txn.get("native")))


def audit_package_database(service, record: dict, txn: dict) -> None:
    if record["target"] == "arch":
        # pacman -Dk checks DB consistency but succeeds with a stale db.lck.
        # Never erase that lock or clear recovery while it still exists.
        lock_argv = ["sh", "-c", "test ! -e /var/lib/pacman/db.lck && "
                     "test ! -L /var/lib/pacman/db.lck"]
        lock = service._exec_record(record, lock_argv, timeout=15, native=True,
                                 pin_package=True)
        txn["database_lock"] = {"argv": lock_argv, "exit_code": lock.returncode}
        if lock.returncode:
            raise UnifierError("pacman database lock db.lck is present or cannot be "
                               "checked; explicit native recovery is required")
    argv = {"debian13": ["dpkg", "--audit"],
            "fedora44": ["rpm", "--verifydb"],
            "arch": ["pacman", "-Dk"]}[record["target"]]
    result = service._exec_record(record, argv, timeout=60, native=True, pin_package=True)
    txn["database_audit"] = {"argv": argv, "exit_code": result.returncode,
                             "stdout_tail": output_tail(result.stdout or ""),
                             "stderr_tail": output_tail(result.stderr or "")}
    if result.returncode or (record["target"] == "debian13" and result.stdout.strip()):
        raise UnifierError("native package database needs repair; inspect pkg show and "
                           "repair it inside this environment before pkg recover")


def native_package_journal(service, record, action, audit):
    runtime = record["runtime"]
    container = service._package_container_ref(record)
    info = service._require_owned(record)
    from unifier.inventory_backends import backend_for
    _, paths, _ = backend_for(record)
    for _, destination, _, _ in info.mounts:
        destination = destination.rstrip("/") or "/"
        if any(destination == "/" or destination == path
               or path.startswith(destination + "/") or destination.startswith(path + "/")
               for path in paths):
            raise UnifierError("mounted package database is unsupported")
    argv = ["sh", "-c", package_journal.SCRIPT, "unifier-package-journal",
            record["target"], action]
    options = {"project": runtime["project"], "env_id": record["id"],
               "env": driver_for(record).native_env(), "timeout": 30}
    if isinstance(service.runtime, PodmanRuntime) and action == "checkpoint":
        result = service.runtime.package_exec(container, argv, interactive=False,
                                           stream=False, **options)
    else:
        if isinstance(service.runtime, PodmanRuntime):
            options["user"] = "0"
        result = service.runtime.exec(container, argv, workdir=None, capture=True, **options)
    status = result.stdout.strip()
    audit.setdefault("database_journal", []).append({
        "action": action, "status": status, "exit_code": result.returncode,
        "stderr_tail": output_tail(result.stderr),
    })
    if result.returncode or status not in {"clean", "pending"}:
        raise UnifierError("could not reconcile the native package database journal")
    return status


def recover_locked(service, env_id: str, maintenance_receipt=None) -> dict:
    with service.package_operation_lock(env_id, blocking=False):
        record = service.store.load(env_id)
        service._require_host_compatibility(record)
        if maintenance_receipt is not None and record["target"] != "arch":
            raise UnifierError("package maintenance receipts require an Arch environment")
        pending = service._pending_package_transactions(env_id)
        journal_backend = record["target"] in package_journal.TARGETS
        clean_record = not pending and not record.get("package_recovery_required")
        info = service._require_owned(record)
        binding = binding_for(record)
        if clean_record and journal_backend and not info.running:
            observed, error = service._probe_inventory(record)
            if observed == record["package_inventory_sha256"] and error is None:
                return {"environment_id": env_id, "status": "no-recovery-needed"}
            if error and not error.startswith(("dpkg database has pending updates",
                                                "RPM database has pending WAL/journal")):
                service._save_package_recovery_state(binding, status="unknown", required=True)
                raise UnifierError(f"stopped package inventory cannot be verified: {error}")
        for txn in pending:
            service._require_record_binding(record, txn["environment_binding"])
            if info.container_id != txn["environment_binding"]["container_id"]:
                raise UnifierError("package recovery container identity changed")
            if info.running:
                for step in txn.get("native", []):
                    if (step.get("state") not in {"starting", "running", "interrupted"}
                            or step.get("cleanup") in {"gone", "container-stopped"}):
                        continue
                    raise UnifierError("package execution cleanup is unverified; explicitly stop "
                                       "this environment before package recovery")
        post_transaction = []
        legacy_post_transaction = []
        if record["target"] == "arch":
            for txn in pending:
                evidence = arch_post_transaction_recovery(txn)
                if evidence:
                    txn["post_transaction_recovery"] = evidence
                    post_transaction.append(txn)
            pending_ids = {txn["id"] for txn in pending}
            for txn in service.store.transactions_for(env_id):
                if (txn["id"] in pending_ids or txn.get("environment_binding") != binding
                        or not txn.get("recovered_at")
                        or txn.get("inventory_status") != "verified"
                        or maintenance_reconciled(record, txn)):
                    continue
                evidence = arch_post_transaction_recovery(txn)
                if evidence:
                    txn["post_transaction_recovery"] = evidence
                    legacy_post_transaction.append(txn)
        if maintenance_receipt is not None:
            blockers = post_transaction + legacy_post_transaction
            return _reconcile_arch_maintenance(
                service, record, pending, blockers, maintenance_receipt)
        # Recheck Arch even without a pending journal: older recovery could
        # report success after -Dk while leaving the native lock behind.
        started = False
        audit = {}
        try:
            if not info.running:
                service.start(env_id, expected_binding=binding)
                started = True
            try:
                if journal_backend:
                    journal = service._native_package_journal(record, "probe", audit)
                    if journal == "pending":
                        if not started:
                            raise UnifierError("stop the environment before recovering its native package database journal")
                        service._save_package_recovery_state(binding, status="unknown", required=True)
                        service._audit_package_database(record, audit)
                        service._native_package_journal(record, "checkpoint", audit)
                        if service._native_package_journal(record, "probe", audit) != "clean":
                            raise UnifierError("native package database journal remains pending after recovery")
                    elif clean_record and info.running:
                        return {"environment_id": env_id, "status": "no-recovery-needed"}
                service._audit_package_database(record, audit)
                if post_transaction or legacy_post_transaction:
                    raise UnifierError("pacman post-transaction completion is unverified; "
                                       "explicit native recovery is required")
                inventory = service._read_inventory_record(record)
                if journal_backend and started:
                    service.stop(env_id, package_lock_held=True)
                    started = False
                    observed, error = service._probe_inventory(record)
                    if error or observed != hashlib.sha256(inventory.encode()).hexdigest():
                        raise UnifierError("stopped package inventory does not match the native inventory"
                                           + (f": {error}" if error else ""))
                record = service._commit_inventory(record, inventory, {"native": []})
                _save_recovered_transactions(service, pending, record, audit)
                service._save_package_recovery_state(binding, status="verified", required=False)
            except Exception:
                service._save_package_recovery_state(binding, status="unknown", required=True)
                for txn in pending:
                    txn.update(phase="needs-recovery", recovery_required=True,
                               inventory_status="unknown", recovery_audit=audit)
                    service.store.save_transaction(txn)
                for txn in legacy_post_transaction:
                    # Preserve the legacy outcome and attach current evidence separately.
                    txn["post_transaction_recovery"]["audit"] = audit
                    service.store.save_transaction(txn)
                raise
            return {"environment_id": env_id, "status": "recovered",
                    "transactions": [txn["id"] for txn in pending],
                    "database_audit": audit["database_audit"],
                    **({"database_journal": audit["database_journal"]} if journal_backend else {}),
                    "package_inventory_sha256": record["package_inventory_sha256"]}
        finally:
            if started:
                service.stop(env_id, package_lock_held=True)


def _save_recovered_transactions(service, pending: list[dict], record: dict,
                                 audit: dict) -> None:
    for txn in pending:
        txn.update(phase="failed", failure_phase=txn.get("failure_phase") or "interrupted",
                   recovery_required=False, inventory_status="verified",
                   recovered_at=service.clock(), database_audit=audit["database_audit"],
                   revision_after={key: record[key] for key in (
                       "writable_revision", "content_revision", "package_inventory_sha256")})
        if "database_journal" in audit:
            txn["recovery_audit"] = audit
        txn["finished_at"] = txn.get("finished_at") or service.clock()
        txn["verdict"] = "failed"
        service.store.save_transaction(txn)


def _reconcile_arch_maintenance(service, record: dict, pending: list[dict],
                               blockers: list[dict], receipt: dict) -> dict:
    env_id = record["id"]
    binding = binding_for(record)
    covered, receipt_hash = _validate_arch_maintenance_receipt(receipt, binding, blockers)
    inventory_hash = receipt["inventory_sha256"]
    audit = {}
    with service._lifecycle_lock(env_id):
        assert_no_active_views(service.root, env_id)
        record = service.store.load(env_id)
        service._require_record_binding(record, binding)
        container = service._package_container_ref(record)
        _require_stopped_maintenance_instance(service, record)
        record.update(package_inventory_status="unknown", package_recovery_required=True)
        service.store.save(record, None)
        project = record["runtime"]["project"]
        # Audit the pinned container under the lifecycle lock, leaving
        # the interrupted native journal untouched by ordinary stop.
        try:
            service.runtime.start(container, project=project, env_id=env_id)
            service._audit_package_database(record, audit)
            inventory = service._read_inventory_record(record)
            if hashlib.sha256(inventory.encode("utf-8")).hexdigest() != inventory_hash:
                raise UnifierError("package maintenance receipt inventory SHA-256 does not "
                                   "match the current native inventory")
        finally:
            info = service.runtime.inspect(container)
            if info is not None and info.container_id == binding["container_id"] and info.running:
                service.runtime.stop(container, project=project, env_id=env_id)
        _require_stopped_maintenance_instance(service, record)
        record = service._commit_inventory(record, inventory, {"native": []})
        stamp = service.clock()
        for txn_id, entry in covered.items():
            txn = service.store.load_transaction(txn_id)
            txn["maintenance_reconciliation"] = {
                "schema_version": 1, "accepted_at": stamp,
                "receipt_sha256": receipt_hash, "inventory_sha256": inventory_hash,
                "native_sha256": entry["native_sha256"], "hooks": entry["hooks"],
                "audit": audit,
            }
            service.store.save_transaction(txn)
        _save_recovered_transactions(
            service, [txn for txn in pending if txn["id"] not in covered], record, audit)
        # A partially saved batch cannot clear any blockers. Activate its
        # evidence in the same atomic record write as the screen clearance.
        record["package_maintenance_receipts"] = [
            *record.get("package_maintenance_receipts", []), receipt_hash]
        record.update(package_inventory_status="verified", package_recovery_required=False)
        service.store.save(record, None)
    return {"environment_id": env_id, "status": "recovered",
            "transactions": sorted(set(covered) | {txn["id"] for txn in pending}),
            "database_audit": audit["database_audit"],
            "package_inventory_sha256": inventory_hash,
            "maintenance_receipt_sha256": receipt_hash}


def _require_stopped_maintenance_instance(service, record: dict) -> None:
    info = service._require_owned(record)
    if (info.container_id != record["runtime"]["container_id"] or info.running
            or info.pid not in (0, None)
            or info.status not in {"configured", "created", "exited", "stopped"}):
        raise UnifierError("package maintenance receipts require the exact stopped Arch instance")
