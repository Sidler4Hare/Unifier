"""Offline contracts for the private Portal bridge OCI bind."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from unifier.camera_device import CameraDevice
from unifier.portal_mount import (
    PORTAL_GUEST, resolve_portal_mount, rewrite_spec, start_with_portal_mount,
    validate_saved_portal_mounts, verify_active_portal_mount,
)
from unifier.render_node import RenderNode
from unifier.wayland import WaylandRefresh


CID = "a" * 64
NODE = RenderNode("/dev/dri/renderD128", 1, 3, os.makedev(226, 128), (1, 1), (1, 2))
OLD = Path("/run/user/1000/wayland-0")
SOURCE = OLD.with_name("wayland-1")
REFRESH = WaylandRefresh(OLD, SOURCE, 8, 9)


def spec():
    return {
        "linux": {"cgroupsPath": f"user.slice:libpod:{CID}"},
        "annotations": {"io.container.manager": "libpod"},
        "mounts": [
            {"type": "tmpfs", "source": "tmpfs", "destination": "/dev"},
            {"type": "bind", "source": str(OLD), "destination": str(OLD)},
            {"type": "bind", "source": "/tmp/.X11-unix", "destination": "/tmp/.X11-unix"},
        ],
    }


class PortalMountTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.guest_root = self.directory / "root"
        self.source = self.guest_root / PORTAL_GUEST.lstrip("/")
        self.source.mkdir(mode=0o700, parents=True)
        self.mount = resolve_portal_mount(self.source, os.getuid())

    def test_requires_canonical_private_directory_owned_by_host_user(self):
        symlink = self.source.with_name("link")
        symlink.symlink_to(self.source, target_is_directory=True)
        for path in (symlink, "relative", str(self.source) + "/",
                     str(self.source.parent) + "/./" + self.source.name):
            with self.subTest(path=path), self.assertRaises(ValueError):
                resolve_portal_mount(path, os.getuid())
        with self.assertRaisesRegex(ValueError, "owned"):
            resolve_portal_mount(self.source, os.getuid() + 1)
        self.source.chmod(0o750)
        with self.assertRaisesRegex(ValueError, "0700"):
            resolve_portal_mount(self.source, os.getuid())

    def test_saved_mount_only_allows_one_exact_expected_rw_bind(self):
        existing = (self.mount.path, PORTAL_GUEST, True, "bind")
        validate_saved_portal_mounts((existing,), self.source)
        validate_saved_portal_mounts(((self.mount.path, str(OLD), True, "bind"),))
        with self.assertRaisesRegex(ValueError, "conflicting"):
            validate_saved_portal_mounts((existing,))
        for mounts in (
            (existing, existing),
            ((self.mount.path, PORTAL_GUEST, False, "bind"),),
            ((self.mount.path, PORTAL_GUEST, True, "volume"),),
            (("/another/path", PORTAL_GUEST, True, "bind"),),
            ((self.mount.path, "//run", True, "bind"),),
            ((self.mount.path, PORTAL_GUEST + "/bus", True, "bind"),),
        ):
            with self.subTest(mounts=mounts), self.assertRaisesRegex(ValueError, "conflicting"):
                validate_saved_portal_mounts(mounts, self.source)

    def test_oci_composes_wayland_drm_and_portal_in_one_hook(self):
        config = spec()
        original = deepcopy(config)
        with (patch("unifier.render_node._identity", return_value=NODE),
              patch("unifier.wayland._socket", return_value=SimpleNamespace(st_dev=8, st_ino=9))):
            rewrite_spec(config, CID, self.mount, REFRESH, NODE)
        self.assertEqual(config["mounts"], [
            original["mounts"][0], dict(original["mounts"][1], source=str(SOURCE)),
            original["mounts"][2],
            {"type": "bind", "source": NODE.path, "destination": NODE.path,
             "options": ["slave", "nosuid", "noexec", "rw", "rbind"]},
            {"type": "bind", "source": self.mount.path, "destination": PORTAL_GUEST,
             "options": ["bind", "private", "rw", "nosuid", "nodev"]},
        ])
        self.assertEqual(config["linux"], original["linux"])
        calls = []

        def start(argv, **kwargs):
            self.assertEqual(argv[:2], ["podman", "--module"])
            self.assertEqual(argv[3:], ["start", CID])
            hook, = Path(argv[2]).parent.glob("*.json")
            definition = json.loads(hook.read_text())
            self.assertEqual(definition["stages"], ["precreate"])
            _, isolated, script, cid, mount, refresh, render = definition["hook"]["args"]
            self.assertEqual(isolated, "-I")
            self.assertEqual(Path(script).name, "portal_mount.py")
            self.assertEqual(cid, CID)
            self.assertEqual(json.loads(mount)["path"], self.mount.path)
            self.assertEqual(json.loads(refresh)["source"], str(SOURCE))
            self.assertEqual(json.loads(render)["path"], NODE.path)
            calls.append(hook)

        with patch.dict(os.environ, {"CONTAINERS_CONF_OVERRIDE": ""}):
            start_with_portal_mount(start, CID, self.mount, REFRESH, NODE)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].exists())

    def test_oci_rejects_conflicts_and_changed_identity(self):
        for target in ("/", "/run", PORTAL_GUEST, PORTAL_GUEST + "/child"):
            config = spec()
            config["mounts"].append({"destination": target, "type": "tmpfs"})
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "conflicts"):
                rewrite_spec(config, CID, self.mount, None, None)
        with self.assertRaisesRegex(ValueError, "expected Podman container"):
            rewrite_spec(spec(), "b" * 64, self.mount, None, None)
        with self.assertRaisesRegex(ValueError, "identity changed"):
            rewrite_spec(spec(), CID, replace(self.mount, inode=self.mount.inode + 1), None, None)

    def test_camera_composes_with_the_wayland_render_and_portal_mounts(self):
        camera = CameraDevice("/dev/video0", "b" * 64, 2, 3, os.makedev(81, 0))
        config = spec()
        with (patch("unifier.render_node._identity", return_value=NODE),
              patch("unifier.wayland._socket", return_value=SimpleNamespace(st_dev=8, st_ino=9)),
              patch("unifier.camera_device._stat_device", return_value=(2, 3, camera.rdev))):
            rewrite_spec(config, CID, self.mount, REFRESH, NODE, camera=camera)
        self.assertEqual(config["mounts"][1]["source"], str(SOURCE))
        self.assertEqual([mount["destination"] for mount in config["mounts"][3:]],
                         [NODE.path, camera.path, PORTAL_GUEST])

        def start(argv, **_kwargs):
            self.assertEqual(argv[3:], ["start", CID])
            hook, = Path(argv[2]).parent.glob("*.json")
            args = json.loads(hook.read_text())["hook"]["args"]
            self.assertEqual(json.loads(args[-1])["path"], camera.path)

        with patch.dict(os.environ, {"CONTAINERS_CONF_OVERRIDE": ""}):
            start_with_portal_mount(start, CID, self.mount, REFRESH, NODE, camera=camera)

    def test_active_mount_requires_writable_unshadowed_matching_inode(self):
        proc_dir = self.directory / "proc"
        proc_dir.mkdir()
        (proc_dir / "root").symlink_to(self.guest_root, target_is_directory=True)
        mountinfo = proc_dir / "mountinfo"
        actual_open = os.open

        def probe(path, flags, *args, dir_fd=None):
            if path == "/proc/9000":
                path = proc_dir
            return actual_open(path, flags, *args, dir_fd=dir_fd)

        root_record = "1 0 0:42 / / rw - overlay overlay rw\n"
        portal_record = f"2 1 0:42 /private {PORTAL_GUEST} rw,relatime - ext4 /dev/disk rw\n"
        with patch("unifier.portal_mount.os.open", side_effect=probe):
            mountinfo.write_text(root_record + portal_record)
            verify_active_portal_mount(9000, self.mount)
            for payload in (
                root_record,
                root_record + portal_record.rstrip("\n"),
                root_record + portal_record * 2,
                root_record + portal_record.replace("rw,relatime", "ro,relatime"),
                root_record + portal_record.replace("/dev/disk rw", "/dev/disk ro"),
                root_record + portal_record + portal_record.replace(
                    PORTAL_GUEST, PORTAL_GUEST + "/nested\\040directory"),
                "a" * (1024 * 1024 + 1),
            ):
                with self.subTest(size=len(payload)), self.assertRaises(ValueError):
                    mountinfo.write_text(payload)
                    verify_active_portal_mount(9000, self.mount)
            mountinfo.write_text(root_record + portal_record)
            alternate = self.source.rename(self.source.with_name("alternate"))
            self.source.mkdir(mode=0o700)
            with self.assertRaisesRegex(ValueError, "active Portal mount source identity"):
                verify_active_portal_mount(9000, resolve_portal_mount(alternate, os.getuid()))
