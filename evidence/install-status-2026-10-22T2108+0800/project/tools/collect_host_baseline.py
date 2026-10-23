#!/usr/bin/env python3
"""Collect a bounded Linux host snapshot without changing system settings.

Uses Python's standard library and optional read-only version queries. Does not
list user processes, container names, network addresses, hostname, or machine ID.
This is a host snapshot, not a cross-distribution benchmark.
"""

import argparse
import datetime as dt
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import time
from zoneinfo import ZoneInfo


def read_text(path):
    try:
        return Path(path).read_text().strip()
    except (OSError, UnicodeError):
        return None


def run_read_only(argv):
    if not shutil.which(argv[0]):
        return None
    try:
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=8,
            env={**os.environ, "LC_ALL": "C"}, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def number(path):
    text = read_text(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def pressure(kind):
    text = read_text(f"/proc/pressure/{kind}")
    if text is None:
        return None
    result = {}
    for line in text.splitlines():
        fields = line.split()
        values = {}
        for item in fields[1:]:
            key, value = item.split("=", 1)
            values[key] = int(value) if key == "total" else float(value)
        result[fields[0]] = values
    return result


def collect(workspace):
    started = time.monotonic()
    release = {}
    for line in (read_text("/etc/os-release") or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in {"NAME", "ID", "VERSION_ID", "PRETTY_NAME"}:
            release[key] = value.strip('"')

    memory = {}
    wanted = {"MemTotal", "MemAvailable", "MemFree", "Cached", "SReclaimable",
              "SwapTotal", "SwapFree", "Shmem", "Slab"}
    for line in (read_text("/proc/meminfo") or "").splitlines():
        key, _, value = line.partition(":")
        if key in wanted:
            memory[key + "_bytes"] = int(value.split()[0]) * 1024

    cpuinfo = read_text("/proc/cpuinfo") or ""
    model = next((line.split(":", 1)[1].strip() for line in cpuinfo.splitlines()
                  if line.startswith("model name")), None)
    governors = sorted({value for path in Path("/sys/devices/system/cpu").glob(
        "cpu[0-9]*/cpufreq/scaling_governor") if (value := read_text(path))})

    swap = []
    for line in (read_text("/proc/swaps") or "").splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 5:
            swap.append({
                "backing": "zram" if fields[0].startswith("/dev/zram") else "disk_or_file",
                "type": fields[1], "size_bytes": int(fields[2]) * 1024,
                "used_bytes": int(fields[3]) * 1024, "priority": int(fields[4]),
            })

    zram = []
    for path in sorted(Path("/sys/block").glob("zram[0-9]*")):
        values = (read_text(path / "mm_stat") or "").split()
        zram.append({
            "device": path.name, "logical_capacity_bytes": number(path / "disksize"),
            "original_data_bytes": int(values[0]) if len(values) > 0 else None,
            "compressed_data_bytes": int(values[1]) if len(values) > 1 else None,
            "physical_memory_used_bytes": int(values[2]) if len(values) > 2 else None,
        })

    workspace = workspace.resolve()
    disk = shutil.disk_usage(workspace)
    mount_type = run_read_only(["findmnt", "-T", str(workspace), "-o", "FSTYPE", "-n"])
    mount_options = run_read_only(["findmnt", "-T", str(workspace), "-o", "OPTIONS", "-n"])

    versions = {}
    for name in ("podman", "docker", "toolbox", "distrobox", "systemd-nspawn", "bwrap",
                 "unshare", "perf", "hyperfine", "fio", "smem"):
        available = shutil.which(name) is not None
        version = run_read_only([name, "--version"]) if available and name in {
            "podman", "toolbox", "bwrap", "unshare"} else None
        versions[name] = {"available": available, "version": version}

    config_path = Path("/boot") / ("config-" + platform.release())
    kernel_config = {}
    wanted_config = {"CONFIG_NAMESPACES", "CONFIG_USER_NS", "CONFIG_PID_NS", "CONFIG_NET_NS",
                     "CONFIG_CGROUPS", "CONFIG_MEMCG", "CONFIG_OVERLAY_FS", "CONFIG_BTRFS_FS"}
    for line in (read_text(config_path) or "").splitlines():
        key, sep, value = line.partition("=")
        if sep and key in wanted_config:
            kernel_config[key] = value

    result = {
        "schema_version": 1,
        "captured_at": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds"),
        "scope": "Host baseline only; no container or cross-distribution performance test executed.",
        "os_release": release, "kernel_release": platform.release(),
        "architecture": platform.machine(),
        "cpu": {"model": model, "logical_cpus": os.cpu_count(), "governors": governors},
        "memory": memory, "swap_devices": swap, "zram": zram,
        "workspace_filesystem": {
            "type": mount_type, "options": mount_options,
            "total_bytes": disk.total, "used_bytes": disk.used, "free_bytes": disk.free,
            "note": "Filesystem-wide values; not the project's or a container's disk use.",
        },
        "cgroup": {"v2": Path("/sys/fs/cgroup/cgroup.controllers").exists(),
                   "root_controllers": read_text("/sys/fs/cgroup/cgroup.controllers")},
        "security": {"selinux_mode": run_read_only(["getenforce"]),
                     "apparmor_module_enabled": read_text("/sys/module/apparmor/parameters/enabled")},
        "namespace_policy": {
            "max_user_namespaces": number("/proc/sys/user/max_user_namespaces"),
            "unprivileged_userns_clone": number("/proc/sys/kernel/unprivileged_userns_clone"),
            "note": "Policy values do not prove that a namespace or container can actually start.",
        },
        "kernel_config": kernel_config or None, "tools": versions,
        "pressure": {kind: pressure(kind) for kind in ("cpu", "memory", "io")},
        "limitations": [
            "A live desktop snapshot contains unrelated application activity.",
            "MemAvailable is an estimate, not a guaranteed allocation budget.",
            "Swap use is logical swapped data; zram's physical use is reported separately.",
            "No package installation, image pull, container start, or setting change is performed.",
        ],
    }
    result["collection_seconds"] = round(time.monotonic() - started, 3)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, help="Write JSON here; otherwise print to stdout.")
    args = parser.parse_args()
    result = collect(args.workspace)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    else:
        print(payload, end="")


if __name__ == "__main__":
    main()
