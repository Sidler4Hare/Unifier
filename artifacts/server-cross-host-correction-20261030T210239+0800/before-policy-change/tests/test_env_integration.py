"""Rootless Podman checks for the M1 lifecycle. Opt in with MULTILINUX_INTEGRATION=1."""

import hashlib
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
from unifier.project import load_project
from unifier.runtime import PodmanRuntime, container_name, project_label
from unifier.service import EnvironmentService, now


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
    def test_three_environments_execute_in_their_own_roots(self):
        config, lock, root = load_project()
        runtime = PodmanRuntime()
        service = EnvironmentService(root, runtime, config, lock)
        share = Path(tempfile.mkdtemp(prefix="ml-m1-"))
        cleanup_errors = []
        evidence = {
            "schema_version": 1,
            "started_at": now(),
            "host_kernel": os.uname().release,
            "host_uid": os.getuid(),
            "host_gid": os.getgid(),
            "targets": {},
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
            for env_id, _target, _distribution in TARGETS:
                _reset(service, runtime, env_id)
            for env_id, target, distribution in TARGETS:
                record = service.create(target, env_id, [str(share)])
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
                smoke = root / "evidence/lab-smoke-2026-10-18.packages" / f"{target}.txt"
                self.assertEqual(view["package_inventory_sha256"], _sorted_sha256(smoke))
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
                    "sorted_inventory_matches_m0_smoke": True,
                }

            self.assertEqual(service.run(
                "m1debian", ["sh", "-c", "printf debian-only > /var/tmp/ml-origin"],
                workdir="/", env_keys=[]), 0)
            self.assertEqual(service.run(
                "m1fedora", ["sh", "-c", "test ! -e /var/tmp/ml-origin"],
                workdir="/", env_keys=[]), 0)
            self.assertEqual(service.run(
                "m1arch", ["sh", "-c", "test ! -e /var/tmp/ml-origin"],
                workdir="/", env_keys=[]), 0)
            self.assertEqual(service.run(
                "m1fedora", ["sh", "-c", "printf fedora-only > /var/tmp/ml-origin"],
                workdir="/", env_keys=[]), 0)
            self.assertEqual(service.run(
                "m1debian", ["sh", "-c", 'test "$(cat /var/tmp/ml-origin)" = debian-only'],
                workdir="/", env_keys=[]), 0)
            owned = share / "owned.txt"
            self.assertEqual(service.run(
                "m1debian", ["sh", "-c", f"printf hi > {owned}"], workdir=str(share),
                env_keys=[]), 0)
            self.assertEqual(owned.read_text(), "hi")
            self.assertEqual(owned.stat().st_uid, os.getuid())
            self.assertEqual(owned.stat().st_gid, os.getgid())

            service.stop("m1debian")
            with self.assertRaises(UnifierError):
                service.run("m1debian", ["true"], workdir="/", env_keys=[])
            service.start("m1debian")
            self.assertEqual(service.run("m1debian", ["true"], workdir="/", env_keys=[]), 0)
            self.assertEqual(service.stop("m1debian")["runtime_state"], "stopped")
            self.assertEqual(service.stop("m1debian")["runtime_state"], "stopped")

            listed = subprocess_list(root)
            self.assertEqual({item["id"] for item in listed}, {item[0] for item in TARGETS})
            evidence["source_isolation"] = True
            evidence["shared_file_owned_by_host_user"] = True
            evidence["restart_after_stop"] = True
            evidence["passed"] = True
            evidence["finished_at"] = now()
        finally:
            for env_id, _target, _distribution in TARGETS:
                try:
                    _reset(service, runtime, env_id)
                except (UnifierError, OSError) as exc:
                    cleanup_errors.append(f"{env_id}: {exc}")
            shutil.rmtree(share, ignore_errors=True)
        if evidence["passed"]:
            self.assertEqual(cleanup_errors, [])
            for env_id, _target, _distribution in TARGETS:
                self.assertFalse(service.store.exists(env_id))
                self.assertIsNone(runtime.inspect(container_name(root, env_id)))
            evidence["containers_removed_after_acceptance"] = True
            if os.environ.get("MULTILINUX_EVIDENCE") == "1":
                from tools import lab
                lab.save_json(root / "evidence/m1-lifecycle-2026-10-19.json", evidence)


def _sorted_sha256(path: Path) -> str:
    lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return hashlib.sha256(("\n".join(sorted(lines)) + "\n").encode()).hexdigest()


def subprocess_list(root: Path):
    import subprocess
    completed = subprocess.run(
        [sys.executable, str(root / "tools/unifier.py"), "env", "list"],
        cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode:
        raise AssertionError(completed.stderr)
    return json.loads(completed.stdout)


if __name__ == "__main__":
    unittest.main()
