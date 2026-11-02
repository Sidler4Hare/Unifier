#!/usr/bin/env python3
"""Stage F read-only product FUSE projection performance and semantics probe.

For each of the three stopped fixtures this runs the same workload on:

* the host ordinary direct bind path;
* the same path inside a live container bind (product runtime exec);
* the actual ``ViewManager`` read-only product FUSE projection.

It reports first-observed and warm distributions separately (page caches are
never dropped), checks byte correctness and projected absolute links, and
tests mmap, inotify and stale external replacement honestly.  An unsupported
or unobservable FUSE semantic is recorded as UNSUPPORTED/NO_EVENT, never as a
synthetic pass.  No latency SLO is asserted.
"""

from __future__ import annotations

import argparse
import ctypes
import errno
import json
import mmap
import os
from pathlib import Path
import select
import struct
import sys
import time
import traceback

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402


# ------------------------------------------------------------------ inotify
IN_NONBLOCK = 0o4000
IN_CLOEXEC = 0o2000000
IN_ALL_EVENTS = 0x00000FFF
# Exclude OPEN/ACCESS/CLOSE_NOWRITE: reading through a projection is not
# evidence that a backend modification was delivered to its watcher.
IN_CHANGE_EVENTS = 0x00000FCE
INOTIFY_EVENT = struct.Struct("<iIII")


class Inotify:
    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.inotify_init1.argtypes = [ctypes.c_int]
        self.libc.inotify_init1.restype = ctypes.c_int
        self.libc.inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p,
                                                ctypes.c_uint32]
        self.libc.inotify_add_watch.restype = ctypes.c_int
        self.libc.inotify_rm_watch.argtypes = [ctypes.c_int, ctypes.c_int]
        self.libc.inotify_rm_watch.restype = ctypes.c_int
        self.fd = self.libc.inotify_init1(IN_NONBLOCK | IN_CLOEXEC)
        if self.fd < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))

    def add(self, path: Path, mask: int = IN_ALL_EVENTS) -> int:
        descriptor = self.libc.inotify_add_watch(
            self.fd, os.fsencode(str(path)), mask)
        if descriptor < 0:
            code = ctypes.get_errno()
            raise OSError(code, os.strerror(code))
        return descriptor

    def read(self) -> list[dict]:
        events = []
        while True:
            try:
                data = os.read(self.fd, 65536)
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in {errno.EAGAIN, errno.EWOULDBLOCK}:
                    break
                raise
            if not data:
                break
            offset = 0
            while offset + INOTIFY_EVENT.size <= len(data):
                wd, mask, cookie, length = INOTIFY_EVENT.unpack_from(data, offset)
                offset += INOTIFY_EVENT.size
                name = data[offset:offset + length].split(b"\0", 1)[0]
                offset += length
                events.append({"wd": wd, "mask": mask, "cookie": cookie,
                               "name": name.decode("utf-8", "replace")})
        return events

    def close(self):
        if getattr(self, "fd", -1) >= 0:
            try:
                os.close(self.fd)
            finally:
                self.fd = -1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


# ---------------------------------------------------------------- workload
def parse_workload(stdout: str) -> list[dict]:
    rows = []
    for line in (stdout or "").splitlines():
        if not line.startswith("rep="):
            continue
        values = {}
        for token in line.split():
            key, separator, value = token.partition("=")
            if not separator:
                continue
            if key in {"stat_ns", "read_ns", "seq_ns", "count"}:
                try:
                    values[key] = int(value)
                except ValueError:
                    values[key] = value
            else:
                values[key] = value
        rows.append(values)
    return rows


def run_host_workload(dataset: Path, reps: int, expected: dict) -> dict:
    result = common.run_command(
        ["/bin/sh", str(dataset / "workload.sh"), str(dataset), str(reps),
         str(expected["small_file_count"]), expected["small_sha256"],
         expected["sequential_sha256"]],
        cwd=dataset.parent, timeout=180)
    return {
        "argv": result["argv"], "exit": result["exit"],
        "timed_out": result["timed_out"], "wall_ns": result["wall_ns"],
        "rows": parse_workload(result["stdout"]),
        "stderr": common.tail(result["stderr"]),
    }


