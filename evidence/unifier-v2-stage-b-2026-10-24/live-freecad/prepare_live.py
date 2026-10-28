"""Freeze current product code and prepare one fresh, local Arch GUI fixture.

Run only after B's offline checks. Does not pull/install or change old fixtures.
"""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())
sys.path[:0] = [str(REPO / "src"), str(REPO)]

from multilinux.desktop import discover_session
from multilinux.packages import HostObserver
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from tools.kwin_window_probe import window_info


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def labels(paths):
    result = {}
    for path in paths:
        try:
            value = os.getxattr(path, "security.selinux").decode(errors="replace")
            result[str(path)] = {"label": value, "readable": True}
        except OSError as exc:
            result[str(path)] = {"label": None, "errno": exc.errno}
    return result


def main():
    project = HERE / "project"
    if project.exists() or (HERE / "gui-manifest.json").exists():
        raise RuntimeError("refusing to overwrite a prepared fixture")
    project.mkdir()
    paths = [*sorted((REPO / "src/multilinux").glob("*.py")),
             *(REPO / "tools" / name for name in ("ml.py", "unifier.py", "lab.py")),
             REPO / "config/targets.toml", REPO / "config/images.lock.json"]
    source_hashes = {}
    for source in paths:
        relative = source.relative_to(REPO)
        destination = project / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        source_hashes[str(relative)] = sha(source)
        assert sha(destination) == source_hashes[str(relative)]
    config_path = project / "config/targets.toml"
    config_text = config_path.read_text()
    config = tomllib.loads(config_text)
    lock_path = project / "config/images.lock.json"
    lock = json.loads(lock_path.read_text())
    old_lock = json.loads((REPO / "evidence/contract-freecad-gui-2026-10-23/project/config/images.lock.json").read_text())
    arch = old_lock["targets"]["arch"]
    reference = arch["locked_reference"]
    inspected = json.loads(subprocess.check_output(["podman", "image", "inspect", reference], text=True))[0]
    assert inspected["Digest"] == arch["manifest_digest"]
    config_text = config_text.replace(
        "source = " + json.dumps(config["targets"]["arch"]["source"]),
        "source = " + json.dumps(arch["source"]), 1)
    config_path.write_text(config_text)
    config = tomllib.loads(config_text)
    lock["targets"]["arch"] = arch
    save(lock_path, lock)
    work = HERE / "host-project"
    control = work / "control"
    marker = json.loads((HERE / "input-manifest.json").read_text())["window_marker"]
    assert not window_info(HERE / "observer", marker)
    observed_paths = [work, control, work / "宿主原件.FCStd", work / "旁注.txt", control / "gui-control.FCMacro"]
    before_labels = labels(observed_paths)
    baseline = json.loads(subprocess.check_output(["podman", "ps", "-a", "--format", "json"], text=True))
    manifest = {"source_sha256": source_hashes,
                "configuration_sha256": {str(p.relative_to(project)): sha(p) for p in (config_path, lock_path)},
                "image_id": inspected["Id"], "image_digest": inspected["Digest"],
                "locked_reference": reference, "project": str(project), "host_project": str(work),
                "host_before": HostObserver().snapshot(),
                "containers_before": [{k: c.get(k) for k in ("Id", "Names", "State")} for c in baseline],
                "labels_before_create": before_labels, "scope": "one real Arch FreeCAD workflow via v2 routing"}
    save(HERE / "gui-manifest.json", manifest)
    service = EnvironmentService(project, PodmanRuntime(), config, lock)
    record = service.create("arch", "v2-freecad", [str(work)], desktop=True,
                            user_namespace="keep-id", gpu=True, share_label_policy="preserve-host")
    save(HERE / "environment-created.json", record)
    after_labels = labels(observed_paths)
    save(HERE / "labels-after-create.json", after_labels)
    assert before_labels == after_labels, "creating the shared environment changed host SELinux labels"
    launch = service.add_app(
        "v2-freecad", "freecad-b", "/usr/bin/FreeCAD",
        ["/usr/bin/FreeCAD", "-u", str(control / "freecad-test.cfg"), str(control / "gui-control.FCMacro")],
        workdir=str(work), as_host_user=True, on_demand=True,
        env_overlay={"QT_QPA_PLATFORM": "wayland", "LC_ALL": "C.UTF-8", "ML_GUI_CONTROL": str(control)})
    save(HERE / "application-created.json", launch)
    query = service.query_package("v2-freecad", "freecad")
    assert query["present"]
    save(HERE / "freecad-package-query.json", query)
    assert service.inspect("v2-freecad")["runtime_state"] == "stopped"
    print(json.dumps({"prepared": True, "environment_uuid": record["environment_uuid"],
                      "share_label_policy": record["runtime"]["share_label_policy"],
                      "host_labels_preserved": before_labels == after_labels}))


if __name__ == "__main__":
    main()
