import os, sys, uuid
from pathlib import Path
RUN = Path(__file__).resolve().parent
ROOT = RUN.parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from multilinux.project import load_project
from multilinux.runtime import PodmanRuntime
from multilinux.service import EnvironmentService, now
from tools.probe_desktop_apps import save, need, sha
config, lock, _ = load_project()
service = EnvironmentService(RUN / "user-project", PodmanRuntime(), config, lock)
token = uuid.uuid4().hex[:8]
env_id, app_id = "uid-contract-" + token, "uid-contract-" + token
docs = RUN / "documents" / env_id
docs.mkdir()
result = {"started_at": now(), "driver_sha256": sha(__file__), "environment_id": env_id, "passed": False}
try:
    record = service.create("debian13", env_id, [str(docs)], desktop=True, user_namespace="keep-id")
    service.start(env_id)
    record = service.store.load(env_id)
    result["package_identity"] = service._exec_record(record, ["id"], timeout=20, native=False).__dict__
    result["passwd_at_create"] = service._exec_record(record, ["getent", "passwd", str(os.getuid())], timeout=20, native=False).__dict__
    service.add_app(env_id, app_id, "/usr/bin/id", ["/usr/bin/id"], workdir=str(docs), as_host_user=True)
    result["launch"] = service.show_app(app_id)
    result["application_identity"] = service.exec_app(app_id, ["id"], capture=True, timeout=20).__dict__
    result["home"] = service.exec_app(app_id, ["printenv", "HOME"], capture=True, timeout=20).__dict__
    result["xauthority_readable"] = service.exec_app(app_id, ["test", "-r", "/tmp/ml-xauthority"], capture=True, timeout=20).returncode == 0
    document = docs / "ordinary-user.txt"
    write = service.exec_app(app_id, ["touch", str(document)], capture=True, timeout=20)
    need(write.returncode == 0, "ordinary user cannot write shared document")
    result["host_file_uid"] = document.stat().st_uid
    result["host_file_gid"] = document.stat().st_gid
    need("uid=0(root)" in result["package_identity"]["stdout"], "package command lost root")
    need(f"uid={os.getuid()}(" in result["application_identity"]["stdout"], "application UID mismatch")
    need(result["home"]["stdout"].strip() == f"/home/multilinux-{os.getuid()}", "application HOME mismatch")
    need(result["passwd_at_create"]["stdout"].strip().split(":")[5] == result["home"]["stdout"].strip(), "passwd home mismatch")
    need(result["host_file_uid"] == os.getuid() and result["host_file_gid"] == os.getgid(), "shared document ownership mismatch")
    need(result["xauthority_readable"], "ordinary application user cannot read X authorization")
    result["passed"] = True
finally:
    if service._launch_exists(app_id):
        service.remove_app(app_id)
    if service.store.exists(env_id):
        service.remove(env_id)
    result["test_environment_removed"] = not service.store.exists(env_id)
    result["finished_at"] = now()
    save(RUN / "keep-id-live-contract.json", result)
print(result["passed"], result["test_environment_removed"])
