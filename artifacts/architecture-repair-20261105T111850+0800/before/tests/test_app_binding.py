"""Application launch records pin one environment UUID/generation/container."""

import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup, managed_open
from test_desktop import SESSION, desktop_service


def ready(root, *, on_demand=True):
    notes = root / "notes"
    notes.mkdir()
    service, runtime = desktop_service(root)
    service.create("debian13", None, [str(notes)], desktop=True, session=SESSION)
    service.add_app(
        "debian13", "notes", "/usr/bin/true", ["/usr/bin/true"], on_demand=on_demand)
    return service, runtime, notes


class Binding(unittest.TestCase):
    def test_new_launch_pins_the_exact_environment_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, _notes = ready(Path(temporary))
            record = service.store.load("debian13")
            launch = service.show_app("notes")
            self.assertEqual(launch["schema_version"], 2)
            self.assertEqual(launch["environment_uuid"], record["environment_uuid"])
            self.assertEqual(launch["generation"], record["generation"])
            self.assertEqual(launch["container_id"], record["runtime"]["container_id"])
            self.assertEqual(launch["environment_binding"], {
                "environment_id": record["id"],
                "environment_uuid": record["environment_uuid"],
                "generation": record["generation"],
                "container_id": record["runtime"]["container_id"],
            })

    def test_legacy_environment_cannot_gain_a_new_application_before_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            notes = root / "notes"
            notes.mkdir()
            service, runtime = desktop_service(root)
            service.create("debian13", None, [str(notes)], desktop=True, session=SESSION)
            record = service.store.load("debian13")
            record.pop("environment_uuid")
            record.pop("generation")
            record["schema_version"] = 1
            service.store.save(record, None)
            runtime.events.clear()
            with self.assertRaisesRegex(UnifierError, "env migrate"):
                service.add_app(
                    "debian13", "notes", "/usr/bin/true", ["/usr/bin/true"])
            self.assertFalse(service._launch_exists("notes"))
            self.assertEqual(runtime.events, [])

    def test_same_name_recreation_is_refused_before_any_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, _notes = ready(root)
            old = service.store.load("debian13")
            old_binding = service.show_app("notes")["environment_binding"]

            service.remove("debian13")
            service.create("debian13", None, [str(root / "notes")], desktop=True, session=SESSION)
            new = service.store.load("debian13")
            self.assertNotEqual(old["environment_uuid"], new["environment_uuid"])
            runtime.events.clear()

            for action in (
                lambda: service.exec_app("notes", ["/usr/bin/true"]),
                lambda: managed_open(service, "notes", []),
                lambda: service.open_app("notes", []),
                lambda: LaunchGroup(service, "debian13").acquire("notes"),
            ):
                with self.subTest(action=getattr(action, "__name__", "launch")), \
                     self.assertRaisesRegex(UnifierError, "stale|re-register"):
                    action()
                self.assertEqual(runtime.events, [])
            self.assertEqual(service.show_app("notes")["environment_binding"], old_binding)

    def test_generation_and_container_mismatch_are_refused_before_start(self):
        for field, value in (("generation", 99), ("container_id", "cid-other-instance")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                service, runtime, _notes = ready(Path(temporary))
                launch = service.show_app("notes")
                launch[field] = value
                launch["environment_binding"][field] = value
                service.store.save_launch(launch)
                runtime.events.clear()
                with self.assertRaisesRegex(UnifierError, "stale"):
                    service.exec_app("notes", ["/usr/bin/true"])
                self.assertEqual(runtime.events, [])

    def test_legacy_launch_stays_readable_but_requires_explicit_re_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _notes = ready(Path(temporary))
            launch = service.show_app("notes")
            launch.pop("environment_binding")
            launch["schema_version"] = 1
            service.store.save_launch(launch)
            runtime.events.clear()

            self.assertEqual(service.show_app("notes")["id"], "notes")
            for action in (
                lambda: service.exec_app("notes", ["/usr/bin/true"]),
                lambda: managed_open(service, "notes", []),
                lambda: service.open_app("notes", []),
                lambda: service.export_app("notes"),
            ):
                with self.assertRaisesRegex(UnifierError, "legacy launch record"):
                    action()
                self.assertEqual(runtime.events, [])

    def test_shared_workdir_rejects_dotdot_symlink_escape_and_missing_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            notes = root / "notes"
            notes.mkdir()
            sub = notes / "sub"
            sub.mkdir()
            secret = root / "secret"
            secret.mkdir()
            service, runtime = desktop_service(root)
            service.create("debian13", None, [str(notes)], desktop=True, session=SESSION)
            launch = service.add_app(
                "debian13", "inside", "/usr/bin/true", ["/usr/bin/true"], workdir=str(sub))
            self.assertEqual(launch["workdir"], str(sub))
            after_valid = list(runtime.events)

            escaped = notes / ".." / "secret"
            with self.assertRaisesRegex(UnifierError, "must not contain"):
                service.add_app("debian13", "dotdot", "/usr/bin/true", ["/usr/bin/true"],
                                workdir=str(escaped))
            os.symlink(secret, notes / "escape")
            with self.assertRaisesRegex(UnifierError, "outside|shared mount"):
                service.add_app("debian13", "symlink", "/usr/bin/true", ["/usr/bin/true"],
                                workdir=str(notes / "escape"))
            with self.assertRaisesRegex(UnifierError, "valid shared directory|existing directory"):
                service.add_app("debian13", "missing", "/usr/bin/true", ["/usr/bin/true"],
                                workdir=str(notes / "missing"))
            self.assertEqual(runtime.events, after_valid)
            for app_id in ("dotdot", "symlink", "missing"):
                self.assertFalse(service._launch_exists(app_id))


if __name__ == "__main__":
    unittest.main()
