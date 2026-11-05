"""Offline contracts for native package transactions.

These tests never talk to a repository. The integration module is the one
that installs GNU hello inside real environments.
"""

import hashlib
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

from unifier import cli
from unifier.errors import UnifierError
from unifier.packages import (
    HostObserver,
    explain_native_error,
    install_commands,
    inventory_has,
    remove_commands,
    update_commands,
)
from unifier.runtime import (
    ROLE_ENVIRONMENT,
    LABEL_ENV,
    LABEL_PROJECT,
    LABEL_ROLE,
    ExecResult,
    PodmanRuntime,
    creation_argv,
    decode_output,
)
from unifier.service import EnvironmentService
from test_env import CONFIG, LOCK, PROFILES, FakeRuntime, _os_release


BANNED = ("SigLevel", "TrustAll", "AllowInsecure", "allow-unauthenticated", "--nogpgcheck")


class StaticHost:
    def snapshot(self):
        return {
            "packages": {
                "available": True, "tool": "rpm", "sha256": "abc", "count": 1, "error": None,
            },
            "entries": {"directory": "/tmp/apps", "names": ["keep.desktop"]},
        }


class FlipHost:
    def __init__(self):
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        names = ["a.desktop"] if self.calls == 1 else ["a.desktop", "b.desktop"]
        return {
            "packages": {
                "available": True, "tool": "rpm", "sha256": "same", "count": 1, "error": None,
            },
            "entries": {"directory": "/tmp/apps", "names": names},
        }


class BlindHost:
    def snapshot(self):
        return {
            "packages": {
                "available": False, "tool": "rpm", "sha256": None, "count": None, "error": "off",
            },
            "entries": {"directory": "/tmp/apps", "names": []},
        }


