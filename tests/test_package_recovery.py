"""Interrupted writes, durable command journals and conservative recovery."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.cli import _read_package_maintenance_receipt, build_parser
from unifier.errors import UnifierError
from unifier.identity import binding_for
from unifier.native_io import NativeCommandError
from unifier.package_inventory import normalize_inventory, observation
from unifier.runtime import ExecResult
from test_packages import PackageBoxes, make_env, package_service


class InterruptedBoxes(PackageBoxes):
    cleanup = "gone"
    audit_problem = ""

    def package_exec(self, name, argv, **options):
        txn, = self.service.transactions_for("probe")[-1:]
        self.journal_before_command = txn
        if argv[:2] == ["apt-get", "install"] and "hello" in argv:
            self.installed.setdefault("probe", set()).add("hello")
            raise NativeCommandError("fixture timeout", stdout="package files written\n",
                                     returncode=-15, cleanup=self.cleanup)
        return self.exec(name, argv, project=options["project"], env_id=options["env_id"],
                         workdir=None, env=options["env"], capture=True, timeout=options["timeout"])

    def _answer(self, info, argv):
        if argv == ["dpkg", "--audit"]:
            return ExecResult(0, self.audit_problem, "")
        return super()._answer(info, argv)


class ArchRecoveryBoxes(PackageBoxes):
    mutation = "-Syu"
    mutation_result = ExecResult(137, "", "")
    mutation_calls = 0
    committed = False

    def _answer(self, info, argv):
        if argv[:2] == ["pacman", self.mutation]:
            self.mutation_calls += 1
            if self.committed:
                self.installed.setdefault(info.env_id, set()).add("openssl-3")
            return self.mutation_result
        return super()._answer(info, argv)


class NativeJournalBoxes(InterruptedBoxes):
    journal = False
    checkpoint_fails = False
    stopped_mismatch = False

    def package_exec(self, name, argv, **options):
        if argv[:2] in (["dnf", "install"], ["apt-get", "install"]):
            self.journal = True
            self.installed.setdefault("probe", set()).add("hello")
            raise NativeCommandError("fixture interruption", cleanup="gone", signum=2)
        return super().package_exec(name, argv, **options)

    def _answer(self, info, argv):
        if argv[0:2] == ["sh", "-c"] and argv[3:4] == ["unifier-package-journal"]:
            if argv[-1] == "checkpoint":
                if self.checkpoint_fails:
                    return ExecResult(1, "", "fixture busy")
                self.journal = False
            return ExecResult(0, "pending\n" if self.journal else "clean\n", "")
        return super()._answer(info, argv)

    def read_inventory_readonly(self, record):
        if self.journal:
            reason = ("RPM database has pending WAL/journal; native recovery required"
                      if record["target"] == "fedora44" else
                      "dpkg database has pending updates; recover it first")
            return observation(record, status="unavailable", reason=reason)
        if self.stopped_mismatch:
            return observation(record, inventory="different\t1\n")
        return super().read_inventory_readonly(record)


class PackageRecovery(unittest.TestCase):
    def journal_fixture(self, target):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = NativeJournalBoxes()
        service, _ = package_service(Path(temporary.name), runtime)
        runtime.service = service
        make_env(service, runtime, target, "probe")
        return service, runtime

    def test_interrupted_database_write_stays_blocked_until_native_checkpoint(self):
        for target in ("fedora44", "debian13"):
            with self.subTest(target=target):
                service, runtime = self.journal_fixture(target)
                baseline = service.store.load("probe")["package_inventory_sha256"]
                txn = service.install_packages("probe", ["hello"])
                self.assertEqual(txn["phase"], "needs-recovery")
                self.assertEqual(txn["database_journal"][0]["status"], "pending")
                self.assertEqual(service.store.load("probe")["package_inventory_sha256"], baseline)
                with self.assertRaisesRegex(UnifierError, "pkg recover"):
                    service.remove_packages("probe", ["hello"])
                result = service.recover_packages("probe")
                self.assertEqual(result["status"], "recovered")
                self.assertEqual(result["transactions"], [txn["id"]])
                self.assertEqual([x["action"] for x in result["database_journal"]],
                                 ["probe", "checkpoint", "probe"])
                self.assertFalse(runtime.journal)
                record = service.store.load("probe")
                inventory = runtime.read_inventory_readonly(record).inventory
                registered = service.root / ".lab/state/environments/probe.packages.txt"
                self.assertEqual(registered.read_text(), inventory)
                self.assertEqual(record["package_inventory_sha256"],
                                 hashlib.sha256(inventory.encode()).hexdigest())
                self.assertEqual(service.recover_packages("probe")["status"], "no-recovery-needed")

    def test_legacy_false_success_preserves_barrier_on_checkpoint_or_verification_failure(self):
        for target in ("fedora44", "debian13"):
            with self.subTest(target=target):
                service, runtime = self.journal_fixture(target)
                runtime.journal = True
                runtime.checkpoint_fails = True
                with self.assertRaisesRegex(UnifierError, "journal"):
                    service.recover_packages("probe")
                self.assertTrue(service.store.load("probe")["package_recovery_required"])
                runtime.checkpoint_fails = False
                runtime.stopped_mismatch = True
                with self.assertRaisesRegex(UnifierError, "does not match"):
                    service.recover_packages("probe")
                self.assertTrue(service.store.load("probe")["package_recovery_required"])
                runtime.stopped_mismatch = False
                self.assertEqual(service.recover_packages("probe")["status"], "recovered")

    def arch_fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = ArchRecoveryBoxes()
        service, _ = package_service(Path(temporary.name), runtime)
        make_env(service, runtime, "arch", "probe")
        return service, runtime

    def maintenance_receipt(self, service, runtime, *transactions):
        record = service.store.load("probe")
        inventory = normalize_inventory(runtime._inventory("probe", ["pacman", "-Q"]))
        return {
            "schema_version": 1,
            "environment_binding": binding_for(record),
            "inventory_sha256": hashlib.sha256(inventory.encode()).hexdigest(),
            "transactions": [{
                "id": txn["id"],
                "native_sha256": hashlib.sha256(json.dumps(
                    txn["native"], sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False).encode()).hexdigest(),
                "hooks": [{"name": "25-systemd-hwdb.hook", "status": "verified",
                           "detail": "Fixture checked the native output."}],
            } for txn in transactions],
        }

    def test_arch_maintenance_receipt_reconciles_legacy_without_rewriting_history(self):
        service, runtime = self.arch_fixture()
        runtime.committed = True
        txn = service.update_packages("probe")
        txn.pop("post_transaction_recovery")
        txn.update(phase="failed", recovery_required=False, inventory_status="verified",
                   recovered_at="2026-11-12T15:33:18+08:00")
        service.store.save_transaction(txn)
        receipt = self.maintenance_receipt(service, runtime, txn)
        result = service.recover_packages("probe", maintenance_receipt=receipt)
        self.assertEqual(result["transactions"], [txn["id"]])
        self.assertEqual(result["package_inventory_sha256"], receipt["inventory_sha256"])
        saved = service.show_transaction(txn["id"])
        evidence = saved.pop("maintenance_reconciliation")
        self.assertEqual(saved, txn)
        self.assertEqual(evidence["receipt_sha256"], result["maintenance_receipt_sha256"])
        self.assertEqual(evidence["hooks"], receipt["transactions"][0]["hooks"])
        self.assertEqual(evidence["audit"]["database_lock"]["exit_code"], 0)
        self.assertFalse(service.store.load("probe")["package_recovery_required"])
        self.assertEqual(service.inspect("probe")["runtime_state"], "stopped")
        self.assertEqual(service.recover_packages("probe")["transactions"], [])
        self.assertEqual(runtime.mutation_calls, 1)

    def test_arch_maintenance_receipt_refuses_wrong_binding_native_inventory_and_lock(self):
        service, runtime = self.arch_fixture()
        runtime.committed = True
        txn = service.update_packages("probe")
        original_inventory = service.store.load("probe")["package_inventory_sha256"]
        for failure in ("binding", "native", "inventory", "lock"):
            with self.subTest(failure=failure):
                receipt = self.maintenance_receipt(service, runtime, txn)
                runtime.pacman_locked = failure == "lock"
                if failure == "binding":
                    receipt["environment_binding"]["generation"] += 1
                elif failure == "native":
                    receipt["transactions"][0]["native_sha256"] = "0" * 64
                elif failure == "inventory":
                    receipt["inventory_sha256"] = "0" * 64
                with self.assertRaisesRegex(UnifierError, {
                    "binding": "environment binding", "native": "native SHA-256",
                    "inventory": "current native inventory", "lock": "db.lck",
                }[failure]):
                    service.recover_packages("probe", maintenance_receipt=receipt)
                record = service.store.load("probe")
                self.assertTrue(record["package_recovery_required"])
                self.assertEqual(record["package_inventory_sha256"], original_inventory)
                self.assertNotIn("maintenance_reconciliation", service.show_transaction(txn["id"]))

    def test_arch_maintenance_receipt_partial_write_requires_full_retry(self):
        service, runtime = self.arch_fixture()
        first = service.update_packages("probe")
        second = {**first, "id": "txn111111111111"}
        service.store.save_transaction(second)
        receipt = self.maintenance_receipt(service, runtime, first, second)
        save_transaction = service.store.save_transaction

        def fail_second(txn):
            if txn["id"] == second["id"]:
                raise OSError("fixture journal write failed")
            save_transaction(txn)

        with patch.object(service.store, "save_transaction", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "journal write failed"):
                service.recover_packages("probe", maintenance_receipt=receipt)
        self.assertIn("maintenance_reconciliation", service.show_transaction(first["id"]))
        with self.assertRaisesRegex(UnifierError, "every post-transaction blocker"):
            service.recover_packages("probe", maintenance_receipt={
                **receipt, "transactions": receipt["transactions"][1:],
            })
        self.assertTrue(service.store.load("probe")["package_recovery_required"])
        result = service.recover_packages("probe", maintenance_receipt=receipt)
        self.assertEqual(set(result["transactions"]), {first["id"], second["id"]})
        self.assertEqual(service.recover_packages("probe")["transactions"], [])
        self.assertEqual(service.show_transaction(first["id"])["phase"], "needs-recovery")

    def test_arch_maintenance_receipt_reader_bounds_input_and_rejects_symlinks(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "receipt.json"
            path.write_text('{"schema_version":1}', encoding="utf-8")
            args = build_parser().parse_args(
                ["pkg", "recover", "probe", "--maintenance-receipt", str(path)])
            self.assertEqual(_read_package_maintenance_receipt(args.maintenance_receipt),
                             {"schema_version": 1})
            symlink = path.with_name("symlink")
            symlink.symlink_to(path)
            fifo = path.with_name("fifo")
            os.mkfifo(fifo)
            for entry in (symlink, fifo):
                with self.assertRaises(UnifierError):
                    _read_package_maintenance_receipt(str(entry))
            for data in (b"{", b"null", b'{"a":1,"a":2}', b" " * 65537):
                with self.subTest(data_length=len(data)):
                    path.write_bytes(data)
                    with self.assertRaises(UnifierError):
                        _read_package_maintenance_receipt(str(path))

    def test_arch_hook_interruption_blocks_repeated_recovery_with_clean_database(self):
        service, runtime = self.arch_fixture()
        runtime.committed = True
        runtime.mutation_result = ExecResult(137, ":: Running post-transaction hooks...\n", "")
        previous = service.store.load("probe")["package_inventory_sha256"]
        txn = service.update_packages("probe")
        self.assertEqual(txn["phase"], "needs-recovery")
        self.assertEqual(txn["database_lock"]["exit_code"], 0)
        self.assertEqual(txn["database_audit"]["exit_code"], 0)
        self.assertEqual(txn["post_transaction_recovery"]["commit_evidence"], "marker-observed")
        self.assertIn("openssl-3", runtime.installed["probe"])
        for _ in range(2):
            with self.assertRaisesRegex(UnifierError, "post-transaction completion"):
                service.recover_packages("probe")
            saved = service.show_transaction(txn["id"])
            self.assertEqual(saved["phase"], "needs-recovery")
            self.assertEqual(saved["recovery_audit"]["database_audit"]["exit_code"], 0)
            self.assertEqual(saved["native"], txn["native"])
            record = service.store.load("probe")
            self.assertTrue(record["package_recovery_required"])
            self.assertEqual(record["package_inventory_status"], "unknown")
            self.assertEqual(record["package_inventory_sha256"], previous)
        with self.assertRaisesRegex(UnifierError, "pkg recover"):
            service.update_packages("probe")
        self.assertEqual(runtime.mutation_calls, 1)

    def test_arch_legacy_recovery_preserves_outcome_and_blocks_unknown_hooks(self):
        service, runtime = self.arch_fixture()
        txn = service.update_packages("probe")
        txn.pop("post_transaction_recovery")
        txn.update(phase="failed", recovery_required=False, inventory_status="verified",
                   recovered_at="2026-11-12T15:33:18+08:00")
        service.store.save_transaction(txn)
        for _ in range(2):
            with self.assertRaisesRegex(UnifierError, "post-transaction completion"):
                service.recover_packages("probe")
            saved = service.show_transaction(txn["id"])
            recovery = saved.pop("post_transaction_recovery")
            self.assertEqual(saved, txn)
            self.assertEqual(recovery["audit"]["database_audit"]["exit_code"], 0)
            self.assertTrue(service.store.load("probe")["package_recovery_required"])
        self.assertEqual(runtime.mutation_calls, 1)

    def test_arch_replaced_generation_ignores_old_recovered_journal(self):
        service, _ = self.arch_fixture()
        txn = service.update_packages("probe")
        txn.pop("post_transaction_recovery")
        txn.update(phase="failed", recovery_required=False, inventory_status="verified",
                   recovered_at="2026-11-12T15:33:18+08:00")
        service.store.save_transaction(txn)
        record = service.store.load("probe")
        record["generation"] += 1
        service.store.save(record, None)
        self.assertEqual(service.recover_packages("probe")["status"], "recovered")
        self.assertEqual(service.show_transaction(txn["id"]), txn)

    def test_arch_interruption_with_unchanged_inventory_is_indeterminate(self):
        service, runtime = self.arch_fixture()
        runtime.mutation_result = ExecResult(137, "checking package integrity...\n", "")
        txn = service.update_packages("probe")
        self.assertEqual(txn["phase"], "needs-recovery")
        self.assertEqual(txn["revision_before"], txn["revision_after"])
        self.assertEqual(txn["post_transaction_recovery"]["commit_evidence"], "indeterminate")
        with self.assertRaisesRegex(UnifierError, "post-transaction completion"):
            service.recover_packages("probe")
        self.assertEqual(runtime.mutation_calls, 1)

    def test_arch_nonzero_post_transaction_marker_survives_truncated_output(self):
        service, runtime = self.arch_fixture()
        runtime.mutation = "-R"
        runtime.mutation_result = ExecResult(
            1, ":: Running post-transaction hooks...\n" + "x" * 5000, "")
        txn = service.remove_packages("probe", ["hello"])
        self.assertEqual(txn["phase"], "needs-recovery")
        self.assertEqual(txn["native"][-1]["stdout_tail"], "x" * 4000)
        self.assertEqual(txn["post_transaction_recovery"]["commit_evidence"], "marker-observed")

    def test_arch_ordinary_native_failure_can_reconcile(self):
        service, runtime = self.arch_fixture()
        runtime.mutation_result = ExecResult(1, "", "error: target not found: missing")
        txn = service.install_packages("probe", ["missing"])
        self.assertEqual(txn["phase"], "failed")
        self.assertFalse(txn["recovery_required"])
        self.assertEqual(txn["inventory_status"], "verified")
        self.assertNotIn("post_transaction_recovery", txn)
        self.assertEqual(service.recover_packages("probe")["status"], "recovered")

    def test_arch_stale_lock_blocks_recovery_even_after_old_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = PackageBoxes()
            service, _ = package_service(Path(temporary), runtime)
            make_env(service, runtime, "arch", "probe")
            runtime.pacman_locked = True
            with self.assertRaisesRegex(UnifierError, "db.lck"):
                service.recover_packages("probe")
            self.assertTrue(service.store.load("probe")["package_recovery_required"])
            with self.assertRaisesRegex(UnifierError, "pkg recover"):
                service.install_packages("probe", ["hello"])
            self.assertFalse(runtime._owned(service.store.load("probe")["runtime"]["container_name"],
                                           str(service.root), "probe").running)
            # Only the fixture's explicit native repair removes its lock.
            runtime.pacman_locked = False
            self.assertEqual(service.recover_packages("probe")["status"], "recovered")
            self.assertFalse(service.store.load("probe")["package_recovery_required"])

    def test_arch_stale_lock_preserves_pending_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            runtime = PackageBoxes()
            service, _ = package_service(Path(temporary), runtime)
            make_env(service, runtime, "arch", "probe")
            txn = service.install_packages("probe", ["hello"])
            txn.update(phase="running", recovery_required=False)
            service.store.save_transaction(txn)
            runtime.pacman_locked = True
            with self.assertRaisesRegex(UnifierError, "db.lck"):
                service.recover_packages("probe")
            saved = service.show_transaction(txn["id"])
            self.assertEqual(saved["phase"], "needs-recovery")
            self.assertTrue(saved["recovery_required"])
            self.assertNotIn("recovered_at", saved)
            self.assertEqual(saved["recovery_audit"]["database_lock"]["exit_code"], 1)

    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        runtime = InterruptedBoxes()
        service, _ = package_service(Path(temporary.name), runtime)
        runtime.service = service
        make_env(service, runtime, "debian13", "probe")
        return service, runtime

    def test_timeout_after_write_reconciles_inventory_and_preserves_command(self):
        service, runtime = self.fixture()
        before = service.store.load("probe")
        txn = service.install_packages("probe", ["hello"])
        after = service.store.load("probe")
        self.assertEqual(txn["phase"], "failed")
        self.assertFalse(txn["recovery_required"])
        self.assertEqual(txn["inventory_status"], "verified")
        self.assertNotEqual(before["package_inventory_sha256"], after["package_inventory_sha256"])
        actual = runtime._inventory("probe", ["dpkg-query"])
        self.assertEqual(after["package_inventory_sha256"], hashlib.sha256(actual.encode()).hexdigest())
        self.assertEqual(after["content_revision"], before["content_revision"] + 1)
        self.assertIn("files written", txn["native"][-1]["stdout_tail"])
        self.assertEqual(runtime.journal_before_command["phase"], "running")
        self.assertEqual(runtime.journal_before_command["native"][-1]["state"], "starting")

    def test_unknown_cleanup_blocks_retry_and_running_recovery_then_reconciles(self):
        service, runtime = self.fixture()
        runtime.cleanup = "unknown"
        txn = service.install_packages("probe", ["hello"])
        self.assertEqual(txn["phase"], "needs-recovery")
        self.assertEqual(service.store.load("probe")["package_inventory_status"], "unknown")
        with self.assertRaisesRegex(UnifierError, "pkg recover"):
            service.install_packages("probe", ["other"])
        with self.assertRaisesRegex(UnifierError, "explicitly stop"):
            service.recover_packages("probe")
        service.stop("probe")
        service.start("probe")  # Explicit native maintenance may need a restart.
        self.assertEqual(service.recover_packages("probe")["status"], "recovered")
        saved = service.show_transaction(txn["id"])
        self.assertFalse(saved["recovery_required"])
        self.assertEqual(saved["phase"], "failed")
        self.assertTrue(service.query_package("probe", "hello")["present"])
        self.assertEqual(service.install_packages("probe", ["other"])["phase"], "completed")

    def test_dirty_database_stays_blocked_until_explicit_native_repair(self):
        service, runtime = self.fixture()
        runtime.audit_problem = "hello: package is unpacked but not configured\n"
        txn = service.install_packages("probe", ["hello"])
        self.assertEqual(txn["phase"], "needs-recovery")
        with self.assertRaisesRegex(UnifierError, "database needs repair"):
            service.recover_packages("probe")
        runtime.audit_problem = ""
        self.assertEqual(service.recover_packages("probe")["status"], "recovered")

    def test_crash_journal_blocks_fresh_service_and_changed_identity(self):
        service, runtime = self.fixture()
        runtime.cleanup = "unknown"
        txn = service.install_packages("probe", ["hello"])
        txn.update(phase="running", recovery_required=False)
        service.store.save_transaction(txn)
        service, _ = package_service(service.root, runtime)
        runtime.service = service
        with self.assertRaisesRegex(UnifierError, "pkg recover"):
            service.remove_packages("probe", ["hello"])
        service.stop("probe")
        record = service.store.load("probe")
        record["generation"] += 1
        service.store.save(record, None)
        with self.assertRaises(UnifierError):
            service.recover_packages("probe")

    def test_replaced_container_is_rejected_before_package_transaction(self):
        service, runtime = self.fixture()
        record = service.store.load("probe")
        name = record["runtime"]["container_name"]
        runtime.containers[name] = replace(runtime.containers[name], container_id="replacement")
        before = list(runtime.events)
        with self.assertRaisesRegex(UnifierError, "container identity changed"):
            service.install_packages("probe", ["other"])
        self.assertEqual(runtime.events, before)
        self.assertEqual(service.transactions_for("probe"), [])

    def test_replacement_between_native_steps_stays_unmodified_and_requires_recovery(self):
        service, runtime = self.fixture()
        record = service.store.load("probe")
        name = record["runtime"]["container_name"]
        original = runtime.package_exec

        def replacing(*args, **options):
            result = original(*args, **options)
            runtime.containers[name] = replace(runtime.containers[name], container_id="replacement")
            return result

        with patch.object(runtime, "package_exec", side_effect=replacing):
            txn = service.install_packages("probe", ["other"])
        self.assertEqual(txn["phase"], "needs-recovery")
        self.assertEqual([step["argv"] for step in txn["native"]], [["apt-get", "update"]])
        self.assertNotIn("other", runtime.installed.get("probe", set()))
        self.assertTrue(runtime.containers[name].running)
        self.assertEqual(service.store.load("probe")["package_inventory_sha256"],
                         record["package_inventory_sha256"])
        with self.assertRaisesRegex(UnifierError, "container identity changed"):
            service.recover_packages("probe")

    def test_stopping_replacement_does_not_prove_old_execution_ended(self):
        service, runtime = self.fixture()
        runtime.cleanup = "unknown"
        txn = service.install_packages("probe", ["hello"])
        record = service.store.load("probe")
        name = record["runtime"]["container_name"]
        runtime.containers[name] = replace(runtime.containers[name], container_id="replacement")
        service.stop("probe")
        saved = service.show_transaction(txn["id"])
        self.assertEqual(saved["native"][-1]["cleanup"], "unknown")
        self.assertNotIn("container_stopped_at", saved)


if __name__ == "__main__":
    unittest.main()
