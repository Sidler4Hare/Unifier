"""Rootless Podman checks for native package transactions.

Opt in with MULTILINUX_INTEGRATION=1. The run downloads repository indexes
and, on Arch, upgrades the environment. It does not pull images again.

The experimental package is `tree`. GNU hello is not in Arch's official
repositories; tree is in Debian, Fedora and Arch extra, and it is not part
of the locked base images.
"""

import json
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from unifier.errors import UnifierError
from unifier.packages import HostObserver
from podman_test_support import admitted_targets, isolated_service
from unifier.runtime import container_name, project_label
from unifier.service import now


TARGETS = (
    ("m2debian", "debian13"),
    ("m2fedora", "fedora44"),
    ("m2arch", "arch"),
)


def _reset(service, runtime, env_id):
    project = project_label(service.root)
    name = container_name(service.root, env_id)
    if service.store.exists(env_id):
        service.remove(env_id)
        return
    info = runtime.inspect(name)
    if info is not None and info.project == project and info.env_id == env_id:
        runtime.remove(name, project=project, env_id=env_id)


def _detail(txn):
    return json.dumps({
        "id": txn.get("id"),
        "operation": txn.get("operation"),
        "phase": txn.get("phase"),
        "failure_phase": txn.get("failure_phase"),
        "native_error": txn.get("native_error"),
        "verification": txn.get("verification"),
        "other_environments": txn.get("other_environments"),
        "host_packages": txn.get("host_packages"),
        "host_entries": txn.get("host_entries"),
        "native": [
            {
                "argv": step.get("argv"),
                "exit_code": step.get("exit_code"),
                "stderr": (step.get("stderr_tail") or "")[-800:],
                "stdout": (step.get("stdout_tail") or "")[-800:],
            }
            for step in txn.get("native") or []
        ],
    }, ensure_ascii=False)


def _brief(txn):
    return {
        "id": txn["id"],
        "operation": txn["operation"],
        "packages": txn.get("packages"),
        "phase": txn["phase"],
        "failure_phase": txn.get("failure_phase"),
        "native_error": txn.get("native_error"),
        "revision_before": txn.get("revision_before"),
        "revision_after": txn.get("revision_after"),
        "other_environments": txn.get("other_environments"),
        "host_packages": txn.get("host_packages"),
        "host_entries": txn.get("host_entries"),
        "verification": {
            "ok": (txn.get("verification") or {}).get("ok"),
            "packages": (txn.get("verification") or {}).get("packages"),
            "command_exit_code": (txn.get("verification") or {}).get("command_exit_code"),
            "stdout_preview": (txn.get("verification") or {}).get("stdout_preview"),
            "problems": (txn.get("verification") or {}).get("problems"),
            "isolation": (txn.get("verification") or {}).get("isolation"),
        },
        "native": [
            {
                "argv": step.get("argv"),
                "exit_code": step.get("exit_code"),
                "stderr_tail": (step.get("stderr_tail") or "")[-400:],
                "stdout_tail": (step.get("stdout_tail") or "")[-400:],
            }
            for step in txn.get("native") or []
        ],
    }


@unittest.skipUnless(os.environ.get("MULTILINUX_INTEGRATION") == "1",
                     "set MULTILINUX_INTEGRATION=1 to install packages with rootless Podman")