def validate_workload(record: dict, reps: int, label: str) -> list[dict]:
    if record["exit"] != 0:
        raise common.ProbeError(
            f"{label}: workload exit={record['exit']} stderr={record['stderr']}")
    rows = record["rows"]
    if len(rows) != reps:
        raise common.ProbeError(f"{label}: expected {reps} reps, got {len(rows)}")
    for index, row in enumerate(rows):
        if row.get("count") != 2000:
            raise common.ProbeError(f"{label}: rep {index} count={row.get('count')}")
        if not all(isinstance(row.get(key), int) and row[key] >= 0
                   for key in ("stat_ns", "read_ns", "seq_ns")):
            raise common.ProbeError(f"{label}: rep {index} has invalid timing")
    return rows


# --------------------------------------------------------- view lifecycle
def view_registry_rows(facade, env_id: str) -> list[dict]:
    return [row for row in facade["views"].registry.list()
            if row["environment_id"] == env_id]


def verify_view_gone(facade, view: dict) -> dict:
    env_id = view["environment_id"]
    mountpoint = view["mountpoint"]
    checks = {
        "registry_empty": not view_registry_rows(facade, env_id),
        "not_mounted": not common.mountinfo_contains(mountpoint),
        "mountpoint_gone": not Path(mountpoint).exists(),
        "control_socket_gone": not Path(view["control_socket"]).exists(),
        "storage_socket_gone": not Path(view["storage_socket"]).exists(),
        "worker_gone": not common.pid_alive(view.get("worker_pid"), view.get("worker_stamp")),
        "broker_gone": not common.pid_alive(view.get("broker_pid"), view.get("broker_stamp")),
        "launcher_gone": not common.pid_alive(
            view.get("broker_launcher_pid"), view.get("broker_launcher_stamp")),
    }
    table = common.proc_table()
    matches = []
    for pid in table:
        command = common.cmdline_of(pid)
        if view["view_uuid"] in command or mountpoint in command:
            matches.append({"pid": pid, "cmdline": command[:500]})
    checks["no_process_references"] = not matches
    checks["process_references"] = matches
    journal = Path(view.get("storage_journal") or "")
    if journal.is_file():
        try:
            state = json.loads(journal.read_text()).get("state")
        except (OSError, json.JSONDecodeError):
            state = "UNREADABLE"
        checks["storage_journal_state"] = state
    else:
        checks["storage_journal_state"] = "absent"
    if checks["storage_journal_state"] in {"mounted", "mounting", "releasing",
                                           "release-failed"}:
        checks["registry_empty"] = False
    return checks


