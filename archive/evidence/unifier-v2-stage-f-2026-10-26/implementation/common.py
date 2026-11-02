"""Shared helpers for the Stage F local performance/stress probes.

The module deliberately contains no product import at module import time.  A
 probe run first verifies the frozen fixture source snapshot, then inserts that
snapshot's ``src`` directory at the front of ``sys.path`` and imports the
product modules from it.  The live checkout is never imported by a probe.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
EVIDENCE = HERE.parent
ROOT = next(p for p in HERE.parents if (p / "src" / "multilinux").is_dir())
FIXTURES_DEFAULT = EVIDENCE / "fixtures.json"
PROJECT_DEFAULT = HERE / "stage-f-project"
WORKSPACES_DEFAULT = HERE / "workspaces"

TARGETS = ("debian13", "fedora44", "arch")
GUEST_IDS = {"debian13": "debian", "fedora44": "fedora", "arch": "arch"}
SESSION_USER = "1000:1000"
SMALL_FILE_COUNT = 2000
SMALL_FILE_BYTES = 4096
SEQUENTIAL_BYTES = 16 * 1024 * 1024
FIXTURE_LIMIT_BYTES = 128 * 1024 * 1024
MEMORY_FLOOR_BYTES = 700 * 1024 * 1024
PSI_MEMORY_FULL_ABORT = 50.0
PSI_MEMORY_SOME_ABORT = 90.0
ZERO_4096_SHA256 = hashlib.sha256(b"\0" * 4096).hexdigest()


class ProbeError(RuntimeError):
    """A probe failure that must propagate instead of becoming a pass."""


class GuardError(ProbeError):
    """The local safety guard stopped the run (low memory or catastrophic PSI)."""


def now_iso() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def monotonic_ns() -> int:
    return time.monotonic_ns()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def hash_tree(root: Path, *, include_suffixes=(".py", ".toml", ".json")) -> dict:
    root = Path(root)
    result = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if include_suffixes and path.suffix not in include_suffixes:
            continue
        result[str(path.relative_to(root))] = sha256_file(path)
    return result


def fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_new_json(path: Path, value) -> None:
    """Create a new JSON record; refuse to replace an earlier run's evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        pass


