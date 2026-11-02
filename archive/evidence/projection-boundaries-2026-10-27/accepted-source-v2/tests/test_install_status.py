"""Offline contracts for layered install status.

A native package tool can exit 0, add the package to its database and still
report broken post-transaction hooks on stdout or stderr. The transaction keeps
`phase` (installed or failed) and adds `maintenance` findings plus a
top-level `verdict` (verified, verified-with-warnings, failed).

The Arch fragments below are quoted verbatim from
evidence/arch-complex-2026-10-22T202359+0800/sunshine-install-ustc.json and
install.json. The tests never read or write the evidence directory.
"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
for _path in (ROOT / "src", ROOT, ROOT / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from multilinux import cli
from multilinux.packages import (
    MAINTENANCE_LINE_LIMIT,
    maintenance_findings,
    merge_maintenance_findings,
    transaction_verdict,
)
from test_packages import (
    FlipHost,
    PackageBoxes,
    _run_cli,
    make_env,
    package_service,
)


SUNSHINE_STDOUT = """input26: Failed to write 'change' to '/sys/devices/pci0000:00/0000:00:1f.3/skl_hda_dsp_generic/sound/card0/input26/uevent': Permission denied
event12: Failed to write 'change' to '/sys/devices/pci0000:00/0000:00:1f.3/skl_hda_dsp_generic/sound/card0/input26/event12/uevent': Permission denied
input27: Failed to write 'change' to '/sys/devices/pci0000:00/0000:00:1f.3/skl_hda_dsp_generic/sound/card0/input27/uevent': Permission denied
modprobe: FATAL: Module uinput not found in directory /lib/modules/7.2.11-200.fc44.x86_64
modprobe: FATAL: Module uhid not found in directory /lib/modules/7.2.11-200.fc44.x86_64
modprobe: FATAL: Module uhid not found in directory /lib/modules/7.2.11-200.fc44.x86_64
Optional dependencies for sunshine
    cuda: Nvidia GPU encoding support
    libva-mesa-driver: AMD GPU encoding support [installed]
    xorg-server-xvfb: Virtual X server for headless testing
:: Running post-transaction hooks...
( 1/10) Reloading user manager configuration...
  Skipped: Current root is not booted.
( 2/10) Reloading device manager configuration...
  Skipped: Current root is not booted.
( 3/10) Arming ConditionNeedsUpdate...
( 4/10) Loading new kernel modules...
System has not been booted with systemd as init system (PID 1). Can't operate.
Failed to connect to system scope bus via local transport: Host is down
( 5/10) Updating fontconfig cache...
( 6/10) Updating GIO module cache...
( 7/10) Compiling GSettings XML schema files...
( 8/10) Updating GTK 3 input method module cache...
( 9/10) Updating icon theme caches...
(10/10) Updating the desktop file MIME type cache...
"""

SUNSHINE_STDERR = """error: command failed to execute correctly
error: command failed to execute correctly
"""

# A successful Arch install (install.json): hook output is noisy but clean.
NORMAL_ARCH_STDOUT = """upgrading systemd...
Failed to connect to audit log, ignoring: Invalid argument
Creating group 'avahi' with GID 969.
upgrading pacman...
:: Running post-transaction hooks...
( 1/15) Creating system user accounts...
Failed to connect to audit log, ignoring: Invalid argument
( 2/15) Creating temporary files...
/usr/lib/tmpfiles.d/journal-nocow.conf:26: Failed to resolve specifier: uninitialized /etc/ detected, skipping.
All rules containing unresolvable specifiers will be skipped.
( 5/15) Applying kernel sysctl settings...
  Skipped: Current root is not booted.
( 6/15) Reloading system manager configuration...
  Skipped: Current root is not booted.
