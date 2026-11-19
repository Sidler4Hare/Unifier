"""Configuration and launch of a private Portal bridge on an existing desktop."""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from test_env import service_in
from unifier.cli import build_parser
from unifier.errors import UnifierError
from unifier.file_chooser import MODE_KEY
from unifier.on_demand import LaunchGroup, _start_bus_bridge
from unifier.portal_mount import PORTAL_GUEST, resolve_portal_mount


CID = "a" * 64
ENV_ID = "debian13"
APP_ID = "desktop-app"


class ExistingPortalBridgeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.service, self.runtime = service_in(self.root)
        self.service.uid = os.getuid()
        self.service.gid = os.getgid()
        self.record = self.service.create("debian13", ENV_ID, [])
        runtime = self.record["runtime"]
        self.name = runtime["container_name"]
        self.project = runtime["project"]
        runtime["container_id"] = CID
        self.desktop = {
            "env": {"DISPLAY": ":91", "XAUTHORITY": "/tmp/ml-xauthority"},
            "mounts": [{"host": str(self.root / "old-xauth"),
                        "container": "/tmp/ml-xauthority"}],
        }
        self.record["desktop"] = self.desktop
        self.service.store.save(self.record, None)
        live = self.runtime.containers[self.name]
        self.stopped = replace(live, container_id=CID)
        self.runtime.containers[self.name] = self.stopped
        self.directory = self.root.resolve() / ".lab" / "desktop" / ENV_ID

    def test_query_enable_and_disable_preserve_existing_mounts_and_identity(self):
        self.service.add_app(ENV_ID, APP_ID, "/usr/bin/example", ["/usr/bin/example"],
                             on_demand=True)
        before_launch = self.service.store.load_launch(APP_ID)
        events = list(self.runtime.events)
        self.assertFalse(self.service.configure_portal_bridge(ENV_ID))
        self.assertTrue(self.service.configure_portal_bridge(ENV_ID, "enable"))
        self.assertTrue(self.service.configure_portal_bridge(ENV_ID, "enable"))
        saved = self.service.store.load(ENV_ID)
        self.assertEqual(saved["desktop"], dict(self.desktop, portal_proxy={
            "directory": str(self.directory), "container": PORTAL_GUEST, "managed_mount": True}))
        self.assertEqual(saved["runtime"], self.record["runtime"])
        self.assertEqual(saved["environment_uuid"], self.record["environment_uuid"])
        self.assertEqual(saved["generation"], self.record["generation"])
        self.assertEqual(self.service.store.load_launch(APP_ID), before_launch)
        self.service.set_app_file_chooser(APP_ID, "gtk-native-portal")
        with self.assertRaisesRegex(UnifierError, "app file-chooser APP native"):
            self.service.configure_portal_bridge(ENV_ID, "disable")
        self.service.set_app_file_chooser(APP_ID, "native")
        self.assertFalse(self.service.configure_portal_bridge(ENV_ID, "disable"))
        self.assertEqual(self.service.store.load(ENV_ID)["desktop"], self.desktop)
        self.assertEqual(self.directory.stat().st_mode & 0o777, 0o700)
        self.assertEqual(self.runtime.events, events)

    def test_refuses_running_wrong_identity_conflicting_mount_and_stale_bridge(self):
        for invalid, message in (
            (replace(self.stopped, running=True, pid=50, status="running"), "stopped container"),
            (replace(self.stopped, container_id="b" * 64), "container identity"),
            (replace(self.stopped, mounts=(("/another", PORTAL_GUEST, True, "bind"),)), "mount"),
            (replace(self.stopped, mounts=(("/another", "/run", True, "bind"),)), "mount"),
        ):
            with self.subTest(container=invalid), self.assertRaisesRegex(UnifierError, message):
                self.runtime.containers[self.name] = invalid
                self.service.configure_portal_bridge(ENV_ID, "enable")
            self.assertFalse(self.directory.exists())
        self.runtime.containers[self.name] = self.stopped
        group = LaunchGroup(self.service, ENV_ID)
        group.save(dict(group.read(), bridge={"pid": 999}))
        with self.assertRaisesRegex(UnifierError, "bridge state"):
            self.service.configure_portal_bridge(ENV_ID, "enable")

    def test_symlink_is_rejected_without_modifying_its_target(self):
        self.directory.parent.mkdir()
        unrelated = self.root / "unrelated"
        unrelated.mkdir(mode=0o755)
        unrelated.chmod(0o755)
        self.directory.symlink_to(unrelated, target_is_directory=True)
        with self.assertRaises(UnifierError):
            self.service.configure_portal_bridge(ENV_ID, "enable")
        self.assertEqual(unrelated.stat().st_mode & 0o777, 0o755)

    def test_static_bridge_and_cli(self):
        parser = build_parser()
        for mode in (None, "enable", "disable"):
            arguments = ["env", "portal-bridge", ENV_ID] + ([mode] if mode else [])
            self.assertEqual(parser.parse_args(arguments).mode, mode)
        self.desktop["portal_proxy"] = {"directory": str(self.directory), "container": PORTAL_GUEST}
        self.service.store.save(self.record, None)
        self.assertTrue(self.service.configure_portal_bridge(ENV_ID))
        self.assertTrue(self.service.configure_portal_bridge(ENV_ID, "enable"))
        with self.assertRaisesRegex(UnifierError, "mounted at creation"):
            self.service.configure_portal_bridge(ENV_ID, "disable")
        self.assertEqual(self.service.store.load(ENV_ID)["desktop"], self.desktop)

    def test_cold_start_and_live_mount_proof_for_portal_app(self):
        self.service.add_app(ENV_ID, APP_ID, "/usr/bin/example", ["/usr/bin/example"],
                             on_demand=True)
        self.service.configure_portal_bridge(ENV_ID, "enable")
        self.service.set_app_file_chooser(APP_ID, "gtk-portal")
        launch = self.service.show_app(APP_ID)
        self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", launch["env"])
        mount = resolve_portal_mount(self.directory, self.service.uid)
        stopped_record = self.service.store.load(ENV_ID)
        group = LaunchGroup(self.service, ENV_ID)
        native_start = self.runtime.start
        bridge = {"pid": 999, "socket": str(self.directory / "bus")}

        def start(target, *, portal_mount, **options):
            native_start(self.name, **options)

        with (patch.object(self.runtime, "start", side_effect=start) as runtime_start,
              patch("unifier.on_demand.start_bridge", return_value=bridge) as start_bridge,
              patch("unifier.on_demand.preflight_picker"),
              patch("unifier.service.preflight_picker"),
              patch("unifier.service.verify_active_portal_mount") as verify):
            group.acquire(APP_ID)
            runtime_start.assert_called_once_with(CID, project=self.project, env_id=ENV_ID,
                                                  portal_mount=mount)
            start_bridge.assert_called_once_with(stopped_record, None, file_delivery=True, mounts=())
            self.assertEqual(group.read()["bridge"], bridge)
            prepared = self.service._prepare_app_execution(APP_ID, ["/usr/bin/example"])
            verify.assert_called_once_with(50, mount)
            self.assertEqual(prepared[-1]["DBUS_SESSION_BUS_ADDRESS"],
                             f"unix:path={PORTAL_GUEST}/files-bus")
            self.assertEqual(prepared[-1]["GTK_USE_PORTAL"], "1")
            self.assertNotIn(MODE_KEY, prepared[-1])
            self.assertEqual(prepared[-1]["XAUTHORITY"], "/tmp/ml-xauthority")
            verify.side_effect = ValueError("unexpected mount source")
            with self.assertRaisesRegex(UnifierError, "active Portal bridge"):
                self.service._prepare_app_execution(APP_ID, ["/usr/bin/example"])
            self.service.set_app_file_chooser(APP_ID, "native")
            native = self.service._prepare_app_execution(APP_ID, ["/usr/bin/example"])
            self.assertNotIn("GTK_USE_PORTAL", native[-1])
            self.assertNotIn("DBUS_SESSION_BUS_ADDRESS", native[-1])
            self.assertEqual(runtime_start.call_count, 1)

    def test_managed_bridge_skips_xauthority_copy(self):
        self.service.configure_portal_bridge(ENV_ID, "enable")
        record = self.service.store.load(ENV_ID)
        bridge = {"socket": str(self.directory / "bus"),
                  "environment_identity": list((record["environment_uuid"], record["generation"]))}
        # The reuse path needs no real proxy, session bus or Xauthority file.
        with (patch("unifier.on_demand._bridge_directory"),
              patch("unifier.on_demand._bridge_alive", return_value=True),
              patch.dict(os.environ, {"XAUTHORITY": str(self.root / "absent")})):
            self.assertIs(_start_bus_bridge(record, bridge), bridge)
        self.assertFalse((self.directory / "Xauthority").exists())
