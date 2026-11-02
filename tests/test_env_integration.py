"""Rootless Podman checks for the M1 lifecycle. Opt in with MULTILINUX_INTEGRATION=1."""

import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from unifier.errors import UnifierError
from podman_test_support import admitted_targets, isolated_service
from unifier.runtime import container_name, project_label
from unifier.service import now


TARGETS = (
    ("m1debian", "debian13", "debian"),
    ("m1fedora", "fedora44", "fedora"),
    ("m1arch", "arch", "arch"),
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


@unittest.skipUnless(os.environ.get("MULTILINUX_INTEGRATION") == "1",
                     "set MULTILINUX_INTEGRATION=1 to use rootless Podman")
class PodmanLifecycle(unittest.TestCase):
    def test_admitted_environments_execute_in_their_own_roots(self):
        service, runtime, root = isolated_service(self, "m1")
        targets, plans = admitted_targets(service.config, TARGETS)
        share = Path(tempfile.mkdtemp(prefix="ml-m1-"))
        cleanup_errors = []
        evidence = {
            "schema_version": 1,
            "started_at": now(),
            "host_kernel": os.uname().release,
            "host_uid": os.getuid(),
            "host_gid": os.getgid(),
            "targets": {},
            "admission": plans,
            "passed": False,
            "settings": {
                "rootless": True,
                "user_namespace": "rootless-default",
                "network": "none",
                "rootfs": "writable",
                "full_init": False,
                "inventory_checksum": "sha256 of the sorted package list",
            },
            "scope": "M1 lifecycle on the current host. Not package install, GUI, full init, or another host.",
        }
        try:
            for env_id, _target, _distribution in targets:
                _reset(service, runtime, env_id)
            for env_id, target, distribution in targets:
                record = service.create(target, env_id, [str(share)],
                                        share_label_policy="shared-relabel")
                self.assertEqual(record["runtime_state"], "stopped")
                self.assertEqual(record["distribution_id"], distribution)
                service.start(env_id)
                self.assertEqual(service.run(env_id, ["true"], workdir="/usr", env_keys=[]), 0)
                self.assertEqual(service.run(env_id, ["false"], workdir="/usr", env_keys=[]), 1)
                self.assertEqual(service.run(env_id, ["sh", "-c", "exit 17"], workdir="/",
                                             env_keys=[]), 17)
                marker = share / f"{env_id}.pwd"
                self.assertEqual(service.run(
                    env_id, ["sh", "-c", f"pwd > {marker}"], workdir="/usr", env_keys=[]), 0)
                self.assertEqual(marker.read_text().strip(), "/usr")
                identity = share / f"{env_id}.id"
                self.assertEqual(service.run(
                    env_id, ["sh", "-c", f'. /etc/os-release; printf %s "$ID" > {identity}'],
                    workdir="/", env_keys=[]), 0)
                self.assertEqual(identity.read_text().strip(), distribution)
                view = service.inspect(env_id)
                self.assertEqual(view["runtime_state"], "running")
                self.assertTrue(view["kernel_matches_current_host"])
                self.assertGreater(view["package_inventory_count"], 0)
                self.assertEqual(view["package_inventory_sha256"],
                                 record["package_inventory_sha256"])
                evidence["targets"][env_id] = {
                    "target": target,
                    "distribution_id": distribution,
                    "version_track": view["version_track"],
                    "package_inventory_count": view["package_inventory_count"],
                    "package_inventory_sha256": view["package_inventory_sha256"],
                    "writable_revision": view["writable_revision"],
                    "kernel": view["observed"]["kernel"],
                    "base_image_digest": view["base_image"]["manifest_digest"],
                    "exit_code_17": True,
                    "workdir": "/usr",
                    "inventory_unchanged_after_commands": True,
                }

            primary = targets[0][0]
            for env_id, _target, _distribution in targets:
                self.assertEqual(service.run(
                    env_id, ["sh", "-c", 'test ! -e /var/tmp/ml-origin'],
                    workdir="/", env_keys=[]), 0)
            self.assertEqual(service.run(
                primary, ["sh", "-c", "printf isolated > /var/tmp/ml-origin"],
                workdir="/", env_keys=[]), 0)
            for env_id, _target, _distribution in targets[1:]:
                self.assertEqual(service.run(
                    env_id, ["sh", "-c", "test ! -e /var/tmp/ml-origin"],
                    workdir="/", env_keys=[]), 0)
            owned = share / "owned.txt"
            self.assertEqual(service.run(
                primary, ["sh", "-c", f"printf hi > {owned}"], workdir=str(share),
                env_keys=[]), 0)
            self.assertEqual(owned.read_text(), "hi")
            self.assertEqual(owned.stat().st_uid, os.getuid())
            self.assertEqual(owned.stat().st_gid, os.getgid())

            service.stop(primary)
            with self.assertRaises(UnifierError):
                service.run(primary, ["true"], workdir="/", env_keys=[])
            service.start(primary)
            self.assertEqual(service.run(primary, ["true"], workdir="/", env_keys=[]), 0)
            self.assertEqual(service.stop(primary)["runtime_state"], "stopped")
            self.assertEqual(service.stop(primary)["runtime_state"], "stopped")

            listed = subprocess_list(root)
            self.assertEqual({item["id"] for item in listed}, {item[0] for item in targets})
            evidence["source_isolation"] = True
            evidence["shared_file_owned_by_host_user"] = True
            evidence["restart_after_stop"] = True
            evidence["passed"] = True
            evidence["finished_at"] = now()
        finally:
            for env_id, _target, _distribution in targets:
                try:
                    _reset(service, runtime, env_id)
                except (UnifierError, OSError) as exc:
                    cleanup_errors.append(f"{env_id}: {exc}")
            shutil.rmtree(share, ignore_errors=True)
        if evidence["passed"]:
            self.assertEqual(cleanup_errors, [])
            for env_id, _target, _distribution in targets:
                self.assertFalse(service.store.exists(env_id))
                self.assertIsNone(runtime.inspect(container_name(root, env_id)))
            evidence["containers_removed_after_acceptance"] = True
            if os.environ.get("MULTILINUX_EVIDENCE") == "1":
                from tools import lab
                lab.save_json(Path(os.environ.get("UNIFIER_TEST_EVIDENCE", root / "evidence"))
                              / "m1-lifecycle.json", evidence)


def subprocess_list(root: Path):
    import subprocess
    environment = dict(os.environ, UNIFIER_HOME=str(root))
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools/unifier.py"), "env", "list"],
        cwd=ROOT, env=environment, capture_output=True, text=True, check=False, timeout=60)
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