def atomic_write_json(path: Path, value) -> None:
    """Replace a status file inside the current run directory.

    Only progress/status files use this.  A previous run's run directory is
    never reused, and failure records are append-only.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    with open(temporary, "w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def append_jsonl(path: Path, value) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def read_json(path: Path):
    with open(path, encoding="utf-8") as stream:
        return json.load(stream)


def create_run_dir(path: Path) -> Path:
    """Create an exclusive run directory.  Existing evidence is never reused."""
    path = Path(path)
    if path.exists():
        raise ProbeError(f"refusing to reuse existing run directory: {path}")
    path.mkdir(parents=True)
    return path


def percentile(ordered: list, fraction: float):
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[rank - 1]


def stats(values) -> dict | None:
    numbers = sorted(float(value) for value in values
                     if isinstance(value, (int, float)) and not isinstance(value, bool))
    if not numbers:
        return None
    return {
        "count": len(numbers),
        "min": numbers[0],
        "p50": percentile(numbers, 0.50),
        "p95": percentile(numbers, 0.95),
        "max": numbers[-1],
        "mean": round(sum(numbers) / len(numbers), 3),
    }


def parse_meminfo_text(text: str) -> dict:
    wanted = {"MemTotal", "MemFree", "MemAvailable", "Cached", "SwapTotal",
              "SwapFree", "Shmem"}
    values = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if not separator or key not in wanted or not rest.split():
            continue
        try:
            values[key + "_bytes"] = int(rest.split()[0]) * 1024
        except ValueError:
            continue
    return values


def parse_pressure_text(text: str) -> dict:
    result = {}
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 2:
            continue
        item = {}
        for token in fields[1:]:
            key, _sep, raw = token.partition("=")
            if not key:
                continue
            try:
                item["total_us" if key == "total" else key] = (
                    int(raw) if key == "total" else float(raw))
            except ValueError:
                continue
        result[fields[0]] = item
    return result


def memory_state(proc_root: Path = Path("/proc")) -> dict:
    proc_root = Path(proc_root)
    state = {"meminfo": {}, "pressure": {}, "errors": []}
    try:
        state["meminfo"] = parse_meminfo_text((proc_root / "meminfo").read_text())
    except OSError as exc:
        state["errors"].append(f"meminfo: {type(exc).__name__}: {exc}")
    for kind in ("memory", "cpu", "io"):
        try:
            state["pressure"][kind] = parse_pressure_text(
                (proc_root / "pressure" / kind).read_text())
        except OSError as exc:
            state["errors"].append(f"pressure/{kind}: {type(exc).__name__}: {exc}")
    return state


def memory_guard_state(*, proc_root: Path = Path("/proc"), label: str = "") -> dict:
    """Return the local headroom decision without raising."""
    state = memory_state(proc_root)
    available = (state.get("meminfo") or {}).get("MemAvailable_bytes")
    memory_pressure = (state.get("pressure") or {}).get("memory") or {}
    full = (memory_pressure.get("full") or {}).get("avg10")
    some = (memory_pressure.get("some") or {}).get("avg10")
    result = {
        "label": label,
        "mem_available_bytes": available,
        "memory_psi_full_avg10": full,
        "memory_psi_some_avg10": some,
        "floor_bytes": MEMORY_FLOOR_BYTES,
        "full_abort": PSI_MEMORY_FULL_ABORT,
        "some_abort": PSI_MEMORY_SOME_ABORT,
        "checked_at": now_iso(),
        "errors": list(state.get("errors") or []),
    }
    reasons = []
    if available is None:
        reasons.append("MemAvailable is unreadable; refusing to assume headroom")
    elif available < MEMORY_FLOOR_BYTES:
        reasons.append(f"MemAvailable {available} is below {MEMORY_FLOOR_BYTES}")
    if isinstance(full, (int, float)) and full >= PSI_MEMORY_FULL_ABORT:
        reasons.append(f"memory PSI full avg10 {full} >= {PSI_MEMORY_FULL_ABORT}")
    if isinstance(some, (int, float)) and some >= PSI_MEMORY_SOME_ABORT:
        reasons.append(f"memory PSI some avg10 {some} >= {PSI_MEMORY_SOME_ABORT}")
    result["ok"] = not reasons
    result["reasons"] = reasons
    return result


def guard_memory(*, proc_root: Path = Path("/proc"), label: str = "") -> dict:
    """Fail closed when the agreed local headroom is gone.

    Missing MemAvailable is not treated as zero, but it is also not proof of
    headroom: the probe refuses to continue.  Missing PSI is recorded as
    UNKNOWN because older kernels may not expose it; a catastrophically high
    available PSI reading stops the run.
    """
    result = memory_guard_state(proc_root=proc_root, label=label)
    if not result["ok"]:
        raise GuardError(
            "local resource guard refused to continue: " + "; ".join(result["reasons"]))
    return result


def parse_proc_stat(pid: int, proc_root: Path = Path("/proc")) -> dict | None:
    try:
        text = (Path(proc_root) / str(pid) / "stat").read_text()
    except OSError:
        return None
    close = text.rfind(")")
    if close < 0:
        return None
    fields = text[close + 2:].split()
    if len(fields) < 20:
        return None
    try:
        return {
            "pid": pid,
            "comm": text[text.find("(") + 1:close],
            "state": fields[0],
            "ppid": int(fields[1]),
            "starttime": int(fields[19]),
        }
    except (ValueError, IndexError):
        return None


def pid_alive(pid, starttime) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    stat = parse_proc_stat(pid)
    return bool(stat and stat["state"] != "Z" and stat["starttime"] == starttime)


def proc_table(proc_root: Path = Path("/proc")) -> dict:
    table = {}
    try:
        entries = list(Path(proc_root).iterdir())
    except OSError:
        return table
    for entry in entries:
        if not entry.name.isdigit():
            continue
        stat = parse_proc_stat(int(entry.name), proc_root)
        if stat is not None:
            table[stat["pid"]] = stat
    return table


def descendants_of(roots, table: dict | None = None) -> set:
    table = table if table is not None else proc_table()
    children = {}
    for pid, stat in table.items():
        children.setdefault(stat["ppid"], []).append(pid)
    result = set()
    pending = [pid for pid in roots if isinstance(pid, int) and pid > 0 and pid in table]
    while pending:
        pid = pending.pop()
        if pid in result:
            continue
        result.add(pid)
        pending.extend(children.get(pid, []))
    return result


def cmdline_of(pid: int, proc_root: Path = Path("/proc")) -> str:
    try:
        raw = (Path(proc_root) / str(pid) / "cmdline").read_bytes()
    except OSError:
        return ""
    return raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()


def run_command(argv, *, timeout=120, cwd=None, env=None, input_text=None) -> dict:
    started = monotonic_ns()
    try:
        completed = subprocess.run(
            [str(item) for item in argv], cwd=str(cwd) if cwd else None,
            input=input_text, capture_output=True, text=True, timeout=timeout,
            env=env, check=False)
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": [str(item) for item in argv],
            "exit": None, "timed_out": True, "wall_ns": monotonic_ns() - started,
            "stdout": (exc.stdout or "")[-8000:] if isinstance(exc.stdout, str) else "",
            "stderr": (exc.stderr or "")[-8000:] if isinstance(exc.stderr, str) else "",
            "error": f"timed out after {timeout}s",
        }
    except OSError as exc:
        return {
            "argv": [str(item) for item in argv],
            "exit": None, "timed_out": False, "wall_ns": monotonic_ns() - started,
            "stdout": "", "stderr": "",
            "error": f"{type(exc).__name__}: {exc}",
        }
    return {
        "argv": [str(item) for item in argv],
        "exit": completed.returncode,
        "timed_out": False,
        "wall_ns": monotonic_ns() - started,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "error": None,
    }


def podman_json(*args, timeout=60):
    result = run_command(["podman", *args], timeout=timeout)
    if result["exit"] != 0:
        raise ProbeError(
            f"podman {' '.join(args)} failed: {result.get('error') or ''} "
            f"{result.get('stderr', '')[-2000:]}")
    try:
        return json.loads(result["stdout"])
    except json.JSONDecodeError as exc:
        raise ProbeError(f"podman {' '.join(args)} returned invalid JSON: {exc}") from exc


def podman_inspect(identifier: str, timeout=60) -> dict:
    rows = podman_json("inspect", identifier, timeout=timeout)
    if not isinstance(rows, list) or len(rows) != 1:
        raise ProbeError(f"podman inspect {identifier} did not return exactly one record")
    return rows[0]


def container_inventory() -> dict:
    rows = podman_json("ps", "-a", "--no-trunc", "--format=json")
    return {row["Id"]: row for row in rows}


def wait_until(predicate, *, timeout=15.0, interval=0.1, description="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    raise ProbeError(f"timed out waiting for {description}; last={last!r}")


def mountinfo_contains(path: str, mountinfo: Path = Path("/proc/self/mountinfo")) -> bool:
    try:
        text = mountinfo.read_text()
    except OSError:
        return False
    needle = str(path).replace("\\", "\\\\").replace(" ", "\\040")
    return any(line.split(" ")[4] == needle for line in text.splitlines())


def unescape_mountinfo(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def format_ns(value) -> int | None:
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def relative_ms(start_ns: int, end_ns: int) -> float:
    return round((end_ns - start_ns) / 1_000_000.0, 3)


def small_file_bytes(target: str, index: int) -> bytes:
    block = hashlib.sha256(f"stage-f:{target}:small:{index}".encode()).digest()
    return block * (SMALL_FILE_BYTES // len(block))


def sequential_chunk(target: str, index: int, size: int = 1024 * 1024) -> bytes:
    block = hashlib.sha256(f"stage-f:{target}:sequential:{index}".encode()).digest()
    return (block * (size // len(block) + 1))[:size]


def load_fixtures(path: Path | str = FIXTURES_DEFAULT) -> dict:
    path = Path(path)
    if not path.is_file():
        raise ProbeError(
            f"fixture manifest is missing: {path}; run prepare_fixtures.py first")
    data = read_json(path)
    if data.get("prepared") is not True:
        raise ProbeError(f"fixture manifest is not a completed prepare: {path}")
    targets = data.get("targets") or {}
    missing = [target for target in TARGETS if target not in targets]
    if missing:
        raise ProbeError(f"fixture manifest lacks targets: {', '.join(missing)}")
    return data


def verify_snapshot(fixtures: dict) -> None:
    """Prove the probes import the same source snapshot prepare recorded."""
    project = Path(fixtures["project"])
    snapshot = fixtures.get("source") or fixtures
    recorded = snapshot.get("source_sha256") or {}
    if not recorded:
        raise ProbeError("fixture manifest has no source snapshot hashes")
    actual = hash_tree(project / "src")
    if actual != recorded:
        changed = sorted(set(actual) | set(recorded))
        changed = [name for name in changed if actual.get(name) != recorded.get(name)]
        raise ProbeError(
            "fixture source snapshot changed since prepare: " + ", ".join(changed[:20]))
    for key in ("tools_sha256", "config_sha256"):
        expected = snapshot.get(key) or {}
        if not expected:
            raise ProbeError(f"fixture manifest has no {key}")
        base = project / ("tools" if key.startswith("tools") else "config")
        if hash_tree(base) != expected:
            raise ProbeError(f"fixture {key} changed since prepare")


def product(fixtures: dict, *, need_sessions=True, need_views=True):
    """Import the frozen product snapshot and return a small facade."""
    project = Path(fixtures["project"]).resolve()
    source = project / "src"
    if not source.is_dir():
        raise ProbeError(f"fixture source snapshot is missing: {source}")
    verify_snapshot(fixtures)
    sys.path.insert(0, str(project))
    sys.path.insert(0, str(source))
    import multilinux  # noqa: F401
    loaded = Path(multilinux.__file__).resolve()
    if not loaded.is_relative_to(source):
        raise ProbeError(
            f"product import escaped the frozen snapshot: {loaded} (expected under {source})")
    import tomllib
    from multilinux.runtime import PodmanRuntime
    from multilinux.service import EnvironmentService
    config = tomllib.loads((project / "config" / "targets.toml").read_text())
    lock = json.loads((project / "config" / "images.lock.json").read_text())
    service = EnvironmentService(project, PodmanRuntime(), config, lock)
    facade = {"project": project, "service": service, "multilinux": loaded}
    if need_sessions:
        from multilinux.project_sessions import ProjectSessions
        facade["sessions"] = ProjectSessions(project, service)
    if need_views:
        from multilinux.views import ViewManager
        facade["views"] = ViewManager(project, service)
    return facade


def fixture(fixtures: dict, target: str) -> dict:
    try:
        return fixtures["targets"][target]
    except KeyError as exc:
        raise ProbeError(f"fixture manifest has no target {target}") from exc


def environment_record(service, target_item: dict) -> dict:
    env_id = target_item["environment"]["id"]
    record = service.store.load(env_id)
    expected_cid = target_item["environment"]["runtime"]["container_id"]
    actual = (record.get("runtime") or {}).get("container_id")
    if actual != expected_cid:
        raise ProbeError(
            f"environment {env_id} container instance changed: {actual} != {expected_cid}")
    return record


def ensure_stopped(service, target_item: dict, *, timeout=20.0) -> dict:
    env_id = target_item["environment"]["id"]
    cid = target_item["environment"]["runtime"]["container_id"]

    def stopped():
        record = service.store.load(env_id)
        info = service.runtime.inspect(record["runtime"]["container_name"])
        if info is None:
            raise ProbeError(f"fixture container for {env_id} is missing")
        return info if (not info.running and info.container_id == cid) else None

    info = stopped()
    if info is None:
        info = wait_until(stopped, timeout=timeout,
                          description=f"{env_id} to be stopped")
    return info


def state_of(service, target_item: dict) -> dict:
    record = service.store.load(target_item["environment"]["id"])
    info = service.runtime.inspect(record["runtime"]["container_name"])
    if info is None:
        raise ProbeError(f"fixture container for {target_item['environment']['id']} is missing")
    return {
        "environment_id": record["id"],
        "record_state": record.get("runtime_state"),
        "container_id": info.container_id,
        "running": bool(info.running),
        "status": info.status,
        "started_at": info.started_at,
        "created_at": getattr(info, "created_at", None),
        "expected_container_id": target_item["environment"]["runtime"]["container_id"],
    }


def check_fixture_state(service, fixtures: dict, *, require_stopped=True) -> dict:
    rows = {}
    for target in TARGETS:
        item = fixture(fixtures, target)
        state = state_of(service, item)
        if state["container_id"] != state["expected_container_id"]:
            raise ProbeError(f"{target} fixture container changed")
        if require_stopped and state["running"]:
            raise ProbeError(f"{target} fixture is running before the probe stage")
        rows[target] = state
    return rows


def create_marker_dir(root: Path, label: str) -> Path:
    """New, never-reused directory for one wave's readiness/result markers."""
    directory = Path(root) / label
    if directory.exists():
        raise ProbeError(f"refusing to reuse marker directory: {directory}")
    directory.mkdir(parents=True)
    return directory


