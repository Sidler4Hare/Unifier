"""Rootless Podman checks for resource sampling. Opt in with MULTILINUX_INTEGRATION=1."""

import os
from pathlib import Path
import shutil
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.project import load_project
from multilinux.resources import ResourceProbe
from multilinux.runtime import PodmanRuntime, container_name, project_label
from multilinux.service import EnvironmentService, now


ENV_ID = "m4resources"


def _reset(service, runtime):
    project = project_label(service.root)
    name = container_name(service.root, ENV_ID)
    if service.store.exists(ENV_ID):
        service.remove(ENV_ID)
        return
    info = runtime.inspect(name)
    if info is not None and info.project == project and info.env_id == ENV_ID:
        runtime.remove(name, project=project, env_id=ENV_ID)


@unittest.skipUnless(os.environ.get("MULTILINUX_INTEGRATION") == "1",
                     "set MULTILINUX_INTEGRATION=1 to use rootless Podman")
class ResourceSampling(unittest.TestCase):
    def test_running_idle_and_stopped_states_are_sampled(self):
        config, lock, root = load_project()
        runtime = PodmanRuntime()
        service = EnvironmentService(root, runtime, config, lock)
        share = Path(tempfile.mkdtemp(prefix="ml-m4res-"))
        probe = ResourceProbe(service)
        evidence = {
            "schema_version": 1,
            "started_at": now(),
            "host_kernel": os.uname().release,
            "scope": "Resource readability and one short idle series; not a cross-host benchmark.",
            "environment_id": ENV_ID,
            "target": "debian13",
            "states": {},
            "passed": False,
        }
        cleanup_errors = []
        try:
            _reset(service, runtime)
            service.create("debian13", ENV_ID, [str(share)])
            stopped = probe.sample(ENV_ID)
            self.assertEqual(stopped["runtime_state"], "stopped")
            self.assertIsNone(stopped["cgroup"])
            self.assertTrue(stopped["errors"])
            self.assertGreater(stopped["disk"]["container"]["size_rw_bytes"], 0)
            self.assertGreater(stopped["disk"]["image"]["size_bytes"], 0)
            evidence["states"]["stopped_before_start"] = {
                "cgroup": None,
                "container_size_rw_bytes": stopped["disk"]["container"]["size_rw_bytes"],
                "image_size_bytes": stopped["disk"]["image"]["size_bytes"],
                "errors": stopped["errors"],
            }

            service.start(ENV_ID)
            time.sleep(2)
            sample = probe.sample(ENV_ID)
            self.assertEqual(sample["runtime_state"], "running")
            self.assertIsNotNone(sample["cgroup"])
            self.assertTrue(sample["cgroup"]["container_id_matches"])
            self.assertGreater(sample["cgroup"]["memory_current_bytes"], 0)
            self.assertGreaterEqual(sample["cgroup"]["memory_peak_bytes"],
                                    sample["cgroup"]["memory_current_bytes"])
            self.assertGreater(sample["processes"]["pss_bytes"], 0)
            self.assertGreaterEqual(sample["processes"]["process_count"], 1)
            self.assertIsNotNone(sample["runtime_processes"])
            self.assertGreater(sample["runtime_processes"]["pss_bytes"], 0)
            self.assertGreater(sample["disk"]["container"]["size_rw_bytes"], 0)
            reset = probe.reset_peak(ENV_ID)
            evidence["states"]["idle"] = {
                "cgroup": sample["cgroup"],
                "pid": sample["pid"],
                "conmon_pid": sample["conmon_pid"],
                "processes": {
                    key: sample["processes"][key]
                    for key in ("process_count", "pss_bytes", "rss_bytes", "complete")
                },
                "runtime_processes": {
                    key: sample["runtime_processes"][key]
                    for key in ("process_count", "pss_bytes", "complete", "source")
                },
                "disk": sample["disk"],
                "peak_reset": reset,
            }
            series = probe.series(ENV_ID, samples=3, interval=0.2, settle=0.3)
            self.assertEqual(series["summary"]["sample_count"], 3)
            self.assertIn("cgroup.memory_current_bytes", series["summary"]["metrics"])
            self.assertIn("processes.pss_bytes", series["summary"]["metrics"])
            evidence["states"]["idle_series"] = series

            service.stop(ENV_ID)
            after = probe.sample(ENV_ID)
            self.assertEqual(after["runtime_state"], "stopped")
            self.assertIsNone(after["cgroup"])
            self.assertGreater(after["disk"]["container"]["size_rw_bytes"], 0)
            evidence["states"]["stopped_after_stop"] = {
                "cgroup": None,
                "container_size_rw_bytes": after["disk"]["container"]["size_rw_bytes"],
                "errors": after["errors"],
            }
            evidence["passed"] = True
            evidence["finished_at"] = now()
        finally:
            try:
                _reset(service, runtime)
            except Exception as exc:
                cleanup_errors.append(f"{type(exc).__name__}: {exc}")
            shutil.rmtree(share, ignore_errors=True)
        self.assertEqual(cleanup_errors, [])
        self.assertFalse(service.store.exists(ENV_ID))
        self.assertIsNone(runtime.inspect(container_name(root, ENV_ID)))
        evidence["container_removed"] = True
        evidence["settings"] = {
            "rootless": True,
            "network": "none",
            "full_init": False,
            "pss_source": "/proc/<pid>/smaps_rollup for the container cgroup and conmon",
        }
        if os.environ.get("MULTILINUX_EVIDENCE") == "1":
            from tools import lab
            lab.save_json(root / "evidence/m4-resources-integration-2026-10-21.json", evidence)


if __name__ == "__main__":
    unittest.main()
