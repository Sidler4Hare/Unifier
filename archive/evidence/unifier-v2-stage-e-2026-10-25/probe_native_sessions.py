"""True three-distro CLI streams, signals, shared leases and terminal sessions."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import termios
import time
import traceback

HERE = Path(__file__).resolve().parent
ROOT = next(p for p in HERE.parents if (p / "src/multilinux").is_dir())


def hashes(base):
    return {str(p.relative_to(base)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in (base / "src/multilinux").glob("*.py")}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--refresh-code", action="store_true")
    p.add_argument("--reset-settled-sessions", action="store_true",
                   help="archive only this experimental fixture's old sessions after guest/worker termination proof")
    a = p.parse_args()
    assert not a.output.exists()
    fixtures = json.loads((HERE / "fixtures.json").read_text())
    inventory_before = {row["Id"] for row in json.loads(subprocess.check_output(
        ["podman", "ps", "-a", "--no-trunc", "--format=json"], text=True))}
    project = Path(fixtures["project"])
    source = hashes(ROOT)
    if (project / "src").exists() and hashes(project) != source:
        assert a.refresh_code, "pass --refresh-code after preserving the previous experiment"
        private = ROOT / ".lab/agents/unifier-v2-stage-e-20261025"
        backup = private / ("fixture-source-before-" + str(time.time_ns()) + ".tar.gz")
        with tarfile.open(backup, "w:gz") as stream:
            stream.add(project / "src", arcname="src", filter=lambda item: None if "__pycache__" in Path(item.name).parts else item)
        shutil.rmtree(project / "src")
    if not (project / "src").exists():
        shutil.copytree(ROOT / "src", project / "src", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (project / "tools").mkdir(exist_ok=True)
    for name in ["ml.py", "unifier.py", "lab.py"]:
        shutil.copy2(ROOT / "tools" / name, project / "tools" / name)
    reset_backup = None
    if a.reset_settled_sessions:
        for row in fixtures["targets"].values():
            cid = row["environment"]["runtime"]["container_id"]
            info = json.loads(subprocess.check_output(["podman", "inspect", cid], text=True))[0]
            assert info["Id"] == cid and not info["State"]["Running"], "fixture still has a running guest"
        registry = project / ".lab/state/project-sessions/registry.json"
        if registry.exists():
            for row in json.loads(registry.read_text())["records"]:
                for key in ["worker", "child"]:
                    pid, expected_stamp = row.get(key + "_pid"), row.get(key + "_stamp")
                    if pid and expected_stamp:
                        try:
                            parts = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                        except FileNotFoundError:
                            continue
                        assert parts[0] == "Z" or int(parts[19]) != expected_stamp, "fixture execution still has a live owner"
        reset_backup = ROOT / ".lab/agents/unifier-v2-stage-e-20261025" / ("settled-fixture-sessions-" + str(time.time_ns()) + ".tar.gz")
        paths = [project / ".lab/state/project-sessions", project / ".lab/leases"]
        with tarfile.open(reset_backup, "w:gz") as stream:
            for directory in paths:
                if directory.exists():
                    stream.add(directory, arcname=str(directory.relative_to(project)))
        for directory in paths:
            if directory.exists():
                shutil.rmtree(directory)
    entry = project / "tools/unifier.py"
    report = {"passed": False, "source_sha256": source, "targets": {}, "commands": [], "cases": {},
              "scope": "isolated real containers, public CLI, actual pipes/signals/PTY; no host command interception"}
    report["settled_experimental_session_backup"] = str(reset_backup) if reset_backup else None
    clients = []

    def command(*args):
        return [sys.executable, str(entry), *args]

    def cli(*args, code=0, input=None, env=None, timeout=25):
        child = subprocess.run(command(*args), input=input, text=True, capture_output=True,
                               env=env, timeout=timeout)
        row = {"args": list(args), "exit": child.returncode,
               "stdout": child.stdout[-8000:], "stderr": child.stderr[-8000:]}
        report["commands"].append(row)
        assert child.returncode == code, row
        return child

    def json_cli(*args, **kw):
        return json.loads(cli(*args, **kw).stdout)

    def inspect(cid):
        return json.loads(subprocess.check_output(["podman", "inspect", cid], text=True, timeout=10))[0]

    def wait_for(fn, seconds=10):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            value = fn()
            if value:
                return value
            time.sleep(.05)
        raise RuntimeError("timed out waiting for a native session condition")

    def stopped(cid):
        wait_for(lambda: not inspect(cid)["State"]["Running"], 12)

    def start(*args):
        child = subprocess.Popen(command(*args), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        clients.append(child)
        return child

    try:
        for target, item in fixtures["targets"].items():
            row = {"passed": False}
            report["targets"][target] = row
            work = Path(item["workspace"])
            env_id = item["environment"]["id"]
            cid = item["environment"]["runtime"]["container_id"]
            roots = json_cli("root", "list")
            sr = next((r for r in roots if r["locator"] == str(work)), None)
            if sr is None:
                sr = json_cli("root", "add", str(work))
            alias = "native-" + target
            projects = json_cli("project", "list")
            proj = next((r for r in projects if r.get("alias") == alias), None)
            if proj is None:
                proj = json_cli("project", "add", "--root", sr["root_uuid"], "--environment", env_id, "--alias", alias)
            row.update(project_uuid=proj["project_uuid"], root_uuid=sr["root_uuid"], container_id=cid)
            before = inspect(cid)["State"]["StartedAt"]
            plan = json_cli("project", "plan", proj["project_uuid"])
            assert not inspect(cid)["State"]["Running"] and inspect(cid)["State"]["StartedAt"] == before
            row["plan_did_not_start"] = True
            guest_id = {"debian13": "debian", "fedora44": "fedora", "arch": "arch"}[target]
            argument = "普通 参数';% value"
            child = cli("project", "run", alias, "--", "./probe.sh", argument)
            assert child.stdout == f"parent:{guest_id}\nchild:{guest_id}:{argument}\n", child.stdout
            stopped(cid)
            child = cli("project", "run", alias, "--", "/bin/sh", "-c",
                        'cat; printf "stderr-marker\\n" >&2; exit 23', code=23, input="管道中文\n")
            assert child.stdout == "管道中文\n" and "stderr-marker\n" in child.stderr
            assert "stderr-marker" not in child.stdout
            stopped(cid)
            marker = work / "host-user-owned-output.txt"
            marker.unlink(missing_ok=True)
            child = cli("project", "run", alias, "--", "/bin/sh", "-c",
                        'cat absolute-os-release; printf owned > "$1"; printf "cwd:%s\\n" "$PWD"', "probe", str(marker))
            assert f"ID={guest_id}" in child.stdout and f"cwd:{work}" in child.stdout
            assert marker.read_text() == "owned"
            owner_matches = marker.stat().st_uid == os.getuid()
            row["observed_output_uid"] = marker.stat().st_uid
            row["expected_host_uid"] = os.getuid()
            stopped(cid)
            host_only = HERE / ("host-only-bin-" + target)
            host_only.mkdir(exist_ok=True)
            executable = host_only / "unifier-e-host-only-interpreter"
            sentinel = HERE / ("host-fallback-must-not-run-" + target)
            executable.write_text('#!/bin/sh\nprintf wrong > "$1"\n')
            executable.chmod(0o700)
            environment = dict(os.environ, PATH=str(host_only) + os.pathsep + os.environ["PATH"])
            cli("project", "run", alias, "--", executable.name, str(sentinel), code=127, env=environment)
            assert not sentinel.exists()
            cli("project", "run", alias, "--", "python3", "-c", "print('host fallback forbidden')", code=127)
            stopped(cid)
            row.update(passed=owner_matches, interpreter_and_subprocess_same_guest=True, argv_preserved=True,
                       stdin_stdout_stderr_and_exit_exact=True, same_path_write_host_uid=owner_matches,
                       guest_absolute_link_semantics=True, missing_command_and_python_no_host_fallback=True)
            print(json.dumps({"target": target, "basic": row["passed"], "owner_matches": owner_matches}), flush=True)

        item = fixtures["targets"]["debian13"]
        work, cid = Path(item["workspace"]), item["environment"]["runtime"]["container_id"]
        alias = "native-debian13"
        marker_a, marker_b = work / "shared-A", work / "shared-B"
        for f in [marker_a, marker_b]:
            f.unlink(missing_ok=True)
        a_proc = start("project", "run", alias, "--", "/bin/sh", "-c",
                       'printf READY > "$1"; sleep 20; printf CANCELED_COMMAND_CONTINUED > "$1"', "A", str(marker_a))
        wait_for(marker_a.exists)
        started_at = inspect(cid)["State"]["StartedAt"]
        b_proc = start("project", "run", alias, "--", "/bin/sh", "-c",
                       'printf READY > "$1"; sleep 4; printf OTHER_SESSION_COMPLETED > "$1"', "B", str(marker_b))
        wait_for(marker_b.exists)
        leases = json.loads((project / ".lab/leases" / (item["environment"]["id"] + ".json")).read_text())
        assert len(leases["leases"]) == 2
        assert inspect(cid)["State"]["StartedAt"] == started_at
        a_proc.send_signal(signal.SIGTERM)
        out_a, err_a = a_proc.communicate(timeout=12)
        assert a_proc.returncode == 143, (a_proc.returncode, out_a, err_a)
        assert inspect(cid)["State"]["Running"] and b_proc.poll() is None
        out_b, err_b = b_proc.communicate(timeout=15)
        assert b_proc.returncode == 0, (out_b, err_b)
        assert marker_b.read_text() == "OTHER_SESSION_COMPLETED"
        assert marker_a.read_text() == "READY"
        stopped(cid)
        report["cases"]["shared-session-TERM"] = {"passed": True, "canceled_exit": 143,
            "other_session_completed": True, "same_CID_and_start": True, "two_live_leases": True}

        # Explicitly started environments stay running after a session exits.
        cli("env", "start", item["environment"]["id"])
        manual_start = inspect(cid)["State"]["StartedAt"]
        cli("project", "run", alias, "--", "/bin/sh", "-c", "exit 15", code=15)
        assert inspect(cid)["State"]["Running"]
        assert inspect(cid)["State"]["StartedAt"] == manual_start
        cli("env", "stop", item["environment"]["id"])
        report["cases"]["manual-prestart-and-voluntary-exit15"] = {
            "passed": True, "manually_started_container_preserved": True, "voluntary_exit15_unchanged": True}

        # An ordinary shell background child may be awaited or explicitly
        # cleaned, but cannot continue without a lease after its session exits.
        background, blocker = work / "background-child", work / "background-blocker"
        for f in [background, blocker]:
            f.unlink(missing_ok=True)
        block = start("project", "run", alias, "--", "/bin/sh", "-c",
                      'printf READY > "$1"; sleep 4', "blocker", str(blocker))
        wait_for(blocker.exists)
        begin = time.monotonic()
        cli("project", "run", alias, "--", "/bin/sh", "-c",
            '(sleep 1; printf COMPLETED > "$1") &', "background", str(background))
        duration = time.monotonic() - begin
        already_done = background.exists()
        time.sleep(1.2)
        assert already_done or not background.exists(), "background guest continued after session lease release"
        bo, be = block.communicate(timeout=10)
        assert block.returncode == 0, (bo, be)
        stopped(cid)
        report["cases"]["background-child-covered-or-cleaned"] = {
            "passed": True, "finished_before_session_return": already_done,
            "session_seconds": duration, "no_untracked_child_after_return": True}

        # Kill both host supervision layers while the actual guest runs.
        # Recovery must retain proof/lease, and another session exiting must
        # not stop that unresolved guest. Then use targeted explicit cancel.
        orphan_mark = work / ("orphan-" + str(time.time_ns()))
        peer_mark = work / ("orphan-peer-" + str(time.time_ns()))
        orphan = start("project", "run", alias, "--", "/bin/sh", "-c",
                       'printf READY > "$1"; sleep 20; printf DONE > "$1"', "orphan", str(orphan_mark))
        wait_for(orphan_mark.exists)
        registry_path = project / ".lab/state/project-sessions/registry.json"
        def orphan_record():
            rows = json.loads(registry_path.read_text())["records"]
            return next((r for r in reversed(rows) if str(orphan_mark) in r.get("argv", [])
                         and r.get("guest_pid") and r.get("worker_pid") and r.get("child_pid")), None)
        record = wait_for(orphan_record)
        peer = start("project", "run", alias, "--", "/bin/sh", "-c",
                     'printf READY > "$1"; sleep 2; printf DONE > "$1"', "peer", str(peer_mark))
        wait_for(peer_mark.exists)
        worker_pid, proxy_pid = record["worker_pid"], record["child_pid"]
        assert record["session_uuid"].encode() in Path(f"/proc/{worker_pid}/cmdline").read_bytes()
        assert b"podman" in Path(f"/proc/{proxy_pid}/cmdline").read_bytes()
        os.kill(worker_pid, signal.SIGKILL)
        os.killpg(proxy_pid, signal.SIGKILL)
        recovered = json_cli("project", "session", "recover", record["session_uuid"])
        assert recovered[0]["action"].startswith("unresolved-guest-"), recovered
        po, pe = peer.communicate(timeout=12)
        assert peer.returncode == 0 and peer_mark.read_text() == "DONE", (po, pe)
        assert inspect(cid)["State"]["Running"], "another session stopped an unresolved live guest"
        lease_record = json.loads((project / ".lab/leases" / (item["environment"]["id"] + ".json")).read_text())
        assert record["lease_token"] in lease_record["leases"]
        assert orphan_mark.read_text() == "READY"
        cancelled = json_cli("project", "session", "cancel", record["session_uuid"], "--signal", "TERM")
        assert cancelled.get("action") == "cancelled", cancelled
        orphan.communicate(timeout=10)
        stopped(cid)
        assert orphan_mark.read_text() == "READY"
        json_cli("project", "session", "recover", record["session_uuid"])
        json_cli("project", "session", "recover", record["session_uuid"])
        report["cases"]["dead-worker-and-proxy-retain-live-guest"] = {
            "passed": True, "unresolved_lease_survived_other_session_exit": True,
            "explicit_targeted_cancel_confirmed": True, "recovery_twice": True}

        # A short-lived launcher dying does not revoke its live worker lease.
        marker = work / "launcher-SIGKILL-completed"
        marker.unlink(missing_ok=True)
        proc = start("project", "run", alias, "--", "/bin/sh", "-c",
                     'printf READY > "$1"; sleep 1; printf DONE > "$1"', "probe", str(marker))
        wait_for(marker.exists)
        proc.kill()
        proc.communicate(timeout=5)
        wait_for(lambda: marker.read_text() == "DONE")
        stopped(cid)
        json_cli("project", "session", "recover")
        json_cli("project", "session", "recover")
        assert not json.loads((project / ".lab/leases" / (item["environment"]["id"] + ".json")).read_text())["leases"]
        report["cases"]["launcher-SIGKILL"] = {"passed": True, "worker_finished_guest": True,
                                                "final_stopped_and_no_lease": True}

        # A real PTY, control-C, then another unprefixed command in the shell.
        master, slave = pty.openpty()
        old_attrs = termios.tcgetattr(slave)
        def control_terminal():
            os.setsid()
            fcntl.ioctl(0, termios.TIOCSCTTY, 0)
        proc = subprocess.Popen(command("project", "shell", alias), stdin=slave,
                                stdout=slave, stderr=slave, preexec_fn=control_terminal)
        clients.append(proc)
        transcript = bytearray()
        def read_until(marker, seconds=12):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                ready, _, _ = select.select([master], [], [], .1)
                if ready:
                    try:
                        data = os.read(master, 65536)
                    except OSError:
                        data = b""
                    transcript.extend(data)
                    if marker in transcript:
                        return
                if proc.poll() is not None:
                    break
            raise RuntimeError("PTY output missing marker: " + repr(marker) + " " + repr(transcript[-1800:]))
        try:
            os.write(master, b"printf '\\nE_PTY_READY\\n'; sleep 20\n")
            read_until(b"E_PTY_READY\r\n")
            time.sleep(.2)
            os.write(master, b"\x03")
            time.sleep(.3)
            os.write(master, b"printf '\\nE_AFTER_CTRL_C:%s\\n' \"$?\"; exit 17\n")
            read_until(b"E_AFTER_CTRL_C:130\r\n")
            assert proc.wait(timeout=12) == 17
            assert termios.tcgetattr(slave) == old_attrs, "terminal attributes not restored"
        finally:
            os.close(master)
            os.close(slave)
        stopped(cid)
        report["cases"]["interactive-PTY-Ctrl-C"] = {"passed": True, "shell_survived_interrupt": True,
            "foreground_signal_exit": 130, "shell_exit": 17, "termios_restored": True,
            "transcript": transcript.decode(errors="replace")[-5000:]}
        inventory = json.loads(subprocess.check_output(["podman", "ps", "-a", "--format=json"], text=True))
        assert {row["Id"] for row in inventory} == inventory_before
        assert all(row["passed"] for row in report["targets"].values()), "project output ownership mismatch"
        report["source_unchanged"] = hashes(ROOT) == source
        assert report["source_unchanged"] and hashes(project) == source
        report["passed"] = True
    except Exception:
        report["error"] = traceback.format_exc()[-10000:]
    finally:
        for proc in clients:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        if not report["passed"]:
            for item in fixtures["targets"].values():
                cid = item["environment"]["runtime"]["container_id"]
                if inspect(cid)["State"]["Running"]:
                    subprocess.run(["podman", "stop", "--time", "2", cid], capture_output=True, timeout=10)
        with a.output.open("x") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    print(json.dumps({"passed": report["passed"], "targets": len(report["targets"]),
                      "cases": len(report["cases"]), "output": str(a.output)}))
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
