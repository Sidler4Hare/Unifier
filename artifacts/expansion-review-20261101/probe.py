#!/usr/bin/env python3
"""Read installed sources and probe interruption in temporary local fixtures.

Does not start containers, invoke a package manager, or change product code.
"""
from pathlib import Path
import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time


ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests"), str(ROOT)]

from unifier.errors import UnifierError
from test_packages import PackageBoxes, make_env, package_service


class InterruptedBoxes(PackageBoxes):
    def package_exec(self, name, argv, **kwargs):
        if argv[:2] == ["apt-get", "install"]:
            self.installed.setdefault(kwargs["env_id"], set()).add("hello")
            raise UnifierError(
                "native package command timed out; inspect package state before retrying")
        return self.exec(name, argv, project=kwargs["project"],
                         env_id=kwargs["env_id"], workdir=None, env=kwargs["env"],
                         capture=True, timeout=kwargs["timeout"])


def source_identity():
    command = shutil.which("unifier")
    installed = Path(command).resolve().parents[1] if command else None
    names = ["views", "shortcuts", "project_sessions", "runtime", "packages",
             "service", "dependencies", "native_io"]
    files = {}
    for name in names:
        relative = Path("src/unifier") / (name + ".py")
        work = hashlib.sha256((ROOT / relative).read_bytes()).hexdigest()
        previous = installed / relative if installed else None
        old = hashlib.sha256(previous.read_bytes()).hexdigest() if previous and previous.exists() else None
        files[str(relative)] = {"worktree_sha256": work,
                                "installed_sha256": old, "matches": old == work}
    return {"command": command, "release": str(installed), "files": files}


def stale_inventory():
    with tempfile.TemporaryDirectory(prefix="unifier-review-inventory-") as temporary:
        runtime = InterruptedBoxes()
        service, _ = package_service(Path(temporary), runtime)
        make_env(service, runtime, "debian13", "probe")
        before = service.store.load("probe")
        transaction = service.install_packages("probe", ["hello"])
        after = service.store.load("probe")
        actual = runtime._inventory("probe", ["dpkg-query", "-W", "-f=${Package}\t${Version}\n"])
        return {"scope": "offline service fault injection; no real containers",
                "actual_package_present": "hello" in runtime.installed["probe"],
                "transaction_phase": transaction["phase"],
                "failure_phase": transaction["failure_phase"],
                "verification": transaction["verification"],
                "recorded_hash_unchanged": before["package_inventory_sha256"] == after["package_inventory_sha256"],
                "recorded_hash_matches_actual": after["package_inventory_sha256"] == hashlib.sha256(actual.encode()).hexdigest(),
                "revision_before": transaction["revision_before"],
                "revision_after": transaction["revision_after"],
                "native": transaction["native"], "error": transaction["native_error"]}


def termination():
    with tempfile.TemporaryDirectory(prefix="unifier-review-sigterm-") as temporary:
        directory = Path(temporary)
        marker, aborted = directory / "child.pid", directory / "aborted"
        child = "import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(60)" % str(marker)
        wrapper = """
from pathlib import Path
import sys
from unifier.native_io import run_native
run_native([sys.executable, '-c', %r], interactive=False, stream=False,
           timeout=30, abort=lambda: Path(%r).write_text('called'))
""" % (child, str(aborted))
        process = subprocess.Popen(
            [sys.executable, "-c", wrapper], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, start_new_session=True,
            env={**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"})
        child_pid = child_start = None
        try:
            deadline = time.monotonic() + 8
            while not marker.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            if not marker.exists():
                raise RuntimeError("local fixture did not start")
            child_pid = int(marker.read_text())
            child_start = Path(f"/proc/{child_pid}/stat").read_text().rsplit(") ", 1)[1].split()[19]
            process.terminate()
            code = process.wait(timeout=5)
            alive = Path(f"/proc/{child_pid}/stat").exists()
            return {"scope": "real local child process; no Podman or package manager",
                    "wrapper_exit_code": code, "child_survived_wrapper_sigterm": alive,
                    "abort_callback_called": aborted.exists(),
                    "child_pid": child_pid, "child_start": child_start,
                    "cleanup": "probe finally kills only its own child group after identity check"}
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if child_pid is not None and child_start is not None:
                stat = Path(f"/proc/{child_pid}/stat")
                if stat.exists() and stat.read_text().rsplit(") ", 1)[1].split()[19] == child_start:
                    os.killpg(child_pid, signal.SIGKILL)
            if process.stderr is not None:
                process.stderr.close()


print(json.dumps({"date": "2026-11-01", "installed_source": source_identity(),
                  "interrupted_inventory": stale_inventory(),
                  "sigterm_cleanup": termination()}, ensure_ascii=False, indent=2))