class _Completed:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class PackageBoxes(FakeRuntime):
    """One package set per environment. Exec is not delegated to FakeRuntime."""

    def __init__(self):
        super().__init__()
        self.installed = {}
        self.copies = []
        self.key_list = "pub 2048R/TESTKEY 2026-01-01\n"
        self.fail_packages = set()
        # Post-transaction hook output reported while the tool still exits 0.
        # Tests set these to replay a real hook failure instead of a clean run.
        self.package_stdout = ""
        self.package_stderr = ""

    def read_inventory_readonly(self, record):
        """Model a runtime with independent access to its current package DB."""
        self._owned(record["runtime"]["container_name"],
                    record["runtime"]["project"], record["id"])
        argv = ["pacman", "-Q"] if record["target"] == "arch" else ["dpkg-query"]
        from unifier.package_inventory import observation
        return observation(record, inventory=self._inventory(record["id"], argv),
                           database='fake', method='independent-fake-database',
                           consistency={'scope': 'offline fake only'})

    def copy_in(self, name, host_path, container_path, *, project, env_id):
        self._owned(name, project, env_id)
        self.copies.append((host_path, container_path, env_id))

    def exec(self, name, argv, *, project, env_id, workdir, env, capture, timeout):
        info = self._owned(name, project, env_id)
        if not info.running:
            raise UnifierError(f"{name} is not running")
        self.events.append(("exec", name, tuple(argv), workdir, dict(env), capture))
        if not capture:
            code = self.next_code
            self.next_code = 0
            return ExecResult(code, "", "")
        return self._answer(info, list(argv))

    def _answer(self, info, argv):
        env_id = info.env_id
        if argv in (["dpkg", "--audit"], ["rpm", "--verifydb"], ["pacman", "-Dk"]):
            return ExecResult(0, "", "")
        if argv[:2] in (["dpkg-query", "-L"], ["rpm", "-ql"], ["pacman", "-Ql"]):
            # Synthetic packages have no on-disk ELF payload. Real dependency
            # discovery/repair is covered by test_dependencies and live probes.
            return ExecResult(0, "", "")
        if argv == ["cat", "/etc/os-release"]:
            profile = self.identities.get(env_id)
            if profile is None:
                return ExecResult(1, "", f"no identity for {env_id}")
            return ExecResult(0, _os_release(profile), "")
        if argv == ["uname", "-r"]:
            return ExecResult(0, self.kernels.get(env_id, "host-kernel") + "\n", "")
        if argv == ["uname", "-m"]:
            return ExecResult(0, self.machines.get(env_id, "x86_64") + "\n", "")
        if self.fail_inventory and _is_inventory(argv):
            return ExecResult(1, "", "inventory failed")
        if argv[:2] == ["pacman-key", "--list-keys"]:
            if self.key_list.strip():
                return ExecResult(0, self.key_list, "")
            return ExecResult(1, "", "error: pacman-key is not initialized")
        if argv == ["pacman-key", "--init"]:
            return ExecResult(0, "", "")
        if argv[:3] == ["pacman-key", "--populate", "archlinux"]:
            self.key_list = "pub 2048R/POPULATED 2026-01-01\n"
            return ExecResult(0, "", "")
        if argv[:2] == ["mkdir", "-p"]:
            return ExecResult(0, "", "")
        if argv == ["hello"]:
            if "hello" in self.installed.get(env_id, set()):
                return ExecResult(0, "Hello, world!\n", "")
            return ExecResult(127, "", "hello: not found")
        if argv[:2] == ["pacman", "-Sy"]:
            return ExecResult(1, "", "partial upgrade is not a valid transaction")
        if argv[:3] == ["apt-get", "install", "-y"]:
            return self._add(env_id, argv[3:])
        if argv[:3] == ["apt-get", "remove", "-y"]:
            return self._remove(env_id, argv[3:])
        if argv in (["apt-get", "update"], ["apt-get", "upgrade", "-y"], ["dnf", "upgrade", "-y"]):
            return ExecResult(0, "", "")
        if argv[:3] == ["dnf", "install", "-y"]:
            return self._add(env_id, argv[3:])
        if argv[:3] == ["dnf", "remove", "-y"]:
            return self._remove(env_id, argv[3:])
        if argv[:3] == ["pacman", "-Syu", "--noconfirm"]:
            names = [item for item in argv[3:] if not item.startswith("-")]
            return self._add(env_id, names)
        if argv[:3] == ["pacman", "-R", "--noconfirm"]:
            return self._remove(env_id, argv[3:])
        if argv[:3] == ["pacman", "-U", "--noconfirm"]:
            return self._add(env_id, argv[3:])
        if _is_inventory(argv):
            return ExecResult(0, self._inventory(env_id, argv), "")
        if argv[:3] == ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"] and len(argv) == 4:
            return self._query(env_id, argv[3], "tab")
        if argv[:2] == ["rpm", "-q"]:
            return self._query(env_id, argv[-1], "tab")
        if argv[:2] == ["pacman", "-Q"] and len(argv) == 3:
            return self._query(env_id, argv[2], "space")
        return ExecResult(127, "", "fake: not implemented: " + " ".join(argv))

    def _names(self, env_id):
        return ["base-" + env_id, *sorted(self.installed.get(env_id, set()))]

    def _inventory(self, env_id, argv):
        style = "space" if argv[:1] == ["pacman"] else "tab"
        lines = []
        for name in self._names(env_id):
            lines.append(f"{name} 1-1" if style == "space" else f"{name}\t1")
        return "\n".join(lines) + "\n"

    def _query(self, env_id, name, style):
        if name not in self._names(env_id):
            return ExecResult(1, "", f"package {name} is not installed")
        line = f"{name} 1-1\n" if style == "space" else f"{name}\t1\n"
        return ExecResult(0, line, "")

    def _add(self, env_id, names):
        plain = [name for name in names if not name.startswith("/")]
        missing = [name for name in plain if name.startswith("missing-") or name in self.fail_packages]
        if missing:
            return ExecResult(100, "", f"E: Unable to locate package {missing[0]}")
        self.installed.setdefault(env_id, set()).update(plain)
        return ExecResult(0, self.package_stdout, self.package_stderr)

    def _remove(self, env_id, names):
        installed = self.installed.setdefault(env_id, set())
        absent = [name for name in names if name not in installed]
        if absent:
            return ExecResult(100, "", f"Package '{absent[0]}' is not installed, so not removed")
        for name in names:
            installed.discard(name)
        return ExecResult(0, "", "")


