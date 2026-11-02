"""Opt-in tests must use admitted guests without sharing daily state."""

from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from podman_test_support import admitted_targets, isolated_service
from unifier.install import load_catalog
from unifier.runtime import container_name


class _CleanupSink:
    """Minimal addCleanup sink so a test can run a helper cleanup and see its error."""

    def __init__(self):
        self.pending = []

    def addCleanup(self, function, *args, **kwargs):
        self.pending.append((function, args, kwargs))

    def run(self):
        for function, args, kwargs in self.pending:
            function(*args, **kwargs)


class IntegrationPolicyTests(unittest.TestCase):
    def test_guest_matrix_follows_current_host_family(self):
        config, _lock = load_catalog()
        targets = tuple((name, name) for name in config["targets"])
        for host, expected in (
                ("fedora", {"debian13", "arch"}),
                ("debian", {"fedora44", "arch"}),
                ("ubuntu", {"fedora44", "arch"}),
                ("arch", {"debian13", "fedora44"}),
                ("rocky", {"debian13", "fedora44", "arch"})):
            with self.subTest(host=host):
                facts = {"os_release": {"ID": host}, "system": "Linux",
                         "machine": "x86_64", "libc": "glibc"}
                selected, plans = admitted_targets(config, targets, host_facts=facts)
                self.assertEqual({item[1] for item in selected}, expected)
                self.assertEqual({name for name, plan in plans.items() if plan["allowed"]}, expected)
                for name in set(plans) - expected:
                    self.assertIn("same-family containers are forbidden", "; ".join(plans[name]["reasons"]))

    def test_unsupported_host_skips_before_creating_guests(self):
        config, _lock = load_catalog()
        facts = {"os_release": {"ID": "alpine"}, "system": "Linux",
                 "machine": "x86_64", "libc": "musl"}
        with self.assertRaisesRegex(unittest.SkipTest, "no admitted integration guests"):
            admitted_targets(config, (("demo", "debian13"),), host_facts=facts)

    def test_new_roots_have_distinct_container_and_state_identities(self):
        # Merely constructing the context must never query or mutate Podman.
        with patch("podman_test_support.PodmanRuntime"):
            first, _runtime, first_root = isolated_service(self, "policy")
            second, _runtime, second_root = isolated_service(self, "policy")
        self.assertNotEqual(first_root, second_root)
        self.assertNotEqual(first_root, ROOT)
        self.assertEqual(first.store.all(), [])
        self.assertEqual(second.store.all(), [])
        self.assertNotEqual(container_name(first.root, "demo"),
                            container_name(second.root, "demo"))

    def test_default_evidence_receipt_survives_temp_root_cleanup(self):
        # The opt-in guests default to <root>/evidence when MULTILINUX_EVIDENCE=1 and
        # UNIFIER_TEST_EVIDENCE is unset, so a cleanup that only removes the fresh
        # root throws away the receipt of a run that already passed.
        with tempfile.TemporaryDirectory() as box:
            evidence_root = Path(box) / "integration-runs"
            with patch("podman_test_support.PodmanRuntime"), \
                    patch("podman_test_support.EVIDENCE_ROOT", evidence_root, create=True):
                service, _runtime, root = isolated_service(self, "evidence")
                receipt = root / "evidence" / "m1-lifecycle.json"
                receipt.parent.mkdir(parents=True)
                receipt.write_text('{"passed": true}\n', encoding="utf-8")
                self.doCleanups()
            self.assertEqual(service.store.all(), [])
            self.assertFalse(root.exists(), "temporary install root must still go away")
            preserved = evidence_root / root.name / "m1-lifecycle.json"
            self.assertTrue(preserved.is_file(),
                            f"default receipt was deleted with the temporary root {root}")
            self.assertEqual(preserved.read_text(encoding="utf-8"), '{"passed": true}\n')

    def test_preservation_conflict_keeps_temp_root_and_existing_target(self):
        sink = _CleanupSink()
        with tempfile.TemporaryDirectory() as box:
            evidence_root = Path(box) / "integration-runs"
            with patch("podman_test_support.PodmanRuntime"), \
                    patch("podman_test_support.EVIDENCE_ROOT", evidence_root, create=True):
                _service, _runtime, root = isolated_service(sink, "evidence")
                receipt = root / "evidence" / "m4-resources.json"
                receipt.parent.mkdir(parents=True)
                receipt.write_text('{"passed": true}\n', encoding="utf-8")
                target = evidence_root / root.name
                target.mkdir(parents=True)
                sentinel = target / "m4-resources.json"
                sentinel.write_text("earlier run\n", encoding="utf-8")
                with self.assertRaisesRegex(RuntimeError, "evidence could not be preserved"):
                    sink.run()
                self.assertEqual(sentinel.read_text(encoding="utf-8"), "earlier run\n")
                self.assertTrue(root.exists(), "failed preservation keeps the temporary root")
                self.assertTrue(receipt.is_file(), "failed preservation keeps the original receipt")
            shutil.rmtree(root, ignore_errors=True)

    def test_explicit_external_evidence_stays_outside_the_preserved_run(self):
        sink = _CleanupSink()
        with tempfile.TemporaryDirectory() as box:
            evidence_root = Path(box) / "integration-runs"
            external = Path(box) / "external"
            with patch("podman_test_support.PodmanRuntime"), \
                    patch("podman_test_support.EVIDENCE_ROOT", evidence_root, create=True):
                _service, _runtime, root = isolated_service(sink, "evidence")
                receipt = external / "m2-packages.json"
                receipt.parent.mkdir(parents=True)
                receipt.write_text('{"passed": true}\n', encoding="utf-8")
                sink.run()
            self.assertFalse(root.exists())
            self.assertEqual(receipt.read_text(encoding="utf-8"), '{"passed": true}\n')
            self.assertFalse(evidence_root.exists(),
                             "an explicit external output needs no preserved run directory")


if __name__ == "__main__":
    unittest.main()
