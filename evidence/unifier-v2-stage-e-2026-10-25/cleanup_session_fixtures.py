"""Retire only the three exact stopped E fixtures through the accepted API."""
from pathlib import Path
import argparse
import json
import subprocess
import sys
import time
import tomllib

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from multilinux.service import EnvironmentService
from multilinux.runtime import PodmanRuntime


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    a = parser.parse_args()
    assert not a.output.exists()
    fixtures = json.loads((HERE / "fixtures.json").read_text())
    root = Path(fixtures["project"])
    service = EnvironmentService(root, PodmanRuntime(),
        tomllib.loads((root / "config/targets.toml").read_text()),
        json.loads((root / "config/images.lock.json").read_text()))
    report = {"passed": False, "removed": [], "time": time.time(),
              "method": "normal EnvironmentService.remove with live-session guard; no bypass"}
    try:
        for target, item in fixtures["targets"].items():
            expected = item["environment"]
            env_id, cid = expected["id"], expected["runtime"]["container_id"]
            current = service.store.load(env_id)
            assert current["runtime"]["container_id"] == cid
            assert current["environment_uuid"] == expected["environment_uuid"]
            assert current["generation"] == expected["generation"]
            assert service.inspect(env_id)["runtime_state"] == "stopped"
            leases = root / ".lab/leases" / (env_id + ".json")
            assert not leases.exists() or not json.loads(leases.read_text())["leases"]
            service.remove(env_id)
            assert service.runtime.inspect(expected["runtime"]["container_name"]) is None
            report["removed"].append({"target": target, "container_id": cid,
                                      "normal_retirement_guard_passed": True})
        report["passed"] = True
    finally:
        with a.output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": report["passed"], "retired_session_fixtures": len(report["removed"])}))


if __name__ == "__main__":
    main()
