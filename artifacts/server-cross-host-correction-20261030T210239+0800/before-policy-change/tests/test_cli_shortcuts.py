"""Offline contracts for the user-facing shortcut CLI.

No test in this module talks to Podman, a registry, a package repository or
the real home directory.  A fake runtime implements the same narrow surface as
PodmanRuntime; the environment service itself remains the real implementation.
"""

from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier import cli, i18n, shortcuts
from unifier.errors import UnifierError
from unifier.guest_paths import list_guest_directories, open_guest_directory
from unifier.install import load_catalog, worker_environment
from unifier.runtime import ExecResult
from test_env import FakeRuntime

CONFIG, LOCK = load_catalog()
HOST_FACTS = {
    "os_release": {"ID": "fedora", "VERSION_ID": "44"},
    "system": "Linux", "machine": "x86_64", "libc": "glibc",
}
SERVICE_KWARGS = {
    "host_kernel": "host-kernel", "host_facts": HOST_FACTS,
    "uid": 1000, "gid": 1000,
}


class ShortcutRuntime(FakeRuntime):
    def __init__(self):
        super().__init__()
        self.images = set()
        self.pulls = []
        self.fail_create = set()
        self.exec_handler = None
        self.extra_inventory = ""
        self.package_files = {}
        self.desktop_files = {}

    def image_info(self, reference):
        if reference in self.images:
            return {"id": "image-" + reference[-8:],
                    "digest": reference.rsplit("@", 1)[1]}
        raise UnifierError("no such image: " + reference)

    def pull_image(self, reference, **kwargs):
        self.pulls.append(reference)
        self.images.add(reference)
        return self.image_info(reference)

    def create(self, spec):
        if spec.env_id in self.fail_create:
            raise UnifierError("simulated create failure for " + spec.env_id)
        return super().create(spec)

    def exec(self, name, argv, *, project, env_id, workdir, env, capture, timeout):
        if self.exec_handler is not None:
            result = self.exec_handler(name, list(argv), env_id)
            if result is not None:
                return result
        return super().exec(name, argv, project=project, env_id=env_id,
                            workdir=workdir, env=env, capture=capture,
                            timeout=timeout)

    def _captured(self, info, argv):
        if argv[:2] == ["dpkg-query", "-L"] or argv[:2] == ["rpm", "-ql"]:
            return ExecResult(0, self.package_files.get(argv[2], ""), "")
        if (argv and argv[0] == "cat" and len(argv) == 2
                and argv[1].endswith(".desktop")):
            return ExecResult(0, self.desktop_files.get(argv[1], ""), "")
        if argv[:2] == ["/bin/sh", "-c"] and len(argv) >= 5:
            # command -v resolver in service.resolve_executable
            if "command -v" in argv[2]:
                return ExecResult(0, "/usr/bin/" + argv[-1] + "\n", "")
        if argv and argv[0] == "dpkg-query":
            return ExecResult(0, self.inventory, "")
        if argv and argv[0] in {"apt-get", "dnf", "pacman-key"}:
            return ExecResult(0, "", "")
        if argv[:2] in (["pacman", "-Syu"], ["pacman", "-S"]):
            return ExecResult(0, "", "")
        return super()._captured(info, argv)


def context(root, runtime=None, *, isatty=False, input_fn=None,
            out=None, err=None, environ=None, locale_files=()):
    runtime = runtime or ShortcutRuntime()
    return shortcuts.build_context(
        root, runtime=runtime, config=CONFIG, lock=LOCK,
        stdin_isatty=isatty, input_fn=input_fn,
        out=out or StringIO(), err=err or StringIO(),
        environ=dict(os.environ) if environ is None else environ,
        locale_files=locale_files, service_kwargs=SERVICE_KWARGS)


def run_ctx(ctx, argv):
    out, err = StringIO(), StringIO()
    ctx.out, ctx.err = out, err
    code = shortcuts.main(argv, context=ctx)
    return code, out.getvalue(), err.getvalue()


def ready_context(root, targets=("debian13",), *, desktop=False):
    runtime = ShortcutRuntime()
    ctx = context(root, runtime)
    argv = ["init"]
    for target in targets:
        argv.extend(["--target", target])
    argv.append("--yes")
    code, out, err = run_ctx(ctx, argv)
    assert code == 0, (code, out, err)
    return ctx, runtime


