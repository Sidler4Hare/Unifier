"""Prepare isolated E session fixtures with frozen accepted B creation code.

All images are already cached and digest locked. Explicit desktop environments
use the existing label=disable contract without relabeling host project data.
Only these newly created instances are started or stopped here.
"""
from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())
PROJECT = HERE / "native-project"
FROZEN = ROOT / "evidence/unifier-v2-stage-b-2026-10-24/live-freecad/project"
sys.path[:0] = [str(FROZEN / "src"), str(FROZEN)]
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService


def main():
    assert not PROJECT.exists()
    PROJECT.mkdir(mode=0o700)
    (PROJECT / "config").mkdir()
    for name in ["targets.toml", "images.lock.json"]:
        shutil.copy2(ROOT / "config" / name, PROJECT / "config" / name)
    config = tomllib.loads((PROJECT / "config/targets.toml").read_text())
    lock = json.loads((PROJECT / "config/images.lock.json").read_text())
    service = EnvironmentService(PROJECT, PodmanRuntime(), config, lock)
    report = {"prepared": False, "project": str(PROJECT), "targets": {},
              "scope": "new isolated same-path project shares, keep-id, existing desktop SELinux contract"}
    for target in ["debian13", "fedora44", "arch"]:
        work = HERE / ("workspace-" + target)
        work.mkdir(mode=0o700)
        (work / "input 中文.txt").write_text("共享项目输入\n")
        child = work / "child.sh"
        child.write_text('#!/bin/sh\n. /etc/os-release\nprintf "child:%s:%s\\n" "$ID" "$1"\n')
        child.chmod(0o700)
        script = work / "probe.sh"
        script.write_text('#!/bin/sh\nset -eu\n. /etc/os-release\nprintf "parent:%s\\n" "$ID"\n/usr/bin/env sh ./child.sh "$1"\n')
        script.chmod(0o700)
        (work / "absolute-os-release").symlink_to("/etc/os-release")
        env_id = "session-" + target
        record = service.create(target, env_id, [str(work)], desktop=True, user_namespace="keep-id")
        row = {"environment": record, "workspace": str(work), "stopped": False}
        report["targets"][target] = row
        (HERE / "fixture-progress.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
        service.start(env_id)
        cid = record["runtime"]["container_id"]
        observation = subprocess.check_output(["podman", "exec", cid, "/bin/sh", "-c",
            'set -eu; . /etc/os-release; printf "ID=%s\\n" "$ID"; '
            'printf "kernel="; uname -r; '
            'for x in sh bash setsid python3 perl; do printf "%s=" "$x"; command -v "$x" || true; done'],
            text=True, timeout=15)
        row["guest_capabilities"] = observation
        service.stop(env_id)
        assert service.inspect(env_id)["runtime_state"] == "stopped"
        row["stopped"] = True
        (HERE / "fixture-progress.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    report["prepared"] = True
    with (HERE / "fixtures.json").open("x") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps({"prepared": True, "targets": list(report["targets"]), "all_stopped": True}))


if __name__ == "__main__":
    main()
