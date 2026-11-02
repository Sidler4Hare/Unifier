"""Read-only resource sampling for one project-owned environment.

The sampler reads the container cgroup, each process's smaps_rollup, the
container's disk accounting and a small host snapshot. Missing fields stay
missing: a stopped environment has no cgroup, and an unreadable file is
recorded as an error instead of a zero.
"""

import datetime as dt
import math
import os
from pathlib import Path
import time
from zoneinfo import ZoneInfo

from multilinux.errors import MultilinuxError


PROC_ROOT = Path("/proc")
CGROUP_ROOT = Path("/sys/fs/cgroup")

SMAPS_FIELDS = {
    "Rss": "rss_bytes",
    "Pss": "pss_bytes",
    "Pss_Anon": "pss_anon_bytes",
    "Pss_File": "pss_file_bytes",
    "Pss_Shmem": "pss_shmem_bytes",
    "Shared_Clean": "shared_clean_bytes",
    "Shared_Dirty": "shared_dirty_bytes",
    "Private_Clean": "private_clean_bytes",
    "Private_Dirty": "private_dirty_bytes",
    "Referenced": "referenced_bytes",
    "Anonymous": "anonymous_bytes",
    "Swap": "swap_bytes",
    "SwapPss": "swap_pss_bytes",
}
# Kernel pages, page tables and socket buffers are charged to the cgroup but
# are not process PSS. Both groups are reported and are not added together.
SUMMARY_PATHS = (
    "cgroup.memory_current_bytes",
    "cgroup.memory_peak_bytes",
    "cgroup.memory_swap_current_bytes",
    "cgroup.pids_current",
    "cgroup.pids_peak",
    "cgroup.memory_stat.anon",
    "cgroup.memory_stat.file",
    "cgroup.memory_stat.kernel",
    "cgroup.memory_stat.slab",
    "cgroup.memory_stat.file_mapped",
    "cgroup.memory_events.oom_kill",
    "cgroup.memory_pressure.some.avg10",
    "cgroup.cpu_pressure.some.avg10",
    "processes.pss_bytes",
    "processes.rss_bytes",
    "processes.process_count",
    "runtime_processes.pss_bytes",
    "host.meminfo.MemAvailable_bytes",
    "host.meminfo.SwapFree_bytes",
    "host.pressure.memory.some.avg10",
    "host.pressure.memory.full.avg10",
    "host.pressure.cpu.some.avg10",
    "host.cpu.min_khz",
    "host.cpu.max_khz",
    "host.thermal.package_c",
)


