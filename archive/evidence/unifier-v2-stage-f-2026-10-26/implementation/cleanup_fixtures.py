#!/usr/bin/env python3
"""Inspect or explicitly clean only the Stage F fixture environments.

``inspect`` is read-only.  ``cleanup`` first preflights every fixture and
refuses to touch anything when an unresolved session, live lease or failed
view remains.  It then revokes only registered views of these exact
environments and uses the product ``EnvironmentService.remove`` lifecycle.
It never runs ``podman system prune`` and never touches a container whose ID
is not in the fixture manifest.

The default tool never performs cleanup automatically after a failed run.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def collect_inventory(facade, fixtures: dict) -> dict:
    service = facade["service"]
    sessions = facade["sessions"]
    views = facade["views"]
    rows = {"targets": {}, "views": [], "sessions": [], "leases": {},
            "volumes": [], "mounts": []}
    current = common.container_inventory()
    own = set()
    for target in common.TARGETS:
        item = common.fixture(fixtures, target)
        env_id = item["environment"]["id"]
        cid = item["environment"]["runtime"]["container_id"]
        own.add(cid)
        record = service.store.load(env_id)
        inspect = common.podman_inspect(record["runtime"]["container_name"])
        view_rows = [row for row in views.registry.list()
                     if row["environment_id"] == env_id]
        session_rows = [row for row in sessions.list()
                        if row["environment_id"] == env_id]
        try:
            from multilinux.on_demand import LaunchGroup
            leases = LaunchGroup(service, env_id).read().get("leases") or {}
        except Exception as exc:
            leases = {"error": f"{type(exc).__name__}: {exc}"}
        volumes = sorted({
            mount.get("Name") for mount in (inspect.get("Mounts") or [])
            if mount.get("Type") == "volume" and mount.get("Name")})
        rows["targets"][target] = {
            "environment_id": env_id,
            "expected_container_id": cid,
            "record_container_id": (record.get("runtime") or {}).get("container_id"),
            "container_id_matches": (
                (record.get("runtime") or {}).get("container_id") == cid),
            "runtime_state": record.get("runtime_state"),
            "container_running": bool((inspect.get("State") or {}).get("Running")),
            "container_status": (inspect.get("State") or {}).get("Status"),
            "views": [{
                "view_uuid": row["view_uuid"], "state": row.get("state"),
                "mountpoint": row.get("mountpoint"),
                "worker_pid": row.get("worker_pid"),
                "worker_stamp": row.get("worker_stamp"),
                "broker_pid": row.get("broker_pid"),
                "broker_stamp": row.get("broker_stamp"),
                "launcher_pid": row.get("broker_launcher_pid"),
                "launcher_stamp": row.get("broker_launcher_stamp"),
            } for row in view_rows],
            "sessions": [{
                "session_uuid": row["session_uuid"],
                "state": row.get("state"),
                "effective_state": row.get("effective_state"),
                "lease_token": row.get("lease_token"),
                "worker_live": row.get("worker_live"),
                "guest_state": row.get("guest_state"),
                "guest_live": row.get("guest_live"),
            } for row in session_rows],
            "leases": leases,
            "volumes": volumes,
        }
        rows["views"].extend(view_rows)
        rows["sessions"].extend(session_rows)
        rows["leases"][env_id] = leases
        rows["volumes"].extend(volumes)
    rows["own_container_ids"] = sorted(own)
    rows["current_container_ids"] = sorted(current)
    rows["old_container_ids"] = sorted(set(current) - own)
    mounts = []
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            if len(fields) > 4 and any(view["mountpoint"] in fields[4]
                                       for view in rows["views"]):
                mounts.append(fields[4])
    except OSError:
        pass
    rows["mounts"] = mounts
    return rows


def preflight(facade, fixtures: dict, inventory: dict) -> list[str]:
    problems = []
    for target, row in inventory["targets"].items():
        if not row["container_id_matches"]:
            problems.append(f"{target}: record container ID changed")
        for session in row["sessions"]:
            if session["effective_state"] not in {"exited", "retired"}:
                problems.append(
                    f"{target}: session {session['session_uuid']} is "
                    f"{session['effective_state']} (not terminal)")
            if session["worker_live"]:
                problems.append(
                    f"{target}: session {session['session_uuid']} has a live worker")
        leases = row["leases"]
        if isinstance(leases, dict) and leases.get("error"):
            problems.append(f"{target}: lease state unreadable: {leases['error']}")
        elif leases:
            problems.append(f"{target}: {len(leases)} live lease(s) remain")
    return problems


def cleanup(args) -> int:
    run_dir = common.create_run_dir(args.run_dir)
    progress = common.Progress(run_dir, "cleanup_fixtures", mode="cleanup")
    try:
        common.guard_memory(label="cleanup preflight")
        fixtures = common.load_fixtures(args.fixtures)
        facade = common.product(fixtures)
        service = facade["service"]
        views = facade["views"]
        before = set(common.container_inventory())
        inventory = collect_inventory(facade, fixtures)
        problems = preflight(facade, fixtures, inventory)
        if problems:
            detail = {
                "schema_version": 1, "status": "refused",
                "acceptance": "pending-primary-review",
                "reason": "unresolved state or live lease; refusing destructive cleanup",
                "problems": problems, "inventory": inventory,
                "started_at": progress.started_at, "finished_at": common.now_iso(),
            }
            common.write_summary(run_dir, detail)
            progress.failure(common.ProbeError(detail["reason"] + ": " + "; ".join(problems)))
            print(json.dumps(detail, ensure_ascii=False))
            return 1

        removed = []
        for target in common.TARGETS:
            item = common.fixture(fixtures, target)
            env_id = item["environment"]["id"]
            cid = item["environment"]["runtime"]["container_id"]
            progress.update("cleanup-target", target=target)
            for view in [row for row in views.registry.list()
                         if row["environment_id"] == env_id]:
                views.revoke(view["view_uuid"])
            remaining_views = [row for row in views.registry.list()
                               if row["environment_id"] == env_id]
            if remaining_views:
                raise common.ProbeError(
                    f"{target}: views remain after revoke: "
                    f"{[row['view_uuid'] for row in remaining_views]}")
            record = service.store.load(env_id)
            inspect = common.podman_inspect(record["runtime"]["container_name"])
            if inspect.get("Id") != cid:
                raise common.ProbeError(
                    f"{target}: refusing to remove a different container instance")
            if (inspect.get("State") or {}).get("Running"):
                raise common.ProbeError(
                    f"{target}: container is still running; refusing blind cleanup")
            volumes = sorted({
                mount.get("Name") for mount in (inspect.get("Mounts") or [])
                if mount.get("Type") == "volume" and mount.get("Name")})
            service.remove(env_id)
            removed.append({"target": target, "environment_id": env_id,
                            "container_id": cid, "volumes": volumes})
            if service.store.exists(env_id):
                raise common.ProbeError(f"{target}: environment record still exists")
            exists = common.run_command(["podman", "inspect", cid], timeout=30)
            if exists["exit"] == 0:
                raise common.ProbeError(f"{target}: container {cid} still exists")

        # Remove only exact named volumes recorded in the manifest.  Stage F
        # prepare creates none; this path exists for a future fixture and
        # never calls a global prune.
        removed_volumes = []
        for name in fixtures.get("volumes") or []:
            if not isinstance(name, str) or not name.startswith(
                    ("stagef-", "unifier-c-")):
                raise common.ProbeError(f"refusing unexpected volume name: {name!r}")
            result = common.run_command(["podman", "volume", "rm", name], timeout=60)
            if result["exit"] != 0:
                raise common.ProbeError(
                    f"exact volume {name} could not be removed: "
                    f"{result.get('stderr') or result.get('error')}")
            removed_volumes.append(name)

        after = set(common.container_inventory())
        expected_remaining = before - {row["container_id"] for row in removed}
        if after != expected_remaining:
            raise common.ProbeError(
                f"container inventory changed unexpectedly: before={sorted(before)} "
                f"after={sorted(after)} removed={removed}")
        leftovers = []
        for target in common.TARGETS:
            item = common.fixture(fixtures, target)
            env_id = item["environment"]["id"]
            if service.store.exists(env_id):
                leftovers.append(f"{target}: environment record")
            if [row for row in views.registry.list()
                    if row["environment_id"] == env_id]:
                leftovers.append(f"{target}: view record")
            try:
                from multilinux.on_demand import LaunchGroup
                if LaunchGroup(service, env_id).read().get("leases"):
                    leftovers.append(f"{target}: lease")
            except Exception:
                pass
        # Prove the captured view processes/mounts are gone as well as the
        # environment records.  This is read-only evidence after removal.
        for view in inventory["views"]:
            if common.mountinfo_contains(view.get("mountpoint", "")):
                leftovers.append(f"view {view['view_uuid']}: mount still present")
            for key, stamp_key in (("worker_pid", "worker_stamp"),
                                   ("broker_pid", "broker_stamp"),
                                   ("broker_launcher_pid", "broker_launcher_stamp")):
                if common.pid_alive(view.get(key), view.get(stamp_key)):
                    leftovers.append(
                        f"view {view['view_uuid']}: {key} still alive")
            journal = Path(view.get("storage_journal") or "")
            if journal.is_file():
                try:
                    journal_state = json.loads(journal.read_text()).get("state")
                except (OSError, json.JSONDecodeError):
                    journal_state = "UNREADABLE"
                if journal_state in {"mounted", "mounting", "releasing",
                                     "release-failed", "needs-reconciliation"}:
                    leftovers.append(
                        f"view {view['view_uuid']}: storage journal {journal_state}")
        for pid in common.proc_table():
            command = common.cmdline_of(pid)
            if any(view["view_uuid"] in command or
                   (view.get("mountpoint") and view["mountpoint"] in command)
                   for view in inventory["views"]):
                leftovers.append(f"process {pid} still references a fixture view")
        if leftovers:
            raise common.ProbeError("cleanup leftovers: " + "; ".join(leftovers))
        summary = {
            "schema_version": 1,
            "status": "passed",
            "acceptance": "pending-primary-review",
            "mode": "cleanup",
            "removed": removed,
            "removed_volumes": removed_volumes,
            "old_containers_untouched": sorted(before - {row["container_id"] for row in removed}),
            "remaining_container_ids": sorted(after),
            "leftovers": leftovers,
            "started_at": progress.started_at,
            "finished_at": common.now_iso(),
        }
        common.write_summary(run_dir, summary)
        progress.finish(status="passed", removed=len(removed))
        print(json.dumps(summary, ensure_ascii=False))
        return 0
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


def inspect(args) -> int:
    run_dir = common.create_run_dir(args.run_dir)
    progress = common.Progress(run_dir, "cleanup_fixtures", mode="inspect")
    try:
        fixtures = common.load_fixtures(args.fixtures)
        facade = common.product(fixtures)
        inventory = collect_inventory(facade, fixtures)
        problems = preflight(facade, fixtures, inventory)
        summary = {
            "schema_version": 1,
            "status": "observations-complete",
            "acceptance": "pending-primary-review",
            "mode": "inspect",
            "inventory": inventory,
            "cleanup_problems": problems,
            "started_at": progress.started_at,
            "finished_at": common.now_iso(),
        }
        common.write_summary(run_dir, summary)
        progress.finish(status="observations-complete")
        print(json.dumps(summary, ensure_ascii=False))
        return 0
    except BaseException as exc:
        progress.failure(exc)
        common.write_summary(run_dir, {
            "schema_version": 1, "status": "failed",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-12000:],
            "started_at": progress.started_at, "finished_at": common.now_iso(),
        })
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect or explicitly clean only the Stage F fixtures. "
                    "Cleanup refuses unresolved sessions/live leases and never "
                    "runs podman system prune.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("inspect", "cleanup"):
        item = sub.add_parser(name)
        item.add_argument("--fixtures", type=Path, default=common.FIXTURES_DEFAULT)
        item.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    return inspect(args) if args.command == "inspect" else cleanup(args)


if __name__ == "__main__":
    raise SystemExit(main())
