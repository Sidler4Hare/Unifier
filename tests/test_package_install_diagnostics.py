import shlex
import unittest

from unifier.i18n import translate
from unifier.package_install_diagnostics import (
    guidance_messages, installation_guidance, native_findings,
)


class PackageInstallDiagnosticsTests(unittest.TestCase):
    def test_native_tools_report_unavailable_packages_and_dependencies(self):
        examples = {
            "debian13": (
                "E: Unable to locate package nosuch\n"
                " demo : Depends: libdemo (>= 3) but it is not installable\n"
                " Package bsdutils is not installed.\n"
                "/var/lib/dpkg/info/pkg.postinst: 24: hexdump: not found",
                ["nosuch", "libdemo", "bsdutils", "hexdump"],
            ),
            "fedora44": (
                "Failed to resolve the transaction:\nNo match for argument: missing\n"
                " - nothing provides libexample.so.0()(64bit) needed by demo-1.x86_64",
                ["missing", "libexample.so.0()(64bit)"],
            ),
            "arch": (
                "error: target not found: nonexistent\n"
                ":: unable to satisfy dependency 'example>=2' required by demo\n"
                ":: installing runtime (2.0) breaks dependency 'old-example=1' required by example",
                ["nonexistent", "example>=2", "old-example=1"],
            ),
        }
        for target, (output, names) in examples.items():
            with self.subTest(target=target):
                self.assertEqual([item["name"] for item in native_findings(target, output, output)],
                                 names)

    def test_skips_unknown_text_and_does_not_turn_a_repo_miss_into_an_install(self):
        hits = native_findings("debian13", "", "E: Unable to locate package --unsafe\n"
                              "E: Unable to locate package x;run\n"
                              "E: Unable to locate package correct")
        self.assertEqual(hits, [{"kind": "missing-package", "name": "correct"}])
        guidance = installation_guidance({
            "phase": "failed", "environment_id": "debian13", "native_dependencies": hits,
        }, "debian13")
        self.assertEqual(guidance["commands"], [{
            "kind": "lookup-package",
            "argv": ["env", "run", "debian13", "--", "apt-cache", "policy", "correct"],
        }])

    def test_hints_use_confirmed_provider_and_target_installation_root(self):
        txn = {
            "phase": "failed", "environment_id": "debian-test",
            "native_dependencies": [{"kind": "missing-dependency", "name": "python3"}],
            "dependency_repair": {"unresolved": ["libsound.so.1"],
                                  "rounds": [{"providers": {"libsound.so.1": ["sound-libs"]}}]},
        }
        advice = installation_guidance(txn, "debian13")
        root = "/home/user/My Unifier"
        messages = guidance_messages(lambda key, **kw: translate("en", key, **kw), advice, root)
        command_lines = [shlex.split(s.splitlines()[-1]) for s in messages if "\n" in s]
        self.assertIn(["env", f"UNIFIER_HOME={root}", "unifier", "pkg", "install",
                       "debian-test", "sound-libs"], command_lines)
        self.assertIn(["env", f"UNIFIER_HOME={root}", "unifier", "pkg", "install",
                       "debian-test", "python3"], command_lines)
        txn.update(phase="needs-recovery", recovery_required=True)
        blocked = installation_guidance(txn, "debian13")
        self.assertEqual([c["kind"] for c in blocked["commands"]], ["recover"])
        self.assertEqual(blocked["findings"], advice["findings"])
