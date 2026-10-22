"""Offline contracts for host session mounts, launch records and export."""

from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from multilinux import cli
from multilinux.desktop import (
    HostDesktop,
    add_mime_association,
    discover_session,
    remove_mime_association,
)
from multilinux.errors import MultilinuxError
from multilinux.runtime import creation_argv
from test_env import PROFILES, service_in


SESSION = {
    "selinux_label": "disable",
    "reason": "test",
    "env": {
        "WAYLAND_DISPLAY": "wayland-0",
        "XDG_RUNTIME_DIR": "/run/user/1000",
        "DISPLAY": ":0",
        "XAUTHORITY": "/tmp/ml-xauthority",
        "PULSE_SERVER": "unix:/run/user/1000/pulse/native",
    },
    "mounts": [
        {"host": "/run/user/1000/wayland-0", "container": "/run/user/1000/wayland-0"},
        {"host": "/tmp/.X11-unix/X0", "container": "/tmp/.X11-unix/X0"},
        {"host": "/tmp/xauth-host", "container": "/tmp/ml-xauthority"},
        {"host": "/run/user/1000/pulse/native", "container": "/run/user/1000/pulse/native"},
    ],
}


class Cache:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()


def desktop_service(root: Path):
    service, runtime = service_in(root)
    host = root / "host-desktop"
    service.host_desktop = HostDesktop(host / "applications", host / "mime")
    service.cache_runner = Cache()
    return service, runtime


