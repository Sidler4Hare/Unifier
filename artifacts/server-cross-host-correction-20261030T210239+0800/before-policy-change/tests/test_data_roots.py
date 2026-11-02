"""Independent data-root registration, aliases, nesting and CLI facade."""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier import cli
from unifier.control import Control
from unifier.errors import UnifierError
from unifier.registry import DataRootRegistry
from test_env import service_in


def _run(argv, control):
    original = cli.control_factory
    cli.control_factory = lambda: control
    stdout, stderr = StringIO(), StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(argv)
    finally:
        cli.control_factory = original
    return code, stdout.getvalue(), stderr.getvalue()


class Registration(unittest.TestCase):
    def test_data_root_is_independent_of_environment_deletion_and_remove_keeps_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "project-data"
            data.mkdir()
            service, _runtime = service_in(root)
            service.create("debian13", None, [])
            control = Control(root)
            record = control.add_root(str(data))
            self.assertEqual(record["management_domain"], "host-project")
            self.assertEqual(record["backend"], "local-directory")
            self.assertEqual(record["access_policy"], "read-write")
            self.assertEqual(record["schema_version"], 1)
            self.assertEqual(record["locator"], str(data.resolve()))

            service.remove("debian13")
            self.assertEqual(control.show_root(record["root_uuid"]), record)
            self.assertTrue(data.is_dir())

            removed = control.remove_root(record["root_uuid"])
            self.assertEqual(removed, record)
            self.assertTrue(data.is_dir())
            self.assertEqual(control.list_roots(), [])

    def test_duplicate_real_root_and_symlink_alias_are_rejected_deterministically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            alias = root / "alias"
            os.symlink(data, alias)
            registry = DataRootRegistry(root)
            first = registry.add(str(data))
            with self.assertRaisesRegex(UnifierError, first["root_uuid"]):
                registry.add(str(data))
            with self.assertRaisesRegex(UnifierError, first["root_uuid"]):
                registry.add(str(alias))
            self.assertEqual([item["root_uuid"] for item in registry.list()],
                             [first["root_uuid"]])
            self.assertEqual(registry.show(str(alias))["root_uuid"], first["root_uuid"])

    def test_nested_roots_use_longest_component_match_not_string_prefix(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outer = root / "project"
            inner = outer / "vendor"
            sibling = root / "project-other"
            inner.mkdir(parents=True)
            sibling.mkdir()
            registry = DataRootRegistry(root)
            outside = registry.add(str(outer))
            inside = registry.add(str(inner))

            matched, relative = registry.match(str(inner / "src" / "file.c"))
            self.assertEqual(matched["root_uuid"], inside["root_uuid"])
            self.assertEqual(relative, "src/file.c")
            matched, relative = registry.match(str(outer / "README"))
            self.assertEqual(matched["root_uuid"], outside["root_uuid"])
            self.assertEqual(relative, "README")
            self.assertEqual(registry.match(str(sibling / "file")), (None, None))
            self.assertEqual(registry.match(str(root / "project-other")), (None, None))
            self.assertEqual(registry.match(str(outer) + "x"), (None, None))

    def test_unknown_backend_domain_and_policy_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            registry = DataRootRegistry(root)
            cases = (
                {"backend": "nfs"},
                {"management_domain": "container-private"},
                {"access_policy": "append-only"},
            )
            for options in cases:
                with self.subTest(options=options), self.assertRaises(UnifierError):
                    registry.add(str(data), **options)
            self.assertFalse(registry.path.exists())
            self.assertEqual(registry.list(), [])

    def test_malformed_persisted_choice_fields_fail_cleanly_and_do_not_mutate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            control = Control(root)
            control.add_root(str(data))
            registry_path = control.registry.path
            original = registry_path.read_bytes()
            cases = (
                ("backend", []), ("backend", {}), ("backend", None), ("backend", 7),
                ("management_domain", []), ("management_domain", {"x": 1}),
                ("access_policy", []), ("access_policy", None),
            )
            for field, value in cases:
                with self.subTest(field=field, value=value):
                    payload = json.loads(original)
                    payload["roots"][0][field] = value
                    registry_path.write_text(json.dumps(payload), encoding="utf-8")
                    malformed = registry_path.read_bytes()
                    with self.assertRaises(UnifierError):
                        control.list_roots()
                    self.assertEqual(registry_path.read_bytes(), malformed)
                    registry_path.write_bytes(original)

    def test_add_rejects_non_string_choice_fields(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            control = Control(root)
            cases = (
                {"backend": []}, {"backend": {}}, {"backend": None},
                {"management_domain": []}, {"access_policy": {}},
            )
            for options in cases:
                with self.subTest(options=options), self.assertRaises(UnifierError):
                    control.add_root(str(data), **options)
            self.assertFalse(control.registry.path.exists())

    def test_cli_root_list_reports_malformed_registry_without_traceback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            control = Control(root)
            control.add_root(str(data))
            payload = json.loads(control.registry.path.read_text(encoding="utf-8"))
            payload["roots"][0]["backend"] = []
            control.registry.path.write_text(json.dumps(payload), encoding="utf-8")
            code, out, err = _run(["root", "list"], control)
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("unifier:", err)
            self.assertNotIn("Traceback", err)

    def test_cli_root_and_file_commands_are_usable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            (data / "note.txt").write_text("hello\n", encoding="utf-8")
            control = Control(root)

            code, out, err = _run(["root", "add", str(data), "--policy", "read-only"], control)
            self.assertEqual(code, 0, err)
            record = json.loads(out)
            code, out, err = _run(["root", "list"], control)
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)[0]["root_uuid"], record["root_uuid"])
            code, out, err = _run(
                ["file", "resolve", record["root_uuid"], "note.txt", "--role", "project"],
                control)
            self.assertEqual(code, 0, err)
            reference = json.loads(out)
            self.assertEqual(reference["role"], "project")
            self.assertEqual(reference["access_policy"], "read-only")
            self.assertTrue(reference["capabilities"]["observe"])
            self.assertFalse(reference["capabilities"]["execute"])
            code, out, err = _run(["root", "remove", record["root_uuid"]], control)
            self.assertEqual(code, 0, err)
            self.assertTrue(data.is_dir())


if __name__ == "__main__":
    unittest.main()
