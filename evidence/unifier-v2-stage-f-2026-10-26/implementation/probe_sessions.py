#!/usr/bin/env python3
"""Stage F real project-session concurrency and end-to-end timing probe.

Fedora 44 is the mandatory concurrency target: N=1/2/4/8, three waves each.
Every wave starts from the stopped fixture, proves one cold container start,
N live leases and N distinct guest identities, keeps the container alive after
the first session exits, then verifies last-lease auto-stop and no unresolved
sessions.  A separate mixed wave runs two sessions on each of the three
distros at once.  Warm/cold command timings and prepare/authorization timings
are kept separate from the concurrency workload and from GUI first-frame.

The workload is deliberately small (a 4 KiB dd + sha256sum in a ~2 s shell
session) so an 8 GiB desktop is not stressed.  False/early completion is a
failure; perf numbers are observations, not an SLO.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402
from resource_sampler import ResourceSampler  # noqa: E402


def parse_result(text: str) -> dict | None:
    for line in (text or "").splitlines():
        if not line.startswith("RESULT "):
            continue
        values = {}
        for token in line.split()[1:]:
            key, _sep, value = token.partition("=")
            if key:
                values[key] = value
        return values
    return None


def guest_command(target: str, marker_dir: Path, index: int,
                  extra_sleep: float) -> str:
    ready = marker_dir / f"{index}.ready"
    output = marker_dir / f"{index}.out"
    done = marker_dir / f"{index}.done"
    script = f"""
set -eu
. /etc/os-release
printf '%s\\n' "$ID" > {common.sh_quote(ready)}
tries=0
while [ ! -f {common.sh_quote(marker_dir / 'go')} ]; do
    tries=$((tries + 1))
    [ "$tries" -lt 1200 ] || exit 98
    sleep 0.05
done
sleep 2
if [ "$ID" != {common.sh_quote(common.GUEST_IDS[target])} ]; then
    echo "wrong distribution: $ID" >&2
    exit 9
