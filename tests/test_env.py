"""Offline contracts for environment identity, execution and ownership."""

from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux import cli
from multilinux.errors import MultilinuxError
from multilinux.runtime import (
    ROLE_ENVIRONMENT,
    LABEL_ENV,
    LABEL_PROJECT,
    LABEL_ROLE,
    SLEEPER,
    ContainerState,
    CreateSpec,
    ExecResult,
    PodmanRuntime,
    container_name,
    creation_argv,
    exec_argv,
)
from multilinux.service import EnvironmentService, normalize_inventory, validate_env_id


def _digest(char: str) -> str:
    return "sha256:" + char * 64


def _lock_item(source: str, char: str, label):
    digest = _digest(char)
    return {
        "source": source,
        "manifest_digest": digest,
        "locked_reference": source.rsplit(":", 1)[0] + "@" + digest,
        "image_version_label": label,
    }


CONFIG = {
    "targets": {
        "debian13": {
            "source": "docker.io/library/debian:13-slim",
            "expected_id": "debian",
            "expected_version_id": "13",
            "update_policy": "versioned-release",
        },
        "fedora44": {
            "source": "registry.fedoraproject.org/fedora:44",
            "expected_id": "fedora",
            "expected_version_id": "44",
            "update_policy": "versioned-release",
        },
        "arch": {
            "source": "docker.io/library/archlinux:latest",
            "expected_id": "arch",
            "update_policy": "full-system-upgrade",
        },
    }
}

LOCK = {
    "targets": {
        "debian13": _lock_item("docker.io/library/debian:13-slim", "a", None),
        "fedora44": _lock_item("registry.fedoraproject.org/fedora:44", "b", "44"),
        "arch": _lock_item("docker.io/library/archlinux:latest", "c", "20261011.0.609700"),
    }
}

PROFILES = {
    "debian13": {"ID": "debian", "VERSION_ID": "13", "BUILD_ID": "",
                 "PRETTY_NAME": "Debian GNU/Linux 13 (trixie)"},
    "fedora44": {"ID": "fedora", "VERSION_ID": "44", "BUILD_ID": "",
                 "PRETTY_NAME": "Fedora Linux 44 (Container Image)"},
    "arch": {"ID": "arch", "VERSION_ID": "20261011.0.609700", "BUILD_ID": "rolling",
             "PRETTY_NAME": "Arch Linux"},
}


def _os_release(profile: dict) -> str:
    return "".join(f'{key}="{profile[key]}"\n' for key in
                   ("ID", "VERSION_ID", "BUILD_ID", "PRETTY_NAME"))


class FakeRuntime:
    def __init__(self):
        self.containers = {}
        self.identities = {name: dict(profile) for name, profile in PROFILES.items()}
        self.kernels = {}
        self.machines = {}
        self.inventory = "b\t2\na\t1\n"
        self.fail_inventory = False
        self.events = []
        self.next_code = 0

    def inspect(self, name):
        return self.containers.get(name)

    def create(self, spec):
        self.events.append(("create", spec))
        if spec.name in self.containers:
            raise MultilinuxError(f"container {spec.name} already exists")
        self.containers[spec.name] = ContainerState(
            name=spec.name, container_id="cid-" + spec.env_id, running=False,
            status="created", project=spec.project, env_id=spec.env_id,
            role=ROLE_ENVIRONMENT, image=spec.image, pid=0, started_at=None,
            finished_at=None, oom_killed=False, exit_code=0)
        return "cid-" + spec.env_id

    def start(self, name, *, project, env_id):
        info = self._owned(name, project, env_id)
        self.events.append(("start", name))
        self.containers[name] = replace(info, running=True, status="running", pid=50,
                                         started_at="2026-10-19T12:00:01+08:00")

    def stop(self, name, *, project, env_id):
        info = self._owned(name, project, env_id)
        self.events.append(("stop", name))
        self.containers[name] = replace(info, running=False, status="exited", pid=0,
                                         finished_at="2026-10-19T12:00:02+08:00")

    def remove(self, name, *, project, env_id):
        self._owned(name, project, env_id)
        self.events.append(("remove", name))
        del self.containers[name]

    def exec(self, name, argv, *, project, env_id, workdir, env, capture, timeout):
        info = self._owned(name, project, env_id)
        if not info.running:
            raise MultilinuxError(f"{name} is not running")
        self.events.append(("exec", name, tuple(argv), workdir, dict(env), capture))
        if not capture:
            code = self.next_code
            self.next_code = 0
            return ExecResult(code, "", "")
        return self._captured(info, argv)

    def _captured(self, info, argv):
        if argv == ["cat", "/etc/os-release"]:
            return ExecResult(0, _os_release(self.identities[info.env_id]), "")
        if argv == ["uname", "-r"]:
            return ExecResult(0, self.kernels.get(info.env_id, "host-kernel") + "\n", "")
        if argv == ["uname", "-m"]:
            return ExecResult(0, self.machines.get(info.env_id, "x86_64") + "\n", "")
        if argv and argv[0] in {"dpkg-query", "rpm", "pacman"}:
            if self.fail_inventory:
                return ExecResult(1, "", "inventory failed")
            return ExecResult(0, self.inventory, "")
        return ExecResult(0, "", "")

    def _owned(self, name, project, env_id):
        info = self.containers.get(name)
        if info is None:
            raise MultilinuxError(f"container {name} does not exist")
        if info.project != project or info.env_id != env_id or info.role != ROLE_ENVIRONMENT:
            raise MultilinuxError(f"refusing to touch {name}")
        return info


