"""Offline classification of registered hardware and guarded evidence."""

from copy import deepcopy
import os
from pathlib import Path
import tempfile
import unittest

from unifier.errors import UnifierError
from unifier.hardware_report import MAX_RECEIPT_BYTES, hardware_report, read_receipt
from unifier.identity import binding_for


class HardwareReportTests(unittest.TestCase):
    def setUp(self):
        self.path = "/dev/dri/renderD128"
        self.device = {
            "sysfs_device": "/devices/pci0000:00/0000:00:02.0",
            "major": 226, "minor": 128, "vendor": "0x8086", "device": "0x9a49",
        }
        self.record = {
            "id": "debian13", "schema_version": 2,
            "environment_uuid": "b8bf73d0-cd6a-4338-8c91-81890b9ce767", "generation": 1,
            "runtime": {"container_id": "c" * 64, "render_node": self.path,
                        "render_node_identity": self.device},
        }
        self.binding = binding_for(self.record)
        self.observation = {
            "kind": "render-node", "selector": self.path, "device_identity": self.device,
            "boot_id": "c2563399-5bde-441d-b167-f35c923caaf5",
            "backend": "mesa-iris", "backend_version": "25.0.7",
        }
        self.receipt = {
            **self.observation, "schema_version": 1, "environment_binding": self.binding,
            "source": "guarded-egl/result.json", "result": "PASS",
            "capability": "egl-offscreen", "capability_state": "available",
            "cleanup_state": "verified", "software_fallback": False,
        }

    def classify(self, **fields):
        return hardware_report(
            self.record, observed_binding=self.binding, observations=[self.observation],
            evidence=[{**self.receipt, **fields}])["evidence"][0]

    def test_receipt_reader_refuses_unbounded_or_non_regular_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            normal = root / "receipt.json"
            normal.write_text('{"result":"PASS"}')
            self.assertEqual(read_receipt(normal), {"result": "PASS"})
            link = root / "linked.json"
            link.symlink_to(normal)
            fifo = root / "fifo"
            os.mkfifo(fifo)
            oversize = root / "oversize.json"
            with oversize.open("wb") as stream:
                stream.truncate(MAX_RECEIPT_BYTES + 1)
            for path in (root, link, fifo, oversize):
                with self.subTest(path=path), self.assertRaisesRegex(UnifierError, "regular non-empty file"):
                    read_receipt(path)

    def test_configured_only_and_no_selection_are_distinct(self):
        report = hardware_report(self.record, evidence=[self.receipt])
        self.assertEqual(report["environment_binding"], self.binding)
        self.assertEqual(report["binding_state"], "registered")
        self.assertEqual(report["selection_state"], "selected")
        self.assertEqual(report["selections"][0]["observation"], "unverified")
        self.assertEqual(report["evidence"][0]["capability_state"], "unknown")
        runtime = self.record["runtime"]
        del runtime["render_node"], runtime["render_node_identity"]
        self.assertEqual(hardware_report(self.record)["selection_state"], "none")

    def test_legacy_and_damaged_records_are_reported_without_forging_a_binding(self):
        legacy = deepcopy(self.record)
        del legacy["schema_version"], legacy["environment_uuid"], legacy["generation"]
        del legacy["runtime"]["render_node_identity"]
        report = hardware_report(legacy)
        self.assertEqual(report["binding_state"], "legacy")
        self.assertIsNone(report["environment_binding"])
        self.assertEqual(report["selections"][0]["state"], "legacy")
        for record in ({**legacy, "generation": 1}, {**self.record, "generation": True},
                       {**self.record, "runtime": {"container_id": "abcd"}}):
            with self.subTest(record=record):
                report = hardware_report(record)
                self.assertEqual(report["binding_state"], "invalid")
                self.assertIsNone(report["environment_binding"])

    def test_camera_and_cuda_keep_their_registered_identity(self):
        runtime = self.record["runtime"]
        runtime.clear()
        runtime.update(container_id=self.binding["container_id"],
                       camera={"path": "/dev/video0", "selection": "f" * 64},
                       cuda={"uuid": "GPU-1c58b790-adea-4f19-8322-346055f96da9", "minor": 1,
                             "pci": "0000:04:00.0", "library": "/usr/lib/libcuda.so.570.86.10"})
        camera, cuda = hardware_report(self.record)["selections"]
        self.assertEqual((camera["kind"], camera["identity"]["selection"]), ("camera", "f" * 64))
        self.assertEqual((cuda["kind"], cuda["selector"]), ("cuda", runtime["cuda"]["uuid"]))
        runtime["camera"]["selection"] = []
        self.assertEqual(hardware_report(self.record)["selection_state"], "invalid")

    def test_only_matching_provenance_and_verified_cleanup_support_available(self):
        row = self.classify()
        self.assertEqual((row["evidence_state"], row["capability_state"]), ("matched", "available"))
        for fields, reason in (
            ({"environment_binding": {**self.binding, "generation": 2}}, "environment-changed"),
            ({"boot_id": "05ae0a4c-abfd-4e15-a013-b40ec71f3fe7"}, "provenance-changed"),
            ({"backend_version": "26.2.4"}, "provenance-changed"),
            ({"cleanup_state": "unknown"}, "cleanup-unverified"),
            ({"result": "FAIL"}, "capability-unverified"),
        ):
            with self.subTest(fields=fields):
                row = self.classify(**fields)
                self.assertEqual((row["capability_state"], row["reason_code"]), ("unknown", reason))

    def test_reselected_and_replaced_devices_withhold_old_evidence(self):
        replacement = {**self.device, "sysfs_device": "/devices/pci0000:00/0000:00:03.0"}
        self.observation = {**self.observation, "device_identity": replacement}
        self.assertEqual(self.classify()["reason_code"], "device-changed")
        self.record["runtime"]["render_node"] = "/dev/dri/renderD129"
        self.record["runtime"]["render_node_identity"] = {**replacement, "minor": 129}
        row = self.classify()
        self.assertEqual((row["evidence_state"], row["reason_code"]), ("stale", "device-reselected"))

    def test_current_boot_disqualifies_receipt_even_if_guest_backend_is_unobserved(self):
        self.observation = {**self.observation, "backend": "kernel-drm"}
        stale = self.classify(boot_id="05ae0a4c-abfd-4e15-a013-b40ec71f3fe7")
        self.assertEqual((stale["evidence_state"], stale["reason_code"]), ("stale", "provenance-changed"))
        current = self.classify()
        self.assertEqual((current["evidence_state"], current["reason_code"]),
                         ("unverifiable", "evidence-provenance"))

    def test_negative_observations_and_software_are_not_hardware_passes(self):
        for state, why in (("unsupported", "profile-unsupported"),
                           ("unavailable", "backend-missing"), ("restricted", "permission-denied")):
            with self.subTest(state=state):
                row = self.classify(capability_state=state, result="NOT_RUN",
                                    reason_code=why, cleanup_state="not-needed")
                self.assertEqual((row["capability_state"], row["reason_code"]), (state, why))
        row = self.classify(software_fallback=True)
        self.assertTrue(row["software_fallback"])
        self.assertEqual((row["capability_state"], row["reason_code"]),
                         ("unknown", "software-fallback"))
