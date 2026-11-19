"""Offline contracts for the standard Portal configuration on new desktops."""

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_cli_shortcuts import context, run_ctx
from test_desktop import SESSION
from test_env import service_in
from unifier import cli
from unifier.file_chooser import MODE_KEY
from unifier.portal_mount import PORTAL_GUEST
from unifier.runtime import ExecResult


class DesktopPortalDefaultTests(unittest.TestCase):
    def test_env_create_defaults_to_managed_arch_bridge_without_static_bind(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            service.uid = os.getuid()
            with (
                patch.object(cli, "service_factory", return_value=service),
                patch("unifier.service.discover_session", return_value=SESSION),
                redirect_stdout(StringIO()),
            ):
                self.assertEqual(cli.main(["env", "create", "--target", "arch", "--desktop"]), 0)
            desktop = service.store.load("arch")["desktop"]
            directory = root.resolve() / ".lab/desktop/arch"
            self.assertEqual(desktop["portal_proxy"], {
                "directory": str(directory), "container": PORTAL_GUEST, "managed_mount": True,
            })
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual(desktop["env"], SESSION["env"])
            spec = next(value for event, value, *_ in runtime.events if event == "create")
            self.assertNotIn(PORTAL_GUEST, (guest for _, guest in spec.session_mounts))

    def test_env_create_opt_out_headless_and_legacy(self):
        cases = (
            ([], "headless"),
            (["--desktop", "--no-portals"], "disabled"),
            (["--desktop", "--portals"], "legacy"),
        )
        for options, expected in cases:
            with self.subTest(options=options), tempfile.TemporaryDirectory() as temporary:
                service, _ = service_in(Path(temporary))
                with (
                    patch.object(cli, "service_factory", return_value=service),
                    patch("unifier.service.discover_session", return_value=SESSION),
                    redirect_stdout(StringIO()),
                ):
                    self.assertEqual(cli.main(["env", "create", "--target", "debian13", *options]), 0)
                desktop = service.store.load("debian13")["desktop"]
                if expected == "headless":
                    self.assertIsNone(desktop)
                elif expected == "legacy":
                    self.assertEqual(desktop["portal_proxy"]["container"], PORTAL_GUEST)
                    self.assertNotIn("managed_mount", desktop["portal_proxy"])
                else:
                    self.assertNotIn("portal_proxy", desktop)

    def test_shortcut_creation_applies_portal_only_to_new_desktop_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = context(Path(temporary))
            ctx.service_kwargs["uid"] = os.getuid()
            ctx.service_kwargs["gid"] = os.getgid()
            with patch("unifier.service.discover_session", return_value=SESSION):
                commands = (
                    ["init", "--target", "debian13", "--yes", "--desktop", "--no-portals"],
                    ["configure", "--target", "debian13", "--target", "arch", "--yes", "--desktop"],
                )
                for argv in commands:
                    code, out, err = run_ctx(ctx, argv)
                    self.assertEqual(code, 0, (argv, out, err))
            service = ctx.service_for("arch")
            self.assertTrue(service.store.load("arch")["desktop"]["portal_proxy"]["managed_mount"])
            self.assertNotIn("portal_proxy", service.store.load("debian13")["desktop"])

    def test_direct_registration_profiles_follow_desktop_intent_and_overrides(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _ = service_in(Path(temporary))
            service.uid = os.getuid()
            service.create("arch", None, [], desktop=True, session=SESSION, portals=None)
            cases = (
                ("dingtalk", "/usr/bin/example", {}, "gtk-portal"),
                ("vendor", "/opt/apps/com.alibabainc.dingtalk/files/Elevator.sh", {}, "gtk-native-portal"),
                ("explicit", "/usr/bin/example", {"env_overlay": {MODE_KEY: "native"}}, "native"),
                ("agnostic", "/usr/bin/example", {"desktop_required": False}, None),
            )
            with (
                patch.object(service, "_ensure_running", return_value=True),
                patch.object(service, "_exec_record", return_value=ExecResult(0, "", "")),
            ):
                for alias, executable, options, expected in cases:
                    with self.subTest(app=alias):
                        launch = service.add_app(
                            "arch", alias, executable, [executable], on_demand=True, **options)
                        self.assertEqual(launch["env"].get(MODE_KEY), expected)
                        self.assertNotIn("LD_LIBRARY_PATH", launch["env"])
                        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", launch["env"])

    def test_install_uses_package_desktop_identity_and_leaves_cli_apps_native(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = context(Path(temporary))
            ctx.service_kwargs["uid"] = os.getuid()
            ctx.service_kwargs["gid"] = os.getgid()
            with patch("unifier.service.discover_session", return_value=SESSION):
                code, out, err = run_ctx(ctx, ["init", "--target", "arch", "--desktop", "--yes"])
            self.assertEqual(code, 0, (out, err))
            service = ctx.service_for("arch")
            entries = {
                "office": {"executable": "/usr/bin/office", "menu": {"name": "Office"}},
                "dingtalk": {"executable": "/usr/bin/vendor", "menu": {"name": "DingTalk"},
                             "menu_desktop_id": "com.alibabainc.dingtalk"},
                "terminal": {"executable": "/usr/bin/terminal", "menu": {"terminal": True}},
                "tool": {"executable": "/usr/bin/tool"},
                "explicit-gui": {"executable": "/usr/bin/gui"},
            }
            expected = {"office": "gtk-portal", "dingtalk": "gtk-native-portal",
                        "terminal": None, "tool": None}
            with (
                patch.object(service, "install_packages", return_value={"phase": "completed"}),
                patch.object(service, "discover_app_entries",
                             side_effect=lambda env, packages: [entries[packages[0]]]),
                patch.object(service, "_ensure_running", return_value=True),
                patch.object(service, "_exec_record", return_value=ExecResult(0, "", "")),
            ):
                for package, mode in expected.items():
                    with self.subTest(package=package):
                        code, out, err = run_ctx(ctx, ["install", "--app", package, package])
                        self.assertEqual(code, 0, (out, err))
                        launch = service.app_by_alias("arch", package)
                        self.assertEqual(launch["env"].get(MODE_KEY), mode)
                argv = ["install", "--gui", "--app", "gui", "explicit-gui"]
                code, out, err = run_ctx(ctx, argv)
                self.assertEqual(code, 0, (out, err))
                launch = service.app_by_alias("arch", "gui")
                self.assertEqual(launch["env"][MODE_KEY], "gtk-portal")
                service.set_app_file_chooser(launch["id"], "native")
                code, out, err = run_ctx(ctx, argv)
                self.assertEqual(code, 0, (out, err))
                self.assertNotIn(MODE_KEY, service.app_by_alias("arch", "gui")["env"])
