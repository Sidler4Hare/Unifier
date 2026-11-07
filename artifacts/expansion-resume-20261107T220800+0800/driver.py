"""Formal Debian package cases for the fixed CID. No new containers."""
import json, os, textwrap, traceback
from pathlib import Path
import sys
ROOT = Path("/home/omg/文档/多兼容linux机")
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from tools.reuse_test_guard import make_service, get, info

spec = Path(os.environ["UNIFIER_TEST_SPEC"])
config = get(spec)
out = spec.parent
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
        workdir=str(share), env={"LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"},
        capture=True, timeout=timeout, user=user)
    if value.returncode not in accepted:
        raise RuntimeError(f"{argv[:4]} -> {value.returncode}: {(value.stderr or value.stdout)[-1500:]}")
    return value

def installed(name):
    value = guest(["dpkg-query", "-W", "-f", "${Package}\t${db:Status-Abbrev}\n", name], accepted=(0, 1))
    return value.returncode == 0 and value.stdout.strip().endswith("ii")

def version_of(name):
    value = guest(["dpkg-query", "-W", "-f", "${Version}", name], accepted=(0, 1))
    return value.stdout.strip() if value.returncode == 0 else ""

def cid_now():
    current = info(config)
    if current["Id"] != config["cid"] or not current["State"]["Running"]:
        raise RuntimeError("fixed container is not the running target")
    return current["Id"]

def summarize(txn):
    repair = txn.get("dependency_repair") or {}
    return {
        "id": txn.get("id"), "phase": txn.get("phase"), "verdict": txn.get("verdict"),
        "failure_phase": txn.get("failure_phase"), "native_error": txn.get("native_error"),
        "verification": txn.get("verification"),
        "repair_status": repair.get("status"), "repair_installed": repair.get("installed"),
        "repair_unresolved": repair.get("unresolved"),
        "started_at": txn.get("started_at"), "finished_at": txn.get("finished_at"),
        "native_argv": [step.get("argv") for step in txn.get("native") or []],
        "native_exits": [step.get("exit_code") for step in txn.get("native") or []],
    }

def require_txn(txn, *, phase="completed"):
    if txn.get("phase") != phase or not (txn.get("verification") or {}).get("ok"):
        raise RuntimeError(json.dumps(summarize(txn), ensure_ascii=False)[-2000:])
    return txn

def run_as_user(argv):
    value = guest(argv, user="1000", timeout=30)
    who = guest(["id", "-u"], user="1000", timeout=15)
    if who.stdout.strip() != "1000":
        raise RuntimeError("oracle did not run as uid 1000: " + who.stdout)
    return value.stdout

def build_linked(package, library_package, soname_hint, depends):
    script = textwrap.dedent(f"""
        set -eu
        work=/tmp/build-{package}
        rm -rf "$work" /tmp/link-{package}
        mkdir -p /tmp/link-{package} "$work/DEBIAN" "$work/usr/bin"
        cd /tmp/link-{package}
        apt-get download {library_package}
        dpkg-deb -x ./*.deb root
        so=$(find root -type f -name '{soname_hint}' | head -n 1)
        test -n "$so"
        soname=$(readelf -d "$so" | sed -n 's/.*Library soname: \\[\\(.*\\)\\]/\\1/p')
        test -n "$soname"
        cat > "$work/usr/bin/{package}.c" <<'EOF'
        #include <stdio.h>
        #include <string.h>
        int main(void) {{
            char line[512];
            FILE *maps = fopen("/proc/self/maps", "r");
            if (!maps) return 2;
            printf("ok\\n");
            while (fgets(line, sizeof line, maps)) {{
                if (strstr(line, "{soname_hint.split('*')[0]}")) printf("%s", line);
            }}
            return 0;
        }}
        EOF
        gcc -O2 -o "$work/usr/bin/{package}" "$work/usr/bin/{package}.c" \\
            -Wl,--no-as-needed "$so" -Wl,--as-needed
        rm -f "$work/usr/bin/{package}.c"
        cat > "$work/DEBIAN/control" <<EOF
        Package: {package}
        Version: 1.0
        Architecture: amd64
        Maintainer: Unifier Test <test@example.invalid>
        Depends: {depends}
        Description: round fixture {package}
        EOF
        dpkg-deb --root-owner-group -b "$work" /tmp/{package}.deb
        cp -f /tmp/{package}.deb "{share}/{package}.deb"
        printf '%s\\n' "$soname"
    """)
    value = guest(["/bin/sh", "-c", script], timeout=180)
    return value.stdout.strip().splitlines()[-1]