def _is_inventory(argv):
    return (argv == ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"]
            or argv[:2] == ["rpm", "-qa"]
            or argv == ["pacman", "-Q"])


def package_service(root, runtime=None, observer=None):
    runtime = runtime or PackageBoxes()
    stamps = {"n": 0}

    def clock():
        stamps["n"] += 1
        return f"2026-10-19T12:00:{stamps['n']:02d}+08:00"

    service = EnvironmentService(
        root, runtime, CONFIG, LOCK, host_kernel="host-kernel", clock=clock,
        uid=1000, gid=1000, host_observer=observer or StaticHost(),
        host_facts={"os_release": {"ID": "rhel", "VERSION_ID": "9"},
                    "system": "Linux", "machine": "x86_64", "libc": "glibc"},
        log=lambda _message: None)
    return service, runtime


def make_env(service, runtime, target, env_id, network="pasta"):
    runtime.identities[env_id] = dict(PROFILES[target])
    return service.create(target, env_id, [], network)


def assert_native_safe(test, txn):
    test.assertRegex(txn["id"], r"\Atxn[0-9a-f]{12}\Z")
    for step in txn["native"]:
        argv = step["argv"]
        blob = " ".join(argv)
        for banned in BANNED:
            test.assertNotIn(banned, blob)
        test.assertNotIn("-Sy", argv)
        if len(argv) >= 2 and argv[0] == "pacman" and argv[1] == "-Sy":
            test.fail(argv)


