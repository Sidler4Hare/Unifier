"""Missing plugin dependencies, repository ambiguity and transaction integrity."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from unifier.dependencies import CACHE, SCAN, parse_scan, provider_command, provider_names
from unifier.errors import UnifierError
from unifier.packages import package_command, package_environment, parse_package_files
from unifier.runtime import ExecResult
from test_packages import PackageBoxes, make_env, package_service


PLUGIN = """UNIFIER_ELF\t/opt/vendor/libframe.so
  Class:                             ELF64
  Machine:                           Advanced Micro Devices X86-64
 0x0000000000000001 (NEEDED) Shared library: [libasound.so.2]
UNIFIER_SCAN_DONE
"""


class PluginBoxes(PackageBoxes):
    def __init__(self):
        super().__init__()
        self.providers = "libasound2t64\n"
        self.repair_effective = True
        self.native_modes = []

    def package_exec(self, name, argv, *, project, env_id, env, interactive, stream, timeout):
        self.native_modes.append((argv, env, interactive, stream))
        # The fake models native confirmation separately from command policy.
        normalized = list(argv)
        if normalized[:3] == ["apt-get", "-o", "Dpkg::Use-Pty=0"]:
            normalized[1:3] = []
        if interactive and normalized[:2] == ["apt-get", "install"]:
            normalized.insert(2, "-y")
        return self.exec(name, normalized, project=project, env_id=env_id,
                         workdir=None, env=env, capture=True, timeout=timeout)

    def _answer(self, info, argv):
        if argv[:2] == ["dpkg-query", "-L"]:
            return ExecResult(0, "/opt/vendor/libframe.so\n", "")
        if argv[:2] == ["/bin/sh", "-c"]:
            if argv[2] == SCAN:
                return ExecResult(0, PLUGIN, "")
            if argv[2] == CACHE:
                installed = "libasound2t64" in self.installed.get(info.env_id, set())
                return ExecResult(0, "libasound.so.2 (libc6,x86-64) => /usr/lib/libasound.so.2\n"
                                  if installed and self.repair_effective else "", "")
            if argv[2] == "command -v readelf":
                return ExecResult(0, "/usr/bin/readelf\n", "")
        if argv[:2] == ["apt-file", "update"]:
            return ExecResult(0, "", "")
        if argv[:1] == ["apt-file"]:
            return ExecResult(0 if self.providers else 1, self.providers, "")
        return super()._answer(info, argv)


def elf_record(path, needed=(), sonames=()):
    return (f"UNIFIER_ELF\t{path}\n"
            "  Class:                             ELF64\n"
            "  Machine:                           Advanced Micro Devices X86-64\n"
            + "".join(f" (NEEDED) Shared library: [{name}]\n" for name in needed)
            + "".join(f" (SONAME) Library soname: [{name}]\n" for name in sonames))


class RecursivePluginBoxes(PluginBoxes):
    """Repository packages can themselves omit ELF dependencies."""

    package_paths = {"vendor": "/opt/vendor/libframe.so",
                     "libasound2t64": "/usr/lib/libasound.so.2",
                     "codec-runtime": "/usr/lib/libcodec.so.1"}

    def __init__(self):
        super().__init__()
        self.last_needed = []
        self.scans = []

    def _answer(self, info, argv):
        if argv[:2] == ["dpkg-query", "-L"]:
            path = self.package_paths.get(argv[2])
            return ExecResult(0, path + "\n" if path else "", "")
        if argv[:3] == ["/bin/sh", "-c", SCAN]:
            self.scans.append(argv[4:])
            records = {
                self.package_paths["vendor"]: elf_record(self.package_paths["vendor"], ["libasound.so.2"]),
                self.package_paths["libasound2t64"]: elf_record(
                    self.package_paths["libasound2t64"], ["libcodec.so.1"], ["libasound.so.2"]),
                self.package_paths["codec-runtime"]: elf_record(
                    self.package_paths["codec-runtime"], self.last_needed, ["libcodec.so.1"]),
            }
            return ExecResult(0, "".join(records[path] for path in argv[4:]) + "UNIFIER_SCAN_DONE\n", "")
        if argv[:3] == ["/bin/sh", "-c", CACHE]:
            installed = self.installed.get(info.env_id, set())
            return ExecResult(0, "".join(
                f"{library} (libc6,x86-64) => {self.package_paths[package]}\n"
                for package, library in (("libasound2t64", "libasound.so.2"),
                                         ("codec-runtime", "libcodec.so.1"))
                if package in installed and self.repair_effective), "")
        if argv[:1] == ["apt-file"] and argv[1:2] != ["update"]:
            package = "codec-runtime" if "libcodec" in argv[-1] else "libasound2t64"
            return ExecResult(0, package + "\n", "")
        return super()._answer(info, argv)


class DependencyTransactions(unittest.TestCase):
    def transaction(self, runtime, **kwargs):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        service, _ = package_service(Path(tmp.name), runtime)
        make_env(service, runtime, "debian13", "deps")
        return service, service.install_packages("deps", ["vendor"], repair_dependencies=True, **kwargs)

    def test_plugin_missing_undeclared_alsa_is_installed_and_rechecked(self):
        runtime = PluginBoxes()
        service, txn = self.transaction(runtime)
        self.assertEqual(txn["phase"], "completed", txn)
        report = txn["dependency_repair"]
        self.assertEqual(report["status"], "repaired")
        self.assertEqual(report["installed"], ["libasound2t64"])
        self.assertEqual(report["unresolved"], [])
        self.assertEqual(report["objects"], 1)
        self.assertNotEqual(txn["revision_before"], txn["revision_after"])
        self.assertEqual(service.show_transaction(txn["id"])["dependency_repair"], report)

    def test_ambiguous_provider_is_reported_without_guessing(self):
        runtime = PluginBoxes()
        runtime.providers = "libasound2t64\nalternative-alsa\n"
        _, txn = self.transaction(runtime)
        self.assertEqual(txn["failure_phase"], "dependency-repair")
        self.assertIn("2 repository providers", txn["native_error"])
        self.assertNotIn("libasound2t64", runtime.installed["deps"])

    def test_absent_provider_and_ineffective_install_are_bounded_failures(self):
        for absent in (True, False):
            runtime = PluginBoxes()
            if absent:
                runtime.providers = ""
            else:
                runtime.repair_effective = False
            _, txn = self.transaction(runtime)
            self.assertEqual(txn["phase"], "failed")
            self.assertEqual(txn["dependency_repair"]["status"], "unresolved")
            self.assertLessEqual(len(txn["dependency_repair"]["rounds"]), 2)

    def test_interactive_repair_retains_native_questions(self):
        runtime = PluginBoxes()
        _, txn = self.transaction(runtime, interactive=True, stream=True)
        self.assertEqual(txn["phase"], "completed", txn)
        for argv, env, interactive, stream in runtime.native_modes:
            self.assertTrue(interactive and stream)
            self.assertNotIn("-y", argv)
            self.assertNotEqual(env.get("DEBIAN_FRONTEND"), "noninteractive")

    def test_failed_dependency_install_is_not_reported_as_success(self):
        runtime = PluginBoxes()
        runtime.fail_packages.add("libasound2t64")
        _, txn = self.transaction(runtime)
        self.assertEqual(txn["failure_phase"], "native-command")
        self.assertEqual(txn["dependency_repair"]["status"], "unresolved")

    def test_installed_provider_elf_dependencies_are_recursively_rechecked(self):
        runtime = RecursivePluginBoxes()
        service, txn = self.transaction(runtime)
        report = txn["dependency_repair"]
        self.assertEqual(txn["phase"], "completed", txn)
        self.assertEqual(report["installed"], ["libasound2t64", "codec-runtime"])
        self.assertEqual(report["objects"], 3)
        self.assertEqual([round_["missing"] for round_ in report["rounds"]],
                         [["libasound.so.2"], ["libcodec.so.1"]])
        self.assertEqual(runtime.scans[-1], sorted(runtime.package_paths.values()))
        self.assertEqual(service.show_transaction(txn["id"])["dependency_repair"], report)

    def test_recursive_repair_limit_keeps_the_last_missing_library(self):
        runtime = RecursivePluginBoxes()
        runtime.last_needed = ["libthird.so.1"]
        _, txn = self.transaction(runtime)
        self.assertEqual(txn["failure_phase"], "dependency-repair", txn)
        report = txn["dependency_repair"]
        self.assertEqual(report["status"], "unresolved")
        self.assertEqual(report["unresolved"], ["libthird.so.1"])
        self.assertEqual(len(report["rounds"]), 2)
        self.assertIn("did not converge", report["error"])

    def test_new_provider_soname_cannot_hide_an_ineffective_install(self):
        runtime = RecursivePluginBoxes()
        runtime.repair_effective = False
        _, txn = self.transaction(runtime)
        self.assertEqual(txn["failure_phase"], "dependency-repair", txn)
        self.assertIn("libasound.so.2", txn["dependency_repair"]["unresolved"])

    def test_partial_provider_output_on_query_failure_is_never_installed(self):
        runtime = PluginBoxes()
        answer = runtime._answer

        def failed_query(info, argv):
            if argv[:1] == ["apt-file"] and argv[1:2] != ["update"]:
                return ExecResult(1, "libasound2t64\n", "Contents index could not be read")
            return answer(info, argv)

        with patch.object(runtime, "_answer", side_effect=failed_query):
            _, txn = self.transaction(runtime)
        self.assertEqual(txn["failure_phase"], "dependency-repair", txn)
        self.assertNotIn("libasound2t64", runtime.installed["deps"])
        self.assertIn("Contents index could not be read", txn["dependency_repair"]["error"])


class DependencyParsing(unittest.TestCase):
    def test_all_native_provider_commands_are_literal_and_architecture_scoped(self):
        for target in ("debian13", "fedora44", "arch"):
            argv = provider_command(target, "libasound.so.2")
            self.assertNotIn("sh", argv)
            with self.assertRaises(UnifierError):
                provider_command(target, "libasound.so.2;touch /tmp/pwn")
        self.assertEqual(provider_names("arch", "\0".join(["extra", "alsa-lib", "1.2.14", "usr/lib/libasound.so.2"]) + "\n"), ["alsa-lib"])
        self.assertEqual(provider_names("fedora44", "alsa-lib\nalsa-lib\n"), ["alsa-lib"])

    def test_partial_scan_wrong_abi_and_untrusted_provider_output_fail(self):
        for output in (PLUGIN.replace("UNIFIER_SCAN_DONE", ""), PLUGIN.replace("ELF64", "ELF32")):
            with self.assertRaises(UnifierError):
                parse_scan(output)
        with self.assertRaises(UnifierError):
            provider_names("debian13", "-o APT::Get::AllowUnauthenticated=true")

    def test_scan_requires_exact_framing_and_complete_abi_fields(self):
        for output in ("unexpected output UNIFIER_SCAN_DONE\n",
                       "UNIFIER_SCAN_DONE\nUNIFIER_SCAN_DONE\n",
                       PLUGIN.replace("ELF64", "ELF640"),
                       PLUGIN.replace("X86-64", "X86-64-corrupt")):
            with self.subTest(output=output), self.assertRaises(UnifierError):
                parse_scan(output)

    def test_native_questions_and_noninteractive_modes_remain_distinct(self):
        self.assertEqual(package_command(["pacman", "-Syu", "--noconfirm", "foo"], interactive=True),
                         ["pacman", "-Syu", "foo"])
        self.assertEqual(package_environment("debian13", interactive=False)["DEBIAN_FRONTEND"], "noninteractive")
        self.assertEqual(package_environment("debian13", interactive=True)["DEBIAN_FRONTEND"], "readline")

    def test_arch_plugin_paths_keep_spaces(self):
        self.assertEqual(parse_package_files("vendor", "vendor /opt/vendor app/libframe.so\n"),
                         ["/opt/vendor app/libframe.so"])

    def test_apt_contents_paths_are_relative_before_regex_wrapping(self):
        # apt-file strips a leading ^/ itself. ^/? would leave an invalid
        # conditional group after its regexp rewrite (observed on Debian 13).
        pattern = provider_command("debian13", "libasound.so.2")[-1]
        self.assertFalse(pattern.startswith("^/"))
        import re
        self.assertIsNotNone(re.fullmatch(pattern, "usr/lib/x86_64-linux-gnu/libasound.so.2"))
        self.assertIsNone(re.fullmatch(pattern, "opt/vendor/libasound.so.2"))


if __name__ == "__main__":
    unittest.main()