try:
    cid_before = config["cid"]
    toolchain = service.install_packages(
        config["env_id"], ["gcc", "binutils", "dpkg-dev", "apt-file"])
    record("TOOLCHAIN", "PASS" if toolchain.get("phase") == "completed" else "FAIL", **summarize(toolchain))
    if toolchain.get("phase") != "completed":
        raise RuntimeError("toolchain install did not complete")
    service.start(config["env_id"])
    for name in ("libestr0", "libfastjson4", "libconfig9", "libcapstone5"):
        if installed(name):
            raise RuntimeError(name + " is already installed; fixture would not prove a missing provider")
    soname_a = build_linked("unifier-round1107a", "libestr0", "libestr.so*", "libestr0")
    absent = guest(["/usr/bin/unifier-round1107a"], user="1000", accepted=(0, 1, 127), timeout=20)
    # Binary is not installed yet; the pre-install failure is the missing file.
    record("DEP01-pre", "PASS", binary_exit=absent.returncode, libestr_installed=installed("libestr0"))
    txn = require_txn(service.install_local(
        config["env_id"], str(share / "unifier-round1107a.deb"),
        package_name="unifier-round1107a", use_argv=["/usr/bin/unifier-round1107a"]))
    oracle = run_as_user(["/usr/bin/unifier-round1107a"])
    if "ok\n" not in oracle or soname_a not in oracle or not installed("libestr0"):
        raise RuntimeError("DEP01 oracle did not load the declared dependency: " + oracle[-500:])
    if cid_now() != cid_before:
        raise RuntimeError("CID changed during DEP01")
    record("DEP01", "PASS", transaction=summarize(txn), oracle=oracle[-400:],
           libestr_version=version_of("libestr0"), soname=soname_a)

    soname_b = build_linked("unifier-round1107b", "libfastjson4", "libfastjson.so*", "libc6")
    txn = require_txn(service.install_local(
        config["env_id"], str(share / "unifier-round1107b.deb"),
        package_name="unifier-round1107b"))
    failed = guest(["/usr/bin/unifier-round1107b"], user="1000", accepted=(0, 1, 127), timeout=20)
    if failed.returncode == 0 or installed("libfastjson4"):
        raise RuntimeError("DEP02 fixture was not actually missing its library")
    repaired = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107b"]))
    repair = repaired.get("dependency_repair") or {}
    if repair.get("status") != "repaired" or "libfastjson4" not in (repair.get("installed") or []):
        raise RuntimeError("DEP02 repair did not select libfastjson4: " + json.dumps(summarize(repaired))[-1500:])
    if repair.get("unresolved"):
        raise RuntimeError("DEP02 left unresolved libraries")
    oracle = run_as_user(["/usr/bin/unifier-round1107b"])
    if "ok\n" not in oracle or soname_b not in oracle:
        raise RuntimeError("DEP02 oracle failed: " + oracle[-500:])
    record("DEP02", "PASS", install=summarize(txn), repair=summarize(repaired),
           pre_exit=failed.returncode, oracle=oracle[-400:])

    # Plugin package: main has no NEEDED on libconfig; the packaged .so does.
    plugin_script = textwrap.dedent(f"""
        set -eu
        work=/tmp/build-unifier-round1107c
        rm -rf "$work" /tmp/link-c
        mkdir -p /tmp/link-c "$work/DEBIAN" "$work/usr/bin" "$work/usr/lib/unifier-round1107c"
        cd /tmp/link-c && apt-get download libconfig9 && dpkg-deb -x ./*.deb root
        so=$(find root -type f -name 'libconfig.so*' | head -n 1)
        test -n "$so"
        cat > /tmp/plugin.c <<'EOF'
        int plugin_mark(void) {{ return 1107; }}
        EOF
        cat > /tmp/main.c <<'EOF'
        #include <dlfcn.h>
        #include <stdio.h>
        int main(void) {{
            void *handle = dlopen("/usr/lib/unifier-round1107c/plugin.so", RTLD_NOW);
            if (!handle) {{ fprintf(stderr, "%s\\n", dlerror()); return 1; }}
            int (*fn)(void) = dlsym(handle, "plugin_mark");
            if (!fn) return 2;
            printf("ok %d\\n", fn());
            return 0;
        }}
        EOF
        gcc -shared -fPIC -o "$work/usr/lib/unifier-round1107c/plugin.so" /tmp/plugin.c \\
            -Wl,--no-as-needed "$so" -Wl,--as-needed
        gcc -O2 -o "$work/usr/bin/unifier-round1107c" /tmp/main.c -ldl
        cat > "$work/DEBIAN/control" <<'EOF'
        Package: unifier-round1107c
        Version: 1.0
        Architecture: amd64
        Maintainer: Unifier Test <test@example.invalid>
        Depends: libc6
        Description: plugin fixture
        EOF
        dpkg-deb --root-owner-group -b "$work" "{share}/unifier-round1107c.deb"
    """)
    guest(["/bin/sh", "-c", plugin_script], timeout=180)
    txn = require_txn(service.install_local(
        config["env_id"], str(share / "unifier-round1107c.deb"),
        package_name="unifier-round1107c"))
    before_plugin = guest(["/usr/bin/unifier-round1107c"], user="1000", accepted=(0, 1, 127), timeout=20)
    if before_plugin.returncode == 0 or installed("libconfig9"):
        raise RuntimeError("DEP03 plugin dependency was already satisfied")
    repaired = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107c"]))
    repair = repaired.get("dependency_repair") or {}
    objects = [item.get("path") for item in repair.get("elf_objects") or []]
    if not any(path.endswith("/plugin.so") for path in objects):
        raise RuntimeError("DEP03 scan did not include the plugin: " + ",".join(objects))
    if "libconfig9" not in (repair.get("installed") or []):
        raise RuntimeError("DEP03 did not install libconfig9")
    oracle = run_as_user(["/usr/bin/unifier-round1107c"])
    if oracle.strip() != "ok 1107":
        raise RuntimeError("DEP03 oracle: " + oracle)
    record("DEP03", "PASS", repair=summarize(repaired), scanned=objects, oracle=oracle.strip())

    before_version = version_of("libestr0")
    soname_e = build_linked("unifier-round1107e", "libestr0", "libestr.so*", "libestr0")
    txn = require_txn(service.install_local(
        config["env_id"], str(share / "unifier-round1107e.deb"),
        package_name="unifier-round1107e", use_argv=["/usr/bin/unifier-round1107e"]))
    after_version = version_of("libestr0")
    native_text = "\n".join(step.get("stdout_tail", "") for step in txn.get("native") or [])
    if before_version != after_version or "Unpacking libestr0" in native_text:
        raise RuntimeError("DEP04 reinstalled or replaced the shared provider")
    oracle_a = run_as_user(["/usr/bin/unifier-round1107a"])
    oracle_e = run_as_user(["/usr/bin/unifier-round1107e"])
    if soname_a not in oracle_a or soname_e not in oracle_e or cid_now() != cid_before:
        raise RuntimeError("DEP04 load paths diverged or CID changed")
    record("DEP04", "PASS", provider_version=after_version, transaction=summarize(txn),
           load_a=oracle_a[-300:], load_e=oracle_e[-300:])

    first = require_txn(service.repair_dependencies(
        config["env_id"], ["unifier-round1107a", "unifier-round1107e"]))
    second = require_txn(service.repair_dependencies(
        config["env_id"], ["unifier-round1107a", "unifier-round1107e"]))
    for item in (first, second):
        repair = item.get("dependency_repair") or {}
        if repair.get("installed") or repair.get("status") != "no-missing-elf-dependencies":
            raise RuntimeError("DEP05 repair was not a no-op: " + json.dumps(summarize(item))[-1500:])
    record("DEP05", "PASS", first=summarize(first), second=summarize(second))

    removed = require_txn(service.remove_packages(
        config["env_id"], ["unifier-round1107a"],
        use_argv=["/usr/bin/unifier-round1107a"]))
    if installed("unifier-round1107a") or not installed("unifier-round1107e") or not installed("libestr0"):
        raise RuntimeError("DEP06 remove changed the shared provider or the surviving package")
    oracle = run_as_user(["/usr/bin/unifier-round1107e"])
    if "ok\n" not in oracle or cid_now() != cid_before:
        raise RuntimeError("DEP06 survivor failed")
    record("DEP06", "PASS", remove=summarize(removed), survivor=oracle[-300:],
           provider_version=version_of("libestr0"))

    dl_script = textwrap.dedent(f"""
        set -eu
        work=/tmp/build-unifier-round1107h
        rm -rf "$work"
        mkdir -p "$work/DEBIAN" "$work/usr/bin"
        cat > /tmp/dlopen.c <<'EOF'
        #define _GNU_SOURCE
        #include <dlfcn.h>
        #include <link.h>
        #include <stdio.h>
        int main(void) {{
            char name[64];
            snprintf(name, sizeof name, "%s%s", "lib", "capstone.so.5");
            void *handle = dlopen(name, RTLD_NOW);
            if (!handle) {{ fprintf(stderr, "%s\\n", dlerror()); return 1; }}
            struct link_map *map = 0;
            if (dlinfo(handle, RTLD_DI_LINKMAP, &map) != 0 || !map) return 2;
            printf("ok %s\\n", map->l_name);
            return 0;
        }}
        EOF
        gcc -O2 -o "$work/usr/bin/unifier-round1107h" /tmp/dlopen.c -ldl
        cat > "$work/DEBIAN/control" <<'EOF'
        Package: unifier-round1107h
        Version: 1.0
        Architecture: amd64
        Maintainer: Unifier Test <test@example.invalid>
        Depends: libc6
        Description: runtime dlopen fixture
        EOF
        dpkg-deb --root-owner-group -b "$work" "{share}/unifier-round1107h.deb"
    """)
    guest(["/bin/sh", "-c", dl_script], timeout=120)
    txn = require_txn(service.install_local(
        config["env_id"], str(share / "unifier-round1107h.deb"),
        package_name="unifier-round1107h"))
    static = require_txn(service.repair_dependencies(config["env_id"], ["unifier-round1107h"]))
    static_repair = static.get("dependency_repair") or {}
    if static_repair.get("installed") or static_repair.get("status") == "repaired":
        raise RuntimeError("DEP08 static scan treated a runtime name as a NEEDED edge")
    failed = guest(["/usr/bin/unifier-round1107h"], user="1000", accepted=(0, 1, 127), timeout=20)
    if failed.returncode == 0:
        raise RuntimeError("DEP08 program ran before the hinted library existed")
    hinted = require_txn(service.repair_dependencies(
        config["env_id"], [], libraries=["libcapstone.so.5"]))
    hint = hinted.get("dependency_repair") or {}
    if "libcapstone5" not in (hint.get("installed") or []):
        raise RuntimeError("DEP08 hint did not install libcapstone5: " + json.dumps(summarize(hinted))[-1500:])
    oracle = run_as_user(["/usr/bin/unifier-round1107h"])
    if not oracle.startswith("ok "):
        raise RuntimeError("DEP08 oracle: " + oracle)
    record("DEP08", "PASS", static=summarize(static), hinted=summarize(hinted),
           pre_exit=failed.returncode, oracle=oracle.strip(), runtime_loading="loaded-after-explicit-library")

    # SH09: one live CLI plus two serialized package writes.
    beat = share / "heartbeat.txt"
    if beat.exists():
        beat.unlink()
    payload = (
        "import time\nfrom pathlib import Path\n"
        f"p=Path({str(beat)!r})\n"
        "n=0\n"
        "while True:\n"
        " n+=1\n p.write_text(str(n))\n time.sleep(0.2)\n"
    )
    log = (out / "heartbeat.log").open("wb")
    child = service.runtime.popen(
        ["podman", "exec", "--user", "1000", config["cid"],
         "/usr/bin/python3", "-c", payload], stdout=log, stderr=log)
    try:
        import time as _time
        end = _time.monotonic() + 10
        while _time.monotonic() < end and not beat.exists():
            _time.sleep(0.1)
        if not beat.exists():
            raise RuntimeError("SH09 heartbeat did not start")
        first_count = int(beat.read_text() or "0")
        tiny = textwrap.dedent(f"""
            set -eu
            name=$1
            work=/tmp/build-$name
            rm -rf "$work"
            mkdir -p "$work/DEBIAN" "$work/usr/bin"
            printf 'int main(void){{return 0;}}' > /tmp/tiny.c
            gcc -O2 -o "$work/usr/bin/$name" /tmp/tiny.c
            printf 'Package: %s\\nVersion: 1.0\\nArchitecture: amd64\\nMaintainer: Unifier Test <test@example.invalid>\\nDepends: libc6\\nDescription: lock fixture\\n' "$name" > "$work/DEBIAN/control"
            dpkg-deb --root-owner-group -b "$work" "{share}/$name.deb"
        """)
        guest(["/bin/sh", "-c", tiny, "sh", "unifier-round1107p"], timeout=60)
        guest(["/bin/sh", "-c", tiny, "sh", "unifier-round1107q"], timeout=60)
        import threading
        found = {}
        def install(name):
            try:
                found[name] = service.install_local(
                    config["env_id"], str(share / f"{name}.deb"), package_name=name)
            except Exception as exc:
                found[name] = exc
        threads = [threading.Thread(target=install, args=(name,))
                   for name in ("unifier-round1107p", "unifier-round1107q")]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=900)
        if any(thread.is_alive() for thread in threads):
            raise RuntimeError("SH09 package transaction did not finish")
        txns = []
        for name, value in found.items():
            if isinstance(value, Exception):
                raise RuntimeError(f"SH09 {name}: {value}")
            require_txn(value)
            txns.append(value)
        txns.sort(key=lambda item: item.get("started_at") or "")
        if txns[0].get("finished_at") > txns[1].get("started_at"):
            raise RuntimeError("SH09 native transactions overlapped")
        later = int(beat.read_text() or "0")
        if later <= first_count or cid_now() != cid_before:
            raise RuntimeError("SH09 heartbeat stopped or CID changed")
        record("SH09", "PASS", first=summarize(txns[0]), second=summarize(txns[1]),
               heartbeat_before=first_count, heartbeat_after=later)
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except Exception:
                child.kill()
        log.close()

    gio = guest(["/bin/sh", "-c", "command -v gio || true"], timeout=20)
    record("DEP11", "FEATURE_GAP", gio=gio.stdout.strip(),
           note="ELF repair does not install missing external commands; gio was not removed or replaced")
    record("DEP07", "BLOCKED", reason="no signed private fixture repository; real distribution libraries were not upgraded")
    record("DEP13", "BLOCKED", reason="interrupting apt on the only fixed Debian CID can leave a database this recover path will not rewrite")
    record("SH10", "BLOCKED", reason="Writer save/reopen needs a private display and DEP11 command repair; active desktop was not used")
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
print(json.dumps({"status": result["status"], "cases": [item["id"] + ":" + item["status"] for item in result["cases"]]}))
