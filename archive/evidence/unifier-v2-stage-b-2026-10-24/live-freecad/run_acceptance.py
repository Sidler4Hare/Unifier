"""Exercise v2 file routing through real FreeCAD windows, dialogs and restart.

Requires prepare_live.py and B's final product snapshot. This fixture owns all
commands, files, windows and containers it changes; previous evidence is read.
"""

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import tomllib
import zipfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
REPO = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())
PROJECT = HERE / "project"
sys.path[:0] = [str(PROJECT / "src"), str(PROJECT), str(REPO), str(HERE)]

from gui_command import command
from multilinux.on_demand import process_stamp
from multilinux.registry import DataRootRegistry
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from tools.kwin_window_probe import screenshot, window_info


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(name, value):
    path = HERE / name
    with path.open("x") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")


def cli(*argv, expected=0):
    result = subprocess.run([sys.executable, str(PROJECT / "tools/unifier.py"), *argv],
                            capture_output=True, text=True, timeout=40)
    if result.returncode != expected:
        raise RuntimeError(f"{argv}: exit {result.returncode}: {result.stderr[-2000:]}")
    return result


def wait_gui(path, objects, process):
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(f"file launcher exited early: {process.returncode}")
        try:
            state = command({"op": "inspect"}, timeout=2)
        except TimeoutError:
            continue
        documents = [doc for doc in state["documents"] if doc["filename"] == str(path)]
        if (state["visible"] and state["platform"] == "wayland" and len(documents) == 1
                and len(documents[0]["objects"]) == objects and not state["dialogs"]):
            assert all(item.get("valid", True) for item in documents[0]["objects"])
            return state
        time.sleep(.2)
    raise RuntimeError("real GUI did not load the expected file/objects")


def raw_leases():
    path = PROJECT / ".lab/leases/v2-freecad.json"
    return json.loads(path.read_text()) if path.exists() else {}


def wait_stopped(service, marker):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        view = service.inspect("v2-freecad")
        leases = raw_leases()
        if (view["runtime_state"] == "stopped" and not leases.get("leases")
                and leases.get("bridge") is None):
            if not window_info(HERE / "observer", marker):
                return {"environment": view, "leases": leases}
        time.sleep(.2)
    raise RuntimeError("managed GUI close did not stop/release the environment")


def capture(name, marker):
    for attempt in range(3):
        try:
            value = screenshot(HERE / "observer", marker, HERE / (name + ".png"))
            save(name + "-window.json", value)
            return
        except RuntimeError as exc:
            if attempt == 2:
                raise
            save(name + f"-capture-retry-{attempt}.json", {"reason": str(exc)})
            time.sleep(.4)


def documents_on_host(paths):
    result = {}
    for path in paths:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            objects = ET.fromstring(archive.read("Document.xml")).findall("./Objects/Object")
        result[path.name] = {"sha256": sha(path), "objects": len(objects),
                             "uid": path.stat().st_uid, "gid": path.stat().st_gid}
    return result


