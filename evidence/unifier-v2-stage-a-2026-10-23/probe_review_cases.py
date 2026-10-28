"""Replay independently discovered stage-A boundary cases in temporary state."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

REPO = next(p for p in Path(__file__).resolve().parents
            if (p / "src/multilinux").is_dir())
sys.path[:0] = [str(REPO / "src"), str(REPO / "tests")]

from multilinux.control import Control
from multilinux.errors import MultilinuxError
from test_env import service_in


def legacy(service):
    record = service.store.load("debian13")
    record.pop("environment_uuid")
    record.pop("generation")
    record["schema_version"] = 1
    return record


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output file")
    checks = {}
    mutations = {
        "schema2_without_identity": lambda r: r.update(schema_version=2),
        "null_identity": lambda r: r.update(environment_uuid=None, generation=None),
        "minimal_legacy": lambda r: (r.clear(), r.update(schema_version=1, id="debian13", target="debian13")),
    }
    for key in ("network", "selinux_label", "user_namespace", "ipc"):
        for value in ([], {}, None):
            mutations[f"runtime_{key}_{type(value).__name__}"] = (
                lambda r, k=key, v=value: r["runtime"].update({k: v}))
    for name, mutate in mutations.items():
        with tempfile.TemporaryDirectory() as tmp:
            service, runtime = service_in(Path(tmp))
            service.create("debian13", None, [])
            record = legacy(service)
            mutate(record)
            service.store.save(record, None)
            before = service.store.path("debian13").read_bytes()
            runtime.events.clear()
            refused = False
            try:
                service.migrate_environment("debian13")
            except MultilinuxError:
                refused = True
            checks[name] = (refused and not runtime.events
                            and service.store.path("debian13").read_bytes() == before
                            and not list((service.store.directory / "backups").glob("*.json")))
    with tempfile.TemporaryDirectory() as tmp:
        service, _ = service_in(Path(tmp))
        originals, backups = [], []
        for index in range(2):
            service.create("debian13", None, [])
            record = legacy(service)
            record["probe_marker"] = index
            service.store.save(record, None)
            originals.append(service.store.path("debian13").read_bytes())
            backups.append(Path(service.migrate_environment("debian13")["backup"]))
            service.remove("debian13")
        checks["same_alias_keeps_two_exact_backups"] = (
            backups[0] != backups[1]
            and all(path.read_bytes() == payload for path, payload in zip(backups, originals)))
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp)
        data = base / "data"
        nested = data / "nested"
        nested.mkdir(parents=True)
        file = nested / "a.txt"
        file.write_text("local fixture")
        control = Control(base / "state")
        parent = control.add_root(str(data), access_policy="read-write")
        child = control.add_root(str(nested), access_policy="read-only")
        try:
            control.resolve_file(parent["root_uuid"], "nested/a.txt")
        except MultilinuxError:
            checks["parent_ref_cannot_bypass_nested_policy"] = True
        else:
            checks["parent_ref_cannot_bypass_nested_policy"] = False
        resolved = control.resolve_path(str(file))
        checks["absolute_ref_selects_nested_read_only_root"] = (
            resolved["root_uuid"] == child["root_uuid"] and resolved["access_policy"] == "read-only")
        os.link(file, data / "alias.txt")
        try:
            control.resolve_file(parent["root_uuid"], "alias.txt")
        except MultilinuxError:
            checks["cross_root_hardlink_refused"] = True
        else:
            checks["cross_root_hardlink_refused"] = False
        original = control.registry.path.read_bytes()
        for field in ("backend", "management_domain", "access_policy"):
            for value in ([], {}, None):
                payload = json.loads(original)
                payload["roots"][0][field] = value
                raw = json.dumps(payload).encode()
                control.registry.path.write_bytes(raw)
                refused = False
                try:
                    control.list_roots()
                except MultilinuxError:
                    refused = True
                checks[f"registry_{field}_{type(value).__name__}"] = (
                    refused and control.registry.path.read_bytes() == raw)
    result = {"checks": checks, "all_passed": all(checks.values()),
              "scope": "independent boundary cases using real temporary files and FakeRuntime"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({"all_passed": result["all_passed"], "checks": len(checks)}))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
