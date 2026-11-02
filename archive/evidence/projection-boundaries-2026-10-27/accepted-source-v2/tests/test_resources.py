"""Offline contracts for cgroup, PSS, disk and summary parsing."""

import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux import cli
from multilinux.resources import (
    parse_kv_int,
    parse_pressure,
    parse_smaps_rollup,
    read_cgroup_path,
    read_cpu_frequency,
    read_thermal,
    ResourceProbe,
    summarize,
)
from tools.probe_resources import _phase_peak, filter_storage_sections


SMAPS = """\
55d2933e7000-7ffe953ec000 ---p 00000000 00:00 0                          [rollup]
Rss:                1628 kB
Pss:                1620 kB
Pss_Anon:            100 kB
Pss_File:           1520 kB
Shared_Clean:          8 kB
Shared_Dirty:          0 kB
Private_Clean:      1520 kB
Private_Dirty:       100 kB
Swap:                  4 kB
SwapPss:               2 kB
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


class View:
    def __init__(self, **overrides):
        self.value = {
            "id": "demo",
            "target": "debian13",
            "runtime_state": "running",
            "content_revision": 1,
            "package_inventory_sha256": "a" * 64,
            "base_image": {
                "manifest_digest": "sha256:" + "b" * 64,
                "locked_reference": "example.invalid/base@sha256:" + "b" * 64,
            },
            "writable_revision": "cid-demo",
            "kernel_matches_current_host": True,
            "resources": {"pid": 50, "conmon_pid": 52},
            "shared_mounts": [],
            "runtime": {"container_name": "ml-demo", "project": "/project"},
        }
        self.value.update(overrides)

    def __getitem__(self, key):
        return self.value[key]

    def get(self, key, default=None):
        return self.value.get(key, default)


class Service:
    def __init__(self, view):
        self.view = view

    def inspect(self, env_id):
        return self.view


class Parsing(unittest.TestCase):
    def test_pressure_smaps_and_integer_tables(self):
        pressure = parse_pressure(
            "some avg10=1.50 avg60=2.00 avg300=3.00 total=12345\n"
            "full avg10=0.00 avg60=0.00 avg300=0.00 total=99\n")
        self.assertEqual(pressure["some"], {
            "avg10": 1.5, "avg60": 2.0, "avg300": 3.0, "total_us": 12345})
        self.assertEqual(pressure["full"]["total_us"], 99)
        smaps = parse_smaps_rollup(SMAPS)
        self.assertEqual(smaps["rss_bytes"], 1628 * 1024)
        self.assertEqual(smaps["pss_bytes"], 1620 * 1024)
        self.assertEqual(smaps["swap_pss_bytes"], 2 * 1024)
        self.assertNotIn("KSM", smaps)
        self.assertEqual(parse_kv_int("anon 1024\nfile 2048\nbroken\n"), {
            "anon": 1024, "file": 2048})

    def test_summary_keeps_missing_values_out_and_uses_nearest_rank(self):
        samples = [
            {"cgroup": {"memory_current_bytes": value,
                        "memory_stat": {"anon": value // 2}}}
            for value in range(10)
        ]
        samples.append({"cgroup": {"memory_current_bytes": None}})
        result = summarize(samples)
        current = result["metrics"]["cgroup.memory_current_bytes"]
        self.assertEqual((current["count"], current["min"], current["max"]),
                         (10, 0, 9))
        self.assertEqual(current["p50"], 4)
        self.assertEqual(current["p95"], 9)
        self.assertNotIn("cgroup.memory_peak_bytes", result["metrics"])


class SampleContract(unittest.TestCase):
    def _probe(self, temporary: Path, view: View) -> ResourceProbe:
        proc = temporary / "proc"
        cgroup = temporary / "cgroup"
        _write(proc / "50" / "cgroup", "0::/libpod-cid-demo.scope/container\n")
        _write(proc / "50" / "comm", "ml-sleeper\n")
        _write(proc / "50" / "smaps_rollup", SMAPS)
        _write(proc / "52" / "comm", "conmon\n")
        _write(proc / "52" / "smaps_rollup", SMAPS.replace("1628", "300").replace("1620", "290"))
        _write(proc / "meminfo", "MemTotal: 8000000 kB\nMemAvailable: 4000000 kB\n")
        _write(proc / "pressure/memory", "some avg10=0.10 avg60=0.20 avg300=0.30 total=7\n")
        _write(proc / "loadavg", "0.50 0.40 0.30 1/100 1234\n")
        container = cgroup / "libpod-cid-demo.scope/container"
        _write(container / "cgroup.procs", "50\n")
        _write(container / "memory.current", "1000000\n")
        _write(container / "memory.peak", "2000000\n")
        _write(container / "memory.swap.current", "0\n")
        _write(container / "memory.stat", "anon 500000\nfile 100000\nkernel 50000\nslab 10000\n")
        _write(container / "memory.events", "low 0\nhigh 0\nmax 0\noom 0\noom_kill 0\n")
        _write(container / "memory.pressure", "some avg10=0.00 avg60=0.00 avg300=0.00 total=1\n")
        _write(container / "cpu.pressure", "some avg10=0.00 avg60=0.00 avg300=0.00 total=2\n")
        _write(container / "io.pressure", "some avg10=0.00 avg60=0.00 avg300=0.00 total=3\n")
        _write(container / "pids.current", "1\n")
        _write(container / "pids.peak", "3\n")
        _write(container / "memory.max", "max\n")
        _write(container / "memory.high", "max\n")
        _write(container / "cpu.max", "max 100000\n")
        return ResourceProbe(Service(view), proc_root=proc, cgroup_root=cgroup)

    def test_running_sample_reads_cgroup_processes_and_conmon(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = self._probe(Path(temporary), View())
            sample = probe.sample("demo", include_disk=False)
            self.assertEqual(sample["cgroup"]["memory_current_bytes"], 1000000)
            self.assertEqual(sample["cgroup"]["memory_stat"]["anon"], 500000)
            self.assertEqual(sample["cgroup"]["pids_current"], 1)
            self.assertTrue(sample["cgroup"]["container_id_matches"])
            self.assertEqual(sample["processes"]["process_count"], 1)
            self.assertEqual(sample["processes"]["pss_bytes"], 1620 * 1024)
            self.assertEqual(sample["runtime_processes"]["source"], "conmon_pid")
            self.assertEqual(sample["runtime_processes"]["pss_bytes"], 290 * 1024)
            self.assertEqual(sample["host"]["meminfo"]["MemAvailable_bytes"], 4000000 * 1024)
            self.assertEqual(sample["host"]["pressure"]["memory"]["some"]["total_us"], 7)
            self.assertNotIn("memory.swap.current", sample["cgroup"]["errors"])

    def test_missing_metric_is_an_error_not_a_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = self._probe(Path(temporary), View())
            cgroup = Path(temporary) / "cgroup/libpod-cid-demo.scope/container"
            (cgroup / "memory.current").unlink()
            sample = probe.sample("demo", include_disk=False)
            self.assertIsNone(sample["cgroup"]["memory_current_bytes"])
            self.assertIn("memory.current", sample["cgroup"]["errors"])

    def test_stopped_sample_has_no_cgroup(self):
        view = View(runtime_state="stopped", resources={"pid": 0, "conmon_pid": None})
        with tempfile.TemporaryDirectory() as temporary:
            probe = ResourceProbe(Service(view), proc_root=Path(temporary) / "proc",
                                  cgroup_root=Path(temporary) / "cgroup")
            sample = probe.sample("demo", include_disk=False)
            self.assertIsNone(sample["cgroup"])
            self.assertIsNone(sample["processes"])
            self.assertIn("stopped", sample["errors"][0])

    def test_peak_reset_reports_when_the_write_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            probe = self._probe(Path(temporary), View())
            cgroup = Path(temporary) / "cgroup/libpod-cid-demo.scope/container"
            result = probe.reset_peak("demo")
            self.assertTrue(result["ok"])
            self.assertEqual(int((cgroup / "memory.peak").read_text()), 0)
            _write(cgroup / "memory.peak", "1234\n")
            with mock.patch("multilinux.resources.os.write", return_value=1):
                result = probe.reset_peak("demo")
            self.assertFalse(result["ok"])
            self.assertIn("did not decrease", result["error"])

    def test_peak_reset_on_a_stopped_environment_is_refused(self):
        view = View(runtime_state="stopped", resources={"pid": 0, "conmon_pid": None})
        with tempfile.TemporaryDirectory() as temporary:
            probe = ResourceProbe(Service(view), proc_root=Path(temporary) / "proc",
                                  cgroup_root=Path(temporary) / "cgroup")
            result = probe.reset_peak("demo")
            self.assertFalse(result["ok"])
            self.assertIn("not running", result["error"])


class CommandLine(unittest.TestCase):
    def test_resource_commands_parse_without_touching_a_runtime(self):
        parser = cli.build_parser()
        sample = parser.parse_args(["res", "sample", "demo"])
        self.assertEqual((sample.func, sample.id, sample.output),
                         (cli.cmd_res_sample, "demo", None))
        series = parser.parse_args(
            ["res", "series", "demo", "--samples", "3", "--interval", "0.5",
             "--settle", "2", "--reset-peak", "--output", "out.json"])
        self.assertEqual(series.func, cli.cmd_res_series)
        self.assertEqual((series.samples, series.interval, series.settle,
                          series.reset_peak, series.output), (3, 0.5, 2.0, True, "out.json"))


class CgroupPath(unittest.TestCase):
    def test_zero_entry_and_missing_process_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            proc = Path(temporary) / "proc"
            cgroup = Path(temporary) / "cgroup"
            _write(proc / "7" / "cgroup", "0::/a/b\n")
            path, error = read_cgroup_path(7, proc_root=proc, cgroup_root=cgroup)
            self.assertEqual(path, cgroup / "a/b")
            self.assertIsNone(error)
            path, error = read_cgroup_path(8, proc_root=proc, cgroup_root=cgroup)
            self.assertIsNone(path)
            self.assertIsNotNone(error)


class StorageFilter(unittest.TestCase):
    def test_verbose_disk_rows_keep_only_this_image_and_container(self):
        text = """\