# ------------------------------------------------------------- semantics
def atomic_replace(path: Path, payload: bytes) -> dict:
    directory = path.parent
    temporary = directory / (".stagef-tmp-" + str(os.getpid()) + "-" + str(time.time_ns()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        common.fsync_directory(directory)
        return {"ok": True, "path": str(path), "bytes": len(payload),
                "sha256": common.sha256_bytes(payload)}
    except BaseException:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise


def semantic_tests(facade, fixtures: dict, target: str, view: dict,
                   mountpoint: Path, workspace: Path, dataset: Path,
                   item: dict) -> dict:
    fuse_dataset = mountpoint / dataset.relative_to("/")
    guest_hash = item["guest"]["os_release_sha256"]
    sample_files = item["dataset"]["sample_files"]
    result = {"target": target, "checks": {}}

    # Absolute link mapping: the raw target is the container path, while the
    # host-visible link text is rewritten inside this projection.
    absolute_container = dataset / "absolute-os-release"
    absolute_fuse = fuse_dataset / "absolute-os-release"
    expected_projected = str(Path(view["mountpoint"])) + "/etc/os-release"
    raw = os.readlink(absolute_fuse)
    result["absolute_link"] = {
        "raw_readlink": raw,
        "expected_projected": expected_projected,
        "raw_is_container_path": absolute_container.is_symlink(),
    }
    result["checks"]["absolute_link_projected"] = raw == expected_projected
    if raw == expected_projected:
        target_path = Path(raw)
        observed_hash = common.sha256_file(target_path)
        result["absolute_link"]["target_sha256"] = observed_hash
        result["absolute_link"]["target_bytes"] = target_path.stat().st_size
        result["checks"]["absolute_link_guest_bytes"] = observed_hash == guest_hash
    else:
        # Do not follow a raw absolute link from the host: that would silently
        # read the host /etc and is exactly the failure this test detects.
        result["checks"]["absolute_link_guest_bytes"] = False
    control = facade["views"].readlink_raw(view["view_uuid"], str(absolute_container))
    result["readlink_raw"] = control
    result["checks"]["readlink_raw_container_path"] = (
        control.get("raw") == "/etc/os-release"
        and control.get("projected") == expected_projected)

    relative_fuse = fuse_dataset / "relative-link"
    relative_raw = os.readlink(relative_fuse)
    expected_relative = str(fuse_dataset / "small" / "f0000")
    relative_control = facade["views"].readlink_raw(
        view["view_uuid"], str(dataset / "relative-link"))
    result["relative_link"] = {
        "projected_readlink": relative_raw,
        "expected_projected": expected_relative,
        "raw_metadata": relative_control,
    }
    result["checks"]["relative_link_text"] = (
        relative_raw == expected_relative
        and relative_control.get("raw") == "small/f0000"
        and relative_control.get("projected") == expected_relative)
    if result["checks"]["relative_link_text"]:
        observed = common.sha256_file(relative_fuse)
        result["relative_link"]["target_sha256"] = observed
        result["checks"]["relative_link_bytes"] = observed == sample_files.get("f0000")

    # mmap read is a real read-only capability test.  Write mappings are only
    # probed for refusal; no write is attempted through the projection.
    first = fuse_dataset / "small" / "f0000"
    mmap_result = {"read": None, "write_open": None, "write_mmap": None}
    try:
        descriptor = os.open(first, os.O_RDONLY | os.O_CLOEXEC)
        try:
            size = os.fstat(descriptor).st_size
            mapping = mmap.mmap(descriptor, size, access=mmap.ACCESS_READ)
            try:
                data = mapping[:size]
                mmap_result["read"] = {
                    "supported": True, "bytes": len(data),
                    "sha256": common.sha256_bytes(data),
                    "matches": common.sha256_bytes(data) == sample_files.get("f0000"),
                }
            finally:
                mapping.close()
        finally:
            os.close(descriptor)
    except OSError as exc:
        mmap_result["read"] = {"supported": False,
                               "errno": errno.errorcode.get(exc.errno, str(exc.errno)),
                               "error": str(exc)}
    try:
        descriptor = os.open(first, os.O_RDWR | os.O_CLOEXEC)
    except OSError as exc:
        mmap_result["write_open"] = {
            "refused": True, "errno": errno.errorcode.get(exc.errno, str(exc.errno)),
            "error": str(exc)}
    else:
        try:
            mapping = mmap.mmap(descriptor, os.fstat(descriptor).st_size,
                                access=mmap.ACCESS_WRITE)
        except (OSError, ValueError) as exc:
            mmap_result["write_mmap"] = {
                "refused": True, "error": f"{type(exc).__name__}: {exc}"}
        else:
            mapping.close()
            mmap_result["write_mmap"] = {
                "refused": False,
                "note": "writable mapping accepted; no write was attempted"}
        finally:
            os.close(descriptor)
    result["mmap"] = mmap_result
    result["checks"]["mmap_read_supported"] = bool(
        mmap_result["read"] and mmap_result["read"].get("supported")
        and mmap_result["read"].get("matches"))
    result["checks"]["write_open_refused"] = bool(
        mmap_result["write_open"] and mmap_result["write_open"].get("refused"))

    # Stale external replacement inside the authorized shared bind.  The file
    # is not in any rootfs.  Kernel FUSE attribute caching may briefly show old
    # metadata; the exact observation is recorded rather than asserted.
    stale_path = dataset / "stale.txt"
    stale_fuse = fuse_dataset / "stale.txt"
    try:
        before = stale_fuse.read_bytes()
        new_payload = ("stage-f-stale-v1-" + str(time.time_ns()) + "\n").encode()
        replaced = atomic_replace(stale_path, new_payload)
        immediate = stale_fuse.read_bytes()
        time.sleep(1.3)
        delayed = stale_fuse.read_bytes()
        observed_stat = stale_fuse.stat()
        result["stale_replacement"] = {
            "before_sha256": common.sha256_bytes(before),
            "new_sha256": replaced["sha256"],
            "immediate_sha256": common.sha256_bytes(immediate),
            "delayed_sha256": common.sha256_bytes(delayed),
            "delayed_matches_new": delayed == new_payload,
            "immediate_matches_new": immediate == new_payload,
            "delayed_size": observed_stat.st_size,
            "verdict": ("NEW_BYTES_AFTER_1.3S" if delayed == new_payload
                        else "STALE_AFTER_1.3S"),
        }
    except OSError as exc:
        result["stale_replacement"] = {
            "error": f"{type(exc).__name__}: {exc}",
            "verdict": "ERROR"}

    # Open-FD replacement semantics.  A caller that keeps an fd open while an
    # external writer replaces the authorized bind path gets either the old
    # inode (ordinary POSIX) or, if the backend reopens by path, the new bytes.
    # This is recorded as a limitation, not asserted away.
    mutable_fuse = fuse_dataset / "mutable.txt"
    mutable_host = dataset / "mutable.txt"
    try:
        descriptor = os.open(mutable_fuse, os.O_RDONLY | os.O_CLOEXEC)
        try:
            before_fd = os.read(descriptor, 4096)
            new_payload = ("stage-f-openfd-v1-" + str(time.time_ns()) + "\n").encode()
            replaced = atomic_replace(mutable_host, new_payload)
            os.lseek(descriptor, 0, os.SEEK_SET)
            after_fd = os.read(descriptor, 4096)
        finally:
            os.close(descriptor)
        time.sleep(1.3)
        reopened = mutable_fuse.read_bytes()
        result["open_fd_across_replace"] = {
            "before_sha256": common.sha256_bytes(before_fd),
            "new_sha256": replaced["sha256"],
            "same_fd_after_sha256": common.sha256_bytes(after_fd),
            "reopened_after_1.3s_sha256": common.sha256_bytes(reopened),
            "same_fd_preserved_old_inode": after_fd == before_fd,
            "reopened_matches_new": reopened == new_payload,
            "verdict": ("OLD_INODE_PRESERVED" if after_fd == before_fd
                        else "SAME_FD_READ_NEW_BYTES"),
        }
    except OSError as exc:
        result["open_fd_across_replace"] = {
            "error": f"{type(exc).__name__}: {exc}",
            "verdict": "ERROR"}

    # inotify: host bind watcher is the control; FUSE may legitimately be
    # unsupported because this product FUSE server does not emit notifications.
    notify_path = dataset / "notify.txt"
    notify_fuse = fuse_dataset / "notify.txt"
    try:
        host_watch = Inotify()
    except OSError as exc:
        result["inotify"] = {
            "verdict": "UNSUPPORTED",
            "error": f"inotify_init1: {type(exc).__name__}: {exc}"}
        return result
    fuse_watch = None
    try:
        host_watch.add(dataset, IN_CHANGE_EVENTS)
        try:
            fuse_watch = Inotify()
            fuse_watch.add(fuse_dataset, IN_CHANGE_EVENTS)
            fuse_add_error = None
        except OSError as exc:
            fuse_add_error = {
                "errno": errno.errorcode.get(exc.errno, str(exc.errno)),
                "error": str(exc)}
        atomic_replace(notify_path, b"stage-f-notify\n")
        deadline = time.monotonic() + 2.0
        host_events = []
        fuse_events = []
        fuse_verdict = ("UNSUPPORTED" if fuse_add_error else "NO_EVENT")
        while time.monotonic() < deadline:
            host_events.extend(host_watch.read())
            if fuse_watch is not None:
                fuse_events.extend(fuse_watch.read())
            if any(e.get('name') == notify_path.name for e in host_events) and (fuse_events or fuse_add_error):
                break
            time.sleep(0.05)
        host_events.extend(host_watch.read())
        if fuse_watch is not None:
            fuse_events.extend(fuse_watch.read())
        if fuse_events:
            fuse_verdict = "OBSERVED"
        result["inotify"] = {
            "host_watch_added": True,
            "host_watch_path": str(dataset),
            "fuse_watch_path": str(fuse_dataset),
            "watch_mask": IN_CHANGE_EVENTS,
            "host_events": host_events[:50],
            "host_event_count": len(host_events),
            "fuse_add_error": fuse_add_error,
            "fuse_events": fuse_events[:50],
            "fuse_event_count": len(fuse_events),
            "fuse_verdict": fuse_verdict,
            "note": ("The host bind watcher is the control. FUSE watch creation "
                     "success without an event is NO_EVENT, not a pass."),
        }
        result["checks"]["host_inotify_control_observed"] = any(
            event.get("name") == notify_path.name
            and event.get("mask", 0) & IN_CHANGE_EVENTS
            for event in host_events)
        result["checks"]["fuse_inotify_honest"] = fuse_verdict in {
            "OBSERVED", "UNSUPPORTED", "NO_EVENT"}
    except OSError as exc:
        result["inotify"] = {
            "verdict": "UNSUPPORTED",
            "error": f"{type(exc).__name__}: {exc}"}
    finally:
        host_watch.close()
        if fuse_watch is not None:
            fuse_watch.close()
    return result


# ---------------------------------------------------------------- summary
def summarize_projection(samples: list[dict], open_samples: list[dict]) -> dict:
    groups = {}
    for sample in samples:
        kind = sample["path_kind"]
        label = "first-observed" if sample["first_observed"] else "warm"
        for phase in ("stat", "read", "seq"):
            key = f"{kind}.{label}.{phase}_ms"
            groups.setdefault(key, []).append(sample[f"{phase}_ns"] / 1_000_000.0)
    metrics = {key: common.stats(values) for key, values in sorted(groups.items())}
    view_metrics = {}
    for field in ("open_ms", "acquire_stat_ms", "acquire_read_ms", "revoke_ms"):
        view_metrics[field] = common.stats(
            sample[field] for sample in open_samples if sample.get(field) is not None)
    return {
        "path_metrics_ms": metrics,
        "view_lifecycle_ms": view_metrics,
        "samples": samples,
        "view_cycles": open_samples,
        "notes": [
            "first-observed is the first access in a fresh view cycle, not a "
            "kernel-cold measurement; caches were never dropped.",
            "The workload measures stat(1) of 2,000 files, 4 KiB reads of all "
            "2,000 files with aggregate hash, and one 16 MiB read+hash.",
            "No latency SLO is asserted.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Measure the real read-only product FUSE projection against "
                    "the host direct bind and a live container bind.")
    parser.add_argument("--fixtures", type=Path, default=common.FIXTURES_DEFAULT)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cycles", type=int, default=5)
    parser.add_argument("--reps", type=int, default=5)
    parser.add_argument("--targets", default=",".join(common.TARGETS))
    args = parser.parse_args(argv)
    targets = [item.strip() for item in args.targets.split(",") if item.strip()]
    if not targets or any(target not in common.TARGETS for target in targets):
        raise common.ProbeError("targets must be a subset of debian13,fedora44,arch")
    if args.cycles < 1 or args.reps < 5:
        raise common.ProbeError("at least one view cycle and five workload reps are required")
    run_dir = common.create_run_dir(args.run_dir)
    progress = common.Progress(run_dir, "probe_projection",
                               cycles=args.cycles, reps=args.reps,
                               targets=targets)
    try:
        common.guard_memory(label="projection preflight")
        fixtures = common.load_fixtures(args.fixtures)
        facade = common.product(fixtures)
        service = facade["service"]
        views = facade["views"]
        common.check_fixture_state(service, fixtures, require_stopped=True)
        results = {}
        all_samples = []
        all_open = []
        for target in targets:
            item = common.fixture(fixtures, target)
            env_id = item["environment"]["id"]
            cid = item["environment"]["runtime"]["container_id"]
            workspace = Path(item["workspace"])
            dataset = workspace / "dataset"
            expected = item["dataset"]
            target_row = {"target": target, "container_id": cid,
                          "workspace": str(workspace), "samples": [],
                          "view_cycles": [], "semantics": None}

            progress.update("host-direct", target=target)
            host_record = run_host_workload(dataset, args.reps, expected)
            host_rows = validate_workload(host_record, args.reps,
                                          f"{target} host-direct")
            for index, row in enumerate(host_rows):
                sample = {
                    "target": target, "path_kind": "host-direct",
                    "first_observed": index == 0,
                    "stat_ns": row["stat_ns"], "read_ns": row["read_ns"],
                    "seq_ns": row["seq_ns"], "rep": index,
                    "wrapper_wall_ns": host_record["wall_ns"],
                }
                target_row["samples"].append(sample)
                all_samples.append(sample)

            progress.update("container-bind", target=target)
            service.start(env_id)
            try:
                running = common.state_of(service, item)
                if not running["running"] or running["container_id"] != cid:
                    raise common.ProbeError(
                        f"{target}: container did not start for the bind measurement")
                record = service.store.load(env_id)
                runtime = record["runtime"]
                command = ["/bin/sh", str(dataset / "workload.sh"),
                           str(dataset), str(args.reps),
                           str(expected["small_file_count"]),
                           expected["small_sha256"], expected["sequential_sha256"]]
                started = time.monotonic_ns()
                executed = service.runtime.exec(
                    runtime["container_name"], command,
                    project=runtime["project"], env_id=env_id,
                    workdir=str(workspace), env={}, capture=True, timeout=180,
                    user=common.SESSION_USER)
                wrapper_ns = time.monotonic_ns() - started
                container_record = {
                    "exit": executed.returncode, "timed_out": False,
                    "wall_ns": wrapper_ns,
                    "rows": parse_workload(executed.stdout),
                    "stderr": common.tail(executed.stderr),
                    "argv": command,
                }
                container_rows = validate_workload(
                    container_record, args.reps, f"{target} container-bind")
                for index, row in enumerate(container_rows):
                    sample = {
                        "target": target, "path_kind": "container-bind",
                        "first_observed": index == 0,
                        "stat_ns": row["stat_ns"], "read_ns": row["read_ns"],
                        "seq_ns": row["seq_ns"], "rep": index,
                        "wrapper_wall_ns": wrapper_ns,
                    }
                    target_row["samples"].append(sample)
                    all_samples.append(sample)
                # Absolute and relative link semantics inside the live guest.
                link_command = [
                    "/bin/sh", "-c",
                    'set -eu; printf "ABS=%s\\n" "$(readlink "$1")"; '
                    'printf "REL=%s\\n" "$(readlink "$2")"; '
                    'sha256sum /etc/os-release | awk \'{print "OSHASH="$1}\'',
                    "links", str(dataset / "absolute-os-release"),
                    str(dataset / "relative-link")]
                links = service.runtime.exec(
                    runtime["container_name"], link_command,
                    project=runtime["project"], env_id=env_id,
                    workdir=str(workspace), env={}, capture=True, timeout=30,
                    user=common.SESSION_USER)
                parsed_links = {}
                for line in (links.stdout or "").splitlines():
                    key, separator, value = line.partition("=")
                    if separator:
                        parsed_links[key] = value
                target_row["container_links"] = {
                    "exit": links.returncode, "stdout": links.stdout,
                    "stderr": common.tail(links.stderr),
                    "parsed": parsed_links,
                    "absolute_is_container_path": parsed_links.get("ABS") == "/etc/os-release",
                    "relative_is_container_path": parsed_links.get("REL") == "small/f0000",
                    "os_release_hash_matches": parsed_links.get("OSHASH")
                    == item["guest"]["os_release_sha256"],
                }
            finally:
                service.stop(env_id)

            for cycle in range(args.cycles):
                progress.update("view-cycle", target=target,
                                message=f"cycle {cycle + 1}/{args.cycles}")
                common.ensure_stopped(service, item)
                open_started = time.monotonic_ns()
                view = views.open(env_id)
                open_ns = time.monotonic_ns() - open_started
                if view.get("state") != "ready":
                    raise common.ProbeError(
                        f"{target} view state is {view.get('state')!r}, not ready")
                if (view.get("environment_binding") or {}).get("container_id") != cid:
                    raise common.ProbeError(f"{target} view bound another container")
                mountpoint = Path(view["mountpoint"])
                if not common.mountinfo_contains(str(mountpoint)):
                    raise common.ProbeError(f"{target} view is not mounted")
                fuse_dataset = mountpoint / dataset.relative_to("/")
                cycle_row = {
                    "target": target, "cycle": cycle, "open_ms": open_ns / 1_000_000.0,
                    "view_uuid": view["view_uuid"],
                    "mountpoint": str(mountpoint),
                }
                try:
                    # Acquire: first metadata and first 4 KiB read after open.
                    acquire_started = time.monotonic_ns()
                    first_path = fuse_dataset / "small" / "f0000"
                    os.stat(first_path)
                    acquire_stat_ns = time.monotonic_ns() - acquire_started
                    acquire_read_started = time.monotonic_ns()
                    with open(first_path, "rb") as stream:
                        first_bytes = stream.read(4096)
                    acquire_read_ns = time.monotonic_ns() - acquire_read_started
                    if common.sha256_bytes(first_bytes) != expected["sample_files"]["f0000"]:
                        raise common.ProbeError(f"{target} first FUSE read mismatch")
                    cycle_row.update({
                        "acquire_stat_ms": acquire_stat_ns / 1_000_000.0,
                        "acquire_read_ms": acquire_read_ns / 1_000_000.0,
                    })
                    for index in range(2):
                        # The same script is invoked through its FUSE path.
                        workload = common.run_command(
                            ["/bin/sh", str(fuse_dataset / "workload.sh"),
                             str(fuse_dataset), "1",
                             str(expected["small_file_count"]),
                             expected["small_sha256"], expected["sequential_sha256"]],
                            cwd=str(mountpoint), timeout=180)
                        parsed = parse_workload(workload["stdout"])
                        if workload["exit"] != 0 or len(parsed) != 1:
                            raise common.ProbeError(
                                f"{target} FUSE workload failed: exit={workload['exit']} "
                                f"stderr={common.tail(workload['stderr'])}")
                        row = parsed[0]
                        sample = {
                            "target": target, "path_kind": "fuse-projection",
                            "first_observed": index == 0,
                            "stat_ns": row["stat_ns"], "read_ns": row["read_ns"],
                            "seq_ns": row["seq_ns"], "rep": index,
                            "cycle": cycle, "wrapper_wall_ns": workload["wall_ns"],
                        }
                        target_row["samples"].append(sample)
                        all_samples.append(sample)
                    if cycle == 0:
                        target_row["semantics"] = semantic_tests(
                            facade, fixtures, target, view, mountpoint,
                            workspace, dataset, item)
                finally:
                    revoke_started = time.monotonic_ns()
                    views.revoke(view["view_uuid"])
                    revoke_ns = time.monotonic_ns() - revoke_started
                    cycle_row["revoke_ms"] = revoke_ns / 1_000_000.0
                    cycle_row["cleanup"] = verify_view_gone(facade, view)
                    if not all(value for key, value in cycle_row["cleanup"].items()
                               if key not in {"process_references", "storage_journal_state",
                                              "mountpoint_gone"}):
                        raise common.ProbeError(
                            f"{target} view cleanup incomplete: {cycle_row['cleanup']}")
                    target_row["view_cycles"].append(cycle_row)
                    all_open.append(cycle_row)
            results[target] = target_row

        summary = summarize_projection(all_samples, all_open)
        checks = {
            "all_targets_have_5_plus_fuse_reps": all(
                sum(1 for sample in results[target]["samples"]
                    if sample["path_kind"] == "fuse-projection") >= 5
                for target in targets),
            "host_and_container_reps": all(
                sum(1 for sample in results[target]["samples"]
                    if sample["path_kind"] == "host-direct") == args.reps
                and sum(1 for sample in results[target]["samples"]
                        if sample["path_kind"] == "container-bind") == args.reps
                for target in targets),
            "view_cleanup_clean": all(
                all(value for key, value in cycle["cleanup"].items()
                    if key not in {"process_references", "storage_journal_state",
                                   "mountpoint_gone"})
                for target in results.values()
                for cycle in target["view_cycles"]),
            "absolute_link_mapping": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "absolute_link_projected") and
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "absolute_link_guest_bytes")
                for target in targets),
            "relative_link_mapping": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "relative_link_text") and
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "relative_link_bytes")
                for target in targets),
            "container_absolute_link_control": all(
                results[target].get("container_links", {}).get(
                    "absolute_is_container_path")
                and results[target].get("container_links", {}).get(
                    "relative_is_container_path")
                and results[target].get("container_links", {}).get(
                    "os_release_hash_matches")
                for target in targets),
            "readlink_raw_container_path": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "readlink_raw_container_path")
                for target in targets),
            "mmap_read_supported": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "mmap_read_supported")
                for target in targets),
            "write_open_refused": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "write_open_refused")
                for target in targets),
            "host_inotify_control": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "host_inotify_control_observed")
                for target in targets),
            "fuse_semantics_honest": all(
                (results[target].get("semantics") or {}).get("checks", {}).get(
                    "fuse_inotify_honest")
                for target in targets),
        }
        status = "passed" if all(checks.values()) else "failed"
        for name, passed in checks.items():
            progress.check(name, passed)
        summary.update({
            "schema_version": 1,
            "status": status,
            "acceptance": "pending-primary-review",
            "checks": checks,
            "targets": results,
            "scope": (
                "Local rootless Podman product ViewManager FUSE projection, "
                "same-path authorized shared bind, stopped environments. "
                "No generic SFTP FUSE and no cross-host claim."),
            "started_at": progress.started_at,
            "finished_at": common.now_iso(),
        })
        if status != "passed":
            progress.failure(common.ProbeError("one or more projection checks failed"))
        common.write_summary(run_dir, summary)
        progress.finish(status=status, checks=checks)
        print(json.dumps({
            "status": status, "run_dir": str(run_dir), "checks": checks,
            "view_lifecycle_ms": summary["view_lifecycle_ms"],
        }, ensure_ascii=False))
        return 0 if status == "passed" else 1
    except BaseException as exc:
        progress.failure(exc)
        common.write_summary(run_dir, {
            "schema_version": 1, "status": "failed",
            "acceptance": "pending-primary-review",
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc()[-12000:],
            "started_at": progress.started_at, "finished_at": common.now_iso(),
        })
        raise


if __name__ == "__main__":
    raise SystemExit(main())
