"""Formal install of host-built fixtures on the fixed Debian CID."""
import json, os, threading, time, traceback
from pathlib import Path
import sys
ROOT = Path("/home/omg/文档/多兼容linux机")
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from tools.reuse_test_guard import make_service, get, info

spec = Path(os.environ["UNIFIER_TEST_SPEC"])
config = get(spec)
out = spec.parent
debs = Path("/home/omg/文档/多兼容linux机/artifacts/expansion-resume-20261107T222500+0800/debs")
share = Path(config["shared"]) / "dep-1107"
share.mkdir(mode=0o700, exist_ok=True)
service = make_service(spec)
result = {"status": "RUNNING", "cases": [], "cid": config["cid"]}

def save():
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))

def record(case, status, **evidence):
    result["cases"].append({"id": case, "status": status, "evidence": evidence})
    save()

def guest(argv, *, user="0", timeout=120, accepted=(0,)):
    value = service.runtime.exec(
        config["cid"], list(argv), project=config["project"], env_id=config["env_id"],
        workdir="/", env={"LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"},
        capture=True, timeout=timeout, user=user)
    if value.returncode not in accepted:
        raise RuntimeError(f"{argv[:3]} -> {value.returncode}: {(value.stderr or value.stdout)[-1500:]}")
    return value

def installed(name):
    value = guest(["dpkg-query", "-W", "-f", "${db:Status-Abbrev}", name], accepted=(0, 1))
    return value.returncode == 0 and value.stdout.strip() == "ii"

def version_of(name):
    value = guest(["dpkg-query", "-W", "-f", "${Version}", name], accepted=(0, 1))
    return value.stdout.strip() if value.returncode == 0 else ""

def cid_now():
    current = info(config)
    if current["Id"] != config["cid"] or not current["State"]["Running"]:
        raise RuntimeError("fixed container is not running")
    return current["Id"]

def summarize(txn):
    repair = txn.get("dependency_repair") or {}
    return {
        "id": txn.get("id"), "phase": txn.get("phase"), "failure_phase": txn.get("failure_phase"),
        "native_error": txn.get("native_error"), "verification": txn.get("verification"),
        "repair_status": repair.get("status"), "repair_installed": repair.get("installed"),
        "repair_unresolved": repair.get("unresolved"),
        "started_at": txn.get("started_at"), "finished_at": txn.get("finished_at"),
    }

def require_txn(txn):
    if txn.get("phase") != "completed" or not (txn.get("verification") or {}).get("ok"):
        raise RuntimeError(json.dumps(summarize(txn), ensure_ascii=False)[-2000:])
    return txn

def run_as_user(argv):
    value = guest(argv, user="1000", timeout=30)
    who = guest(["id", "-u"], user="1000", timeout=15)
    if who.stdout.strip() != "1000":
        raise RuntimeError("oracle uid: " + who.stdout)
    return value.stdout

def install_deb(package, **kwargs):
    return service.install_local(config["env_id"], str(debs / f"{package}.deb"), package_name=package, **kwargs)