fi
dd if=/dev/zero of={common.sh_quote(output)} bs=4096 count=1 status=none
H=$(sha256sum {common.sh_quote(output)} | cut -d' ' -f1)
U=$(id -u)
printf 'RESULT id=%s uid=%s hash=%s\\n' "$ID" "$U" "$H"
sleep {extra_sleep:.3f}
printf 'DONE\\n' > {common.sh_quote(done)}
"""
    return script


def launch_cli(project: Path, alias: str, script: str, *, cwd: Path):
    command = [sys.executable, str(project / "tools" / "unifier.py"),
               "project", "run", alias, "--", "/bin/sh", "-c", script]
    environment = dict(os.environ, PYTHONUNBUFFERED="1", LC_ALL="C")
    return subprocess.Popen(
        command, cwd=str(cwd), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, env=environment), command


def wait_processes(procs, timeout: float) -> list[tuple[int, str, str]]:
    results = []
    deadline = time.monotonic() + timeout
    for process in procs:
        remaining = max(0.01, deadline - time.monotonic())
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            raise common.ProbeError(
                f"project session did not finish within {timeout}s")
        results.append((process.returncode, stdout, stderr))
    return results


def terminate_processes(procs) -> None:
    for process in procs:
        if process.poll() is None:
            try:
                process.send_signal(signal.SIGTERM)
            except OSError:
                pass
    deadline = time.monotonic() + 5
    for process in procs:
        while process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.communicate(timeout=2)
        except (subprocess.TimeoutExpired, OSError):
            pass


def new_session_rows(sessions, env_id: str, before: set) -> list[dict]:
    return [sessions.show(row["session_uuid"]) for row in sessions.store.list()
            if row["environment_id"] == env_id
            and row["session_uuid"] not in before]


def running_session_rows(sessions, env_id: str, before: set, count: int):
    rows = new_session_rows(sessions, env_id, before)
    if len(rows) != count:
        return None
    for row in rows:
        if (row.get("state") != "running"
                or row.get("guest_pid") is None
                or row.get("guest_start") is None
                or row.get("guest_state") not in {"alive", None}):
            return None
    return rows


def read_leases(facade, env_id: str):
    from multilinux.on_demand import LaunchGroup
    return LaunchGroup(facade["service"], env_id).read()


def verify_completed_rows(rows: list[dict], expected_count: int,
                          env_id: str, cid: str) -> list[str]:
    problems = []
    if len(rows) != expected_count:
        problems.append(f"expected {expected_count} session records, got {len(rows)}")
    identities = set()
    for row in rows:
        if row["environment_id"] != env_id:
            problems.append(f"session {row['session_uuid']} belongs to another env")
        if row.get("container_id") != cid:
            problems.append(f"session {row['session_uuid']} used another CID")
        if row.get("state") != "exited":
            problems.append(f"session {row['session_uuid']} state={row.get('state')}")
        if row.get("exit_code") != 0:
            problems.append(f"session {row['session_uuid']} exit={row.get('exit_code')}")
        if row.get("lease_token") is not None:
            problems.append(f"session {row['session_uuid']} still records a lease")
        if row.get("guest_state") != "gone":
            problems.append(f"session {row['session_uuid']} guest_state={row.get('guest_state')}")
        if row.get("guest_pid") is None or row.get("guest_start") is None:
            problems.append(f"session {row['session_uuid']} lacks guest identity")
        identities.add((row.get("guest_pid"), row.get("guest_start")))
    if len(identities) != expected_count:
        problems.append(f"guest identities not distinct: {len(identities)}/{expected_count}")
    return problems


def unresolved_for(facade, fixtures: dict, env_id: str) -> list[dict]:
    bad = []
    for row in facade["sessions"].list():
        if row["environment_id"] != env_id:
            continue
        if row.get("effective_state") not in {"exited", "retired"}:
            bad.append({
                "session_uuid": row["session_uuid"],
                "state": row.get("state"),
                "effective_state": row.get("effective_state"),
                "error": row.get("error"),
            })
    return bad


def run_wave(facade: dict, fixtures: dict, target: str, level: int, wave: int,
             *, timeout: float, sampler: ResourceSampler, progress: common.Progress,
             baseline_ids: set) -> dict:
    service = facade["service"]
    sessions = facade["sessions"]
    item = common.fixture(fixtures, target)
    env_id = item["environment"]["id"]
    cid = item["environment"]["runtime"]["container_id"]
    workspace = Path(item["workspace"])
    alias = item["project_alias"]
    before_state = common.ensure_stopped(service, item)
    started_before = before_state.started_at
    ids_before = set(common.container_inventory())
    before_uuids = {row["session_uuid"] for row in sessions.store.list()}
    marker_dir = common.create_marker_dir(
        workspace / ".stagef-sessions", f"wave-{target}-n{level}-{wave}-{time.time_ns()}")
    procs = []

    try:
        for index in range(level):
            proc, command = launch_cli(
                facade["project"], alias,
                guest_command(target, marker_dir, index, 1.0 * index),
                cwd=workspace)
            procs.append(proc)
        ready_deadline = time.monotonic() + 35
        while True:
            if sampler.abort_event.is_set():
                raise common.GuardError(f"resource sampler aborted: {sampler.abort_reason}")
            exited = [index for index, proc in enumerate(procs) if proc.poll() is not None]
            if exited:
                index = exited[0]
                stdout, stderr = procs[index].communicate(timeout=5)
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} exited before all were ready "
                    f"(exit={procs[index].returncode}) stdout={common.tail(stdout)} "
                    f"stderr={common.tail(stderr)}")
            ready = [marker_dir / f"{index}.ready" for index in range(level)]
            if all(path.exists() for path in ready):
                break
            if time.monotonic() > ready_deadline:
                raise common.ProbeError(
                    f"N={level} wave {wave}: readiness timed out")
            time.sleep(0.05)
        ready_at = time.monotonic()
        ready_contents = [(marker_dir / f"{index}.ready").read_text().strip()
                          for index in range(level)]
        expected_id = common.GUEST_IDS[target]
        if any(value != expected_id for value in ready_contents):
            raise common.ProbeError(
                f"N={level} wave {wave}: wrong guest IDs {ready_contents}")
        ready_times = [(marker_dir / f"{index}.ready").stat().st_mtime_ns
                       for index in range(level)]

        rows = None
        session_deadline = time.monotonic() + 15
        while rows is None:
            if sampler.abort_event.is_set():
                raise common.GuardError(f"resource sampler aborted: {sampler.abort_reason}")
            rows = running_session_rows(sessions, env_id, before_uuids, level)
            if rows is None and time.monotonic() > session_deadline:
                raise common.ProbeError(
                    f"N={level} wave {wave}: live session records did not reach N")
            if rows is None:
                time.sleep(0.05)

        during_state = common.state_of(service, item)
        if not during_state["running"]:
            raise common.ProbeError(f"N={level} wave {wave}: container not running")
        if during_state["container_id"] != cid:
            raise common.ProbeError(f"N={level} wave {wave}: container ID changed")
        if during_state["started_at"] == started_before:
            raise common.ProbeError(f"N={level} wave {wave}: no cold start observed")
        lease_deadline = time.monotonic() + 5
        while True:
            leases = read_leases(facade, env_id)
            if len(leases.get("leases") or {}) == level:
                break
            if any(proc.poll() is not None for proc in procs):
                raise common.ProbeError(
                    f"N={level} wave {wave}: a session exited before the go marker")
            if time.monotonic() > lease_deadline:
                raise common.ProbeError(
                    f"N={level} wave {wave}: expected {level} leases, "
                    f"got {len(leases.get('leases') or {})}")
            time.sleep(0.02)
        guest_identities = {(row["guest_pid"], row["guest_start"]) for row in rows}
        if len(guest_identities) != level:
            raise common.ProbeError(
                f"N={level} wave {wave}: guest identities not distinct")
        if any(row.get("container_id") != cid for row in rows):
            raise common.ProbeError(f"N={level} wave {wave}: session CID mismatch")
        live_workers = sum(1 for row in rows if row.get("worker_live"))
        if live_workers != level:
            raise common.ProbeError(
                f"N={level} wave {wave}: live workers {live_workers}/{level}")

        (marker_dir / "go").write_text("GO\n")
        go_at = time.monotonic()
        first_exit = None
        while first_exit is None:
            if sampler.abort_event.is_set():
                raise common.GuardError(f"resource sampler aborted: {sampler.abort_reason}")
            done = [index for index, proc in enumerate(procs) if proc.poll() is not None]
            if done:
                first_exit = done[0]
                break
            if time.monotonic() > go_at + timeout:
                raise common.ProbeError(f"N={level} wave {wave}: no session completed")
            time.sleep(0.05)
        first_exit_elapsed = round(time.monotonic() - go_at, 3)
        early_state = common.state_of(service, item)
        early_leases = len(read_leases(facade, env_id).get("leases") or {})
        if level > 1:
            if not early_state["running"]:
                raise common.ProbeError(
                    f"N={level} wave {wave}: container stopped after the first session")
            if early_leases < 1:
                raise common.ProbeError(
                    f"N={level} wave {wave}: no lease remained after the first session")

        outputs = wait_processes(procs, max(5.0, timeout - (time.monotonic() - go_at)))
        for index, (code, stdout, stderr) in enumerate(outputs):
            if code != 0:
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} exit={code} "
                    f"stdout={common.tail(stdout)} stderr={common.tail(stderr)}")
            result = parse_result(stdout)
            if result is None:
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} has no RESULT line")
            if (result.get("id") != expected_id or result.get("uid") != "1000"
                    or result.get("hash") != common.ZERO_4096_SHA256):
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} result mismatch: {result}")
            output_path = marker_dir / f"{index}.out"
            if not output_path.is_file():
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} output file missing")
            if common.sha256_file(output_path) != common.ZERO_4096_SHA256:
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} output hash mismatch")
            done_path = marker_dir / f"{index}.done"
            if done_path.read_text().strip() != "DONE":
                raise common.ProbeError(
                    f"N={level} wave {wave}: session {index} did not write DONE")

        def stopped_now():
            state = common.state_of(service, item)
            return state if not state["running"] and state["record_state"] == "stopped" else None

        stopped = common.wait_until(
            stopped_now, timeout=20,
            description=f"N={level} wave {wave} auto-stop")
        if stopped["started_at"] != during_state["started_at"]:
            raise common.ProbeError(
                f"N={level} wave {wave}: container restarted during auto-stop")
        ids_after = set(common.container_inventory())
        created = sorted(ids_after - ids_before)
        if created:
            raise common.ProbeError(
                f"N={level} wave {wave}: new containers were created: {created}")
        after_rows = new_session_rows(sessions, env_id, before_uuids)
        problems = verify_completed_rows(after_rows, level, env_id, cid)
        if problems:
            raise common.ProbeError(
                f"N={level} wave {wave}: " + "; ".join(problems))
        unresolved = unresolved_for(facade, fixtures, env_id)
        if unresolved:
            raise common.ProbeError(
                f"N={level} wave {wave}: unresolved sessions {unresolved[:4]}")
        remaining_leases = read_leases(facade, env_id).get("leases") or {}
        if remaining_leases:
            raise common.ProbeError(
                f"N={level} wave {wave}: leases remain after auto-stop")

        result = {
            "target": target, "level": level, "wave": wave,
            "passed": True,
            "container_id": cid,
            "started_at_before": started_before,
            "started_at_during": during_state["started_at"],
            "started_at_after": stopped["started_at"],
            "cold_start_count": 1 if during_state["started_at"] != started_before else 0,
            "container_create_delta": len(created),
            "session_count": len(after_rows),
            "lease_peak": level,
            "guest_identity_count": len(guest_identities),
            "live_worker_peak": live_workers,
            "ready_span_ms": round((max(ready_times) - min(ready_times)) / 1_000_000.0, 3),
            "first_exit_after_ready_seconds": first_exit_elapsed,
            "early_stop_check": {
                "container_running_after_first_exit": bool(early_state["running"]),
                "leases_after_first_exit": early_leases,
            },
            "final_auto_stop": True,
            "final_leases": 0,
            "unresolved_sessions": 0,
            "result_uid": "1000",
            "result_hash": common.ZERO_4096_SHA256,
        }
        common.append_jsonl(progress.run_dir / "waves.jsonl", result)
        progress.update("wave-complete", target=target, level=level, wave=wave,
                        message="N sessions completed and auto-stopped")
        return result
    finally:
        terminate_processes(procs)


def run_mixed_wave(facade: dict, fixtures: dict, wave: int, *, timeout: float,
                   sampler: ResourceSampler, progress: common.Progress) -> dict:
    service = facade["service"]
    sessions = facade["sessions"]
    plan = {target: 2 for target in common.TARGETS}
    before_states = {}
    before_uuids = {}
    marker_dirs = {}
    procs = []
    targets_by_index = []
    try:
        for target in common.TARGETS:
            item = common.fixture(fixtures, target)
            before_states[target] = common.ensure_stopped(service, item)
            before_uuids[target] = {row["session_uuid"] for row in sessions.store.list()}
            workspace = Path(item["workspace"])
            marker_dir = common.create_marker_dir(
                workspace / ".stagef-mixed",
                f"wave-{wave}-{time.time_ns()}")
            marker_dirs[target] = marker_dir
            for local_index in range(2):
                index = len(procs)
                proc, command = launch_cli(
                    facade["project"], item["project_alias"],
                    guest_command(target, marker_dir, local_index, 3.0 * local_index),
                    cwd=workspace)
                procs.append(proc)
                targets_by_index.append((target, local_index))
        ready_deadline = time.monotonic() + 45
        while True:
            if sampler.abort_event.is_set():
                raise common.GuardError(f"resource sampler aborted: {sampler.abort_reason}")
            exited = [index for index, proc in enumerate(procs) if proc.poll() is not None]
            if exited:
                index = exited[0]
                stdout, stderr = procs[index].communicate(timeout=5)
                raise common.ProbeError(
                    f"mixed wave {wave}: session {index} exited early "
                    f"(exit={procs[index].returncode}) stderr={common.tail(stderr)}")
            ready = [marker_dirs[target] / f"{local}.ready"
                     for target, local in targets_by_index]
            if all(path.exists() for path in ready):
                break
            if time.monotonic() > ready_deadline:
                raise common.ProbeError(f"mixed wave {wave}: readiness timed out")
            time.sleep(0.05)
        ready_at = time.monotonic()
        for target, local in targets_by_index:
            content = (marker_dirs[target] / f"{local}.ready").read_text().strip()
            if content != common.GUEST_IDS[target]:
                raise common.ProbeError(
                    f"mixed wave {wave}: {target} guest ID {content!r}")

        rows_by_target = {}
        deadline = time.monotonic() + 15
        while len(rows_by_target) < len(common.TARGETS):
            if sampler.abort_event.is_set():
                raise common.GuardError(f"resource sampler aborted: {sampler.abort_reason}")
            rows_by_target = {}
            for target in common.TARGETS:
                rows = running_session_rows(
                    sessions, common.fixture(fixtures, target)["environment"]["id"],
                    before_uuids[target], plan[target])
                if rows is not None:
                    rows_by_target[target] = rows
            if len(rows_by_target) != len(common.TARGETS):
                if time.monotonic() > deadline:
                    raise common.ProbeError(
                        f"mixed wave {wave}: not all target sessions reached running")
                time.sleep(0.05)
                continue
        for target, rows in rows_by_target.items():
            item = common.fixture(fixtures, target)
            cid = item["environment"]["runtime"]["container_id"]
            state = common.state_of(service, item)
            if not state["running"] or state["container_id"] != cid:
                raise common.ProbeError(f"mixed wave {wave}: {target} container not running")
            if state["started_at"] == before_states[target].started_at:
                raise common.ProbeError(f"mixed wave {wave}: {target} did not cold-start")
            leases = read_leases(facade, item["environment"]["id"]).get("leases") or {}
            if len(leases) != plan[target]:
                raise common.ProbeError(
                    f"mixed wave {wave}: {target} leases {len(leases)} != {plan[target]}")
            if len({(row["guest_pid"], row["guest_start"]) for row in rows}) != plan[target]:
                raise common.ProbeError(
                    f"mixed wave {wave}: {target} guest identities not distinct")
            if any(row.get("container_id") != cid for row in rows):
                raise common.ProbeError(f"mixed wave {wave}: {target} CID mismatch")

        for marker in marker_dirs.values():
            (marker / "go").write_text("GO\n")
        go_at = time.monotonic()
        # One session on each distro finishes first; every other session must
        # still have a live lease and every container must still be running.
        first_done = None
        while first_done is None:
            if sampler.abort_event.is_set():
                raise common.GuardError(f"resource sampler aborted: {sampler.abort_reason}")
            done = [index for index, proc in enumerate(procs) if proc.poll() is not None]
            if done:
                first_done = done[0]
                break
            if time.monotonic() > go_at + timeout:
                raise common.ProbeError(f"mixed wave {wave}: no session completed")
            time.sleep(0.05)
        early = {}
        for target in common.TARGETS:
            item = common.fixture(fixtures, target)
            state = common.state_of(service, item)
            leases = len(read_leases(facade, item["environment"]["id"]).get("leases") or {})
            early[target] = {"running": bool(state["running"]), "leases": leases}
            if not state["running"] or leases < 1:
                raise common.ProbeError(
                    f"mixed wave {wave}: {target} stopped early: {early[target]}")

        outputs = wait_processes(procs, timeout)
        for index, (code, stdout, stderr) in enumerate(outputs):
            target, local = targets_by_index[index]
            if code != 0:
                raise common.ProbeError(
                    f"mixed wave {wave}: {target}[{local}] exit={code} "
                    f"stderr={common.tail(stderr)}")
            result = parse_result(stdout)
            if (result is None or result.get("id") != common.GUEST_IDS[target]
                    or result.get("uid") != "1000"
                    or result.get("hash") != common.ZERO_4096_SHA256):
                raise common.ProbeError(
                    f"mixed wave {wave}: {target}[{local}] result mismatch: {result}")

        for target in common.TARGETS:
            item = common.fixture(fixtures, target)
            env_id = item["environment"]["id"]
            cid = item["environment"]["runtime"]["container_id"]

            def stopped_now():
                state = common.state_of(service, item)
                return state if not state["running"] and state["record_state"] == "stopped" else None

            common.wait_until(stopped_now, timeout=20,
                              description=f"mixed {target} auto-stop")
            rows = new_session_rows(sessions, env_id, before_uuids[target])
            problems = verify_completed_rows(rows, plan[target], env_id, cid)
            if problems:
                raise common.ProbeError(
                    f"mixed wave {wave}: {target}: " + "; ".join(problems))
            if unresolved_for(facade, fixtures, env_id):
                raise common.ProbeError(f"mixed wave {wave}: {target} unresolved sessions")
            if read_leases(facade, env_id).get("leases"):
                raise common.ProbeError(f"mixed wave {wave}: {target} leases remain")
        result = {
            "target": "mixed", "wave": wave, "passed": True,
            "per_target": plan, "early_stop_check": early,
            "container_ids": {target: common.fixture(fixtures, target)["environment"]["runtime"]["container_id"]
                              for target in common.TARGETS},
            "final_auto_stop_all": True, "unresolved_sessions": 0,
        }
        common.append_jsonl(progress.run_dir / "mixed-waves.jsonl", result)
        progress.update("mixed-wave-complete", wave=wave,
                        message="two sessions per distro completed")
        return result
    finally:
        terminate_processes(procs)


def run_cli_command(facade: dict, target: str, script: str, workspace: Path,
                    timeout: float = 60) -> dict:
    item = common.fixture(facade["fixtures"], target)
    command = [sys.executable, str(facade["project"] / "tools" / "unifier.py"),
               "project", "run", item["project_alias"], "--", "/bin/sh", "-c", script]
    started = time.monotonic_ns()
    result = subprocess.run(
        command, cwd=str(workspace), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=timeout,
        env=dict(os.environ, PYTHONUNBUFFERED="1", LC_ALL="C"))
    elapsed = time.monotonic_ns() - started
    return {
        "exit": result.returncode, "wall_ms": round(elapsed / 1_000_000.0, 3),
        "stdout": result.stdout, "stderr": result.stderr,
        "command": command,
    }


def run_timings(facade: dict, fixtures: dict, *, reps: int,
                sampler: ResourceSampler, progress: common.Progress) -> dict:
    facade = dict(facade, fixtures=fixtures)
    service = facade["service"]
    results = {}
    for target in common.TARGETS:
        item = common.fixture(fixtures, target)
        env_id = item["environment"]["id"]
        workspace = Path(item["workspace"])
        target_row = {"cold": [], "warm": [], "prepare": [], "cli_plan": []}

        # Cold: a stopped environment is started by the session and auto-stopped.
        for index in range(reps):
            common.ensure_stopped(service, item)
            marker = workspace / f".stagef-timing-cold-{target}-{index}-{time.time_ns()}.out"
            script = f""". /etc/os-release