class SessionMounts(unittest.TestCase):
    def test_discovery_keeps_present_sockets_and_requires_a_display(self):
        present = {
            "/run/user/1000/wayland-0",
            "/tmp/.X11-unix/X0",
            "/run/user/1000/pulse/native",
            "/tmp/xauth-host",
        }
        session = discover_session(
            {
                "XDG_RUNTIME_DIR": "/run/user/1000",
                "WAYLAND_DISPLAY": "wayland-0",
                "DISPLAY": ":0",
                "XAUTHORITY": "/tmp/xauth-host",
            },
            is_socket=present.__contains__,
            is_file=lambda path: path == "/tmp/xauth-host",
        )
        self.assertEqual(session["selinux_label"], "disable")
        self.assertEqual(session["env"]["XAUTHORITY"], "/tmp/ml-xauthority")
        self.assertEqual(session["env"]["PULSE_SERVER"], "unix:/run/user/1000/pulse/native")
        self.assertTrue(all(item["options"] == "" for item in session["mounts"]))
        with self.assertRaises(MultilinuxError):
            discover_session({}, is_socket=lambda _path: False, is_file=lambda _path: False)

    def test_desktop_create_mounts_sockets_without_relabeling_them(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            notes = root / "notes"
            notes.mkdir()
            service, runtime = desktop_service(root)
            record = service.create("debian13", None, [str(notes)], "pasta",
                                    desktop=True, session=SESSION)
            spec = runtime.events[0][1]
            argv = creation_argv(spec)
            joined = " ".join(argv)
            self.assertIn("label=disable", argv)
            self.assertIn("/run/user/1000/wayland-0:/run/user/1000/wayland-0", joined)
            self.assertNotIn("/run/user/1000/wayland-0:/run/user/1000/wayland-0:z", joined)
            self.assertIn(f"{notes.resolve().as_posix()}:{notes.resolve().as_posix()}:z", joined)
            self.assertEqual(record["shared_mounts"][0]["options"], "z")
            self.assertEqual(record["desktop"]["mounts"][0]["options"], "")
            self.assertEqual(record["runtime"]["selinux_label"], "disable")
            runtime.identities["plain"] = dict(PROFILES["fedora44"])
            self.assertIsNone(service.create("fedora44", "plain", [], "none")["desktop"])
            created = [event[1] for event in runtime.events if event[0] == "create"]
            plain = creation_argv(created[-1])
            self.assertNotIn("--security-opt", plain)
            with self.assertRaises(MultilinuxError):
                service.create("arch", "again", [], session=SESSION)


class LaunchRecords(unittest.TestCase):
    def _ready(self, root: Path):
        notes = root / "notes"
        notes.mkdir()
        service, runtime = desktop_service(root)
        service.create("debian13", None, [str(notes)], "pasta", desktop=True, session=SESSION)
        return service, runtime, notes

    def test_add_open_and_export_stay_on_the_recorded_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, notes = self._ready(root)
            with self.assertRaises(MultilinuxError):
                service.add_app("debian13", "notes", "/usr/bin/gvim", ["/usr/bin/gvim"],
                                workdir="/tmp/elsewhere")
            self.assertFalse(service._launch_exists("notes"))
            launch = service.add_app(
                "debian13", "notes", "/usr/bin/gvim",
                ["/usr/bin/gvim", "--nofork"], workdir=str(notes),
                env_overlay={"GDK_BACKEND": "x11"})
            self.assertEqual(launch["env"]["GDK_BACKEND"], "x11")
            self.assertEqual(launch["env"]["DISPLAY"], ":0")
            exported = service.export_app("notes")
            desktop = Path(exported["export"]["desktop_path"])
            text = desktop.read_text(encoding="utf-8")
            self.assertIn("app open notes --", text)
            self.assertNotIn("Exec=/usr/bin/gvim", text)
            self.assertIn("X-Multilinux-App=notes", text)
            mimeapps = service.host_desktop.mimeapps.read_text(encoding="utf-8")
            self.assertIn("application/x-multilinux-note=", mimeapps)
            self.assertIn("text/plain=kate.desktop", add_mime_association(
                "text/plain=kate.desktop\n", "application/x-multilinux-note", "demo.desktop"))
            restored = remove_mime_association(
                "text/plain=kate.desktop\napplication/x-multilinux-note=demo.desktop\n",
                "application/x-multilinux-note", "demo.desktop")
            self.assertEqual(restored, "text/plain=kate.desktop\n")
            with self.assertRaises(MultilinuxError):
                service.export_app("notes", mime_type="text/plain")
            result = service.open_app("notes", [str(notes / "note.mlnote")])
            self.assertEqual(result.returncode, 0)
            opened = [event for event in runtime.events if event[0] == "exec" and event[2][0] == "/usr/bin/gvim"]
            self.assertEqual(opened[-1][2][-1], str(notes / "note.mlnote"))
            self.assertEqual(opened[-1][4]["GDK_BACKEND"], "x11")
            self.assertIn("update-mime-database", service.cache_runner.calls[0])
            service.unexport_app("notes")
            self.assertFalse(desktop.exists())
            self.assertFalse(service.host_desktop.mimeapps.exists())
            runtime.identities["plain"] = dict(PROFILES["fedora44"])
            plain = service.create("fedora44", "plain", [str(notes)])
            self.assertIsNone(plain["desktop"])
            with self.assertRaises(MultilinuxError):
                service.add_app("plain", "other", "/usr/bin/gvim", ["/usr/bin/gvim"])

    def test_command_line_exports_and_refuses_a_foreign_mime_type(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime, notes = self._ready(root)
            service.add_app("debian13", "notes", "/usr/bin/gvim", ["/usr/bin/gvim"],
                            workdir=str(notes))
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                code, out, err = _run(["app", "export", "notes"])
                bad, _bad_out, bad_err = _run(
                    ["app", "export", "notes", "--mime-type", "text/plain"])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 0, err)
            self.assertTrue(Path(out.strip()).is_file())
            self.assertEqual(bad, 1)
            self.assertIn("application/x-multilinux", bad_err)
            shown = json.loads(service.show_app("notes")["export"] and
                               json.dumps(service.show_app("notes")))
            self.assertEqual(shown["export"]["mime_type"], "application/x-multilinux-note")

    def test_command_line_add_keeps_flags_before_the_program_argv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime, notes = self._ready(root)
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                code, out, err = _run([
                    "app", "add", "debian13", "notes",
                    "--exec", "/usr/bin/gvim",
                    "--workdir", str(notes),
                    "--set", "GDK_BACKEND=x11",
                    "--", "-N", "--servername", "MLNOTE", "--nofork",
                ])
                missing, _missing_out, missing_err = _run(["app", "exec", "notes"])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 0, err)
            self.assertIn("added notes", out)
            launch = service.show_app("notes")
            self.assertEqual(
                launch["argv"],
                ["/usr/bin/gvim", "-N", "--servername", "MLNOTE", "--nofork"])
            self.assertEqual(launch["env"]["GDK_BACKEND"], "x11")
            self.assertEqual(launch["workdir"], str(notes))
            self.assertEqual(missing, 1)
            self.assertIn("app exec requires argv", missing_err)


def _run(argv):
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


if __name__ == "__main__":
    unittest.main()
