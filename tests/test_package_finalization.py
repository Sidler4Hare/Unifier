"""Offline regressions for independent transaction cleanup and annotations."""
import ast
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from unifier.errors import UnifierError
from test_packages import package_service, make_env


class PackageFinalization(unittest.TestCase):
    def fixture(self, target="debian13"):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        service, runtime = package_service(Path(temporary.name))
        make_env(service, runtime, target, "probe")
        service.stop("probe")
        return service, runtime

    def assert_blocked(self, service, txn, stage):
        self.assertEqual(txn["phase"], "needs-recovery")
        self.assertTrue(txn["recovery_required"])
        self.assertIn(stage, [item["step"] for item in txn["finalize_errors"]])
        self.assertEqual(service.show_transaction(txn["id"])["phase"], "needs-recovery")
        with self.assertRaisesRegex(UnifierError, "pkg recover"):
            service.install_packages("probe", ["other"])

    def test_stop_failure_is_recorded_and_does_not_skip_isolation(self):
        service, runtime = self.fixture()
        with patch.object(service, "stop", side_effect=OSError("stop failed")), \
             patch.object(service, "_finish_isolation", wraps=service._finish_isolation) as isolation:
            txn = service.install_packages("probe", ["hello"])
        self.assert_blocked(service, txn, "stop")
        isolation.assert_called_once()
        self.assertTrue(service.store.load("probe")["package_recovery_required"])

    def test_failed_arch_local_staging_prevents_native_writes(self):
        service, runtime = self.fixture("arch")
        package = service.root / "hello.pkg.tar.zst"
        package.write_bytes(b"fake package")

        def fail_copy(*args, **kwargs):
            txn, = service.transactions_for("probe")
            self.assertEqual(txn["phase"], "running")
            self.assertEqual(txn["native"], [])
            commands = [event[2] for event in runtime.events if event[0] == "exec"]
            self.assertEqual(commands[-2:], [
                ("pacman-key", "--list-keys"), ("mkdir", "-p", "/tmp/ml-incoming"),
            ])
            raise OSError("local copy failed")

        with patch.object(runtime, "copy_in", side_effect=fail_copy) as copy:
            txn = service.install_local("probe", str(package))

        copy.assert_called_once()
        self.assertEqual(txn["phase"], "failed")
        self.assertEqual(txn["failure_phase"], "runtime")
        self.assertEqual(txn["native_error"], "local copy failed")
        self.assertEqual(txn["native"], [])
        self.assertFalse(txn["recovery_required"])
        self.assertEqual(service.show_transaction(txn["id"]), txn)

    def test_inventory_commit_failure_does_not_skip_journal_or_isolation(self):
        service, runtime = self.fixture()
        with patch.object(service, "_commit_inventory", side_effect=OSError("write failed")), \
             patch.object(service, "_finish_isolation", wraps=service._finish_isolation) as isolation:
            txn = service.install_packages("probe", ["hello"])
        self.assert_blocked(service, txn, "inventory-commit")
        isolation.assert_called_once()
        self.assertEqual(txn["inventory_status"], "unknown")

    def test_isolation_failure_still_saves_final_journal(self):
        service, runtime = self.fixture()
        with patch.object(service, "_finish_isolation", side_effect=OSError("snapshot failed")):
            txn = service.install_packages("probe", ["hello"])
        self.assertEqual(txn['phase'], 'completed')
        self.assertEqual(txn['isolation_observation']['status'], 'unavailable')
        self.assertFalse(txn['recovery_required'])
        self.assertEqual(service.show_transaction(txn['id'])['isolation_observation'], txn['isolation_observation'])

    def test_record_save_failure_still_saves_final_journal(self):
        service, runtime = self.fixture()
        original = service.store.save
        def save(record, inventory):
            if "package_recovery_required" in record:
                raise OSError("state disk failed")
            return original(record, inventory)
        with patch.object(service.store, "save", side_effect=save):
            txn = service.install_packages("probe", ["hello"])
        self.assert_blocked(service, txn, "record-save")

    def test_native_failure_is_not_replaced_by_cleanup_failure(self):
        service, runtime = self.fixture()
        runtime.fail_packages.add("hello")
        with patch.object(service, "stop", side_effect=OSError("stop failed")):
            txn = service.install_packages("probe", ["hello"])
        self.assert_blocked(service, txn, "stop")
        self.assertEqual(txn["failure_phase"], "native-command")
        self.assertNotEqual(txn["native_error"], "stop failed")

    def test_final_journal_failure_is_explicit_and_old_journal_blocks_retry(self):
        service, runtime = self.fixture()
        original = service.store.save_transaction
        def save(txn):
            if txn["phase"] != "running":
                raise OSError("journal disk failed")
            return original(txn)
        with patch.object(service.store, "save_transaction", side_effect=save):
            with self.assertRaisesRegex(UnifierError, "cannot persist final package transaction txn"):
                service.install_packages("probe", ["hello"])
        txn, = service.transactions_for("probe")
        self.assertEqual(txn["phase"], "running")
        with self.assertRaisesRegex(UnifierError, "pkg recover"):
            service.install_packages("probe", ["other"])

    def test_unexpected_coordinator_error_still_saves_recovery_journal(self):
        service, runtime = self.fixture()
        txn = service.install_packages("probe", ["hello"])
        txn.update(phase="running", recovery_required=False, failure_phase=None)
        service.store.save_transaction(txn)
        record = service.store.load("probe")
        record["content_revision"] = "invalid"
        with patch.object(service.store, "load", return_value=record), \
             patch.object(service, "_commit_inventory", return_value=record):
            with self.assertRaises(ValueError):
                service._finalize_package_transaction(record, txn, None, None, {}, {})
        self.assertEqual(service.show_transaction(txn["id"])["phase"], "needs-recovery")

    def test_package_cleanup_inside_a_caller_exception_can_complete(self):
        service, _ = self.fixture()
        self.assertEqual(service.install_packages('probe', ['hello'])['phase'], 'completed')
        try:
            raise RuntimeError('application launch failed')
        except RuntimeError:
            transaction = service.remove_packages('probe', ['hello'])
        self.assertEqual(transaction['phase'], 'completed')
        self.assertFalse(transaction['recovery_required'])


class ListAnnotations(unittest.TestCase):
    def test_list_methods_keep_postponed_annotations_for_python_311(self):
        root = Path(__file__).resolve().parents[1]
        for name in ("projects", "views", "file_operations"):
            with self.subTest(module=name):
                tree = ast.parse((root / "src/unifier" / (name + ".py")).read_text())
                self.assertTrue(any(isinstance(node, ast.ImportFrom)
                                    and node.module == "__future__"
                                    and any(alias.name == "annotations" for alias in node.names)
                                    for node in tree.body))
                __import__("unifier." + name)


if __name__ == "__main__":
    unittest.main()
