#!/usr/bin/env python3
"""Preparation commands for the Debian 13 / Fedora 44 / Arch laboratory.

Uses Python 3.11+, rootless Podman and Skopeo. Images are resolved to the exact
linux/amd64 manifest before pulling. This is preparation tooling, not the v1
application/environment lifecycle manager.
"""

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
PROJECT_LABEL = "multilinux.lab"


class LabError(RuntimeError):
    pass


def now():
    return dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def command(args, *, timeout=60, binary=False):
    try:
        result = subprocess.run(args, capture_output=True, text=not binary,
                                timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise LabError(f"{args[0]} failed: {exc}") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace") if binary else result.stderr
        raise LabError(f"{args[0]} exited {result.returncode}: {detail[-2500:]}")
    return result.stdout


def save_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Replace only after a complete payload has been written.
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     prefix=".lab-json-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def targets():
    data = tomllib.loads((ROOT / "config/targets.toml").read_text())
    if (data.get("schema_version") != 1 or data.get("platform") != "linux/amd64"
            or data.get("runtime") != "podman"):
        raise LabError("Unsupported target configuration")
    if set(data.get("targets", {})) != {"debian13", "fedora44", "arch"}:
        raise LabError("The v1 experiment requires exactly debian13, fedora44 and arch")
    prepared = _prepared_catalog()
    from multilinux.compatibility import compatibility_mode
    from multilinux.errors import MultilinuxError
    try:
        compatibility_mode(data)
    except MultilinuxError as exc:
        raise LabError(str(exc)) from exc
    if set(prepared) != set(data["targets"]):
        raise LabError("Prepared catalog does not match the v1 target set")
    for name, expected in prepared.items():
        target = data["targets"][name]
        for key, value in expected.items():
            if target.get(key) != value:
                raise LabError(
                    f"{name} catalog field {key} must be {value} for the prepared experiment")
    return data


def _prepared_catalog():
    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    from multilinux.catalog import PREPARED
    return PREPARED


def repository(reference):
    return reference.rsplit(":", 1)[0]


def select_manifest(raw, wanted_platform):
    manifest = json.loads(raw)
    root_digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if "manifests" not in manifest:
        return root_digest, root_digest
    os_name, architecture = wanted_platform.split("/")
    matches = [item for item in manifest["manifests"]
               if item.get("platform", {}).get("os") == os_name
               and item.get("platform", {}).get("architecture") == architecture
               and item.get("platform", {}).get("variant", "") in ("", "v1")]
    if len(matches) != 1 or not DIGEST.fullmatch(matches[0].get("digest", "")):
        raise LabError("Image index does not contain one unambiguous linux/amd64 manifest")
    return root_digest, matches[0]["digest"]


def read_lock(config):
    path = ROOT / "config/images.lock.json"
    if not path.exists():
        raise LabError("Image lock is missing; run: python3 tools/lab.py lock")
    lock = json.loads(path.read_text())
    if lock.get("schema_version") != 1 or lock.get("platform") != config["platform"]:
        raise LabError("Image lock schema/platform does not match targets.toml")
    if set(lock.get("targets", {})) != set(config["targets"]):
        raise LabError("Image lock target set does not match targets.toml")
    for name, target in config["targets"].items():
        item = lock["targets"][name]
        digest = item.get("manifest_digest", "")
        expected_ref = repository(target["source"]) + "@" + digest
        if (not DIGEST.fullmatch(digest) or item.get("source") != target["source"]
                or item.get("locked_reference") != expected_ref
                or item.get("os") != "linux" or item.get("architecture") != "amd64"):
            raise LabError(f"Invalid or stale image lock for {name}")
    return lock


def require_rootless():
    if os.geteuid() == 0:
        raise LabError("Run the laboratory as your ordinary user, without sudo")
    data = json.loads(command(["podman", "info", "--format", "json"], timeout=30))
    host = data.get("host", {})
    if host.get("security", {}).get("rootless") is not True:
        raise LabError("This laboratory requires rootless Podman")
    return data


def doctor(args):
    config = targets()
    from multilinux.compatibility import classify_host, observe_host, relationship_plan
    facts = observe_host()
    host_profile = classify_host(facts)
    checks = []
    checks.append({"name": "host-whitelist", "ok": host_profile["admitted"],
                   "detail": host_profile})
    tools = {name: shutil.which(name) is not None for name in ("podman", "skopeo")}
    checks.append({"name": "python", "ok": sys.version_info >= (3, 11),
                   "detail": platform.python_version()})
    checks.append({"name": "linux-amd64", "ok": sys.platform == "linux" and
                   platform.machine() == "x86_64", "detail": platform.machine()})
    for name, available in tools.items():
        checks.append({"name": name, "ok": available})
    runtime = None
    if tools["podman"]:
        try:
            info = require_rootless()
            host, store = info.get("host", {}), info.get("store", {})
            runtime = {"rootless": True, "cgroup_version": host.get("cgroupVersion"),
                       "cgroup_manager": host.get("cgroupManager"),
                       "oci_runtime": host.get("ociRuntime", {}).get("name"),
                       "storage_driver": store.get("graphDriverName")}
            checks.append({"name": "rootless-runtime", "ok": True})
            checks.append({"name": "cgroup-v2", "ok": runtime["cgroup_version"] == "v2"})
        except LabError as exc:
            checks.append({"name": "rootless-runtime", "ok": False, "detail": str(exc)})
    lock_status = "not-created"
    if (ROOT / "config/images.lock.json").exists():
        try:
            read_lock(config)
            lock_status = "valid"
        except LabError as exc:
            lock_status = str(exc)
            checks.append({"name": "image-lock", "ok": False, "detail": str(exc)})
    result = {"schema_version": 1, "captured_at": now(),
              "ready_for_image_preparation": all(c["ok"] for c in checks),
              "kernel": platform.release(), "uid": os.getuid(), "gid": os.getgid(),
              "runtime": runtime, "checks": checks, "image_lock": lock_status,
              "compatibility": {
                  name: relationship_plan(facts, name, target)
                  for name, target in config["targets"].items()},
              "scope": "Runtime preflight; does not prove any target application is compatible."}
    if args.output:
        save_json(args.output, result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ready_for_image_preparation"] else 1


def lock_images(args):
    config = targets()
    lock_path = ROOT / "config/images.lock.json"
    if lock_path.exists() and not args.refresh:
        lock = read_lock(config)
        print(f"Keeping existing lock from {lock['resolved_at']}; use --refresh explicitly to resolve new images")
        return 0
    lock = {"schema_version": 1, "resolved_at": now(), "platform": config["platform"],
            "arch_policy": "Resolve current latest, then pin its manifest. Before installing packages in a mutable Arch environment, perform pacman -Syu.",
            "targets": {}}
    for name, target in config["targets"].items():
        print(f"Resolving {name}: {target['source']}", flush=True)
        raw = command(["skopeo", "inspect", "--raw", "--command-timeout", "45s",
                       "docker://" + target["source"]], timeout=50, binary=True)
        index_digest, manifest_digest = select_manifest(raw, config["platform"])
        ref = repository(target["source"]) + "@" + manifest_digest
        meta = json.loads(command(["skopeo", "inspect", "--no-tags", "--command-timeout",
                                   "45s", "docker://" + ref], timeout=50))
        if meta.get("Os") != "linux" or meta.get("Architecture") != "amd64":
            raise LabError(f"Resolved image has wrong platform: {name}")
        if meta.get("Digest") != manifest_digest:
            raise LabError(f"Registry manifest digest mismatch: {name}")
        lock["targets"][name] = {
            "source": target["source"], "source_manifest_digest": index_digest,
            "manifest_digest": manifest_digest, "locked_reference": ref,
            "os": meta["Os"], "architecture": meta["Architecture"],
            "image_created_at": meta.get("Created"),
            "image_version_label": (meta.get("Labels") or {}).get("org.opencontainers.image.version"),
            "compressed_layer_bytes": sum(layer.get("Size", 0) for layer in meta.get("LayersData", [])),
        }
    save_json(lock_path, lock)
    print(f"Saved complete image lock: {lock_path}")
    return 0


def pull_images(args):
    config = targets()
    lock = read_lock(config)
    require_rootless()
    result = {"schema_version": 1, "started_at": now(), "targets": {}}
    for name, item in lock["targets"].items():
        print(f"Pulling {name}: {item['locked_reference']}", flush=True)
        started = time.monotonic()
        command(["podman", "pull", "--platform", config["platform"],
                 item["locked_reference"]], timeout=600)
        image = json.loads(command(["podman", "image", "inspect", item["locked_reference"]]))[0]
        result["targets"][name] = {
            "image_id": image["Id"], "locked_reference": item["locked_reference"],
            "reported_image_size_bytes": image.get("Size"),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        print(f"Ready: {name}", flush=True)
    result["finished_at"] = now()
    result["size_note"] = "Per-image reported size, not additive unique physical disk usage. Pull duration is not an application benchmark."
    save_json(args.output or ROOT / "evidence/lab-pull-current.json", result)
    return 0


def parse_probe(stdout):
    return dict(line[3:].split("=", 1) for line in stdout.splitlines()
                if line.startswith("ML_") and "=" in line)


def validate_probe(probe, target, expected_hash, uid, gid, kernel):
    expected = {"ID": target["expected_id"], "UID": str(uid), "GID": str(gid),
                "KERNEL": kernel, "ARCH": "x86_64", "INPUT_SHA256": expected_hash}
    if "expected_version_id" in target:
        expected["VERSION_ID"] = target["expected_version_id"]
    for key, value in expected.items():
        if probe.get(key) != value:
            raise LabError(f"Smoke mismatch for {key}: expected {value!r}, got {probe.get(key)!r}")


def smoke(args):
    config = targets()
    lock = read_lock(config)
    require_rootless()
    run_id = uuid.uuid4().hex[:12]
    shared = ROOT / ".lab/runs" / run_id
    shared.mkdir(parents=True)
    fixture = shared / "probe.sh"
    shutil.copyfile(ROOT / "fixtures/lab-smoke.sh", fixture)
    host_input = shared / "host-input.txt"
    host_input.write_text("multilinux shared-file probe " + run_id + "\n")
    expected_hash = hashlib.sha256(host_input.read_bytes()).hexdigest()
    uid, gid, kernel = os.getuid(), os.getgid(), platform.release()
    evidence_path = Path(args.output or ROOT / "evidence/lab-smoke-current.json")
    inventory_dir = evidence_path.parent / (evidence_path.stem + ".packages")
    inventory_dir.mkdir(parents=True, exist_ok=True)
    report = {"schema_version": 1, "run_id": run_id, "started_at": now(),
              "lock_resolved_at": lock["resolved_at"], "host_kernel": kernel,
              "host_uid": uid, "host_gid": gid, "targets": {}, "passed": False,
              "scope": "Ephemeral CLI base-image probes; not package-install, GUI, service or cross-host acceptance.",
              "settings": {"rootless": True, "user_namespace": "keep-id", "network": "none",
                           "rootfs": "read-only", "full_init": False,
                           "shared_path_mode": "same absolute host/container path", "volume_label": "z"}}
    failure = None
    try:
        for name, target in config["targets"].items():
            item = lock["targets"][name]
            ref = item["locked_reference"]
            # Inspection must succeed locally; --pull=never prevents tag/network fallback.
            image = json.loads(command(["podman", "image", "inspect", ref]))[0]
            container_name = f"multilinux-smoke-{name}-{run_id}"
            argv = ["podman", "run", "--rm", "--name", container_name,
                    "--label", PROJECT_LABEL + "=" + str(ROOT), "--pull=never",
                    "--network=none", "--http-proxy=false", "--read-only",
                    "--userns=keep-id", "--user", f"{uid}:{gid}", "--cap-drop=all",
                    "--security-opt=no-new-privileges", "--pids-limit=64",
                    "--tmpfs", "/tmp:rw,nosuid,nodev,size=32m", "--volume",
                    f"{shared}:{shared}:z", "--entrypoint", "/bin/sh", ref,
                    str(fixture), str(shared), name, str(uid), str(gid), kernel,
                    expected_hash, target["package_manager"]]
            print(f"Probing {name}", flush=True)
            try:
                stdout = command(argv, timeout=60)
                probe = parse_probe(stdout)
                validate_probe(probe, target, expected_hash, uid, gid, kernel)
                output_file = shared / f"{name}.result.txt"
                if output_file.read_text().strip() != name + ":" + expected_hash:
                    raise LabError(f"Host cannot verify output from {name}")
                if (output_file.stat().st_uid, output_file.stat().st_gid) != (uid, gid):
                    raise LabError(f"Unexpected shared-file ownership from {name}")
                packages = shared / f"{name}.packages.txt"
                inventory = inventory_dir / f"{name}.txt"
                shutil.copyfile(packages, inventory)
                report["targets"][name] = {
                    "passed": True, "locked_reference": ref, "image_id": image["Id"],
                    "probe": probe, "package_count": len(packages.read_text().splitlines()),
                    "package_inventory": str(inventory.relative_to(ROOT)) if inventory.is_relative_to(ROOT) else str(inventory),
                    "package_inventory_sha256": hashlib.sha256(inventory.read_bytes()).hexdigest(),
                    "shared_file_host_owner_verified": True,
                }
            finally:
                # Only this run's uniquely named probe container can be removed.
                command(["podman", "rm", "--force", "--ignore", container_name], timeout=20)
        seen = (shared / "cross-environment.txt").read_text().splitlines()
        if seen != list(config["targets"]):
            raise LabError("Cross-environment shared-file sequence is incorrect")
        report["cross_environment_shared_file_verified"] = True
        report["passed"] = True
    except (LabError, OSError) as exc:
        failure = exc
        report["error"] = str(exc)
    finally:
        report["finished_at"] = now()
        report["probe_files_retained_under"] = str(shared.relative_to(ROOT))
        save_json(evidence_path, report)
    if failure:
        raise LabError(str(failure))
    print(f"All three base-image probes passed; report: {evidence_path}")
    return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    p = sub.add_parser("doctor", help="Check the host runtime without pulling or starting images")
    p.add_argument("--output", type=Path)
    p.set_defaults(func=doctor)
    p = sub.add_parser("lock", help="Resolve all three sources to immutable linux/amd64 digests")
    p.add_argument("--refresh", action="store_true")
    p.set_defaults(func=lock_images)
    p = sub.add_parser("pull", help="Sequentially pull the locked images into rootless Podman")
    p.add_argument("--output", type=Path)
    p.set_defaults(func=pull_images)
    p = sub.add_parser("smoke", help="Verify identity, native package inventories and shared files")
    p.add_argument("--output", type=Path)
    p.set_defaults(func=smoke)
    args = parser.parse_args()
    try:
        return args.func(args)
    except (LabError, OSError, ValueError) as exc:
        print(f"lab: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