(11/15) Arming ConditionNeedsUpdate...
(15/15) Updating fontconfig cache...
"""

NORMAL_ARCH_STDERR = (
    "error: failed retrieving file 'mesa-1:26.2.5-1-x86_64.pkg.tar.zst' from "
    "fastly.mirror.pkgbuild.com : Operation too slow. Less than 1 bytes/sec "
    "transferred the last 10 seconds\n")

NORMAL_DEBIAN_STDOUT = """Reading package lists...
Building dependency tree...
Setting up hello (2.10-3) ...
Processing triggers for libc-bin (2.41-12) ...
"""

NORMAL_DEBIAN_STDERR = "invoke-rc.d: policy-rc.d denied execution of start.\n"

NORMAL_FEDORA_STDOUT = """Last metadata expiration check: 0:00:01 ago.
Installed:
  hello-1.0-1.fc44.x86_64
Complete!
"""

FEDORA_SCRIPTLET_STDERR = (
    "warning: %post(hello-1-1.x86_64) scriptlet failed, exit status 1\n")
FEDORA_DNF_SCRIPTLET_STDOUT = (
    "Error in POSTIN scriptlet in rpm package hello-1-1.x86_64\n")

SUNSHINE_EXPECTED_REASONS = {
    "pacman-hook-failed",
    "modprobe-fatal",
    "systemd-not-pid1",
    "systemd-system-bus-unavailable",
    "udev-uevent-write-failed",
}


class MaintenanceFindings(unittest.TestCase):
    def test_sunshine_output_becomes_one_item_per_hook_failure(self):
        items = maintenance_findings("arch", SUNSHINE_STDOUT, SUNSHINE_STDERR)
        by_reason = {item["reason"]: item for item in items}
        self.assertEqual(set(by_reason), SUNSHINE_EXPECTED_REASONS)
        self.assertEqual(len(items), len(SUNSHINE_EXPECTED_REASONS))
        pacman = by_reason["pacman-hook-failed"]
        self.assertEqual(pacman["source"], "stderr")
        self.assertEqual(pacman["tool"], "pacman")
        self.assertEqual(pacman["count"], 2)
        self.assertEqual(pacman["line"], "error: command failed to execute correctly")
        modprobe = by_reason["modprobe-fatal"]
        self.assertEqual(modprobe["source"], "stdout")
        self.assertEqual(modprobe["tool"], "modprobe")
        self.assertEqual(modprobe["count"], 3)
        self.assertIn("modprobe: FATAL: Module uinput not found", modprobe["line"])
        udev = by_reason["udev-uevent-write-failed"]
        self.assertEqual(udev["tool"], "udev")
        self.assertEqual(udev["count"], 3)
        self.assertIn("/uevent", udev["line"])
        self.assertEqual(by_reason["systemd-not-pid1"]["tool"], "systemd")
        self.assertIn("PID 1", by_reason["systemd-not-pid1"]["line"])
        self.assertEqual(
            by_reason["systemd-system-bus-unavailable"]["tool"], "systemd")
        for item in items:
            self.assertIn(item["source"], {"stdout", "stderr"})
            self.assertIn(item["tool"], {"pacman", "rpm", "modprobe", "systemd", "udev"})
            self.assertLessEqual(len(item["line"]), MAINTENANCE_LINE_LIMIT + 3)

    def test_normal_arch_install_reports_no_hook_failure(self):
        self.assertEqual(
            maintenance_findings("arch", NORMAL_ARCH_STDOUT, NORMAL_ARCH_STDERR), [])
        stdout_only = maintenance_findings("arch", SUNSHINE_STDOUT, "")
        self.assertTrue(stdout_only)
        self.assertTrue(all(item["source"] == "stdout" for item in stdout_only))
        self.assertNotIn("pacman-hook-failed",
                         {item["reason"] for item in stdout_only})

    def test_skips_generic_errors_and_container_conditions(self):
        text = "\n".join([
            "error: failed retrieving file 'x.pkg.tar.zst' from mirror: Operation too slow",
            "error: target not found: nope",
            "warning: too many errors from mirror, skipping for the remainder",
            "  Skipped: Current root is not booted.",
            "Failed to connect to audit log, ignoring: Invalid argument",
            "All rules containing unresolvable specifiers will be skipped.",
            "Failed to resolve specifier: uninitialized /etc/ detected, skipping.",
        ])
        self.assertEqual(maintenance_findings("arch", text, text), [])
        self.assertEqual(maintenance_findings("debian13", NORMAL_DEBIAN_STDOUT,
                                             NORMAL_DEBIAN_STDERR), [])
        self.assertEqual(maintenance_findings("fedora44", NORMAL_FEDORA_STDOUT, ""), [])
        self.assertEqual(maintenance_findings("arch", "", ""), [])
        self.assertEqual(maintenance_findings("gentoo", SUNSHINE_STDOUT, SUNSHINE_STDERR), [])

    def test_rpm_scriptlet_failures_are_fedora_only_and_case_insensitive(self):
        for stderr in (FEDORA_SCRIPTLET_STDERR,
                       "error: %pre(hello-1-1.x86_64) scriptlet failed, exit status 1\n"):
            items = maintenance_findings("fedora44", "", stderr)
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["reason"], "rpm-scriptlet-failed")
            self.assertEqual(items[0]["tool"], "rpm")
            self.assertEqual(items[0]["source"], "stderr")
        dnf_items = maintenance_findings("fedora44", FEDORA_DNF_SCRIPTLET_STDOUT, "")
        self.assertEqual([item["reason"] for item in dnf_items], ["rpm-scriptlet-failed"])
        self.assertEqual(dnf_items[0]["source"], "stdout")
        self.assertEqual(maintenance_findings("arch", "", FEDORA_SCRIPTLET_STDERR), [])
        self.assertEqual(maintenance_findings("debian13", "", FEDORA_SCRIPTLET_STDERR), [])

    def test_long_raw_lines_are_truncated_but_keep_the_marker(self):
        line = ("input9: Failed to write 'change' to '/sys/devices/"
                + "a" * 600 + "/uevent': Permission denied")
        items = maintenance_findings("arch", line, "")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["line"].endswith("..."))
        self.assertLessEqual(len(items[0]["line"]), MAINTENANCE_LINE_LIMIT + 3)
        self.assertIn("Failed to write", items[0]["line"])

    def test_merge_keeps_one_item_per_mode_and_sums_counts(self):
        first = maintenance_findings("arch", "", SUNSHINE_STDERR)
        merged = merge_maintenance_findings(
            first, maintenance_findings("arch", "", SUNSHINE_STDERR))
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0]["reason"], "pacman-hook-failed")
        self.assertEqual(merged[0]["count"], 4)
        self.assertEqual(merge_maintenance_findings([], []), [])
        self.assertEqual(merge_maintenance_findings(None, None), [])


class Verdict(unittest.TestCase):
    def test_verdict_layers_the_phase_and_the_maintenance_status(self):
        warnings = {"status": "warnings", "items": [{"reason": "pacman-hook-failed"}]}
        self.assertEqual(transaction_verdict("completed", {"status": "ok", "items": []}),
                         "verified")
        self.assertEqual(transaction_verdict("completed", warnings),
                         "verified-with-warnings")
        self.assertEqual(transaction_verdict("failed", warnings), "failed")
        self.assertEqual(transaction_verdict("completed", {"status": "warnings"}),
                         "verified-with-warnings")
        self.assertEqual(transaction_verdict("completed", None), "verified")
        self.assertEqual(transaction_verdict("failed", None), "failed")
        self.assertEqual(transaction_verdict(None, None), "failed")


class InstallStatusTransactions(unittest.TestCase):
    def test_arch_hook_failures_keep_the_package_but_change_the_verdict(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            runtime.package_stdout = SUNSHINE_STDOUT
            runtime.package_stderr = SUNSHINE_STDERR
            make_env(service, runtime, "arch", "box")
            txn = service.install_packages("box", ["sunshine"])
            self.assertEqual(txn["phase"], "completed")
            self.assertIsNone(txn["failure_phase"])
            self.assertEqual(txn["verdict"], "verified-with-warnings")
            self.assertEqual(txn["maintenance"]["status"], "warnings")
            self.assertEqual(txn["maintenance"]["system_integration"], "unverified")
            self.assertFalse(txn["maintenance"]["full_init"])
            self.assertTrue(txn["verification"]["ok"])
            self.assertEqual(txn["verification"]["packages"], {"sunshine": True})
            self.assertEqual(
                {item["reason"] for item in txn["maintenance"]["items"]},
                SUNSHINE_EXPECTED_REASONS)
            self.assertTrue(service.query_package("box", "sunshine")["present"])

    def test_local_arch_install_matches_the_evidence_transaction(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            runtime.package_stdout = SUNSHINE_STDOUT
            runtime.package_stderr = SUNSHINE_STDERR
            make_env(service, runtime, "arch", "box")
            package = root / "sunshine-2026.914.233613-1-x86_64.pkg.tar.zst"
            package.write_bytes(b"fake-arch-package")
            txn = service.install_local("box", str(package))
            self.assertEqual(txn["operation"], "install-local")
            self.assertEqual(txn["native"][-1]["argv"][:2], ["pacman", "-U"])
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual(txn["verdict"], "verified-with-warnings")
            self.assertEqual(
                {item["reason"] for item in txn["maintenance"]["items"]},
                SUNSHINE_EXPECTED_REASONS)
            self.assertEqual(
                runtime.copies[0][1], "/tmp/ml-incoming/" + package.name)

    def test_detection_uses_the_full_output_not_the_saved_tail(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            runtime.package_stdout = (
                "modprobe: FATAL: Module uinput not found in directory /lib/modules/x\n"
                + "padding\n" * 800)
            make_env(service, runtime, "arch", "box")
            txn = service.install_packages("box", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual(txn["verdict"], "verified-with-warnings")
            self.assertNotIn("modprobe: FATAL", txn["native"][-1]["stdout_tail"])
            self.assertEqual(
                [item["reason"] for item in txn["maintenance"]["items"]],
                ["modprobe-fatal"])

    def test_quiet_install_is_verified_with_no_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            runtime.package_stdout = NORMAL_DEBIAN_STDOUT
            runtime.package_stderr = NORMAL_DEBIAN_STDERR
            make_env(service, runtime, "debian13", "deb")
            txn = service.install_packages("deb", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual(txn["verdict"], "verified")
            self.assertEqual(txn["maintenance"], {
                "status": "ok", "items": [], "system_integration": "unverified",
                "full_init": False,
            })

    def test_native_command_failure_is_failed_not_a_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            txn = service.install_packages("deb", ["missing-pkg"])
            self.assertEqual(txn["phase"], "failed")
            self.assertEqual(txn["failure_phase"], "native-command")
            self.assertEqual(txn["verdict"], "failed")
            self.assertEqual(txn["maintenance"]["items"], [])

    def test_isolation_failure_is_failed_even_with_quiet_hooks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root, observer=FlipHost())
            make_env(service, runtime, "debian13", "deb")
            txn = service.install_packages("deb", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["phase"], "failed")
            self.assertEqual(txn["failure_phase"], "verification")
            self.assertEqual(txn["verdict"], "failed")
            self.assertEqual(txn["maintenance"]["status"], "ok")
            self.assertTrue(service.query_package("deb", "hello")["present"])

    def test_fedora_scriptlet_failure_is_a_warning_on_an_installed_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            runtime.package_stderr = FEDORA_SCRIPTLET_STDERR
            make_env(service, runtime, "fedora44", "fed")
            txn = service.install_packages("fed", ["hello"], use_argv=["hello"])
            self.assertEqual(txn["phase"], "completed")
            self.assertEqual(txn["verdict"], "verified-with-warnings")
            self.assertEqual(
                [item["reason"] for item in txn["maintenance"]["items"]],
                ["rpm-scriptlet-failed"])
            self.assertTrue(txn["verification"]["ok"])
            self.assertTrue(service.query_package("fed", "hello")["present"])


class InstallStatusCommandLine(unittest.TestCase):
    def test_warning_is_one_stderr_line_with_exit_code_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            runtime.package_stdout = SUNSHINE_STDOUT
            runtime.package_stderr = SUNSHINE_STDERR
            make_env(service, runtime, "arch", "box")
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                code, out, err = _run_cli(["pkg", "install", "box", "sunshine"])
                log_code, log_out, _log_err = _run_cli(["pkg", "log", "box"])
                txn_id = json.loads(log_out)[0]["id"]
                show_code, show_out, _show_err = _run_cli(["pkg", "show", txn_id])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 0)
            self.assertEqual(len(err.splitlines()), 1)
            self.assertIn("warning", err)
            self.assertIn("maintenance hook", err)
            self.assertIn("pacman-hook-failed", err)
            self.assertIn("exit code 0", err)
            data = json.loads(out)
            self.assertEqual(data["phase"], "completed")
            self.assertEqual(data["verdict"], "verified-with-warnings")
            self.assertEqual(data["maintenance"]["status"], "warnings")
            self.assertEqual(log_code, 0)
            row = json.loads(log_out)[0]
            self.assertEqual(row["verdict"], "verified-with-warnings")
            self.assertEqual(row["maintenance"], "warnings")
            self.assertEqual(show_code, 0)
            self.assertEqual(json.loads(show_out)["verdict"],
                             "verified-with-warnings")

    def test_verified_install_keeps_the_output_and_stderr_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                code, out, err = _run_cli(
                    ["pkg", "install", "deb", "hello", "--use-command", "hello"])
                log_code, log_out, _log_err = _run_cli(["pkg", "log", "deb"])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 0)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["verdict"], "verified")
            self.assertEqual(json.loads(out)["phase"], "completed")
            row = json.loads(log_out)[0]
            self.assertEqual(row["verdict"], "verified")
            self.assertEqual(row["maintenance"], "ok")

    def test_failed_transaction_stays_failed_and_writes_no_warning(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                code, out, err = _run_cli(["pkg", "install", "deb", "missing-pkg"])
                log_code, log_out, _log_err = _run_cli(["pkg", "log", "deb"])
            finally:
                cli.service_factory = original
            self.assertEqual(code, 1)
            self.assertEqual(err, "")
            self.assertEqual(json.loads(out)["verdict"], "failed")
            row = json.loads(log_out)[0]
            self.assertEqual(row["verdict"], "failed")

    def test_legacy_transaction_without_new_fields_stays_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime = package_service(root)
            make_env(service, runtime, "debian13", "deb")
            txn_id = "txn0123456789ab"
            path = service.store.transaction_path(txn_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({
                "schema_version": 1,
                "id": txn_id,
                "environment_id": "deb",
                "operation": "install",
                "packages": ["hello"],
                "native": [],
                "native_error": None,
                "verification": {"ok": True, "problems": []},
                "phase": "completed",
                "failure_phase": None,
                "started_at": "2026-10-01T00:00:00+08:00",
                "finished_at": "2026-10-01T00:00:01+08:00",
            }) + "\n", encoding="utf-8")
            shown = service.show_transaction(txn_id)
            self.assertNotIn("maintenance", shown)
            self.assertNotIn("verdict", shown)
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                show_code, show_out, show_err = _run_cli(["pkg", "show", txn_id])
                log_code, log_out, _log_err = _run_cli(["pkg", "log", "deb"])
            finally:
                cli.service_factory = original
            self.assertEqual(show_code, 0)
            self.assertEqual(show_err, "")
            self.assertEqual(json.loads(show_out)["phase"], "completed")
            self.assertEqual(log_code, 0)
            row = json.loads(log_out)[0]
            self.assertEqual(row["id"], txn_id)
            self.assertEqual(row["verdict"], "verified")
            self.assertIsNone(row["maintenance"])


if __name__ == "__main__":
    unittest.main()
