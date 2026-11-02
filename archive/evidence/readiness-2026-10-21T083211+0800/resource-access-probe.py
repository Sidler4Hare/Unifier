#!/usr/bin/env python3
"""Check M4 metric readability in temporary, serial product environments.

These single samples establish collection feasibility, not performance.
The existing project's environments and launch records are not modified.
"""

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime, container_name, project_label
from multilinux.service import EnvironmentService, now


def read_metric(path):
    try:
        return {"value": path.read_text().strip(), "error": None}
    except OSError as exc:
        return {"value": None, "error": str(exc)}


def main():
    config, lock, _ = load_project()
    runtime = PodmanRuntime()
    report = {
        "started_at": now(),
        "scope": "Metric access smoke only; serial fresh environments, single unsettled samples. Not an M4 benchmark.",
        "targets": {},
        "cleanup_errors": [],
    }
    with tempfile.TemporaryDirectory(prefix="ml-readiness-") as temporary:
        isolated_root = Path(temporary)
        service = EnvironmentService(isolated_root, runtime, config, lock)
        for target in config["targets"]:
            env_id = "resource-probe"
            name = container_name(isolated_root, env_id)
            result = {"passed": False, "errors": []}
            report["targets"][target] = result
            try:
                record = service.create(target, env_id, [])
                service.start(env_id)
                view = service.inspect(env_id)
                pid = view["resources"]["pid"]
                result.update({
                    "kernel": view["observed"]["kernel"],
                    "kernel_matches_current_host": view["kernel_matches_current_host"],
                    "image": record["base_image"]["locked_reference"],
                    "inventory_sha256": record["package_inventory_sha256"],
                    "container_id": view["writable_revision"],
                    "pid": pid,
                    "collected_at": now(),
                })
                cgroup_text = (Path("/proc") / str(pid) / "cgroup").read_text()
                relative = next(line[3:] for line in cgroup_text.splitlines() if line.startswith("0::"))
                cgroup = Path("/sys/fs/cgroup") / relative.lstrip("/")
                result["cgroup"] = str(cgroup)
                result["metrics"] = {
                    field: read_metric(cgroup / field)
                    for field in (
                        "memory.current", "memory.peak", "memory.stat", "memory.events",
                        "memory.swap.current", "memory.pressure", "cpu.pressure", "io.pressure",
                        "memory.high", "memory.max", "cpu.max", "cpuset.cpus.effective", "pids.current",
                    )
                }
                result["smaps_rollup"] = read_metric(Path("/proc") / str(pid) / "smaps_rollup")
                required = ("memory.current", "memory.peak", "memory.stat", "memory.events", "memory.pressure")
                result["required_cgroup_readable"] = all(result["metrics"][field]["error"] is None for field in required)
                result["pss_readable"] = result["smaps_rollup"]["error"] is None and "Pss:" in result["smaps_rollup"]["value"]
                result["exec_exit_code"] = service.run(env_id, ["true"], workdir="/usr", env_keys=[])
                service.stop(env_id)
                result["stopped"] = service.inspect(env_id)["runtime_state"] == "stopped"
                result["passed"] = all((result["kernel_matches_current_host"], result["required_cgroup_readable"], result["pss_readable"], result["exec_exit_code"] == 0, result["stopped"]))
            except Exception as exc:
                result["errors"].append(f"{type(exc).__name__}: {exc}")
            finally:
                try:
                    if service.store.exists(env_id):
                        service.remove(env_id)
                    else:
                        info = runtime.inspect(name)
                        if info is not None:
                            runtime.remove(name, project=project_label(isolated_root), env_id=env_id)
                    result["container_removed"] = runtime.inspect(name) is None
                except Exception as exc:
                    report["cleanup_errors"].append(f"{target}: {type(exc).__name__}: {exc}")
            print(target, json.dumps({k: result.get(k) for k in ("passed", "required_cgroup_readable", "pss_readable", "container_removed", "errors")}), flush=True)
    report["finished_at"] = now()
    report["passed"] = not report["cleanup_errors"] and all(item["passed"] and item.get("container_removed") for item in report["targets"].values())
    output = Path(__file__).with_name("resource-access.json")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
