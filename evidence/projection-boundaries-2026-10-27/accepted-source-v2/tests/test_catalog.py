"""Catalog contract: in-place package upgrades, replacement for frozen identity."""

from pathlib import Path
import copy
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from multilinux.catalog import (
    IMPLEMENTED_DRIVERS,
    PREPARED,
    describe,
    plan_change,
    require_runnable,
    resolve_record_contract,
    system_contract,
)
from multilinux.errors import MultilinuxError
from multilinux.packages import TARGETS, driver_for
from multilinux.routing import route_package
from test_env import service_in
from tools import lab


OPENSUSE = {
    "family": "suse",
    "libc": "glibc",
    "layout": "fhs",
    "package_driver": "zypper",
    "package_format": "rpm",
    "runtime_backend": "podman-rootless",
    "update_policy": "versioned-release",
}


class CatalogContract(unittest.TestCase):
    def test_prepared_toml_matches_the_catalog(self):
        config = lab.targets()
        self.assertEqual(set(TARGETS), set(PREPARED))
        for name, expected in PREPARED.items():
            for key, value in expected.items():
                self.assertEqual(config["targets"][name][key], value)
            self.assertIn(driver_for(name).name, IMPLEMENTED_DRIVERS)
            self.assertEqual(driver_for(name).name, expected["package_driver"])

    def test_prepared_target_rejects_a_different_libc(self):
        with self.assertRaisesRegex(MultilinuxError, "fixed at glibc"):
            describe("debian13", {"libc": "musl"})

    def test_unnamed_target_must_carry_the_whole_contract(self):
        with self.assertRaisesRegex(MultilinuxError, "missing catalog fields"):
            describe("alpine", {})
        spec = describe("opensuse-leap", OPENSUSE)
        self.assertEqual(spec["package_driver"], "zypper")
        with self.assertRaisesRegex(MultilinuxError, "no runnable upgrade driver"):
            require_runnable("opensuse-leap", spec)

    def test_create_stores_the_runnable_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            self.assertEqual(record["system"]["package_driver"], "apt")
            self.assertEqual(record["system"]["package_change"], "in-place")
            self.assertEqual(record["system"]["base_image_change"], "replace")
            self.assertEqual(record["system"], system_contract("debian13", service.config["targets"]["debian13"]))
            plan = service.change_plan(record["id"])
            self.assertEqual(plan["action"], "unchanged")
            self.assertEqual(plan["package_change"], "in-place")
            self.assertIn("user_namespace", plan["replace_fields"])

    def test_identity_change_is_a_replacement_and_does_not_touch_the_container(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            before = list(runtime.events)
            same = service.change_plan("debian13", {"user_namespace": "rootless-default"})
            changed = service.change_plan("debian13", {"user_namespace": "keep-id"})
            desktop = service.change_plan("debian13", {"desktop": True})
            self.assertEqual(same["action"], "unchanged")
            self.assertEqual(changed["action"], "replace")
            self.assertEqual(changed["changes"], ["user_namespace"])
            self.assertEqual(desktop["action"], "replace")
            self.assertEqual(desktop["changes"], ["desktop", "ipc", "selinux_label"])
            switched = service.change_plan("debian13", {"target": "arch"})
            self.assertEqual(switched["action"], "replace")
            self.assertIn("target", switched["changes"])
            self.assertIn("package_driver", switched["changes"])
            self.assertEqual(switched["changes"].count("image_digest"), 1)
            self.assertEqual(runtime.events, before)
            self.assertEqual(service.inspect("debian13")["id"], record["id"])

    def test_reserved_package_policy_is_refused_before_any_command(self):
        record = {
            "id": "nix",
            "target": "nixos",
            "system": {
                "family": "nixos", "libc": "glibc", "layout": "nix-store",
                "package_format": "nix", "runtime_backend": "podman-rootless",
                "update_policy": "generation-switch",
                "package_driver": "nix",
            },
        }
        refused = plan_change(record, operation="package-change")
        self.assertEqual(refused["action"], "refused")
        self.assertIn("generation-switch", refused["reason"])
        image = dict(record)
        image["system"] = {**record["system"], "update_policy": "image-replace", "package_driver": "android-apk"}
        replaced = plan_change(image, operation="package-change")
        self.assertEqual(replaced["action"], "refused")
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            created = service.create("debian13", None, [])
            created["system"]["update_policy"] = "generation-switch"
            created["system"]["package_driver"] = "nix"
            service.store.save(created, None)
            mark = len(runtime.events)
            with self.assertRaisesRegex(MultilinuxError, "generation-switch"):
                service.install_packages("debian13", ["hello"])
            self.assertEqual(runtime.events[mark:], [])

    def test_new_name_with_implemented_driver_is_rejected_before_create(self):
        raw = dict(PREPARED["debian13"])
        with self.assertRaisesRegex(MultilinuxError, "no runnable upgrade driver"):
            require_runnable("future-debian", raw)
        with self.assertRaises(MultilinuxError):
            driver_for("future-debian")
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.config = copy.deepcopy(service.config)
            service.config["targets"]["future-debian"] = {
                **service.config["targets"]["debian13"], **raw}
            service.lock = copy.deepcopy(service.lock)
            service.lock["targets"]["future-debian"] = service.lock["targets"]["debian13"]
            with self.assertRaisesRegex(MultilinuxError, "no runnable upgrade driver"):
                service.create("future-debian", None, [])
            self.assertEqual(runtime.events, [])
            self.assertFalse(service.store.exists("future-debian"))

    def test_create_rejects_conflicting_prepared_contract_before_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, runtime = service_in(Path(temporary))
            service.config = copy.deepcopy(service.config)
            service.config["targets"]["debian13"]["package_driver"] = "dnf"
            with self.assertRaisesRegex(MultilinuxError, "package_driver"):
                service.create("debian13", None, [])
            self.assertEqual(runtime.events, [])

    def test_conflicting_record_is_refused_by_plan_driver_and_all_package_operations(self):
        for field, value in (("package_driver", "dnf"), ("package_format", "rpm"),
                             ("libc", "musl"), ("runtime_backend", "android")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as temporary:
                service, runtime = service_in(Path(temporary))
                record = service.create("debian13", None, [], network="pasta")
                record["system"][field] = value
                service.store.save(record, None)
                before = list(runtime.events)
                self.assertEqual(service.change_plan("debian13")["action"], "refused")
                with self.assertRaises(MultilinuxError):
                    driver_for(record)
                with self.assertRaises(MultilinuxError):
                    service.query_package("debian13", "hello")
                for operation in ("install", "update", "remove", "install-local"):
                    with self.subTest(operation=operation), self.assertRaises(MultilinuxError):
                        service._package_transaction("debian13", operation, ["hello"])
                self.assertEqual(runtime.events, before)
                self.assertEqual(service.transactions_for("debian13"), [])

    def test_legacy_records_agree_with_actual_three_driver_transactions(self):
        from test_packages import PackageBoxes, StaticHost
        for target in PREPARED:
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                runtime = PackageBoxes()
                service, _ = service_in(Path(temporary), runtime)
                service.host_observer = StaticHost()
                record = service.create(target, None, [], network="pasta")
                record.pop("system")
                service.store.save(record, None)
                plan = plan_change(record, operation="package-change")
                self.assertEqual(plan["package_driver"], driver_for(record).name)
                self.assertEqual(resolve_record_contract(record), PREPARED[target])
                txn = service.install_packages(target, ["hello"])
                self.assertEqual(txn["phase"], "completed", txn)
                expected = {"apt": "apt-get", "dnf": "dnf", "pacman": "pacman"}[plan["package_driver"]]
                commands = [step["argv"][0] for step in txn["native"]]
                self.assertIn(expected, commands)
                self.assertFalse((set(commands) & {"apt-get", "dnf", "pacman"}) - {expected})

    def test_malformed_system_is_not_treated_as_a_legacy_record(self):
        for system in (None, {}, [], {"package_driver": "apt"}):
            record = {"target": "debian13", "system": system}
            with self.subTest(system=system):
                self.assertEqual(plan_change(record, operation="package-change")["action"], "refused")
                with self.assertRaises(MultilinuxError):
                    driver_for(record)

    def test_legacy_policy_conflict_cannot_override_prepared_target(self):
        for system in (None, dict(PREPARED["debian13"])):
            record = {"target": "debian13", "update_policy": "full-system-upgrade"}
            if system is not None:
                record["system"] = system
            self.assertEqual(plan_change(record, operation="package-change")["action"], "refused")
            with self.assertRaises(MultilinuxError):
                driver_for(record)

    def test_one_rpm_format_can_match_two_targets(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "test.rpm"
            path.write_bytes(b"\xed\xab\xee\xdb")
            single = route_package(path)
            self.assertEqual(single["target"], "fedora44")
            self.assertEqual(single["candidates"], ["fedora44"])
            several = route_package(path, {"fedora44": {}, "opensuse-leap": OPENSUSE})
            self.assertIsNone(several["target"])
            self.assertEqual(several["candidates"], ["fedora44", "opensuse-leap"])


if __name__ == "__main__":
    unittest.main()