try:
    cid_before = config["cid"]
    service.start(config["env_id"])
    audit = guest(["dpkg", "--audit"])
    if audit.stdout.strip():
        guest(["dpkg", "--purge", "unifier-round1107c"], accepted=(0, 1))
    if not (installed("unifier-round1107a") and installed("libestr0")):
        txn = require_txn(install_deb("unifier-round1107a", use_argv=["/usr/bin/unifier-round1107a"]))
        record("DEP01", "PASS", transaction=summarize(txn))
    oracle = run_as_user(["/usr/bin/unifier-round1107a"])
    if "ok\n" not in oracle or "libestr.so.0" not in oracle or not installed("libestr0") or cid_now() != cid_before:
        raise RuntimeError("DEP01 oracle: " + oracle[-500:])
    if not result["cases"] or result["cases"][-1]["id"] != "DEP01":
        record("DEP01", "PASS", recheck=oracle[-200:], version=version_of("libestr0"))

    if not (installed("unifier-round1107b") and installed("libfastjson4")):
        txn = require_txn(install_deb("unifier-round1107b"))
        failed = guest(["/usr/bin/unifier-round1107b"], user="1000", accepted=(0, 1, 127), timeout=20)
        if failed.returncode == 0 or installed("libfastjson4"):
            raise RuntimeError("DEP02 library was already usable")
        repaired = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107b"]))
        repair = repaired.get("dependency_repair") or {}
        if repair.get("status") != "repaired" or "libfastjson4" not in (repair.get("installed") or []) or repair.get("unresolved"):
            raise RuntimeError("DEP02 repair: " + json.dumps(summarize(repaired))[-1500:])
        record("DEP02", "PASS", install=summarize(txn), repair=summarize(repaired), pre_exit=failed.returncode)
    oracle = run_as_user(["/usr/bin/unifier-round1107b"])
    if "ok\n" not in oracle or "libfastjson.so.4" not in oracle:
        raise RuntimeError("DEP02 oracle: " + oracle[-500:])
    if not result["cases"] or result["cases"][-1]["id"] != "DEP02":
        record("DEP02", "PASS", recheck=oracle[-200:])

    txn = require_txn(install_deb("unifier-round1107c"))
    failed = guest(["/usr/bin/unifier-round1107c"], user="1000", accepted=(0, 1, 127), timeout=20)
    if failed.returncode == 0 or installed("libconfig11"):
        raise RuntimeError("DEP03 dependency was already present")
    repaired = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107c"]))
    repair = repaired.get("dependency_repair") or {}
    objects = [item.get("path") for item in repair.get("elf_objects") or []]
    if not any(str(path).endswith("/plugin.so") for path in objects):
        raise RuntimeError("DEP03 did not scan the plugin: " + ",".join(objects))
    if "libconfig11" not in (repair.get("installed") or []):
        raise RuntimeError("DEP03 provider: " + json.dumps(summarize(repaired))[-1500:])
    oracle = run_as_user(["/usr/bin/unifier-round1107c"])
    if oracle.strip() != "ok 1107":
        raise RuntimeError("DEP03 oracle: " + oracle)
    record("DEP03", "PASS", repair=summarize(repaired), scanned=objects, oracle=oracle.strip())

    before = version_of("libestr0")
    txn = require_txn(install_deb("unifier-round1107e", use_argv=["/usr/bin/unifier-round1107e"]))
    native = "\n".join(step.get("stdout_tail", "") for step in txn.get("native") or [])
    if version_of("libestr0") != before or "Unpacking libestr0" in native or cid_now() != cid_before:
        raise RuntimeError("DEP04 replaced the provider or the container")
    record("DEP04", "PASS", version=before, transaction=summarize(txn),
           load_a=run_as_user(["/usr/bin/unifier-round1107a"])[-250:],
           load_e=run_as_user(["/usr/bin/unifier-round1107e"])[-250:])

    quiet = []
    for _ in range(2):
        item = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107a", "unifier-round1107e"]))
        repair = item.get("dependency_repair") or {}
        if repair.get("installed") or repair.get("status") != "no-missing-elf-dependencies":
            raise RuntimeError("DEP05: " + json.dumps(summarize(item))[-1500:])
        quiet.append(summarize(item))
    record("DEP05", "PASS", repairs=quiet)

    removed = require_txn(service.remove_packages(config["env_id"], ["unifier-round1107a"], use_argv=["/usr/bin/unifier-round1107a"]))
    if installed("unifier-round1107a") or not installed("unifier-round1107e") or not installed("libestr0"):
        raise RuntimeError("DEP06 changed the survivor or the shared library")
    record("DEP06", "PASS", remove=summarize(removed), survivor=run_as_user(["/usr/bin/unifier-round1107e"])[-250:],
           version=version_of("libestr0"))

    txn = require_txn(install_deb("unifier-round1107h"))
    static = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107h"]))
    if (static.get("dependency_repair") or {}).get("installed"):
        raise RuntimeError("DEP08 static repair installed a runtime-only library")
    failed = guest(["/usr/bin/unifier-round1107h"], user="1000", accepted=(0, 1, 127), timeout=20)
    if failed.returncode == 0 or installed("libcapstone5"):
        raise RuntimeError("DEP08 library was already available")
    hinted = require_txn(service.repair_dependencies(config["env_id"], [], libraries=["libcapstone.so.5"]))
    if "libcapstone5" not in ((hinted.get("dependency_repair") or {}).get("installed") or []):
        raise RuntimeError("DEP08 hint: " + json.dumps(summarize(hinted))[-1500:])
    oracle = run_as_user(["/usr/bin/unifier-round1107h"])
    if not oracle.startswith("ok "):
        raise RuntimeError("DEP08 oracle: " + oracle)
    record("DEP08", "PASS", static=summarize(static), hinted=summarize(hinted), pre_exit=failed.returncode, oracle=oracle.strip())

    beat = share / "heartbeat.txt"
    beat.unlink(missing_ok=True)
    payload = "import time\nfrom pathlib import Path\np=Path(" + repr(str(beat)) + ")\nn=0\nwhile True:\n n+=1\n p.write_text(str(n))\n time.sleep(0.2)\n"
    log = (out / "heartbeat.log").open("wb")
    child = service.runtime.popen(["podman", "exec", "--user", "1000", config["cid"], "/usr/bin/python3", "-c", payload], stdout=log, stderr=log)
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not beat.exists():
            time.sleep(0.1)
        if not beat.exists():
            raise RuntimeError("heartbeat did not start")
        first_count = int(beat.read_text() or "0")
        found = {}
        def install(name):
            try:
                found[name] = install_deb(name)
            except Exception as exc:
                found[name] = exc
        threads = [threading.Thread(target=install, args=(name,)) for name in ("unifier-round1107p", "unifier-round1107q")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=900)
        txns = []
        for name in ("unifier-round1107p", "unifier-round1107q"):
            value = found.get(name)
            if isinstance(value, Exception) or value is None:
                raise RuntimeError(f"SH09 {name}: {value}")
            require_txn(value)
            txns.append(value)
        txns.sort(key=lambda item: item.get("started_at") or "")
        if txns[0].get("finished_at") > txns[1].get("started_at"):
            raise RuntimeError("package transactions overlapped")
        later = int(beat.read_text() or "0")
        if later <= first_count or cid_now() != cid_before:
            raise RuntimeError("heartbeat stopped or CID changed")
        record("SH09", "PASS", first=summarize(txns[0]), second=summarize(txns[1]), heartbeat_before=first_count, heartbeat_after=later)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except Exception:
                child.kill()
        log.close()

    gio = guest(["/bin/sh", "-c", "command -v gio || true"])
    record("DEP11", "FEATURE_GAP", gio=gio.stdout.strip(), note="ELF repair does not install missing external commands")
    record("DEP07", "BLOCKED", reason="no signed private fixture repository")
    record("DEP13", "BLOCKED", reason="an interrupted apt database on the only fixed Debian CID is not a safe recover fixture")
    record("SH10", "BLOCKED", reason="private Writer workflow was not started on the active desktop")
    record("AR01", "BLOCKED", reason="fixed Arch CID has no admitted RAM/CPU quota")
    record("AR02", "BLOCKED", reason="fixed Arch CID has no admitted RAM/CPU quota")
    record("AR03", "BLOCKED", reason="fixed Arch CID has no admitted RAM/CPU quota")
    result["status"] = "PASS"
    save()
except Exception as exc:
    result.update(status="FAIL", error=str(exc), traceback=traceback.format_exc())
    save()
    raise
finally:
    try:
        if info(config)["State"]["Running"]:
            service.stop(config["env_id"])
    except Exception as exc:
        result["stop_error"] = str(exc)
        save()
print(json.dumps([item["id"] + ":" + item["status"] for item in result["cases"]]))