class HelpAndLanguage(unittest.TestCase):
    def test_help_never_builds_runtime_and_writes_no_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with mock.patch.dict(os.environ, {"UNIFIER_HOME": str(home)}), \
                    mock.patch.object(shortcuts, "build_context",
                                      side_effect=AssertionError("runtime context built")):
                for argv in (["--help"], ["help"], ["help", "init"],
                             ["install", "--help"]):
                    out, err = StringIO(), StringIO()
                    with redirect_stdout(out), redirect_stderr(err):
                        code = cli.main(argv)
                    self.assertEqual(code, 0, (argv, out.getvalue(), err.getvalue()))
                    self.assertTrue(out.getvalue())
            self.assertFalse((home / "install-state.json").exists())
            self.assertFalse((home / ".lab").exists())
            with mock.patch.dict(os.environ, {"UNIFIER_HOME": str(home)}), \
                    mock.patch("unifier.runtime.PodmanRuntime",
                               side_effect=AssertionError("runtime constructed")):
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(["language", "--json"])
                self.assertEqual(code, 0, (out.getvalue(), err.getvalue()))

    def test_language_precedence_and_persistence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = {"LC_ALL": "zh_CN.UTF-8", "LC_MESSAGES": "en_US.UTF-8",
                   "LANG": "en_US.UTF-8"}
            ctx = context(root, environ=env, locale_files=())
            code, out, _ = run_ctx(ctx, ["language", "--json"])
            self.assertEqual(code, 0)
            first = json.loads(out)
            self.assertEqual((first["language"], first["source"]), ("zh-CN", "LC_ALL"))
            code, out, _ = run_ctx(ctx, ["language", "en"])
            self.assertEqual(code, 0)
            ctx._language = None
            code, out, _ = run_ctx(ctx, ["language", "--json"])
            second = json.loads(out)
            self.assertEqual((second["language"], second["source"]), ("en", "explicit"))
            # Explicit preference beats a Chinese LA_ALL even after a new context.
            ctx2 = context(root, environ={**env, "LC_ALL": "zh_CN.UTF-8"},
                           locale_files=())
            code, out, _ = run_ctx(ctx2, ["language", "--json"])
            self.assertEqual(json.loads(out)["language"], "en")
            # auto removes the preference and falls back through the locale order.
            code, out, _ = run_ctx(ctx2, ["language", "auto"])
            self.assertEqual(code, 0)
            ctx2._language = None
            code, out, _ = run_ctx(ctx2, ["language", "--json"])
            third = json.loads(out)
            self.assertEqual((third["language"], third["source"]),
                             ("zh-CN", "LC_ALL"))
            self.assertIsNone(third["preference"])
            # LC_MESSAGES is next when LC_ALL is empty.
            ctx3 = context(root, environ={"LC_ALL": "", "LC_MESSAGES": "zh_CN.UTF-8",
                                          "LANG": "en_US.UTF-8"}, locale_files=())
            code, out, _ = run_ctx(ctx3, ["language", "--json"])
            self.assertEqual(json.loads(out)["source"], "LC_MESSAGES")
            # LANG is next.
            ctx4 = context(root, environ={"LC_ALL": "", "LC_MESSAGES": "",
                                          "LANG": "zh_CN.UTF-8"}, locale_files=())
            code, out, _ = run_ctx(ctx4, ["language", "--json"])
            self.assertEqual(json.loads(out)["source"], "LANG")
            # Locale files are last before English.
            locale = root / "locale.conf"
            locale.write_text('LANG="zh_CN.UTF-8"\n', encoding="utf-8")
            ctx5 = context(root, environ={}, locale_files=(str(locale),))
            code, out, _ = run_ctx(ctx5, ["language", "--json"])
            self.assertEqual(json.loads(out)["language"], "zh-CN")
            self.assertEqual(json.loads(out)["source"], "locale.conf")

    def test_chinese_and_english_help_are_real_translations(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            zh = context(root, environ={"LC_ALL": "zh_CN.UTF-8"}, locale_files=())
            code, out, _ = run_ctx(zh, ["help", "init"])
            self.assertEqual(code, 0)
            self.assertIn("选项", out)
            self.assertIn("--target", out)
            en = context(root, environ={"LC_ALL": "en_US.UTF-8"}, locale_files=())
            code, out, _ = run_ctx(en, ["help", "init"])
            self.assertIn("Options", out)
            self.assertNotIn("选项", out)

    def test_auto_persists_and_is_cleared_by_a_new_explicit_choice(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = context(root, environ={"LANG": "zh_CN.UTF-8"}, locale_files=())
            run_ctx(ctx, ["language", "zh-CN"])
            self.assertEqual(i18n.read_preference(root), "zh-CN")
            run_ctx(ctx, ["language", "auto"])
            self.assertIsNone(i18n.read_preference(root))
            run_ctx(ctx, ["language", "en"])
            self.assertEqual(i18n.read_preference(root), "en")


class InitAndConfigure(unittest.TestCase):
    def test_selected_targets_are_the_only_images_pulled_and_environments_created(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--target", "arch", "--yes"])
            self.assertEqual(code, 0, (out, err))
            pulled = [item.rsplit("@", 1)[0] for item in runtime.pulls]
            self.assertEqual([item.rsplit(":", 1)[0] for item in pulled],
                             ["docker.io/library/debian", "docker.io/library/archlinux"])
            state = json.loads((root / "install-state.json").read_text())
            self.assertEqual(state["enabled"], ["debian13", "arch"])
            self.assertEqual(state["targets"]["debian13"]["state"], "ready")
            self.assertEqual(state["targets"]["arch"]["state"], "ready")
            self.assertEqual(state["targets"]["fedora44"]["state"], "not_configured")
            self.assertFalse(any("fedora" in item for item in runtime.pulls))
            created = [event[1].env_id for event in runtime.events if event[0] == "create"]
            self.assertEqual(created, ["debian13", "arch"])
            # Re-running reuses records and never creates a duplicate container.
            runtime.events.clear()
            runtime.pulls.clear()
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--target", "arch", "--yes"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(runtime.events, [])
            self.assertEqual(runtime.pulls, [])
            self.assertEqual(len([event for event in runtime.events
                                  if event[0] == "create"]), 0)

    def test_none_is_an_explicit_empty_selection_without_runtime_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            ctx = context(root, runtime)
            code, out, err = run_ctx(ctx, ["init", "--none", "--yes"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(runtime.events, [])
            self.assertEqual(runtime.pulls, [])
            state = json.loads((root / "install-state.json").read_text())
            self.assertTrue(state["initialized"])
            self.assertEqual(state["enabled"], [])
            self.assertEqual(state["targets"]["debian13"]["state"], "not_configured")

    def test_noninteractive_without_selection_fails_instead_of_waiting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx = context(root, ShortcutRuntime(), isatty=False)
            code, out, err = run_ctx(ctx, ["init"])
            self.assertEqual(code, 1)
            self.assertIn("--target", err)
            self.assertFalse((root / "install-state.json").exists())

    def test_partial_failure_is_recorded_and_can_continue_without_redoing_ready_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.fail_create.add("arch")
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--target", "arch", "--yes"])
            self.assertEqual(code, 1)
            state = json.loads((root / "install-state.json").read_text())
            self.assertEqual(state["targets"]["debian13"]["state"], "ready")
            self.assertEqual(state["targets"]["arch"]["state"], "failed")
            self.assertIn("container-create",
                          state["targets"]["arch"]["failure"]["phase"])
            runtime.events.clear()
            runtime.fail_create.clear()
            code, out, err = run_ctx(
                ctx, ["init", "--target", "arch", "--yes"])
            self.assertEqual(code, 0, (out, err))
            created = [event[1].env_id for event in runtime.events
                       if event[0] == "create"]
            self.assertEqual(created, ["arch"])
            state = json.loads((root / "install-state.json").read_text())
            self.assertEqual(state["targets"]["arch"]["state"], "ready")

    def test_configure_disable_keeps_container_and_does_not_stop_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root, ("debian13", "arch"))
            runtime.events.clear()
            code, out, err = run_ctx(
                ctx, ["configure", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(runtime.events, [])
            state = json.loads((root / "install-state.json").read_text())
            self.assertFalse(state["targets"]["arch"]["selected"])
            self.assertEqual(state["targets"]["arch"]["state"], "ready")
            self.assertTrue((root / "containers/arch/files").is_dir())
            code, out, err = run_ctx(ctx, ["start", "arch"])
            self.assertEqual(code, 1)
            self.assertIn("disabled", err)
            # Stop is still allowed for a retained, disabled environment.
            code, out, err = run_ctx(ctx, ["stop", "arch"])
            self.assertEqual(code, 0, (out, err))

    def test_concurrent_init_creates_one_container_per_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            results = []

            def worker():
                ctx = context(root, runtime)
                results.append(run_ctx(
                    ctx, ["init", "--target", "debian13", "--yes"])[0])

            threads = [threading.Thread(target=worker) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(20)
                self.assertFalse(thread.is_alive())
            self.assertEqual(results, [0, 0])
            created = [event for event in runtime.events if event[0] == "create"]
            self.assertEqual(len(created), 1)

    def test_dirs_and_path_use_the_configured_root_and_do_not_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["path", "debian13", "--files"])
            self.assertEqual(code, 0)
            self.assertEqual(out.strip(), str(root / "containers/debian13/files"))
            self.assertEqual(len(out.splitlines()), 1)
            code, out, err = run_ctx(ctx, ["dirs", "debian13", "--json"])
            self.assertEqual(code, 0)
            payload = json.loads(out)
            row = payload["targets"][0]
            self.assertEqual(row["target"], "debian13")
            self.assertTrue(row["files_mounted"])
            self.assertEqual(row["directory"], str(root / "containers/debian13"))
            self.assertNotIn("start", [event[0] for event in runtime.events])
            code, out, err = run_ctx(ctx, ["dirs", "fedora44"])
            self.assertEqual(code, 1)
            self.assertIn("not configured", err)


class SafeGuestListing(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "usr/bin").mkdir(parents=True)
        (self.root / "etc").mkdir()
        (self.root / "tmp").mkdir()
        (self.root / "tmp/guest-only").mkdir()
        os.symlink("usr/bin", self.root / "bin")
        os.symlink("/etc", self.root / "absolute-etc")
        os.symlink("/../../etc", self.root / "escaping-etc")
        os.symlink("/tmp/guest-only", self.root / "host-tmp-link")

    def tearDown(self):
        self.temporary.cleanup()

    def test_absolute_and_dotdot_links_resolve_inside_the_view_not_the_host(self):
        with open_guest_directory(self.root, "/absolute-etc") as (_fd, canonical):
            self.assertEqual(canonical, "/etc")
        with open_guest_directory(self.root, "/escaping-etc") as (_fd, canonical):
            self.assertEqual(canonical, "/etc")
        listing = list_guest_directories(self.root, "/")
        self.assertIn("bin", listing["entries"])
        self.assertIn("absolute-etc", listing["entries"])
        self.assertIn("host-tmp-link", listing["entries"])
        # /tmp/guest-only exists in the view; the host's /tmp is irrelevant.
        linked = list_guest_directories(self.root, "/host-tmp-link")
        self.assertEqual(linked["entries"], [])

    def test_dotdot_is_clamped_at_the_view_root(self):
        with open_guest_directory(self.root, "/../../etc") as (_fd, canonical):
            self.assertEqual(canonical, "/etc")

    def test_path_join_bug_is_not_used_for_absolute_links(self):
        # A pathological Path(view)/'/usr' would discard the view prefix.  The
        # resolver must keep the guest root even when a host /usr exists.
        listing = list_guest_directories(self.root, "/")
        self.assertIn("usr", listing["entries"])


class LsCommand(unittest.TestCase):
    def test_running_uses_runtime_exec_argv_and_does_not_open_a_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            service = ctx.service_for("debian13")
            record = service.store.load("debian13")
            runtime.containers[record["runtime"]["container_name"]] = \
                runtime.containers[record["runtime"]["container_name"]].__class__(
                    **{**runtime.containers[record["runtime"]["container_name"]].__dict__,
                       "running": True, "status": "running", "pid": 22})
            seen = []

            def handler(name, argv, env_id):
                seen.append((name, argv))
                return ExecResult(0, "/\x00usr\x00etc\x00", "")

            runtime.exec_handler = handler
            manager = mock.Mock()
            ctx.view_managers[str(service.root)] = manager
            code, out, err = run_ctx(ctx, ["ls", "debian13"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(out.strip(), "etc  usr")
            self.assertEqual(len(seen), 1)
            self.assertEqual(seen[0][1][0], "/bin/sh")
            self.assertIn("unifier-ls", seen[0][1])
            manager.open.assert_not_called()
            manager.revoke.assert_not_called()

    def test_stopped_opens_and_releases_only_its_own_view(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            mount = root / "view-mount"
            (mount / "usr").mkdir(parents=True)
            (mount / "etc").mkdir()
            manager = mock.Mock()
            manager.list.return_value = []
            manager.open.return_value = {
                "environment_id": "debian13", "view_uuid": "view-1",
                "mountpoint": str(mount), "state": "ready",
            }
            manager.revoke.return_value = {}
            ctx.view_managers[str(root)] = manager
            code, out, err = run_ctx(ctx, ["ls", "debian13", "/"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(out.strip(), "etc  usr")
            manager.open.assert_called_once_with("debian13")
            manager.revoke.assert_called_once_with("view-1")

    def test_existing_ready_view_is_used_and_never_revoked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            mount = root / "view-mount"
            (mount / "usr").mkdir(parents=True)
            manager = mock.Mock()
            existing = {"environment_id": "debian13", "view_uuid": "someone-else",
                        "mountpoint": str(mount), "state": "ready"}
            manager.list.return_value = [existing]
            ctx.view_managers[str(root)] = manager
            with mock.patch.object(shortcuts, "_active_views", return_value=[existing]):
                code, out, err = run_ctx(ctx, ["ls", "debian13", "/"])
            self.assertEqual(code, 0, (out, err))
            manager.open.assert_not_called()
            manager.revoke.assert_not_called()

    def test_view_failure_reports_recovery_and_does_not_return_fake_empty_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            manager = mock.Mock()
            manager.list.return_value = [{"environment_id": "debian13",
                                          "view_uuid": "view-busy",
                                          "mountpoint": str(root / "busy"),
                                          "state": "starting"}]
            manager.open.side_effect = UnifierError("mount is busy (EBUSY)")
            ctx.view_managers[str(root)] = manager
            with mock.patch.object(shortcuts, "_active_views", return_value=[]):
                code, out, err = run_ctx(ctx, ["ls", "debian13"])
            self.assertEqual(code, 1)
            self.assertEqual(out, "")
            self.assertIn("view-busy", err)
            self.assertIn("view status", err)


class InstallAndRun(unittest.TestCase):
    def _runtime(self):
        runtime = ShortcutRuntime()
        runtime.inventory = "base\t1\nhello\t2\n"
        runtime.package_files = {
            "hello": "/usr/bin/hello\n/usr/share/applications/hello.desktop\n",
        }
        runtime.desktop_files = {
            "/usr/share/applications/hello.desktop":
                "[Desktop Entry]\nName=Hello\nExec=/usr/bin/hello %f\n",
        }
        return runtime

    def _service(self, root, runtime):
        from unifier.service import EnvironmentService
        class StaticHost:
            def snapshot(self):
                return {"packages": {"available": True, "tool": "rpm",
                                     "sha256": "host", "count": 1, "error": None},
                        "entries": {"directory": "/tmp/apps", "names": []}}
        return EnvironmentService(root, runtime, CONFIG, LOCK,
                                  host_observer=StaticHost(), **SERVICE_KWARGS)

    def test_install_discovers_one_entry_and_run_reuses_the_same_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = self._runtime()
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            service = self._service(root, runtime)
            ctx.services[str(root)] = service
            code, out, err = run_ctx(ctx, ["install", "hello", "--json"])
            self.assertEqual(code, 0, (out, err))
            payload = json.loads(out)
            self.assertEqual(payload["target"], "debian13")
            self.assertEqual(payload["app"], "hello")
            self.assertEqual(payload["executable"], "/usr/bin/hello")
            self.assertEqual(payload["transaction"]["phase"], "completed")
            record = service.store.load("debian13")
            first_cid = record["runtime"]["container_id"]
            code, out, err = run_ctx(ctx, ["install", "hello", "--json"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(service.store.load("debian13")["runtime"]["container_id"],
                             first_cid)
            # run uses the registered launch record and exact environment.
            calls = []
            service.open_app = lambda app, extra: (
                calls.append((app, extra)) or ExecResult(23, "", ""))
            code, out, err = run_ctx(
                ctx, ["run", "--target", "debian13", "hello", "--", "--version"])
            self.assertEqual(code, 23)
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0][0], service.app_by_alias("debian13", "hello")["id"])
            self.assertEqual(calls[0][1], ["--version"])
            self.assertEqual(service.store.load("debian13")["runtime"]["container_id"],
                             first_cid)

    def test_disabled_target_install_has_no_transaction_or_container_side_effect(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = self._runtime()
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            run_ctx(ctx, ["configure", "--target", "arch", "--yes"])
            runtime.events.clear()
            code, out, err = run_ctx(
                ctx, ["install", "--target", "debian13", "hello"])
            self.assertEqual(code, 1)
            self.assertIn("disabled", err)
            self.assertEqual(runtime.events, [])
            self.assertEqual(list((root / ".lab/state/transactions").glob("*.json"))
                             if (root / ".lab/state/transactions").exists() else [], [])

    def test_local_format_mismatch_is_refused_before_any_runtime_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = self._runtime()
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            package = root / "foreign.rpm"
            package.write_bytes(b"\xed\xab\xee\xdb" + b"0" * 32)
            runtime.events.clear()
            code, out, err = run_ctx(
                ctx, ["install", "--target", "debian13", str(package)])
            self.assertEqual(code, 1)
            self.assertIn("not accepted", err)
            self.assertEqual(runtime.events, [])


class Adoption(unittest.TestCase):
    def test_adopt_reads_identity_without_modifying_legacy_state_and_reports_mount_truth(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy-root"
            runtime = self._runtime()
            from unifier.service import EnvironmentService
            class StaticHost:
                def snapshot(self):
                    return {"packages": {"available": True, "tool": "rpm",
                                         "sha256": "host", "count": 1, "error": None},
                            "entries": {"directory": "/tmp/apps", "names": []}}
            legacy_service = EnvironmentService(
                legacy, runtime, CONFIG, LOCK, host_observer=StaticHost(),
                **SERVICE_KWARGS)
            runtime.identities["freecad"] = dict(runtime.identities["debian13"])
            old = legacy_service.create("debian13", "freecad", [], "pasta")
            before = (legacy / ".lab/state/environments/freecad.json").read_bytes()
            digest = hashlib.sha256(before).hexdigest()
            new_root = root / "new-root"
            ctx = context(new_root, runtime)
            files = new_root / "containers/debian13/files"
            files.mkdir(parents=True)
            os.symlink(str(files), legacy / "not-used")
            code, out, err = run_ctx(
                ctx, ["adopt", "--target", "debian13", "--root", str(legacy),
                      "--environment", "freecad", "--json"])
            self.assertEqual(code, 0, (out, err))
            payload = json.loads(out)
            self.assertEqual(payload["environment_uuid"], old["environment_uuid"])
            self.assertEqual(payload["container_id"], old["runtime"]["container_id"])
            self.assertFalse(payload["files_mounted"])
            after = (legacy / ".lab/state/environments/freecad.json").read_bytes()
            self.assertEqual(hashlib.sha256(after).hexdigest(), digest)
            state = json.loads((new_root / "install-state.json").read_text())
            self.assertEqual(state["targets"]["debian13"]["source"], "adopted")
            self.assertEqual(state["targets"]["debian13"]["source_environment"],
                             "freecad")
            code, out, err = run_ctx(ctx, ["dirs", "debian13", "--json"])
            self.assertEqual(code, 0, (out, err))
            row = json.loads(out)["targets"][0]
            self.assertFalse(row["files_mounted"])
            self.assertEqual(row["source_environment"], "freecad")

    def test_adopt_refuses_to_replace_a_new_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = self._runtime()
            legacy = root / "legacy"
            from unifier.service import EnvironmentService
            class StaticHost:
                def snapshot(self):
                    return {"packages": {"available": True, "tool": "rpm",
                                         "sha256": "host", "count": 1, "error": None},
                            "entries": {"directory": "/tmp/apps", "names": []}}
            legacy_service = EnvironmentService(
                legacy, runtime, CONFIG, LOCK, host_observer=StaticHost(),
                **SERVICE_KWARGS)
            runtime.identities["freecad"] = dict(runtime.identities["debian13"])
            legacy_service.create("debian13", "freecad", [], "pasta")
            new_root = root / "new"
            ctx, _runtime = ready_context(new_root, ("debian13",))
            code, out, err = run_ctx(
                ctx, ["adopt", "--target", "debian13", "--root", str(legacy),
                      "--environment", "freecad"])
            self.assertEqual(code, 1)
            self.assertIn("conflict", err)

    def _runtime(self):
        runtime = ShortcutRuntime()
        runtime.inventory = "base\t1\n"
        return runtime


class WorkerRoot(unittest.TestCase):
    def test_worker_environment_pins_one_absolute_root(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            env = worker_environment(root, {"UNIFIER_HOME": "/somewhere/else",
                                            "PATH": "/bin"})
            self.assertEqual(env["UNIFIER_HOME"], str(root.resolve()))
            self.assertEqual(env["PATH"], "/bin")

    def test_file_worker_spawn_inherits_root(self):
        from unifier.file_routes import FileRoutes
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = SimpleNamespace(root=root)
            routes = FileRoutes(root, service)
            captured = {}

            class Process:
                def wait(self):
                    return 0
            def popen(command, **kwargs):
                captured.update(kwargs)
                return Process()
            with mock.patch("unifier.file_routes.subprocess.Popen", popen):
                routes._spawn_worker("/not/used", "edit")
            self.assertEqual(captured["env"]["UNIFIER_HOME"], str(root.resolve()))


class LabPullFilter(unittest.TestCase):
    def test_only_selected_targets_are_pulled(self):
        from tools import lab
        calls = []
        output = Path(tempfile.mkdtemp()) / "pull.json"
        with mock.patch.object(lab, "require_rootless", return_value={}), \
                mock.patch.object(lab, "command", side_effect=lambda argv, **kw: (
                    calls.append(argv) or
                    json.dumps([{"Id": "id-" + argv[-1][-8:], "Size": 1}]))):
            lab.pull_images(SimpleNamespace(target=["debian13"], output=output))
        pulled = [item[-1] for item in calls if item[:2] == ["podman", "pull"]]
        self.assertEqual(len(pulled), 1)
        self.assertIn("debian", pulled[0])
        self.assertNotIn("fedora", pulled[0])
        self.assertNotIn("archlinux", pulled[0])


class AdditionalContracts(unittest.TestCase):
    def test_low_level_help_is_reachable_without_a_runtime(self):
        with tempfile.TemporaryDirectory() as temporary, \
                mock.patch.dict(os.environ, {"UNIFIER_HOME": temporary}), \
                mock.patch("unifier.runtime.PodmanRuntime",
                           side_effect=AssertionError("runtime constructed")):
            out, err = StringIO(), StringIO()
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["help", "env"])
            self.assertEqual(code, 0, (out.getvalue(), err.getvalue()))
            self.assertIn("env", out.getvalue())
            self.assertIn("create", out.getvalue())

    def test_argument_error_is_localized(self):
        with tempfile.TemporaryDirectory() as temporary:
            ctx = context(Path(temporary), environ={"LC_ALL": "zh_CN.UTF-8"},
                          locale_files=())
            code, out, err = run_ctx(ctx, ["path", "debian13"])
            self.assertEqual(code, 1)
            self.assertIn("参数错误", err)

    def test_init_json_stdout_is_one_document(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            ctx.json_mode = False
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes", "--json"])
            self.assertEqual(code, 0, (out, err))
            payload = json.loads(out)
            self.assertEqual(payload["enabled"], ["debian13"])

    def test_missing_local_package_is_refused_before_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["install", str(root / "missing.deb")])
            self.assertEqual(code, 1)
            self.assertIn("not a file", err)
            self.assertEqual(runtime.events, [])

    def test_existing_headless_environment_is_not_silently_reused_as_desktop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            runtime.events.clear()
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--desktop", "--yes"])
            self.assertEqual(code, 1)
            self.assertIn("desktop", err)
            self.assertEqual(runtime.events, [])

    def test_adoption_lists_other_legacy_containers_for_the_same_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy = root / "legacy"
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\n"
            runtime.identities["freecad"] = dict(runtime.identities["debian13"])
            runtime.identities["notes"] = dict(runtime.identities["debian13"])
            from unifier.service import EnvironmentService
            class StaticHost:
                def snapshot(self):
                    return {"packages": {"available": True, "tool": "rpm",
                                         "sha256": "host", "count": 1, "error": None},
                            "entries": {"directory": "/tmp/apps", "names": []}}
            legacy_service = EnvironmentService(
                legacy, runtime, CONFIG, LOCK, host_observer=StaticHost(),
                **SERVICE_KWARGS)
            legacy_service.create("debian13", "freecad", [], "pasta")
            legacy_service.create("debian13", "notes", [], "pasta")
            ctx = context(root / "new", runtime)
            code, out, err = run_ctx(
                ctx, ["adopt", "--target", "debian13", "--root", str(legacy),
                      "--environment", "freecad", "--json"])
            self.assertEqual(code, 0, (out, err))
            self.assertEqual(json.loads(out)["conflicts"], ["notes"])

    def test_installer_refuses_to_overwrite_an_unknown_file(self):
        from tools import install_unifier
        with tempfile.TemporaryDirectory() as temporary:
            bin_dir = Path(temporary)
            destination = bin_dir / "unifier"
            destination.write_text("#!/bin/sh\necho unknown\n", encoding="utf-8")
            destination.chmod(0o755)
            code = install_unifier.main(["--bin-dir", str(bin_dir)])
            self.assertEqual(code, 1)
            self.assertEqual(destination.read_text(encoding="utf-8"),
                             "#!/bin/sh\necho unknown\n")

    def test_supervised_open_worker_inherits_the_installation_root(self):
        from unifier import on_demand
        from unifier.service import EnvironmentService
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\n"
            class StaticHost:
                def snapshot(self):
                    return {"packages": {"available": True, "tool": "rpm",
                                         "sha256": "host", "count": 1, "error": None},
                            "entries": {"directory": "/tmp/apps", "names": []}}
            service = EnvironmentService(root, runtime, CONFIG, LOCK,
                                         host_observer=StaticHost(),
                                         **SERVICE_KWARGS)
            service.create("debian13", "debian13", [], "pasta")
            service.add_app("debian13", "hello", "/usr/bin/true",
                            ["/usr/bin/true"], on_demand=True,
                            desktop_required=False)
            captured = {}
            class Process:
                def wait(self):
                    return 0
            def popen(command, **kwargs):
                captured.update(kwargs)
                return Process()
            with mock.patch.object(on_demand.subprocess, "Popen", popen):
                result = on_demand.supervised_open(service, "hello", [])
            self.assertEqual(result.returncode, 0)
            self.assertEqual(captured["env"]["UNIFIER_HOME"], str(root.resolve()))
            self.assertEqual(captured["env"]["MULTILINUX_APP_WORKER"], "1")


    def test_install_refuses_ambiguous_multiple_environments_for_one_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\nhello\t2\n"
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            from unifier.service import EnvironmentService
            class StaticHost:
                def snapshot(self):
                    return {"packages": {"available": True, "tool": "rpm",
                                         "sha256": "host", "count": 1, "error": None},
                            "entries": {"directory": "/tmp/apps", "names": []}}
            service = EnvironmentService(root, runtime, CONFIG, LOCK,
                                         host_observer=StaticHost(),
                                         **SERVICE_KWARGS)
            runtime.identities["other"] = dict(runtime.identities["debian13"])
            service.create("debian13", "other", [], "pasta")
            ctx.services[str(root)] = service
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["install", "hello"])
            self.assertEqual(code, 1)
            self.assertIn("multiple environment", err)
            self.assertEqual(runtime.events, [])
            self.assertFalse((root / ".lab/state/transactions").exists())


class IsolatedRoot(unittest.TestCase):
    def test_low_level_control_state_is_written_under_unifier_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            data = home / "project-data"
            data.mkdir()
            with mock.patch.dict(os.environ, {"UNIFIER_HOME": str(home)}):
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(["root", "add", str(data)])
            self.assertEqual(code, 0, (out.getvalue(), err.getvalue()))
            record = json.loads(out.getvalue())
            self.assertEqual(record["locator"], str(data.resolve()))
            self.assertTrue((home / ".lab/state").is_dir())
            repo_state = ROOT / ".lab/state/data-roots.json"
            if repo_state.is_file():
                self.assertNotIn(record["root_uuid"],
                                 repo_state.read_text(encoding="utf-8"))


class RepairContracts(unittest.TestCase):
    def _static_service(self, root, runtime):
        from unifier.service import EnvironmentService
        class StaticHost:
            def snapshot(self):
                return {"packages": {"available": True, "tool": "rpm",
                                     "sha256": "host", "count": 1, "error": None},
                        "entries": {"directory": "/tmp/apps", "names": []}}
        return EnvironmentService(root, runtime, CONFIG, LOCK,
                                  host_observer=StaticHost(),
                                  **SERVICE_KWARGS)

    def _legacy(self, root, runtime, env_id="freecad"):
        runtime.identities[env_id] = dict(runtime.identities["debian13"])
        service = self._static_service(root, runtime)
        record = service.create("debian13", env_id, [], "pasta")
        return service, record

    def test_stopped_ls_requests_projected_link_semantics(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            mount = root / "view-mount"
            (mount / "bin").mkdir(parents=True)
            manager = mock.Mock()
            manager.list.return_value = []
            manager.open.return_value = {
                "environment_id": "debian13", "view_uuid": "view-p",
                "mountpoint": str(mount), "state": "ready",
            }
            manager.revoke.return_value = {}
            ctx.view_managers[str(root)] = manager
            seen = {}
            def fake(root_path, guest_path, *, projected=False):
                seen["projected"] = projected
                return {"path": guest_path, "entries": ["bin"]}
            with mock.patch.object(shortcuts, "list_guest_directories", fake):
                code, out, err = run_ctx(ctx, ["ls", "debian13", "/"])
            self.assertEqual(code, 0, (out, err))
            self.assertIs(seen.get("projected"), True)

    def test_adopt_rejects_stale_uuid_or_container_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\n"
            legacy = root / "legacy"
            legacy_service, old = self._legacy(legacy, runtime)
            ctx = context(root / "new", runtime)
            code, out, err = run_ctx(
                ctx, ["adopt", "--target", "debian13", "--root", str(legacy),
                      "--environment", "freecad", "--json"])
            self.assertEqual(code, 0, (out, err))
            legacy_service.remove("freecad")
            legacy_service.create("debian13", "freecad", [], "pasta")
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["start", "debian13"])
            self.assertEqual(code, 1)
            self.assertIn("stale", err)
            self.assertEqual(runtime.events, [])

    def test_adopt_requires_versioned_identity_and_proves_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\n"
            legacy = root / "legacy"
            legacy_service, record = self._legacy(legacy, runtime)
            record.pop("environment_uuid", None)
            record.pop("generation", None)
            record["schema_version"] = 1
            legacy_service.store.save(record, None)
            ctx = context(root / "new", runtime)
            code, out, err = run_ctx(
                ctx, ["adopt", "--target", "debian13", "--root", str(legacy),
                      "--environment", "freecad"])
            self.assertEqual(code, 1)
            self.assertIn("identity", err)
            # A removed runtime container is not accepted as an adopted identity.
            legacy_service, record = self._legacy(root / "legacy2", runtime,
                                                  env_id="notes")
            runtime.containers.pop(
                legacy_service.store.load("notes")["runtime"]["container_name"])
            ctx2 = context(root / "new2", runtime)
            code, out, err = run_ctx(
                ctx2, ["adopt", "--target", "debian13", "--root",
                       str(root / "legacy2"), "--environment", "notes"])
            self.assertEqual(code, 1)
            self.assertIn("stale", err)

    def test_exec_arguments_distinguish_components_and_are_registered_literally(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\nwps-office\t2\n"
            runtime.package_files = {
                "wps-office": ("/usr/share/applications/wps-writer.desktop\n"
                               "/usr/share/applications/wps-sheets.desktop\n"),
            }
            runtime.desktop_files = {
                "/usr/share/applications/wps-writer.desktop":
                    "[Desktop Entry]\nExec=/usr/bin/wps --component=writer %F\n",
                "/usr/share/applications/wps-sheets.desktop":
                    "[Desktop Entry]\nExec=/usr/bin/wps --component=sheets %U\n",
            }
            ctx = context(root, runtime)
            code, out, err = run_ctx(ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            service = self._static_service(root, runtime)
            ctx.services[str(root)] = service
            code, out, err = run_ctx(ctx, ["install", "wps-office"])
            self.assertEqual(code, 1)
            self.assertIn("--component=writer", err)
            self.assertIn("--component=sheets", err)
            ctx.stdin_isatty = True
            ctx.input_fn = lambda prompt="": "2"
            code, out, err = run_ctx(ctx, ["install", "wps-office", "--json"])
            self.assertEqual(code, 0, (out, err))
            payload = json.loads(out)
            self.assertEqual(payload["app"], "wps-sheets")
            launch = service.app_by_alias("debian13", "wps-sheets")
            self.assertEqual(launch["argv"],
                             ["/usr/bin/wps", "--component=sheets"])
            self.assertEqual(launch["user"], "1000:1000")

    def test_same_alias_is_namespaced_per_target_and_run_picks_the_right_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\nhello\t2\n"
            runtime.package_files = {"hello": "/usr/bin/hello\n"}
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--target", "fedora44",
                      "--yes"])
            self.assertEqual(code, 0, (out, err))
            code, out, err = run_ctx(
                ctx, ["install", "--target", "debian13", "hello", "--json"])
            self.assertEqual(code, 0, (out, err))
            debian = json.loads(out)
            code, out, err = run_ctx(
                ctx, ["install", "--target", "fedora44", "hello", "--json"])
            self.assertEqual(code, 0, (out, err))
            fedora = json.loads(out)
            self.assertEqual(debian["app"], "hello")
            self.assertEqual(fedora["app"], "hello")
            self.assertNotEqual(debian["app_id"], fedora["app_id"])
            state = json.loads((root / "install-state.json").read_text())
            deb = ctx.service_for("debian13", state)
            fed = ctx.service_for("fedora44", state)
            self.assertEqual(deb.app_by_alias("debian13", "hello")["id"],
                             debian["app_id"])
            self.assertEqual(fed.app_by_alias("fedora44", "hello")["id"],
                             fedora["app_id"])
            calls = []
            deb.open_app = lambda app, extra: (calls.append((app, extra))
                                               or ExecResult(0, "", ""))
            self.assertEqual(run_ctx(ctx, ["run", "--target", "debian13",
                                           "hello"])[0], 0)
            self.assertEqual(run_ctx(ctx, ["run", "--target", "fedora44",
                                           "hello"])[0], 0)
            self.assertEqual(calls, [
                (debian["app_id"], []),
                (fedora["app_id"], []),
            ])

    def test_help_anywhere_before_double_dash_has_no_runtime_and_respects_boundary(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with mock.patch.dict(os.environ, {"UNIFIER_HOME": str(home)}), \
                    mock.patch("unifier.runtime.PodmanRuntime",
                               side_effect=AssertionError("runtime constructed")):
                out, err = StringIO(), StringIO()
                with redirect_stdout(out), redirect_stderr(err):
                    code = cli.main(["install", "--target", "arch", "--help"])
                self.assertEqual(code, 0, (out.getvalue(), err.getvalue()))
                self.assertIn("Usage", out.getvalue())
            self.assertFalse((home / "install-state.json").exists())
            self.assertFalse(shortcuts._wants_help(
                ["run", "--target", "arch", "hello", "--", "--help"]))
            self.assertTrue(shortcuts._wants_help(
                ["install", "--target", "arch", "--help"]))

    def test_json_stdout_has_no_progress_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\nhello\t2\n"
            runtime.package_files = {"hello": "/usr/bin/hello\n"}
            ctx = context(root, runtime)
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 0, (out, err))
            ctx.services[str(root)] = self._static_service(root, runtime)
            code, out, err = run_ctx(ctx, ["install", "hello", "--json"])
            self.assertEqual(code, 0, (out, err))
            self.assertTrue(out.lstrip().startswith("{"))
            json.loads(out)
            self.assertIn("installing", err)

    def test_reinit_and_install_refuse_a_record_with_the_wrong_target_or_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = ShortcutRuntime()
            runtime.inventory = "base\t1\nhello\t2\n"
            runtime.package_files = {"hello": "/usr/bin/hello\n"}
            ctx, _ = ready_context(root)
            service = self._static_service(root, runtime)
            ctx.services[str(root)] = service
            record = service.store.load("debian13")
            record["target"] = "fedora44"
            service.store.save(record, None)
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["install", "hello"])
            self.assertEqual(code, 1)
            self.assertIn("target", err)
            self.assertEqual(runtime.events, [])
            code, out, err = run_ctx(
                ctx, ["init", "--target", "debian13", "--yes"])
            self.assertEqual(code, 1)
            self.assertIn("does not match", err)

    def test_stop_refuses_corrupt_or_live_occupancy_without_stopping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            service = ctx.service_for("debian13")
            service.start("debian13")
            lease = root / ".lab/leases/debian13.json"
            lease.parent.mkdir(parents=True, exist_ok=True)
            lease.write_text("{not json", encoding="utf-8")
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["stop", "debian13"])
            self.assertEqual(code, 1)
            self.assertIn("cannot be confirmed", err)
            self.assertNotIn("stop", [event[0] for event in runtime.events])
            from unifier.on_demand import process_stamp
            lease.write_text(json.dumps({"leases": {"tok": {
                "pid": os.getpid(), "stamp": process_stamp(os.getpid()),
                "app_id": "hello"}}}), encoding="utf-8")
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["stop", "debian13"])
            self.assertEqual(code, 1)
            self.assertIn("hello", err)
            self.assertNotIn("stop", [event[0] for event in runtime.events])

    def test_stop_refuses_while_a_native_package_transaction_holds_the_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            service = ctx.service_for("debian13")
            service.start("debian13")
            entered, release = threading.Event(), threading.Event()
            original = service._package_transaction_locked

            def slow(*args, **kwargs):
                entered.set()
                release.wait(10)
                return {"phase": "completed"}

            service._package_transaction_locked = slow
            worker = threading.Thread(
                target=lambda: service.install_packages("debian13", ["hello"]))
            worker.start()
            try:
                self.assertTrue(entered.wait(5))
                runtime.events.clear()
                code, out, err = run_ctx(ctx, ["stop", "debian13"])
                self.assertEqual(code, 1)
                self.assertIn("package transaction", err)
                self.assertNotIn("stop", [event[0] for event in runtime.events])
            finally:
                release.set()
                worker.join(10)
                service._package_transaction_locked = original
            self.assertFalse(worker.is_alive())


    def test_desktop_exec_parser_keeps_component_arguments_and_literal_percent(self):
        from unifier.service import parse_desktop_exec
        parsed = parse_desktop_exec(
            '"/usr/bin/wps" --component=writer "100%%" %F')
        self.assertEqual(parsed["argv"],
                         ["/usr/bin/wps", "--component=writer", "100%"])
        with self.assertRaises(UnifierError):
            parse_desktop_exec('sh -c "wps %f"')
        with self.assertRaises(UnifierError):
            parse_desktop_exec('/usr/bin/app %z')

    def test_record_uuid_replacement_is_refused_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            service = ctx.service_for("debian13")
            record = service.store.load("debian13")
            record["environment_uuid"] = "00000000-0000-4000-8000-000000000001"
            service.store.save(record, None)
            runtime.events.clear()
            code, out, err = run_ctx(ctx, ["start", "debian13"])
            self.assertEqual(code, 1)
            self.assertIn("UUID", err)
            self.assertEqual(runtime.events, [])

    def test_service_stop_itself_refuses_an_active_package_lock(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ctx, runtime = ready_context(root)
            service = ctx.service_for("debian13")
            service.start("debian13")
            runtime.events.clear()
            with service.package_operation_lock("debian13"):
                with self.assertRaisesRegex(UnifierError,
                                            "native package transaction"):
                    service.stop("debian13")
            self.assertNotIn("stop", [event[0] for event in runtime.events])


if __name__ == "__main__":
    unittest.main()
