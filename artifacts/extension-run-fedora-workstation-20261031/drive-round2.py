#!/usr/bin/env python3
"""Second pass: jq, desktop sockets, and the stopped-ls mount options."""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

REPO = Path("/home/omg/文档/多兼容linux机")
EVID = REPO / "artifacts/extension-run-fedora-workstation-20261031/round2"
HOME = Path("/home/omg/.local/share/unifier-extension-20261031-r2")
DAILY = Path("/home/omg/.local/share/unifier")
DAILY_NAMES = ("ml-53c5c8a5-freecad-arch", "ml-6b061a2f-meeting")
BASELINE = REPO / "artifacts/extension-run-fedora-workstation-20261031/daily-before.json"
LAUNCHER = REPO / "bin/unifier"
ARCH_MAX = 80 * 1024 * 1024
results: list[dict] = []


def snap() -> dict:
    rows = {}
    for name in DAILY_NAMES:
        proc = subprocess.run(
            ["podman", "inspect", name, "--format",
             "{{.Id}}|{{.State.Status}}|{{.State.StartedAt}}|{{.State.FinishedAt}}"],
            text=True, capture_output=True, check=False)
        rows[name] = {"exit": proc.returncode, "line": (proc.stdout or proc.stderr).strip()}
    return rows


def run(name: str, args: list[str], *, timeout: int = 180, expect: int | None = 0) -> dict:
    env = os.environ.copy()
    env.update({"UNIFIER_HOME": str(HOME), "LC_ALL": "C", "LANG": "C"})
    started = time.time()
    try:
        proc = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                              cwd=str(REPO), env=env, check=False)
        code, out, err, timed = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        code, timed = 124, True
        out = exc.stdout or ""
        err = (exc.stderr or "") + f"\nTIMEOUT {timeout}s"
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
    EVID.mkdir(parents=True, exist_ok=True)
    (EVID / f"{name}.stdout").write_text(out, encoding="utf-8")
    (EVID / f"{name}.stderr").write_text(err, encoding="utf-8")
    ok = code == expect if expect is not None else True
    row = {"name": name, "exit": code, "expect": expect, "ok": ok,
           "seconds": round(time.time() - started, 2), "timed_out": timed}
    results.append(row)
    print(f"STEP {name} exit={code} ok={ok} {row['seconds']}s", flush=True)
    return row


def U(name: str, args: list[str], **kwargs) -> dict:
    return run(name, [str(LAUNCHER), *args], **kwargs)


def note(name: str, payload: dict) -> None:
    (EVID / f"{name}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    results.append({"name": name, "ok": bool(payload.get("ok", True)), **payload})
    print(f"STEP {name} ok={payload.get('ok')}", flush=True)


def record(env_id: str) -> dict:
    path = HOME / ".lab/state/environments" / f"{env_id}.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}


def q(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def parse_size(token: str) -> int | None:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?i?B)", token.strip())
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2)
    scale = {"": 1, "B": 1, "KiB": 1024, "MiB": 1024**2, "GiB": 1024**3,
             "KB": 1000, "MB": 1000**2, "GB": 1000**3, "TiB": 1024**4}.get(unit)
    if scale is None:
        return None
    return int(value * scale)


