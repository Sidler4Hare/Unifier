"""Whitelist admission is independent of guest identity and runtime acceptance."""

import contextlib
import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_env import CONFIG, PROFILES, service_in
from multilinux import cli
from multilinux.compatibility import (
    FAMILIES, classify_host, compatibility_mode, observe_host, relationship_plan,
)
from multilinux.errors import MultilinuxError
from tools import lab


def host(distro="ubuntu", **changes):
    facts = {
        "os_release": {"ID": distro, "VERSION_ID": "24.04"},
        "system": "Linux", "machine": "x86_64", "libc": "glibc",
        "ostree": False, "transactional": False,
    }
    facts.update(changes)
    return facts


class FamilyWhitelist(unittest.TestCase):
    def test_all_explicit_hosts_have_three_separate_guest_relationships(self):
        seen = set()
        for family, group in FAMILIES.items():
            for distro in group["ids"]:
                with self.subTest(distro=distro):
                    self.assertNotIn(distro, seen)
                    seen.add(distro)
                    profile = classify_host(host(distro))
                    self.assertEqual(profile["family"], family)
                    self.assertEqual(profile["admitted"], group["host_admitted"])
                    for target in CONFIG["targets"]:
                        plan = relationship_plan(host(distro), target)
                        self.assertEqual(plan["allowed"], group["host_admitted"])
                        self.assertEqual(plan["execution_domain"], "isolated-container")
                        self.assertFalse(plan["share_system_trees"])
                        self.assertFalse(plan["share_package_databases"])
                        self.assertEqual(plan["validation_scope"], "policy-only")

    def test_ubuntu_and_debian_share_host_strategy_without_becoming_the_same_os(self):
        ubuntu = relationship_plan(host("ubuntu"), "debian13")
        debian = relationship_plan(host("debian"), "debian13")
        self.assertEqual(ubuntu["host"]["reference_host"], debian["host"]["reference_host"])
        self.assertEqual(ubuntu["relationship"], "same-family")
        self.assertEqual(debian["relationship"], "same-distribution")
        self.assertEqual(ubuntu["host"]["id"], "ubuntu")
        self.assertEqual(ubuntu["container_contract"]["family"], "debian")
        self.assertEqual(ubuntu["native_package_validation"], "required-even-within-family")

    def test_rpm_families_keep_distinct_drivers_and_profiles(self):
        profiles = [classify_host(host(distro)) for distro in ("fedora", "rocky", "opensuse-leap")]
        self.assertEqual([p["family"] for p in profiles], ["fedora", "rhel", "suse"])
        self.assertEqual([p["package_driver"] for p in profiles], ["dnf", "dnf", "zypper"])
        self.assertTrue(all(p["package_format"] == "rpm" for p in profiles))
        self.assertEqual(relationship_plan(host("rocky"), "fedora44")["relationship"], "cross-family")

    def test_id_like_does_not_grant_admission_or_override_exact_id(self):
        for distro in ("unknown", "ubuntuish", "", "alpine", "nixos"):
            with self.subTest(distro=distro):
                facts = host(os_release={"ID": distro, "ID_LIKE": "ubuntu debian fedora arch"})
                self.assertFalse(classify_host(facts)["admitted"])
        facts = host(os_release={"ID": "manjaro", "ID_LIKE": "debian"})
        self.assertEqual(classify_host(facts)["family"], "arch")

    def test_atomic_variants_and_live_markers_override_fedora_or_suse_id(self):
        for variant in ("silverblue", "kinoite", "coreos", "iot", "cosmic-atomic", "future-atomic"):
            with self.subTest(variant=variant):
                facts = host(os_release={"ID": "fedora", "VARIANT_ID": variant})
                self.assertFalse(classify_host(facts)["admitted"])
                self.assertTrue(classify_host(facts)["atomic"])
        for marker in ("ostree", "transactional"):
            self.assertFalse(classify_host(host("opensuse-tumbleweed", **{marker: True}))["admitted"])
        self.assertTrue(classify_host(host(os_release={"ID": "fedora", "VARIANT_ID": "kde"}))["admitted"])

    def test_wrong_platform_or_libc_is_refused(self):
        for changes in ({"machine": "aarch64"}, {"system": "Darwin"}, {"libc": "musl"}, {"libc": ""}):
            with self.subTest(changes=changes):
                self.assertFalse(relationship_plan(host(**changes), "arch")["allowed"])

    def test_rolling_hosts_do_not_require_version_id(self):
        self.assertTrue(classify_host(host(os_release={"ID": "arch"}))["admitted"])

    def test_host_whitelist_does_not_install_a_new_container_adapter(self):
        raw = dict(relationship_plan(host(), "debian13")["container_contract"])
        plan = relationship_plan(host(), "ubuntu", raw)
        self.assertFalse(plan["allowed"])
        self.assertIn("no runnable upgrade driver", plan["reasons"][-1])

    def test_malformed_release_facts_are_rejected(self):
        for facts in ({}, {"os_release": []}, host(os_release={"ID": ["ubuntu"]}),
                      host(ostree="false"), host(transactional=1)):
            with self.subTest(facts=facts), self.assertRaises(MultilinuxError):
                classify_host(facts)

    def test_mode_is_explicit_and_legacy_configs_use_the_same_whitelist(self):
        self.assertEqual(compatibility_mode({}), "whitelist")
        for section in (None, [], {"mode": "auto"}, {"mode": "off"}):
            with self.subTest(section=section), self.assertRaises(MultilinuxError):
                compatibility_mode({"compatibility": section})

    def test_release_file_is_parsed_as_data_and_marked_as_simulation(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "os-release"
            path.write_text('# test\nID=ubuntu\nID_LIKE="debian"\nPRETTY_NAME="$(touch sentinel)"\n')
            with patch("multilinux.compatibility.platform.machine", return_value="x86_64"), \
                 patch("multilinux.compatibility.platform.system", return_value="Linux"), \
                 patch("multilinux.compatibility.platform.libc_ver", return_value=("glibc", "2.43")):
                facts = observe_host(path)
            self.assertEqual(facts["os_release"]["PRETTY_NAME"], "$(touch sentinel)")
            self.assertEqual(facts["observation"], "release-file-simulation")
            path.write_text('ID="unclosed\n')
            with self.assertRaises(MultilinuxError):
                observe_host(path)


class ServiceAdmission(unittest.TestCase):
    def test_application_refusal_precedes_supervisor_or_portal_bridge(self):
        from test_app_binding import ready
        from multilinux.on_demand import LaunchGroup, managed_open
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _notes = ready(Path(temporary))
            service.host_facts = host("nixos")
            mark = list(runtime.events)
            with patch("multilinux.on_demand.subprocess.Popen", side_effect=AssertionError("supervisor reached")), \
                 patch("multilinux.on_demand.start_bridge", side_effect=AssertionError("portal reached")):
                for operation in (
                    lambda: service.open_app("notes"),
                    lambda: service.exec_app("notes", ["/usr/bin/true"]),
                    lambda: managed_open(service, "notes", []),
                    lambda: LaunchGroup(service, "debian13").acquire("notes"),
                ):
                    with self.assertRaisesRegex(MultilinuxError, "whitelist refused"):
                        operation()
            self.assertEqual(runtime.events, mark)

    def test_project_session_preflight_refuses_unlisted_host(self):
        from test_project_sessions import ready
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime, _routes, project, _root, document = ready(Path(temporary))
            service.host_facts = host("nixos")
            from multilinux.project_sessions import ProjectSessions
            sessions = ProjectSessions(service.root, service)
            mark = list(runtime.events)
            with self.assertRaisesRegex(MultilinuxError, "whitelist refused"):
                sessions.prepare(project["project_uuid"], cwd=str(document.parent), argv=["true"])
            self.assertEqual(runtime.events, mark)

    def test_refused_host_has_no_create_or_runtime_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.host_facts = host(os_release={"ID": "unlisted", "ID_LIKE": "debian"})
            with patch.object(runtime, "inspect", side_effect=AssertionError("runtime reached")):
                with self.assertRaisesRegex(MultilinuxError, "whitelist refused"):
                    service.create("debian13", None, [])
            self.assertEqual(runtime.events, [])
            self.assertFalse(service.store.exists("debian13"))
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_ubuntu_creation_records_relationship_but_preserves_guest_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.host_facts = host()
            record = service.create("debian13", None, [])
            self.assertEqual(record["distribution_id"], "debian")
            self.assertEqual(record["version_track"], "13")
            self.assertEqual(record["system"]["package_driver"], "apt")
            self.assertEqual(record["compatibility_at_creation"]["relationship"], "same-family")
            self.assertEqual(service.inspect("debian13")["compatibility"]["host"]["id"], "ubuntu")
            self.assertEqual(service.list_summaries()[0]["host_relationship"], "same-family")

    def test_same_family_does_not_relax_locked_guest_os_validation(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.host_facts = host()
            runtime.identities["debian13"] = {**PROFILES["debian13"], "ID": "ubuntu"}
            with self.assertRaisesRegex(MultilinuxError, "reported distribution"):
                service.create("debian13", None, [])
            self.assertFalse(service.store.exists("debian13"))
            self.assertEqual(runtime.containers, {})

    def test_current_host_is_rechecked_and_saved_admission_cannot_override_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.create("debian13", None, [], network="pasta")
            service.start("debian13")
            saved = copy.deepcopy(service.store.load("debian13"))
            mark = list(runtime.events)
            service.host_facts = host("nixos")
            operations = (
                lambda: service.start("debian13"),
                lambda: service.run("debian13", ["true"], workdir=None, env_keys=[]),
                lambda: service.query_package("debian13", "hello"),
                lambda: service.install_packages("debian13", ["hello"]),
                lambda: service.update_packages("debian13"),
                lambda: service.remove_packages("debian13", ["hello"]),
            )
            for operation in operations:
                with self.assertRaisesRegex(MultilinuxError, "whitelist refused"):
                    operation()
            self.assertEqual(runtime.events, mark)
            self.assertEqual(service.store.load("debian13"), saved)
            self.assertEqual(service.transactions_for("debian13"), [])
            self.assertFalse(service.inspect("debian13")["compatibility"]["allowed"])
            self.assertEqual(service.change_plan("debian13")["action"], "refused")
            service.stop("debian13")
            service.remove("debian13")
            self.assertFalse(service.store.exists("debian13"))

    def test_old_record_without_admission_snapshot_still_uses_current_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _ = service_in(Path(temporary))
            record = service.create("arch", None, [])
            record.pop("compatibility_at_creation")
            service.store.save(record, None)
            service.host_facts = host("manjaro")
            self.assertEqual(service.change_plan("arch")["compatibility"]["relationship"], "same-family")
            service.start("arch")
            self.assertNotIn("compatibility_at_creation", service.store.load("arch"))


class CompatibilityCLI(unittest.TestCase):
    def test_commands_are_read_only_and_refusals_return_nonzero(self):
        for distro, code in (("ubuntu", 0), ("unknown", 1)):
            for args in (("compat", "check", "--target", "debian13"), ("compat", "matrix")):
                with self.subTest(distro=distro, args=args), \
                     patch("multilinux.compatibility.observe_host", return_value=host(distro)), \
                     patch.object(cli, "service_factory", side_effect=AssertionError("runtime factory reached")), \
                     contextlib.redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(cli.main(list(args)), code)
                    result = json.loads(output.getvalue())
                    plans = result if isinstance(result, list) else [result]
                    self.assertTrue(all(plan["allowed"] == (code == 0) for plan in plans))
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(cli.main(["compat", "list"]), 0)
        self.assertFalse(json.loads(output.getvalue())["id_like_grants_admission"])

    def test_doctor_reports_whitelist_refusal_even_when_runtime_tools_are_absent(self):
        with patch("multilinux.compatibility.observe_host", return_value=host("unknown")), \
             patch.object(lab.shutil, "which", return_value=None), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(lab.doctor(type("Args", (), {"output": None})()), 1)
        result = json.loads(output.getvalue())
        self.assertFalse(result["ready_for_image_preparation"])
        check = next(item for item in result["checks"] if item["name"] == "host-whitelist")
        self.assertFalse(check["ok"])
        self.assertTrue(all(not plan["allowed"] for plan in result["compatibility"].values()))


if __name__ == "__main__":
    unittest.main()
