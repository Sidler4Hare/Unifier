import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from unifier.cli import main
from unifier.errors import UnifierError
from unifier.file_chooser import MODE_KEY, launch_argv, launch_environment, preflight_picker, verified_shares
from unifier.kdialog_args import parse_args
from unifier.kdialog_portal import SHARES_ENV, guest_paths, picker_options
from unifier.on_demand import LaunchGroup, managed_open
from unifier.service import EnvironmentService
from unifier.zenity_args import parse_args as parse_zenity_args
from test_desktop import SESSION, desktop_service


class KdialogTests(unittest.TestCase):
    def test_chromium_kdialog_arguments_and_portal_types(self):
        request = parse_args(["--attach=420", "--title=选择附件", "--multiple",
                              "--separate-output", "--getopenfilename", ".",
                              "文档 (*.pdf *.txt)|所有文件 (*)"])
        self.assertEqual(request.parent, "x11:1a4")
        self.assertEqual(request.title, "选择附件")
        values = picker_options(request, [("/host", "/guest", True)],
                                lambda type_, value: (type_, value), "token")
        self.assertEqual(values["filters"], ("a(sa(us))", [
            ("文档", [(0, "*.pdf"), (0, "*.txt")]), ("所有文件", [(0, "*")]),
        ]))
        self.assertEqual(values["multiple"], ("b", True))
        self.assertEqual(values["writable"], ("b", False))
        self.assertEqual(values["current_folder"], ("ay", b"/host\0"))
        self.assertIsNone(parse_args(["--version"]))

    def test_rejects_non_file_dialog_and_ambiguous_arguments(self):
        for args in (["--msgbox", "message"], ["--multiple", "--getopenfilename", "."],
                     ["--attach=4294967296", "--getexistingdirectory", "."],
                     ["--getsavefilename", "--getopenfilename", "."],
                     ["--getopenfilename", ".", "bad filter"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_args(args)

    def test_legacy_mime_filters_are_forwarded_as_mime_types(self):
        request = parse_args(["--getopenfilename", ".", "application/pdf image/*"])
        options = picker_options(request, [], lambda type_, value: (type_, value), "token")
        self.assertEqual(options["filters"], ("a(sa(us))", [
            ("application/pdf", [(1, "application/pdf")]), ("image/*", [(1, "image/*")]),
        ]))

    def test_maps_unicode_and_multiple_selections_without_copying(self):
        with tempfile.TemporaryDirectory() as tmp:
            guest = Path(tmp)
            sample = guest / "附件.pdf"
            sample.write_text("original")
            other = guest / "空格 文件.txt"
            other.write_text("second")
            shares = [("/host", str(guest), True)]
            request = parse_args(["--multiple", "--separate-output", "--getopenfilename", "."])
            uris = [sample.as_uri(), other.as_uri().replace('file:///', 'file://localhost/')]
            self.assertEqual(guest_paths(uris, shares, request), [str(sample), str(other)])
            for bad in ("file:///outside/secret", "file://remote/host/secret",
                        "file:///host/%2e%2e/secret", "file:///host/a%0ab"):
                with self.subTest(bad=bad), self.assertRaises((ValueError, OSError)):
                    guest_paths([uris[0], bad], shares, request)
            (guest / "alias").symlink_to(sample)
            with self.assertRaises(ValueError):
                guest_paths([(guest / "alias").as_uri()], shares, request)
            self.assertEqual(sample.read_text(), "original")

    def test_folder_and_save_result_require_correct_type_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            guest = Path(tmp)
            save = parse_args(["--getsavefilename", "."])
            self.assertEqual(guest_paths([(guest / "new.txt").as_uri()], [("/host", tmp, True)], save),
                             [str(guest / "new.txt")])
            self.assertFalse((guest / "new.txt").exists())
            with self.assertRaises(ValueError):
                guest_paths([(guest / "new.txt").as_uri()], [("/host", tmp, False)], save)
            (guest / "folder").mkdir()
            folder = parse_args(["--getexistingdirectory", "."])
            self.assertEqual(guest_paths([(guest / "folder").as_uri()], [("/host", tmp, False)], folder),
                             [str(guest / "folder")])
            with self.assertRaises(ValueError):
                guest_paths([(guest / "folder").as_uri()], [("/host", tmp, True)], save)


class ZenityTests(unittest.TestCase):
    def test_file_selection_options_survive_the_portal_conversion(self):
        request = parse_zenity_args([
            "--file-selection", "--title=选择", "--multiple", "--separator", ":",
            "--file-filter=图像 | *.png *.webp", "--attach=0x100",
        ])
        values = picker_options(request, [("/host", "/guest", True)],
                                lambda kind, value: (kind, value), "token")
        self.assertEqual(request.separator, ":")
        self.assertEqual(request.parent, "x11:100")
        self.assertEqual(values["filters"], ("a(sa(us))", [("图像", [(0, "*.png"), (0, "*.webp")])]))
        self.assertEqual(values["modal"], ("b", False))
        save = parse_zenity_args(["--file-selection", "--save", "--confirm-overwrite",
                                  "--filename=中文.txt", "--modal"])
        values = picker_options(save, [("/ro", "/guest/ro", False), ("/rw", "/guest/rw", True)],
                                lambda kind, value: (kind, value), "token")
        self.assertEqual(values["current_folder"], ("ay", b"/rw\0"))
        self.assertEqual(values["current_name"], ("s", "中文.txt"))
        self.assertEqual(values["modal"], ("b", True))

    def test_refuses_options_for_unrelated_dialogs_and_conflicting_requests(self):
        for args in (["--entry"], ["--file-selection", "--save", "--directory"],
                     ["--file-selection", "--separator=:"], ["--file-selection", "--title", "--modal"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                parse_zenity_args(args)


class LaunchModesTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bridge = self.root / ".lab" / "desktop" / "guest"
        self.bridge.mkdir(parents=True)
        self.inside = "/run/multilinux-portal"
        self.share = self.root / "docs"
        self.share.mkdir()
        self.record = {
            "id": "guest",
            "desktop": {"portal_proxy": {"directory": str(self.bridge), "container": self.inside}},
            "shared_mounts": [{"host": str(self.share), "container": "/guest/docs"}],
        }
        self.env = {"PATH": "/custom/bin:/usr/bin",
                    "DBUS_SESSION_BUS_ADDRESS": f"unix:path={self.inside}/bus"}
        self.mounts = [(str(self.bridge), self.inside, True, "bind"),
                       (str(self.share), "/guest/docs", True, "bind")]

    def mode(self, name):
        launch = {"env": {**self.env, MODE_KEY: name}, "on_demand": True}
        return launch_environment(self.record, launch, self.mounts, self.root)

    def test_native_keeps_existing_launch_environment(self):
        launch = {"env": dict(self.env)}
        self.assertEqual(launch_environment({}, launch, [], self.root), self.env)
        self.assertFalse(list(self.bridge.iterdir()))

    def test_native_gtk_mode_publishes_guest_only_wrapper_and_can_be_disabled(self):
        self.record["shared_mounts"][0]["container"] = str(self.share)
        self.mounts[-1] = (str(self.share), str(self.share), True, "bind")
        launch = {"env": {**self.env, MODE_KEY: "gtk-native-portal"}, "on_demand": True}
        original = dict(launch["env"])
        argv = ["/opt/app/start.sh", "中文 空格", ";echo unsafe"]
        with patch("ctypes.CDLL", side_effect=AssertionError("host must not load GTK")):
            env = launch_environment(self.record, launch, self.mounts, self.root)
            wrapped = launch_argv(self.record, launch, argv)
        self.assertEqual(env["GTK_USE_PORTAL"], "1")
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(wrapped[:2], ["/usr/bin/python3", "-I"])
        self.assertEqual(wrapped[3:], ["gtk3-native", "--", *argv])
        helper = self.bridge / Path(wrapped[2]).relative_to(self.inside)
        self.assertTrue(helper.is_file())
        self.assertEqual(launch["env"], original)
        launch["env"][MODE_KEY] = "native"
        self.assertEqual(launch_argv(self.record, launch, argv), argv)
        self.assertNotIn("GTK_USE_PORTAL", launch_environment(
            self.record, launch, self.mounts, self.root))

    def test_native_gtk_refuses_publication_symlink(self):
        self.record["shared_mounts"][0]["container"] = str(self.share)
        self.mounts[-1] = (str(self.share), str(self.share), True, "bind")
        launch = {"env": {**self.env, MODE_KEY: "gtk-native-portal"}, "on_demand": True}
        wrapped = launch_argv(self.record, launch, ["app"])
        name = Path(wrapped[2]).parent.name
        outside = self.root / "outside"
        outside.mkdir()
        (self.bridge / name).symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(UnifierError, "prepare GTK Native"):
            self.mode("gtk-native-portal")
        self.assertFalse(list(outside.iterdir()))

    def test_direct_portal_uses_delivery_relay_without_same_path_share(self):
        gtk = self.mode("gtk-portal")
        qt = self.mode("qt-portal")
        self.assertEqual(gtk["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={self.inside}/files-bus")
        self.assertEqual(qt["DBUS_SESSION_BUS_ADDRESS"], f"unix:path={self.inside}/files-bus")
        self.assertEqual(gtk["GTK_USE_PORTAL"], "1")
        self.assertNotIn("QT_QPA_PLATFORMTHEME", gtk)
        self.assertEqual(qt["QT_QPA_PLATFORMTHEME"], "xdgdesktopportal")
        self.assertNotIn("GTK_USE_PORTAL", qt)

    def test_kdialog_uses_actual_mount_and_does_not_change_global_path(self):
        env = self.mode("kdialog-portal")
        prefix, original = env["PATH"].split(":", 1)
        adapter = self.bridge / Path(prefix).name / "kdialog"
        self.assertTrue(os.access(adapter, os.X_OK))
        self.assertEqual(env["XDG_CURRENT_DESKTOP"], "KDE")
        self.assertEqual(original, self.env["PATH"])
        self.assertEqual(json.loads(env[SHARES_ENV]), [[str(self.share), "/guest/docs", True]])
        self.assertEqual(self.mode("kdialog-portal")["PATH"], env["PATH"])
        self.assertEqual(len(list(self.bridge.iterdir())), 1)

    def test_zenity_uses_the_host_portal_without_changing_the_guest_desktop(self):
        self.env["XDG_CURRENT_DESKTOP"] = "GNOME"
        env = self.mode("zenity-portal")
        prefix = env["PATH"].split(":", 1)[0]
        self.assertTrue(os.access(self.bridge / Path(prefix).name / "zenity", os.X_OK))
        self.assertEqual(env["XDG_CURRENT_DESKTOP"], "GNOME")
        self.assertNotIn("KDE_SESSION_VERSION", env)

    def test_preflight_accepts_wayland_and_x11_without_assuming_a_desktop_brand(self):
        launch = {"env": {**self.env, MODE_KEY: "zenity-portal"}, "on_demand": True}
        for host, socket in (
                ({"XDG_RUNTIME_DIR": "/session", "WAYLAND_DISPLAY": "wayland-1",
                  "XDG_CURRENT_DESKTOP": "GNOME"}, "/session/wayland-1"),
                ({"DISPLAY": ":83"}, "/tmp/.X11-unix/X83")):
            with self.subTest(host=host):
                host["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/session/bus"
                preflight_picker(self.record, launch, environ=host,
                                 is_socket=lambda candidate: candidate == socket)
                with self.assertRaisesRegex(UnifierError, "graphical host session"):
                    preflight_picker(self.record, launch, environ=host, is_socket=lambda _: False)

    def test_headless_does_not_allow_desktop_bindings_or_pass_its_mode_to_the_guest(self):
        launch = {"env": {MODE_KEY: "headless", "PATH": "/usr/bin"}}
        preflight_picker({}, launch, environ={})
        self.assertEqual(launch_environment({}, launch, [], self.root), {"PATH": "/usr/bin"})
        with self.assertRaisesRegex(UnifierError, "without desktop bindings"):
            launch_environment(self.record, launch, [], self.root)
        launch["env"]["DISPLAY"] = ":0"
        with self.assertRaisesRegex(UnifierError, "without desktop bindings"):
            preflight_picker({}, launch, environ={})

    def test_refuses_stale_or_shadowed_mounts(self):
        self.mounts.append(("/other", "/guest/docs/shadow", True, "bind"))
        self.assertEqual(verified_shares(self.record, self.mounts), [])
        self.assertTrue(self.mode("kdialog-portal")["DBUS_SESSION_BUS_ADDRESS"].endswith("/files-bus"))
        self.mounts[0] = ("/other", self.inside, True, "bind")
        with self.assertRaisesRegex(UnifierError, "bridge mount"):
            self.mode("kdialog-portal")

    def test_cli_sets_mode_on_the_named_launch(self):
        with patch("unifier.cli.service_factory") as factory, patch("builtins.print") as output:
            factory.return_value.set_app_file_chooser.return_value = {"env": {MODE_KEY: "kdialog-portal"}}
            self.assertEqual(main(["app", "file-chooser", "dingtalk", "kdialog-portal"]), 0)
            factory.return_value.set_app_file_chooser.assert_called_once_with("dingtalk", "kdialog-portal")
            output.assert_called_once_with("kdialog-portal")

    def test_cli_can_register_headless_without_creating_a_desktop(self):
        with patch("unifier.cli.service_factory") as factory, patch("builtins.print"):
            factory.return_value.add_app.return_value = {"id": "tool", "environment_id": "guest"}
            self.assertEqual(main(["app", "add", "guest", "tool", "--exec", "/usr/bin/tool",
                                   "--headless", "--on-demand"]), 0)
            call = factory.return_value.add_app.call_args
            self.assertEqual(call.kwargs["env_overlay"], {MODE_KEY: "headless"})
            self.assertFalse(call.kwargs["desktop_required"])


class LeaseTests(unittest.TestCase):
    def test_both_execution_paths_wrap_argv_after_binding_preparation(self):
        service = object.__new__(EnvironmentService)
        service.runtime = MagicMock()
        record = {"id": "guest", "runtime": {"container_id": "cid", "container_name": "name",
                  "project": "project"}, "desktop": {"portal_proxy": {
                  "container": "/run/multilinux-portal"}}}
        launch = {"env": {MODE_KEY: "gtk-native-portal"}, "user": "1000:1000"}
        env = {"GTK_USE_PORTAL": "1"}
        argv = ["app", "文件 a", "$(false)"]
        with patch.object(service, "_prepare_app_execution", return_value=(
                record, launch, "/work", env)) as prepare:
            service.exec_app("demo", argv, capture=True, timeout=30,
                             expected_execution_fingerprint="pin")
            first = service.runtime.exec.call_args
            self.assertEqual(first.args[1], launch_argv(record, launch, argv))
            self.assertEqual(first.kwargs["env"], env)
            self.assertEqual(first.kwargs["user"], "1000:1000")
            self.assertEqual(prepare.call_args.kwargs["expected_execution_fingerprint"], "pin")
            service.start_app_execution("demo", argv, token="token")
            second = service.runtime.start_application_exec.call_args
            self.assertEqual(second.args, ("cid", first.args[1]))
            self.assertEqual(second.kwargs["token"], "token")
            self.assertEqual(second.kwargs["workdir"], "/work")

    def test_wrapper_preparation_failure_is_marked_before_execution(self):
        service = object.__new__(EnvironmentService)
        service.runtime = MagicMock()
        with (patch.object(service, "_prepare_app_execution", return_value=(
                {"runtime": {}}, {"env": {}}, None, {})),
              patch("unifier.service.launch_argv", side_effect=OSError("missing launcher"))):
            with self.assertRaises(OSError) as failure:
                service.start_app_execution("demo", ["app"], token="token")
        self.assertFalse(failure.exception.execution_started)
        service.runtime.start_application_exec.assert_not_called()

    def test_missing_host_display_refuses_graphical_mode_before_start_or_bridge(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime = desktop_service(Path(tmp))
            service.create("debian13", None, [], desktop=True, session=SESSION, portals=True)
            service.add_app("debian13", "demo", "/usr/bin/true", ["/usr/bin/true"], on_demand=True)
            service.set_app_file_chooser("demo", "zenity-portal")
            before = list(runtime.events)
            with (
                patch.dict(os.environ, {"DBUS_SESSION_BUS_ADDRESS": "unix:path=/test/bus"}, clear=True),
                patch("unifier.on_demand.start_bridge") as bridge,
                self.assertRaisesRegex(UnifierError, "graphical host session"),
            ):
                managed_open(service, "demo", [])
            self.assertEqual(runtime.events, before)
            self.assertEqual(LaunchGroup(service, "debian13").read()["leases"], {})
            bridge.assert_not_called()

    def test_error_before_guest_submission_releases_the_execution_lease(self):
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime = desktop_service(Path(tmp))
            service.create("debian13", None, [])
            service.add_app("debian13", "demo", "/usr/bin/true", ["/usr/bin/true"],
                            on_demand=True, desktop_required=False, env_overlay={MODE_KEY: "headless"})
            with (
                patch.object(service, "_prepare_app_execution", side_effect=UnifierError("picker error")),
                self.assertRaisesRegex(UnifierError, "picker error"),
            ):
                managed_open(service, "demo", [])
            self.assertEqual(LaunchGroup(service, "debian13").read()["leases"], {})
            self.assertEqual(service.inspect("debian13")["runtime_state"], "stopped")