def main() -> int:
    if HOME.resolve() == DAILY.resolve() or HOME.exists():
        raise SystemExit(f"refusing root {HOME}")
    before = snap()
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    if before != baseline:
        raise SystemExit("daily containers changed before round 2; aborting")
    try:
        U("init", ["init", "--target", "debian13", "--target", "arch", "--yes",
                   "--desktop", "--network", "pasta", "--json"], timeout=180)
        for target in ("debian13", "arch"):
            U(f"{target}-start", ["start", target, "--json"], timeout=90)
            U(f"{target}-identity", ["env", "run", target, "--", "sh", "-c",
              "id; printf 'HOME=%s\\n' \"$HOME\"; "
              "for p in /run/user/1000/wayland-0 /tmp/.X11-unix/X0 "
              "/run/user/1000/pulse/native /run/user/1000/pipewire-0; do "
              "if [ -S \"$p\" ]; then echo SOCKET \"$p\"; else echo MISSING \"$p\"; fi; done"],
              timeout=40)
            U(f"{target}-stop", ["stop", target, "--json"], timeout=90)
            info = record(target)
            cid = (info.get("runtime") or {}).get("container_id", "")
            name = (info.get("runtime") or {}).get("container_name", "")
            proc = subprocess.run(
                ["podman", "inspect", cid, "--format", "{{json .Mounts}}"],
                text=True, capture_output=True, check=False)
            (EVID / f"{target}-mounts.json").write_text(proc.stdout or proc.stderr, encoding="utf-8")
            listed = U(f"{target}-ls-stopped", ["ls", target, "/", "--json"],
                       expect=None, timeout=90)
            files = next((item for item in (info.get("shared_mounts") or [])
                          if str(item.get("host", "")).endswith("/files")), {})
            try:
                mounts = json.loads(proc.stdout or "[]")
            except json.JSONDecodeError:
                mounts = []
            match = next((item for item in mounts
                          if item.get("Destination") == files.get("container")), {})
            note(f"{target}-stopped-ls", {
                "ok": listed["exit"] == 0,
                "recorded_options": files.get("options"),
                "podman_options": match.get("Options"),
                "podman_rw": match.get("RW"),
                "stderr": (EVID / f"{target}-ls-stopped.stderr").read_text(encoding="utf-8").strip(),
                "container_name": name,
            })
            text = (EVID / f"{target}-identity.stdout").read_text(encoding="utf-8")
            note(f"{target}-desktop-sockets", {
                "ok": "SOCKET /run/user/1000/wayland-0" in text,
                "probe": text.strip().splitlines(),
            })

        sample = HOME / "containers/debian13/files/sample.json"
        bad = HOME / "containers/debian13/files/bad.json"
        sample.parent.mkdir(parents=True, exist_ok=True)
        sample.write_text('{"name":"中文","n":2}\n{"name":"space name","n":3}\n', encoding="utf-8")
        bad.write_text("{", encoding="utf-8")
        installed = U("debian-jq-install", ["pkg", "install", "debian13", "jq"], timeout=420)
        U("debian-jq-start", ["start", "debian13", "--json"], timeout=90)
        U("debian-jq-oracle", ["env", "run", "debian13", "--", "sh", "-c",
           f"jq --version; jq -s 'map(.n) | add' {q(str(sample))}; "
           f"jq -e 'select(.name==\"中文\")' {q(str(sample))} >/dev/null; "
           f"jq -e . {q(str(bad))} >/dev/null; echo bad_exit=$?"], timeout=40)
        U("debian-jq-stop", ["stop", "debian13", "--json"], timeout=90)
        oracle = (EVID / "debian-jq-oracle.stdout").read_text(encoding="utf-8").splitlines()
        install_ok = installed["exit"] == 0
        note("debian-jq", {
            "ok": install_ok and any(line.strip() == "5" for line in oracle)
            and any(line.startswith("bad_exit=") and line != "bad_exit=0" for line in oracle),
            "install_ok": install_ok,
            "oracle": [line.strip() for line in oracle if line.strip()],
        })

        U("arch-start", ["start", "arch", "--json"], timeout=90)
        U("arch-size", ["env", "run", "arch", "--", "sh", "-c",
           "pacman -Sy --noconfirm >/tmp/sy.err 2>&1 || { cat /tmp/sy.err; exit 1; }; "
           "pkgs=$(pacman -Quq || true); "
           "if [ -z \"$pkgs\" ]; then echo NO_UPGRADES; exit 0; fi; "
           "pacman -Si $pkgs | awk '/^Name /{name=$3} /^Download Size/{print name, $4, $5}'"],
           timeout=240)
        U("arch-stop", ["stop", "arch", "--json"], timeout=90)
        size_text = (EVID / "arch-size.stdout").read_text(encoding="utf-8")
        total = 0
        parsed = []
        for line in size_text.splitlines():
            if line.strip() == "NO_UPGRADES":
                continue
            parts = line.split()
            if len(parts) >= 3:
                amount = parse_size(parts[-2] + " " + parts[-1])
                if amount is not None:
                    total += amount
                    parsed.append({"name": parts[0], "bytes": amount})
        allowed = (size_text.strip() == "NO_UPGRADES") or (parsed and total <= ARCH_MAX)
        note("arch-upgrade-measure", {
            "ok": True,
            "allowed": bool(allowed),
            "download_bytes": total if parsed or size_text.strip() == "NO_UPGRADES" else None,
            "packages": len(parsed),
            "sample": parsed[:8],
            "threshold_bytes": ARCH_MAX,
        })
        if allowed:
            installed = U("arch-jq-install", ["pkg", "install", "arch", "jq"], timeout=900)
            arch_sample = HOME / "containers/arch/files/sample.json"
            arch_sample.write_text('{"n":2}\n{"n":3}\n', encoding="utf-8")
            U("arch-jq-start", ["start", "arch", "--json"], timeout=90)
            U("arch-jq-oracle", ["env", "run", "arch", "--", "jq", "-s", "map(.n)|add",
                                 str(arch_sample)], timeout=40)
            U("arch-jq-stop", ["stop", "arch", "--json"], timeout=90)
            oracle = (EVID / "arch-jq-oracle.stdout").read_text(encoding="utf-8").strip()
            install_ok = installed["exit"] == 0
            note("arch-jq", {"ok": install_ok and oracle == "5", "oracle": oracle,
                             "status": "PASS_WORKFLOW" if install_ok and oracle == "5" else "FAIL"})
        else:
            note("arch-jq", {"ok": True, "status": "DEFERRED_BANDWIDTH",
                             "download_bytes": total, "packages": len(parsed)})
    finally:
        for target in ("debian13", "arch"):
            if record(target):
                U(f"cleanup-stop-{target}", ["stop", target, "--json"], expect=None, timeout=90)
                U(f"cleanup-remove-{target}", ["env", "remove", target, "--yes"],
                  expect=None, timeout=120)
        after = snap()
        note("daily-containers-preserved", {"ok": after == baseline, "after": after})
    failed = [row["name"] for row in results if not row.get("ok")]
    (EVID / "result.json").write_text(json.dumps(
        {"failed": failed, "steps": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    print(f"DONE failed={len(failed)}", flush=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
