"""Real Debian install interruption and recovery in a dedicated environment."""
import gzip
import io
import json
import os
import fcntl
from pathlib import Path
import pty
import select
import signal
import struct
import subprocess
import sys
import tarfile
import time
import termios

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "native-attempt3"
HOME = ROOT / ".lab/expansion-fix-20261101/environment"
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]
from unifier.install import load_catalog
from unifier.runtime import PodmanRuntime
from unifier.service import EnvironmentService


def deb(version, script):
    def archive(files):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, (content, mode) in files.items():
                entry = tarfile.TarInfo(name)
                data = content.encode()
                entry.size, entry.mode = len(data), mode
                tar.addfile(entry, io.BytesIO(data))
        return stream.getvalue()
    control = f"Package: unifier-interrupt-probe\nVersion: {version}\nArchitecture: all\nMaintainer: Local Test <test@localhost>\nDescription: isolated interrupt fixture\n"
    members = {"debian-binary": b"2.0\n",
               "control.tar.gz": archive({"control": (control, 0o644), "postinst": (script, 0o755)}),
               "data.tar.gz": archive({"usr/share/unifier-interrupt-probe-version": (version + "\n", 0o644)})}
    output = bytearray(b"!<arch>\n")
    for name, data in members.items():
        output.extend(f"{name+'/':<16}{0:<12}{0:<6}{0:<6}{'100644':<8}{len(data):<10}`\n".encode())
        output.extend(data)
        if len(data) % 2:
            output.extend(b"\n")
    path = OUT / f"probe-{version}.deb"
    path.write_bytes(output)
    return path


def command(*args, check=False):
    completed = subprocess.run([str(ROOT / "bin/unifier"), *args],
        env={**os.environ, "UNIFIER_HOME": str(HOME), "LC_ALL": "C", "LANG": "C",
             "PYTHONDONTWRITEBYTECODE": "1"}, capture_output=True, text=True, timeout=240)
    result = {"argv": list(args), "code": completed.returncode,
              "stdout": completed.stdout, "stderr": completed.stderr}
    results.append(result)
    (OUT / "native-steps.json").write_text(json.dumps(results, indent=2))
    if check and completed.returncode:
        raise RuntimeError(result)
    return completed


OUT.mkdir(exist_ok=True)
if (OUT / "native-result.json").exists():
    raise SystemExit("completed receipt already exists")
config, lock = load_catalog(ROOT)
service = EnvironmentService(HOME, PodmanRuntime(), config, lock)
results = []
if service.store.exists("interrupt-debian"):
    previous = json.loads((OUT.parent / "native-fixture.json").read_text())
    record = service.inspect("interrupt-debian")
    assert record["runtime"]["container_id"] == previous["runtime"]["container_id"]
    assert record["runtime_state"] == "stopped"
else:
    record = service.create("debian13", "interrupt-debian", [], "pasta", user_namespace="keep-id")
(OUT / "native-fixture.json").write_text(json.dumps(record, indent=2))
v1 = deb("3.0", "#!/bin/sh\nset -eu\necho WAIT_READY\nsleep 60\n")
v2 = deb("4.0", "#!/bin/sh\nset -eu\necho CONFIGURED_VERSION_4\n")
environment = {**os.environ, "UNIFIER_HOME": str(HOME), "LC_ALL": "C", "LANG": "C",
               "PYTHONDONTWRITEBYTECODE": "1"}
