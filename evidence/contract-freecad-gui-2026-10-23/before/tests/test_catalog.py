"""Catalog contract: in-place package upgrades, replacement for frozen identity."""

from pathlib import Path
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
                "update_policy": "generation-switch",
                "package_driver": "nix",
            },
        }
        refused = plan_change(record, operation="package-change")
        self.assertEqual(refused["action"], "refused")
        self.assertIn("generation-switch", refused["reason"])
        image = dict(record)
        image["system"] = {"update_policy": "image-replace", "package_driver": "android-apk"}
        replaced = plan_change(image, operation="package-change")
        self.assertEqual(replaced["action"], "replace")
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
