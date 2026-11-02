"""Lease retention must follow guest proof, not a dead project worker PID."""
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from unifier.on_demand import LaunchGroup, process_stamp
from unifier.project_sessions import assert_environment_not_live


class UnresolvedProjectLeaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        def write(path, record):
            path.write_text(json.dumps(record))
        self.service = SimpleNamespace(root=self.root, stop=Mock(),
                                       store=SimpleNamespace(_write_json=write))
        self.group = LaunchGroup(self.service, "fixture")
        # The current PID with a deliberately nonmatching stamp is an actual
        # stale identity; no assumption about an arbitrary unused PID is made.
        self.stale = {"pid": os.getpid(), "stamp": process_stamp(os.getpid()) + 1}

    def save(self, leases):
        self.group.path.write_text(json.dumps({"leases": leases, "auto_stop": True,
                                              "bridge": None, "container_id": "fixture-id"}))

    def test_dead_project_worker_is_not_proof_its_guest_has_exited(self):
        self.save({"unresolved": dict(self.stale, kind="project-session", project_uuid="fixture-project"),
                   "expired-app": dict(self.stale, app_id="legacy-app")})
        leases = self.group.read()["leases"]
        self.assertIn("unresolved", leases)
        self.assertNotIn("expired-app", leases)

    def test_releasing_another_lease_must_not_stop_an_unresolved_guest(self):
        self.save({"unresolved": dict(self.stale, kind="project-session", project_uuid="fixture-project"),
                   "other": {"pid": os.getpid(), "stamp": process_stamp(os.getpid()), "app_id": "other"}})
        self.group.release("other")
        self.service.stop.assert_not_called()
        self.assertIn("unresolved", json.loads(self.group.path.read_text())["leases"])

    def test_confirmed_cancel_does_not_block_retirement_after_container_stopped(self):
        self.service.runtime = SimpleNamespace(probe_guest=Mock(return_value="unknown"))
        self.service.store.load = Mock(return_value={"runtime": {"container_name": "fixture-container"}})
        record = {"session_uuid": "cancelled-fixture", "environment_id": "fixture",
                  "state": "interrupted", "guest_state": "gone", "lease_token": None,
                  "guest_pid": 2, "guest_start": 3, "guest_pgrp": 2, "guest_sid": 2,
                  "guest_token": "a" * 32, "session_user": "1000:1000"}
        with patch("unifier.project_sessions.SessionStore") as store:
            store.return_value.list.return_value = [record]
            assert_environment_not_live(self.root, "fixture", self.service)
        self.service.runtime.probe_guest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