log = OUT / "interrupted-native.log"
with log.open("w") as stream:
    process = subprocess.Popen([str(ROOT / "bin/unifier"), "pkg", "install", "interrupt-debian",
        "--file", str(v1), "--package", "unifier-interrupt-probe", "--yes", "--no-repair-deps"],
        env=environment, stdout=stream, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + 180
        while "WAIT_READY" not in log.read_text() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.1)
        if "WAIT_READY" not in log.read_text():
            raise RuntimeError("package did not reach postinst fixture; inspect log")
        during = service.transactions_for("interrupt-debian")[-1]
        assert during["phase"] == "running" and during["native"][-1]["guest"]
        (OUT / "during-native.json").write_text(json.dumps(during, indent=2))
        process.send_signal(signal.SIGTERM)
        exit_code = process.wait(timeout=40)
        txn = service.transactions_for("interrupt-debian")[-1]
        (OUT / "interrupted-transaction.json").write_text(json.dumps(txn, indent=2))
        assert exit_code == 143, exit_code
        assert txn["native"][-1]["cleanup"] == "gone", txn
        assert txn["phase"] == "needs-recovery", txn
        assert "Another process has locked" not in txn["database_audit"]["stdout_tail"], txn
        refused = command("pkg", "remove", "interrupt-debian", "unifier-interrupt-probe", "--yes")
        assert refused.returncode != 0 and "pkg recover" in refused.stderr
        recovery = command("pkg", "recover", "interrupt-debian")
        assert recovery.returncode != 0 and "database needs repair" in recovery.stderr
        # This fixture's script can be replayed after removing only its test
        # sleep; it contains no business data or third-party maintenance code.
        command("env", "start", "interrupt-debian", check=True)
        command("env", "run", "interrupt-debian", "--", "/bin/sh", "-c",
                "sed -i '/sleep 60/d' /var/lib/dpkg/info/unifier-interrupt-probe.postinst; dpkg --configure -a", check=True)
        command("pkg", "recover", "interrupt-debian", check=True)
        upgraded = command("pkg", "install", "interrupt-debian", "--file", str(v2),
                           "--package", "unifier-interrupt-probe", "--yes", "--no-repair-deps", "--json", check=True)
        assert json.loads(upgraded.stdout)["phase"] == "completed"
        command("pkg", "remove", "interrupt-debian", "unifier-interrupt-probe", "--yes", "--json", check=True)
        command("pkg", "install", "interrupt-debian", "--file", str(v2), "--package",
                "unifier-interrupt-probe", "--yes", "--no-repair-deps", "--json", check=True)
        v3 = deb("5.0", "#!/bin/sh\nset -eu\nprintf 'RESIZE_READY\\n' >/dev/tty\nIFS= read -r answer </dev/tty\nstty size </dev/tty >/tmp/unifier-resize-size\n")
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
        terminal = subprocess.Popen([str(ROOT / "bin/unifier"), "pkg", "install", "interrupt-debian",
            "--file", str(v3), "--package", "unifier-interrupt-probe", "--interactive", "--no-repair-deps"],
            stdin=slave, stdout=slave, stderr=slave, env=environment, start_new_session=True)
        os.close(slave)
        data, answered, confirmed = b"", False, False
        try:
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                if select.select([master], [], [], .1)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    data += chunk
                if b"Do you want to continue?" in data and not confirmed:
                    os.write(master, b"y\n")
                    confirmed = True
                if b"RESIZE_READY" in data and not answered:
                    fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 38, 120, 0, 0))
                    terminal.send_signal(signal.SIGWINCH)
                    time.sleep(.5)
                    os.write(master, b"ok\n")
                    answered = True
                if terminal.poll() is not None:
                    break
            assert terminal.wait(timeout=10) == 0, data[-3000:]
            assert answered
        finally:
            (OUT / "apt-resize.terminal.log").write_bytes(data)
            if terminal.poll() is None:
                terminal.terminate()
                terminal.wait(timeout=40)
            os.close(master)
        size = command("env", "run", "interrupt-debian", "--", "cat", "/tmp/unifier-resize-size", check=True)
        assert size.stdout.strip() == "38 120", size
        command("env", "stop", "interrupt-debian", check=True)
        (OUT / "native-result.json").write_text(json.dumps({"passed": True,
            "exit_code": exit_code, "cleanup": txn["native"][-1]["cleanup"],
            "recovery_blocked_dirty_database": True, "upgrade_remove_reinstall": True,
            "apt_postinst_resize": size.stdout.strip(),
            "fixture_retained_stopped": record["runtime"]["container_id"]}, indent=2))
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=40)
