"""Audit saved acceptance evidence and current source; does not rerun fixtures."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(name):
    return json.loads((HERE / name).read_text())


def main():
    checks = {}

    def check(name, value):
        checks[name] = bool(value)
        assert checks[name], name

    accepted = load("acceptance-result.json")
    gate = load("primary-final2-acceptance.json")
    source = {str(p.relative_to(ROOT)): digest(p)
              for p in (ROOT / "src/multilinux").glob("*.py")}
    check("accepted_and_final_gate", accepted["passed"] and gate["passed"]
          and gate["source_unchanged"] and accepted["source_sha256"] == gate["source_sha256"])
    check("current_source_matches_accepted", source == gate["source_sha256"] and len(source) == 32)
    check("eleven_real_steps_successful", len(gate["steps"]) == 11
          and all(v["exit"] == 0 for v in gate["steps"].values()))
    check("regression_359_with_four_skips", "Ran 359 tests" in (HERE / "primary-final2-C-regression.txt").read_text()
          and "OK (skipped=4)" in (HERE / "primary-final2-C-regression.txt").read_text()
          and accepted["regression"] == {"total": 359, "passed": 355, "skipped": 4})
    for name in ["C-gate", "C-D-copy", "D-controlled", "D-windows", "D-legacy", "D-ENOSPC",
                 "D-CLI", "D-legacy-CLI", "E-sessions", "E-desktop", "E-windows"]:
        item = load("primary-final2-" + name + ".json")
        check(name + "_passed", item["passed"])
        # The ENOSPC report uses this key for the copied payload digest. The
        # enclosing gate records unchanged implementation hashes for that run.
        if name != "D-ENOSPC":
            check(name + "_source_matches", item["source_sha256"] == source)
    for name, count in [("D-controlled", 35), ("D-windows", 7), ("D-legacy", 20), ("E-windows", 4)]:
        cases = load("primary-final2-" + name + ".json")["cases"]
        values = cases.values() if isinstance(cases, dict) else cases
        check(name + "_case_count_and_results", len(cases) == count and all(v["passed"] for v in values))
    prior = json.loads((ROOT / "evidence/unifier-v2-stage-d-complete-2026-10-25/acceptance-result.json").read_text())
    check("D_core_source_frozen", all(source["src/multilinux/" + n] == prior["source_sha256"]["src/multilinux/" + n]
          for n in ["file_operations.py", "lease.py", "registry.py"]))
    enospc = load("primary-final2-D-ENOSPC.json")
    check("real_ENOSPC_and_nonzero_CLI", enospc["cli_exit_after_real_enospc"] == 1
          and enospc["source_retained"] and enospc["no_published_target"] and enospc["unmounted"])
    sessions = load("primary-final2-E-sessions.json")
    check("three_native_targets", set(sessions["targets"]) == {"debian13", "fedora44", "arch"}
          and all(v["passed"] for v in sessions["targets"].values()))
    check("six_session_lifecycle_cases", len(sessions["cases"]) == 6
          and all(v["passed"] for v in sessions["cases"].values()))
    desktop = load("primary-final2-E-desktop.json")
    check("native_GIO_authority_and_revoke", len(desktop["steps"]) == 17 and all(desktop[k] for k in [
          "default_changed_to_exported_entry", "real_GIO_launch_argv_preserved", "recreated_app_refused_by_old_entry",
          "original_default_and_exact_bytes_restored", "unrelated_entries_preserved", "recovery_twice",
          "revoked_cached_launcher_refused_without_start", "later_external_choice_preserved_on_conflict"]))
    upgrade = load("daily-source-upgrade.json")
    check("daily_source_upgrade_and_preservation", upgrade["passed"] and upgrade["source_sha256"] == source
          and all(upgrade[k] for k in ["state_preserved", "config_preserved", "desktop_preserved"]))
    check("daily_code_backup_intact", digest(Path(upgrade["backup"])) == upgrade["backup_sha256"])
    for label, directory in [("native_fixture", HERE / "native-project"), ("daily", Path(upgrade["app_root"]))]:
        check(label + "_source_matches", all(digest(directory / name) == value for name, value in source.items()))
    menu = load("freecad-final/menu-validation.json")
    check("two_native_menu_launches_and_autostops", menu["passed"] and menu["desktop_discoverable"]
          and menu["final_state"] == "stopped" and len(menu["sessions"]) == 2
          and all(s["menu_exit"] == 0 and s["stopped_after_close"] and s["same_container"]
                  and not s["leases_after"]["leases"] for s in menu["sessions"]))
    check("primary_visual_inspection", load("visual-qa.json")["freecad_main_window_observed"])
    for name in ["session-fixture-cleanup.json", "projection-cleanup.json"]:
        item = load(name)
        check(name + "_three_removed", item["passed"] and len(item["removed"]) == 3)
    check("normal_session_retirement_not_bypassed", all(v["normal_retirement_guard_passed"]
          for v in load("session-fixture-cleanup.json")["removed"]))
    post = load("postconditions.json")
    check("preservation_postconditions", post["passed"] and post["source_unchanged"]
          and post["original_container_count"] == post["container_count_now"] == 27
          and post["main_state_actual_count"] == post["main_state_count"] == 23
          and post["historical_evidence_count"] == 13572
          and all(not post[k] for k in ["original_container_errors", "main_state_errors", "historical_errors",
                 "desktop_errors", "main_state_extra_files", "original_container_name_changes",
                 "storage_mounts", "unifier_fuse_mounts"]))
    for stage in ["b-2026-10-24", "c-2026-10-24", "d-2026-10-24", "d-complete-2026-10-25"]:
        manifest = ROOT / "evidence" / ("unifier-v2-stage-" + stage) / "SHA256SUMS"
        result = subprocess.run(["sha256sum", "--quiet", "-c", str(manifest)], cwd=ROOT, capture_output=True)
        check("earlier_manifest_" + stage, result.returncode == 0)
    result = {"passed": True, "checked_at": time.time(), "scope": "saved evidence consistency and current source audit; not additional runtime tests",
              "checks": checks, "check_count": len(checks), "source_file_count": len(source)}
    (HERE / "evidence-audit.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"passed": True, "consistency_checks": len(checks), "source_files": len(source)}))


if __name__ == "__main__":
    main()
