"""Explicit ordinary-share SELinux policy: preserve by default, :z only on request."""

from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from unifier import cli
from unifier.errors import UnifierError
from unifier.runtime import creation_argv
from test_env import service_in


def _ready(root: Path):
    notes = root / "notes"
    notes.mkdir()
    service, runtime = service_in(root)
    return service, runtime, notes


class ShareLabelPolicy(unittest.TestCase):
    def test_default_preserve_host_adds_no_z_and_persists_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, notes = _ready(root)
            record = service.create("debian13", None, [str(notes)], network="pasta")
            self.assertEqual(record["runtime"]["share_label_policy"], "preserve-host")
            self.assertEqual(record["shared_mounts"][0]["options"], "")
            spec = [event[1] for event in runtime.events if event[0] == "create"][0]
            argv = creation_argv(spec)
            volume = f"{notes.resolve().as_posix()}:{notes.resolve().as_posix()}"
            self.assertIn(volume, argv)
            self.assertNotIn(volume + ":z", argv)
            self.assertNotIn(volume + ":Z", argv)
            self.assertNotIn("--security-opt", argv)
            self.assertEqual(service.inspect("debian13")["share_label_policy"],
                             "preserve-host")

    def test_explicit_shared_relabel_adds_z_only_to_ordinary_shares(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, notes = _ready(root)
            record = service.create(
                "debian13", None, [str(notes)], share_label_policy="shared-relabel")
            self.assertEqual(record["runtime"]["share_label_policy"], "shared-relabel")
            self.assertEqual(record["shared_mounts"][0]["options"], "z")
            spec = [event[1] for event in runtime.events if event[0] == "create"][0]
            argv = creation_argv(spec)
            volume = f"{notes.resolve().as_posix()}:{notes.resolve().as_posix()}"
            self.assertIn(volume + ":z", argv)

    def test_unknown_or_non_string_policy_is_rejected_before_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, notes = _ready(root)
            for value in ("relabel", [], {}, None, True, 1):
                with self.subTest(value=value), self.assertRaises(UnifierError):
                    service.create(
                        "debian13", None, [str(notes)], share_label_policy=value)
            self.assertEqual(runtime.events, [])
            self.assertFalse(service.store.exists("debian13"))

            # Use a standalone spec for argv validation; no create happened above.
            from unifier.runtime import CreateSpec
            spec = CreateSpec("name", "id", "/project", "repo@sha256:" + "a" * 64,
                              (("/data", "/data"),))
            for value in ("relabel", [], {}, None):
                with self.subTest(argv=value), self.assertRaises(UnifierError):
                    creation_argv(replace(spec, share_label_policy=value))

    def test_plan_treats_policy_change_as_replacement_without_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, runtime, notes = _ready(root)
            service.create("debian13", None, [str(notes)])
            source = service.store.path("debian13")
            before_bytes = source.read_bytes()
            before_events = list(runtime.events)
            self.assertEqual(service.change_plan("debian13")["action"], "unchanged")
            same = service.change_plan(
                "debian13", {"share_label_policy": "preserve-host"})
            self.assertEqual(same["action"], "unchanged")
            changed = service.change_plan(
                "debian13", {"share_label_policy": "shared-relabel"})
            self.assertEqual(changed["action"], "replace")
            self.assertEqual(changed["changes"], ["share_label_policy"])
            self.assertEqual(source.read_bytes(), before_bytes)
            self.assertEqual(runtime.events, before_events)

    def test_legacy_record_infers_z_policy_without_rewriting(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime, notes = _ready(root)
            service.create("debian13", None, [str(notes)], share_label_policy="shared-relabel")
            record = service.store.load("debian13")
            record["runtime"].pop("share_label_policy")
            service.store.save(record, None)
            source = service.store.path("debian13")
            before = source.read_bytes()
            view = service.inspect("debian13")
            self.assertEqual(view["share_label_policy"], "shared-relabel")
            self.assertNotIn("share_label_policy", service.store.load("debian13")["runtime"])
            self.assertEqual(service.change_plan("debian13")["action"], "unchanged")
            replaced = service.change_plan(
                "debian13", {"share_label_policy": "preserve-host"})
            self.assertEqual(replaced["action"], "replace")
            self.assertEqual(source.read_bytes(), before)

            # Explicit migration keeps the inferred historical policy and does
            # not add the new runtime field to the old record.
            record = service.store.load("debian13")
            record.pop("environment_uuid")
            record.pop("generation")
            record["schema_version"] = 1
            service.store.save(record, None)
            migration = service.migrate_environment("debian13")
            self.assertEqual(migration["status"], "migrated")
            migrated = service.store.load("debian13")
            self.assertNotIn("share_label_policy", migrated["runtime"])
            self.assertEqual(migrated["shared_mounts"][0]["options"], "z")
            self.assertEqual(service.inspect("debian13")["share_label_policy"],
                             "shared-relabel")

    def test_empty_legacy_options_mean_preserve_host(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime, notes = _ready(root)
            service.create("debian13", None, [str(notes)])
            record = service.store.load("debian13")
            record["runtime"].pop("share_label_policy")
            service.store.save(record, None)
            self.assertEqual(service.inspect("debian13")["share_label_policy"],
                             "preserve-host")
            self.assertEqual(service.change_plan("debian13")["action"], "unchanged")

    def test_persisted_policy_conflicting_with_mount_options_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service, _runtime, notes = _ready(root)
            service.create("debian13", None, [str(notes)], share_label_policy="shared-relabel")
            record = service.store.load("debian13")
            record["runtime"]["share_label_policy"] = "preserve-host"
            service.store.save(record, None)
            with self.assertRaisesRegex(UnifierError, "conflicts"):
                service.inspect("debian13")
            with self.assertRaisesRegex(UnifierError, "conflicts"):
                service.change_plan("debian13")

    def test_cli_create_and_plan_forward_the_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, _runtime, notes = _ready(Path(temporary))
            calls = {}

            def create(target, env_id, shares, network, **options):
                calls["create"] = (target, env_id, shares, network, options)
                return {"id": "debian13", "target": target, "runtime_state": "stopped"}

            def plan(env_id, desired=None):
                calls["plan"] = (env_id, desired)
                return {"action": "unchanged"}

            service.create = create
            service.change_plan = plan
            original = cli.service_factory
            cli.service_factory = lambda: service
            try:
                from contextlib import redirect_stdout
                from io import StringIO
                with redirect_stdout(StringIO()):
                    self.assertEqual(cli.main([
                        "env", "create", "--target", "debian13",
                        "--share", str(notes), "--share-label", "shared-relabel"]), 0)
                    self.assertEqual(cli.main([
                        "env", "plan", "debian13",
                        "--share-label", "preserve-host"]), 0)
            finally:
                cli.service_factory = original
            self.assertEqual(calls["create"][4]["share_label_policy"], "shared-relabel")
            self.assertEqual(calls["plan"][1]["share_label_policy"], "preserve-host")


if __name__ == "__main__":
    unittest.main()
