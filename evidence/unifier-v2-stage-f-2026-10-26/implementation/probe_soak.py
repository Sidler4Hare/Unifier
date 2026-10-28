#!/usr/bin/env python3
"""Bounded Stage F read-cache / watcher soak on the real product FUSE view.

A 180-300 second local soak repeatedly stats and reads the 2,000-file fixture
through the read-only product view while a host writer atomically replaces a
4096-byte file inside the authorized shared bind every second.  The host bind
inotify watcher is the control; the product FUSE watcher is reported as
OBSERVED, UNSUPPORTED or NO_EVENT without pretending success.  Resource
sampling runs once per second in a separate thread.

This is explicitly a short bounded soak.  Page caches are not dropped and no
24-hour or latency-SLO claim is made.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import threading
import time
import traceback

HERE = Path(__file__).resolve().parent
sys_path = str(HERE)
import sys
sys.path.insert(0, sys_path)
import common  # noqa: E402
from resource_sampler import ResourceSampler  # noqa: E402
from probe_projection import Inotify, IN_CHANGE_EVENTS, atomic_replace, verify_view_gone  # noqa: E402


class SoakWriter(threading.Thread):
    def __init__(self, path: Path, *, interval: float = 1.0):
        super().__init__(name="stage-f-soak-writer", daemon=True)
        self.path = Path(path)
        self.interval = max(0.2, interval)
        self.stop_event = threading.Event()
        self.lock = threading.Lock()
        self.records = []
        self.latest = None
        self.error = None
        self.nonce = str(time.time_ns())

    def run(self):
        version = 0
        while not self.stop_event.is_set():
            started = time.monotonic_ns()
            version += 1
            try:
                payload = (f"stage-f-soak-v{version}-{self.nonce}\n".encode()
                           * 256)[:4096]
                outcome = atomic_replace(self.path, payload)
                record = {
                    "version": version, "at_ns": time.monotonic_ns(),
                    "duration_ns": time.monotonic_ns() - started,
                    "sha256": outcome["sha256"], "bytes": len(payload),
                }
                with self.lock:
                    self.records.append(record)
                    self.latest = record
            except BaseException as exc:
                self.error = f"{type(exc).__name__}: {exc}"
                self.stop_event.set()
                break
            wait = self.interval - (time.monotonic_ns() - started) / 1_000_000_000
            if wait > 0:
                self.stop_event.wait(wait)

    def stop(self):
        self.stop_event.set()

    def latest_record(self):
        with self.lock:
            return dict(self.latest) if self.latest else None


def read_watchers(host_watch, fuse_watch, host_events, fuse_events):
    if host_watch is not None:
        host_events.extend(host_watch.read())
    if fuse_watch is not None:
        fuse_events.extend(fuse_watch.read())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Bounded 180-300 s product FUSE read-cache and shared-data "
                    "atomic-writer soak with 1 s resource sampling.")
    parser.add_argument("--fixtures", type=Path, default=common.FIXTURES_DEFAULT)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--target", default="debian13", choices=common.TARGETS)
    parser.add_argument("--seconds", type=int, default=180,
                        help="bounded soak length, 180-300 seconds")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args(argv)
    if not 180 <= args.seconds <= 300:
        raise common.ProbeError("Stage F soak is bounded to 180-300 seconds")
    run_dir = common.create_run_dir(args.run_dir)
    progress = common.Progress(run_dir, "probe_soak", target=args.target,
                               seconds=args.seconds)
    sampler = None
    host_watch = fuse_watch = None
    writer = None
    view = None
    try:
        common.guard_memory(label="soak preflight")
        fixtures = common.load_fixtures(args.fixtures)
        facade = common.product(fixtures)
        service = facade["service"]
        views = facade["views"]
        item = common.fixture(fixtures, args.target)
        env_id = item["environment"]["id"]
        cid = item["environment"]["runtime"]["container_id"]
        workspace = Path(item["workspace"])
        dataset = workspace / "dataset"
        expected = item["dataset"]
        common.ensure_stopped(service, item)

        view = views.open(env_id)
        if view.get("state") != "ready":
            raise common.ProbeError(f"view state {view.get('state')!r}, not ready")
        mountpoint = Path(view["mountpoint"])
        if not common.mountinfo_contains(str(mountpoint)):
            raise common.ProbeError("view is not mounted")
        mountpoint_str = str(mountpoint)
        fuse_dataset = mountpoint / dataset.relative_to("/")
        fuse_small = fuse_dataset / "small"
        if not fuse_small.is_dir():
            raise common.ProbeError("FUSE small-file directory is missing")
        current_path = dataset / "soak" / "current.bin"
        fuse_current = fuse_dataset / "soak" / "current.bin"
        # These must be two distinct real paths: the host ordinary bind and the
        # product FUSE projection.  Watching the FUSE path twice would not be a
        # host-native control.
        host_watch_path = current_path.parent
        fuse_watch_path = fuse_current.parent
        watch_paths_distinct = (
            str(host_watch_path) != str(fuse_watch_path)
            and host_watch_path.is_dir() and fuse_watch_path.is_dir())

        # Watchers are created before the writer so host events can be proven.
        host_watch = Inotify()
        host_watch.add(host_watch_path, IN_CHANGE_EVENTS)
        fuse_watch = None
        fuse_watch_error = None
        try:
            fuse_watch = Inotify()
            fuse_watch.add(fuse_watch_path, IN_CHANGE_EVENTS)
        except OSError as exc:
            fuse_watch_error = {
                "errno": getattr(exc, "errno", None), "error": str(exc)}
        host_events = []
        fuse_events = []

        writer = SoakWriter(current_path, interval=args.interval)
        writer.start()
        sampler = ResourceSampler(fixtures, facade, run_dir=run_dir,
                                  interval=args.interval)
        sampler.start()
        progress.update("soak", message=f"bounded {args.seconds}s soak")

        start = time.monotonic()
        deadline = start + args.seconds
        latencies = []
        errors = []
        iteration = 0
        stale_reads = 0
        fresh_reads = 0
        current_checks = 0
        current_hashes = []
        last_current_sha = None
        sequential_checks = 0
        sequential_bytes = 0
        while time.monotonic() < deadline:
            if sampler.abort_event.is_set():
                raise common.GuardError(
                    f"resource sampler aborted: {sampler.abort_reason}")
            if writer.error:
                raise common.ProbeError(f"soak writer failed: {writer.error}")
            started = time.monotonic_ns()
            try:
                for offset in range(20):
                    index = (iteration * 20 + offset) % common.SMALL_FILE_COUNT
                    path = fuse_small / f"f{index:04d}"
                    os.stat(path)
                    with open(path, "rb") as stream:
                        payload = stream.read(common.SMALL_FILE_BYTES)
                    if payload != common.small_file_bytes(args.target, index):
                        raise common.ProbeError(
                            f"soak byte mismatch at f{index:04d}")
                latencies.append(time.monotonic_ns() - started)
            except OSError as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                break

            try:
                current = fuse_current.read_bytes()
                current_checks += 1
                latest = writer.latest_record()
                last_current_sha = common.sha256_bytes(current)
                current_hashes.append(last_current_sha)
                if latest is not None:
                    if last_current_sha == latest["sha256"]:
                        fresh_reads += 1
                    else:
                        stale_reads += 1
            except OSError as exc:
                errors.append(f"current.bin: {type(exc).__name__}: {exc}")
                break

            # Exercise the larger file through the same view every tenth loop.
            if iteration % 10 == 0:
                try:
                    offset = (iteration // 10) % (common.SEQUENTIAL_BYTES // (1024 * 1024))
                    with open(fuse_dataset / "sequential.bin", "rb") as stream:
                        stream.seek(offset * 1024 * 1024)
                        payload = stream.read(1024 * 1024)
                    expected_chunk = common.sequential_chunk(args.target, offset)
                    if payload != expected_chunk:
                        raise common.ProbeError(
                            f"soak sequential mismatch at MiB {offset}")
                    sequential_checks += 1
                    sequential_bytes += len(payload)
                except OSError as exc:
                    errors.append(f"sequential: {type(exc).__name__}: {exc}")
                    break

            read_watchers(host_watch, fuse_watch, host_events, fuse_events)
            iteration += 1
            time.sleep(0.05)

        actual = time.monotonic() - start
        writer.stop()
        writer.join(timeout=10)
        writer_alive = writer.is_alive()
        read_watchers(host_watch, fuse_watch, host_events, fuse_events)
        sampler.stop("soak complete")
        sampler.join(timeout=20)
        sampler_alive = sampler.is_alive()
        resource_summary = common.read_json(run_dir / "resources-summary.json")

        revoke_started = time.monotonic_ns()
        views.revoke(view["view_uuid"])
        revoke_ns = time.monotonic_ns() - revoke_started
        cleanup = verify_view_gone(facade, view)
        view = None

        host_event_names = sorted({event.get("name", "")
                                   for event in host_events if event.get("name")})
        host_writer_events = [event for event in host_events
                              if event.get("name") == current_path.name]
        host_internal_temp_names = sorted(
            name for name in host_event_names if name.startswith(".stagef-tmp-"))
        host_external_event_names = sorted(
            name for name in host_event_names
            if name != current_path.name and not name.startswith(".stagef-tmp-"))
        host_masks = sorted({int(event.get("mask", 0)) for event in host_events})
        resource_coverage = resource_summary.get("coverage") or {}
        expected_resource_samples = max(2, int(actual / max(0.2, args.interval)) - 3)
        expected_writer_writes = max(2, int(actual / max(0.2, args.interval)) - 3)
        environment_after = common.state_of(service, item)
        fuse_verdict = "UNSUPPORTED" if fuse_watch_error else (
            "OBSERVED" if fuse_events else "NO_EVENT")
        checks = {
            "bounded_duration": 180 <= args.seconds <= 300 and actual >= args.seconds * 0.9,
            "real_reader_iterations": iteration >= 50,
            "read_batches_present": bool(latencies),
            "no_reader_errors": not errors,
            "sequential_evidence_present": sequential_checks > 0 and sequential_bytes > 0,
            "current_read_evidence_present": current_checks > 0,
            "writer_progressed": len(writer.records) >= expected_writer_writes,
            "writer_no_error": writer.error is None,
            "writer_thread_stopped": not writer_alive,
            "watch_paths_distinct": watch_paths_distinct,
            "host_inotify_control_observed": bool(host_events),
            "host_writer_event_observed": bool(host_writer_events),
            "host_no_external_events": not host_external_event_names,
            "fuse_watch_honest": fuse_verdict in {"OBSERVED", "UNSUPPORTED", "NO_EVENT"},
            "resource_samples_present": (
                isinstance(resource_summary.get("sample_count"), int)
                and resource_summary["sample_count"] >= expected_resource_samples),
            "resource_coverage_present": (
                isinstance(resource_coverage, dict)
                and set(resource_coverage) == set(common.TARGETS)
                and all((resource_coverage.get(target) or {}).get("samples", 0) > 0
                        for target in common.TARGETS)),
            "resource_sampling_not_aborted": (
                (resource_summary.get("abort") or {}).get("aborted") is False),
            "resource_sampling_no_errors": not (resource_summary.get("errors") or []),
            "sampler_thread_stopped": not sampler_alive,
            "view_revoked_clean": all(
                value for key, value in cleanup.items()
                if key not in {"process_references", "storage_journal_state",
                               "mountpoint_gone"}),
            "storage_journal_not_active": cleanup.get("storage_journal_state")
            not in {"mounted", "mounting", "releasing", "release-failed",
                    "UNREADABLE"},
            "no_guard_abort": not sampler.abort_event.is_set(),
            "environment_still_stopped": (
                environment_after["container_id"] == cid
                and not environment_after["running"]
                and environment_after.get("record_state") == "stopped"),
        }
        status = "passed" if all(checks.values()) else "failed"
        for name, passed in checks.items():
            progress.check(name, passed)
        summary = {
            "schema_version": 1,
            "status": status,
            "acceptance": "pending-primary-review",
            "scope": ("Short bounded local soak of the actual read-only product "
                      "FUSE view plus host-bind atomic writer. Not a 24-hour "
                      "claim and not an SLO."),
            "target": args.target,
            "container_id": cid,
            "mountpoint": mountpoint_str,
            "bounded_seconds": args.seconds,
            "actual_seconds": round(actual, 3),
            "cache_dropped": False,
            "reader_iterations": iteration,
            "read_batch_ms": common.stats(value / 1_000_000.0 for value in latencies),
            "sequential_checks": sequential_checks,
            "sequential_bytes": sequential_bytes,
            "current_reads": {
                "checks": current_checks, "fresh": fresh_reads,
                "stale_or_racy": stale_reads,
                "fresh_ratio": (round(fresh_reads / current_checks, 4)
                                if current_checks else None),
                "distinct_reader_sha256": len(set(current_hashes)),
                "last_reader_sha256": last_current_sha,
                "last_writer_sha256": (writer.latest_record() or {}).get("sha256"),
                "writer_versions": len(writer.records),
            },
            "errors": errors,
            "writer": {
                "writes": len(writer.records),
                "interval_seconds": args.interval,
                "first": writer.records[0] if writer.records else None,
                "last": writer.records[-1] if writer.records else None,
                "error": writer.error,
            },
            "inotify": {
                "host_watch_path": str(host_watch_path),
                "fuse_watch_path": str(fuse_watch_path),
                "watch_mask": IN_CHANGE_EVENTS,
                "host_writer_path": str(current_path),
                "host_event_count": len(host_events),
                "host_writer_event_count": len(host_writer_events),
                "host_event_names": host_event_names,
                "host_internal_temp_event_names": host_internal_temp_names,
                "host_external_event_names": host_external_event_names,
                "host_event_masks": host_masks,
                "fuse_event_count": len(fuse_events),
                "fuse_error": fuse_watch_error,
                "fuse_verdict": fuse_verdict,
                "host_events_sample": host_events[:30],
                "fuse_events_sample": fuse_events[:30],
                "note": ("Host watcher is the ordinary bind path; FUSE watcher "
                         "is the projection path. FUSE watch creation without "
                         "an event is NO_EVENT, not a supported result. "
                         "host_internal_temp_event_names are this probe's "
                         "atomic-replace temporary files; any "
                         "host_external_event_names are unrelated/external "
                         "modifications and should be inspected."),
            },
            "revoke_ms": round(revoke_ns / 1_000_000.0, 3),
            "cleanup": cleanup,
            "resource_summary": {
                "sample_count": resource_summary.get("sample_count"),
                "coverage": resource_summary.get("coverage"),
                "abort": resource_summary.get("abort"),
                "errors": resource_summary.get("errors"),
                "path": str(run_dir / "resources-summary.json"),
            },
            "checks": checks,
            "started_at": progress.started_at,
            "finished_at": common.now_iso(),
        }
        if status != "passed":
            progress.failure(common.ProbeError("one or more soak checks failed"))
        common.write_summary(run_dir, summary)
        progress.finish(status=status, checks=checks)
        print(json.dumps({
            "status": status, "run_dir": str(run_dir),
            "actual_seconds": summary["actual_seconds"],
            "iterations": iteration, "checks": checks,
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
    finally:
        if writer is not None:
            writer.stop()
            if writer.is_alive():
                writer.join(timeout=10)
            if writer.is_alive():
                common.append_jsonl(progress.failures_path, {
                    "at": common.now_iso(), "phase": "finally-writer",
                    "error": "soak writer thread still alive after join timeout"})
        if sampler is not None:
            sampler.stop("probe exiting")
            sampler.join(timeout=20)
            if sampler.is_alive():
                common.append_jsonl(progress.failures_path, {
                    "at": common.now_iso(), "phase": "finally-sampler",
                    "error": "resource sampler thread still alive after join timeout"})
        if host_watch is not None:
            host_watch.close()
        if fuse_watch is not None:
            fuse_watch.close()
        if view is not None:
            try:
                facade_views = locals().get("views")
                if facade_views is not None:
                    facade_views.revoke(view["view_uuid"])
            except BaseException as revoke_exc:
                # A failure path must not silently hide an unrevoked view.
                common.append_jsonl(progress.failures_path, {
                    "at": common.now_iso(),
                    "phase": "finally-revoke-view",
                    "view_uuid": view.get("view_uuid"),
                    "error": f"{type(revoke_exc).__name__}: {revoke_exc}",
                    "traceback": traceback.format_exc()[-8000:],
                })


if __name__ == "__main__":
    raise SystemExit(main())
