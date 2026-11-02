"""SIGKILL loses the controller; the persisted journal must block re-entry."""
from pathlib import Path
import io
import json
import os
import subprocess
import tarfile
import time

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "crash-final"
OUT.mkdir(exist_ok=True)
HOME = ROOT / ".lab/expansion-fix-20261101/environment"
environment = {**os.environ, "UNIFIER_HOME": str(HOME), "LC_ALL": "C", "LANG": "C",
               "PYTHONDONTWRITEBYTECODE": "1"}
if (OUT / "result.json").exists():
    raise SystemExit("completed crash receipt already exists")


def archive(files):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, (content, mode) in files.items():
            entry = tarfile.TarInfo(name)
            data = content.encode()
            entry.size, entry.mode = len(data), mode
            tar.addfile(entry, io.BytesIO(data))
    return stream.getvalue()


members = {"debian-binary": b"2.0\n", "control.tar.gz": archive({
    "control": ("Package: unifier-interrupt-probe\nVersion: 7.0\nArchitecture: all\nMaintainer: Local Test <test@localhost>\nDescription: local crash fixture\n", 0o644),
    "postinst": ("#!/bin/sh\nset -eu\necho CRASH_READY\nsleep 60\n", 0o755)}),
    "data.tar.gz": archive({"usr/share/unifier-interrupt-probe-version": ("7.0\n", 0o644)})}
content = bytearray(b"!<arch>\n")
for name, data in members.items():
    content.extend(f"{name+'/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode())
    content.extend(data)
    if len(data) % 2:
        content.extend(b"\n")
package = OUT / "probe-7.0.deb"
package.write_bytes(content)
steps = []


def run(*argv, check=False):
    result = subprocess.run([str(ROOT / "bin/unifier"), *argv], env=environment,
                            capture_output=True, text=True, timeout=120)
    steps.append({"argv": argv, "code": result.returncode,
                  "stdout": result.stdout, "stderr": result.stderr})
    (OUT / "steps.json").write_text(json.dumps(steps, indent=2))
    if check and result.returncode:
        raise RuntimeError(steps[-1])
    return result


with (OUT / "terminal.log").open("w") as log:
    process = subprocess.Popen([str(ROOT / "bin/unifier"), "pkg", "install", "interrupt-debian",
        "--file", str(package), "--package", "unifier-interrupt-probe", "--yes", "--no-repair-deps"],
        env=environment, stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 120
        while "CRASH_READY" not in (OUT / "terminal.log").read_text() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        assert "CRASH_READY" in (OUT / "terminal.log").read_text()
        process.kill()
        assert process.wait(timeout=5) == -9
        view = json.loads(run("env", "inspect", "interrupt-debian", check=True).stdout)
        assert view["package_inventory_status"] == "unknown"
        assert view["pending_package_transactions"]
        refused = run("pkg", "remove", "interrupt-debian", "unifier-interrupt-probe", "--yes")
        assert refused.returncode and "pkg recover" in refused.stderr
        refused = run("pkg", "recover", "interrupt-debian")
        assert refused.returncode and "explicitly stop" in refused.stderr
        run("env", "stop", "interrupt-debian", check=True)
        run("env", "start", "interrupt-debian", check=True)
        run("env", "run", "interrupt-debian", "--", "/bin/sh", "-c",
            "sed -i '/sleep 60/d' /var/lib/dpkg/info/unifier-interrupt-probe.postinst; dpkg --configure -a", check=True)
        recovered = json.loads(run("pkg", "recover", "interrupt-debian", check=True).stdout)
        assert recovered["status"] == "recovered"
        run("env", "stop", "interrupt-debian", check=True)
        (OUT / "result.json").write_text(json.dumps({"passed": True, "controller_exit": -9,
            "unknown_inventory_visible": True, "retry_blocked": True,
            "running_recovery_refused": True, "recovery_after_explicit_stop_and_repair": recovered}, indent=2))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=40)
