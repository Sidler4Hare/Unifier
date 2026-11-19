"""Offline validation of one user-selected camera on an existing guest."""

from dataclasses import replace
import os
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from test_env import service_in
from unifier.camera_device import CameraDevice, resolve_camera
from unifier.cli import build_parser
from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup
from unifier.portal_mount import rewrite_spec


CID = "a" * 64
CAMERA = CameraDevice("/dev/video0", "b" * 64, 2, 3, os.makedev(81, 0))
METADATA = """DEVNAME=/dev/video0
SUBSYSTEM=video4linux
DEVPATH=/devices/pci/video4linux/video0
ID_V4L_CAPABILITIES=:capture:
ID_SERIAL=fixture
"""


class CameraTests(unittest.TestCase):
    def test_requires_exact_capture_node_and_read_write_access(self):
        details = SimpleNamespace(st_dev=2, st_ino=3, st_rdev=CAMERA.rdev, st_mode=stat.S_IFCHR)
        with (patch("unifier.camera_device.os.lstat", return_value=details) as lookup,
              patch("unifier.camera_device.os.access", return_value=True) as access,
              patch("unifier.camera_device.subprocess.run") as udev):
            udev.return_value.stdout = METADATA
            original = resolve_camera(CAMERA.path)
            self.assertEqual((original.device, original.inode, original.rdev), (2, 3, CAMERA.rdev))
            self.assertEqual(udev.call_args.args[0][-1], "--name=/dev/video0")
            udev.return_value.stdout = METADATA.replace("fixture", "replacement")
            self.assertNotEqual(resolve_camera(CAMERA.path).selection, original.selection)
            udev.return_value.stdout = METADATA.replace(":capture:", ":")
            with self.assertRaisesRegex(ValueError, "capture node"):
                resolve_camera(CAMERA.path)
            with self.assertRaisesRegex(ValueError, "exact /dev/videoN"):
                resolve_camera("/dev/v4l/by-id/link")
            access.return_value = False
            with self.assertRaisesRegex(ValueError, "read/write access"):
                resolve_camera(CAMERA.path)
            lookup.return_value = SimpleNamespace(st_mode=stat.S_IFLNK)
            with self.assertRaisesRegex(ValueError, "character device"):
                resolve_camera(CAMERA.path)

    def test_camera_uses_same_oci_start_without_portal_and_rejects_conflicts(self):
        def spec():
            return {
                "linux": {"cgroupsPath": f"user.slice:libpod:{CID}"},
                "annotations": {"io.container.manager": "libpod"},
                "mounts": [{"type": "tmpfs", "source": "tmpfs", "destination": "/dev"}],
            }

        with patch("unifier.camera_device._stat_device", return_value=(2, 3, CAMERA.rdev)) as lookup:
            config = spec()
            rewrite_spec(config, CID, None, None, None, camera=CAMERA)
            self.assertEqual(config["mounts"][-1], {
                "type": "bind", "source": CAMERA.path, "destination": CAMERA.path,
                "options": ["bind", "private", "nosuid", "noexec", "rw"],
            })
            with self.assertRaisesRegex(ValueError, "camera mount conflicts"):
                rewrite_spec(config, CID, None, None, None, camera=CAMERA)
            config = spec()
            config["linux"]["devices"] = [{"path": "/dev/video1"}]
            with self.assertRaisesRegex(ValueError, "native camera device"):
                rewrite_spec(config, CID, None, None, None, camera=CAMERA)
            lookup.return_value = (2, 4, CAMERA.rdev)
            with self.assertRaisesRegex(ValueError, "changed before container start"):
                rewrite_spec(spec(), CID, None, None, None, camera=CAMERA)

    def test_configure_start_and_clear_keep_the_existing_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, fake = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            name = record["runtime"]["container_name"]
            record["runtime"]["container_id"] = CID
            record["desktop"] = {"env": {"DISPLAY": ":99"}}
            service.store.save(record, None)
            fake.containers[name] = replace(fake.containers[name], container_id=CID, bind_mounts=())
            env_id = record["id"]
            group = LaunchGroup(service, env_id)
            native_start = fake.start

            def start(_target, *, camera=None, **kwargs):
                native_start(name, **kwargs)

            with (patch("unifier.service.resolve_camera", return_value=CAMERA) as resolve,
                  patch.object(fake, "start", side_effect=start) as runtime_start):
                group.save(dict(group.read(), leases={"token": {"kind": "application"}}))
                with self.assertRaisesRegex(UnifierError, "has leases"):
                    service.configure_camera(env_id, CAMERA.path)
                group.save(dict(group.read(), leases={}))
                service.configure_camera(env_id, CAMERA.path)
                saved = service.store.load(env_id)
                self.assertEqual(saved["runtime"]["camera"],
                                 {"path": CAMERA.path, "selection": CAMERA.selection})
                for key in ("environment_uuid", "generation", "writable_revision"):
                    self.assertEqual(saved[key], record[key])
                resolve.return_value = replace(CAMERA, selection="c" * 64)
                with self.assertRaisesRegex(UnifierError, "configured camera changed"):
                    service.start(env_id)
                runtime_start.assert_not_called()
                resolve.return_value = CAMERA
                service.start(env_id)
                runtime_start.assert_called_once_with(
                    CID, project=record["runtime"]["project"], env_id=env_id, camera=CAMERA)
                with self.assertRaisesRegex(UnifierError, "stopped container"):
                    service.configure_camera(env_id, None)
                service.stop(env_id)
                service.configure_camera(env_id, None)
                runtime_start.reset_mock()
                service.start(env_id)
                runtime_start.assert_called_once_with(
                    name, project=record["runtime"]["project"], env_id=env_id)

    def test_camera_cli_requires_an_explicit_selection_or_clear(self):
        parser = build_parser()
        args = ["env", "camera", "debian13"]
        self.assertEqual(parser.parse_args([*args, CAMERA.path]).path, CAMERA.path)
        self.assertTrue(parser.parse_args([*args, "--clear"]).clear)
