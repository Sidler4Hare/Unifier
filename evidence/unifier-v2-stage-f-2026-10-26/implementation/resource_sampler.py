#!/usr/bin/env python3
"""Reusable, read-only resource sampler for Stage F local probes.

It samples:

* host ``/proc`` memory, PSI and load;
* the real container cgroup of each fixture (memory.current/peak/stat/events,
  pressure, pids);
* host PID trees for project-session workers/proxies, FUSE worker/broker/
  launcher, storage helpers and conmon;
* guest processes discovered from the container cgroup, with PSS attempted
  from host ``/proc/<pid>/smaps_rollup`` and a documented guest-/proc/
  ``podman top`` fallback when host permission is denied.

PSS and cgroup values are reported separately and are never added together.
Unreadable targets are recorded as UNKNOWN/error with coverage; they are not
silently replaced by zero.  Process identity is PID + kernel start time.

The module can be imported by probe scripts or run as a bounded sampler:

    python3 resource_sampler.py --fixtures fixtures.json --run-dir RUN --seconds 30
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402

CGROUP_ROOT = Path("/sys/fs/cgroup")
PROC_ROOT = Path("/proc")
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


def _read(path: Path) -> tuple[str | None, str | None]:
    try:
        return path.read_text(encoding="utf-8").strip(), None
    except (OSError, UnicodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _int(value, error):
    if error is not None or value is None:
        return None, error or "value is missing"
    try:
        return int(value), None
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
        item = {}
        for token in fields[1:]:
            key, _sep, raw = token.partition("=")
            if not key:
                continue
            try:
                item["total_us" if key == "total" else key] = (
                    int(raw) if key == "total" else float(raw))
            except ValueError:
                continue
        result[fields[0]] = item
    return result


def parse_smaps_rollup(text: str) -> dict:
    values = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if not separator or key == "Name":
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


def cgroup_path_for(pid: int, *, proc_root: Path = PROC_ROOT,
                    cgroup_root: Path = CGROUP_ROOT) -> tuple[Path | None, str | None]:
    text, error = _read(Path(proc_root) / str(pid) / "cgroup")
    if error is not None:
        return None, error
    for line in (text or "").splitlines():
        if line.startswith("0::"):
            relative = line[3:].strip()
            if not relative:
                return None, "process reports the cgroup root"
            return Path(cgroup_root) / relative.lstrip("/"), None
    return None, "no cgroup v2 entry"


def walk_cgroup_pids(cgroup: Path) -> tuple[list[int], list[str]]:
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


def cgroup_metrics(cgroup: Path) -> dict:
    result = {"path": str(cgroup), "errors": {}, "stat": None, "events": None,
              "memory_pressure": None}
    scalar = {
        "memory_current_bytes": ("memory.current", int),
        "memory_peak_bytes": ("memory.peak", int),
        "memory_swap_current_bytes": ("memory.swap.current", int),
        "pids_current": ("pids.current", int),
        "pids_peak": ("pids.peak", int),
    }
    for target, (name, parser) in scalar.items():
        text, error = _read(cgroup / name)
        value, parse_error = _int(text, error)
        result[target] = value
        if parse_error is not None:
            result["errors"][name] = parse_error
    for target, (name, parser) in {
            "stat": ("memory.stat", parse_kv_int),
            "events": ("memory.events", parse_kv_int),
            "cpu_stat": ("cpu.stat", parse_kv_int),
            "memory_pressure": ("memory.pressure", parse_pressure),
            "cpu_pressure": ("cpu.pressure", parse_pressure),
            "io_pressure": ("io.pressure", parse_pressure),
    }.items():
        text, error = _read(cgroup / name)
        if error is not None:
            result[target] = None
            result["errors"][name] = error
        else:
            result[target] = parser(text or "")
    return result


def process_row(pid: int, table: dict, *, proc_root: Path = PROC_ROOT,
                cid: str | None = None, target: str | None = None,
                allow_guest_fallback: bool = False,
                fallback_state: dict | None = None) -> dict:
    stat = table.get(pid)
    if stat is None:
        return {"pid": pid, "identity": None, "error": "process disappeared from /proc"}
    row = {
        "pid": pid,
        "identity": stat["starttime"],
        "comm": stat["comm"],
        "ppid": stat["ppid"],
        "state": stat["state"],
        "pss_bytes": None,
        "pss_source": None,
        "pss_error": None,
        "rss_bytes": None,
        "threads": None,
        "fds": None,
        "fd_error": None,
    }
    smaps_text, smaps_error = _read(Path(proc_root) / str(pid) / "smaps_rollup")
    if smaps_error is None:
        values = parse_smaps_rollup(smaps_text or "")
        row["pss_bytes"] = values.get("pss_bytes")
        row["rss_bytes"] = values.get("rss_bytes")
        row["pss_source"] = "host-smaps_rollup"
        if row["pss_bytes"] is None:
            row["pss_error"] = "smaps_rollup has no Pss field"
    else:
        row["pss_error"] = smaps_error
    status_text, status_error = _read(Path(proc_root) / str(pid) / "status")
    if status_error is None:
        for line in (status_text or "").splitlines():
            if line.startswith("Threads:"):
                try:
                    row["threads"] = int(line.split()[1])
                except (IndexError, ValueError):
                    pass
            elif line.startswith("VmRSS:") and row["rss_bytes"] is None:
                try:
                    row["rss_bytes"] = int(line.split()[1]) * 1024
                except (IndexError, ValueError):
                    pass
    try:
        row["fds"] = len(list((Path(proc_root) / str(pid) / "fd").iterdir()))
    except OSError as exc:
        row["fd_error"] = f"{type(exc).__name__}: {exc}"

    # If the host cannot read a guest process's smaps, try the guest's own
    # /proc through podman top (host pid -> guest pid).  A failure remains an
    # explicit UNKNOWN; it is never converted to zero.
    if (row["pss_bytes"] is None and cid and target and allow_guest_fallback
            and fallback_state is not None
            and fallback_state.get("count", 0) < 3):
        key = (cid, pid)
        if key not in fallback_state["attempted"]:
            fallback_state["attempted"].add(key)
            fallback_state["count"] += 1
            mapped, map_error = guest_pid_for_host(cid, pid)
            if mapped is not None:
                result = subprocess.run(
                    ["podman", "exec", "--user", common.SESSION_USER, cid,
                     "/bin/cat", f"/proc/{mapped}/smaps_rollup"],
                    capture_output=True, text=True, timeout=10, check=False)
                if result.returncode == 0:
                    values = parse_smaps_rollup(result.stdout)
                    if values.get("pss_bytes") is not None:
                        row["pss_bytes"] = values["pss_bytes"]
                        row["rss_bytes"] = values.get("rss_bytes", row["rss_bytes"])
                        row["pss_source"] = "guest-proc-via-podman-exec"
                        row["pss_guest_pid"] = mapped
                        row["pss_error"] = None
                    else:
                        row["pss_error"] = "guest smaps_rollup has no Pss field"
                else:
                    row["pss_error"] = (
                        f"guest smaps fallback failed: {result.stderr[-500:]}")
            else:
                row["pss_error"] = f"guest pid mapping failed: {map_error}"
    return row


def guest_pid_for_host(cid: str, host_pid: int) -> tuple[int | None, str | None]:
    result = subprocess.run(
        ["podman", "top", cid, "hpid", "pid"],
        capture_output=True, text=True, timeout=10, check=False)
    if result.returncode != 0:
        return None, (result.stderr or result.stdout)[-500:]
    for line in result.stdout.splitlines()[1:]:
        fields = line.split()
        if len(fields) >= 2 and fields[0].isdigit() and int(fields[0]) == host_pid:
            try:
                return int(fields[1]), None
            except ValueError:
                break
    return None, f"host pid {host_pid} not present in podman top"


def podman_state(name: str) -> dict:
    result = common.run_command(
        ["podman", "inspect", "--format",
         "{{.Id}}\t{{.State.Pid}}\t{{.State.Running}}\t{{.State.Status}}\t"
         "{{.State.StartedAt}}\t{{.State.ConmonPid}}\t{{.State.CgroupPath}}", name],
        timeout=30)
    if result["exit"] != 0:
        return {"name": name, "error": (result.get("stderr") or result.get("error") or "")[-1000:]}
    fields = (result["stdout"].strip().split("\t") + [""] * 7)[:7]
    pid = int(fields[1]) if fields[1].isdigit() else None
    conmon = int(fields[5]) if fields[5].isdigit() else None
    return {
        "name": name, "id": fields[0] or None, "pid": pid,
        "running": fields[2] == "true", "status": fields[3] or None,
        "started_at": fields[4] or None, "conmon_pid": conmon,
        "cgroup_path": fields[6] or None, "error": None,
    }


def _status_and_children(proc_root: Path) -> dict:
    return common.proc_table(proc_root)


class ResourceSampler(threading.Thread):
    def __init__(self, fixtures: dict, facade: dict, *, run_dir: Path,
                 interval: float = 1.0, proc_root: Path = PROC_ROOT,
                 cgroup_root: Path = CGROUP_ROOT, client_pids=None):
        super().__init__(name="stage-f-resource-sampler", daemon=True)
        self.fixtures = fixtures
        self.service = facade["service"]
        self.sessions = facade.get("sessions")
        self.views = facade.get("views")
        self.project = Path(fixtures["project"])
        self.run_dir = Path(run_dir)
        self.interval = max(0.2, float(interval))
        self.proc_root = Path(proc_root)
        self.cgroup_root = Path(cgroup_root)
        self.client_pids = set(client_pids or [])
        self.samples_path = self.run_dir / "resources.jsonl"
        self.guard_path = self.run_dir / "resource-guard.jsonl"
        self.summary_path = self.run_dir / "resources-summary.json"
        self._stop_requested = threading.Event()
        self._paused = threading.Event()
        self.abort_event = threading.Event()
        self.abort_reason = None
        self.samples = []
        self._env_cache = {}
        self._baseline_oom = {}
        self._close_reason = None

    # ----- lifecycle ------------------------------------------------------
    def pause(self):
        self._paused.set()

    def resume(self):
        self._paused.clear()

    def stop(self, reason="requested"):
        self._close_reason = reason
        self._stop_requested.set()

    def run(self):
        index = 0
        while not self._stop_requested.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            started = time.monotonic_ns()
            try:
                sample = self.sample_once(index)
            except Exception as exc:
                sample = {
                    "index": index, "at": common.now_iso(),
                    "duration_ns": time.monotonic_ns() - started,
                    "errors": [f"sampler exception: {type(exc).__name__}: {exc}"],
                }
                self.abort_event.set()
                self.abort_reason = f"sampler failure: {type(exc).__name__}: {exc}"
            sample["duration_ns"] = time.monotonic_ns() - started
            self.samples.append(sample)
            common.append_jsonl(self.samples_path, sample)
            if sample.get("guard") and sample["guard"].get("ok") is False:
                self.abort_event.set()
                self.abort_reason = "; ".join(sample["guard"].get("reasons") or [])
                common.append_jsonl(self.guard_path, sample["guard"])
            if sample.get("errors"):
                common.append_jsonl(self.guard_path, {
                    "at": sample["at"], "kind": "sample-errors",
                    "errors": sample["errors"][:20]})
            index += 1
            elapsed = time.monotonic_ns() - started
            wait = self.interval - elapsed / 1_000_000_000
            if wait > 0 and not self._stop_requested.is_set():
                self._stop_requested.wait(wait)
        self.write_summary()

    # ----- one sample -----------------------------------------------------
    def _env_info(self, target: str, item: dict) -> dict:
        env_id = item["environment"]["id"]
        expected_cid = item["environment"]["runtime"]["container_id"]
        cached = self._env_cache.get(target)
        now = time.monotonic()
        need_refresh = cached is None or now - cached["at"] > 5.0
        if cached is not None and cached["state"].get("pid"):
            stat = common.parse_proc_stat(cached["state"]["pid"], self.proc_root)
            if stat is None or stat["state"] == "Z":
                need_refresh = True
        if need_refresh:
            record = self.service.store.load(env_id)
            state = podman_state(record["runtime"]["container_name"])
            cached = {"at": now, "state": state, "record": record}
            self._env_cache[target] = cached
        state = dict(cached["state"])
        record = cached["record"]
        state["environment_id"] = env_id
        state["expected_container_id"] = expected_cid
        state["container_id_matches"] = state.get("id") == expected_cid
        state["user_namespace"] = (record.get("runtime") or {}).get("user_namespace")
        state["container_name"] = (record.get("runtime") or {}).get("container_name")
        state["project"] = (record.get("runtime") or {}).get("project")
        return cached, state

    def sample_once(self, index: int = 0) -> dict:
        sample = {
            "index": index,
            "at": common.now_iso(),
            "monotonic_ns": time.monotonic_ns(),
            "guard": common.memory_guard_state(label=f"sample-{index}"),
            "host": None,
            "environments": {},
            "sessions": [],
            "views": [],
            "host_processes": {"categories": {}, "rows": [], "unattributed": [],
                               "discovery": []},
            "errors": [],
        }
        try:
            from multilinux.resources import host_snapshot
            raw_host = host_snapshot(self.proc_root)
            sample["host"] = {
                "meminfo": raw_host.get("meminfo"),
                "pressure": raw_host.get("pressure"),
                "load_average": raw_host.get("load_average"),
            }
        except Exception as exc:
            sample["errors"].append(
                f"host snapshot: {type(exc).__name__}: {exc}")

        table = _status_and_children(self.proc_root)
        env_guest_pids = {}
        conmon_pids = {}
        for target in common.TARGETS:
            if target not in (self.fixtures.get("targets") or {}):
                continue
            item = self.fixtures["targets"][target]
            cached, state = self._env_info(target, item)
            env_row = {
                "environment_id": item["environment"]["id"],
                "target": target,
                "container": state,
                "cgroup": None,
                "guest_pids": [],
                "guest_processes": None,
                "coverage": {"cgroup": "UNKNOWN", "guest_pss": "UNKNOWN"},
            }
            if state.get("error"):
                env_row["error"] = state["error"]
                sample["environments"][target] = env_row
                continue
            if not state.get("running") or not state.get("pid"):
                env_row["note"] = "environment is not running; cgroup/PSS UNKNOWN"
                sample["environments"][target] = env_row
                continue
            cgroup_path, error = cgroup_path_for(
                state["pid"], proc_root=self.proc_root, cgroup_root=self.cgroup_root)
            if error:
                sample["errors"].append(f"{target}: cgroup: {error}")
            if cgroup_path is None:
                env_row["note"] = "cgroup path unavailable"
                sample["environments"][target] = env_row
                continue
            metrics = cgroup_metrics(cgroup_path)
            pids, pid_errors = walk_cgroup_pids(cgroup_path)
            env_row["cgroup"] = metrics
            env_row["guest_pids"] = pids
            env_row["coverage"]["cgroup"] = "PRESENT" if metrics.get("memory_current_bytes") is not None else "PARTIAL"
            if pid_errors:
                env_row["cgroup_pid_errors"] = pid_errors
            fallback_state = {"attempted": set(), "count": 0}
            rows = [process_row(pid, table, proc_root=self.proc_root,
                                cid=state.get("id"), target=target,
                                allow_guest_fallback=True,
                                fallback_state=fallback_state)
                    for pid in pids]
            known = [row for row in rows if row.get("pss_bytes") is not None]
            unknown = [row for row in rows if row.get("pss_bytes") is None]
            env_row["guest_processes"] = {
                "expected_count": len(pids),
                "known_pss_count": len(known),
                "unknown_pss_count": len(unknown),
                "pss_complete": len(known) == len(pids) and bool(pids),
                "pss_partial_bytes": sum(row["pss_bytes"] for row in known),
                "pss_bytes": (sum(row["pss_bytes"] for row in known)
                              if len(known) == len(pids) and pids else None),
                "rss_partial_bytes": sum(row.get("rss_bytes") or 0 for row in known),
                "threads": sum(row.get("threads") or 0 for row in known),
                "fds": sum(row.get("fds") or 0 for row in known),
                "fallback_attempts": fallback_state["count"],
                "rows": rows,
            }
            env_row["coverage"]["guest_pss"] = (
                "COMPLETE" if env_row["guest_processes"]["pss_complete"]
                else ("PARTIAL" if known else "UNKNOWN"))
            env_guest_pids[target] = set(pids)
            if state.get("conmon_pid"):
                conmon_pids[target] = state["conmon_pid"]
            oom = (metrics.get("events") or {}).get("oom_kill")
            if index == 0 or target not in self._baseline_oom:
                self._baseline_oom[target] = oom
            elif oom is not None and self._baseline_oom[target] is not None and oom > self._baseline_oom[target]:
                env_row["oom_kill_increase"] = oom - self._baseline_oom[target]
                sample["errors"].append(
                    f"{target}: cgroup oom_kill increased by {env_row['oom_kill_increase']}")
                self.abort_event.set()
                self.abort_reason = f"{target}: cgroup OOM kill observed"
            sample["environments"][target] = env_row

        session_records = []
        if self.sessions is not None:
            for record in self.sessions.store.list():
                session_records.append({
                    "session_uuid": record["session_uuid"],
                    "environment_id": record["environment_id"],
                    "state": record.get("state"),
                    "worker_pid": record.get("worker_pid"),
                    "worker_stamp": record.get("worker_stamp"),
                    "child_pid": record.get("child_pid"),
                    "child_stamp": record.get("child_stamp"),
                    "guest_pid": record.get("guest_pid"),
                    "guest_start": record.get("guest_start"),
                    "lease_token": record.get("lease_token"),
                })
        sample["sessions"] = session_records
        view_records = []
        if self.views is not None:
            for record in self.views.registry.list():
                view_records.append({
                    "view_uuid": record["view_uuid"],
                    "environment_id": record["environment_id"],
                    "state": record.get("state"),
                    "worker_pid": record.get("worker_pid"),
                    "worker_stamp": record.get("worker_stamp"),
                    "broker_pid": record.get("broker_pid"),
                    "broker_stamp": record.get("broker_stamp"),
                    "broker_launcher_pid": record.get("broker_launcher_pid"),
                    "broker_launcher_stamp": record.get("broker_launcher_stamp"),
                    "mountpoint": record.get("mountpoint"),
                })
        sample["views"] = view_records

        self._host_processes(sample, table, env_guest_pids, conmon_pids,
                             session_records, view_records)
        return sample

    def _host_processes(self, sample, table, env_guest_pids, conmon_pids,
                        session_records, view_records):
        guest_claimed = set()
        for pids in env_guest_pids.values():
            guest_claimed.update(pids)
        claimed = set(guest_claimed)
        categories = {}
        discovery = []

        def add(category, pid, source):
            if pid in claimed or pid not in table:
                return
            claimed.add(pid)
            categories.setdefault(category, set()).add(pid)
            discovery.append({"category": category, "pid": pid,
                              "starttime": table[pid]["starttime"],
                              "source": source})

        for target, pid in conmon_pids.items():
            add("conmon", pid, f"{target}:State.ConmonPid")

        for record in session_records:
            root = record.get("worker_pid")
            if root:
                for pid in common.descendants_of({root}, table):
                    if pid in claimed:
                        continue
                    command = common.cmdline_of(pid, self.proc_root)
                    if "podman" in command and " exec" in " " + command:
                        category = "session-podman-proxy"
                    else:
                        category = "session-worker"
                    add(category, pid, f"session {record['session_uuid']}")

        for record in view_records:
            for category, key in (("view-worker", "worker_pid"),
                                  ("view-broker", "broker_pid"),
                                  ("view-launcher", "broker_launcher_pid")):
                root = record.get(key)
                if not root:
                    continue
                for pid in common.descendants_of({root}, table):
                    if pid in claimed:
                        continue
                    command = common.cmdline_of(pid, self.proc_root)
                    if any(token in command for token in
                           ("unshare", "fuse-overlayfs", "storage")):
                        actual = "storage-helper"
                    else:
                        actual = category
                    add(actual, pid, f"view {record['view_uuid']} {key}")

        for pid in self.client_pids:
            add("probe-client", pid, "probe launcher")

        # Processes that still match a fixture identifier but were not reached
        # through a known root are reported instead of being assumed absent.
        needles = set()
        for record in view_records:
            needles.add(record["view_uuid"])
        for target, item in (self.fixtures.get("targets") or {}).items():
            needles.add(item["environment"]["runtime"]["container_id"])
            needles.add(item["environment"]["runtime"]["container_name"])
        for pid, stat in table.items():
            if pid in claimed:
                continue
            command = common.cmdline_of(pid, self.proc_root)
            if any(needle and needle in command for needle in needles):
                sample["host_processes"]["unattributed"].append({
                    "pid": pid, "starttime": stat["starttime"],
                    "comm": stat["comm"], "cmdline": command[:1000]})

        rows = []
        for category, pids in sorted(categories.items()):
            cat_rows = [process_row(pid, table, proc_root=self.proc_root)
                        for pid in sorted(pids)]
            known = [row for row in cat_rows if row.get("pss_bytes") is not None]
            rows.extend(cat_rows)
            sample["host_processes"]["categories"][category] = {
                "process_count": len(cat_rows),
                "known_pss_count": len(known),
                "unknown_pss_count": len(cat_rows) - len(known),
                "pss_complete": len(known) == len(cat_rows) and bool(cat_rows),
                "pss_bytes": (sum(row["pss_bytes"] for row in known)
                              if len(known) == len(cat_rows) and cat_rows else None),
                "pss_partial_bytes": sum(row["pss_bytes"] for row in known),
                "rss_partial_bytes": sum(row.get("rss_bytes") or 0 for row in known),
                "threads": sum(row.get("threads") or 0 for row in known),
                "fds": sum(row.get("fds") or 0 for row in known),
                "rows": cat_rows,
            }
        sample["host_processes"]["rows"] = rows
        sample["host_processes"]["discovery"] = discovery
        sample["host_processes"]["note"] = (
            "Categories are disjoint. PSS is host-process coverage and is not "
            "part of the guest cgroup memory.current total.")

    # ----- summary --------------------------------------------------------
    def write_summary(self) -> dict:
        summary = summarize_samples(self.samples, self.run_dir)
        summary["close_reason"] = self._close_reason
        summary["abort"] = {
            "aborted": self.abort_event.is_set(),
            "reason": self.abort_reason,
        }
        summary["sampling"] = {
            "interval_seconds": self.interval,
            "sample_count": len(self.samples),
            "mean_sample_duration_ms": (
                round(sum(item.get("duration_ns") or 0 for item in self.samples)
                      / len(self.samples) / 1_000_000.0, 3)
                if self.samples else None),
            "note": ("Timed command paths pause this sampler where possible; "
                     "the sampler itself only reads /proc and cgroup files."),
        }
        common.atomic_write_json(self.summary_path, summary)
        return summary


def _get_path(value: dict, dotted: str):
    current = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def summarize_samples(samples: list[dict], run_dir: Path | None = None) -> dict:
    series = {}
    coverage = {}
    errors = []
    for sample in samples:
        host = sample.get("host") or {}
        values = {
            "host.mem_available_bytes": _get_path(host, "meminfo.MemAvailable_bytes"),
            "host.swap_free_bytes": _get_path(host, "meminfo.SwapFree_bytes"),
            "host.psi_memory_full_avg10": _get_path(host, "pressure.memory.full.avg10"),
            "host.psi_memory_some_avg10": _get_path(host, "pressure.memory.some.avg10"),
            "host.sample_duration_ns": sample.get("duration_ns"),
        }
        for target, env in (sample.get("environments") or {}).items():
            cgroup = env.get("cgroup") or {}
            guest = env.get("guest_processes") or {}
            prefix = f"env.{target}"
            values.update({
                f"{prefix}.cgroup.memory_current_bytes": cgroup.get("memory_current_bytes"),
                f"{prefix}.cgroup.memory_peak_bytes": cgroup.get("memory_peak_bytes"),
                f"{prefix}.cgroup.memory_swap_current_bytes": cgroup.get("memory_swap_current_bytes"),
                f"{prefix}.cgroup.pids_current": cgroup.get("pids_current"),
                f"{prefix}.guest.pss_partial_bytes": guest.get("pss_partial_bytes"),
                f"{prefix}.guest.pss_unknown_count": guest.get("unknown_pss_count"),
                f"{prefix}.guest.threads": guest.get("threads"),
                f"{prefix}.guest.fds": guest.get("fds"),
            })
            cov = coverage.setdefault(target, {
                "samples": 0, "running": 0, "cgroup_present": 0,
                "guest_pss_complete": 0, "guest_pss_partial": 0,
                "guest_pss_unknown": 0,
            })
            cov["samples"] += 1
            if (env.get("container") or {}).get("running"):
                cov["running"] += 1
            if cgroup.get("memory_current_bytes") is not None:
                cov["cgroup_present"] += 1
            state = (env.get("coverage") or {}).get("guest_pss")
            if state == "COMPLETE":
                cov["guest_pss_complete"] += 1
            elif state == "PARTIAL":
                cov["guest_pss_partial"] += 1
            else:
                cov["guest_pss_unknown"] += 1
        for category, item in ((sample.get("host_processes") or {}).get("categories") or {}).items():
            values[f"host.processes.{category}.pss_partial_bytes"] = item.get("pss_partial_bytes")
            values[f"host.processes.{category}.pss_unknown_count"] = item.get("unknown_pss_count")
            values[f"host.processes.{category}.fds"] = item.get("fds")
            values[f"host.processes.{category}.threads"] = item.get("threads")
        for key, value in values.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                series.setdefault(key, []).append(value)
        errors.extend((sample.get("errors") or [])[:5])

    metrics = {}
    for key, values in sorted(series.items()):
        ordered = sorted(values)
        metrics[key] = {
            "count": len(ordered),
            "min": ordered[0],
            "p50": common.percentile(ordered, 0.50),
            "p95": common.percentile(ordered, 0.95),
            "max": ordered[-1],
            "mean": round(sum(ordered) / len(ordered), 3),
            "first": values[0],
            "last": values[-1],
        }
    return {
        "schema_version": 1,
        "scope": "read-only local process/cgroup/host sampling; PSS and cgroup not summed",
        "sample_count": len(samples),
        "metrics": metrics,
        "coverage": coverage,
        "errors": errors[:100],
        "notes": [
            "PSS sums are only meaningful when pss_complete is true.",
            "Partial PSS coverage is reported as partial, not as zero usage.",
            "cgroup memory.current is the guest charge and is not added to host PSS.",
            "UNKNOWN means the kernel/runtime denied or removed an observable.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Sample real fixture PID trees, cgroups and host pressure.")
    parser.add_argument("--fixtures", type=Path, default=common.FIXTURES_DEFAULT)
    parser.add_argument("--run-dir", type=Path, required=True,
                        help="new directory for resources.jsonl and summary")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="bounded sampling duration in seconds (default 10)")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    run_dir = common.create_run_dir(args.run_dir)
    progress = common.Progress(run_dir, "resource_sampler", seconds=args.seconds)
    try:
        common.guard_memory(label="resource sampler preflight")
        fixtures = common.load_fixtures(args.fixtures)
        facade = common.product(fixtures)
        sampler = ResourceSampler(fixtures, facade, run_dir=run_dir,
                                  interval=args.interval)
        progress.update("sampling", message="starting sampler")
        sampler.start()
        deadline = time.monotonic() + max(0.0, args.seconds)
        while time.monotonic() < deadline:
            if sampler.abort_event.is_set():
                break
            time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
        sampler.stop("bounded sampler complete")
        sampler.join(timeout=15)
        summary = common.read_json(run_dir / "resources-summary.json")
        status = "passed" if not sampler.abort_event.is_set() and not summary.get("errors") else "failed"
        if status == "failed":
            progress.failure(common.ProbeError(
                "sampler reported abort or errors; see resources-summary.json"),
                phase="sampling")
        progress.finish(status=status, samples=summary.get("sample_count"),
                        abort=summary.get("abort"))
        common.write_summary(run_dir, summary)
        print(json.dumps({
            "status": status, "run_dir": str(run_dir),
            "samples": summary.get("sample_count"),
            "coverage": summary.get("coverage"),
        }, ensure_ascii=False))
        return 0 if status == "passed" else 1
    except BaseException as exc:
        progress.failure(exc)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
