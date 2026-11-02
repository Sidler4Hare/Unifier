#!/usr/bin/env python3
"""Audit saved acceptance artifacts; --live-checks also inspects this host.

This checks evidence consistency and code identity, not new product acceptance.
It never starts environments, mounts views, or replays destructive probes.
"""
import argparse
import hashlib
import json
import re
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())


def read_json(path):
    return json.loads(path.read_text())


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live-checks", action="store_true")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("use a new output path; existing evidence is preserved")
    checks = {}

    def check(name, condition):
        checks[name] = bool(condition)
        if not condition:
            raise RuntimeError(f"evidence check failed: {name}")

    def hashes_match(base, expected):
        return bool(expected) and all(
            (base / name).is_file() and sha256(base / name) == value
            for name, value in expected.items()
        )

    def manifest_count(path):
        lines = path.read_text().splitlines()
        for line in lines:
            expected, relative = line.split("  ", 1)
            target = ROOT / relative
            check(f"manifest:{relative}", target.is_file() and sha256(target) == expected)
        return len(lines)

    gate = read_json(HERE / "acceptance-result.json")
    source = gate["source_sha256"]
    check("final combination gate", gate["passed"] and gate["source_unchanged"])
    check("current source matches acceptance", hashes_match(ROOT, source))
    check("no untested added source", set(source) == {
        str(p.relative_to(ROOT)) for p in (ROOT / "src/multilinux").rglob("*.py")
    })
    check("all gate subprocesses succeeded", all(s["exit"] == 0 for s in gate["steps"].values()))

    for filename in ["native-final.json", "view-copy-final.json", "cli-final.json",
                     "c-regression-gate.json", "c-regression-matrix.json",
                     "c-regression-lifecycle.json"]:
        result = read_json(HERE / filename)
        check(f"accepted report:{filename}", result["passed"] and result["source_sha256"] == source)
        if "source_unchanged" in result:
            check(f"stable source:{filename}", result["source_unchanged"])

    regression = (HERE / "c-regression-regression.txt").read_text()
    check("regression total 293", bool(re.search(r"Ran 293 tests", regression)))
    check("regression 4 conditional skips", "OK (skipped=4)" in regression)
    native = read_json(HERE / "native-final.json")
    check("20 accepted native cases", len(native["cases"]) == 20 and all(
        c["passed"] and c["source_retained"] for c in native["cases"].values()))
    check("real different filesystems", all(
        c["source_dev"] != c["target_dev"]
        for c in native["cases"].values() if "source_dev" in c))
    check("move remains incomplete with source retained",
          native["cases"]["move-retains-source"]["final_state"] == "SOURCE_CLEANUP_PENDING")
    projected = read_json(HERE / "view-copy-final.json")["targets"]
    check("3 stopped projection sources", set(projected) == {"debian13", "fedora44", "arch"} and all(
        all(c[k] for k in ["passed", "environment_stayed_stopped", "copied_bytes_match",
                            "source_fds_protect_busy_view", "revoked_view_not_empty_fallback"])
        for c in projected.values()))
    enospc = read_json(HERE / "enospc-final2.json")
    # This probe's source_sha256 is the input payload, not the Python code map.
    # The combination gate recorded stable code before and after running it.
    check("real ENOSPC retains source and stage", enospc["passed"]
          and json.loads(gate["steps"]["enospc"]["stdout"])["actual_enospc"]
          and bool(re.fullmatch(r"[0-9a-f]{64}", enospc["source_sha256"]))
          and all(enospc[k] for k in [
        "source_retained", "partial_stage_retained", "no_published_target", "no_registration", "unmounted"])
          and enospc["operation"]["state"] == "NEEDS_RECONCILIATION"
          and 0 < enospc["partial_stage_bytes"] <= enospc["limit_bytes"]
          and enospc["cli_exit_after_real_enospc"] == 1)
    cli = read_json(HERE / "cli-final.json")
    check("9 CLI commands and truthful completion exits", len(cli["commands"]) == 9
          and cli["completed_copy_exit"] == 0 and cli["incomplete_move_exit"] == 1
          and cli["source_retained"] and cli["existing_target_refused"]
          and cli["whitespace_quotes_semicolon_preserved"])
    check("CLI snapshot matches source", hashes_match(Path(cli["project"]), source))
    missing = read_json(HERE / "c-regression-missing-backend.json")
    check("missing backend refused and cleaned", all(missing[k] for k in [
        "passed", "source_restored", "no_storage_leak", "environment_stopped"]))

    deployed = read_json(HERE / "daily-source-upgrade.json")
    check("daily FreeCAD deployed accepted source", deployed["passed"]
          and deployed["source_sha256"] == source
          and hashes_match(Path(deployed["app_root"]), source)
          and all(deployed[k] for k in ["state_preserved", "config_preserved", "desktop_preserved"]))
    check("daily previous code backup retained", sha256(Path(deployed["backup"])) == deployed["backup_sha256"])
    menu = read_json(HERE / "freecad-final/menu-validation.json")
    check("two native menu launches then stopped", menu["passed"] and len(menu["sessions"]) == 2
          and menu["final_state"] == "stopped" and menu["native_menu_entry_retained"]
          and menu["persistent_container_retained"] and menu["desktop_discoverable"]
          and menu["no_test_macro_in_application"] and Path(menu["desktop"]).is_file()
          and all(s["menu_exit"] == 0 and s["same_container"] and s["stopped_after_close"]
                  and not s["leases_after"]["leases"] for s in menu["sessions"]))
    check("GUI screenshots retained", all(Path(s["screenshot"]["path"]).is_file()
                                          for s in menu["sessions"]))
    check("GUI inspection recorded", read_json(HERE / "visual-qa.json")["freecad_main_window_observed"])
    cleanup = read_json(HERE / "fixture-cleanup.json")
    check("3 exact fixtures cleaned", cleanup["passed"] and len(cleanup["removed"]) == 3
          and cleanup["view_registry_empty"])
    post = read_json(HERE / "postconditions.json")
    check("postconditions accepted", post["passed"] and post["original_container_count"] == 26
          and len(post["containers_after"]) == 27 and post["historical_file_count"] == 12670
          and all(not post[k] for k in ["original_container_deltas", "main_state_errors",
                                       "historical_errors", "storage_mounts", "fuse_mounts"])
          and post["stage_b_manifest_valid"] and post["stage_c_manifest_valid"])
    manifest_counts = {stage: manifest_count(ROOT / "evidence" / folder / "SHA256SUMS")
                       for stage, folder in [("B", "unifier-v2-stage-b-2026-10-24"),
                                             ("C", "unifier-v2-stage-c-2026-10-24")]}

    if args.live_checks:
        def command_json(argv):
            return json.loads(subprocess.check_output(argv, cwd=ROOT, text=True, timeout=30))

        inventory = command_json(["podman", "ps", "-a", "--no-trunc", "--format=json"])
        check("live containers unchanged since accepted cleanup",
              {c["Id"]: c["State"] for c in inventory} == {
                  c["id"]: c["state"] for c in post["containers_after"]})
        check("live storage mounts empty", not command_json(
            ["podman", "unshare", "podman", "mount", "--format=json"]))
        fuse = [line for line in Path("/proc/self/mountinfo").read_text().splitlines()
                if line.split(" - ", 1)[1].split()[0] == "fuse.unifier"
                or line.split(" - ", 1)[1].split()[1].startswith("unifier-view-")]
        check("live Unifier FUSE mounts empty", not fuse)
        for label, filename, total in [
            ("main state", ".lab/agents/unifier-v2-stage-c-20261024/state-before.json", 23),
            ("historical evidence", ".lab/agents/unifier-v2-stage-a-20261023/historical-evidence-before.json", 12670),
        ]:
            baseline = read_json(ROOT / filename)
            check(f"live {label} preserved", len(baseline) == total and hashes_match(ROOT, baseline))

    result = {"passed": True, "checked_at": time.time(), "live_checks": args.live_checks,
              "purpose": "evidence consistency audit; does not replay product tests",
              "checks_passed": len(checks), "source_file_count": len(source),
              "earlier_manifest_files": manifest_counts, "checks": checks}
    with args.output.open("x") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({k: v for k, v in result.items() if k != "checks"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
