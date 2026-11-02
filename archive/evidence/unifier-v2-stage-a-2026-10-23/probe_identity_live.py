"""Isolated native-runtime acceptance for Unifier's v2 identity contract.

Uses local locked images, no network, no GUI, no shares or package installs.
Every created container belongs to a temporary project and is removed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import uuid

REPO = next(parent for parent in Path(__file__).resolve().parents
            if (parent / "src/multilinux").is_dir())
sys.path[:0] = [str(REPO / "src"), str(REPO)]

from multilinux.control import Control
from multilinux.errors import MultilinuxError
from multilinux.identity import binding_for
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime, container_name, project_label
from multilinux.service import EnvironmentService


class AuditedRuntime:
    def __init__(self):
        self.backend = PodmanRuntime()
        self.mutations = []

    def __getattr__(self, name):
        function = getattr(self.backend, name)
        if name not in {"create", "start", "stop", "remove", "exec", "copy_in"}:
            return function

        def recorded(*args, **kwargs):
            self.mutations.append(name)
            return function(*args, **kwargs)

        return recorded


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists; choose a new acceptance run")
    config, lock, _ = load_project()
    report = {"scope": "v2 identity and read-only FileRef on one host; no GUI or file transfer",
              "targets": {}, "passed": False}
    sources = [REPO / "src/multilinux" / (name + ".py") for name in (
        "service", "identity", "store", "catalog", "runtime", "share_label", "control", "registry")]
    report["source_sha256"] = {
        str(path.relative_to(REPO)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sources}
    with tempfile.TemporaryDirectory(prefix="unifier-v2-acceptance-") as temporary:
        root = Path(temporary)
        runtime = AuditedRuntime()
        service = EnvironmentService(root, runtime, config, lock)
        control = Control(root)
        data = root / "user-data"
        data.mkdir()
        (data / "note.txt").write_text("Unifier identity acceptance\n", encoding="utf-8")
        data_root = control.add_root(str(data))
        alias = "v2-identity-probe"
        try:
            for target in ("debian13", "fedora44", "arch"):
                item = {"locked_reference": lock["targets"][target]["locked_reference"]}
                report["targets"][target] = item
                record = service.create(target, alias, [])
                assert record["schema_version"] == 2 and record["generation"] == 1
                assert uuid.UUID(record["environment_uuid"]).version == 4
                item["created_identity"] = record["environment_uuid"]
                container_id = record["runtime"]["container_id"]

                # A persisted non-GUI launch fixture exercises the actual exec
                # path without installing an app or exporting a desktop entry.
                launch = {"schema_version": 2, "id": "identity-probe",
                          "environment_id": alias, "environment_binding": binding_for(record),
                          "argv": ["/usr/bin/true"], "executable": "/usr/bin/true",
                          "workdir": None, "env": {}, "user": None, "on_demand": False}
                service.store.save_launch(launch)
                assert service.exec_app("identity-probe", ["/usr/bin/true"], capture=True).returncode == 0
                assert service.exec_app("identity-probe", ["/bin/sh", "-c", "exit 17"], capture=True).returncode == 17
                assert service.store.load(alias)["runtime"]["container_id"] == container_id
                item.update(repeated_exec_same_container=True, exit_code_17_preserved=True,
                            share_label_policy=record["runtime"].get("share_label_policy"))
                service.stop(alias)
                record = service.store.load(alias)
                record.pop("environment_uuid")
                record.pop("generation")
                record["schema_version"] = 1
                service.store.save(record, None)
                original = service.store.path(alias).read_bytes()
                inventory = service.store.inventory_path(alias).read_bytes()
                before = list(runtime.mutations)
                first = service.migrate_environment(alias)
                assert runtime.mutations == before
                assert Path(first["backup"]).read_bytes() == original
                migrated = service.store.load(alias)
                assert migrated["runtime"]["container_id"] == container_id
                assert service.store.inventory_path(alias).read_bytes() == inventory
                after_migration = service.store.path(alias).read_bytes()
                second = service.migrate_environment(alias)
                assert second["environment_uuid"] == first["environment_uuid"]
                assert service.store.path(alias).read_bytes() == after_migration
                assert runtime.mutations == before
                item["migration"] = {"no_runtime_mutation": True, "exact_backup": True,
                                     "idempotent": True, "inventory_preserved": True,
                                     "container_id_preserved": True}

                launch["environment_binding"] = binding_for(migrated)
                service.store.save_launch(launch)
                assert service.exec_app("identity-probe", ["/usr/bin/true"], capture=True).returncode == 0
                service.stop(alias)
                old_uuid = migrated["environment_uuid"]
                service.remove(alias)
                assert control.show_root(data_root["root_uuid"])["locator"] == str(data)
                replacement = service.create(target, alias, [])
                assert replacement["environment_uuid"] != old_uuid
                assert replacement["runtime"]["container_id"] != container_id
                before = list(runtime.mutations)
                try:
                    service.exec_app("identity-probe", ["/usr/bin/true"], capture=True)
                except MultilinuxError as exc:
                    item["stale_binding_error"] = str(exc)
                else:
                    raise AssertionError("same-name replacement accepted an old application binding")
                assert runtime.mutations == before
                assert service.inspect(alias)["runtime_state"] == "stopped"
                reference = control.resolve_file(data_root["root_uuid"], "note.txt")
                assert reference["observed_version"]["ino"] == (data / "note.txt").stat().st_ino
                assert not reference["capabilities"]["execute"]
                item.update(replacement_has_new_uuid=True, stale_binding_zero_mutations=True,
                            data_root_survives_environment_removal=True, file_ref_observed=True)
                service.remove(alias)
                assert runtime.inspect(container_name(root, alias)) is None
                item["temporary_containers_removed"] = True
        finally:
            if service.store.exists(alias):
                service.remove(alias)
            else:
                name = container_name(root, alias)
                info = runtime.inspect(name)
                if info is not None and info.project == project_label(root) and info.env_id == alias:
                    runtime.remove(name, project=project_label(root), env_id=alias)
        report["source_unchanged_during_run"] = all(
            hashlib.sha256((REPO / path).read_bytes()).hexdigest() == digest
            for path, digest in report["source_sha256"].items())
        report["passed"] = report["source_unchanged_during_run"]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"passed": report["passed"], "targets": list(report["targets"])}))
    if not report["passed"]:
        raise SystemExit("Source changed during acceptance; inspect and rerun a new result file")


if __name__ == "__main__":
    main()
