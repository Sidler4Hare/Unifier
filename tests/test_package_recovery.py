"""Interrupted writes, durable command journals and conservative recovery."""
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.errors import UnifierError
from unifier.native_io import NativeCommandError
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


class PackageRecovery(unittest.TestCase):
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
