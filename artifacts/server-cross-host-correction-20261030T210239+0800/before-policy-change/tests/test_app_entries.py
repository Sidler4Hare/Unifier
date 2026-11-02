"""Application discovery preserves literal arguments and runtime ownership."""

from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.app_entries import desktop_exec_argv, parse_desktop_exec
from unifier.errors import UnifierError
from unifier.packages import parse_package_files
from unifier.runtime import ExecResult
from unifier.service import parse_desktop_exec as legacy_parse_desktop_exec
from test_cli_shortcuts import ready_context


class AppEntries(unittest.TestCase):
    def test_native_package_list_formats_preserve_order_and_path_spaces(self):
        for text in (" /usr/bin/app\n\n/usr/share/data/app sample\nrelative\n",
                     "app /usr/bin/app\napp /usr/share/applications/app.desktop\n"):
            with self.subTest(text=text):
                expected = (["/usr/bin/app", "/usr/share/applications/app.desktop"]
                            if text.startswith("app ") else
                            ["/usr/bin/app", "/usr/share/data/app sample"])
                self.assertEqual(parse_package_files("app", text), expected)

    def test_first_desktop_exec_remains_authoritative(self):
        self.assertEqual(desktop_exec_argv(
            '[Desktop Entry]\nExec=app "a b" "literal;arg" 50%% %F\nExec=other'),
            ["app", "a b", "literal;arg", "50%"])
        self.assertIsNone(desktop_exec_argv('Exec=sh -c "app"\nExec=other'))
        self.assertIsNone(desktop_exec_argv('Exec=app %z\nExec=other'))
        self.assertIsNone(desktop_exec_argv('Name=Missing Exec\n'))
        self.assertIs(legacy_parse_desktop_exec, parse_desktop_exec)

    def test_discovery_deduplicates_argv_and_keeps_first_source_and_components(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            service = ctx.service_for("debian13")
            runtime.package_files = {"app": (
                "/usr/bin/app\n/usr/bin/subdir/ignored\n"
                "/usr/share/applications/alias.desktop\n"
                "/usr/share/applications/second.desktop\n"
                "/usr/local/share/applications/third.desktop\n"
                "/usr/share/applications/wrapper.desktop\n")}
            runtime.desktop_files = {
                "/usr/share/applications/alias.desktop": "Exec=app %F\n",
                "/usr/share/applications/second.desktop": "Exec=app --component=writer %U\n",
                "/usr/local/share/applications/third.desktop": "Exec=app --component=sheets %f\n",
                "/usr/share/applications/wrapper.desktop": 'Exec=sh -c "app"\n',
            }
            candidates = service.discover_app_entries("debian13", ["", "app", "app"])
            self.assertEqual([item["argv"] for item in candidates], [
                ["/usr/bin/app"], ["/usr/bin/app", "--component=writer"],
                ["/usr/bin/app", "--component=sheets"]])
            self.assertEqual(candidates[0]["source"], "package-file:app")
            self.assertIsNone(candidates[0]["desktop_id"])
            self.assertEqual(candidates[1]["desktop_id"], "second")
            self.assertEqual(service.inspect("debian13")["runtime_state"], "stopped")

    def test_discovery_queries_restore_initial_running_state_on_failure(self):
        for running in (False, True):
            for query in ("files", "resolve", "desktop"):
                with self.subTest(running=running, query=query), tempfile.TemporaryDirectory() as temporary:
                    ctx, runtime = ready_context(Path(temporary))
                    service = ctx.service_for("debian13")
                    if running:
                        service.start("debian13")
                    runtime.package_files = {"app": "/usr/share/applications/app.desktop\n"}

                    def fail_query(_name, argv, _env):
                        if ((query == "files" and argv[:2] == ["dpkg-query", "-L"])
                                or (query == "resolve" and argv[:2] == ["/bin/sh", "-c"])
                                or (query == "desktop" and argv == ["cat", "/usr/share/applications/app.desktop"])):
                            raise UnifierError("injected discovery failure")
                        return None

                    runtime.exec_handler = fail_query
                    runtime.events.clear()
                    with self.assertRaisesRegex(UnifierError, "injected discovery failure"):
                        if query == "files":
                            service.list_package_files("debian13", "app")
                        elif query == "resolve":
                            service.resolve_executable("debian13", "app")
                        else:
                            service.discover_app_entries("debian13", ["app"])
                    self.assertEqual(service.inspect("debian13")["runtime_state"],
                                     "running" if running else "stopped")
                    lifecycle = [event[0] for event in runtime.events if event[0] in {"start", "stop"}]
                    self.assertEqual(lifecycle, [] if running else
                                     ["start", "stop"] * (2 if query == "desktop" else 1))

    def test_unreadable_desktop_is_not_a_launch_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx, runtime = ready_context(Path(temporary))
            service = ctx.service_for("debian13")
            runtime.package_files = {"app": "/usr/share/applications/app.desktop\n"}
            runtime.exec_handler = lambda _name, argv, _env: (
                ExecResult(1, "", "unreadable") if argv[0] == "cat" else None)
            self.assertEqual(service.discover_app_entries("debian13", ["app"]), [])


if __name__ == "__main__":
    unittest.main()
