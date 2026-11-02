#!/usr/bin/env python3
"""Run the approved Fedora-workstation slice of the extension plan."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path("/home/omg/文档/多兼容linux机")
EVID = REPO / "artifacts/extension-run-fedora-workstation-20261031"
HOME = Path("/home/omg/.local/share/unifier-extension-20261031")
DAILY = Path("/home/omg/.local/share/unifier")
DAILY_NAMES = ("ml-53c5c8a5-freecad-arch", "ml-6b061a2f-meeting")
LAUNCHER = REPO / "bin/unifier"
ARCH_MAX_BYTES = 80 * 1024 * 1024
STEPS = EVID / "steps"

results: list[dict] = []


def daily_snapshot() -> dict:
    rows = {}
    for name in DAILY_NAMES:
        proc = subprocess.run(
            ["podman", "inspect", name, "--format",
             "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}"],
            text=True, capture_output=True, check=False)
        rows[name] = {
            "exit": proc.returncode,
            "line": (proc.stdout or proc.stderr).strip(),
        }
    return rows


def container_names() -> list[str]:
    proc = subprocess.run(
        ["podman", "ps", "-a", "--format", "{{.Names}}"],
        text=True, capture_output=True, check=False)
    return sorted(line.strip() for line in proc.stdout.splitlines() if line.strip())


def run_step(name: str, args: list[str], *, expect: int | None = 0,
             timeout: int = 180, env: dict | None = None) -> dict:
    STEPS.mkdir(parents=True, exist_ok=True)
    merged = os.environ.copy()
    merged["UNIFIER_HOME"] = str(HOME)
    merged["LC_ALL"] = "C"
    merged["LANG"] = "C"
    merged["HOME"] = str(Path.home())
    if env:
        merged.update(env)
    started = time.time()
    try:
        proc = subprocess.run(
            args, text=True, capture_output=True, timeout=timeout,
            cwd=str(REPO), env=merged, check=False)
        code, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        code, timed_out = 124, True
        out = exc.stdout or ""
        err = (exc.stderr or "") + f"\nTIMEOUT after {timeout}s"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
    (STEPS / f"{name}.stdout").write_text(out, encoding="utf-8")
    (STEPS / f"{name}.stderr").write_text(err, encoding="utf-8")
    (STEPS / f"{name}.exit").write_text(str(code), encoding="utf-8")
    ok = (code == expect) if expect is not None else True
    row = {
        "name": name,
        "argv": args,
        "exit": code,
        "expect": expect,
        "ok": ok,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 2),
    }
    results.append(row)
    print(f"STEP {name} exit={code} ok={ok} {row['seconds']}s", flush=True)
    return row


def unifier(name: str, args: list[str], **kwargs) -> dict:
    return run_step(name, [str(LAUNCHER), *args], **kwargs)


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"<unreadable {exc}>"


def record_note(name: str, payload: dict) -> None:
    (STEPS / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results.append({"name": name, "ok": bool(payload.get("ok", True)), **payload})
    print(f"STEP {name} ok={payload.get('ok')}", flush=True)


def environment_record(env_id: str) -> dict:
    path = HOME / ".lab/state/environments" / f"{env_id}.json"
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def files_dir(target: str) -> Path:
    return HOME / "containers" / target / "files"


def canary(target: str) -> None:
    directory = files_dir(target)
    directory.mkdir(parents=True, exist_ok=True)
    host_file = directory / "host-cycle.txt"
    guest_file = directory / "guest-cycle.txt"
    chinese = directory / "中文 样本.txt"
    host_file.write_text("cycle1-host\n", encoding="utf-8")
    chinese.write_text("中文保存\n", encoding="utf-8")
    guest_file.unlink(missing_ok=True)
    unifier(f"{target}-start-1", ["start", target, "--json"], timeout=120)
    quoted_host = sh_quote(str(host_file))
    quoted_guest = sh_quote(str(guest_file))
    quoted_zh = sh_quote(str(chinese))
    unifier(
        f"{target}-guest-read-1",
        ["env", "run", target, "--", "sh", "-c",
         f"cat {quoted_host} {quoted_zh}; printf 'cycle1-guest\\n' > {quoted_guest}"],
        timeout=60)
    unifier(f"{target}-stop-1", ["stop", target, "--json"], timeout=120)
    cycle1_host = read_text(guest_file)
    unifier(f"{target}-ls-stopped", ["ls", target, "/", "--json"], timeout=120)
    host_file.write_text("cycle2-host\n", encoding="utf-8")
    unifier(f"{target}-start-2", ["start", target, "--json"], timeout=120)
    unifier(
        f"{target}-guest-read-2",
        ["env", "run", target, "--", "sh", "-c",
         f"cat {quoted_host}; printf 'cycle2-guest\\n' >> {quoted_guest}"],
        timeout=60)
    unifier(
        f"{target}-desktop-env",
        ["env", "run", target, "--", "sh", "-c",
         "printf 'WAYLAND=%s\\nXDG_RUNTIME_DIR=%s\\nDISPLAY=%s\\n' "
         "\"$WAYLAND_DISPLAY\" \"$XDG_RUNTIME_DIR\" \"$DISPLAY\"; "
         "if [ -n \"$XDG_RUNTIME_DIR\" ] && [ -n \"$WAYLAND_DISPLAY\" ]; then "
         "ls -l \"$XDG_RUNTIME_DIR/$WAYLAND_DISPLAY\"; "
         "else printf 'NO_WAYLAND_ENV\\n'; env | sort; fi"],
        timeout=60)
    unifier(f"{target}-ls-running", ["ls", target, "/", "--json"], timeout=120)
    unifier(f"{target}-stop-2", ["stop", target, "--json"], timeout=120)
    guest_text = read_text(guest_file)
    stdout1 = read_text(STEPS / f"{target}-guest-read-1.stdout")
    stdout2 = read_text(STEPS / f"{target}-guest-read-2.stdout")
    desktop = read_text(STEPS / f"{target}-desktop-env.stdout")
    record = environment_record(target)
    mounts = record.get("shared_mounts") or []
    ok = (
        "cycle1-host" in stdout1
        and "中文保存" in stdout1
        and cycle1_host == "cycle1-guest\n"
        and "cycle2-host" in stdout2
        and guest_text == "cycle1-guest\ncycle2-guest\n"
        and bool(record.get("desktop"))
        and any(str(item.get("host", "")).endswith(f"/{target}/files") for item in mounts)
    )
    record_note(f"{target}-canary", {
        "ok": ok,
        "desktop_flag": bool(record.get("desktop")),
        "userns": record.get("user_namespace") or record.get("userns"),
        "network": record.get("network"),
        "mounts": mounts,
        "guest_file": guest_text,
        "desktop_probe": desktop.strip().splitlines()[:8],
        "wayland_socket_listed": "wayland" in desktop and "NO_WAYLAND_ENV" not in desktop,
    })


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def debian_jq() -> None:
    sample = files_dir("debian13") / "sample.json"
    sample.write_text(
        '{"name":"中文","n":2}\n{"name":"space name","n":3}\n',
        encoding="utf-8")
    bad = files_dir("debian13") / "bad.json"
    bad.write_text("{", encoding="utf-8")
    unifier(
        "debian-jq-install",
        ["pkg", "install", "debian13", "jq", "--use-command", "jq", "--use-command", "--version"],
        timeout=420)
    quoted = sh_quote(str(sample))
    badq = sh_quote(str(bad))
    unifier("debian-jq-start", ["start", "debian13", "--json"], timeout=120)
    unifier(
        "debian-jq-oracle",
        ["env", "run", "debian13", "--", "sh", "-c",
         f"jq -s 'map(.n) | add' {quoted}; "
         f"jq -e 'select(.name==\"中文\")' {quoted} >/dev/null; "
         f"jq -e . {badq} >/dev/null; echo bad_exit=$?"],
        timeout=60)
    unifier("debian-jq-stop", ["stop", "debian13", "--json"], timeout=120)
    oracle = read_text(STEPS / "debian-jq-oracle.stdout")
    install_ok = (STEPS / "debian-jq-install.exit").read_text(encoding="utf-8").strip() == "0"
    # jq -e on bad json must fail, and the shell records that as bad_exit.
    lines = [line.strip() for line in oracle.splitlines() if line.strip()]
    ok = install_ok and lines[:1] == ["5"] and any(line.startswith("bad_exit=") and line != "bad_exit=0" for line in lines)
    record_note("debian-jq", {"ok": ok, "oracle": lines, "install_ok": install_ok})


def arch_jq_gate() -> None:
    unifier("arch-start-measure", ["start", "arch", "--json"], timeout=120)
    unifier(
        "arch-pacman-print",
        ["env", "run", "arch", "--", "sh", "-c",
         "pacman -Syu --print --noconfirm --print-format '%n %m' || "
         "pacman -Syu --print --noconfirm"],
        timeout=300)
    text = read_text(STEPS / "arch-pacman-print.stdout")
    err = read_text(STEPS / "arch-pacman-print.stderr")
    total = 0
    parsed = 0
    names = []
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1].isdigit():
            total += int(parts[-1])
            parsed += 1
            names.append(parts[0])
        elif line.strip() and not line.startswith(" "):
            names.append(line.strip())
    measured = parsed > 0
    allowed = measured and total <= ARCH_MAX_BYTES
    record_note("arch-upgrade-measure", {
        "ok": True,
        "measured": measured,
        "download_bytes": total if measured else None,
        "package_lines": len(names),
        "allowed": allowed,
        "threshold_bytes": ARCH_MAX_BYTES,
        "stderr_tail": err.strip().splitlines()[-8:],
    })
    unifier("arch-stop-measure", ["stop", "arch", "--json"], timeout=120)
    if not allowed:
        record_note("arch-jq", {
            "ok": True,
            "status": "DEFERRED_BANDWIDTH",
            "reason": "Arch -Syu payload was not measured under 80 MiB; no upgrade was applied",
            "download_bytes": total if measured else None,
            "package_lines": len(names),
        })
        return
    unifier(
        "arch-jq-install",
        ["pkg", "install", "arch", "jq", "--use-command", "jq", "--use-command", "--version"],
        timeout=900)
    sample = files_dir("arch") / "sample.json"
    sample.write_text('{"n":2}\n{"n":3}\n', encoding="utf-8")
    unifier("arch-jq-start", ["start", "arch", "--json"], timeout=120)
    unifier(
        "arch-jq-oracle",
        ["env", "run", "arch", "--", "jq", "-s", "map(.n)|add", str(sample)],
        timeout=60)
    unifier("arch-jq-stop", ["stop", "arch", "--json"], timeout=120)
    oracle = read_text(STEPS / "arch-jq-oracle.stdout").strip()
    install_ok = (STEPS / "arch-jq-install.exit").read_text(encoding="utf-8").strip() == "0"
    record_note("arch-jq", {
        "ok": install_ok and oracle == "5",
        "status": "PASS_WORKFLOW" if install_ok and oracle == "5" else "FAIL_PRODUCT",
        "oracle": oracle,
    })


def preserve(before: dict) -> None:
    after = daily_snapshot()
    (EVID / "daily-after.json").write_text(
        json.dumps(after, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    same = before == after
    names = container_names()
    daily_still = all(name in names for name in DAILY_NAMES)
    record_note("daily-containers-preserved", {
        "ok": same and daily_still,
        "before": before,
        "after": after,
    })


def cleanup() -> None:
    for target in ("debian13", "arch", "fedora44"):
        record = environment_record(target)
        if not record:
            continue
        unifier(f"cleanup-stop-{target}", ["stop", target, "--json"],
                expect=None, timeout=120)
        unifier(f"cleanup-remove-{target}",
                ["env", "remove", target, "--yes"], expect=None, timeout=180)
    leftover = [name for name in container_names()
                if name not in DAILY_NAMES and name.startswith("ml-")]
    # Only report new ml containers whose inspect label matches this root.
    owned = []
    for name in leftover:
        proc = subprocess.run(
            ["podman", "inspect", name, "--format", "{{json .Config.Labels}}"],
            text=True, capture_output=True, check=False)
        if str(HOME) in (proc.stdout or ""):
            owned.append(name)
    record_note("cleanup", {"ok": not owned, "owned_leftovers": owned})


def main() -> int:
    EVID.mkdir(parents=True, exist_ok=True)
    STEPS.mkdir(parents=True, exist_ok=True)
    if HOME.resolve() == DAILY.resolve():
        raise SystemExit("refusing to use the daily installation root")
    if HOME.exists():
        raise SystemExit(f"refusing to reuse existing {HOME}")
    before = daily_snapshot()
    (EVID / "daily-before.json").write_text(
        json.dumps(before, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    before_names = container_names()
    try:
        unifier("cli-help", ["help"], timeout=30)
        unifier("cli-help-init", ["help", "init"], timeout=30)
        unifier("cli-unknown", ["nosuch"], expect=2, timeout=30)
        unifier("cli-version", ["--version"], timeout=30)
        unifier("language-explicit", ["language", "en", "--json"], timeout=30)
        unifier("compat-debian", ["compat", "check", "--target", "debian13"], timeout=30)
        unifier("compat-arch", ["compat", "check", "--target", "arch"], timeout=30)
        unifier("compat-fedora", ["compat", "check", "--target", "fedora44"],
                expect=None, timeout=30)
        fedora_compat = read_text(STEPS / "compat-fedora.stdout") + read_text(STEPS / "compat-fedora.stderr")
        fedora_exit = int((STEPS / "compat-fedora.exit").read_text(encoding="utf-8"))
        record_note("fedora44-compat-refusal", {
            "ok": fedora_exit != 0,
            "exit": fedora_exit,
            "tail": fedora_compat.strip().splitlines()[-6:],
        })
        unifier("compat-matrix", ["compat", "matrix"], timeout=30)
        unifier("init-fedora44",
                ["init", "--target", "fedora44", "--yes", "--no-desktop", "--network", "none", "--json"],
                expect=None, timeout=120)
        after_refuse = container_names()
        refuse_exit = int((STEPS / "init-fedora44.exit").read_text(encoding="utf-8"))
        record_note("fedora44-init-refusal", {
            "ok": refuse_exit != 0 and after_refuse == before_names,
            "exit": refuse_exit,
            "new_containers": sorted(set(after_refuse) - set(before_names)),
        })
        unifier("env-create-fedora44",
                ["env", "create", "--target", "fedora44", "--network", "none"],
                expect=None, timeout=120)
        create_exit = int((STEPS / "env-create-fedora44.exit").read_text(encoding="utf-8"))
        after_create = container_names()
        record_note("fedora44-create-refusal", {
            "ok": create_exit != 0 and after_create == before_names,
            "exit": create_exit,
            "new_containers": sorted(set(after_create) - set(before_names)),
        })
        unifier(
            "init-admitted",
            ["init", "--target", "debian13", "--target", "arch", "--yes",
             "--desktop", "--network", "pasta", "--json"],
            timeout=300)
        unifier("list", ["list", "--json"], timeout=30)
        unifier("dirs-debian", ["dirs", "debian13"], timeout=30)
        unifier("dirs-arch", ["dirs", "arch"], timeout=30)
        for target in ("debian13", "arch"):
            record = environment_record(target)
            namespace = (record.get("runtime") or {}).get("user_namespace")
            record_note(f"{target}-record", {
                "ok": bool(record) and bool(record.get("desktop"))
                and namespace == "keep-id" and record.get("network") == "pasta",
                "id": record.get("id"),
                "target": record.get("target"),
                "desktop": record.get("desktop"),
                "network": record.get("network"),
                "user_namespace": namespace,
                "runtime_state": record.get("runtime_state"),
                "mounts": record.get("shared_mounts"),
            })
            canary(target)
        debian_jq()
        arch_jq_gate()
    finally:
        try:
            cleanup()
        finally:
            preserve(before)
    payload = {
        "host_context": "fedora-workstation",
        "installation_root": str(HOME),
        "steps": results,
        "failed": [row["name"] for row in results if not row.get("ok")],
    }
    (EVID / "result.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"DONE failed={len(payload['failed'])}", flush=True)
    return 0 if not payload["failed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
