"""Offline desktop backend and guest package mapping contracts."""

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_desktop import SESSION
from test_packages import PackageBoxes, package_service
from unifier import cli, shortcuts
from unifier.desktop_clients import CLIENTS, client_packages
from unifier.errors import UnifierError
from unifier.host_portal import describe_host_portal
from unifier.identity import binding_for


class HostPortalConfiguration(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "share"
        self.env = {
            "HOME": str(self.root / "home"),
            "XDG_CONFIG_HOME": str(self.root / "user-config"),
            "XDG_CONFIG_DIRS": str(self.root / "config"),
            "XDG_DATA_DIRS": str(self.data),
            "XDG_RUNTIME_DIR": str(self.root / "runtime"),
        }
        self.write(self.data, "kde-portals.conf", "[preferred]\ndefault=kde\n")
        self.write(self.data, "gnome-portals.conf",
                   "[preferred]\ndefault=gnome;gtk\norg.freedesktop.impl.portal.FileChooser=gtk;gnome\n")
        for name, interfaces in (("kde", ("FileChooser", "ScreenCast")),
                                 ("gnome", ("FileChooser", "ScreenCast")),
                                 ("gtk", ("FileChooser",))):
            declared = ";".join(f"org.freedesktop.impl.portal.{item}" for item in interfaces)
            self.write(self.data, f"portals/{name}.portal",
                       f"[portal]\nDBusName=org.freedesktop.impl.portal.desktop.{name}\nInterfaces={declared};\n")

    @staticmethod
    def write(root, name, content):
        path = Path(root) / "xdg-desktop-portal" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def describe(self, desktop):
        return describe_host_portal({**self.env, "XDG_CURRENT_DESKTOP": desktop},
                                    sysconfdir=self.root / "etc", datadir=self.data)

    def test_kde_and_gnome_choose_backend_per_interface(self):
        for desktop, chooser, screencast in (("KDE", "kde", "kde"), ("Vendor:GNOME", "gtk", "gnome")):
            with self.subTest(desktop=desktop):
                report = self.describe(desktop)
                self.assertEqual(report["frontend"], "no-session")
                self.assertIsNone(report["interfaces"]["Camera"]["frontend_version"])
                for name, expected in (("FileChooser", chooser), ("ScreenCast", screencast)):
                    self.assertEqual(report["interfaces"][name]["configured_candidate"]["name"], expected)

    def test_first_config_wins_and_can_disable_one_capability(self):
        file = self.write(self.env["XDG_CONFIG_HOME"], "portals.conf",
                          "[preferred]\ndefault=missing;gtk;*\norg.freedesktop.impl.portal.FileChooser=none\n")
        report = self.describe("KDE")
        self.assertEqual(report["configuration"], str(file))
        interfaces = report["interfaces"]
        self.assertEqual(interfaces["FileChooser"]["preferred"], ["none"])
        self.assertIsNone(interfaces["FileChooser"]["configured_candidate"])
        self.assertEqual(interfaces["ScreenCast"]["configured_candidate"]["name"], "gnome")

    def test_unknown_configuration_does_not_guess_from_desktop_name(self):
        result = self.describe("Custom")
        self.assertEqual(result["configuration_status"], "unknown")
        self.assertIsNone(result["interfaces"]["FileChooser"]["configured_candidate"])


class DesktopPackageRuntime(PackageBoxes):
    def image_info(self, reference):
        digest = reference.rsplit("@", 1)[1]
        return {"id": digest, "digest": digest}

    def create(self, spec):
        super().create(spec)
        cid = hashlib.sha256(spec.name.encode()).hexdigest()
        self.containers[spec.name] = replace(self.containers[spec.name], container_id=cid)
        return cid

    def start(self, name, *, portal_mount=None, **kwargs):
        return super().start(self.inspect(name).name, **kwargs)


class DesktopClientTransactions(unittest.TestCase):
    def new_service(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        service, runtime = package_service(Path(directory.name), runtime=DesktopPackageRuntime())
        service.uid = os.getuid()
        return service, runtime

    def make_environment(self, target, *, desktop=True, portals=None):
        service, runtime = self.new_service()
        service.create(target, None, [], "pasta", desktop=desktop,
                       session=SESSION if desktop else None, portals=portals)
        return service, runtime

    def test_overview_uses_the_host_without_touching_the_guest(self):
        service, runtime = self.make_environment("debian13")
        before = list(runtime.events)
        out = StringIO()
        with (patch.object(cli, "service_factory", return_value=service),
              patch("unifier.host_portal.describe_host_portal", return_value={"desktops": ["GNOME"]}),
              redirect_stdout(out)):
            self.assertEqual(cli.main(["env", "desktop-integration", "debian13"]), 0)
        report = json.loads(out.getvalue())
        self.assertEqual(report["portal_bridge"], "managed")
        self.assertEqual(report["host_portal"]["desktops"], ["GNOME"])
        self.assertEqual(report["client_packages"]["qt6"], "qt6-xdgdesktopportal-platformtheme")
        self.assertEqual(runtime.events, before)

    def test_cli_installs_native_clients_and_retry_keeps_database_unchanged(self):
        cases = (
            ("debian13", ["apt-get", "install", "-y", "--no-install-recommends"],
             ["qt6-xdgdesktopportal-platformtheme", "pipewire-bin"]),
            ("arch", ["pacman", "-Syu", "--noconfirm", "--needed"], ["qt6-base", "pipewire"]),
            ("fedora44", ["dnf", "install", "-y"], ["qt6-qtbase-gui", "pipewire-libs"]),
        )
        for target, native, packages in cases:
            with self.subTest(target=target):
                service, runtime = self.make_environment(target)
                out = StringIO()
                with (patch.object(cli, "service_factory", return_value=service),
                      redirect_stdout(out), redirect_stderr(StringIO())):
                    result = cli.main(["env", "desktop-integration", target,
                                       "--install", "qt6", "--install", "pipewire", "--install", "qt6"])
                txn = json.loads(out.getvalue())
                self.assertEqual(result, 0, txn.get("native_error"))
                self.assertEqual(txn["native"][-1]["argv"], [*native, *packages])
                self.assertEqual(txn["desktop_clients"]["inventory"], {"qt6": True, "pipewire": True})
                self.assertEqual(runtime.installed[target], set(packages))
                repeat = service.install_desktop_clients(target, ["qt6", "pipewire"])
                self.assertEqual(repeat["phase"], "completed")
                self.assertEqual(repeat["desktop_clients"]["status"], "already-installed")
                self.assertEqual(repeat["native"], [])
                self.assertEqual(repeat["revision_before"], repeat["revision_after"])

    def test_managed_create_and_explicit_repair_use_the_same_native_clients(self):
        for target in ("debian13", "fedora44", "arch"):
            with self.subTest(target=target):
                service, runtime = self.new_service()
                installed = set(client_packages(target, CLIENTS).values())
                with (patch.object(cli, "service_factory", return_value=service),
                      patch("unifier.service.discover_session", return_value=SESSION),
                      redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                    self.assertEqual(cli.main(["env", "create", "--target", target,
                                               "--desktop", "--network", "pasta"]), 0)
                    record = service.store.load(target)
                    marker = record["desktop_client_provisioning"]
                    self.assertEqual(marker["phase"], "completed")
                    self.assertEqual(runtime.installed[target], installed)
                    original_txn = service.show_transaction(marker["transaction_id"])
                    self.assertEqual(original_txn["desktop_clients"]["inventory"],
                                     dict.fromkeys(CLIENTS, True))
                    self.assertEqual(cli.main(["env", "desktop-integration", target,
                                               "--repair"]), 0)
                self.assertEqual(binding_for(service.store.load(target)), binding_for(record))
                recent = [txn for txn in service.transactions_for(target)
                          if txn["id"] != marker["transaction_id"]]
                self.assertEqual(len(recent), 1)
                self.assertEqual(recent[0]["native"], [])
                self.assertEqual(recent[0]["revision_before"], recent[0]["revision_after"])
                self.assertEqual(service.inspect(target)["runtime_state"], "stopped")

    def test_automatic_provisioning_respects_offline_and_explicit_no_portals(self):
        options = [[], ["--network", "pasta", "--no-portals"]]
        for flags in options:
            with self.subTest(flags=flags):
                service, runtime = self.new_service()
                with (patch.object(cli, "service_factory", return_value=service),
                      patch("unifier.service.discover_session", return_value=SESSION),
                      redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                    self.assertEqual(cli.main(["env", "create", "--target", "debian13",
                                               "--desktop", *flags]), 0)
                record = service.store.load("debian13")
                self.assertNotIn("desktop_client_provisioning", record)
                self.assertEqual(bool(record["desktop"].get("portal_proxy")), not flags)
                self.assertEqual(service.transactions_for("debian13"), [])
                self.assertFalse(runtime.installed.get("debian13"))

    def test_shortcut_retains_failed_desktop_until_explicit_repair(self):
        service, runtime = self.new_service()
        target = "debian13"
        ctx = shortcuts.build_context(
            service.root, out=StringIO(), err=StringIO(),
            config=service.config, lock=service.lock, runtime=runtime,
            service_kwargs={"host_facts": service.host_facts})
        ctx.services[str(service.root)] = service
        provision = ["init", "--target", target, "--desktop", "--yes"]
        runtime.fail_packages.add(client_packages(target, CLIENTS)["gtk3"])
        with (patch.object(runtime, "image_info", return_value={}, create=True),
              patch("unifier.service.discover_session", return_value=SESSION)):
            self.assertEqual(shortcuts.main(provision, context=ctx), 1,
                             ctx.err.getvalue())
            first = service.store.load(target)
            history = service.transactions_for(target)
            self.assertEqual(len(history), 1)
            self.assertEqual(first["desktop_client_provisioning"]["phase"], "failed")
            state = ctx.state.load()["targets"][target]
            self.assertTrue(state["container_retained"])
            self.assertEqual(state["failure"]["phase"], "desktop-clients")
            self.assertEqual(shortcuts.main(provision, context=ctx), 1)
            self.assertEqual(service.transactions_for(target), history)

            runtime.fail_packages.clear()
            with (patch.object(cli, "service_factory", return_value=service),
                  redirect_stdout(StringIO()), redirect_stderr(StringIO())):
                self.assertEqual(cli.main(["env", "desktop-integration", target,
                                           "--repair"]), 0)
            self.assertEqual(shortcuts.main(provision, context=ctx), 0,
                             ctx.err.getvalue())
        self.assertEqual(ctx.state.load()["targets"][target]["state"], "ready")
        self.assertEqual(binding_for(service.store.load(target)), binding_for(first))
        self.assertEqual(len(service.transactions_for(target)), 2)
        self.assertEqual([event[0] for event in runtime.events].count("create"), 1)

    def test_repair_refuses_changed_binding_and_pending_recovery_before_runtime(self):
        service, runtime = self.make_environment("debian13", portals=False)
        target = "debian13"
        record = service.store.load(target)
        binding = binding_for(record)
        runtime.events.clear()
        with self.assertRaisesRegex(UnifierError, "binding is stale"):
            service.repair_desktop_clients(target, expected_binding={
                **binding, "generation": binding["generation"] + 1})
        record["package_recovery_required"] = True
        service.store.save(record, None)
        with self.assertRaisesRegex(UnifierError, "pkg recover"):
            service.repair_desktop_clients(target)
        self.assertEqual(runtime.events, [])
        self.assertEqual(service.transactions_for(target), [])

    def test_repair_of_existing_desktop_preserves_disabled_portal_setting(self):
        service, runtime = self.make_environment("debian13", portals=False)
        target = "debian13"
        before = service.store.load(target)
        txn = service.repair_desktop_clients(target)
        self.assertEqual(txn["phase"], "completed", txn.get("native_error"))
        after = service.store.load(target)
        self.assertEqual(after["desktop"], before["desktop"])
        self.assertEqual(binding_for(after), binding_for(before))
        self.assertEqual(runtime.installed[target], set(client_packages(target, CLIENTS).values()))

    def test_plain_package_install_keeps_its_scope_and_desktop_app_gets_gtk(self):
        service, runtime = self.make_environment("debian13")
        name = "com.alibabainc.dingtalk"
        plain = service.install_packages("debian13", [name])
        self.assertEqual(plain["phase"], "completed", plain.get("native_error"))
        self.assertNotIn("desktop_clients", plain)
        self.assertNotIn("libgtk-3-0t64", runtime.installed["debian13"])
        with (patch.object(service, "_application_python_ready", return_value=True),
              patch.object(service, "_desktop_fonts_ready", return_value=True)):
            txn = service.install_packages("debian13", [name], application_runtime=True)
        self.assertEqual(txn["phase"], "completed", txn.get("native_error"))
        self.assertEqual(txn["desktop_clients"]["inventory"], {"gtk3": True})
        self.assertIn("libgtk-3-0t64", runtime.installed["debian13"])

    def test_headless_cannot_install_a_desktop_profile(self):
        service, runtime = self.make_environment("debian13", desktop=False)
        before = list(runtime.events)
        self.assertEqual(service.desktop_integration("debian13")["portal_bridge"], "headless")
        with self.assertRaisesRegex(UnifierError, "requires? an environment created with --desktop"):
            service.install_desktop_clients("debian13", ["gtk3"])
        self.assertEqual(runtime.events, before)
        self.assertEqual(service.transactions_for("debian13"), [])

    def test_install_reflects_native_repository_failure(self):
        service, runtime = self.make_environment("debian13")
        provider = "qt6-xdgdesktopportal-platformtheme"
        runtime.fail_packages.add(provider)
        txn = service.install_desktop_clients("debian13", ["qt6"])
        self.assertEqual(txn["phase"], "failed")
        self.assertEqual(txn["native"][-1]["exit_code"], 100)
        self.assertEqual(txn["desktop_clients"]["inventory"], {"qt6": False})
        self.assertIn(provider, str(txn["dependency_guidance"]))