def service_in(root: Path, runtime=None, lock=None):
    runtime = runtime or FakeRuntime()
    service = EnvironmentService(
        root, runtime, CONFIG, lock or LOCK, host_kernel="host-kernel",
        clock=lambda: "2026-10-19T12:00:00+08:00", uid=1000, gid=1000)
    return service, runtime


class IdentityAndRecord(unittest.TestCase):
    def test_create_records_identity_inventory_and_leaves_container_stopped(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            record = service.create("debian13", None, [])
            self.assertEqual(record["id"], "debian13")
            self.assertEqual(record["distribution_id"], "debian")
            self.assertEqual(record["version_track"], "13")
            self.assertEqual(record["architecture"], "amd64")
            self.assertEqual(record["runtime_state"], "stopped")
            self.assertEqual(record["writable_revision"], "cid-debian13")
            self.assertEqual(record["base_image"]["manifest_digest"], _digest("a"))
            self.assertTrue(record["base_image"]["locked_reference"].endswith("@" + _digest("a")))
            self.assertIsNone(record["arch"])
            self.assertEqual(record["shared_mounts"], [])
            self.assertEqual(record["created_at"], "2026-10-19T12:00:00+08:00")
            self.assertFalse(runtime.containers[container_name(root, "debian13")].running)
            inventory = normalize_inventory("b\t2\na\t1\n")
            stored = (root / record["package_inventory_path"]).read_text()
            self.assertEqual(stored, inventory)
            self.assertEqual(record["package_inventory_sha256"],
                             hashlib.sha256(inventory.encode()).hexdigest())
            self.assertEqual(record["package_inventory_count"], 2)
            self.assertEqual(runtime.events[0][1].image, record["base_image"]["locked_reference"])

    def test_arch_keeps_image_version_update_time_and_package_set_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = service_in(Path(temporary))
            record = service.create("arch", None, [])
            self.assertEqual(record["version_track"], "rolling")
            self.assertEqual(record["arch"]["image_version_label"], "20261011.0.609700")
            self.assertIsNone(record["arch"]["repository_updated_at"])
            self.assertEqual(record["arch"]["package_set_sha256"], record["package_inventory_sha256"])
            self.assertEqual(record["arch"]["os_release_version_id"], "20261011.0.609700")
            self.assertEqual(record["arch"]["os_release_build_id"], "rolling")

    def test_wrong_distribution_kernel_or_inventory_removes_the_container(self):
        cases = ("distribution", "kernel", "inventory")
        for case in cases:
            with self.subTest(case=case), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                runtime = FakeRuntime()
                if case == "distribution":
                    runtime.identities["debian13"]["ID"] = "ubuntu"
                elif case == "kernel":
                    runtime.kernels["debian13"] = "other-kernel"
                else:
                    runtime.fail_inventory = True
                service, runtime = service_in(root, runtime)
                with self.assertRaises(MultilinuxError):
                    service.create("debian13", None, [])
                self.assertFalse(service.store.exists("debian13"))
                self.assertEqual(runtime.containers, {})
                self.assertIn(("remove", container_name(root, "debian13")), runtime.events)

    def test_unlocked_reference_never_reaches_the_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = json.loads(json.dumps(LOCK))
            lock["targets"]["debian13"]["locked_reference"] = "docker.io/library/debian:13-slim"
            service, runtime = service_in(root, lock=lock)
            with self.assertRaises(MultilinuxError):
                service.create("debian13", None, [])
            self.assertEqual(runtime.events, [])

    def test_environment_id_and_duplicate_create_are_rejected(self):
        for value in ("", "Debian", "a-", "has_space", "a" * 33):
            with self.subTest(value=value):
                with self.assertRaises(MultilinuxError):
                    validate_env_id(value)
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            runtime.identities["docs"] = dict(PROFILES["debian13"])
            service.create("debian13", "docs", [])
            with self.assertRaises(MultilinuxError):
                service.create("fedora44", "docs", [])
            self.assertEqual(len(runtime.containers), 1)


class ExecutionAndOwnership(unittest.TestCase):
    def test_run_passes_argv_workdir_exit_code_and_only_the_allowlist(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            service.create("debian13", None, [])
            service.create("fedora44", None, [])
            with self.assertRaises(MultilinuxError):
                service.run("debian13", ["true"], workdir="/usr", env_keys=[])
            before = len(runtime.events)
            service.start("debian13")
            os.environ["ML_TEST_PASS"] = "yes"
            os.environ["ML_TEST_SECRET"] = "hidden"
            try:
                runtime.next_code = 17
                code = service.run("debian13", ["printf", "%s", "hi"], workdir="/usr",
                                   env_keys=["ML_TEST_PASS"])
            finally:
                os.environ.pop("ML_TEST_PASS", None)
                os.environ.pop("ML_TEST_SECRET", None)
            self.assertEqual(code, 17)
            user_exec = [event for event in runtime.events[before:]
                         if event[0] == "exec" and event[5] is False]
            self.assertEqual(user_exec, [("exec", container_name(root, "debian13"),
                                          ("printf", "%s", "hi"), "/usr",
                                          {"ML_TEST_PASS": "yes"}, False)])
            with self.assertRaises(MultilinuxError):
                service.run("debian13", ["true"], workdir="relative", env_keys=[])
            with self.assertRaises(MultilinuxError):
                service.run("debian13", ["true"], workdir="/usr", env_keys=["ML_TEST_MISSING"])

    def test_start_and_stop_are_repeatable_and_run_stays_in_its_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            service.create("debian13", None, [])
            name = container_name(root, "debian13")
            stops_after_create = [event for event in runtime.events if event[:2] == ("stop", name)]
            self.assertEqual(len(stops_after_create), 1)
            service.stop("debian13")
            self.assertEqual(len([event for event in runtime.events if event[:2] == ("stop", name)]), 1)
            service.start("debian13")
            service.start("debian13")
            self.assertEqual(len([event for event in runtime.events if event[:2] == ("start", name)]), 2)
            service.run("debian13", ["true"], workdir=None, env_keys=[])
            self.assertTrue(all(event[1] == name for event in runtime.events
                                if event[0] == "exec" and event[2] == ("true",)))
            view = service.inspect("debian13")
            self.assertEqual(view["runtime_state"], "running")
            self.assertEqual(view["resources"]["pid"], 50)
            self.assertTrue(view["kernel_matches_current_host"])

    def test_foreign_or_missing_container_is_not_destroyed_by_mistake(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            service.create("debian13", None, [])
            name = container_name(root, "debian13")
            runtime.containers[name] = replace(runtime.containers[name], project="elsewhere")
            with self.assertRaises(MultilinuxError):
                service.remove("debian13")
            with self.assertRaises(MultilinuxError):
                service.start("debian13")
            self.assertTrue(service.store.exists("debian13"))
            self.assertEqual(runtime.containers[name].project, "elsewhere")
            self.assertNotIn(("remove", name), runtime.events)

            del runtime.containers[name]
            self.assertEqual(service.inspect("debian13")["runtime_state"], "missing")
            service.remove("debian13")
            self.assertFalse(service.store.exists("debian13"))

    def test_share_paths_stay_out_of_the_system_tree_and_laboratory_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = service_in(root)
            notes = root / "notes"
            notes.mkdir()
            colon = root / "has:colon"
            colon.mkdir()
            (root / ".lab").mkdir()
            record = service.create("debian13", None, [str(notes)])
            self.assertEqual(record["shared_mounts"][0]["host"], notes.resolve().as_posix())
            self.assertEqual(record["shared_mounts"][0]["options"], "z")
            refused = [str(root), str(root / ".lab"), str(root.parent), "/usr/bin", "notes",
                       str(colon)]
            for path in refused:
                with self.subTest(path=path), self.assertRaises(MultilinuxError):
                    service.create("fedora44", "other", [path])
            with self.assertRaises(MultilinuxError):
                service.create("fedora44", "other", [str(notes), str(notes)])
            self.assertEqual(len(runtime.containers), 1)


class PodmanCommandBoundary(unittest.TestCase):
    def test_create_and_exec_argv_are_locked_to_one_owned_environment(self):
        spec = CreateSpec("ml-demo", "demo", "/project",
                          "example.invalid/app@sha256:" + "d" * 64,
                          (("/data/notes", "/data/notes"),))
        argv = creation_argv(spec)
        self.assertEqual(argv[-2:], [spec.image, "infinity"])
        self.assertIn("--pull=never", argv)
        self.assertIn("--network=none", argv)
        self.assertIn(SLEEPER, argv)
        self.assertIn("/data/notes:/data/notes:z", argv)
        self.assertNotIn("--userns=keep-id", argv)
        self.assertNotIn("--read-only", argv)
        self.assertIn(f"{LABEL_PROJECT}=/project", argv)
        self.assertIn(f"{LABEL_ENV}=demo", argv)
        self.assertIn(f"{LABEL_ROLE}={ROLE_ENVIRONMENT}", argv)
        with self.assertRaises(MultilinuxError):
            creation_argv(replace(spec, image="example.invalid/app:latest"))
        self.assertEqual(
            exec_argv("ml-demo", ["printf", "%s", "hi"], workdir="/usr", env={"LANG": "C"}),
            ["podman", "exec", "--workdir", "/usr", "--env", "LANG=C", "ml-demo",
             "printf", "%s", "hi"])

    def test_runtime_does_not_operate_on_a_foreign_or_missing_name(self):
        runner = _Runner()
        runtime = PodmanRuntime(runner, check_rootless=False)
        self.assertIsNone(runtime.inspect("missing"))
        runner.missing = False
        runner.inspect_body = _inspect_body(project="elsewhere", env_id="demo")
        for action in (runtime.start, runtime.stop, runtime.remove):
            runner.calls.clear()
            with self.subTest(action=action.__name__), self.assertRaises(MultilinuxError):
                action("ml-demo", project="/project", env_id="demo")
            self.assertEqual([call[1] for call in runner.calls], ["inspect"])
        runner.calls.clear()
        with self.assertRaises(MultilinuxError):
            runtime.exec("ml-demo", ["true"], project="/project", env_id="demo",
                         workdir=None, env={}, capture=True, timeout=5)
        self.assertEqual([call[1] for call in runner.calls], ["inspect"])

        runner.inspect_body = _inspect_body(project="/project", env_id="demo", running=True)
        result = runtime.exec("ml-demo", ["printf", "%s", "hi"], project="/project",
                              env_id="demo", workdir="/usr", env={"LANG": "C"},
                              capture=True, timeout=5)
        self.assertEqual(result.returncode, 9)
        executed = [call for call in runner.calls if call[1] == "exec"][-1]
        self.assertEqual(executed, exec_argv("ml-demo", ["printf", "%s", "hi"],
                                             workdir="/usr", env={"LANG": "C"}))


class CommandLine(unittest.TestCase):
    def test_run_splits_argv_from_options(self):
        args = cli.parse_run_args(["demo", "--workdir", "/usr", "--env", "LANG", "--",
                                   "printf", "%s", "hi"])
        self.assertEqual((args.id, args.workdir, args.env, args.argv),
                         ("demo", "/usr", ["LANG"], ["printf", "%s", "hi"]))
        with self.assertRaises(MultilinuxError):
            cli.parse_run_args(["demo", "printf", "hi"])

    def test_run_exit_code_is_the_command_and_remove_requires_confirmation(self):
        class Spy:
            def __init__(self):
                self.removed = False
                self.run_args = None

            def run(self, env_id, argv, *, workdir, env_keys):
                self.run_args = (env_id, argv, workdir, env_keys)
                return 17

            def remove(self, env_id):
                self.removed = True

        spy = Spy()
        original = cli.service_factory
        cli.service_factory = lambda: spy
        try:
            self.assertEqual(cli.main(["env", "run", "demo", "--workdir", "/usr", "--",
                                       "sh", "-c", "exit 17"]), 17)
            self.assertEqual(spy.run_args, ("demo", ["sh", "-c", "exit 17"], "/usr", []))
            self.assertEqual(cli.main(["env", "remove", "demo"]), 1)
            self.assertFalse(spy.removed)
            self.assertEqual(cli.main(["env", "remove", "demo", "--yes"]), 0)
            self.assertTrue(spy.removed)
        finally:
            cli.service_factory = original


class _Runner:
    def __init__(self):
        self.calls = []
        self.missing = True
        self.inspect_body = None
        self.exec_code = 9

    def __call__(self, argv, *, capture, timeout):
        self.calls.append(list(argv))
        if argv[1] == "inspect":
            if self.missing or self.inspect_body is None:
                return _Result(1, "", "Error: no such object: missing")
            return _Result(0, json.dumps([self.inspect_body]), "")
        if argv[1] == "exec":
            return _Result(self.exec_code, "", "")
        return _Result(0, "cid-created\n", "")


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _inspect_body(*, project, env_id, running=False):
    return {
        "Id": "cid-demo",
        "ImageName": "example.invalid/app@sha256:" + "d" * 64,
        "Config": {"Labels": {
            LABEL_PROJECT: project, LABEL_ENV: env_id, LABEL_ROLE: ROLE_ENVIRONMENT}},
        "State": {"Running": running, "Status": "running" if running else "exited",
                  "Pid": 7 if running else 0, "StartedAt": "", "FinishedAt": "",
                  "OOMKilled": False, "ExitCode": 0},
    }


if __name__ == "__main__":
    unittest.main()
