#!/usr/bin/env python3
"""Correct the old-fd race and read FreeCAD's backup and STEP as saved."""

import datetime as dt
import json
import os
from pathlib import Path
import subprocess
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT / "evidence/arch-complex-2026-10-22T202359+0800/work"
PROJECT = WORK / "cross-save-2026-10-23"
CONTAINER = "ml-edae1d18-complex-arch"
XAUTH = Path("/run/user/1000/xauth_WtbESS")
MARKER = ROOT / "evidence/cross-save-2026-10-23/outside-marker.txt"

STAGE = r'''
import json, os, sys, time
from pathlib import Path
path, ready, go, dest = map(Path, sys.argv[1:])
fd = os.open(path, os.O_RDWR)
stale = os.read(fd, 4096)
Path(ready).write_text("ready\n")
while not Path(go).exists():
    time.sleep(0.02)
os.lseek(fd, 0, os.SEEK_SET)
os.ftruncate(fd, 0)
written = os.write(fd, "容器写到替换前打开的描述符\n".encode())
os.fsync(fd)
os.close(fd)
dest.write_text(json.dumps({
    "stale": stale.decode(), "bytes_written_to_old_fd": written,
    "path_text_after_old_fd_write": path.read_text(),
    "path_inode_after": path.stat().st_ino,
}, ensure_ascii=False, indent=2))
'''

READBACK = r'''
import json, os
from pathlib import Path
import FreeCAD as App
import Part
project = Path(os.environ["ML_PROJECT"])
backup = App.openDocument(str(project / "零件.20261023-030530.FCBak"))
box = backup.getObject("Box")
backup.recompute()
shape = Part.Shape()
shape.read(str(project / "零件.step"))
report = {"backup_length": box.Length.Value, "backup_volume": box.Shape.Volume,
          "step_volume": shape.Volume, "step_valid": shape.isValid()}
(project / "adjacent-readback.json").write_text(json.dumps(report))
'''


def run(argv, timeout=60, stdin=None):
    proc = subprocess.run(argv, text=True, capture_output=True, timeout=timeout,
                          input=stdin, encoding="utf-8", errors="replace")
    return {"argv": argv, "exit_code": proc.returncode, "stdout": proc.stdout,
            "stderr": proc.stderr}


def main():
    out = Path(__file__).resolve().parent / "result-followup.json"
    if out.exists():
        raise SystemExit("result-followup.json already exists")
    created = False
    started = False
    report = {"time": dt.datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
              "product_acceptance": False}
    try:
        state = run(["podman", "inspect", CONTAINER, "--format", "{{.State.Status}}"])
        report["state_before"] = state["stdout"].strip()
        if report["state_before"] != "exited":
            report["error"] = "container was not stopped"
            return
        if not XAUTH.exists():
            fd = os.open(XAUTH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            created = True
        start = run(["podman", "start", CONTAINER], timeout=40)
        report["start_exit"] = start["exit_code"]
        if start["exit_code"] != 0:
            report["error"] = start["stderr"]
            return
        started = True
        (PROJECT / "stage_fd.py").write_text(STAGE)
        name = "竞争-描述符-重测.txt"
        path = PROJECT / name
        path.write_text("描述符打开时的原文\n")
        ready, go = PROJECT / f"{name}.ready", PROJECT / f"{name}.go"
        for flag in (ready, go):
            if flag.exists():
                flag.unlink()
        proc = subprocess.Popen(
            ["podman", "exec", "-u", "1000:1000", "-w", str(PROJECT), CONTAINER,
             "python3", str(PROJECT / "stage_fd.py"), str(path), str(ready), str(go),
             str(PROJECT / "stage-fd.json")],
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace")
        deadline = time.time() + 30
        while not ready.exists() and proc.poll() is None and time.time() < deadline:
            time.sleep(0.02)
        opened_inode = path.stat().st_ino
        host_tmp = PROJECT / f"{name}.host.tmp"
        host_tmp.write_text("宿主换了新 inode\n")
        os.replace(host_tmp, path)
        replaced_inode = path.stat().st_ino
        go.write_text("go\n")
        stdout, stderr = proc.communicate(timeout=30)
        report["stage_fd"] = {
            "exit_code": proc.returncode, "stdout": stdout, "stderr": stderr,
            "opened_inode": opened_inode, "host_replacement_inode": replaced_inode,
            "inside": json.loads((PROJECT / "stage-fd.json").read_text()),
            "final_text": path.read_text(), "final_inode": path.stat().st_ino,
        }
        report["dates"] = {
            "host": run(["date", "-Is"])["stdout"].strip(),
            "container": run(["podman", "exec", "-u", "1000:1000", CONTAINER, "date", "-Is"])["stdout"].strip(),
        }
        report["outside_absolute"] = run([
            "podman", "exec", "-u", "1000:1000", CONTAINER, "sh", "-c",
            f"test -e {str(MARKER)!r} && echo visible || echo absent"])
        report["root_relative_leftover"] = run([
            "podman", "exec", "-u", "1000:1000", CONTAINER, "sh", "-c",
            "test -e /相对零件.FCStd && echo present || echo absent"])
        report["home_leftover"] = run([
            "podman", "exec", "-u", "1000:1000", CONTAINER, "sh", "-c",
            "test -e /home/multilinux-1000/默认家目录.FCStd && echo present || echo absent"])
        (PROJECT / "readback.py").write_text(READBACK)
        report["readback_cmd"] = run([
            "podman", "exec", "-u", "1000:1000", "-w", str(PROJECT),
            "-e", "QT_QPA_PLATFORM=offscreen", "-e", "DISPLAY=", "-e", "WAYLAND_DISPLAY=",
            "-e", f"ML_PROJECT={PROJECT}", CONTAINER, "freecadcmd", str(PROJECT / "readback.py")],
            timeout=180)
        report["readback"] = json.loads((PROJECT / "adjacent-readback.json").read_text())
    except Exception as exc:
        report["error"] = repr(exc)
    finally:
        if started:
            report["stop"] = run(["podman", "stop", "-t", "0", CONTAINER], timeout=40)
            report["state_after"] = run(
                ["podman", "inspect", CONTAINER, "--format", "{{.State.Status}}"])["stdout"].strip()
        if created and XAUTH.exists():
            XAUTH.unlink()
            report["xauth_removed"] = not XAUTH.exists()
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        stage = report.get("stage_fd") or {}
        print(json.dumps({
            "error": report.get("error"),
            "state_after": report.get("state_after"),
            "stage_exit": stage.get("exit_code"),
            "stage_stderr": (stage.get("stderr") or "")[-400:],
            "opened_inode": stage.get("opened_inode"),
            "host_inode": stage.get("host_replacement_inode"),
            "inside": stage.get("inside"),
            "final_text": stage.get("final_text"),
            "final_inode": stage.get("final_inode"),
            "dates": report.get("dates"),
            "outside": (report.get("outside_absolute") or {}).get("stdout"),
            "root_relative": (report.get("root_relative_leftover") or {}).get("stdout"),
            "home": (report.get("home_leftover") or {}).get("stdout"),
            "readback": report.get("readback"),
            "readback_exit": (report.get("readback_cmd") or {}).get("exit_code"),
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
