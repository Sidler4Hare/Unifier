#!/usr/bin/env python3
"""Prepare the isolated Stage F local fixtures (primary owns execution).

This script is the only Stage F tool that creates or starts containers.  It:

* snapshots the current live ``src``, ``tools`` and ``config`` into a private
  fixture project, then imports the product from that snapshot;
* creates exactly one stopped keep-id environment for each locked Debian 13,
  Fedora 44 and Arch target;
* gives each environment one same-path authorized shared directory containing
  2,000 x 4 KiB files, one 16 MiB sequential file, relative/absolute symlinks
  and atomic-write test files (well below the 128 MiB per-fixture limit);
* uses the existing ``desktop=True`` / ``label=disable`` session convention so
  the host is not relabelled;
* records exact container IDs, environment UUIDs, project/root UUIDs, image
  digests and source/tool/config hashes in a manifest.

It does NOT run any performance or stress workload.  It starts a fixture only
long enough to capture guest capabilities and the guest ``/etc/os-release``
bytes, then stops it.  Every fixture is stopped in the final manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


def build_dataset(workspace: Path, target: str) -> dict:
    """Create the deterministic, bounded per-fixture data set."""
    workspace.mkdir(mode=0o700)
    dataset = workspace / "dataset"
    small = dataset / "small"
    soak = dataset / "soak"
    for directory in (dataset, small, soak):
        directory.mkdir(mode=0o700)

    small_digest = hashlib.sha256()
    sample_files = {}
    total = 0
    for index in range(common.SMALL_FILE_COUNT):
        payload = common.small_file_bytes(target, index)
        if len(payload) != common.SMALL_FILE_BYTES:
            raise common.ProbeError("internal small-file size mismatch")
        path = small / f"f{index:04d}"
        with open(path, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        small_digest.update(payload)
        if index in {0, common.SMALL_FILE_COUNT - 1, common.SMALL_FILE_COUNT // 2}:
            sample_files[path.name] = hashlib.sha256(payload).hexdigest()
        total += len(payload)

    sequential_digest = hashlib.sha256()
    sequential_path = dataset / "sequential.bin"
    with open(sequential_path, "xb") as stream:
        for chunk_index in range(common.SEQUENTIAL_BYTES // (1024 * 1024)):
            block = common.sequential_chunk(target, chunk_index)
            stream.write(block)
            sequential_digest.update(block)
        stream.flush()
        os.fsync(stream.fileno())
    total += common.SEQUENTIAL_BYTES

    files = {
        "mutable.txt": b"stage-f-mutable-v0\n",
        "stale.txt": b"stage-f-stale-v0\n",
        "absolute-os-release": None,
        "relative-link": None,
        "sequential-link": None,
        "soak/current.bin": b"stage-f-soak-v0\n",
        "soak/static.txt": b"stage-f-soak-static\n",
    }
    for name, payload in files.items():
        path = dataset / name
        if payload is None:
            continue
        with open(path, "xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        total += len(payload)

    # Absolute links deliberately point at the container-root path.  The
    # product view must rewrite them to its own mountpoint; a host readlink
    # is not evidence of the guest semantics.
    (dataset / "absolute-os-release").symlink_to("/etc/os-release")
    (dataset / "relative-link").symlink_to("small/f0000")
    (dataset / "sequential-link").symlink_to("sequential.bin")

    workload = dataset / "workload.sh"
    shutil.copy2(HERE / "workload.sh", workload)
    workload.chmod(0o755)

    manifest = {
        "target": target,
        "small_file_count": common.SMALL_FILE_COUNT,
        "small_file_bytes": common.SMALL_FILE_BYTES,
        "small_bytes": common.SMALL_FILE_COUNT * common.SMALL_FILE_BYTES,
        "small_sha256": small_digest.hexdigest(),
        "sequential_file_bytes": common.SEQUENTIAL_BYTES,
        "sequential_sha256": sequential_digest.hexdigest(),
        "sample_files": sample_files,
        "total_fixture_bytes": total,
        "limit_bytes": common.FIXTURE_LIMIT_BYTES,
        "zero_4096_sha256": common.ZERO_4096_SHA256,
        "workload_sha256": common.sha256_file(workload),
    }
    if total > common.FIXTURE_LIMIT_BYTES:
        raise common.ProbeError(
            f"fixture {target} is {total} bytes, over the {common.FIXTURE_LIMIT_BYTES} limit")
    with open(dataset / "manifest.json", "x", encoding="utf-8") as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    common.fsync_directory(dataset)
    return manifest


def copy_snapshot(project: Path) -> dict:
    shutil.copytree(
        common.ROOT / "src", project / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(
        common.ROOT / "tools", project / "tools",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(
        common.ROOT / "config", project / "config",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return {
        "source_sha256": common.hash_tree(project / "src"),
        "tools_sha256": common.hash_tree(project / "tools"),
        "config_sha256": common.hash_tree(project / "config"),
    }


def git_identity() -> dict:
    result = {"head": None, "branch": None, "error": None}
    try:
        result["head"] = subprocess.check_output(
            ["git", "-C", str(common.ROOT), "rev-parse", "HEAD"],
            text=True, timeout=15).strip()
        result["branch"] = subprocess.check_output(
            ["git", "-C", str(common.ROOT), "branch", "--show-current"],
            text=True, timeout=15).strip()
    except (OSError, subprocess.SubprocessError) as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def guest_capture(service, target_item: dict) -> dict:
    """Fixture-only guest capture; no workload is timed or stressed here."""
    env_id = target_item["environment"]["id"]
    cid = target_item["environment"]["runtime"]["container_id"]
    service.start(env_id)
    try:
        info = common.podman_inspect(cid)
        if not info.get("State", {}).get("Running"):
            raise common.ProbeError(f"fixture {env_id} did not start")
        os_release = subprocess.run(
            ["podman", "exec", "--user", common.SESSION_USER, cid,
             "/bin/cat", "/etc/os-release"],
            capture_output=True, timeout=30, check=False)
        if os_release.returncode != 0:
            raise common.ProbeError(
                f"cannot read guest /etc/os-release: {os_release.stderr[-2000:]!r}")
        capabilities = subprocess.run(
            ["podman", "exec", "--user", common.SESSION_USER, cid,
             "/bin/sh", "-c",
             'set -eu; . /etc/os-release; printf "ID=%s\\n" "$ID"; '
             'for x in sh dd sha256sum stat cat date wc awk cut id find; do '
             'printf "%s=" "$x"; command -v "$x" || true; done'],
            capture_output=True, text=True, timeout=30, check=False)
        if capabilities.returncode != 0:
            raise common.ProbeError(
                f"guest capability check failed: {capabilities.stderr[-2000:]}")
        workspace = Path(target_item["workspace"])
        check = subprocess.run(
            ["podman", "exec", "--user", common.SESSION_USER, cid,
             "/bin/sh", "-c",
             'set -eu; test -r "$1/dataset/small/f0000"; '
             'test -r "$1/dataset/sequential.bin"; '
             'sha256sum "$1/dataset/sequential.bin"',
             "guest", str(workspace)],
            capture_output=True, text=True, timeout=30, check=False)
        if check.returncode != 0:
            raise common.ProbeError(
                f"guest cannot read the shared fixture: {check.stderr[-2000:]}")
        return {
            "os_release_bytes": os_release.stdout,
            "os_release_sha256": hashlib.sha256(os_release.stdout).hexdigest(),
            "capabilities": capabilities.stdout,
            "workspace_check": check.stdout.strip(),
            "container": {
                "id": info["Id"],
                "started_at": info["State"].get("StartedAt"),
            },
        }
    finally:
        service.stop(env_id)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the isolated Stage F three-distro local fixtures. "
                    "Creates only new, explicitly named stopped environments; "
                    "does not run any performance or stress workload.")
    parser.add_argument("--output", type=Path, default=common.FIXTURES_DEFAULT,
                        help="new manifest path (default: fixtures.json in the evidence root)")
    parser.add_argument("--project-dir", type=Path, default=common.PROJECT_DEFAULT,
                        help="new private fixture project directory")
    parser.add_argument("--workspaces", type=Path, default=common.WORKSPACES_DEFAULT,
                        help="new directory for the three same-path shared workspaces")
    parser.add_argument("--targets", default=",".join(common.TARGETS),
                        help="comma-separated targets (default: all three)")
    args = parser.parse_args()

    targets = [item.strip() for item in args.targets.split(",") if item.strip()]
    if targets != list(common.TARGETS):
        raise common.ProbeError(
            "Stage F prepare requires exactly debian13,fedora44,arch in that order")
    for path in (args.output, args.project_dir, args.workspaces):
        if path.exists():
            raise common.ProbeError(f"refusing to replace existing fixture path: {path}")
    common.guard_memory(label="prepare preflight")

    args.project_dir.mkdir(mode=0o700)
    args.workspaces.mkdir(mode=0o700)
    progress_path = args.project_dir / "prepare-progress.json"
    containers_before = common.container_inventory()
    report = {
        "schema_version": 1,
        "prepared": False,
        "status": "pending-primary-run",
        "scope": (
            "Local Stage F fixtures only: rootless Podman, rootless keep-id, "
            "existing desktop=True label=disable convention, same-path authorized "
            "shared directory. No cross-host claim."
        ),
        "started_at": common.now_iso(),
        "project": str(args.project_dir.resolve()),
        "workspaces": str(args.workspaces.resolve()),
        "targets": {},
        "containers_before": containers_before,
        "volumes": [],
        "source": {},
        "limits": {
            "fixture_bytes_max": common.FIXTURE_LIMIT_BYTES,
            "mem_available_floor_bytes": common.MEMORY_FLOOR_BYTES,
            "soak_seconds_allowed": [180, 300],
        },
    }

    def save_progress():
        common.atomic_write_json(progress_path, report)

    try:
        snapshot = copy_snapshot(args.project_dir)
        report["source"] = {
            "git": git_identity(),
            "snapshot_root": str(args.project_dir.resolve()),
            **snapshot,
        }
        report["source"]["git"]["dirty_note"] = (
            "Source snapshot hashes are authoritative for probes; git status "
            "is not used as evidence.")
        save_progress()

        # Import the product from the frozen snapshot, never from the live tree.
        sys.path.insert(0, str(args.project_dir))
        sys.path.insert(0, str(args.project_dir / "src"))
        import multilinux  # noqa: F401
        loaded = Path(multilinux.__file__).resolve()
        if not loaded.is_relative_to(args.project_dir / "src"):
            raise common.ProbeError(
                f"prepare imported product outside the snapshot: {loaded}")
        import tomllib
        from multilinux.runtime import PodmanRuntime
        from multilinux.service import EnvironmentService
        from multilinux.registry import DataRootRegistry
        from multilinux.projects import ProjectRegistry, build_project
        config = tomllib.loads((args.project_dir / "config" / "targets.toml").read_text())
        lock = json.loads((args.project_dir / "config" / "images.lock.json").read_text())
        service = EnvironmentService(args.project_dir, PodmanRuntime(), config, lock)
        roots = DataRootRegistry(args.project_dir)
        projects = ProjectRegistry(args.project_dir)

        for target in targets:
            item_report = {"target": target, "prepared": False}
            report["targets"][target] = item_report
            workspace = args.workspaces / target
            dataset = build_dataset(workspace, target)
            item_report["workspace"] = str(workspace.resolve())
            item_report["dataset"] = dataset
            save_progress()

            env_id = "stagef-" + target
            alias = "stagef-" + target
            record = service.create(
                target, env_id, [str(workspace.resolve())],
                desktop=True, user_namespace="keep-id")
            cid = record["runtime"]["container_id"]
            item_report.update({
                "container_id": cid,
                "environment_uuid": record["environment_uuid"],
                "generation": record["generation"],
                "base_image_digest": record["base_image"]["manifest_digest"],
                "locked_reference": record["base_image"]["locked_reference"],
                "session_user": common.SESSION_USER,
                "environment": record,
            })
            # Persist the exact CID before any guest capture so an interrupted
            # prepare remains inspectable and precisely cleanable.
            save_progress()
            item = {"environment": record, "workspace": str(workspace.resolve())}
            guest = guest_capture(service, item)
            os_release_path = args.project_dir / ("guest-os-release-" + target + ".txt")
            if os_release_path.exists():
                raise common.ProbeError(f"refusing to replace {os_release_path}")
            os_release_path.write_bytes(guest.pop("os_release_bytes"))
            item_report.update({
                "guest_os_release_path": str(os_release_path),
                "guest": guest,
            })

            root = roots.add(str(workspace.resolve()), access_policy="read-write")
            project = projects.add(build_project(
                root, record, alias=alias, default_save_root=str(workspace.resolve()),
                timestamp=service.clock()))
            item_report.update({
                "root_uuid": root["root_uuid"],
                "project_uuid": project["project_uuid"],
                "project_alias": alias,
            })
            service.stop(env_id)
            final_record = service.inspect(env_id)
            if final_record["runtime_state"] != "stopped":
                raise common.ProbeError(f"fixture {env_id} did not stop")

            item_report["environment"] = final_record
            item_report["stopped"] = True
            item_report["prepared"] = True
            report["targets"][target] = item_report
            save_progress()
            print(json.dumps({
                "target": target, "environment": env_id, "container_id": cid,
                "environment_uuid": record["environment_uuid"],
                "project_uuid": project["project_uuid"],
                "root_uuid": root["root_uuid"], "stopped": True,
                "fixture_bytes": dataset["total_fixture_bytes"],
            }, ensure_ascii=False), flush=True)

        containers_after = common.container_inventory()
        new_ids = sorted(set(containers_after) - set(containers_before))
        own_ids = sorted(item["container_id"] for item in report["targets"].values())
        if new_ids != own_ids:
            raise common.ProbeError(
                f"prepare created unexpected containers: new={new_ids}, own={own_ids}")
        if any(row.get("State") == "running" for row in containers_after.values()
               if row["Id"] in set(own_ids)):
            raise common.ProbeError("a Stage F fixture is still running after prepare")
        report.update({
            "prepared": True,
            "finished_at": common.now_iso(),
            "containers_after_prepare": containers_after,
            "new_container_ids": own_ids,
            "all_stopped": True,
        })
        save_progress()
        common.write_new_json(args.output, report)
        print(json.dumps({
            "prepared": True, "output": str(args.output),
            "targets": targets, "new_container_ids": own_ids,
            "all_stopped": True, "acceptance": "pending-primary-review",
        }, ensure_ascii=False))
        return 0
    except BaseException as exc:
        report["prepared"] = False
        report["status"] = "failed"
        report["finished_at"] = common.now_iso()
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()[-12000:]
        save_progress()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
