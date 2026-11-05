"""Conservative opt-in runtime for isolated tests, never daily deployments.

The caller starts controllers in unifier-tests.slice. Containers join that
same aggregate cgroup and also have individual limits. This adapter refuses
desktop sockets, devices and host IPC: GUI tests must use a guest-local display.
It does not change the normal product runtime or host desktop configuration.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading

from unifier.errors import UnifierError
from unifier.atomic_io import atomic_replace
from unifier.runtime import PodmanRuntime, creation_argv


MIB = 1024 * 1024
SLICE = "unifier-tests.slice"
CONTAINER_MEMORY = 768 * MIB
CONTAINER_SWAP = 128 * MIB
HOST_RESERVE = 2048 * MIB
SLICE_MEMORY = 1024 * MIB
SLICE_TASKS = 256


def host_available() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    raise UnifierError("MemAvailable is unknown; refusing a real test")


def require_headroom(available=None) -> None:
    available = host_available() if available is None else available
    if available < HOST_RESERVE + CONTAINER_MEMORY:
        raise UnifierError("test budget refused: reserve 2 GiB for the host plus "
                           "768 MiB for the test container")


def require_slice() -> str:
    if os.environ.get("GOMAXPROCS") not in {"1", "2"}:
        raise UnifierError("guarded test controllers must set GOMAXPROCS=2; "
                           "unbounded Podman Go workers can exhaust the task budget")
    result = subprocess.run(
        ["systemctl", "--user", "show", SLICE, "-p", "ActiveState",
         "-p", "ControlGroup", "-p", "MemoryMax", "-p", "MemorySwapMax",
         "-p", "TasksMax", "-p", "CPUQuotaPerSecUSec"],
        capture_output=True, text=True, timeout=5, check=True)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    try:
        bounded = (fields["ActiveState"] == "active"
                   and 0 < int(fields["MemoryMax"]) <= SLICE_MEMORY
                   and int(fields["MemorySwapMax"]) <= CONTAINER_SWAP
                   and 0 < int(fields["TasksMax"]) <= SLICE_TASKS
                   and fields["CPUQuotaPerSecUSec"] == "1s")
    except (KeyError, ValueError):
        bounded = False
    group = fields.get("ControlGroup", "")
    if not bounded or not group.endswith("/" + SLICE):
        raise UnifierError("test slice is absent or its aggregate limits are unverified")
    if group + "/" not in Path("/proc/self/cgroup").read_text():
        raise UnifierError("the test controller itself must run inside " + SLICE)
    return group


class GuardedPodmanRuntime(PodmanRuntime):
    """Only operate on one explicit private test root and bounded containers."""

    def __init__(self, root, *args, **kwargs):
        self.test_root = Path(root).absolute()
        private = Path(__file__).resolve().parents[1] / ".lab"
        if (not self.test_root.is_relative_to(private)
                or self.test_root.resolve() != self.test_root):
            raise UnifierError("guarded tests require a real path under this checkout's .lab")
        self.abort_latch = self.test_root.parent / ".guard-abort.json"
        super().__init__(*args, **kwargs)

    def require_not_aborted(self):
        if self.abort_latch.exists() or self.abort_latch.is_symlink():
            raise UnifierError("this fixture was aborted; review " + str(self.abort_latch)
                               + " before an explicit new test attempt")

    def latch_abort(self, result):
        # Shared mounts are below this parent, never the parent itself. Guest
        # applications cannot remove this host-side interlock to restart.
        atomic_replace(self.abort_latch, json.dumps(result).encode(), prefix=".guard-abort-")

    def create(self, spec):
        self.require_not_aborted()
        if spec.project != str(self.test_root):
            raise UnifierError("test creation names a different project")
        if (spec.session_mounts or spec.devices or spec.ipc != "private"
                or spec.selinux_label != "default"):
            raise UnifierError("guarded test refuses host desktop sockets, devices or IPC")
        for source, target in spec.mounts:
            path = Path(source)
            if (not path.is_relative_to(self.test_root.parent)
                    or path.resolve() != path or target != source):
                raise UnifierError("guarded shares must stay in this test fixture")
        require_headroom()
        require_slice()
        self._ensure_rootless()
        if self.inspect(spec.name) is not None:
            raise UnifierError("guarded test will not reuse an existing container")
        argv = creation_argv(spec)
        argv[2:2] = ["--ipc=private", "--ulimit=core=0:0",
                     "--cgroup-parent=" + SLICE, "--memory=768m",
                     "--memory-swap=896m", "--cpus=1", "--pids-limit=192"]
        result = self.runner(argv, capture=True, timeout=60)
        if result.returncode or not result.stdout.strip():
            raise UnifierError("guarded container creation failed: " + result.stderr[-1000:])
        cid = result.stdout.strip()
        try:
            self.verify_limits(cid, expected_id=cid)
        except BaseException:
            # Only the newly returned CID is eligible for this failed-create cleanup.
            self.runner(["podman", "rm", cid], capture=True, timeout=20)
            raise
        return cid

    def verify_limits(self, name, *, expected_id=None):
        result = self.runner(["podman", "inspect", name], capture=True, timeout=15)
        if result.returncode:
            raise UnifierError("cannot verify guarded container limits")
        try:
            info, = json.loads(result.stdout)
            config = info["HostConfig"]
            labels = info["Config"]["Labels"]
            valid = (labels["multilinux.project"] == str(self.test_root)
                     and labels["multilinux.role"] == "environment"
                     and config["IpcMode"] == "private"
                     and config["PidMode"] == "private"
                     and config.get("Privileged") is False
                     and not config.get("Devices")
                     and all(Path(m["Source"]).is_relative_to(self.test_root.parent)
                             for m in info["Mounts"])
                     and config["CgroupParent"] == SLICE
                     and 0 < config["Memory"] <= CONTAINER_MEMORY
                     and config["MemorySwap"] == config["Memory"] + CONTAINER_SWAP
                     and 0 < config["PidsLimit"] <= 192
                     and 0 < config["NanoCpus"] <= 1_000_000_000
                     and (expected_id is None or info["Id"] == expected_id))
        except (KeyError, TypeError, ValueError):
            valid = False
        if not valid:
            raise UnifierError("container identity or hard test limits do not match")
        return info

    def start(self, name, *, project, env_id):
        self.require_not_aborted()
        if project != str(self.test_root):
            raise UnifierError("guarded runtime refuses another installation")
        require_headroom()
        parent = require_slice()
        before = self.verify_limits(name)
        super().start(name, project=project, env_id=env_id)
        try:
            after = self.verify_limits(name, expected_id=before["Id"])
            pid = after["State"]["Pid"]
            actual = Path(f"/proc/{pid}/cgroup").read_text()
            if parent + "/" not in actual:
                raise UnifierError("container did not join the aggregate test cgroup")
        except BaseException:
            super().stop(before["Id"], project=project, env_id=env_id)
            raise


class HostReserveMonitor:
    """Emergency stop of one exact test CID if host headroom disappears.

    This is a fixture abort, not a successful application exit. An interrupted
    package transaction must retain its recovery journal. Identity and limits
    are checked again immediately before the stop.
    """

    def __init__(self, runtime, cid, *, project, env_id, receipt):
        self.runtime, self.cid = runtime, cid
        self.project, self.env_id, self.receipt = project, env_id, Path(receipt)
        self.done = threading.Event()
        self.aborted = False
        self.thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self):
        low = 0
        while not self.done.wait(1):
            available = None
            result = None
            try:
                available = host_available()
                low = low + 1 if available < HOST_RESERVE else 0
                if low < 3:
                    continue
                result = {"status": "ABORTED_HOST_RESERVE", "container_id": self.cid,
                          "available_bytes": available, "reserve_bytes": HOST_RESERVE}
            except Exception as exc:
                # Losing the metric also loses permission to keep consuming the
                # test budget. Only the separately reverified fixture may stop.
                result = {"status": "ABORTED_MONITOR_FAILURE", "container_id": self.cid,
                          "error": str(exc)}
            try:
                self.runtime.latch_abort(result)
            except Exception as exc:
                result["latch_error"] = str(exc)
            try:
                self.runtime.verify_limits(self.cid, expected_id=self.cid)
                self.runtime.stop(self.cid, project=self.project, env_id=self.env_id)
                result["fixture_stopped"] = True
            except Exception as exc:
                result["fixture_stopped"] = False
                result["stop_error"] = str(exc)
            self.aborted = True
            self.receipt.write_text(json.dumps(result, indent=2))
            return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.done.set()
        self.thread.join(20)
        if self.thread.is_alive():
            raise RuntimeError("test reserve monitor did not settle")
        if self.aborted:
            raise UnifierError("the guarded test was aborted; inspect " + str(self.receipt))
