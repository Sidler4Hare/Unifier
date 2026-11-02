"""Versioned environment identity and explicit legacy migration contracts."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier import cli
from unifier.errors import UnifierError
from unifier.service import EnvironmentService
from test_env import CONFIG, LOCK, service_in


class ExplodingRuntime:
    """Any runtime call during migration is a test failure."""

    def __getattr__(self, name):
        raise AssertionError(f"migration touched runtime.{name}")


def _legacy(service, env_id="debian13"):
    record = service.store.load(env_id)
    record.pop("environment_uuid", None)
    record.pop("generation", None)
    record["schema_version"] = 1
    service.store.save(record, None)
    return record


def _backups(store):
    directory = store.directory / "backups"
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


def _migration_service(root):
    return EnvironmentService(
        Path(root), ExplodingRuntime(), CONFIG, LOCK, host_kernel="host-kernel",
        clock=lambda: "2026-10-19T12:00:00+08:00", uid=1000, gid=1000)


def _migrate_worker(root, start, results):
    try:
        service, _runtime = service_in(Path(root))
        start.wait()
        outcome = service.migrate_environment("debian13")
        results.put((outcome["status"], outcome["environment_uuid"]))
    except BaseException as exc:  # pragma: no cover - reported as a test failure
        results.put(("error", repr(exc)))


class NewIdentity(unittest.TestCase):
    def test_new_environment_has_canonical_uuid_generation_and_read_only_views(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            self.assertEqual(record["schema_version"], 2)
            self.assertEqual(uuid.UUID(record["environment_uuid"]).version, 4)
            self.assertEqual(record["generation"], 1)
            self.assertEqual(record["id"], "debian13")

            summaries = service.list_summaries()
            self.assertEqual(summaries[0]["environment_uuid"], record["environment_uuid"])
            self.assertEqual(summaries[0]["generation"], 1)
            view = service.inspect("debian13")
            self.assertEqual(view["environment_uuid"], record["environment_uuid"])
            plan = service.change_plan("debian13")
            self.assertEqual(plan["environment_uuid"], record["environment_uuid"])
            self.assertEqual(plan["generation"], 1)

    def test_legacy_list_inspect_and_plan_do_not_write_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            _legacy(service)
            source = service.store.path("debian13")
            before = source.read_bytes()
            service.list_summaries()
            view = service.inspect("debian13")
            plan = service.change_plan("debian13")
            self.assertIsNone(view["environment_uuid"])
            self.assertIsNone(view["generation"])
            self.assertIsNone(plan["environment_uuid"])
            self.assertIsNone(plan["generation"])
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(_backups(service.store), [])


class ExplicitMigration(unittest.TestCase):
    def test_migration_is_idempotent_and_preserves_alias_container_and_package_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            created = service.create("debian13", None, [])
            _legacy(service)
            source = service.store.path("debian13")
            original_bytes = source.read_bytes()
            original = json.loads(original_bytes)
            inventory = root / created["package_inventory_path"]
            inventory_bytes = inventory.read_bytes()
            events = list(runtime.events)

            migration = _migration_service(root)
            first = migration.migrate_environment("debian13")
            self.assertEqual(first["status"], "migrated")
            self.assertEqual(first["generation"], 1)
            self.assertEqual(uuid.UUID(first["environment_uuid"]).version, 4)
            backup = Path(first["backup"])
            digest = hashlib.sha256(original_bytes).hexdigest()
            self.assertEqual(backup.name, f"debian13.pre-identity-{digest}.json")
            self.assertEqual(backup.read_bytes(), original_bytes)
            migrated = migration.store.load("debian13")
            self.assertEqual(migrated["environment_uuid"], first["environment_uuid"])
            self.assertEqual(migrated["generation"], 1)
            self.assertEqual(migrated["schema_version"], 2)
            self.assertEqual(migrated["id"], original["id"])
            self.assertEqual(migrated["content_revision"], original["content_revision"])
            self.assertEqual(migrated["runtime"]["container_id"], original["runtime"]["container_id"])
            self.assertEqual(migrated["runtime"]["container_name"], original["runtime"]["container_name"])
            self.assertEqual(inventory.read_bytes(), inventory_bytes)
            self.assertEqual(runtime.events, events)

            after_first = source.read_bytes()
            backup_bytes = backup.read_bytes()
            second = migration.migrate_environment("debian13")
            self.assertEqual(second["status"], "already-migrated")
            self.assertEqual(second["environment_uuid"], first["environment_uuid"])
            self.assertIsNone(second["backup"])
            self.assertEqual(source.read_bytes(), after_first)
            self.assertEqual(backup.read_bytes(), backup_bytes)

    def test_malformed_or_future_records_fail_with_zero_writes_and_no_runtime(self):
        mutations = {
            "uuid-without-generation": lambda record: record.update(
                environment_uuid=str(uuid.uuid4())),
            "generation-without-uuid": lambda record: record.update(generation=1),
            "invalid-uuid": lambda record: record.update(
                environment_uuid="not-a-uuid", generation=1),
            "noncanonical-uuid": lambda record: record.update(
                environment_uuid=str(uuid.uuid4()).upper(), generation=1),
            "zero-generation": lambda record: record.update(
                environment_uuid=str(uuid.uuid4()), generation=0),
            "boolean-generation": lambda record: record.update(
                environment_uuid=str(uuid.uuid4()), generation=True),
            "null-identity-both": lambda record: record.update(
                environment_uuid=None, generation=None),
            "null-identity-uuid": lambda record: record.update(environment_uuid=None),
            "null-identity-generation": lambda record: record.update(generation=None),
            "future-schema": lambda record: record.update(schema_version=99),
            "new-schema-without-identity": lambda record: record.update(schema_version=2),
            "new-schema-with-null-identity": lambda record: record.update(
                schema_version=2, environment_uuid=None, generation=None),
            "minimal-legacy": lambda record: (
                record.clear(),
                record.update(schema_version=1, id="debian13", target="debian13")),
            "runtime-network-list": lambda record: record["runtime"].update(network=[]),
            "runtime-selinux-dict": lambda record: record["runtime"].update(selinux_label={}),
            "runtime-userns-none": lambda record: record["runtime"].update(
                user_namespace=None),
            "runtime-ipc-list": lambda record: record["runtime"].update(ipc=[]),
            "share-policy-list": lambda record: record["runtime"].update(
                share_label_policy=[]),
            "share-policy-none": lambda record: record["runtime"].update(
                share_label_policy=None),
            "future-schema-with-identity": lambda record: record.update(
                schema_version=99, environment_uuid=str(uuid.uuid4()), generation=1),
            "broken-contract": lambda record: record.update(
                system={"package_driver": "apt"}),
            "mismatched-id": lambda record: record.update(id="somewhere-else"),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                setup, runtime = service_in(root)
                setup.create("debian13", None, [])
                _legacy(setup)
                record = setup.store.load("debian13")
                mutate(record)
                source = setup.store.path("debian13")
                source.write_text(
                    json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
                before = source.read_bytes()
                events = list(runtime.events)
                migration = _migration_service(root)
                with self.assertRaises(UnifierError):
                    migration.migrate_environment("debian13")
                self.assertEqual(source.read_bytes(), before)
                self.assertEqual(_backups(migration.store), [])
                self.assertFalse(migration.store.identity_lock_path("debian13").exists())
                self.assertEqual(runtime.events, events)

    def test_cli_migrate_reports_malformed_runtime_types_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            setup, _runtime = service_in(root)
            setup.create("debian13", None, [])
            record = _legacy(setup)
            record["runtime"]["network"] = []
            setup.store.save(record, None)
            source = setup.store.path("debian13")
            before = source.read_bytes()
            migration = _migration_service(root)
            original = cli.service_factory
            cli.service_factory = lambda: migration
            stdout, stderr = StringIO(), StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(["env", "migrate", "debian13"])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 1)
            self.assertEqual(stdout.getvalue(), "")
            self.assertIn("unifier:", stderr.getvalue())
            self.assertNotIn("Traceback", stderr.getvalue())
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(_backups(migration.store), [])

    def test_cli_migrate_prints_the_explicit_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            setup, _runtime = service_in(root)
            setup.create("debian13", None, [])
            _legacy(setup)
            migration = _migration_service(root)
            original = cli.service_factory
            cli.service_factory = lambda: migration
            stdout, stderr = StringIO(), StringIO()
            try:
                with redirect_stdout(stdout), redirect_stderr(stderr):
                    code = cli.main(["env", "migrate", "debian13"])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 0, stderr.getvalue())
            result = json.loads(stdout.getvalue())
            self.assertEqual(result["status"], "migrated")
            self.assertEqual(result["generation"], 1)
            self.assertEqual(migration.store.load("debian13")["environment_uuid"],
                             result["environment_uuid"])

    def test_second_legacy_with_the_same_alias_keeps_both_exact_backups(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            _legacy(service)
            source = service.store.path("debian13")
            first_source = source.read_bytes()
            first = _migration_service(root).migrate_environment("debian13")
            first_backup = Path(first["backup"])
            self.assertEqual(first_backup.read_bytes(), first_source)

            service.remove("debian13")
            service.create("debian13", None, [])
            replacement = _legacy(service)
            replacement["content_revision"] = 7
            service.store.save(replacement, None)
            second_source = source.read_bytes()
            self.assertNotEqual(second_source, first_source)
            second = _migration_service(root).migrate_environment("debian13")
            second_backup = Path(second["backup"])
            self.assertNotEqual(first_backup, second_backup)
            self.assertEqual(first_backup.read_bytes(), first_source)
            self.assertEqual(second_backup.read_bytes(), second_source)
            self.assertEqual(len(_backups(service.store)), 2)
            self.assertEqual(
                hashlib.sha256(first_source).hexdigest(),
                first_backup.name.rsplit("-", 1)[-1].removesuffix(".json"))

    def test_valid_legacy_with_older_optional_fields_still_migrates(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            record = _legacy(service)
            record.pop("system", None)
            record.pop("arch", None)
            for key in ("network", "devices", "ipc", "full_init"):
                record["runtime"].pop(key, None)
            service.store.save(record, None)
            result = _migration_service(root).migrate_environment("debian13")
            self.assertEqual(result["status"], "migrated")
            self.assertEqual(result["generation"], 1)
            self.assertEqual(len(_backups(service.store)), 1)

    def test_unreadable_record_is_not_rewritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            setup, _runtime = service_in(root)
            setup.create("debian13", None, [])
            source = setup.store.path("debian13")
            source.write_text("{not json", encoding="utf-8")
            before = source.read_bytes()
            migration = _migration_service(root)
            with self.assertRaises(UnifierError):
                migration.migrate_environment("debian13")
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(_backups(migration.store), [])
            self.assertFalse(migration.store.identity_lock_path("debian13").exists())

    def test_concurrent_processes_converge_on_one_uuid_and_one_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            setup, _runtime = service_in(root)
            setup.create("debian13", None, [])
            _legacy(setup)
            context = multiprocessing.get_context("fork")
            start = context.Event()
            results = context.Queue()
            processes = [context.Process(target=_migrate_worker, args=(str(root), start, results))
                         for _ in range(6)]
            for process in processes:
                process.start()
            start.set()
            collected = [results.get(timeout=20) for _ in processes]
            for process in processes:
                process.join(20)
                self.assertEqual(process.exitcode, 0)
            self.assertNotIn("error", [status for status, _ in collected])
            uuids = {value for _status, value in collected}
            self.assertEqual(len(uuids), 1)
            self.assertEqual(sum(1 for status, _ in collected if status == "migrated"), 1)
            record = setup.store.load("debian13")
            self.assertEqual(record["environment_uuid"], uuids.pop())
            self.assertEqual(record["generation"], 1)
            backups = _backups(setup.store)
            self.assertEqual(len(backups), 1)
            self.assertIn(".pre-identity-", backups[0].name)


if __name__ == "__main__":
    unittest.main()