class PodmanPackages(unittest.TestCase):
    def test_hello_install_update_and_remove_stay_inside_one_environment(self):
        service, runtime, root = isolated_service(self, "m2")
        targets, plans = admitted_targets(service.config, TARGETS)
        observer = HostObserver()
        host_before = observer.snapshot()
        self.assertTrue(host_before["packages"]["available"], host_before["packages"].get("error"))
        cleanup_errors = []
        evidence = {
            "schema_version": 1,
            "started_at": now(),
            "host_kernel": os.uname().release,
            "host_uid": os.getuid(),
            "host_gid": os.getgid(),
            "experimental_package": "tree",
            "host_packages_before": host_before["packages"],
            "targets": {},
            "admission": plans,
            "transactions": [],
            "passed": False,
            "settings": {
                "rootless": True,
                "user_namespace": "rootless-default",
                "network": "pasta",
                "images": "locked digests already present; create uses --pull=never",
                "arch_install": "pacman -Syu --needed, after pacman-key --init when needed",
                "signature_checks": "left at the distribution default",
            },
            "scope": (
                "M2 native package transactions on the current host. "
                "Not GUI, full init, or another host."
            ),
        }
        try:
            for env_id, _target in targets:
                _reset(service, runtime, env_id)
            for env_id, target in targets:
                record = service.create(target, env_id, [], "pasta")
                self.assertEqual(record["runtime"]["network"], "pasta")
                self.assertEqual(record["runtime_state"], "stopped")
                self.assertEqual(record["content_revision"], 1)
                evidence["targets"][env_id] = {
                    "target": target,
                    "base_inventory_sha256": record["package_inventory_sha256"],
                    "base_inventory_count": record["package_inventory_count"],
                    "base_image_digest": record["base_image"]["manifest_digest"],
                    "writable_revision": record["writable_revision"],
                }

            primary = targets[0][0]
            base_inventory = evidence["targets"][primary]["base_inventory_sha256"]
            failed = service.install_packages(primary, ["missing-pkg-ml"])
            evidence["transactions"].append(_brief(failed))
            self.assertEqual(failed["phase"], "failed", _detail(failed))
            self.assertEqual(failed["failure_phase"], "native-command", _detail(failed))
            debian = service.inspect(primary)
            self.assertEqual(debian["content_revision"], 1)
            self.assertEqual(debian["package_inventory_sha256"], base_inventory)

            installed = {}
            for env_id, _target in targets:
                txn = service.install_packages(env_id, ["tree"], use_argv=["tree", "--version"])
                evidence["transactions"].append(_brief(txn))
                self.assertEqual(txn["phase"], "completed", _detail(txn))
                self.assertIn("tree", (txn["verification"]["stdout_preview"] or "").lower(),
                              _detail(txn))
                self._assert_isolated(txn, env_id, host_before)
                installed[env_id] = txn
                view = service.inspect(env_id)
                evidence["targets"][env_id]["after_install"] = {
                    "content_revision": view["content_revision"],
                    "package_inventory_sha256": view["package_inventory_sha256"],
                    "package_inventory_count": view["package_inventory_count"],
                    "repository_updated_at": (view.get("arch") or {}).get("repository_updated_at"),
                    "keyring_initialized_at": (view.get("arch") or {}).get("keyring_initialized_at"),
                    "tree": service.query_package(env_id, "tree"),
                }
                self.assertNotEqual(view["package_inventory_sha256"],
                                    evidence["targets"][env_id]["base_inventory_sha256"])
                self.assertEqual(view["writable_revision"],
                                 evidence["targets"][env_id]["writable_revision"])
                self.assertGreater(view["content_revision"], 1)

            if ("m2arch" in evidence["targets"] and
                    evidence["targets"]["m2arch"]["after_install"]["repository_updated_at"] is None):
                self.fail("Arch install did not record repository_updated_at")
            if "m2arch" in evidence["targets"]:
                self.assertIsNotNone(evidence["targets"]["m2arch"]["after_install"]["keyring_initialized_at"])

            for env_id, _target in targets:
                txn = service.update_packages(env_id)
                evidence["transactions"].append(_brief(txn))
                self.assertEqual(txn["phase"], "completed", _detail(txn))
                self._assert_isolated(txn, env_id, host_before)
                view = service.inspect(env_id)
                evidence["targets"][env_id]["after_update"] = {
                    "content_revision": view["content_revision"],
                    "package_inventory_sha256": view["package_inventory_sha256"],
                    "package_inventory_count": view["package_inventory_count"],
                    "repository_updated_at": (view.get("arch") or {}).get("repository_updated_at"),
                }

            for env_id, _target in targets:
                txn = service.remove_packages(env_id, ["tree"], use_argv=["tree", "--version"])
                evidence["transactions"].append(_brief(txn))
                self.assertEqual(txn["phase"], "completed", _detail(txn))
                self.assertNotEqual(txn["verification"]["command_exit_code"], 0, _detail(txn))
                self.assertFalse(txn["verification"]["packages"]["tree"], _detail(txn))
                self._assert_isolated(txn, env_id, host_before)
                view = service.inspect(env_id)
                evidence["targets"][env_id]["after_remove"] = {
                    "content_revision": view["content_revision"],
                    "package_inventory_sha256": view["package_inventory_sha256"],
                    "package_inventory_count": view["package_inventory_count"],
                    "tree_present": service.query_package(env_id, "tree")["present"],
                }
                self.assertFalse(evidence["targets"][env_id]["after_remove"]["tree_present"])

            host_after = observer.snapshot()
            self.assertTrue(host_after["packages"]["available"])
            self.assertEqual(host_before["packages"]["sha256"], host_after["packages"]["sha256"])
            self.assertEqual(host_before["entries"]["names"], host_after["entries"]["names"])
            evidence["host_packages_after"] = host_after["packages"]
            evidence["host_packages_unchanged"] = True
            evidence["passed"] = True
            evidence["finished_at"] = now()
        finally:
            for env_id, _target in targets:
                try:
                    _reset(service, runtime, env_id)
                except (UnifierError, OSError) as exc:
                    cleanup_errors.append(f"{env_id}: {exc}")
        if evidence["passed"]:
            self.assertEqual(cleanup_errors, [])
            for env_id, _target in targets:
                self.assertFalse(service.store.exists(env_id))
                self.assertIsNone(runtime.inspect(container_name(root, env_id)))
            evidence["containers_removed_after_acceptance"] = True
            evidence["cleaned_at"] = now()
            if os.environ.get("MULTILINUX_EVIDENCE") == "1":
                from tools import lab
                lab.save_json(Path(os.environ.get("UNIFIER_TEST_EVIDENCE", root / "evidence"))
                              / "m2-packages.json", evidence)

    def _assert_isolated(self, txn, env_id, host_before):
        for other_id, item in txn["other_environments"].items():
            self.assertTrue(item.get("unchanged"), _detail(txn))
            self.assertNotEqual(other_id, env_id)
        self.assertEqual(txn["host_packages"]["before"]["sha256"],
                         txn["host_packages"]["after"]["sha256"], _detail(txn))
        self.assertEqual(txn["host_packages"]["before"]["sha256"],
                         host_before["packages"]["sha256"], _detail(txn))
        self.assertEqual(txn["host_entries"]["added"], [], _detail(txn))
        self.assertEqual(txn["host_entries"]["removed"], [], _detail(txn))
        self.assertTrue(txn["host_packages"]["before"]["available"], _detail(txn))


if __name__ == "__main__":
    unittest.main()