def timestamp() -> str:
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _read(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8").strip(), None
    except (OSError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _int(text: str | None, error: str | None) -> tuple[int | None, str | None]:
    if error is not None or text is None:
        return None, error or "value is missing"
    try:
        return int(text), None
    except ValueError as exc:
        return None, f"not an integer: {exc}"


def parse_kv_int(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            values[fields[0]] = int(fields[1])
        except ValueError:
            continue
    return values


def parse_pressure(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        values = {}
        for item in fields[1:]:
            key, _, raw = item.partition("=")
            if not key:
                continue
            try:
                values["total_us" if key == "total" else key] = (
                    int(raw) if key == "total" else float(raw))
            except ValueError:
                continue
        result[fields[0]] = values
    return result


def parse_smaps_rollup(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, sep, rest = line.partition(":")
        if not sep or key in {"Name"}:
            continue
        fields = rest.split()
        if len(fields) < 2 or fields[-1] != "kB":
            continue
        target = SMAPS_FIELDS.get(key)
        if target is None:
            continue
        try:
            values[target] = int(fields[0]) * 1024
        except ValueError:
            continue
    return values


def read_cgroup_path(pid: int, *, proc_root: Path = PROC_ROOT,
                     cgroup_root: Path = CGROUP_ROOT) -> tuple[Path | None, str | None]:
    text, error = _read(proc_root / str(pid) / "cgroup")
    if error is not None:
        return None, error
    for line in (text or "").splitlines():
        if line.startswith("0::"):
            relative = line[3:].strip()
            if not relative:
                return None, "the process reports the cgroup root"
            return cgroup_root / relative.lstrip("/"), None
    return None, "no cgroup v2 entry for this process"


def _walk_cgroup_pids(cgroup: Path) -> tuple[list[int], list[str]]:
    pids = set()
    errors = []
    pending = [cgroup]
    seen = set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        text, error = _read(current / "cgroup.procs")
        if error is not None:
            errors.append(f"{current}: {error}")
        else:
            for line in (text or "").splitlines():
                try:
                    pids.add(int(line))
                except ValueError:
                    errors.append(f"{current}: invalid pid line {line!r}")
        try:
            children = [entry for entry in current.iterdir()
                        if entry.is_dir() and (entry / "cgroup.procs").is_file()]
        except OSError as exc:
            errors.append(f"{current}: {type(exc).__name__}: {exc}")
            children = []
        pending.extend(children)
    return sorted(pids), errors


def _direct_cgroup_pids(cgroup: Path) -> tuple[list[int], list[str]]:
    text, error = _read(cgroup / "cgroup.procs")
    if error is not None:
        return [], [f"{cgroup}: {error}"]
    pids = []
    errors = []
    for line in (text or "").splitlines():
        try:
            pids.append(int(line))
        except ValueError:
            errors.append(f"{cgroup}: invalid pid line {line!r}")
    return sorted(pids), errors


def process_details(pids: list[int], *, proc_root: Path = PROC_ROOT) -> dict:
    details = []
    errors = []
    for pid in pids:
        text, error = _read(proc_root / str(pid) / "smaps_rollup")
        if error is not None:
            errors.append({"pid": pid, "error": error})
            continue
        values = parse_smaps_rollup(text or "")
        if "pss_bytes" not in values:
            errors.append({"pid": pid, "error": "smaps_rollup has no Pss field"})
            continue
        comm, _ = _read(proc_root / str(pid) / "comm")
        details.append({"pid": pid, "comm": comm, **values})
    complete = not errors and len(details) == len(pids)
    return {
        "process_count": len(details),
        "expected_process_count": len(pids),
        "complete": complete,
        "pss_bytes": sum(item["pss_bytes"] for item in details) if complete else None,
        "rss_bytes": sum(item["rss_bytes"] for item in details) if complete else None,
        "pss_partial_bytes": sum(item["pss_bytes"] for item in details),
        "details": details,
        "errors": errors,
    }


def _cgroup_metrics(cgroup: Path) -> dict:
    metrics = {"path": str(cgroup), "errors": {}}
    scalar = {
        "memory_current_bytes": ("memory.current", int),
        "memory_peak_bytes": ("memory.peak", int),
        "memory_swap_current_bytes": ("memory.swap.current", int),
        "pids_current": ("pids.current", int),
        "pids_peak": ("pids.peak", int),
        "memory_high": ("memory.high", str),
        "memory_max": ("memory.max", str),
        "cpu_max": ("cpu.max", str),
    }
    for target, (name, parser) in scalar.items():
        text, error = _read(cgroup / name)
        if error is not None:
            metrics[target] = None
            metrics["errors"][name] = error
            continue
        try:
            metrics[target] = parser(text)
        except ValueError as exc:
            metrics[target] = None
            metrics["errors"][name] = f"invalid value: {exc}"
    parsed = {
        "memory_stat": ("memory.stat", parse_kv_int),
        "memory_events": ("memory.events", parse_kv_int),
        "cpu_stat": ("cpu.stat", parse_kv_int),
        "memory_pressure": ("memory.pressure", parse_pressure),
        "cpu_pressure": ("cpu.pressure", parse_pressure),
        "io_pressure": ("io.pressure", parse_pressure),
    }
    for target, (name, parser) in parsed.items():
        text, error = _read(cgroup / name)
        if error is not None:
            metrics[target] = None
            metrics["errors"][name] = error
            continue
        metrics[target] = parser(text)
    return metrics


def _host_snapshot(proc_root: Path = PROC_ROOT) -> dict:
    meminfo = {}
    text, _ = _read(proc_root / "meminfo")
    wanted = {"MemTotal", "MemAvailable", "MemFree", "Cached", "SwapTotal", "SwapFree", "Shmem"}
    for line in (text or "").splitlines():
        key, sep, rest = line.partition(":")
        if sep and key in wanted and rest.split():
            try:
                meminfo[key + "_bytes"] = int(rest.split()[0]) * 1024
            except ValueError:
                continue
    pressure = {}
    for kind in ("cpu", "memory", "io"):
        raw, _ = _read(proc_root / "pressure" / kind)
        if raw is not None:
            pressure[kind] = parse_pressure(raw)
    load = []
    raw, _ = _read(proc_root / "loadavg")
    for item in (raw or "").split()[:3]:
        try:
            load.append(float(item))
        except ValueError:
            break
    return {"meminfo": meminfo, "pressure": pressure, "load_average": load}


def read_cpu_frequency(sysfs_root: Path = Path("/sys")) -> dict:
    cpu_root = Path(sysfs_root) / "devices/system/cpu"
    if not cpu_root.is_dir():
        return {"sample_count": 0, "min_khz": None, "max_khz": None,
                "khz": [], "errors": [f"missing {cpu_root}"]}
    freqs = []
    errors = []
    paths = sorted(cpu_root.glob("cpu[0-9]*/cpufreq/scaling_cur_freq"))
    for path in paths:
        text, error = _read(path)
        if error is not None or text is None:
            errors.append(f"{path.parent.parent.name}: {error or 'value is missing'}")
            continue
        try:
            freqs.append(int(text))
        except ValueError:
            errors.append(f"{path.parent.parent.name}: not an integer")
    return {
        "sample_count": len(freqs),
        "min_khz": min(freqs) if freqs else None,
        "max_khz": max(freqs) if freqs else None,
        "khz": freqs,
        "errors": errors,
    }


def read_thermal(sysfs_root: Path = Path("/sys")) -> dict:
    root = Path(sysfs_root) / "class/thermal"
    if not root.is_dir():
        return {"zones": [], "package_c": None, "errors": [f"missing {root}"]}
    zones = []
    errors = []
    for zone in sorted(root.glob("thermal_zone*")):
        kind, kind_error = _read(zone / "type")
        raw, temp_error = _read(zone / "temp")
        if kind_error is not None or temp_error is not None or raw is None:
            errors.append(f"{zone.name}: {kind_error or temp_error or 'value is missing'}")
            continue
        try:
            milli = int(raw)
        except ValueError:
            errors.append(f"{zone.name}: not an integer")
            continue
        zones.append({
            "zone": zone.name,
            "type": kind,
            "millicelsius": milli,
            "celsius": round(milli / 1000.0, 3),
        })
    package = next((item["celsius"] for item in zones if item["type"] == "x86_pkg_temp"), None)
    return {"zones": zones, "package_c": package, "errors": errors}


def host_snapshot(proc_root: Path = PROC_ROOT, *, sysfs_root: Path = Path("/sys")) -> dict:
    snapshot = _host_snapshot(proc_root)
    snapshot["cpu"] = read_cpu_frequency(sysfs_root)
    snapshot["thermal"] = read_thermal(sysfs_root)
    return snapshot


def _filesystem(path: str) -> dict:
    try:
        usage = os.statvfs(path)
    except OSError as exc:
        return {"path": path, "error": f"{type(exc).__name__}: {exc}"}
    total = usage.f_blocks * usage.f_frsize
    free = usage.f_bavail * usage.f_frsize
    return {
        "path": path,
        "total_bytes": total,
        "free_bytes": free,
        "used_bytes": total - free,
        "note": "Filesystem-wide values, not this environment's exclusive use.",
    }


def _get_path(value: dict, dotted: str):
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _percentile(ordered: list, fraction: float):
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def summarize(samples: list[dict]) -> dict:
    metrics = {}
    for path in SUMMARY_PATHS:
        values = [_get_path(sample, path) for sample in samples]
        numbers = [value for value in values
                   if isinstance(value, (int, float)) and not isinstance(value, bool)]
        if not numbers:
            continue
        ordered = sorted(numbers)
        metrics[path] = {
            "count": len(ordered),
            "min": ordered[0],
            "p50": _percentile(ordered, 0.50),
            "p95": _percentile(ordered, 0.95),
            "max": ordered[-1],
            "mean": round(sum(ordered) / len(ordered), 3),
        }
    return {"sample_count": len(samples), "metrics": metrics}


class ResourceProbe:
    def __init__(self, service, *, proc_root: Path = PROC_ROOT,
                 cgroup_root: Path = CGROUP_ROOT, clock=None):
        self.service = service
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self.clock = clock or timestamp

    def sample(self, env_id: str, *, include_disk: bool = True) -> dict:
        view = self.service.inspect(env_id)
        running = view["runtime_state"] == "running"
        pid = (view.get("resources") or {}).get("pid") if running else None
        conmon_pid = (view.get("resources") or {}).get("conmon_pid") if running else None
        sample = {
            "collected_at": self.clock(),
            "environment_id": view["id"],
            "target": view["target"],
            "runtime_state": view["runtime_state"],
            "content_revision": view.get("content_revision"),
            "package_inventory_sha256": view.get("package_inventory_sha256"),
            "image_digest": (view.get("base_image") or {}).get("manifest_digest"),
            "container_id": view.get("writable_revision"),
            "kernel_matches_current_host": view.get("kernel_matches_current_host"),
            "pid": pid,
            "conmon_pid": conmon_pid,
            "cgroup": None,
            "processes": None,
            "runtime_processes": None,
            "errors": [],
        }
        if not running or not pid:
            sample["errors"].append(
                f"environment is {view['runtime_state']}; cgroup metrics are unavailable")
        else:
            cgroup, error = read_cgroup_path(
                pid, proc_root=self.proc_root, cgroup_root=self.cgroup_root)
            if error is not None:
                sample["errors"].append(f"cgroup: {error}")
            else:
                sample["cgroup"] = _cgroup_metrics(cgroup)
                if view.get("writable_revision") and view["writable_revision"] not in str(cgroup):
                    sample["cgroup"]["container_id_matches"] = False
                    sample["cgroup"]["errors"]["cgroup"] = (
                        "cgroup path does not contain the recorded container id")
                else:
                    sample["cgroup"]["container_id_matches"] = True
                pids, errors = _walk_cgroup_pids(cgroup)
                sample["processes"] = process_details(pids, proc_root=self.proc_root)
                sample["processes"]["errors"].extend(errors)
                if not pids:
                    sample["processes"]["errors"].append("the container cgroup has no processes")
                    sample["processes"]["complete"] = False
                    sample["processes"]["pss_bytes"] = None
                    sample["processes"]["rss_bytes"] = None
                scope = cgroup.parent
                if conmon_pid:
                    sample["runtime_processes"] = process_details(
                        [conmon_pid], proc_root=self.proc_root)
                    sample["runtime_processes"]["source"] = "conmon_pid"
                elif scope != cgroup and scope.name.startswith("libpod-"):
                    runtime_pids, runtime_errors = _direct_cgroup_pids(scope)
                    sample["runtime_processes"] = process_details(
                        runtime_pids, proc_root=self.proc_root)
                    sample["runtime_processes"]["errors"].extend(runtime_errors)
                    sample["runtime_processes"]["source"] = "scope_root"
        sample["host"] = host_snapshot(self.proc_root)
        if include_disk:
            sample["disk"] = self._disk(view)
        return sample

    def reset_peak(self, env_id: str) -> dict:
        view = self.service.inspect(env_id)
        if view["runtime_state"] != "running" or not (view.get("resources") or {}).get("pid"):
            return {"ok": False, "error": "environment is not running"}
        cgroup, error = read_cgroup_path(
            (view.get("resources") or {}).get("pid") or 0,
            proc_root=self.proc_root, cgroup_root=self.cgroup_root)
        if error is not None:
            return {"ok": False, "error": error}
        path = cgroup / "memory.peak"
        before, before_error = _read(path)
        if before_error is not None:
            return {"ok": False, "error": before_error, "before": None}
        try:
            fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
            try:
                written = os.write(fd, b"0\n")
            finally:
                os.close(fd)
        except OSError as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}", "before": before}
        after, after_error = _read(path)
        try:
            before_value = int(before) if before is not None else None
            after_value = int(after) if after is not None else None
        except ValueError:
            before_value = after_value = None
        changed = (after_error is None and before_value is not None
                   and after_value is not None and after_value < before_value)
        if after_error is not None:
            error = after_error
        elif not changed and before != "0":
            error = ("memory.peak did not decrease after writing 0; "
                     "peak reset is not effective on this kernel or cgroup")
        else:
            error = None
        return {
            "ok": after_error is None and (changed or before == "0"),
            "before": before,
            "after": after,
            "changed": changed,
            "written": written,
            "error": error,
        }

    def series(self, env_id: str, *, samples: int = 10, interval: float = 1.0,
               settle: float = 0.0, reset_peak: bool = False) -> dict:
        if samples < 1:
            raise MultilinuxError("samples must be at least 1")
        if interval < 0 or settle < 0:
            raise MultilinuxError("interval and settle must not be negative")
        started_at = self.clock()
        reset = self.reset_peak(env_id) if reset_peak else None
        started = time.monotonic()
        collected = []
        for index in range(samples):
            due = settle + index * interval
            wait = due - (time.monotonic() - started)
            if wait > 0:
                time.sleep(wait)
            collected.append(self.sample(
                env_id, include_disk=index in {0, samples - 1}))
        return {
            "environment_id": env_id,
            "started_at": started_at,
            "finished_at": self.clock(),
            "samples": samples,
            "interval_seconds": interval,
            "settle_seconds": settle,
            "disk_sampled_indices": sorted({0, samples - 1}),
            "peak_reset": reset,
            "series": collected,
            "summary": summarize(collected),
        }

    def _disk(self, view: dict) -> dict:
        runtime = self.service.runtime
        record_runtime = view.get("runtime") or {}
        name = record_runtime.get("container_name") or ""
        image = {"reference": (view.get("base_image") or {}).get("locked_reference")}
        try:
            image.update(runtime.image_info(image["reference"]))
        except Exception as exc:
            image["error"] = f"{type(exc).__name__}: {exc}"
        container = None
        if name:
            try:
                container = runtime.container_size(
                    name, project=record_runtime.get("project", ""),
                    env_id=view["id"])
            except Exception as exc:
                container = {"error": f"{type(exc).__name__}: {exc}"}
        shares = [_filesystem(item["host"]) for item in view.get("shared_mounts") or []]
        return {
            "image": image,
            "container": container,
            "shared_mounts": shares,
            "note": ("SizeRootFs includes lower layers shared with other containers; "
                     "sizes are not exclusive and must not be added across environments."),
        }
