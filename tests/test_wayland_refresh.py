"""Regress a local Debian package install after a Wayland session change."""
from contextlib import closing
from dataclasses import replace
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from test_packages import make_env, package_service
from unifier.runtime import ExecResult, PodmanRuntime
from unifier.wayland import plan_refresh


class WaylandRefreshTests(unittest.TestCase):
    def session(self, directory):
        old, source = directory / "wayland-0", directory / "wayland-1"
        desktop = {
            "env": {"WAYLAND_DISPLAY": old.name, "XDG_RUNTIME_DIR": str(directory)},
            "mounts": [{"host": str(old), "container": str(old)}],
        }
        options = {"uid": os.getuid(), "runtime_directory": directory,
                   "mounts": ((str(old), str(old), True, "bind"),),
                   "environ": {"WAYLAND_DISPLAY": source.name, "XDG_RUNTIME_DIR": str(directory)}}
        return old, source, desktop, options

    def test_package_probe_and_install_both_refresh_the_same_instance(self):
        with tempfile.TemporaryDirectory(prefix="uw-") as temporary:
            directory = Path(temporary)
            old, source, desktop, options = self.session(directory)
            with closing(socket.socket(socket.AF_UNIX)) as compositor:
                compositor.bind(str(source))
                service, fake = package_service(directory)
                make_env(service, fake, "debian13", "debian13")
                record = service.store.load("debian13")
                name = record["runtime"]["container_name"]
                container_id = "a" * 64
                record["runtime"]["container_id"] = container_id
                record["desktop"] = desktop
                service.store.save(record, None)
                fake.containers[name] = replace(
                    fake.containers[name], container_id=container_id,
                    mounts=((str(old), str(old), True, "bind"),))
                original_mounts = [
                    {"type": "bind", "source": str(old), "destination": str(old), "options": ["rbind"]},
                    {"type": "bind", "source": "/host/private", "destination": "/run/multilinux-portal"},
                ]
                config = {"mounts": original_mounts, "process": {"env": ["WAYLAND_DISPLAY=wayland-0"]}}
                calls = []
                native_start = fake.start
                answer = fake._answer
                incoming = "/tmp/ml-incoming/qq.deb"

                def answer_qq(info, argv):
                    if argv == ["dpkg-deb", "-f", incoming, "Package"]:
                        return ExecResult(0, "linuxqq\n", "")
                    if argv == ["apt-get", "install", "-y", incoming]:
                        fake.installed.setdefault(info.env_id, set()).add("linuxqq")
                    return answer(info, argv)

                def run(argv, *, capture, timeout):
                    self.assertEqual(argv[:2], ["podman", "--module"])
                    self.assertEqual(argv[3:], ["start", container_id])
                    module = Path(argv[2])
                    calls.append(module)
                    hook = json.loads(next(module.parent.glob("*.json")).read_text())
                    result = subprocess.run(hook["hook"]["args"], input=json.dumps(config),
                                            text=True, capture_output=True, timeout=5, check=True)
                    refreshed = json.loads(result.stdout)
                    self.assertEqual(refreshed["mounts"], [
                        dict(original_mounts[0], source=str(source)), original_mounts[1]])
                    self.assertEqual(refreshed["process"], config["process"])
                    native_start(name, project=record["runtime"]["project"], env_id="debian13")
                    # Inspect still sees this effective mount after stop, but
                    # the next start is generated from the original config.
                    fake.containers[name] = replace(
                        fake.containers[name], mounts=((str(source), str(old), True, "bind"),))
                    return ExecResult(0, "", "")

                adapter = PodmanRuntime(runner=run, check_rootless=False)
                adapter.inspect = fake.inspect
                package = directory / "qq.deb"
                package.touch()
                private_session = {key: options[key] for key in ("environ", "runtime_directory")}
                with (patch("unifier.service.plan_refresh",
                            side_effect=lambda desktop, **kwargs: plan_refresh(
                                desktop, mounts=kwargs["mounts"], uid=os.getuid(), **private_session)),
                      patch.object(fake, "start", side_effect=adapter.start),
                      patch.object(fake, "_answer", side_effect=answer_qq)):
                    package_name = service.local_package_name("debian13", str(package))
                    self.assertEqual(package_name, "linuxqq")
                    self.assertFalse(fake.inspect(container_id).running)
                    self.assertEqual(fake.inspect(container_id).mounts[0][:2], (str(source), str(old)))
                    txn = service.install_local("debian13", str(package), package_name=package_name)
                    self.assertEqual(txn["phase"], "completed", txn["native_error"])
                    self.assertIn("linuxqq", fake.installed["debian13"])

                self.assertEqual(len(calls), 2)
                self.assertTrue(all(not path.parent.exists() for path in calls))
                self.assertFalse(fake.inspect(container_id).running)
                self.assertEqual(service.store.load("debian13")["runtime"]["container_id"], container_id)
                self.assertFalse(old.exists())

    def test_missing_current_socket_fails_and_current_original_is_a_noop(self):
        with tempfile.TemporaryDirectory(prefix="uw-") as temporary:
            old, source, desktop, options = self.session(Path(temporary))
            self.assertIsNone(plan_refresh(desktop, **dict(options, mounts=())))
            other_mount = (("/unrelated/wayland-1", str(old), True, "bind"),)
            self.assertIsNone(plan_refresh(desktop, **dict(options, mounts=other_mount)))
            with self.assertRaisesRegex(ValueError, "unavailable"):
                plan_refresh(desktop, **options)
            with closing(socket.socket(socket.AF_UNIX)) as compositor:
                compositor.bind(str(old))
                options["environ"]["WAYLAND_DISPLAY"] = old.name
                self.assertIsNone(plan_refresh(desktop, **options))
                source.symlink_to(old)
                options["environ"]["WAYLAND_DISPLAY"] = source.name
                with self.assertRaisesRegex(ValueError, "unavailable"):
                    plan_refresh(desktop, **options)
