#!/usr/bin/env python3
"""Verify this review and recompute historical benchmark summaries, offline."""
import hashlib
import json
from pathlib import Path
import re
import statistics
import sys

ROOT = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read(path):
    return json.loads(path.read_text())


def main(output):
    baseline = read(HERE / "baseline.json")
    current = {str(p.relative_to(ROOT)): sha(p)
               for directory in ("src", "tests", "config", "tools")
               for p in (ROOT / directory).rglob("*")
               if p.is_file() and "__pycache__" not in p.parts}
    checks = {"production_sources_tests_config_tools_unchanged": current == baseline["files"]}
    testlog = (HERE / "unittest.txt").read_text()
    count = re.search(r"Ran (\d+) tests", testlog)
    skipped = re.search(r"OK \(skipped=(\d+)\)", testlog)
    checks["regression_log_success"] = bool(count and skipped and "FAILED (" not in testlog)
    fresh = read(HERE / "result.json")
    checks["new_probe_source_matches"] = fresh["source_sha256"] == sha(HERE / "probe.py")
    checks["new_probe_checks"] = fresh["all_mechanism_checks_passed"] and all(fresh["checks"].values())
    checks["probe_not_product_acceptance"] = fresh["product_acceptance"] is False
    historical_dir = ROOT / "evidence/file-layer-feasibility-2026-10-23"
    measured = read(historical_dir / "result.json")
    summary = read(historical_dir / "summary.json")
    checks["historical_benchmark_source_matches"] = measured["source_sha256"] == sha(historical_dir / "probe.py")
    checks["historical_28_waves"] = len(measured["waves"]) == 28
    expected = measured["dataset"]["expected"]
    checks["historical_workload_hashes_and_saves"] = all(
        w["measurement"]["read_2000x4k"]["value"] == expected["small_sha256"] and
        w["measurement"]["sequential_128m_sha256"]["value"]["sha256"] == expected["large_sha256"] and
        w["measurement"]["save_20x4k_fsync_replace"]["value"] == 20
        for w in measured["waves"])
    recomputed = {}
    for mode, metrics in summary["performance"].items():
        recomputed[mode] = {}
        for metric, saved in metrics.items():
            values = [w["measurement"][metric]["wall_ms"] for w in measured["waves"] if w["mode"] == mode]
            median = statistics.median(values)
            recomputed[mode][metric] = median
            checks[f"historical_summary_{mode}_{metric}"] = len(values) == 7 and abs(median - saved["p50_ms"]) < 1e-6
    previous = read(ROOT / "evidence/architecture-context-2026-10-23/result.json")
    previous_hashes = previous["source_sha256"]
    delta = {
        "baseline_time": previous["time"],
        "changed": [p for p, h in previous_hashes.items() if current.get(p) != h],
        "added_src": [p for p in current if p.startswith("src/") and p not in previous_hashes],
        "attribution": "content comparison only; does not authenticate an editor/model"}
    artifacts = {}
    for directory in ("architecture-context-2026-10-23", "file-layer-feasibility-2026-10-23",
                      "env-view-2026-10-23", "cross-save-2026-10-23"):
        for p in (ROOT / "evidence" / directory).glob("*"):
            if p.is_file() and p.suffix in (".json", ".py", ".md"):
                artifacts[str(p.relative_to(ROOT))] = sha(p)
    result = {"checks": checks, "all_passed": all(checks.values()),
              "regression": {"run": int(count[1]) if count else None,
                             "skipped": int(skipped[1]) if skipped else None},
              "historical_medians_recomputed": recomputed,
              "source_delta_since_morning": delta, "reviewed_artifact_sha256": artifacts,
              "limits": ["No old GUI/container experiment rerun.",
                         "Some old probes have no historical source hash or used an overwritten script version.",
                         "New mechanism checks do not validate production integration."]}
    with output.open("x") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(json.dumps({"all_passed": result["all_passed"], "regression": result["regression"],
                      "delta": delta}, ensure_ascii=False, indent=2))
    return 0 if result["all_passed"] else 1


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: verify.py NEW_OUTPUT.json")
    raise SystemExit(main(Path(sys.argv[1])))