def main():
    if (HERE / "acceptance.json").exists():
        raise RuntimeError("refusing to overwrite completed acceptance")
    manifest = json.loads((HERE / "gui-manifest.json").read_text())
    inputs = json.loads((HERE / "input-manifest.json").read_text())
    marker = inputs["window_marker"]
    for relative, digest in manifest["source_sha256"].items():
        if not relative.startswith("config/"):
            assert sha(REPO / relative) == sha(PROJECT / relative) == digest
    config = tomllib.loads((PROJECT / "config/targets.toml").read_text())
    lock = json.loads((PROJECT / "config/images.lock.json").read_text())
    service = EnvironmentService(PROJECT, PodmanRuntime(), config, lock)
    work = HERE / "host-project"
    original = work / "宿主原件.FCStd"
    created = work / "新建 零件.FCStd"
    assert sha(original) == inputs["input_sha256"]
    root = json.loads(cli("root", "add", str(work)).stdout)
    handler = json.loads(cli("handler", "add", "freecad-b", "--intent", "edit",
                             "--save-capability", "explicit-path", "--alias", "freecad-edit").stdout)
    project = json.loads(cli("project", "add", "--root", root["root_uuid"],
                             "--environment", "v2-freecad", "--alias", "cad-project",
                             "--handler", "edit=" + handler["handler_uuid"]).stdout)
    save("routes-created.json", {"root": root, "handler": handler, "project": project})
    plan = json.loads(cli("file", "plan", "--path", str(original), "--intent", "edit").stdout)
    save("file-plan.json", plan)
    assert plan["file_path"] == str(original) and plan["cwd"] == str(work)
    assert plan["container_id"] == service.store.load("v2-freecad")["runtime"]["container_id"]
    assert plan["execution"]["on_demand"] and plan["access_scope"]["mode"] == "environment-shared-static-mounts"
    sessions = {}
    active = None
    initial_container = plan["container_id"]
    try:
        for phase, path, count in (("edit", original, 3), ("reopen-original", original, 4),
                                   ("reopen-new-kill-launcher", created, 1)):
            with (HERE / (phase + ".log")).open("x") as log:
                active = subprocess.Popen(
                    [sys.executable, str(PROJECT / "tools/unifier.py"), "file", "open",
                     "--path", str(path), "--intent", "edit"],
                    cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT)
                state = wait_gui(path, count, active)
                save(phase + "-opened.json", state)
                leases = raw_leases()
                assert len(leases.get("leases", {})) == 1
                lease = next(iter(leases["leases"].values()))
                assert lease["pid"] != active.pid, "file open regressed to a foreground lease owner"
                assert process_stamp(lease["pid"]) == lease["stamp"]
                sessions[phase] = {"launcher_pid": active.pid, "worker_pid": lease["pid"],
                                   "container_id": service.inspect("v2-freecad")["runtime"]["container_id"]}
                assert sessions[phase]["container_id"] == initial_container
                command({"op": "view"})
                capture(phase + "-window", marker)
                if phase == "edit":
                    result = subprocess.run([sys.executable, str(HERE / "run_gui_steps.py")],
                                            cwd=HERE, capture_output=True, text=True, timeout=100)
                    save("gui-steps-process.json", {"exit_code": result.returncode,
                                                    "stdout": result.stdout, "stderr": result.stderr})
                    assert result.returncode == 0, result.stderr
                if phase == "reopen-new-kill-launcher":
                    active.kill()
                    sessions[phase]["launcher_exit"] = active.wait(timeout=5)
                    assert sessions[phase]["launcher_exit"] == -9
                    assert process_stamp(lease["pid"]) == lease["stamp"]
                    after_kill = command({"op": "inspect"})
                    assert after_kill["visible"] and after_kill["documents"]
                    save("gui-survives-launcher-kill.json", after_kill)
                command({"op": "exit"})
                if active.poll() is None:
                    sessions[phase]["launcher_exit"] = active.wait(timeout=30)
                    assert sessions[phase]["launcher_exit"] == 0
                stopped = wait_stopped(service, marker)
                save(phase + "-stopped.json", stopped)
                active = None
        readback = documents_on_host([original, created])
        assert readback[original.name]["objects"] == 4 and readback[created.name]["objects"] == 1
        assert all(item["uid"] == item["gid"] == 1000 for item in readback.values())
        save("host-readback.json", readback)
        before_uninstall = {path.name: sha(path) for path in (original, created, work / "旁注.txt")}
        transaction = service.remove_packages("v2-freecad", ["freecad"],
                                              use_argv=["test", "-x", "/usr/bin/FreeCAD"])
        save("package-remove.json", transaction)
        assert not service.query_package("v2-freecad", "freecad")["present"]
        assert before_uninstall == {path.name: sha(path) for path in (original, created, work / "旁注.txt")}
        assert DataRootRegistry(PROJECT).show(root["root_uuid"])["locator"] == str(work)
        save("project-survives-package-uninstall.json", {"files_preserved": True,
                                                        "root_registration_preserved": True,
                                                        "freecad_removed": True})
        service.remove_app("freecad-b")
        cli("file", "plan", "--path", str(original), expected=1)
        cli("handler", "remove", handler["handler_uuid"])
        cli("project", "remove", project["project_uuid"])
        service.stop("v2-freecad")
        service.remove("v2-freecad")
        assert DataRootRegistry(PROJECT).show(root["root_uuid"])["locator"] == str(work)
        assert before_uninstall == {path.name: sha(path) for path in (original, created, work / "旁注.txt")}
        assert sha(Path(inputs["source"])) == inputs["source_sha256"]
        assert sha(work / "旁注.txt") == inputs["note_sha256"]
        save("acceptance.json", {"passed": True, "sessions": sessions,
                                  "root_survives_environment_removal": True,
                                  "original_evidence_unchanged": True,
                                  "scope": "real Wayland FreeCAD via v2 file routing, actions/dialogs, restart, launcher SIGKILL, native package uninstall"})
        print(json.dumps({"passed": True, "sessions": list(sessions)}))
    finally:
        if service.store.exists("v2-freecad"):
            service.stop("v2-freecad")
        if active is not None and active.poll() is None:
            active.wait(timeout=20)


if __name__ == "__main__":
    main()
