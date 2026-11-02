"""A recovery pass cannot grant authority withdrawn by a durable revoke."""
import base64
import hashlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from multilinux.desktop import HostDesktop
from multilinux.desktop_integrations import DesktopIntegrationManager


class DesktopRevocationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        service = SimpleNamespace(root=self.root, clock=lambda: "2026-10-25T00:00:00+08:00")
        self.manager = DesktopIntegrationManager(self.root, service,
            HostDesktop(self.root / "applications", self.root / "mime"))
        self.paths = [self.root / "entry.desktop", self.root / "mimeapps.list"]
        self.before = [b"original desktop bytes\n", b"original association bytes\n"]
        self.after = [b"installed desktop bytes\n", b"installed association bytes\n"]

    def record(self, state, *, partial=False):
        files = []
        for index, path in enumerate(self.paths):
            path.write_bytes(self.before[index] if partial and index == 0 else self.after[index])
            files.append({"role": "desktop-entry" if index == 0 else "mime-default",
                          "path": str(path),
                          "before": base64.b64encode(self.before[index]).decode(),
                          "after": base64.b64encode(self.after[index]).decode(),
                          "before_sha256": hashlib.sha256(self.before[index]).hexdigest(),
                          "after_sha256": hashlib.sha256(self.after[index]).hexdigest()})
        return self.manager._save({"schema_version": 1,
            "integration_uuid": str(uuid.uuid4()), "project_uuid": str(uuid.uuid4()),
            "environment_binding": {"environment_id": "fixture", "environment_uuid": str(uuid.uuid4()),
                                    "generation": 1, "container_id": "a" * 64},
            "environment_id": "fixture", "app_id": None, "session_uuid": None,
            "desktop_id": "fixture", "desktop_path": str(self.paths[0]),
            "mime_type": None, "extension": None, "terminal": False,
            "state": state, "files": files,
            "conflict": {"reason": "prior external change"} if state == "conflict" else None,
            "created_at": "2026-10-25T00:00:00+08:00", "updated_at": "2026-10-25T00:00:00+08:00"})

    def test_revoke_intent_is_not_reinstalled_if_crash_preceded_first_file_edit(self):
        record = self.record("revoking")
        self.manager.recover()
        self.manager.recover()
        current = self.manager.show(record["integration_uuid"])
        self.assertEqual("revoked", current["state"])
        for path, before in zip(self.paths, self.before):
            self.assertEqual(before, path.read_bytes())

    def test_partial_known_revoke_can_finish_without_regranting_the_entry(self):
        record = self.record("revoking", partial=True)
        self.manager.recover()
        self.manager.recover()
        self.assertEqual("revoked", self.manager.show(record["integration_uuid"])["state"])
        for path, before in zip(self.paths, self.before):
            self.assertEqual(before, path.read_bytes())

    def test_old_installed_bytes_do_not_regrant_a_revoked_or_conflicted_entry(self):
        for state in ["revoked", "conflict"]:
            with self.subTest(state=state):
                record = self.record(state)
                self.manager.recover()
                self.assertNotEqual("installed", self.manager.show(record["integration_uuid"])["state"])


if __name__ == "__main__":
    unittest.main()
