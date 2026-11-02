"""Real-host refusal and concurrent-create acceptance, preserving daily containers."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from tools.probe_headless import inventory
from unifier.install import load_catalog
from unifier.runtime import PodmanRuntime, container_name, project_label
from unifier.service import EnvironmentService

before = inventory()
result = {"before": before, "refusals": []}
base_env = {**os.environ, "LC_ALL": "C", "LANG": "C", "PYTHONDONTWRITEBYTECODE": "1"}
with tempfile.TemporaryDirectory(prefix="unifier-authority-live-") as temporary:
    home = Path(temporary)
    commands = [[str(ROOT / "bin/unifier")], [sys.executable, str(ROOT / "tools/ml.py")]]
    commands += [[sys.executable, str(Path.home() / f".local/share/multilinux/apps/{app}/tools/ml.py")]
                 for app in ("freecad", "wemeet")]
    for index, command in enumerate(commands):
        state_root = home / f"refuse-{index}"
        completed = subprocess.run(command + ["env", "create", "--target", "fedora44", "--id", "must-refuse"],
                                   env={**base_env, "UNIFIER_HOME": str(state_root)},
                                   text=True, capture_output=True)
        assert completed.returncode == 1 and "same-family containers are forbidden" in completed.stderr
        assert not state_root.exists()
        result["refusals"].append({"entry": command[-1], "exit_code": completed.returncode,
                                    "error": completed.stderr.strip(), "isolated_root_absent": True})

    # Two independent CLI processes request different aliases for one target.
    # The root is new and only this probe's containers are eligible for cleanup.
    work = home / "race"
    config, lock = load_catalog()
    runtime = PodmanRuntime()
    service = EnvironmentService(work, runtime, config, lock)
    processes = []
    try:
        for name in ("alpha", "beta"):
            command = [str(ROOT / "bin/unifier"), "env", "create", "--target", "debian13", "--id", name]
            process = subprocess.Popen(command, env={**base_env, "UNIFIER_HOME": str(work)},
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            processes.append((name, process))
        outputs = []
        for name, process in processes:
            stdout, stderr = process.communicate(timeout=60)
            outputs.append({"alias": name, "exit_code": process.returncode,
                            "stdout": stdout, "stderr": stderr})
        assert sorted(item["exit_code"] for item in outputs) == [0, 1]
        rejected = next(item for item in outputs if item["exit_code"] == 1)
        assert "only one environment per target" in rejected["stderr"]
        records = service.store.all()
        assert len(records) == 1 and records[0]["target"] == "debian13"
        result["concurrent_create"] = {"processes": outputs, "record_count": len(records),
                                         "container_id": records[0]["runtime"]["container_id"]}
    finally:
        for name, process in processes:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=10)
        for name in ("alpha", "beta"):
            if service.store.exists(name):
                service.remove(name)
            else:
                info = runtime.inspect(container_name(work, name))
                if info is not None:
                    assert info.project == project_label(work) and info.env_id == name
                    runtime.remove(info.name, project=info.project, env_id=name)

# Read-only validation of the exact deployed app bindings, using their own code.
result["daily_app_preflight"] = []
(OUT / "live-result.partial.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
for app, launch_id in (("freecad", "freecad"), ("wemeet", "wemeet")):
    deployment = Path.home() / ".local/share/multilinux/apps" / app
    code = """
import json,sys
from pathlib import Path
root=Path(sys.argv[1]);sys.path[:0]=[str(root/'src'),str(root)]
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService
from multilinux.compatibility import POLICY_VERSION
config,lock,_=load_project()
service=EnvironmentService(root,PodmanRuntime(),config,lock)
launch=service.store.load_launch(sys.argv[2])
binding=service.validate_app_binding(sys.argv[2])
view=service.inspect(launch['environment_id'])
assert POLICY_VERSION==2 and view['compatibility']['allowed']
pin={key:binding[key] for key in ('environment_id','environment_uuid','generation','container_id','app_instance_uuid')}
print(json.dumps({'application':sys.argv[2],'binding':pin,'policy_version':POLICY_VERSION,
                  'runtime_state':view['runtime_state'],'host_relationship':view['compatibility']['relationship']}))
"""
    completed = subprocess.run([sys.executable, "-c", code, str(deployment), launch_id],
                               env=base_env, text=True, capture_output=True)
    if completed.returncode:
        raise RuntimeError(completed.stderr)
    result["daily_app_preflight"].append(json.loads(completed.stdout))

result["after"] = inventory()
assert result["before"] == result["after"]
result["existing_containers_preserved"] = True
result["passed"] = True
(OUT / "live-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
print(json.dumps({"passed": True, "real_refusals": len(result["refusals"]),
                  "concurrent_create_records": 1, "daily_app_bindings_checked": 2}))