Images space usage:

REPOSITORY                    TAG      IMAGE ID      CREATED  SIZE      SHARED SIZE  UNIQUE SIZE  CONTAINERS
docker.io/library/debian      13       abc123def456  2 weeks  162.5MB   0B           162.5MB      1
docker.io/library/fedora      44       fed654cba321  2 weeks  382MB     10MB         372MB        0

Containers space usage:

CONTAINER ID  IMAGE         COMMAND   LOCAL VOLUMES  SIZE    CREATED  STATUS  NAMES
aaaabbbbcccc  abc123def456  infinity  1              590MB   1 hour   exited  ml-demo
ddddccccbbbb  fed654cba321  infinity  0              1MB     1 hour   running  other

Local Volumes space usage:

VOLUME NAME  LINKS  SIZE
"""
        selected = filter_storage_sections(
            text, "abc123def456" + "0" * 52, "ml-demo")
        images = selected["Images space usage:"]["rows"]
        containers = selected["Containers space usage:"]["rows"]
        self.assertEqual(len(images), 1)
        self.assertIn("abc123def456", images[0])
        self.assertEqual(len(containers), 1)
        self.assertIn("ml-demo", containers[0])
        self.assertNotIn("fed654cba321", images[0])
        self.assertEqual(selected["Local Volumes space usage:"]["rows"], [])


class HostControls(unittest.TestCase):
    def test_frequency_and_thermal_keep_missing_values_empty(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write(root / "devices/system/cpu/cpu0/cpufreq/scaling_cur_freq", "1600000\n")
            _write(root / "devices/system/cpu/cpu1/cpufreq/scaling_cur_freq", "800000\n")
            _write(root / "class/thermal/thermal_zone0/type", "x86_pkg_temp\n")
            _write(root / "class/thermal/thermal_zone0/temp", "52000\n")
            freq = read_cpu_frequency(root)
            self.assertEqual((freq["min_khz"], freq["max_khz"], freq["sample_count"]),
                             (800000, 1600000, 2))
            self.assertEqual(freq["errors"], [])
            thermal = read_thermal(root)
            self.assertEqual(thermal["package_c"], 52.0)
            self.assertEqual(thermal["zones"][0]["type"], "x86_pkg_temp")
            missing = read_cpu_frequency(root / "absent")
            self.assertIsNone(missing["min_khz"])
            self.assertEqual(missing["khz"], [])
            self.assertTrue(missing["errors"])
            self.assertIsNone(read_thermal(root / "absent")["package_c"])


class PhasePeak(unittest.TestCase):
    def test_lifetime_peak_stays_separate_from_sampled_current(self):
        def sample(peak, current, pss):
            return {
                "cgroup": {
                    "memory_peak_bytes": peak,
                    "memory_current_bytes": current,
                    "memory_stat": {"anon": 1, "file": 2, "kernel": 3, "slab": 4},
                    "memory_pressure": {"some": {"avg10": 0.0}},
                    "cpu_pressure": {"some": {"avg10": 0.0}},
                },
                "processes": {"pss_bytes": pss},
                "host": {"meminfo": {"MemAvailable_bytes": 1000}},
            }

        sampler = mock.Mock(samples=[
            sample(50_000_000, 1_000_000, 2_000_000),
            sample(50_000_000, 5_700_000, 4_000_000),
        ], interval=0.5, errors=[])
        phase = _phase_peak(
            "update", {"ok": False}, sampler, None,
            {"disk": {"container": {"size_rw_bytes": 10}}},
            {"disk": {"container": {"size_rw_bytes": 12}}},
            6.5)
        self.assertEqual(phase["memory_peak_bytes"], 50_000_000)
        self.assertEqual(phase["memory_peak_first_bytes"], 50_000_000)
        self.assertEqual(phase["memory_current_max_bytes"], 5_700_000)
        self.assertEqual(phase["pss_max_bytes"], 4_000_000)
        self.assertIn("cgroup lifetime", phase["memory_peak_note"])


if __name__ == "__main__":
    unittest.main()