dd if=/dev/zero of={common.sh_quote(marker)} bs=4096 count=1 status=none
H=$(sha256sum {common.sh_quote(marker)} | cut -d' ' -f1)
U=$(id -u)
printf 'RESULT id=%s uid=%s hash=%s\\n' "$ID" "$U" "$H"
"""
            sampler.pause()
            try:
                result = run_cli_command(facade, target, script, workspace)
            finally:
                sampler.resume()
            if result["exit"] != 0:
                raise common.ProbeError(
                    f"cold timing {target} exit={result['exit']} "
                    f"stderr={common.tail(result['stderr'])}")
            parsed = parse_result(result["stdout"])
            if (parsed is None or parsed.get("id") != common.GUEST_IDS[target]
                    or parsed.get("uid") != "1000"
                    or parsed.get("hash") != common.ZERO_4096_SHA256):
                raise common.ProbeError(f"cold timing {target} result mismatch: {parsed}")
            if common.sha256_file(marker) != common.ZERO_4096_SHA256:
                raise common.ProbeError(f"cold timing {target} output hash mismatch")
            common.wait_until(
                lambda: (lambda state: state if not state["running"] else None)(
                    common.state_of(service, item)), timeout=20,
                description=f"cold timing {target} auto-stop")
            target_row["cold"].append({
                "wall_ms": result["wall_ms"], "exit": result["exit"],
                "sampling_paused": True,
            })
            progress.update("timing-cold", target=target,
                            message=f"cold {index + 1}/{reps}")

        # Warm: one manual start is preserved; the session must not auto-stop it.
        service.start(env_id)
        warm_state = common.state_of(service, item)
        if not warm_state["running"]:
            raise common.ProbeError(f"warm timing {target}: manual start failed")
        try:
            for index in range(reps):
                marker = workspace / f".stagef-timing-warm-{target}-{index}-{time.time_ns()}.out"
                script = f""". /etc/os-release