def sh_quote(value) -> str:
    return shlex.quote(str(value))


def tail(text, limit=4000) -> str:
    if not isinstance(text, str):
        return ""
    return text[-limit:]


class Progress:
    """Incremental run status plus append-only failure evidence."""

    def __init__(self, run_dir: Path, tool: str, **scope):
        self.run_dir = Path(run_dir)
        self.tool = tool
        self.started_at = now_iso()
        self.events_path = self.run_dir / "events.jsonl"
        self.failures_path = self.run_dir / "failures.jsonl"
        self.path = self.run_dir / "progress.json"
        self.state = {
            "tool": tool,
            "status": "running",
            "phase": "starting",
            "acceptance": "pending-primary-review",
            "started_at": self.started_at,
            "updated_at": self.started_at,
            "scope": scope,
            "checks": {},
            "error": None,
        }
        self.update("starting")

    def update(self, phase: str, **fields) -> dict:
        self.state.update(fields)
        self.state["phase"] = phase
        self.state["updated_at"] = now_iso()
        atomic_write_json(self.path, self.state)
        append_jsonl(self.events_path, {
            "at": self.state["updated_at"], "phase": phase,
            **{key: value for key, value in fields.items()
               if key in {"message", "target", "level", "wave", "status"}},
        })
        return self.state

    def check(self, name: str, passed: bool, detail=None) -> bool:
        self.state.setdefault("checks", {})[name] = {
            "passed": bool(passed), "detail": detail}
        atomic_write_json(self.path, self.state)
        return bool(passed)

    def failure(self, exc: BaseException, phase: str | None = None) -> None:
        record = {
            "at": now_iso(),
            "phase": phase or self.state.get("phase"),
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-12000:],
        }
        append_jsonl(self.failures_path, record)
        self.state["status"] = "failed"
        self.state["error"] = record["error"]
        self.state["updated_at"] = record["at"]
        atomic_write_json(self.path, self.state)

    def finish(self, *, status="passed", **fields) -> dict:
        self.state.update(fields)
        self.state["status"] = status
        self.state["phase"] = "finished"
        self.state["finished_at"] = now_iso()
        self.state["updated_at"] = self.state["finished_at"]
        atomic_write_json(self.path, self.state)
        return self.state


def write_summary(run_dir: Path, value: dict) -> None:
    atomic_write_json(Path(run_dir) / "summary.json", value)


def emit_result(result: dict) -> None:
    print(json.dumps(result, ensure_ascii=False, indent=2))
