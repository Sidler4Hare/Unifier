"""Input method settings follow a verified local display per application."""

from contextlib import redirect_stdout
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_env import service_in
from unifier.cli import main
from unifier.errors import UnifierError
from unifier.handlers import execution_fingerprint
from unifier.input_method import apply_input_method, validate_input_method


SOCKET = "/tmp/.X11-unix/X7"
RECORD = {"desktop": {"mounts": [{"host": SOCKET, "container": SOCKET}]}}
BINDS = ((SOCKET, SOCKET, True, "bind"),)
HOST = {"DISPLAY": ":7.0", "XMODIFIERS": "@im=fcitx", "GTK_IM_MODULE": "fcitx"}


class InputMethod(unittest.TestCase):
    def apply(self, env, *, mounts=BINDS, host=HOST, argv=(), mode="auto", socket_present=True):
        apply_input_method(RECORD, env, mounts, argv, mode=mode, environ=host,
                           is_socket=lambda path: path == SOCKET and socket_present)
        return env

    def test_x11_launch_uses_gtk_xim_and_retains_session_values(self):
        env = {"DISPLAY": ":7", "LC_CTYPE": "C.UTF-8",
               "DBUS_SESSION_BUS_ADDRESS": "unix:path=/private/portal"}
        self.assertEqual(self.apply(dict(env)), dict(env, GTK_IM_MODULE="xim", XMODIFIERS="@im=fcitx"))

    def test_ambiguous_defaults_leave_gtk_backend_and_explicit_values_alone(self):
        env = {"DISPLAY": ":7", "WAYLAND_DISPLAY": "wayland-1"}
        self.assertEqual(self.apply(dict(env)), dict(env, XMODIFIERS="@im=fcitx"))
        gdk_x11 = dict(env, GDK_BACKEND="x11")
        self.assertEqual(self.apply(dict(gdk_x11)),
                         dict(gdk_x11, GTK_IM_MODULE="xim", XMODIFIERS="@im=fcitx"))
        self.assertEqual(self.apply(dict(env), argv=["app", "--ozone-platform=x11"]),
                         dict(env, GTK_IM_MODULE="xim", XMODIFIERS="@im=fcitx"))
        explicit = dict(env, GTK_IM_MODULE="ibus", XMODIFIERS="@im=ibus")
        self.assertEqual(self.apply(dict(explicit)), explicit)
        self.assertEqual(self.apply(dict(explicit), mode="host-xim"),
                         dict(env, GTK_IM_MODULE="xim", XMODIFIERS="@im=fcitx"))
        self.assertEqual(self.apply(dict(env), mode="native"), env)

    def test_remote_stale_or_unverified_mounts_do_not_inherit_the_host_method(self):
        env = {"DISPLAY": ":7"}
        for options in (
            {"host": dict(HOST, DISPLAY="localhost:7")},
            {"host": dict(HOST, DISPLAY=":8")},
            {"host": dict(HOST, XMODIFIERS="@im=none")},
            {"host": dict(HOST, XMODIFIERS="@im=none;unknown")},
            {"mounts": ()},
            {"mounts": (("/foreign/socket", SOCKET, True, "bind"),)},
            {"mounts": BINDS + BINDS},
            {"socket_present": False},
        ):
            with self.subTest(options=options):
                self.assertEqual(self.apply(dict(env), **options), env)
        with self.assertRaisesRegex(UnifierError, "registered local X11"):
            validate_input_method({}, {"DISPLAY": ":7"}, "host-xim")

    def test_existing_record_uses_cli_setting_at_normal_exec_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            service, runtime = service_in(Path(directory))
            service.create("debian13", None, [], desktop=True,
                           session={"env": {"DISPLAY": ":7", "WAYLAND_DISPLAY": "wayland-1"},
                                    "selinux_label": "disable",
                                    "mounts": RECORD["desktop"]["mounts"]})
            service.add_app("debian13", "demo", "/usr/bin/demo", ["/usr/bin/demo"])
            fingerprint = execution_fingerprint(service.show_app("demo"))
            name = service.store.load("debian13")["runtime"]["container_name"]
            runtime.containers[name] = replace(runtime.containers[name], mounts=BINDS)
            output = io.StringIO()
            with (patch("unifier.cli.service_factory", return_value=service),
                  patch.dict("os.environ", HOST), patch("pathlib.Path.is_socket", return_value=True),
                  redirect_stdout(output)):
                self.assertEqual(main(["app", "input-method", "demo"]), 0)
                self.assertEqual(main(["app", "input-method", "demo", "host-xim"]), 0)
                self.assertEqual(execution_fingerprint(service.show_app("demo")), fingerprint)
                service.exec_app("demo", ["/usr/bin/demo"], capture=True)
                result = runtime.events[-1][4]
                self.assertEqual(result["GTK_IM_MODULE"], "xim")
                self.assertEqual(result["XMODIFIERS"], "@im=fcitx")
                self.assertEqual(main(["app", "input-method", "demo", "auto"]), 0)
            self.assertEqual(output.getvalue().splitlines(), ["auto", "host-xim", "auto"])
            self.assertNotIn("GTK_IM_MODULE", service.show_app("demo")["env"])
