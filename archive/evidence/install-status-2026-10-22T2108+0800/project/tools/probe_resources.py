#!/usr/bin/env python3
"""Run the M4 resource experiments on the prepared Fedora host.

Subcommands:
  states        stopped, settled idle, active workload, post-active idle, stopped
  same-binary   host direct, direct podman exec, and ml env run with one static probe
  update-peak   install and update with cgroup/PSS/disk sampling

The experiments use the locked images, serial environments, and isolated
project state. Containers and temporary shares are removed in finally blocks.
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.project import load_project
from multilinux.resources import ResourceProbe, host_snapshot, summarize
from multilinux.runtime import PodmanRuntime, container_name, project_label
from multilinux.service import EnvironmentService, now


TARGETS = ("debian13", "fedora44", "arch")
ENV_IDS = {
    "states": "m4-{target}",
    "binary": "m4bin",
    "update": "m4upd-{target}",
}
PROBE_SOURCE = ROOT / "fixtures/resource_probe.c"
PROBE_FLAGS = (
    "-O2", "-static", "-nostdlib", "-ffreestanding", "-fno-builtin",
    "-fno-stack-protector", "-fno-asynchronous-unwind-tables",
    "-fno-unwind-tables", "-no-pie", "-Wl,--build-id=none",
)
INPUT = bytes((index * 37 + 11) % 251 for index in range(4096))
SHORT_SAMPLES = 30
LONG_SAMPLES = 7
LONG_ITERATIONS = 20_000_000


def log(message):
    print(message, file=sys.stderr, flush=True)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _text(path):
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _stats(values):
    numbers = sorted(value for value in values
                     if isinstance(value, (int, float)) and not isinstance(value, bool))
    if not numbers:
        return None
    count = len(numbers)
    return {
        "count": count,
        "min": numbers[0],
        "p50": numbers[max(1, math.ceil(0.50 * count)) - 1],
        "p95": numbers[max(1, math.ceil(0.95 * count)) - 1],
        "max": numbers[-1],
        "mean": round(sum(numbers) / count, 3),
    }


def _run(argv, *, cwd=None, timeout=120):
    started = time.monotonic_ns()
    environment = {**os.environ, "LC_ALL": "C"}
    try:
        completed = subprocess.run(
            argv, cwd=str(cwd or ROOT), capture_output=True, text=True,
            timeout=timeout, env=environment, check=False)
    except subprocess.TimeoutExpired as exc:
        wall = time.monotonic_ns() - started
        return {
            "argv": list(argv),
            "exit_code": None,
            "wall_ns": wall,
            "stdout": exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or ""),
            "stderr": exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or ""),
            "timed_out": True,
            "error": f"timed out after {timeout}s",
        }
    except OSError as exc:
        wall = time.monotonic_ns() - started
        return {
            "argv": list(argv),
            "exit_code": None,
            "wall_ns": wall,
            "stdout": "",
            "stderr": "",
            "timed_out": False,
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "argv": list(argv),
        "exit_code": completed.returncode,
        "wall_ns": time.monotonic_ns() - started,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "timed_out": False,
        "error": None,
    }


def _parse_pairs(line):
    pairs = {}
    for item in line.split()[1:]:
        key, separator, value = item.partition("=")
        if not separator:
            continue
        if key in {"bytes", "iterations"}:
            try:
                pairs[key] = int(value)
                continue
            except ValueError:
                pass
        pairs[key] = value
    return pairs


def parse_probe(stdout):
    parsed = {"ready": False, "result": None, "timing": None,
              "stdout_tail": (stdout or "")[-1000:]}
    for line in (stdout or "").splitlines():
        fields = line.split()
        if not fields:
            continue
        if fields[0] == "ready":
            parsed["ready"] = True
        elif fields[0] == "result":
            parsed["result"] = _parse_pairs(line)
        elif fields[0] == "timing":
            pairs = _parse_pairs(line)
            if isinstance(pairs.get("cpu_ns"), str):
                try:
                    pairs["cpu_ns"] = int(pairs["cpu_ns"])
                except ValueError:
                    pass
            parsed["timing"] = pairs
    return parsed


def probe_signature(parsed, mode):
    result = parsed.get("result") or {}
    if mode == "short":
        keys = ("bytes", "hash")
    elif mode == "burn":
        keys = ("bytes", "hash", "iterations", "work")
    else:
        keys = ("bytes", "hash")
    return tuple(result.get(key) for key in keys)


def _tracked_run(argv, *, mode, reference, label=None, sample=None, cwd=None, timeout=120):
    result = _run(argv, cwd=cwd, timeout=timeout)
    parsed = parse_probe(result["stdout"])
    signature = probe_signature(parsed, mode)
    return {
        "mode": label or mode,
        "probe_mode": mode,
        "sample": sample,
        "exit_code": result["exit_code"],
        "wall_ns": result["wall_ns"],
        "timed_out": result["timed_out"],
        "error": result["error"],
        "signature": list(signature),
        "signature_matches": result["exit_code"] == 0 and signature == reference,
        "cpu_ns": (parsed.get("timing") or {}).get("cpu_ns"),
        "ready": parsed["ready"],
        "stdout_tail": parsed["stdout_tail"],
        "stderr_tail": (result["stderr"] or "")[-800:],
    }


def _write_input(path):
    path.write_bytes(INPUT)
    return {
        "bytes": len(INPUT),
        "sha256": hashlib.sha256(INPUT).hexdigest(),
    }


def build_fixture(share):
    input_path = share / "input.bin"
    input_info = _write_input(input_path)
    binary = share / "resource_probe"
    argv = ["gcc", *PROBE_FLAGS, "-o", str(binary), str(PROBE_SOURCE)]
    build = _run(argv, timeout=120)
    if build["exit_code"] != 0:
        raise RuntimeError("probe build failed: " + (build["stderr"] or build["stdout"])[-2000:])
    payload = binary.read_bytes()
    if not payload.startswith(b"\x7fELF") or len(payload) < 1024:
        raise RuntimeError("probe build did not produce an ELF binary")
    host_run = _run([str(binary), "short", str(input_path)], timeout=30)
    parsed = parse_probe(host_run["stdout"])
    reference = probe_signature(parsed, "short")
    if host_run["exit_code"] != 0 or None in reference:
        raise RuntimeError("probe host check failed: " + (host_run["stderr"] or host_run["stdout"])[-2000:])
    return {
        "source": PROBE_SOURCE.name,
        "source_sha256": _sha256_file(PROBE_SOURCE),
        "binary_sha256": _sha256_file(binary),
        "binary_bytes": len(payload),
        "build_argv": argv,
        "gcc": (_run(["gcc", "--version"], timeout=30).get("stdout") or "").splitlines()[:1],
        "input": {**input_info, "fnv1a": parsed["result"]["hash"]},
        "host_check": {
            "exit_code": host_run["exit_code"],
            "result": parsed["result"],
            "timing": parsed["timing"],
        },
        "probe_path": str(binary),
        "input_path": str(input_path),
        "short_reference": reference,
    }


def host_context():
    governors = sorted({
        value for path in Path("/sys/devices/system/cpu").glob(
            "cpu[0-9]*/cpufreq/scaling_governor")
        if (value := _text(path))
    })
    return {
        "captured_at": now(),
        "kernel": os.uname().release,
        "cpu_governors": governors,
        "host": host_snapshot(),
        "podman": (_run(["podman", "--version"], timeout=30).get("stdout") or "").strip(),
        "python": sys.version.split()[0],
    }


def source_identity():
    relative = (
        "src/multilinux/resources.py",
        "src/multilinux/runtime.py",
        "src/multilinux/service.py",
        "src/multilinux/packages.py",
        "tools/probe_resources.py",
        "fixtures/resource_probe.c",
    )
    files = {}
    for name in relative:
        path = ROOT / name
        files[name] = _sha256_file(path) if path.is_file() else None
    git = _run(["git", "rev-parse", "HEAD"], timeout=10)
    head = (git.get("stdout") or "").strip()
    return {
        "files_sha256": files,
        "git_head": head if git.get("exit_code") == 0 and head else None,
        "git_note": None if git.get("exit_code") == 0 else (
            (git.get("stderr") or git.get("error") or "").strip()[:300] or "no git revision"),
    }


def _finish(report):
    report["host_finished"] = host_context()
    report["finished_at"] = now()
    report["passed"] = _report_passed(report)
    return report


def filter_storage_sections(text, image_id, container_name_value):
    selected = {}
    section = None
    section_data = None
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.endswith("space usage:"):
            section = stripped
            section_data = {"header": None, "rows": []}
            selected[section] = section_data
            continue
        if section_data is None or not stripped:
            continue
        if section_data["header"] is None:
            section_data["header"] = line.rstrip()
            continue
        if section.startswith("Images") and image_id and image_id[:12] in line:
            section_data["rows"].append(line.rstrip())
        elif section.startswith("Containers") and container_name_value in line:
            section_data["rows"].append(line.rstrip())
    return selected


def storage_snapshot(image_id, container_name_value):
    """Keep only this image's and this container's rows from verbose disk usage."""
    totals = _run(["podman", "system", "df", "--format", "json"], timeout=60)
    try:
        total_rows = json.loads(totals["stdout"])
    except (json.JSONDecodeError, TypeError):
        total_rows = None
    verbose = _run(["podman", "system", "df", "-v"], timeout=90)
    selected = filter_storage_sections(
        verbose["stdout"], image_id, container_name_value)
    return {
        "totals": total_rows,
        "totals_error": totals["error"],
        "verbose_error": verbose["error"],
        "sections": selected,
        "note": ("SIZE is the image's logical size; SHARED SIZE is shared with other "
                 "local images, UNIQUE SIZE is exclusive to this image. Container size "
                 "includes the shared lower layers and is not exclusive space."),
    }



def service_in(root):
    config, lock, _ = load_project()
    return EnvironmentService(root, PodmanRuntime(), config, lock)


def _reset(service, runtime, env_id):
    project = project_label(service.root)
    name = container_name(service.root, env_id)
    if service.store.exists(env_id):
        service.remove(env_id)
        return
    info = runtime.inspect(name)
    if info is not None and info.project == project and info.env_id == env_id:
        runtime.remove(name, project=project, env_id=env_id)


def _cleanup(service, runtime, env_id):
    errors = []
    try:
        _reset(service, runtime, env_id)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
    return errors


def _metric(series, path, field="p50"):
    value = (series.get("summary") or {}).get("metrics", {}).get(path)
    return value.get(field) if value else None


def _popen_active(name, probe_path, input_path, seconds):
    argv = ["podman", "exec", name, probe_path, "active", input_path, str(seconds)]
    started = time.monotonic_ns()
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**os.environ, "LC_ALL": "C"})
    return process, argv, started


def _finish_active(process, argv, started, timeout):
    try:
        stdout, stderr = process.communicate(timeout=timeout)
        timed_out = False
        error = None
    except subprocess.TimeoutExpired:
        process.kill()
        stdout, stderr = process.communicate()
        timed_out = True
        error = f"active probe timed out after {timeout}s"
    parsed = parse_probe(stdout)
    return {
        "argv": argv,
        "exit_code": None if timed_out else process.returncode,
        "wall_ns": time.monotonic_ns() - started,
        "timed_out": timed_out,
        "error": error,
        "ready": parsed["ready"],
        "result": parsed["result"],
        "timing": parsed["timing"],
        "stderr_tail": (stderr or "")[-800:],
    }


class Sampler(threading.Thread):
    def __init__(self, probe, env_id, *, interval, disk_every):
        super().__init__(daemon=True)
        self.probe = probe
        self.env_id = env_id
        self.interval = interval
        self.disk_every = disk_every
        self.stop_event = threading.Event()
        self.samples = []
        self.errors = []
        self.index = 0

    def run(self):
        while not self.stop_event.is_set():
            include_disk = bool(self.disk_every and self.index % self.disk_every == 0)
            try:
                self.samples.append(self.probe.sample(
                    self.env_id, include_disk=include_disk))
            except Exception as exc:
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self.index += 1
            self.stop_event.wait(self.interval)

    def stop(self):
        self.stop_event.set()
        self.join(timeout=15)
        if self.is_alive():
            self.errors.append("sampler thread did not stop within 15s")


def _phase_peak(phase, reset, sampler, txn, before, after, duration):
    samples = sampler.samples
    memory = summarize(samples) if samples else {"sample_count": 0, "metrics": {}}
    def maximum(path):
        metrics = memory["metrics"].get(path)
        return metrics["max"] if metrics else None
    peaks = []
    for sample in samples:
        peak = (sample.get("cgroup") or {}).get("memory_peak_bytes")
        if isinstance(peak, int):
            peaks.append(peak)
    disk = []
    for sample in samples:
        size = ((sample.get("disk") or {}).get("container") or {}).get("size_rw_bytes")
        if isinstance(size, int):
            disk.append(size)
    for sample in (before, after):
        size = ((sample.get("disk") or {}).get("container") or {}).get("size_rw_bytes")
        if isinstance(size, int):
            disk.append(size)
    return {
        "operation": phase,
        "duration_seconds": round(duration, 3),
        "sample_count": len(samples),
        "sampling_interval_seconds": sampler.interval,
        "sampling_errors": sampler.errors,
        "peak_reset": reset,
        "memory_peak_bytes": maximum("cgroup.memory_peak_bytes"),
        "memory_peak_first_bytes": peaks[0] if peaks else None,
        "memory_peak_last_bytes": peaks[-1] if peaks else None,
        "memory_peak_note": (
            "memory_peak_bytes is the highest cgroup lifetime peak observed while "
            "sampling. Peak reset may be ineffective, so the value can include "
            "container start before the transaction. memory_current_max_bytes and "
            "pss_max_bytes are maxima of samples taken during the transaction and "
            "do not include gaps between samples."
        ),
        "memory_current_max_bytes": maximum("cgroup.memory_current_bytes"),
        "pss_max_bytes": maximum("processes.pss_bytes"),
        "anon_max_bytes": maximum("cgroup.memory_stat.anon"),
        "file_max_bytes": maximum("cgroup.memory_stat.file"),
        "kernel_max_bytes": maximum("cgroup.memory_stat.kernel"),
        "slab_max_bytes": maximum("cgroup.memory_stat.slab"),
        "memory_pressure_some_avg10_max": maximum("cgroup.memory_pressure.some.avg10"),
        "cpu_pressure_some_avg10_max": maximum("cgroup.cpu_pressure.some.avg10"),
        "disk_rw_max_bytes": max(disk) if disk else None,
        "disk_before": (before.get("disk") or {}).get("container"),
        "disk_after": (after.get("disk") or {}).get("container"),
        "host_memory_available_min_bytes": (
            memory["metrics"].get("host.meminfo.MemAvailable_bytes") or {}).get("min"),
        "transaction": _transaction_summary(txn),
    }


def _transaction_summary(txn):
    if txn is None:
        return None
    return {
        "id": txn.get("id"),
        "operation": txn.get("operation"),
        "packages": txn.get("packages"),
        "phase": txn.get("phase"),
        "failure_phase": txn.get("failure_phase"),
        "native_error": txn.get("native_error"),
        "verification": txn.get("verification"),
        "revision_before": txn.get("revision_before"),
        "revision_after": txn.get("revision_after"),
        "started_at": txn.get("started_at"),
        "finished_at": txn.get("finished_at"),
        "native": [
            {
                "argv": step.get("argv"),
                "exit_code": step.get("exit_code"),
                "stdout_tail": (step.get("stdout_tail") or "")[-1000:],
                "stderr_tail": (step.get("stderr_tail") or "")[-1000:],
            }
            for step in txn.get("native") or []
        ],
    }


def run_states(args):
    config, lock, _ = load_project()
    report = {
        "schema_version": 1,
        "kind": "environment-states",
        "started_at": now(),
        "host": host_context(),
        "source": source_identity(),
        "settings": {
            "targets": args.targets,
            "idle_settle_seconds": args.settle,
            "idle_samples": args.idle_samples,
            "active_seconds": args.active_seconds,
            "active_samples": args.active_samples,
            "post_idle_samples": args.post_samples,
            "network": "none",
            "scope": "Single serial environment per target; raw samples and summaries.",
        },
        "targets": {},
        "passed": False,
    }
    share = Path(tempfile.mkdtemp(prefix="ml-m4-states-share-"))
    try:
        fixture = build_fixture(share)
        report["fixture"] = fixture
        with tempfile.TemporaryDirectory(prefix="ml-m4-states-project-") as temporary:
            root = Path(temporary) / "project"
            root.mkdir()
            service = service_in(root)
            runtime = service.runtime
            probe = ResourceProbe(service)
            for target in args.targets:
                env_id = ENV_IDS["states"].format(target=target)
                log(f"{target}: creating {env_id}")
                result = {
                    "target": target,
                    "environment_id": env_id,
                    "errors": [],
                    "checks": {},
                    "passed": False,
                }
                try:
                    _reset(service, runtime, env_id)
                    record = service.create(target, env_id, [str(share)])
                    result["identity"] = {
                        "distribution_id": record["distribution_id"],
                        "version_track": record["version_track"],
                        "image_digest": record["base_image"]["manifest_digest"],
                        "package_inventory_count": record["package_inventory_count"],
                        "package_inventory_sha256": record["package_inventory_sha256"],
                    }
                    stopped_before = probe.sample(env_id)
                    service.start(env_id)
                    log(f"{target}: idle series")
                    idle = probe.series(
                        env_id, samples=args.idle_samples, interval=1.0,
                        settle=args.settle)
                    reset = probe.reset_peak(env_id)
                    log(f"{target}: active workload")
                    name = container_name(root, env_id)
                    process, active_argv, active_started = _popen_active(
                        name, fixture["probe_path"], fixture["input_path"],
                        args.active_seconds)
                    time.sleep(2.0)
                    active = probe.series(
                        env_id, samples=args.active_samples, interval=1.0, settle=0.0)
                    active_run = _finish_active(
                        process, active_argv, active_started, args.active_seconds + 30)
                    log(f"{target}: post-active idle")
                    post_idle = probe.series(
                        env_id, samples=args.post_samples, interval=1.0, settle=5.0)
                    service.stop(env_id)
                    stopped_after = probe.sample(env_id)
                    result["states"] = {
                        "stopped_before_start": stopped_before,
                        "idle": idle,
                        "active": active,
                        "active_run": active_run,
                        "post_active_idle": post_idle,
                        "stopped_after_stop": stopped_after,
                    }
                    result["peak_reset_before_active"] = reset
                    result["comparisons"] = {
                        "idle_pss_p50_bytes": _metric(idle, "processes.pss_bytes"),
                        "active_pss_p50_bytes": _metric(active, "processes.pss_bytes"),
                        "post_active_pss_p50_bytes": _metric(post_idle, "processes.pss_bytes"),
                        "idle_memory_current_p50_bytes": _metric(
                            idle, "cgroup.memory_current_bytes"),
                        "active_memory_current_p50_bytes": _metric(
                            active, "cgroup.memory_current_bytes"),
                        "active_pss_delta_bytes": _delta(
                            _metric(active, "processes.pss_bytes"),
                            _metric(idle, "processes.pss_bytes")),
                        "post_active_pss_drop_bytes": _delta(
                            _metric(active, "processes.pss_bytes"),
                            _metric(post_idle, "processes.pss_bytes")),
                    }
                    image = ((idle.get("series") or [{}])[0].get("disk") or {}).get("image") or {}
                    result["storage"] = storage_snapshot(
                        image.get("id"), container_name(root, env_id))
                    result["checks"] = _state_checks(
                        stopped_before, idle, active, active_run, post_idle,
                        stopped_after, fixture)
                    result["passed"] = all(result["checks"].values())
                    if not result["passed"]:
                        result["errors"].append("one or more state checks failed")
                except Exception as exc:
                    result["errors"].append(f"{type(exc).__name__}: {exc}")
                finally:
                    result["cleanup_errors"] = _cleanup(service, runtime, env_id)
                report["targets"][target] = result
                log(f"{target}: {'passed' if result['passed'] else 'failed'}")
    finally:
        shutil.rmtree(share, ignore_errors=True)
    return _finish(report)


def _delta(left, right):
    if left is None or right is None:
        return None
    return left - right


def _state_checks(stopped_before, idle, active, active_run, post_idle, stopped_after, fixture):
    active_result = active_run.get("result") or {}
    expected = fixture["short_reference"]
    return {
        "stopped_before_has_no_cgroup": stopped_before.get("cgroup") is None,
        "stopped_before_has_disk": (
            ((stopped_before.get("disk") or {}).get("container") or {})
            .get("size_rw_bytes", 0) or 0) > 0,
        "idle_cgroup_readable": _metric(idle, "cgroup.memory_current_bytes") is not None,
        "idle_pss_positive": (_metric(idle, "processes.pss_bytes") or 0) > 0,
        "active_output_matches": (
            active_run.get("exit_code") == 0
            and active_result.get("bytes") == expected[0]
            and active_result.get("hash") == expected[1]),
        "active_pss_increase": (
            _delta(_metric(active, "processes.pss_bytes"),
                   _metric(idle, "processes.pss_bytes")) or 0) > 8 * 1024 * 1024,
        "post_active_pss_reclaimed": (
            _delta(_metric(active, "processes.pss_bytes"),
                   _metric(post_idle, "processes.pss_bytes")) or 0) > 4 * 1024 * 1024,
        "stopped_after_has_no_cgroup": stopped_after.get("cgroup") is None,
        "stopped_after_has_disk": (
            ((stopped_after.get("disk") or {}).get("container") or {})
            .get("size_rw_bytes", 0) or 0) > 0,
    }


def run_same_binary(args):
    config, lock, root = load_project()
    report = {
        "schema_version": 1,
        "kind": "same-binary",
        "started_at": now(),
        "host": host_context(),
        "source": source_identity(),
        "settings": {
            "targets": args.targets,
            "modes": ["host", "podman-exec", "ml-env-run"],
            "short_samples": args.short_samples,
            "long_samples": args.long_samples,
            "long_iterations": args.long_iterations,
            "order": "round-robin rotation per sample",
            "scope": "One static ELF and one input; no dynamic libraries or package differences.",
        },
        "targets": {},
        "passed": False,
    }
    share = Path(tempfile.mkdtemp(prefix="ml-m4-binary-share-"))
    try:
        fixture = build_fixture(share)
        report["fixture"] = fixture
        service = service_in(root)
        runtime = service.runtime
        for target in args.targets:
            env_id = ENV_IDS["binary"]
            log(f"{target}: creating {env_id}")
            result = {"target": target, "environment_id": env_id, "errors": [],
                      "checks": {}, "passed": False}
            try:
                _reset(service, runtime, env_id)
                record = service.create(target, env_id, [str(share)])
                result["identity"] = {
                    "distribution_id": record["distribution_id"],
                    "version_track": record["version_track"],
                    "image_digest": record["base_image"]["manifest_digest"],
                    "package_inventory_sha256": record["package_inventory_sha256"],
                }
                service.start(env_id)
                name = container_name(root, env_id)
                probe_path = fixture["probe_path"]
                input_path = fixture["input_path"]
                host_before = host_context()
                short = _binary_set(
                    name, env_id, probe_path, input_path, "short", [],
                    args.short_samples, fixture)
                long = _binary_set(
                    name, env_id, probe_path, input_path, "burn",
                    [str(args.long_iterations)], args.long_samples, fixture)
                host_after = host_context()
                result["short"] = short
                result["long"] = long
                result["host_before"] = host_before
                result["host_after"] = host_after
                result["checks"] = _binary_checks(short, long, args)
                result["passed"] = all(result["checks"].values())
                if not result["passed"]:
                    result["errors"].append("one or more same-binary checks failed")
            except Exception as exc:
                result["errors"].append(f"{type(exc).__name__}: {exc}")
            finally:
                result["cleanup_errors"] = _cleanup(service, runtime, env_id)
            report["targets"][target] = result
            log(f"{target}: {'passed' if result['passed'] else 'failed'}")
    finally:
        shutil.rmtree(share, ignore_errors=True)
    return _finish(report)


def _binary_set(name, env_id, probe_path, input_path, mode, extra, samples, fixture):
    if mode == "short":
        reference = tuple(fixture["short_reference"])
    else:
        host = _tracked_run(
            [probe_path, mode, input_path, *extra], mode=mode, reference=None,
            label="host-reference")
        reference = tuple(host["signature"])
    rows = []
    modes = ("host", "podman-exec", "ml-env-run")
    for index in range(samples):
        order = list(modes[index % len(modes):] + modes[:index % len(modes)])
        for mode_name in order:
            if mode_name == "host":
                argv = [probe_path, mode, input_path, *extra]
            elif mode_name == "podman-exec":
                argv = ["podman", "exec", name, probe_path, mode, input_path, *extra]
            else:
                argv = [sys.executable, str(ROOT / "tools/ml.py"), "env", "run",
                        env_id, "--", probe_path, mode, input_path, *extra]
            rows.append(_tracked_run(
                argv, mode=mode, reference=reference, label=mode_name,
                sample=index))
    summaries = {}
    for mode_name in modes:
        selected = [row for row in rows if row["mode"] == mode_name]
        summaries[mode_name] = {
            "runs": len(selected),
            "successful": sum(1 for row in selected if row["signature_matches"]),
            "wall_ns": _stats(row["wall_ns"] for row in selected),
            "cpu_ns": _stats(row["cpu_ns"] for row in selected),
        }
    host_wall = (summaries["host"]["wall_ns"] or {}).get("p50")
    host_cpu = (summaries["host"]["cpu_ns"] or {}).get("p50")
    paired = {mode_name: {"wall": [], "cpu": []}
              for mode_name in ("podman-exec", "ml-env-run")}
    groups = {}
    for row in rows:
        groups.setdefault(row["sample"], {})[row["mode"]] = row
    for index in sorted(groups):
        host = groups[index].get("host")
        if not host or host["exit_code"] != 0:
            continue
        for mode_name, ratios in paired.items():
            row = groups[index].get(mode_name)
            if not row or row["exit_code"] != 0:
                continue
            if host["wall_ns"] and row["wall_ns"] is not None:
                ratios["wall"].append(row["wall_ns"] / host["wall_ns"])
            if host["cpu_ns"] and row["cpu_ns"] is not None:
                ratios["cpu"].append(row["cpu_ns"] / host["cpu_ns"])
    for mode_name, item in summaries.items():
        item["wall_overhead_percent"] = _overhead(
            (item["wall_ns"] or {}).get("p50"), host_wall)
        item["cpu_overhead_percent"] = _overhead(
            (item["cpu_ns"] or {}).get("p50"), host_cpu)
        ratios = paired.get(mode_name)
        if ratios is None:
            item["paired_wall_ratio"] = None
            item["paired_cpu_ratio"] = None
            item["paired_wall_overhead_percent"] = None
        else:
            item["paired_wall_ratio"] = _stats(ratios["wall"])
            item["paired_cpu_ratio"] = _stats(ratios["cpu"])
            p50 = (item["paired_wall_ratio"] or {}).get("p50")
            item["paired_wall_overhead_percent"] = (
                round((p50 - 1.0) * 100.0, 2) if p50 is not None else None)
    return {
        "mode": mode,
        "experiment_argv": [mode, *extra],
        "reference_signature": list(reference),
        "rows": rows,
        "summary": summaries,
    }


def _overhead(test, control):
    if test is None or control in (None, 0):
        return None
    return round((test / control - 1.0) * 100.0, 2)


def _binary_checks(short, long, args):
    modes = ("host", "podman-exec", "ml-env-run")
    return {
        "short_sample_count": all(
            short["summary"][mode]["runs"] == args.short_samples for mode in modes),
        "long_sample_count": all(
            long["summary"][mode]["runs"] == args.long_samples for mode in modes),
        "short_functional": all(row["signature_matches"] for row in short["rows"]),
        "long_functional": all(row["signature_matches"] for row in long["rows"]),
        "short_summaries_present": all(
            short["summary"][mode]["wall_ns"] for mode in modes),
        "long_summaries_present": all(
            long["summary"][mode]["wall_ns"] for mode in modes),
        "paired_ratios_present": all(
            short["summary"][mode]["paired_wall_ratio"]
            and long["summary"][mode]["paired_wall_ratio"]
            for mode in ("podman-exec", "ml-env-run")),
    }


def run_update_peak(args):
    config, lock, _ = load_project()
    report = {
        "schema_version": 1,
        "kind": "update-peak",
        "started_at": now(),
        "host": host_context(),
        "source": source_identity(),
        "settings": {
            "targets": args.targets,
            "package": "tree",
            "network": "pasta",
            "sample_interval_seconds": args.interval,
            "disk_sample_every": args.disk_every,
            "scope": "Serial install then update per target; memory and disk peaks are separate from PSS.",
        },
        "targets": {},
        "passed": False,
    }
    with tempfile.TemporaryDirectory(prefix="ml-m4-update-project-") as temporary:
        root = Path(temporary) / "project"
        root.mkdir()
        service = service_in(root)
        runtime = service.runtime
        probe = ResourceProbe(service)
        for target in args.targets:
            env_id = ENV_IDS["update"].format(target=target)
            log(f"{target}: creating {env_id}")
            result = {"target": target, "environment_id": env_id, "errors": [],
                      "phases": {}, "checks": {}, "passed": False}
            try:
                _reset(service, runtime, env_id)
                record = service.create(target, env_id, [], network="pasta")
                result["identity"] = {
                    "distribution_id": record["distribution_id"],
                    "version_track": record["version_track"],
                    "image_digest": record["base_image"]["manifest_digest"],
                    "package_inventory_sha256": record["package_inventory_sha256"],
                }
                service.start(env_id)
                time.sleep(1)
                before = probe.sample(env_id)
                install = _run_phase(
                    probe, env_id, "install",
                    lambda: service.install_packages(
                        env_id, ["tree"], use_argv=["tree", "--version"]),
                    before, args.interval, args.disk_every)
                result["phases"]["install"] = install
                after_install = probe.sample(env_id)
                service.stop(env_id)
                service.start(env_id)
                time.sleep(1)
                before_update = probe.sample(env_id)
                result["phase_boundary"] = {
                    "restarted": True,
                    "note": ("The environment was stopped and started between install and update "
                             "so memory.peak belongs to a fresh cgroup; memory.peak reset "
                             "itself is not effective on this kernel."),
                }
                update = _run_phase(
                    probe, env_id, "update",
                    lambda: service.update_packages(env_id, use_argv=["tree", "--version"]),
                    before_update, args.interval, args.disk_every)
                result["phases"]["update"] = update
                final = probe.sample(env_id)
                result["final"] = {
                    "content_revision": final.get("content_revision"),
                    "package_inventory_sha256": final.get("package_inventory_sha256"),
                    "disk": final.get("disk"),
                }
                result["checks"] = {
                    "install_completed": _txn_completed(install),
                    "update_completed": _txn_completed(update),
                    "install_samples": install["sample_count"] > 0,
                    "update_samples": update["sample_count"] > 0,
                    "sampling_clean": not install["sampling_errors"] and not update["sampling_errors"],
                    "disk_positive": (
                        ((final.get("disk") or {}).get("container") or {})
                        .get("size_rw_bytes", 0) or 0) > 0,
                }
                result["passed"] = all(result["checks"].values())
                if not result["passed"]:
                    result["errors"].append("one or more update checks failed")
            except Exception as exc:
                result["errors"].append(f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    service.stop(env_id)
                except Exception:
                    pass
                result["cleanup_errors"] = _cleanup(service, runtime, env_id)
            report["targets"][target] = result
            log(f"{target}: {'passed' if result['passed'] else 'failed'}")
    return _finish(report)


def _run_phase(probe, env_id, name, action, before, interval, disk_every):
    log(f"{env_id}: {name} phase")
    reset = probe.reset_peak(env_id)
    sampler = Sampler(probe, env_id, interval=interval, disk_every=disk_every)
    sampler.start()
    started = time.monotonic()
    txn = None
    error = None
    try:
        txn = action()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    finally:
        duration = time.monotonic() - started
        sampler.stop()
    after = probe.sample(env_id)
    phase = _phase_peak(name, reset, sampler, txn, before, after, duration)
    phase["error"] = error
    return phase


def _txn_completed(phase):
    txn = phase.get("transaction") or {}
    return txn.get("phase") == "completed" and (txn.get("verification") or {}).get("ok") is True


def _report_passed(report):
    targets = report.get("targets") or {}
    return bool(targets) and all(
        item.get("passed") and not item.get("cleanup_errors")
        for item in targets.values())


def _emit(report, output):
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if output:
        from tools import lab
        lab.save_json(Path(output), report)
        log(f"wrote {output}")
    else:
        print(payload, end="")
    return 0 if report.get("passed") else 1


def add_targets(parser):
    parser.add_argument("--target", action="append", choices=TARGETS, default=None,
                        help="repeat to select targets; defaults to all three")
    parser.add_argument("--output", help="write the evidence JSON here")


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    states = commands.add_parser("states", help="stopped, idle, active and stopped again")
    add_targets(states)
    states.add_argument("--settle", type=float, default=8.0)
    states.add_argument("--idle-samples", type=int, default=10)
    states.add_argument("--active-seconds", type=int, default=20)
    states.add_argument("--active-samples", type=int, default=10)
    states.add_argument("--post-samples", type=int, default=5)
    states.set_defaults(func=run_states)
    binary = commands.add_parser("same-binary", help="host direct, podman exec and ml env run")
    add_targets(binary)
    binary.add_argument("--short-samples", type=int, default=SHORT_SAMPLES)
    binary.add_argument("--long-samples", type=int, default=LONG_SAMPLES)
    binary.add_argument("--long-iterations", type=int, default=LONG_ITERATIONS)
    binary.set_defaults(func=run_same_binary)
    update = commands.add_parser("update-peak", help="native install and update peaks")
    add_targets(update)
    update.add_argument("--interval", type=float, default=0.5)
    update.add_argument("--disk-every", type=int, default=20)
    update.set_defaults(func=run_update_peak)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    args.targets = args.target or list(TARGETS)
    if getattr(args, "interval", 1.0) <= 0:
        raise SystemExit("--interval must be positive")
    for name in ("idle_samples", "active_samples", "post_samples",
                 "short_samples", "long_samples"):
        if getattr(args, name, 1) < 1:
            raise SystemExit(f"--{name.replace('_', '-')} must be at least 1")
    report = args.func(args)
    return _emit(report, args.output)


if __name__ == "__main__":
    raise SystemExit(main())
