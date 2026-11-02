"""Contract checks for immutable images and target/host identity boundaries."""

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import lab


class ManifestContracts(unittest.TestCase):
    def test_selects_amd64_and_ignores_attestation_or_other_architectures(self):
        wanted = "sha256:" + "a" * 64
        raw = json.dumps({"manifests": [
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64"}},
            {"digest": "sha256:" + "c" * 64, "platform": {"os": "unknown", "architecture": "unknown"}},
            {"digest": wanted, "platform": {"os": "linux", "architecture": "amd64"}},
        ]}).encode()
        root_digest, selected = lab.select_manifest(raw, "linux/amd64")
        self.assertEqual(root_digest, "sha256:" + hashlib.sha256(raw).hexdigest())
        self.assertEqual(selected, wanted)

    def test_missing_or_ambiguous_platform_is_rejected(self):
        for entries in ([], [
            {"digest": "sha256:" + "a" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
            {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "amd64"}},
        ]):
            with self.subTest(entries=entries), self.assertRaises(lab.LabError):
                lab.select_manifest(json.dumps({"manifests": entries}).encode(), "linux/amd64")

    def test_floating_reference_cannot_replace_locked_digest(self):
        config = lab.targets()
        items = {}
        for name, target in config["targets"].items():
            digest = "sha256:" + "a" * 64
            items[name] = {"source": target["source"], "manifest_digest": digest,
                           "locked_reference": lab.repository(target["source"]) + "@" + digest,
                           "os": "linux", "architecture": "amd64"}
        items["arch"]["locked_reference"] = "docker.io/library/archlinux:latest"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lab.save_json(root / "config/images.lock.json", {
                "schema_version": 1, "platform": "linux/amd64", "targets": items,
            })
            with patch.object(lab, "ROOT", root), self.assertRaises(lab.LabError):
                lab.read_lock(config)


class ExecutionBoundaryContracts(unittest.TestCase):
    def probe(self):
        return {"ID": "debian", "VERSION_ID": "13", "UID": "1000", "GID": "1000",
                "KERNEL": "host-kernel", "ARCH": "x86_64", "INPUT_SHA256": "expected"}

    def test_wrong_distribution_version_or_kernel_is_rejected(self):
        target = lab.targets()["targets"]["debian13"]
        for key, value in (("ID", "ubuntu"), ("VERSION_ID", "12"), ("KERNEL", "other-kernel")):
            probe = self.probe()
            probe[key] = value
            with self.subTest(key=key), self.assertRaises(lab.LabError):
                lab.validate_probe(probe, target, "expected", 1000, 1000, "host-kernel")

    def test_wrong_uid_or_shared_file_content_is_rejected(self):
        target = lab.targets()["targets"]["debian13"]
        for key, value in (("UID", "0"), ("GID", "0"), ("INPUT_SHA256", "different")):
            probe = self.probe()
            probe[key] = value
            with self.subTest(key=key), self.assertRaises(lab.LabError):
                lab.validate_probe(probe, target, "expected", 1000, 1000, "host-kernel")

    def test_root_execution_is_rejected_before_runtime_use(self):
        with patch.object(lab.os, "geteuid", return_value=0), patch.object(lab, "command") as command:
            with self.assertRaises(lab.LabError):
                lab.require_rootless()
            command.assert_not_called()


if __name__ == "__main__":
    unittest.main()
