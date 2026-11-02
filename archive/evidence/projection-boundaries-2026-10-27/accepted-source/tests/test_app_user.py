"""Package root and the desktop user's UID must remain separate in keep-id."""

from dataclasses import replace
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from multilinux.errors import MultilinuxError
from multilinux.runtime import CreateSpec, creation_argv, exec_argv
from test_env import FakeRuntime, service_in
from test_desktop import SESSION, _run
from multilinux import cli


class UserRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.users = []

    def exec(self, name, argv, *, user=None, **kwargs):
        self.users.append((list(argv), user))
        return super().exec(name, argv, **kwargs)


class AppUser(unittest.TestCase):
    def test_keep_id_still_uses_root_for_package_commands(self):
        spec = CreateSpec("ml-demo", "demo", "/project", "image@sha256:" + "a" * 64, (),
                          user_namespace="keep-id")
        command = creation_argv(spec)
        self.assertIn("--userns=keep-id", command)
        self.assertEqual(command[command.index("--user") + 1], "0")
        self.assertEqual(command[command.index("--passwd-entry") + 1],
                         "$USERNAME:*:$UID:$GID:$NAME:/home/multilinux-$UID:/bin/sh")
        with self.assertRaises(MultilinuxError):
            creation_argv(replace(spec, user_namespace="host"))
        command = exec_argv("ml-demo", ["printf", "%s", "literal ; $HOME"],
                            workdir=None, env={}, user="1000:1000")
        self.assertEqual(command, ["podman", "exec", "--user", "1000:1000", "ml-demo",
                                   "printf", "%s", "literal ; $HOME"])

    def test_host_user_requires_keep_id_before_starting_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary), runtime=UserRuntime())
            service.create("debian13", None, [], desktop=True, session=SESSION)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "requires.*keep-id"):
                service.add_app("debian13", "notes", "/usr/bin/true", ["/usr/bin/true"],
                                as_host_user=True)
            self.assertEqual(runtime.events, [])

    def test_cli_records_user_and_only_app_exec_changes_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary), runtime=UserRuntime())
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                with patch("multilinux.service.discover_session", return_value=SESSION):
                    code, _out, err = _run(["env", "create", "--target", "debian13", "--desktop",
                                            "--userns", "keep-id"])
                self.assertEqual(code, 0, err)
                code, _out, err = _run(["app", "add", "debian13", "notes", "--exec",
                                        "/usr/bin/true", "--as-host-user"])
                self.assertEqual(code, 0, err)
            finally:
                cli.service_factory = original
            launch = service.show_app("notes")
            self.assertEqual(launch["user"], "1000:1000")
            self.assertEqual(launch["env"]["HOME"], "/home/multilinux-1000")
            self.assertEqual(service.inspect("debian13")["runtime"]["user_namespace"], "keep-id")
            self.assertTrue(any(argv[:3] == ["install", "-d", "-m"] and user is None
                                for argv, user in runtime.users))
            self.assertTrue(all(user is None for _argv, user in runtime.users))
            service.exec_app("notes", ["id", "-u"], capture=True, timeout=5)
            self.assertEqual(runtime.users[-1], (["id", "-u"], "1000:1000"))


if __name__ == "__main__":
    unittest.main()
