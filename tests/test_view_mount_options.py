"""Frozen share validation against the real Podman mount lifecycle.

Podman exposes a creation-time ``:z``/``:Z`` in ``Mounts[].Mode`` before the
first start.  On start it consumes that flag: ``Mode`` and ``HostConfig.Binds``
both become empty, while ``Config.CreateCommand`` keeps the original volume
request.  These tests use the shape captured from real Podman 5.8.9 creates and
starts, without starting any container here.
"""

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier.errors import UnifierError
from unifier.views import ViewManager
from test_env import service_in


_MISSING = object()


class MountOptionNormalization(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.notes = self.root / "notes"
        self.notes.mkdir()
        self.service, self.runtime = service_in(self.root)
        self.service.create(
            "debian13", None, [str(self.notes)],
            share_label_policy="shared-relabel")
        self.record = self.service.store.load("debian13")
        self.cid = self.record["runtime"]["container_id"]
        self.host = self.record["shared_mounts"][0]["host"]
        self.destination = self.record["shared_mounts"][0]["container"]
        self.manager = ViewManager(self.root, self.service)

    def tearDown(self):
        self.temporary.cleanup()

    def _mount(self, *, mode="z", rw=True, source=None, options=None):
        return {
            "Type": "bind",
            "Source": source or self.host,
            "Destination": self.destination,
            "Driver": "",
            "Mode": mode,
            "Options": ["rbind"] if options is None else list(options),
            "RW": rw,
            "Propagation": "rprivate",
        }

    def _command(self, *, options="z", source=None, destination=None,
                 flag="-v"):
        source = source or self.host
        destination = destination or self.destination
        volume = source + ":" + destination
        if options:
            volume += ":" + options
        command = [
            "podman", "create", "--pull=never", "--name", "lifecycle-probe",
            "--network", "none", "--userns", "keep-id", "--user", "0",
            "--init", "--entrypoint", "/usr/bin/sleep",
            "--security-opt", "label=disable",
        ]
        if flag == "--volume":
            command.extend(["--volume", volume])
        elif flag == "--volume=":
            command.append("--volume=" + volume)
        else:
            command.extend(["-v", volume])
        command.extend(["image@sha256:" + "a" * 64, "infinity"])
        return command

    def _collect(self, record, mounted, create_command=_MISSING):
        calls = []

        def fake(argv, **kwargs):
            calls.append(argv)
            if any(".Mounts" in item for item in argv):
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(mounted), stderr="")
            if any(".Config.CreateCommand" in item for item in argv):
                if create_command is _MISSING:
                    return SimpleNamespace(
                        returncode=0, stdout="null", stderr="")
                return SimpleNamespace(
                    returncode=0, stdout=json.dumps(create_command), stderr="")
            raise AssertionError(f"unexpected inspect command: {argv}")

        with mock.patch("unifier.views.subprocess.run", side_effect=fake):
            result = self.manager._collect_mounts(record)
        self.calls = calls
        return result

    def test_real_z_mode_with_rbind_options_is_unchanged(self):
        data, extra = self._collect(self.record, [self._mount()])
        self.assertEqual(extra, set())
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["destination"], self.destination)
        self.assertEqual(data[0]["source"], self.host)
        tokens = set(data[0]["options"].split(","))
        self.assertIn("z", tokens)
        self.assertIn("rbind", tokens)
        # Mode was still present, so no creation proof was needed.
        self.assertEqual(len(self.calls), 1)

    def test_preserve_host_mode_without_relabel_is_unchanged(self):
        record = copy.deepcopy(self.record)
        record["shared_mounts"][0]["options"] = ""
        data, _extra = self._collect(record, [self._mount(mode="")])
        self.assertEqual(len(data), 1)
        self.assertNotIn("z", data[0]["options"].split(","))
        self.assertNotIn("Z", data[0]["options"].split(","))

    def test_started_mount_uses_same_cid_creation_command(self):
        data, _extra = self._collect(
            self.record, [self._mount(mode="")], self._command(options="z"))
        self.assertEqual(len(data), 1)
        creation_calls = [call for call in self.calls
                          if any(".Config.CreateCommand" in item for item in call)]
        self.assertEqual(len(creation_calls), 1)
        # The same bound CID is inspected, not a name and not a new instance.
        self.assertEqual(creation_calls[0][-1], self.cid)

    def test_z_and_Z_are_not_interchangeable(self):
        mounted = self._mount(mode="z")
        record = copy.deepcopy(self.record)
        record["shared_mounts"][0]["options"] = "Z"
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(record, [mounted])

        record = copy.deepcopy(self.record)
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(record, [self._mount(mode="Z")])

    def test_consumed_z_with_correct_creation_command_passes(self):
        data, _extra = self._collect(
            self.record, [self._mount(mode="")], self._command(options="z"))
        self.assertEqual(len(data), 1)

        # The supported parser forms all resolve the same exact host/dest.
        for flag in ("--volume", "--volume="):
            with self.subTest(flag=flag):
                data, _extra = self._collect(
                    self.record, [self._mount(mode="")],
                    self._command(options="z", flag=flag))
                self.assertEqual(len(data), 1)

    def test_consumed_z_requires_creation_evidence(self):
        with self.assertRaisesRegex(UnifierError, "creation evidence is missing"):
            self._collect(self.record, [self._mount(mode="")])
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(self.record, [self._mount(mode="")],
                          self._command(options=""))
        with self.assertRaisesRegex(UnifierError, "creation evidence is unavailable"):
            # A failed inspect must fail closed rather than assume :z.
            def fake(argv, **kwargs):
                if any(".Mounts" in item for item in argv):
                    return SimpleNamespace(
                        returncode=0, stdout=json.dumps([self._mount(mode="")]),
                        stderr="")
                return SimpleNamespace(returncode=125, stdout="",
                                       stderr="no such container")
            with mock.patch("unifier.views.subprocess.run", side_effect=fake):
                self.manager._collect_mounts(self.record)

    def test_consumed_z_rejects_wrong_z_or_Z(self):
        for options in ("Z", "z,noexec"):
            with self.subTest(options=options):
                with self.assertRaisesRegex(UnifierError, "options changed"):
                    self._collect(self.record, [self._mount(mode="")],
                                  self._command(options=options))

    def test_creation_evidence_must_match_exact_source_and_destination(self):
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(UnifierError, "creation evidence is missing"):
            self._collect(self.record, [self._mount(mode="")],
                          self._command(source=str(other)))
        with self.assertRaisesRegex(UnifierError, "creation evidence is missing"):
            self._collect(self.record, [self._mount(mode="")],
                          self._command(destination="/somewhere-else"))

    def test_ambiguous_creation_evidence_is_rejected(self):
        command = self._command(options="z")
        volume = command[command.index("-v") + 1]
        command.extend(["-v", volume])
        with self.assertRaisesRegex(UnifierError, "creation evidence is ambiguous"):
            self._collect(self.record, [self._mount(mode="")], command)

    def test_missing_mode_cannot_be_excused_by_label_disable(self):
        record = copy.deepcopy(self.record)
        record["runtime"]["selinux_label"] = "disable"
        with self.assertRaisesRegex(UnifierError, "creation evidence is missing"):
            self._collect(record, [self._mount(mode="")])
        data, _extra = self._collect(
            record, [self._mount(mode="")], self._command(options="z"))
        self.assertEqual(len(data), 1)

    def test_unexpected_relabel_is_rejected_before_start(self):
        record = copy.deepcopy(self.record)
        record["shared_mounts"][0]["options"] = ""
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(record, [self._mount(mode="z")])

    def test_read_only_and_read_write_mismatch_is_rejected(self):
        record = copy.deepcopy(self.record)
        record["shared_mounts"][0]["options"] = "ro,z"
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(record, [self._mount(mode="z", rw=True)])
        data, _extra = self._collect(
            record, [self._mount(mode="", rw=False)],
            self._command(options="ro,z"))
        self.assertEqual(len(data), 1)
        self.assertIn("ro", data[0]["options"].split(","))

        # A record without ``ro`` expects read-write; a read-only mount is a
        # changed contract even though it is more restrictive.
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(self.record, [self._mount(mode="z", rw=False)])

        # The consumed creation request must also agree about read-only state.
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(self.record, [self._mount(mode="", rw=False)],
                          self._command(options="z"))
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(self.record, [self._mount(mode="", rw=True)],
                          self._command(options="ro,z"))

    def test_real_source_change_is_rejected(self):
        other = self.root / "other"
        other.mkdir()
        with self.assertRaisesRegex(UnifierError, "source changed"):
            self._collect(self.record, [self._mount(source=str(other))])

    def test_destination_must_exist_in_podman_mounts(self):
        mounted = self._mount()
        mounted["Destination"] = "/somewhere-else"
        with self.assertRaisesRegex(UnifierError, "not in Podman mounts"):
            self._collect(self.record, [mounted])

    def test_unknown_expected_option_is_not_filtered_out(self):
        record = copy.deepcopy(self.record)
        record["shared_mounts"][0]["options"] = "z,noexec"
        # The consumed creation request and runtime flags agree.
        data, _extra = self._collect(
            record, [self._mount(mode="", options=["rbind", "noexec"])],
            self._command(options="z,noexec"))
        self.assertEqual(len(data), 1)
        self.assertIn("noexec", data[0]["options"].split(","))

        # An unexpected creation flag is not silently ignored.
        with self.assertRaisesRegex(UnifierError, "options changed"):
            self._collect(self.record, [self._mount(mode="")],
                          self._command(options="z,noexec"))


if __name__ == "__main__":
    unittest.main()
