"""Verify final B evidence without launching containers or a GUI."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
LIVE = HERE / "live-freecad"


def read(path):
    return json.loads(Path(path).read_text())


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("choose a new output file")
    checks = {}
    regression = (HERE / "finalize-regression.txt").read_text()
    checks["236_tests_232_passed_4_conditional_skips"] = (
        "Ran 236 tests" in regression and "OK (skipped=4)" in regression and "FAILED (" not in regression)
    checks["independent_routing_boundaries"] = read(HERE / "independent-boundaries-2026-10-24.json")["all_passed"]
    three = read(HERE / "live-three-distros-regression.json")
    checks["three_native_targets_passed"] = three["passed"] and set(three["targets"]) == {"debian13", "fedora44", "arch"}
    checks["three_target_source_did_not_change"] = three["source_unchanged_during_run"] and all(
        sha(ROOT / path) == digest for path, digest in three["source_sha256"].items())
    checks["three_target_test_containers_removed"] = all(
        item["temporary_containers_removed"] for item in three["targets"].values())
    manifest = read(LIVE / "gui-manifest.json")
    checks["live_code_matches_current_and_snapshot"] = all(
        sha(ROOT / path) == sha(LIVE / "project" / path) == digest
        for path, digest in manifest["source_sha256"].items() if not path.startswith("config/"))
    checks["gui_snapshot_configuration_unchanged"] = all(
        sha(LIVE / "project" / path) == digest
        for path, digest in manifest["configuration_sha256"].items())
    checks["host_labels_preserved_on_mount_create"] = manifest["labels_before_create"] == read(LIVE / "labels-after-create.json")
    environment = read(LIVE / "environment-created.json")
    checks["preserve_host_has_no_relabel_options"] = (
        environment["runtime"]["share_label_policy"] == "preserve-host"
        and all(item["options"] == "" for item in environment["shared_mounts"]))
    acceptance = read(LIVE / "acceptance.json")
    checks["real_gui_acceptance_passed"] = acceptance["passed"]
    sessions = acceptance["sessions"]
    checks["three_gui_sessions_share_one_container"] = len({s["container_id"] for s in sessions.values()}) == 1
    checks["lease_is_in_a_different_worker"] = all(s["launcher_pid"] != s["worker_pid"] for s in sessions.values())
    checks["normal_launchers_return_zero"] = all(sessions[phase]["launcher_exit"] == 0 for phase in ("edit", "reopen-original"))
    checks["launcher_was_really_sigkilled"] = sessions["reopen-new-kill-launcher"]["launcher_exit"] == -9
    alive = read(LIVE / "gui-survives-launcher-kill.json")
    checks["gui_still_alive_after_launcher_kill"] = alive["visible"] and alive["platform"] == "wayland" and bool(alive["documents"])
    for phase in sessions:
        state = read(LIVE / (phase + "-stopped.json"))
        checks[phase + "_stopped_and_released"] = (
            state["environment"]["runtime_state"] == "stopped"
            and not state["leases"]["leases"] and state["leases"]["bridge"] is None)
        frame = read(LIVE / (phase + "-window-window.json"))
        checks[phase + "_real_native_window"] = (
            frame["before"]["internalId"] == frame["after"]["internalId"]
            and all("XdgToplevelWindow" in frame[side]["objectType"]
                    and frame[side]["bufferWidth"] > 0 and frame[side]["bufferHeight"] > 0
                    for side in ("before", "after")))
    submission = read(LIVE / "save-as-submission.json")
    checks["new_file_used_host_project_default_and_basename"] = (
        submission["initial_directory"] == str(LIVE / "host-project")
        and submission["submitted_path"] == "新建 零件.FCStd" and submission["button"] == "&Save")
    plan = read(LIVE / "file-plan.json")
    checks["generic_save_as_guarantee_stays_unknown"] = plan["save_as_default"] == "unknown" and not plan["default_save_enforced"]
    saved = read(LIVE / "saved.json")
    checks["original_actually_saved_with_different_bytes"] = saved["before_sha256"] != saved["after_sha256"]
    readback = read(LIVE / "host-readback.json")
    workflow = read(LIVE / "gui-workflow.json")
    for name, count in (("宿主原件.FCStd", 4), ("新建 零件.FCStd", 1)):
        path = LIVE / "host-project" / name
        with zipfile.ZipFile(path) as archive:
            checks[name + "_valid_native_document"] = (
                archive.testzip() is None
                and len(ET.fromstring(archive.read("Document.xml")).findall("./Objects/Object")) == count)
        checks[name + "_hash_and_owner_preserved"] = (
            sha(path) == readback[name]["sha256"] == workflow["files"][name]["sha256"]
            and path.stat().st_uid == path.stat().st_gid == 1000)
    original = read(LIVE / "reopen-original-opened.json")["documents"][0]
    checks["original_geometry_preserved"] = any(
        item["name"] == "Cut" and item["valid"] and abs(item["volume"] - 21738.05328941535) < 1e-6
        for item in original["objects"])
    package = read(LIVE / "package-remove.json")
    checks["freecad_removed_by_native_package_transaction"] = (
        package["operation"] == "remove" and package["verdict"] == "verified"
        and package["verification"]["ok"] and package["verification"]["packages"]["freecad"] is False
        and package["verification"]["command_exit_code"] != 0)
    checks["data_survives_uninstall_and_environment_removal"] = (
        all(read(LIVE / "project-survives-package-uninstall.json").values())
        and acceptance["root_survives_environment_removal"])
    inputs = read(LIVE / "input-manifest.json")
    checks["original_historical_model_and_sibling_note_unchanged"] = (
        sha(Path(inputs["source"])) == inputs["source_sha256"]
        and sha(LIVE / "host-project/旁注.txt") == inputs["note_sha256"])
    post = read(HERE / "postconditions.json")
    for key in ("original_containers_and_states_preserved", "original_state_files_preserved",
                "historical_evidence_preserved", "verified_code_matches_current_and_snapshot", "no_test_windows"):
        checks[key] = post[key]
    checks["host_packages_and_desktop_entries_preserved"] = post["host_snapshot_before"] == post["host_snapshot_after"]
    report = {"all_passed": all(checks.values()), "checks": checks,
              "scope": "bounded B routing, one Arch FreeCAD GUI workflow, and three native identity regressions; no C-F acceptance"}
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"all_passed": report["all_passed"], "checks": len(checks),
                      "failed": [key for key, value in checks.items() if not value]}))
    return 0 if report["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