class AdapterCommands(unittest.TestCase):
    def test_commands_keep_each_native_tool_and_arch_stays_a_full_upgrade(self):
        self.assertEqual(
            [step[0] for step in install_commands("debian13", ["hello"])],
            [["apt-get", "update"], ["apt-get", "install", "-y", "hello"]])
        self.assertEqual(install_commands("debian13", ["hello"])[0][1], 180)
        self.assertEqual(
            [step[0] for step in install_commands("fedora44", ["hello"])],
            [["dnf", "install", "-y", "hello"]])
        self.assertEqual(
            [step[0] for step in install_commands("arch", ["hello"])],
            [["pacman", "-Syu", "--noconfirm", "--needed", "hello"]])
        self.assertEqual(install_commands("arch", ["hello"])[0][1], 900)
        self.assertEqual(
            [step[0] for step in update_commands("arch")],
            [["pacman", "-Syu", "--noconfirm"]])
        self.assertEqual(
            [step[0] for step in remove_commands("arch", ["hello"])],
            [["pacman", "-R", "--noconfirm", "hello"]])
        self.assertEqual(
            [step[0] for step in update_commands("debian13")],
            [["apt-get", "update"], ["apt-get", "upgrade", "-y"]])
        self.assertEqual(
            [step[0] for step in update_commands("fedora44")],
            [["dnf", "upgrade", "-y"]])
        for steps in (
            install_commands("arch", ["hello"]),
            update_commands("arch"),
            remove_commands("debian13", ["hello"]),
            install_commands("fedora44", ["hello", "coreutils"]),
        ):
            for argv, _timeout in steps:
                blob = " ".join(argv)
                for banned in BANNED:
                    self.assertNotIn(banned, blob)
                self.assertNotIn("-Sy", argv)

    def test_native_error_names_the_cause_and_leaves_other_text_alone(self):
        self.assertIn("index", explain_native_error("E: Unable to locate package hello"))
        self.assertIn("index", explain_native_error("No match for argument: hello"))
        self.assertIn("index", explain_native_error("error: target not found: hello"))
        self.assertIn("signature", explain_native_error(
            "error: hello: signature from x is invalid\n").lower())
        self.assertIn("signature", explain_native_error(
            "error: failed to commit transaction (invalid or corrupted package (PGP signature))").lower())
        self.assertIn("reached", explain_native_error(
            "Temporary failure resolving 'deb.debian.org'").lower())
        self.assertIsNone(explain_native_error("disk full"))

    def test_host_observer_sorts_package_lines_and_lists_desktop_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            apps = Path(temporary)
            (apps / "b.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
            (apps / "a.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
            (apps / "note.txt").write_text("no", encoding="utf-8")

            def runner(argv):
                self.assertEqual(argv, ["rpm", "-qa"])
                return _Completed(0, "zeta\nalpha\n", "")

            snap = HostObserver(runner=runner, os_id="fedora", applications=apps).snapshot()
            self.assertTrue(snap["packages"]["available"])
            self.assertEqual(snap["packages"]["count"], 2)
            self.assertEqual(snap["packages"]["sha256"],
                             hashlib.sha256(b"alpha\nzeta\n").hexdigest())
            self.assertEqual(snap["entries"]["names"], ["a.desktop", "b.desktop"])

            empty = HostObserver(
                runner=lambda _argv: _Completed(0, "", ""), os_id="fedora", applications=apps)
            self.assertEqual(empty.snapshot()["packages"]["sha256"],
                             hashlib.sha256(b"").hexdigest())
            self.assertEqual(empty.snapshot()["packages"]["count"], 0)

        seen = []
        HostObserver(
            runner=lambda argv: seen.append(argv) or _Completed(0, "a\t1\n", ""),
            os_id="debian").snapshot()
        self.assertEqual(seen[0][0], "dpkg-query")
        seen.clear()
        HostObserver(
            runner=lambda argv: seen.append(argv) or _Completed(0, "a 1\n", ""),
            os_id="arch").snapshot()
        self.assertEqual(seen[0], ["pacman", "-Q"])
        def forbid(_argv):
            raise AssertionError("host package command ran")

        blind = HostObserver(runner=forbid, os_id="gentoo").snapshot()
        self.assertFalse(blind["packages"]["available"])


class PackageTransactions(unittest.TestCase):
    def test_pasta_is_stored_at_create_and_none_refuses_install_before_a_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            with self.assertRaises(UnifierError):
                service.create("debian13", "deb", [], "bridge")
            self.assertEqual(runtime.events, [])
            record = make_env(service, runtime, "debian13", "deb", "pasta")
            spec = runtime.events[0][1]
            self.assertEqual(spec.network, "pasta")
            self.assertEqual(record["runtime"]["network"], "pasta")
            self.assertEqual(record["content_revision"], 1)
            argv = creation_argv(spec)
            self.assertIn("--network=pasta", argv)
            self.assertNotIn("--network=none", argv)
            self.assertEqual(sum(item.startswith("--network=") for item in argv), 1)

            # Network contracts are independent installations, not duplicate targets.
            service, runtime = package_service(root / "offline")
            make_env(service, runtime, "debian13", "plain", "none")
            with self.assertRaises(UnifierError) as caught:
                service.install_packages("plain", ["hello"])
            self.assertIn("pasta", str(caught.exception))
            self.assertFalse(any(event[0] == "exec" and event[2][:1] == ("apt-get",)
                                 for event in runtime.events))
            self.assertEqual(service.transactions_for("plain"), [])
            removed = service.remove_packages("plain", ["hello"])
            self.assertEqual(removed["phase"], "failed")
            self.assertEqual(removed["failure_phase"], "native-command")
            self.assertTrue(service.store.transaction_path(removed["id"]).is_file())

    def test_failed_install_keeps_the_inventory_and_a_failed_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            make_env(service, runtime, "fedora44", "fed")
            before = service.inspect("deb")
            peer = service.inspect("fed")["package_inventory_sha256"]
            txn = service.install_packages("deb", ["missing-pkg"])
            assert_native_safe(self, txn)
            self.assertEqual(txn["phase"], "failed")
            self.assertEqual(txn["failure_phase"], "native-command")
            self.assertIn("index", txn["native_error"])
            after = service.inspect("deb")
            self.assertEqual(after["content_revision"], 1)
            self.assertEqual(after["package_inventory_sha256"], before["package_inventory_sha256"])
            self.assertEqual(after["writable_revision"], before["writable_revision"])
            self.assertTrue(txn["other_environments"]["fed"]["unchanged"])
            self.assertEqual(service.inspect("fed")["package_inventory_sha256"], peer)
            self.assertEqual(service.inspect("fed")["runtime_state"], "stopped")
            self.assertEqual(txn["host_packages"]["before"]["sha256"],
                             txn["host_packages"]["after"]["sha256"])
            with self.assertRaises(UnifierError):
                service.install_packages("deb", ["-hello"])
            self.assertEqual(len(service.transactions_for("deb")), 1)

    def test_debian_install_update_and_remove_leave_the_peer_and_the_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            make_env(service, runtime, "fedora44", "fed")
            peer = service.inspect("fed")["package_inventory_sha256"]
            base = service.inspect("deb")
            installed = service.install_packages("deb", ["hello"], use_argv=["hello"])
            assert_native_safe(self, installed)
            self.assertEqual(installed["phase"], "completed")
            self.assertIn("Hello, world!", installed["verification"]["stdout_preview"])
            self.assertEqual(installed["verification"]["command_exit_code"], 0)
            self.assertTrue(installed["verification"]["packages"]["hello"])
            self.assertTrue(installed["other_environments"]["fed"]["unchanged"])
            self.assertEqual(installed["host_entries"], {"added": [], "removed": []})
            deb = service.inspect("deb")
            self.assertEqual(deb["content_revision"], 2)
            self.assertEqual(deb["writable_revision"], base["writable_revision"])
            self.assertNotEqual(deb["package_inventory_sha256"], base["package_inventory_sha256"])
            apt = [event for event in runtime.events
                   if event[0] == "exec" and event[2][:1] == ("apt-get",)]
            self.assertTrue(apt)
            self.assertTrue(all(event[4].get("DEBIAN_FRONTEND") == "noninteractive" for event in apt))
            self.assertEqual(service.inspect("deb")["runtime_state"], "stopped")
            self.assertEqual(service.inspect("fed")["runtime_state"], "stopped")
            self.assertEqual(service.inspect("fed")["package_inventory_sha256"], peer)

            updated = service.update_packages("deb")
            self.assertEqual(updated["phase"], "completed")
            self.assertIn(["apt-get", "upgrade", "-y"], [step["argv"] for step in updated["native"]])
            self.assertEqual(service.inspect("deb")["content_revision"], 2)
            self.assertTrue(updated["other_environments"]["fed"]["unchanged"])

            removed = service.remove_packages("deb", ["hello"], use_argv=["hello"])
            assert_native_safe(self, removed)
            self.assertEqual(removed["phase"], "completed")
            self.assertNotEqual(removed["verification"]["command_exit_code"], 0)
            self.assertFalse(removed["verification"]["packages"]["hello"])
            self.assertEqual(service.inspect("deb")["content_revision"], 3)
            self.assertTrue(removed["other_environments"]["fed"]["unchanged"])
            self.assertFalse(service.query_package("deb", "hello")["present"])
            self.assertEqual(service.inspect("deb")["runtime_state"], "stopped")

            service.remove("deb")
            self.assertFalse(service.store.exists("deb"))
            self.assertEqual(service.show_transaction(installed["id"])["phase"], "completed")

    def test_fedora_install_uses_dnf(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "fedora44", "fed")
            make_env(service, runtime, "debian13", "deb", "none")
            txn = service.install_packages("fed", ["hello"], use_argv=["hello"])
            assert_native_safe(self, txn)
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual([step["argv"] for step in txn["native"]],
                             [["dnf", "install", "-y", "hello"]])
            self.assertIn("Hello, world!", txn["verification"]["stdout_preview"])
            self.assertTrue(txn["other_environments"]["deb"]["unchanged"])
            dnf = [event for event in runtime.events
                   if event[0] == "exec" and event[2][:1] == ("dnf",)]
            self.assertEqual(dnf[-1][4], {"LC_ALL": "C.UTF-8"})
            self.assertTrue(service.query_package("fed", "hello")["present"])

    def test_arch_initializes_the_keyring_once_and_upgrades_the_whole_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            created = make_env(service, runtime, "arch", "box")
            self.assertIsNone(created["arch"]["keyring_initialized_at"])
            self.assertIsNone(created["arch"]["repository_updated_at"])
            first = service.install_packages("box", ["hello"], use_argv=["hello"])
            assert_native_safe(self, first)
            self.assertEqual(first["phase"], "completed")
            self.assertEqual([step["argv"] for step in first["native"]], [
                ["pacman-key", "--init"],
                ["pacman", "-Syu", "--noconfirm", "--needed", "hello"],
            ])
            self.assertIn("Hello, world!", first["verification"]["stdout_preview"])
            arch = service.inspect("box")
            self.assertEqual(arch["content_revision"], 2)
            self.assertEqual(arch["writable_revision"], created["writable_revision"])
            self.assertIsNotNone(arch["arch"]["keyring_initialized_at"])
            self.assertEqual(arch["arch"]["repository_updated_at"], arch["updated_at"])
            self.assertEqual(arch["arch"]["package_set_sha256"], arch["package_inventory_sha256"])
            stamped = arch["arch"]["keyring_initialized_at"]
            second = service.install_packages("box", ["hello"], use_argv=["hello"])
            self.assertEqual(second["phase"], "completed")
            self.assertEqual([step["argv"] for step in second["native"]], [
                ["pacman", "-Syu", "--noconfirm", "--needed", "hello"],
            ])
            self.assertEqual(service.inspect("box")["arch"]["keyring_initialized_at"], stamped)
            updated = service.update_packages("box")
            self.assertEqual(updated["phase"], "completed")
            self.assertEqual(updated["native"][0]["argv"], ["pacman", "-Syu", "--noconfirm"])
            self.assertGreater(service.inspect("box")["arch"]["repository_updated_at"],
                               arch["arch"]["repository_updated_at"])

    def test_arch_populates_an_empty_keyring(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runtime = PackageBoxes()
            runtime.key_list = ""
            service, runtime = package_service(root, runtime)
            make_env(service, runtime, "arch", "box")
            txn = service.install_packages("box", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual([step["argv"] for step in txn["native"]], [
                ["pacman-key", "--init"],
                ["pacman-key", "--populate", "archlinux"],
                ["pacman", "-Syu", "--noconfirm", "--needed", "hello"],
            ])
            self.assertIsNotNone(service.inspect("box")["arch"]["keyring_initialized_at"])

    def test_local_deb_is_copied_in_and_a_foreign_suffix_writes_no_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            package = root / "hello.deb"
            package.write_bytes(b"not-a-real-deb")
            txn = service.install_local("deb", str(package))
            assert_native_safe(self, txn)
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual(runtime.copies[0][1], "/tmp/ml-incoming/hello.deb")
            self.assertIn(["apt-get", "install", "-y", "/tmp/ml-incoming/hello.deb"],
                          [step["argv"] for step in txn["native"]])
            self.assertEqual(service.inspect("deb")["content_revision"], 1)
            foreign = root / "hello.rpm"
            foreign.write_bytes(b"x")
            with self.assertRaises(UnifierError):
                service.install_local("deb", str(foreign))
            self.assertEqual(len(service.transactions_for("deb")), 1)

    def test_host_entry_change_fails_verification_without_removing_the_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root, observer=FlipHost())
            make_env(service, runtime, "debian13", "deb")
            txn = service.install_packages("deb", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["phase"], "failed")
            self.assertEqual(txn["failure_phase"], "verification")
            self.assertEqual(txn["host_entries"]["added"], ["b.desktop"])
            self.assertEqual(service.inspect("deb")["content_revision"], 2)
            self.assertTrue(service.query_package("deb", "hello")["present"])
            inventory = (root / service.inspect("deb")["package_inventory_path"]).read_text()
            self.assertTrue(inventory_has(inventory, "hello"))

    def test_unavailable_host_observer_fails_verification(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root, observer=BlindHost())
            make_env(service, runtime, "debian13", "deb")
            txn = service.install_packages("deb", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["failure_phase"], "verification")
            self.assertIn("host package observer unavailable", txn["verification"]["isolation"])
            self.assertTrue(service.query_package("deb", "hello")["present"])


class PackageCommandLine(unittest.TestCase):
    def test_create_forwards_network_and_pkg_returns_the_transaction_status(self):
        seen = {}

        class Spy:
            def create(self, target, env_id, shares, network="none", desktop=False):
                seen["args"] = (target, env_id, shares, network, desktop)
                return {"id": env_id or target, "target": target, "runtime_state": "stopped"}

        original = cli.service_factory
        cli.service_factory = lambda: Spy()
        try:
            code, out, _err = _run_cli(
                ["env", "create", "--target", "debian13", "--id", "deb", "--network", "pasta"])
        finally:
            cli.service_factory = original
        self.assertEqual(code, 0)
        self.assertEqual(seen["args"], ("debian13", "deb", [], "pasta", False))
        self.assertIn("created deb", out)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            cli.service_factory = lambda: service
            try:
                failed_code, failed_out, failed_err = _run_cli(["pkg", "install", "deb", "missing-pkg"])
                failed = json.loads(failed_out)
                show_code, show_out, _show_err = _run_cli(["pkg", "show", failed["id"]])
                log_code, log_out, _log_err = _run_cli(["pkg", "log", "deb"])
                ok_code, ok_out, _ok_err = _run_cli(
                    ["pkg", "install", "deb", "hello", "--use-command", "hello"])
                query_code, query_out, _query_err = _run_cli(["pkg", "query", "deb", "hello"])
                bad_code, _bad_out, bad_err = _run_cli(["pkg", "show", "../x"])
            finally:
                cli.service_factory = original
        self.assertEqual(failed_code, 1)
        self.assertEqual(failed_err, "")
        self.assertEqual(failed["phase"], "failed")
        self.assertIn("index", failed["native_error"])
        self.assertEqual(show_code, 0)
        self.assertEqual(json.loads(show_out)["id"], failed["id"])
        self.assertEqual(log_code, 0)
        self.assertEqual(json.loads(log_out)[0]["id"], failed["id"])
        self.assertEqual(ok_code, 0)
        self.assertEqual(json.loads(ok_out)["phase"], "completed")
        self.assertEqual(query_code, 0)
        self.assertTrue(json.loads(query_out)["present"])
        self.assertEqual(bad_code, 1)
        self.assertIn("transaction id", bad_err)


def _run_cli(argv):
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        code = cli.main(argv)
    return code, stdout.getvalue(), stderr.getvalue()


class _Result:
    def __init__(self, returncode, stdout, stderr):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class CopyBoundary(unittest.TestCase):
    def test_copy_in_does_not_pause_the_container(self):
        calls = []
        body = {
            "Id": "cid",
            "Config": {"Labels": {
                LABEL_PROJECT: "/project", LABEL_ENV: "deb", LABEL_ROLE: ROLE_ENVIRONMENT,
            }},
            "State": {
                "Running": True, "Status": "running", "Pid": 1, "ExitCode": 0,
                "OOMKilled": False, "StartedAt": "", "FinishedAt": "",
            },
        }

        def runner(argv, *, capture, timeout):
            calls.append(list(argv))
            if argv[1] == "inspect":
                return _Result(0, json.dumps([body]), "")
            return _Result(0, "", "")

        runtime = PodmanRuntime(runner, check_rootless=False)
        runtime.copy_in("ml-deb", "/tmp/hello.deb", "/tmp/ml-incoming/hello.deb",
                        project="/project", env_id="deb")
        self.assertEqual(calls[-1], [
            "podman", "cp", "--pause=false", "/tmp/hello.deb",
            "ml-deb:/tmp/ml-incoming/hello.deb"])
        self.assertEqual(calls[0][1], "inspect")

    def test_non_utf8_tool_output_is_replaced_instead_of_failing_the_call(self):
        self.assertEqual(decode_output(b"Ke\xdfler"), "Ke\ufffdler")
        self.assertEqual(decode_output(None), "")
        self.assertEqual(decode_output("already"), "already")


if __name__ == "__main__":
    unittest.main()