dd if=/dev/zero of={common.sh_quote(marker)} bs=4096 count=1 status=none
H=$(sha256sum {common.sh_quote(marker)} | cut -d' ' -f1)
U=$(id -u)
printf 'RESULT id=%s uid=%s hash=%s\\n' "$ID" "$U" "$H"
"""
                sampler.pause()
                try:
                    result = run_cli_command(facade, target, script, workspace)
                finally:
                    sampler.resume()
                if result["exit"] != 0:
                    raise common.ProbeError(
                        f"warm timing {target} exit={result['exit']} "
                        f"stderr={common.tail(result['stderr'])}")
                parsed = parse_result(result["stdout"])
                if (parsed is None or parsed.get("id") != common.GUEST_IDS[target]
                        or parsed.get("uid") != "1000"
                        or parsed.get("hash") != common.ZERO_4096_SHA256):
                    raise common.ProbeError(f"warm timing {target} result mismatch: {parsed}")
                after = common.state_of(service, item)
                if (not after["running"]
                        or after["started_at"] != warm_state["started_at"]):
                    raise common.ProbeError(
                        f"warm timing {target}: manual start was not preserved")
                target_row["warm"].append({
                    "wall_ms": result["wall_ms"], "exit": result["exit"],
                    "sampling_paused": True,
                })
                progress.update("timing-warm", target=target,
                                message=f"warm {index + 1}/{reps}")
        finally:
            service.stop(env_id)

        # Prepare/authorization timing: product plan path only, no start.
        common.ensure_stopped(service, item)
        sampler.pause()
        try:
            for _index in range(20):
                started = time.monotonic_ns()
                prepared = facade["sessions"].prepare(
                    item["project_alias"], cwd=str(workspace),
                    argv=["/bin/sh", "-c", "true"], mode="run")
                elapsed = time.monotonic_ns() - started
                if (prepared.get("container_id") != item["environment"]["runtime"]["container_id"]
                        or prepared.get("environment_uuid") != item["environment_uuid"]):
                    raise common.ProbeError(
                        f"prepare timing {target}: binding changed")
                target_row["prepare"].append(round(elapsed / 1_000_000.0, 3))
        finally:
            sampler.resume()

        # Public CLI plan timing; it must not start the stopped fixture.
        before = common.state_of(service, item)
        sampler.pause()
        try:
            for _index in range(5):
                started = time.monotonic_ns()
                process = subprocess.run(
                    [sys.executable, str(facade["project"] / "tools" / "unifier.py"),
                     "project", "plan", item["project_alias"]],
                    cwd=str(workspace), capture_output=True, text=True, timeout=60,
                    env=dict(os.environ, PYTHONUNBUFFERED="1", LC_ALL="C"))
                elapsed = time.monotonic_ns() - started
                if process.returncode != 0:
                    raise common.ProbeError(
                        f"cli plan {target}: {common.tail(process.stderr)}")
                payload = json.loads(process.stdout)
                if payload.get("container_id") != item["environment"]["runtime"]["container_id"]:
                    raise common.ProbeError(f"cli plan {target}: binding changed")
                target_row["cli_plan"].append(round(elapsed / 1_000_000.0, 3))
        finally:
            sampler.resume()
        after = common.state_of(service, item)
        if before["started_at"] != after["started_at"] or after["running"]:
            raise common.ProbeError(f"prepare/CLI plan {target} started the fixture")
        target_row["prepare_seconds"] = common.stats(
            value / 1000.0 for value in target_row["prepare"])
        target_row["cli_plan_seconds"] = common.stats(
            value / 1000.0 for value in target_row["cli_plan"])
        target_row["cold_seconds"] = common.stats(
            row["wall_ms"] / 1000.0 for row in target_row["cold"])
        target_row["warm_seconds"] = common.stats(
            row["wall_ms"] / 1000.0 for row in target_row["warm"])
        results[target] = target_row
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Stage F real project-session concurrency (Fedora N=1/2/4/8), "
                    "three-distro mixed waves, warm/cold end-to-end timing and "
                    "prepare/authorization timing.")
    parser.add_argument("--fixtures", type=Path, default=common.FIXTURES_DEFAULT)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--levels", default="1,2,4,8")
    parser.add_argument("--waves", type=int, default=3)
    parser.add_argument("--mixed-waves", type=int, default=2)
    parser.add_argument("--timing-reps", type=int, default=5)
    parser.add_argument("--timeout", type=float, default=90.0)
    args = parser.parse_args(argv)
    levels = [int(item) for item in args.levels.split(",") if item.strip()]
    if levels != [1, 2, 4, 8]:
        raise common.ProbeError("Stage F mandatory Fedora levels are exactly 1,2,4,8")
    run_dir = common.create_run_dir(args.run_dir)
    progress = common.Progress(run_dir, "probe_sessions", levels=levels,
                               waves=args.waves, mixed_waves=args.mixed_waves)
    sampler = None
    try:
        common.guard_memory(label="sessions preflight")
        fixtures = common.load_fixtures(args.fixtures)
        facade = common.product(fixtures)
        common.check_fixture_state(facade["service"], fixtures, require_stopped=True)
        baseline_ids = set(common.container_inventory())
        sampler = ResourceSampler(fixtures, facade, run_dir=run_dir, interval=1.0)
        sampler.start()
        waves = []
        for level in levels:
            for wave in range(args.waves):
                progress.update("wave", level=level, wave=wave,
                                message=f"Fedora N={level} wave {wave + 1}")
                waves.append(run_wave(
                    facade, fixtures, "fedora44", level, wave,
                    timeout=args.timeout, sampler=sampler, progress=progress,
                    baseline_ids=baseline_ids))
        mixed = []
        for wave in range(args.mixed_waves):
            progress.update("mixed-wave", wave=wave,
                            message=f"three-distro mixed wave {wave + 1}")
            mixed.append(run_mixed_wave(
                facade, fixtures, wave, timeout=args.timeout,
                sampler=sampler, progress=progress))
        timings = run_timings(
            facade, fixtures, reps=args.timing_reps, sampler=sampler,
            progress=progress)
        sampler.stop("sessions probe complete")
        sampler.join(timeout=20)
        resource_summary = common.read_json(run_dir / "resources-summary.json")

        checks = {
            "fedora_levels_1_2_4_8": sorted({item["level"] for item in waves}) == levels,
            "three_waves_each": all(
                sum(1 for item in waves if item["level"] == level) == args.waves
                for level in levels),
            "same_cid_each_wave": all(item["container_id"] for item in waves),
            "container_create_delta_zero": all(
                item["container_create_delta"] == 0 for item in waves),
            "cold_start_one_each_wave": all(
                item["cold_start_count"] == 1 for item in waves),
            "lease_and_guest_peak_match": all(
                item["lease_peak"] == item["level"]
                and item["guest_identity_count"] == item["level"]
                for item in waves),
            "no_early_stop": all(
                item["level"] == 1 or (
                    item["early_stop_check"]["container_running_after_first_exit"]
                    and item["early_stop_check"]["leases_after_first_exit"] >= 1)
                for item in waves),
            "final_auto_stop": all(item["final_auto_stop"] for item in waves),
            "no_unresolved_sessions": all(
                item["unresolved_sessions"] == 0 for item in waves + mixed),
            "mixed_two_per_distro": all(
                item["per_target"] == {target: 2 for target in common.TARGETS}
                for item in mixed),
            "mixed_no_early_stop": all(
                all(value["running"] and value["leases"] >= 1
                    for value in item["early_stop_check"].values())
                for item in mixed),
            "timings_exit_zero": all(
                row["exit"] == 0 for target in timings.values()
                for kind in ("cold", "warm") for row in target[kind]),
            "prepare_never_started": common.check_fixture_state(
                facade["service"], fixtures, require_stopped=True) is not None,
            "no_new_containers": set(common.container_inventory()) == baseline_ids,
            "resource_sampler_not_aborted": not sampler.abort_event.is_set(),
        }
        for name, passed in checks.items():
            progress.check(name, passed)
        status = "passed" if all(checks.values()) else "failed"
        summary = {
            "schema_version": 1,
            "status": status,
            "acceptance": "pending-primary-review",
            "scope": (
                "Local rootless Podman, frozen fixture source, keep-id, same-path "
                "shared bind. Real public CLI project sessions. No GUI first-frame "
                "claim and no cross-host claim."),
            "checks": checks,
            "waves": waves,
            "mixed": mixed,
            "timings": timings,
            "resource_summary": {
                "sample_count": resource_summary.get("sample_count"),
                "coverage": resource_summary.get("coverage"),
                "abort": resource_summary.get("abort"),
                "path": str(run_dir / "resources-summary.json"),
            },
            "workload": {
                "interpreter": "/bin/sh",
                "tools": ["dd", "sha256sum", "id", "cut", "sleep"],
                "bytes": 4096,
                "expected_hash": common.ZERO_4096_SHA256,
                "ready_marker": True,
                "overlap_sleep_seconds": 2,
                "per_index_extra_sleep_seconds": 1.0,
                "ready_barrier": "release only after all live guest identities and N leases observed",
                "note": "Command completion is not GUI first frame.",
            },
            "started_at": progress.started_at,
            "finished_at": common.now_iso(),
        }
        if status != "passed":
            progress.failure(common.ProbeError("one or more session checks failed"))
        common.write_summary(run_dir, summary)
        progress.finish(status=status, checks=checks)
        print(json.dumps({
            "status": status, "run_dir": str(run_dir),
            "waves": len(waves), "mixed": len(mixed),
            "checks": checks,
        }, ensure_ascii=False))
        return 0 if status == "passed" else 1
    except BaseException as exc:
        progress.failure(exc)
        common.write_summary(run_dir, {
            "schema_version": 1, "status": "failed",
            "acceptance": "pending-primary-review",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-12000:],
            "started_at": progress.started_at, "finished_at": common.now_iso(),
        })
        raise
    finally:
        if sampler is not None:
            sampler.stop("probe exiting")
            sampler.join(timeout=20)


if __name__ == "__main__":
    raise SystemExit(main())
