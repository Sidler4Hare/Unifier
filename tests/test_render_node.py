"""Offline contracts for an explicit render bind on an existing container."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_env import service_in
from unifier.cli import build_parser
from unifier.errors import UnifierError
from unifier.on_demand import LaunchGroup
from unifier.render_node import (
    RenderNode, resolve_render_node, rewrite_spec, start_with_render_node,
    validate_saved_mounts,
)
from unifier.wayland import WaylandRefresh


DEVICE_PATH = "/dev/dri/renderD128"
CID = "a" * 64
RDEV = os.makedev(226, 128)
NODE = RenderNode(DEVICE_PATH, 1, 3, RDEV, (1, 1), (1, 2))


def oci_spec():
    return {
        "linux": {"cgroupsPath": f"user.slice:libpod:{CID}"},
        "annotations": {"io.container.manager": "libpod"},
        "mounts": [{"type": "tmpfs", "source": "tmpfs", "destination": "/dev"}],
    }


class RenderNodeTests(unittest.TestCase):
    def test_canonical_character_device_and_host_access(self):
        def lstat(path):
            inode = {"/dev": 1, "/dev/dri": 2, DEVICE_PATH: 3}[path]
            return SimpleNamespace(st_ino=inode, st_dev=1, st_rdev=RDEV,
                                   st_mode=stat.S_IFCHR if inode == 3 else stat.S_IFDIR)

        with (patch("unifier.render_node.os.lstat", side_effect=lstat) as inspect,
              patch("unifier.render_node.os.access", return_value=True) as access):
            self.assertEqual(resolve_render_node(DEVICE_PATH), NODE)
            access.assert_called_once_with(DEVICE_PATH, os.R_OK | os.W_OK,
                                           effective_ids=True, follow_symlinks=False)
            for invalid in ("/dev/dri/card0", "/dev/dri/renderD0128", "/dev/dri/renderD127",
                            "/dev/dri/../dri/renderD128", "/dev/dri/renderD128/"):
                with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "exact /dev/dri"):
                    resolve_render_node(invalid)
            access.return_value = False
            with self.assertRaisesRegex(ValueError, "read/write access"):
                resolve_render_node(DEVICE_PATH)
            for mode, rdev in ((stat.S_IFLNK, RDEV), (stat.S_IFCHR, os.makedev(226, 129)),
                               (stat.S_IFCHR, os.makedev(225, 128))):
                inspect.side_effect = lambda path: (SimpleNamespace(
                    st_mode=mode, st_rdev=rdev) if path == DEVICE_PATH else lstat(path))
                with self.assertRaisesRegex(ValueError, "matching its minor"):
                    resolve_render_node(DEVICE_PATH)

    def test_exact_oci_bind_with_wayland_refresh_and_single_hook(self):
        old = Path("/run/user/1000/wayland-0")
        source = Path("/run/user/1000/wayland-1")
        refresh = WaylandRefresh(old, source, 8, 9)
        spec = oci_spec()
        spec["mounts"].append({"type": "bind", "source": str(old), "destination": str(old)})
        original = deepcopy(spec)
        with (patch("unifier.render_node._identity", return_value=NODE),
              patch("unifier.wayland._socket", return_value=SimpleNamespace(st_dev=8, st_ino=9))):
            rewrite_spec(spec, CID, NODE, refresh)
        self.assertEqual(spec["mounts"], [
            original["mounts"][0], dict(original["mounts"][1], source=str(source)),
            {"type": "bind", "source": DEVICE_PATH, "destination": DEVICE_PATH,
             "options": ["slave", "nosuid", "noexec", "rw", "rbind"]},
        ])
        self.assertEqual(spec["linux"], original["linux"])
        calls = []

        def start(argv, **kwargs):
            self.assertEqual(argv[:2], ["podman", "--module"])
            self.assertEqual(argv[3:], ["start", CID])
            module = Path(argv[2])
            hooks = list(module.parent.glob("*.json"))
            self.assertEqual(len(hooks), 1)
            hook = json.loads(hooks[0].read_text())
            self.assertEqual(hook["stages"], ["precreate"])
            args = hook["hook"]["args"]
            self.assertEqual(args[1:2], ["-I"])
            self.assertEqual(args[3], CID)
            self.assertEqual(json.loads(args[4])["path"], DEVICE_PATH)
            self.assertEqual(json.loads(args[5])["source"], str(source))
            calls.append(module)

        with patch.dict(os.environ, {"CONTAINERS_CONF_OVERRIDE": ""}):
            start_with_render_node(start, CID, NODE, refresh)
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].parent.exists())

    def test_reject_wrong_container_changed_identity_and_existing_mounts(self):
        for target in ("/dev/dri", "//dev/dri", DEVICE_PATH, "/dev/dri/card0"):
            spec = oci_spec()
            spec["mounts"].append({"type": "bind", "source": target, "destination": target})
            with self.subTest(target=target), self.assertRaisesRegex(ValueError, "conflicts"):
                rewrite_spec(spec, CID, NODE, None)
        with self.assertRaisesRegex(ValueError, "expected Podman container"):
            rewrite_spec(oci_spec(), "b" * 64, NODE, None)
        with patch("unifier.render_node._identity", return_value=replace(NODE, inode=4)):
            with self.assertRaisesRegex(ValueError, "changed before container start"):
                rewrite_spec(oci_spec(), CID, NODE, None)
        mount = (DEVICE_PATH, DEVICE_PATH, True, "bind")
        validate_saved_mounts((mount,), DEVICE_PATH)
        with self.assertRaisesRegex(ValueError, "conflicting DRM mount"):
            validate_saved_mounts((mount, mount), DEVICE_PATH)
        with self.assertRaisesRegex(ValueError, "conflicting DRM mount"):
            validate_saved_mounts((("/dev/other", DEVICE_PATH, True, "bind"),), DEVICE_PATH)

    def test_configuration_keeps_existing_environment_identity_and_rejects_leases(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, fake = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            env_id = record["id"]
            name = record["runtime"]["container_name"]
            record["desktop"] = {"env": {"DISPLAY": ":99"}}
            record["runtime"]["container_id"] = CID
            service.store.save(record, None)
            fake.containers[name] = replace(fake.containers[name], container_id=CID)
            before = len(fake.events)
            with patch("unifier.service.resolve_render_node", return_value=NODE):
                stopped = fake.containers[name]
                fake.containers[name] = replace(stopped, device_destinations=(DEVICE_PATH,))
                with self.assertRaisesRegex(UnifierError, "native DRM device"):
                    service.configure_render_node(env_id, DEVICE_PATH)
                self.assertNotIn("render_node", service.store.load(env_id)["runtime"])
                fake.containers[name] = stopped
                service.configure_render_node(env_id, DEVICE_PATH)
                saved = service.store.load(env_id)
                self.assertEqual(saved["runtime"], dict(record["runtime"], render_node=DEVICE_PATH))
                for field in ("environment_uuid", "generation", "writable_revision"):
                    self.assertEqual(saved[field], record[field])
                fake.containers[name] = replace(
                    fake.containers[name], mounts=((DEVICE_PATH, DEVICE_PATH, True, "bind"),))
                group = LaunchGroup(service, env_id)
                group.save(dict(group.read(), leases={"token": {"kind": "application"}}))
                with self.assertRaisesRegex(UnifierError, "active leases"):
                    service.configure_render_node(env_id, None)
                group.save(dict(group.read(), leases={}))
                fake.containers[name] = replace(fake.containers[name], running=True, status="running")
                with self.assertRaisesRegex(UnifierError, "stopped container"):
                    service.configure_render_node(env_id, None)
                fake.containers[name] = replace(fake.containers[name], running=False, status="exited")
                service.configure_render_node(env_id, None)
            self.assertEqual(service.store.load(env_id)["runtime"], record["runtime"])
            self.assertEqual(len(fake.events), before)

    def test_start_passes_render_node_only_while_configured(self):
        with tempfile.TemporaryDirectory() as temporary:
            service, fake = service_in(Path(temporary))
            record = service.create("debian13", None, [])
            env_id = record["id"]
            runtime = record["runtime"]
            name = runtime["container_name"]
            record["desktop"] = {"env": {"DISPLAY": ":99"}}
            runtime["container_id"] = CID
            service.store.save(record, None)
            fake.containers[name] = replace(fake.containers[name], container_id=CID)
            native_start = fake.start

            def start(target, *, render_node=None, **options):
                native_start(name, **options)

            with (patch("unifier.service.resolve_render_node", return_value=NODE),
                  patch.object(fake, "start", side_effect=start) as runtime_start):
                service.configure_render_node(env_id, DEVICE_PATH)
                service.start(env_id)
                runtime_start.assert_called_once_with(CID, project=runtime["project"],
                                                      env_id=env_id, render_node=NODE)
                service.stop(env_id)
                service.configure_render_node(env_id, None)
                runtime_start.reset_mock()
                service.start(env_id)
                runtime_start.assert_called_once_with(name, project=runtime["project"],
                                                      env_id=env_id)

    def test_cli_requires_path_or_clear(self):
        parser = build_parser()
        base = ["env", "render-node", "debian13"]
        self.assertEqual(parser.parse_args([*base, DEVICE_PATH]).path, DEVICE_PATH)
        self.assertIsNone(parser.parse_args([*base, "--clear"]).path)
