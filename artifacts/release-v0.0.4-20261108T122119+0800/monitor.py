#!/usr/bin/env python3
"""Sample host memory outside the release cgroup. Abort the unit on a breach."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

RUN = Path(__file__).resolve().parent
UNIT = "unifier-v004-build"
INTERVAL = 0.25
RESERVE_KIB = 1024 * 1024
ADMISSION_KIB = RESERVE_KIB + 512 * 1024 + 128 * 1024


def meminfo() -> dict[str, int]:
    values = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = line.partition(":")
        values[key] = int(rest.strip().split()[0])
    return values


def psi() -> dict[str, float]:
    text = Path("/proc/pressure/memory").read_text().splitlines()
    out = {}
    for line in text:
        kind, _, rest = line.partition(" ")
        fields = dict(part.split("=") for part in rest.split())
        out[kind] = float(fields["avg10"])
    return out


def cgroup_path() -> Path | None:
    pid_file = RUN / "worker.pid"
    if not pid_file.is_file():
        return None
    pid = pid_file.read_text().strip()
    for line in Path(f"/proc/{pid}/cgroup").read_text().splitlines():
        path = line.split(":", 2)[-1]
        if path:
            return Path("/sys/fs/cgroup") / path.lstrip("/")
    return None


def read_int(path: Path) -> int | None:
    if not path.is_file():
        return None
    text = path.read_text().strip()
    if text == "max":
        return None
    return int(text)


def abort(reason: str, sample: dict) -> None:
    payload = {"reason": reason, "sample": sample, "time": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    (RUN / "abort.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    os.system(f"systemctl --user stop {UNIT}")
    print(f"ABORT {reason}", flush=True)
    raise SystemExit(2)


def main() -> None:
    log = (RUN / "samples.jsonl").open("a", encoding="utf-8")
    low_mem = 0
    high_mem = 0
    high_tasks = 0
    unread = 0
    psi_bad_since = None
    started = time.monotonic()
    while not (RUN / "monitor-stop").is_file():
        now = time.monotonic()
        sample = {"t": round(now - started, 3)}
        try:
            info = meminfo()
            pressure = psi()
            sample["mem_available_kib"] = info["MemAvailable"]
            sample["psi_some_avg10"] = pressure.get("some")
            sample["psi_full_avg10"] = pressure.get("full")
            group = cgroup_path()
            if group is not None:
                sample["cgroup"] = str(group)
                sample["memory_current"] = read_int(group / "memory.current")
                sample["memory_max"] = read_int(group / "memory.max")
                sample["pids_current"] = read_int(group / "pids.current")
                sample["pids_max"] = read_int(group / "pids.max")
            unread = 0
        except OSError as exc:
            unread += 1
            sample["error"] = str(exc)
            log.write(json.dumps(sample) + "\n")
            log.flush()
            if unread * INTERVAL > 2:
                abort("metrics unreadable", sample)
            time.sleep(INTERVAL)
            continue
        log.write(json.dumps(sample) + "\n")
        log.flush()
        if sample["mem_available_kib"] < RESERVE_KIB:
            low_mem += 1
            if low_mem >= 2:
                abort("MemAvailable below 1 GiB", sample)
        else:
            low_mem = 0
        full = sample["psi_full_avg10"] or 0
        some = sample["psi_some_avg10"] or 0
        if full > 3 or some > 10:
            psi_bad_since = psi_bad_since or now
            if now - psi_bad_since >= 2:
                abort("memory pressure", sample)
        else:
            psi_bad_since = None
        current = sample.get("memory_current")
        limit = sample.get("memory_max")
        if current is not None and limit:
            if current >= int(limit * 0.9):
                high_mem += 1
                if high_mem >= 2:
                    abort("cgroup memory at 90 percent", sample)
            else:
                high_mem = 0
        tasks = sample.get("pids_current")
        task_limit = sample.get("pids_max")
        if tasks is not None and task_limit:
            if tasks >= int(task_limit * 0.9):
                high_tasks += 1
                if high_tasks >= 2:
                    abort("cgroup tasks at 90 percent", sample)
            else:
                high_tasks = 0
        time.sleep(INTERVAL)
    (RUN / "admission-threshold-kib").write_text(str(ADMISSION_KIB) + "\n")


if __name__ == "__main__":
    main()
