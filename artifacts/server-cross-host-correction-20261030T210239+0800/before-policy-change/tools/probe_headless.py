"""Headless host acceptance using cached images and an isolated registry.

Usage: python3 tools/probe_headless.py --output /tmp/new-result.json
Never overwrites a receipt, pulls an image or operates on a pre-existing container.
"""

import argparse
import datetime
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.errors import MultilinuxError
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime, container_name
from multilinux.service import EnvironmentService


def inventory():
    rows = json.loads(subprocess.check_output(["podman", "ps", "-a", "--format", "json"], text=True))
    if not rows:
        return []
    details = json.loads(subprocess.check_output(
        ["podman", "inspect", *[row["Id"] for row in rows]], text=True))
    return sorted(({
        "id": item["Id"], "name": item["Name"],
        "status": item["State"]["Status"],
        "started_at": item["State"]["StartedAt"],
        "finished_at": item["State"]["FinishedAt"],
    } for item in details), key=lambda item: item["id"])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--target", choices=("debian13", "fedora44", "arch"), action="append")
    args = parser.parse_args()
    output = args.output
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error("output already exists; choose a new receipt path")
    config, lock, _root = load_project()
    for name, item in lock["targets"].items():
        if args.target and name not in args.target:
            continue
        subprocess.run(["podman", "image", "exists", item["locked_reference"]], check=True)
    before = inventory()
    (ROOT / ".lab").mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="whitelist-live-", dir=ROOT / ".lab"))
    runtime = PodmanRuntime()
    service = EnvironmentService(work, runtime, config, lock)
    result = {
        "captured_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "scope": "Current-host headless lifecycle, native package query and policy replay; no GUI, devices or other-host acceptance",
        "registry": str(work), "before": before, "targets": {}, "errors": [],
    }
    queries = {"debian13": "base-files", "fedora44": "filesystem", "arch": "filesystem"}
    if args.target:
        queries = {name: queries[name] for name in dict.fromkeys(args.target)}
    try:
        for target, package in queries.items():
            record = service.create(target, target, [])
            try:
                created_policy = record["compatibility_at_creation"]
                assert created_policy["allowed"]
                assert created_policy["host"]["observation"] == "live-host"
                running = service.start(target)
                assert running["kernel_matches_current_host"]
                assert service.run(target, ["true"], workdir="/usr", env_keys=[]) == 0
                assert service.run(target, ["sh", "-c", "exit 17"], workdir="/", env_keys=[]) == 17
                query = service.query_package(target, package)
                assert query["present"], query
                # Replay a refused host against the actual running test container.
                # This remains simulated policy input, not another real host.
                service.host_facts = {
                    "os_release": {"ID": "unlisted", "ID_LIKE": "fedora"},
                    "system": "Linux", "machine": "x86_64", "libc": "glibc",
                }
                try:
                    service.run(target, ["true"], workdir="/", env_keys=[])
                except MultilinuxError as exc:
                    assert "whitelist refused" in str(exc)
                else:
                    raise AssertionError("unlisted host executed in a running container")
                service.host_facts = None
                service.stop(target)
                restarted = service.start(target)
                assert restarted["runtime"]["container_id"] == record["runtime"]["container_id"]
                assert service.run(target, ["true"], workdir="/", env_keys=[]) == 0
                result["targets"][target] = {
                    "container_id": record["runtime"]["container_id"],
                    "distribution_id": record["distribution_id"],
                    "version_track": record["version_track"],
                    "base_digest": record["base_image"]["manifest_digest"],
                    "compatibility": created_policy, "query": query,
                    "shared_kernel": True, "exit_code_17": True,
                    "simulated_host_refusal": True,
                    "restart_after_stop": True,
                }
            finally:
                service.host_facts = None
                service.stop(target)
                service.remove(target)
                assert runtime.inspect(container_name(work, target)) is None
    except Exception as exc:
        result["errors"].append(f"{type(exc).__name__}: {exc}")
    result["after"] = inventory()
    result["existing_containers_preserved"] = result["after"] == before
    result["passed"] = (not result["errors"] and len(result["targets"]) == len(queries)
                        and result["existing_containers_preserved"])
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    if result["existing_containers_preserved"]:
        shutil.rmtree(work)
    print(json.dumps({"passed": result["passed"], "errors": result["errors"], "output": str(output)}))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
