"""E-stage reversible desktop integration journal and Exec contracts."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

import io

from multilinux import cli
from multilinux import desktop_integrations as integrations_module
from multilinux.desktop import HostDesktop, render_desktop_entry, render_exec
from multilinux.desktop_integrations import DesktopIntegrationManager
from multilinux.errors import MultilinuxError
from multilinux.handlers import execution_fingerprint
from multilinux.on_demand import managed_open, worker_command
from multilinux.project_sessions import ProjectSessions
from test_file_routes import ready


def manager_for(root: Path, service):
    host = root / "desktop"
    return DesktopIntegrationManager(
        root, service, HostDesktop(host / "applications", host / "mime"))


class InstallAndRevoke(unittest.TestCase):
    def test_project_terminal_entry_is_journaled_and_removed_exactly(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Demo project",
                                     terminal=True)
            desktop = Path(record["desktop_path"])
            self.assertEqual(record["state"], "installed")
            self.assertTrue(desktop.is_file())
            text = desktop.read_text(encoding="utf-8")
            self.assertIn("Terminal=true", text)
            self.assertIn(project["project_uuid"], text)
            self.assertIn("project shell", text)
            self.assertEqual(record["files"][0]["before"], None)
            self.assertIsNotNone(record["files"][0]["after_sha256"])
            # Repeated identical export is idempotent, not a second journal.
            again = manager.install(project["project_uuid"], name="Demo project",
                                    terminal=True)
            self.assertEqual(again["integration_uuid"], record["integration_uuid"])
            self.assertEqual(len(manager.list()), 1)
            revoked = manager.revoke(record["integration_uuid"])
            self.assertEqual(revoked["state"], "revoked")
            self.assertFalse(desktop.exists())
            self.assertFalse(manager.host.mimeapps.exists())
            # Revoking again is a no-op.
            self.assertEqual(manager.revoke(record["integration_uuid"])["state"], "revoked")

    def test_app_mime_association_preserves_foreign_defaults_and_restores_bytes(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            manager.host.mimeapps.parent.mkdir(parents=True, exist_ok=True)
            original = b"[Default Applications]\ntext/plain=kate.desktop;\n"
            manager.host.mimeapps.write_bytes(original)
            foreign = manager.host.applications / "kate.desktop"
            foreign.write_text("[Desktop Entry]\nName=Kate\n", encoding="utf-8")
            record = manager.install(
                project["project_uuid"], app_id="freecad", name="FreeCAD note",
                mime_type="application/x-multilinux-note", extension="mlnote")
            desktop = Path(record["desktop_path"])
            text = desktop.read_text(encoding="utf-8")
            self.assertIn("app open freecad", text)
            self.assertIn("--project " + project["project_uuid"], text)
            self.assertIn(" %f", text)
            mimeapps = manager.host.mimeapps.read_text(encoding="utf-8")
            self.assertIn("text/plain=kate.desktop;", mimeapps)
            self.assertIn("application/x-multilinux-note=" + desktop.name, mimeapps)
            xml = manager.host.mime_packages / (record["integration_uuid"] + ".xml")
            self.assertTrue(xml.is_file())
            self.assertEqual(foreign.read_text(encoding="utf-8"),
                             "[Desktop Entry]\nName=Kate\n")
            manager.revoke(record["integration_uuid"])
            self.assertEqual(manager.host.mimeapps.read_bytes(), original)
            self.assertFalse(desktop.exists())
            self.assertFalse(xml.exists())
            self.assertTrue(foreign.is_file())

    def test_foreign_desktop_file_is_never_replaced_and_conflict_is_journaled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            manager.host.applications.mkdir(parents=True, exist_ok=True)
            foreign = manager.host.applications / "taken.desktop"
            foreign.write_text("foreign application\n", encoding="utf-8")
            with self.assertRaisesRegex(MultilinuxError, "refusing to replace"):
                manager.install(project["project_uuid"], name="Taken",
                                desktop_alias="taken")
            self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign application\n")
            self.assertEqual(manager.list()[0]["state"], "conflict")
            self.assertEqual(manager.recover()[0]["action"], "conflict")
            self.assertEqual(foreign.read_text(encoding="utf-8"), "foreign application\n")


class ConflictAndRecovery(unittest.TestCase):
    def test_external_desktop_replacement_is_preserved_on_revoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Demo")
            desktop = Path(record["desktop_path"])
            desktop.write_text("later user choice\n", encoding="utf-8")
            with self.assertRaisesRegex(MultilinuxError, "needs reconciliation"):
                manager.revoke(record["integration_uuid"])
            self.assertEqual(desktop.read_text(encoding="utf-8"), "later user choice\n")
            conflicted = manager.show(record["integration_uuid"])
            self.assertEqual(conflicted["state"], "conflict")
            self.assertEqual(conflicted["conflict"]["observed_sha256"],
                             __import__("hashlib").sha256(b"later user choice\n").hexdigest())
            self.assertEqual(manager.recover()[0]["action"], "conflict")
            self.assertEqual(desktop.read_text(encoding="utf-8"), "later user choice\n")

    def test_later_mimeapps_edit_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(
                project["project_uuid"], app_id="freecad",
                mime_type="application/x-multilinux-note", extension="mlnote")
            later = b"[Default Applications]\ntext/plain=other.desktop;\n"
            manager.host.mimeapps.write_bytes(later)
            with self.assertRaisesRegex(MultilinuxError, "needs reconciliation"):
                manager.revoke(record["integration_uuid"])
            self.assertEqual(manager.host.mimeapps.read_bytes(), later)
            self.assertTrue(Path(record["desktop_path"]).exists())

    def test_recover_is_idempotent_for_published_and_unpublished_states(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Demo")
            # Simulate a journal left before the final state write.
            manager._save({**record, "state": "planned"})
            self.assertEqual(manager.recover()[0]["action"], "installed")
            self.assertEqual(manager.show(record["integration_uuid"])["state"], "installed")
            manager._save({**manager.show(record["integration_uuid"]), "state": "planned"})
            manager.revoke(record["integration_uuid"])
            self.assertEqual(manager.recover()[0]["action"], "revoked")
            self.assertFalse(Path(record["desktop_path"]).exists())

    def test_revoke_project_restores_all_its_integrations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, document = ready(root)
            manager = manager_for(root, service)
            first = manager.install(project["project_uuid"], name="One")
            second = manager.install(
                project["project_uuid"], app_id="freecad", name="Two",
                mime_type="application/x-multilinux-note", extension="mlnote")
            result = manager.revoke(project["project_uuid"])
            self.assertEqual(len(result["integrations"]), 2)
            self.assertFalse(Path(first["desktop_path"]).exists())
            self.assertFalse(Path(second["desktop_path"]).exists())


class LauncherPins(unittest.TestCase):
    def test_app_exec_pins_integration_instance_fingerprint_and_file_role(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(
                project["project_uuid"], app_id="freecad",
                mime_type="application/x-multilinux-note", extension="mlnote")
            launch = service.show_app("freecad")
            text = Path(record["desktop_path"]).read_text(encoding="utf-8")
            self.assertEqual(record["app_instance_uuid"], launch["app_instance_uuid"])
            self.assertEqual(record["execution_fingerprint"],
                             execution_fingerprint(launch))
            self.assertIn("--integration " + record["integration_uuid"], text)
            self.assertIn("--app-instance " + record["app_instance_uuid"], text)
            self.assertIn("--execution-fingerprint "
                          + record["execution_fingerprint"], text)
            self.assertIn("X-Multilinux-Integration=" + record["integration_uuid"], text)
            exec_line = [line for line in text.splitlines()
                         if line.startswith("Exec=")][0]
            self.assertTrue(exec_line.endswith(" %f"))
            self.assertGreater(exec_line.index("--integration"),
                               exec_line.index("--project"))
            # The journal's stored path is authoritative: another manager with
            # different default HostDesktop paths still validates this entry.
            other = DesktopIntegrationManager(
                root, service, HostDesktop(root / "unrelated/apps",
                                           root / "unrelated/mime"))
            validated = other.validate_launcher(
                record["integration_uuid"], project_uuid=project["project_uuid"],
                app_id="freecad",
                app_instance_uuid=record["app_instance_uuid"],
                execution_fingerprint=record["execution_fingerprint"])
            self.assertEqual(validated["desktop_path"], record["desktop_path"])
            self.assertEqual(
                manager.validate_launcher(
                    record["integration_uuid"],
                    project_uuid=project["project_uuid"], app_id="freecad",
                    app_instance_uuid=record["app_instance_uuid"],
                    execution_fingerprint=record["execution_fingerprint"])
                ["integration_uuid"], record["integration_uuid"])

    def test_project_exec_pins_the_integration_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Project")
            text = Path(record["desktop_path"]).read_text(encoding="utf-8")
            self.assertIn("project shell " + project["project_uuid"], text)
            self.assertIn("--integration " + record["integration_uuid"], text)
            self.assertEqual(
                manager.validate_launcher(
                    record["integration_uuid"],
                    project_uuid=project["project_uuid"], app_id=None)
                ["integration_uuid"], record["integration_uuid"])

    def test_recreated_app_and_revoked_launcher_are_refused_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], app_id="freecad")
            old_instance = record["app_instance_uuid"]
            old_fingerprint = record["execution_fingerprint"]
            service.remove_app("freecad")
            service.add_app("arch", "freecad", "/usr/bin/freecad",
                            ["/usr/bin/freecad"], workdir=str(root / "project"))
            self.assertNotEqual(service.show_app("freecad")["app_instance_uuid"],
                                old_instance)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "instance changed"):
                service.open_app(
                    "freecad", [], expected_app_instance_uuid=old_instance,
                    expected_execution_fingerprint=old_fingerprint,
                    integration_uuid=record["integration_uuid"],
                    project_uuid=project["project_uuid"])
            self.assertFalse(any(event[0] == "start" for event in runtime.events))
            self.assertEqual(service.inspect("arch")["runtime_state"], "stopped")
            # The old cached launcher pins are also rejected directly.
            with self.assertRaisesRegex(MultilinuxError, "instance changed"):
                manager.validate_launcher(
                    record["integration_uuid"],
                    project_uuid=project["project_uuid"], app_id="freecad",
                    app_instance_uuid=old_instance,
                    execution_fingerprint=old_fingerprint)
            manager.revoke(record["integration_uuid"])
            with self.assertRaisesRegex(MultilinuxError, "revoked"):
                manager.validate_launcher(
                    record["integration_uuid"],
                    project_uuid=project["project_uuid"], app_id="freecad",
                    app_instance_uuid=old_instance,
                    execution_fingerprint=old_fingerprint)

    def test_detached_worker_command_and_recheck_carry_the_exact_pins(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], app_id="freecad")
            old_instance = record["app_instance_uuid"]
            old_fingerprint = record["execution_fingerprint"]
            command = worker_command(
                service, "freecad", ["file.mlnote"],
                expected_app_instance_uuid=old_instance,
                expected_execution_fingerprint=old_fingerprint,
                integration_uuid=record["integration_uuid"],
                project_uuid=project["project_uuid"])
            self.assertIn("--integration", command)
            self.assertIn(record["integration_uuid"], command)
            self.assertIn("--app-instance", command)
            self.assertIn(old_instance, command)
            self.assertIn("--execution-fingerprint", command)
            self.assertIn(old_fingerprint, command)
            service.remove_app("freecad")
            service.add_app("arch", "freecad", "/usr/bin/freecad",
                            ["/usr/bin/freecad"], workdir=str(root / "project"))
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "instance changed"):
                managed_open(
                    service, "freecad", [],
                    expected_app_instance_uuid=old_instance,
                    expected_execution_fingerprint=old_fingerprint,
                    integration_uuid=record["integration_uuid"],
                    project_uuid=project["project_uuid"])
            self.assertFalse(any(event[0] == "start" for event in runtime.events))

    def test_cli_open_with_the_old_entry_pins_rejects_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], app_id="freecad")
            old_instance = record["app_instance_uuid"]
            old_fingerprint = record["execution_fingerprint"]
            service.remove_app("freecad")
            service.add_app("arch", "freecad", "/usr/bin/freecad",
                            ["/usr/bin/freecad"], workdir=str(root / "project"))
            runtime.events.clear()
            sessions = ProjectSessions(root, service)
            with patch.object(cli, "service_factory", lambda: service), \
                 patch.object(cli, "session_factory", lambda: sessions), \
                 patch.object(cli, "integration_factory", lambda: manager):
                with patch("sys.stderr", new_callable=io.StringIO):
                    code = cli.main([
                        "app", "open", "freecad",
                        "--project", project["project_uuid"],
                        "--integration", record["integration_uuid"],
                        "--app-instance", old_instance,
                        "--execution-fingerprint", old_fingerprint,
                        "--",
                    ])
            self.assertEqual(code, 1)
            self.assertFalse(any(event[0] == "start" for event in runtime.events))

    def test_a_cached_project_launcher_is_refused_after_revoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Project")
            manager.revoke(record["integration_uuid"])
            with self.assertRaisesRegex(MultilinuxError, "revoked"):
                manager.validate_launcher(
                    record["integration_uuid"],
                    project_uuid=project["project_uuid"], app_id=None)


class ExactRestoreAndConflicts(unittest.TestCase):
    def test_revoke_restores_exact_mimeapps_bytes_without_trailing_newline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            manager.host.mimeapps.parent.mkdir(parents=True, exist_ok=True)
            original = b"# user comment\n[Default Applications]\ntext/plain=kate.desktop;"
            manager.host.mimeapps.write_bytes(original)
            record = manager.install(
                project["project_uuid"], app_id="freecad",
                mime_type="application/x-multilinux-note", extension="mlnote")
            self.assertNotEqual(manager.host.mimeapps.read_bytes(), original)
            manager.revoke(record["integration_uuid"])
            self.assertEqual(manager.host.mimeapps.read_bytes(), original)
            self.assertFalse(Path(record["desktop_path"]).exists())

    def test_conflict_journal_is_not_turned_into_a_clean_revoke(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Project")
            desktop = Path(record["desktop_path"])
            desktop.write_text("external choice\n", encoding="utf-8")
            with self.assertRaisesRegex(MultilinuxError, "needs reconciliation"):
                manager.revoke(record["integration_uuid"])
            self.assertEqual(manager.show(record["integration_uuid"])["state"],
                             "conflict")
            with self.assertRaisesRegex(MultilinuxError, "is in conflict"):
                manager.revoke(record["integration_uuid"])
            self.assertEqual(desktop.read_text(encoding="utf-8"), "external choice\n")
            self.assertEqual(manager.show(record["integration_uuid"])["state"],
                             "conflict")

    def test_failed_revoke_is_journaled_as_mixed_conflict_and_not_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            manager.host.mimeapps.parent.mkdir(parents=True, exist_ok=True)
            manager.host.mimeapps.write_bytes(
                b"[Default Applications]\ntext/plain=kate.desktop;\n")
            record = manager.install(
                project["project_uuid"], app_id="freecad",
                mime_type="application/x-multilinux-note", extension="mlnote")
            desktop = Path(record["desktop_path"])
            with patch.object(integrations_module, "_atomic_write",
                              side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    manager.revoke(record["integration_uuid"])
            current = manager.show(record["integration_uuid"])
            self.assertEqual(current["state"], "conflict")
            self.assertIn("revoke failed", current["conflict"]["reason"])
            self.assertTrue(desktop.exists())
            self.assertEqual(manager.recover()[0]["action"], "conflict")
            self.assertEqual(manager.show(record["integration_uuid"])["state"],
                             "conflict")

    def test_revoke_intent_before_first_effect_is_finished_not_regranted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, service, _runtime, project, _root, _handler, _document = ready(root)
            manager = manager_for(root, service)
            record = manager.install(project["project_uuid"], name="Project")
            current = manager.show(record["integration_uuid"])
            # A crash after durable revoke intent but before any file effect
            # must finish the revoke, never turn the entry back into installed.
            manager._save({**current, "state": "revoking"})
            self.assertEqual(manager.recover()[0]["action"], "revoked")
            self.assertEqual(manager.show(record["integration_uuid"])["state"],
                             "revoked")
            self.assertFalse(Path(record["desktop_path"]).exists())
            self.assertEqual(manager.recover()[0]["action"], "revoked")


class ExecRendering(unittest.TestCase):
    def test_literal_percent_and_special_arguments_are_encoded_once(self):
        line = render_exec(["launcher", "a b", "100%", "$HOME", "`x`", 'q"q'], "%f")
        self.assertTrue(line.startswith("launcher "))
        self.assertIn('"a b"', line)
        self.assertIn('"100%%"', line)
        self.assertIn('"\\$HOME"', line)
        self.assertIn('"\\`x\\`"', line)
        self.assertIn('"q\\"q"', line)
        self.assertTrue(line.endswith(" %f"))
        self.assertNotIn(" %f %", line)

    def test_desktop_entry_keeps_file_field_code_outside_quoting(self):
        text = render_desktop_entry(
            launcher=["/opt/My App/run", "--name", "100%"],
            app_id="demo", environment_id="arch", project="p-uuid",
            mime_type="application/x-multilinux-note", name="Demo")
        exec_line = [line for line in text.splitlines() if line.startswith("Exec=")][0]
        self.assertIn('"/opt/My App/run"', exec_line)
        self.assertIn('"100%%"', exec_line)
        self.assertTrue(exec_line.endswith(" %f"))
        self.assertIn("MimeType=application/x-multilinux-note;", text)


if __name__ == "__main__":
    unittest.main()
