"""Independent routing counterexamples; all state is temporary, no Podman."""

import argparse
import json
from pathlib import Path
import sys
import tempfile

REPO = next(p for p in Path(__file__).resolve().parents
            if (p / "src/multilinux").is_dir())
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

from multilinux.errors import MultilinuxError
from multilinux.file_routes import FileRoutes
from multilinux.registry import DataRootRegistry
from test_desktop import SESSION, desktop_service


def ready(base, *, intent="edit", policy="read-write", nested=False):
    work = base / "work"
    (work / "nested").mkdir(parents=True)
    path = work / "nested/doc.txt" if nested else work / "doc.txt"
    path.write_text("fixture")
    service, runtime = desktop_service(base)
    service.create("debian13", None, [str(work)], desktop=True, session=SESSION)
    service.add_app("debian13", "notes", "/usr/bin/true", ["/usr/bin/true"], workdir=str(work))
    roots = DataRootRegistry(base)
    root = roots.add(str(work), access_policy=policy)
    routes = FileRoutes(base, service)
    handler = routes.add_handler("notes", intent=intent, save_capability="explicit-path")
    project = routes.add_project(root["root_uuid"], "debian13", access_policy=policy,
                                 intent_handlers={intent: handler["handler_uuid"]})
    runtime.events.clear()
    return service, runtime, roots, routes, root, handler, project, work, path


def refused(action, runtime):
    try:
        action()
    except MultilinuxError:
        return not runtime.events
    return False


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output")
    checks = {}
    with tempfile.TemporaryDirectory() as tmp:
        service, runtime, roots, routes, root, handler, project, work, path = ready(Path(tmp))
        first = service.show_app("notes")["app_instance_uuid"]
        service.remove_app("notes")
        service.add_app("debian13", "notes", "/usr/bin/true", ["/usr/bin/true"], workdir=str(work))
        runtime.events.clear()
        checks["identical_app_recreation_has_new_uuid"] = first != service.show_app("notes")["app_instance_uuid"]
        checks["old_handler_refused_after_identical_app_recreation"] = refused(lambda: routes.plan(str(path)), runtime)
    with tempfile.TemporaryDirectory() as tmp:
        service, runtime, roots, routes, root, handler, project, work, path = ready(Path(tmp), intent="view", policy="read-only")
        checks["readonly_view_cannot_use_writable_mount"] = refused(lambda: routes.plan(str(path), "view"), runtime)
    with tempfile.TemporaryDirectory() as tmp:
        service, runtime, roots, routes, root, handler, project, work, path = ready(Path(tmp))
        child = work / "restricted"
        child.mkdir()
        roots.add(str(child), access_policy="read-only")
        routes.remove_project(project["project_uuid"])
        checks["default_save_cannot_enter_nested_readonly_root"] = refused(
            lambda: routes.add_project(root["root_uuid"], "debian13", default_save_root=str(child),
                                       intent_handlers={"edit": handler["handler_uuid"]}), runtime)
    with tempfile.TemporaryDirectory() as tmp:
        service, runtime, roots, routes, root, handler, project, work, path = ready(Path(tmp), nested=True)
        other = Path(tmp) / "other"
        other.mkdir()
        (other / "doc.txt").write_text("different content")
        record = service.store.load("debian13")
        record["shared_mounts"].append({"host": str(other), "container": str(work / "nested"), "options": ""})
        service.store.save(record, None)
        checks["shadowing_mount_cannot_substitute_another_host_file"] = refused(lambda: routes.plan(str(path)), runtime)
    with tempfile.TemporaryDirectory() as tmp:
        service, runtime, roots, routes, root, handler, project, work, path = ready(Path(tmp), nested=True)
        record = service.store.load("debian13")
        record["shared_mounts"].append({"host": str(work / "nested"), "container": str(work / "nested"), "options": "ro"})
        service.store.save(record, None)
        checks["nested_readonly_mount_blocks_edit"] = refused(lambda: routes.plan(str(path)), runtime)
    with tempfile.TemporaryDirectory() as tmp:
        service, runtime, roots, routes, root, handler, project, work, path = ready(Path(tmp))
        original_wait = service._wait_while_stopping
        changed = [False]

        def wait_then_change(record):
            info = original_wait(record)
            if not changed[0]:
                changed[0] = True
                launch = service.show_app("notes")
                launch["env"]["AFTER_WAIT"] = "changed"
                service.store.save_launch(launch)
            return info

        service._wait_while_stopping = wait_then_change
        checks["config_change_during_wait_has_zero_start_or_exec"] = refused(lambda: routes.open(str(path)), runtime)
    report = {"all_passed": all(checks.values()), "checks": checks,
              "scope": "independent temporary-file/FakeRuntime routing boundary checks"}
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({"all_passed": report["all_passed"], "checks": len(checks)}))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
