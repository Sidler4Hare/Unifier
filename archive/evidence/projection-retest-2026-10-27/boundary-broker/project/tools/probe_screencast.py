#!/usr/bin/env python3
"""Drive one portal ScreenCast session and a native PipeWire consumer.

The OS portal performs the selection.  The probe only asks for a window or
monitor source, keeps the portal session alive for the consumer subprocess,
passes the restricted OpenPipeWireRemote fd to that child, and records
metadata-only JSON.  No portal permission is persisted (persist_mode 0).

The native consumer contract is documented in fixtures/screencast_consumer.c.
This tool intentionally does not import Gio at module import time.
"""

import argparse
import errno
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT)]

from multilinux.screencast import (  # noqa: E402
    SOURCE_MONITOR,
    SOURCE_WINDOW,
    ScreenCastClient,
    ScreenCastError,
)

SCHEMA = "multilinux.screencast.probe.v1"
DEFAULT_RENDER_NODE = "/dev/dri/renderD128"
MAX_EVENT_LINE = 65536
MAX_STDERR_TAIL = 4000
MAX_HASHES = 256
_SENSITIVE_KEYS = {
    "base64",
    "bgra",
    "buffer_content",
    "data",
    "frame_bytes",
    "frame_data",
    "image",
    "images",
    "payload",
    "pixel_bytes",
    "pixels",
    "raw",
    "rgba",
}


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _scrub(value, depth=0):
    """Keep metadata/status data and drop anything that looks like pixels."""
    if depth > 12:
        return "<depth omitted>"
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            key = str(key)
            if key.lower() in _SENSITIVE_KEYS:
                result[key] = "<omitted>"
            else:
                result[key] = _scrub(item, depth + 1)
        return result
    if isinstance(value, (list, tuple)):
        items = list(value)
        if len(items) > 10000:
            items = items[:10000] + ["<truncated>"]
        return [_scrub(item, depth + 1) for item in items]
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "<binary omitted>"
    if isinstance(value, str):
        return value if len(value) <= 65536 else value[:65536] + "<truncated>"
    if value is None or isinstance(value, (int, float, bool)):
        return value
    return str(value)


def _phase(metadata, phase, status, **details):
    entry = {"phase": phase, "status": status, "at": now()}
    entry.update({key: _scrub(value) for key, value in details.items()})
    metadata["phases"].append(entry)
    return entry


def _metadata_path(path):
    return Path(path).expanduser()


def _write_metadata(path, metadata):
    output = _metadata_path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    payload = json.dumps(_scrub(metadata), ensure_ascii=False, indent=2) + "\n"
    descriptor = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, output)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _normalize_mode(value):
    text = re.sub(r"[^a-z0-9]", "", str(value).lower())
    if "dmabuf" in text:
        return "dmabuf"
    if "memfd" in text:
        return "memfd"
    return None


def _summary_metrics(summary, events):
    """Return (pixel_hashes, color_counts) without accepting image payloads."""
    hashes = []
    if isinstance(summary, dict):
        candidate = summary.get("pixel_hashes")
        if isinstance(candidate, (list, tuple)):
            hashes = [item for item in candidate if isinstance(item, str)]
        elif isinstance(candidate, str):
            hashes = [candidate]
        elif isinstance(summary.get("pixel_hash"), str):
            hashes = [summary["pixel_hash"]]
        if not hashes:
            # The native consumer keeps bounded first/last hash samples.
            hashes = [item for key in ("hashes_first", "hashes_last")
                      for item in (summary.get(key) or [])
                      if isinstance(item, str)]
        if not hashes:
            for event in reversed(events or []):
                if not isinstance(event, dict) or event.get("type") != "frame":
                    continue
                value = event.get("pixel_hashes")
                if isinstance(value, (list, tuple)):
                    hashes = [item for item in value if isinstance(item, str)]
                elif isinstance(value, str):
                    hashes = [value]
                elif isinstance(event.get("pixel_hash"), str):
                    hashes = [event["pixel_hash"]]
                elif isinstance(event.get("hash"), str):
                    hashes = [event["hash"]]
                if hashes:
                    break
    colors = {}
    if isinstance(summary, dict):
        candidate = summary.get("color_counts")
        if candidate is None:
            candidate = summary.get("colors")
        if candidate is None:
            candidate = summary.get("top_colors")
        if isinstance(candidate, (dict, list)) and candidate:
            colors = candidate
    if not colors:
        for event in reversed(events or []):
            if not isinstance(event, dict) or event.get("type") != "frame":
                continue
            candidate = event.get("color_counts")
            if candidate is None:
                candidate = event.get("top_colors")
            if isinstance(candidate, (dict, list)) and candidate:
                colors = candidate
                break
    return hashes[:MAX_HASHES], colors


def _consumer_errors(mode, frames_requested, child):
    """Check the child's final summary; return explicit failure strings."""
    errors = []
    if not isinstance(child, dict):
        return ["consumer produced no result metadata"]
    if child.get("error"):
        errors.append(str(child["error"]))
    if child.get("timed_out"):
        errors.append(
            f"consumer timed out after {child.get('timeout_ms', '?')} ms")
    returncode = child.get("returncode")
    if returncode != 0:
        errors.append(f"consumer exited with return code {returncode!r}")
    summary = child.get("summary")
    if not isinstance(summary, dict):
        errors.append("consumer emitted no final summary event")
        return errors
    if summary.get("passed") is not True:
        errors.append("consumer summary did not report passed=true")
    actual_frames = summary.get("frames")
    if (isinstance(actual_frames, bool) or not isinstance(actual_frames, int)
            or actual_frames < frames_requested):
        errors.append(
            f"consumer captured {actual_frames!r} frame(s), requested {frames_requested}")
    reported_mode = _normalize_mode(summary.get("mode"))
    if reported_mode != mode:
        errors.append(
            f"consumer reported mode {summary.get('mode')!r}, expected {mode!r}")
    buffer_type = summary.get("buffer_type")
    if _normalize_mode(buffer_type) != mode:
        errors.append(
            f"consumer reported buffer_type {buffer_type!r}, expected {mode!r}")
    if mode == "dmabuf" and summary.get("gpu_import") is not True:
        errors.append("consumer did not confirm the dmabuf GPU import")
    if summary.get("cpu_readback") is not True:
        errors.append("consumer did not confirm CPU readback of real pixels")
    hashes, colors = _summary_metrics(summary, child.get("events"))
    if not hashes:
        errors.append("consumer summary has no pixel hashes")
    if not colors:
        errors.append("consumer summary has no color counts")
    event_errors = [event for event in child.get("events") or []
                    if isinstance(event, dict) and event.get("type") == "error"]
    if event_errors:
        errors.append(f"consumer emitted {len(event_errors)} error event(s)")
    return errors


def _parse_events(stdout):
    events = []
    malformed = 0
    oversized = 0
    for line in (stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if len(line) > MAX_EVENT_LINE:
            oversized += 1
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            malformed += 1
            continue
        if not isinstance(event, dict):
            malformed += 1
            continue
        events.append(_scrub(event))
    summary = None
    for event in reversed(events):
        if event.get("type") == "summary":
            summary = event
            break
    frames = sum(1 for event in events if event.get("type") == "frame")
    return events, summary, malformed, oversized, frames


def _tail(text, limit=MAX_STDERR_TAIL):
    text = str(text or "")
    return text[-limit:]


def _kill_consumer(process):
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        return process.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
        return process.communicate()


def run_consumer(consumer, fd, node_id, *, serial=None, mode, frames,
                 timeout_ms, render_node=None, expect_pattern=False):
    """Run the native consumer with only the portal fd inherited.

    Returns parsed NDJSON metadata.  The caller still owns ``fd`` and must
    close it after this function returns.
    """
    argv = [
        str(consumer),
        "--fd", str(int(fd)),
        "--node", str(int(node_id)),
        "--mode", str(mode),
        "--frames", str(int(frames)),
        "--timeout-ms", str(int(timeout_ms)),
    ]
    if serial is not None:
        argv += ["--target-serial", str(int(serial))]
    if mode == "dmabuf" and render_node:
        argv += ["--render-node", str(render_node)]
    if expect_pattern:
        argv += ["--expect-pattern"]
    started = time.monotonic()
    metadata = {
        "argv": argv,
        "returncode": None,
        "timed_out": False,
        "timeout_ms": int(timeout_ms),
        "duration_ms": 0,
        "events": [],
        "summary": None,
        "malformed_events": 0,
        "oversized_events": 0,
        "frames_observed": 0,
        "stderr_tail": "",
    }
    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            pass_fds=(int(fd),),
            start_new_session=True,
        )
    except OSError as exc:
        metadata["error"] = f"cannot start consumer: {exc}"
        metadata["duration_ms"] = int((time.monotonic() - started) * 1000)
        return metadata
    stdout = stderr = ""
    try:
        stdout, stderr = process.communicate(timeout=max(0.001, timeout_ms / 1000))
    except subprocess.TimeoutExpired:
        metadata["timed_out"] = True
        stdout, stderr = _kill_consumer(process)
    metadata["duration_ms"] = int((time.monotonic() - started) * 1000)
    metadata["returncode"] = process.returncode
    events, summary, malformed, oversized, frame_count = _parse_events(stdout)
    metadata["events"] = events
    metadata["summary"] = summary
    metadata["malformed_events"] = malformed
    metadata["oversized_events"] = oversized
    metadata["frames_observed"] = frame_count
    metadata["stderr_tail"] = _tail(stderr)
    return metadata


def default_client_factory(bus_address=None):
    return ScreenCastClient(bus_address=bus_address)


def _fd_is_closed(fd):
    try:
        os.fstat(fd)
    except OSError as exc:
        return exc.errno == errno.EBADF
    return False


def _stream_metadata(stream):
    if hasattr(stream, "as_metadata"):
        return stream.as_metadata()
    if isinstance(stream, dict):
        return {
            "node_id": stream.get("node_id"),
            "pipewire_serial": stream.get("pipewire_serial", stream.get("serial")),
            "properties": stream.get("properties", {}),
        }
    return {
        "node_id": getattr(stream, "node_id", None),
        "pipewire_serial": getattr(stream, "serial", None),
        "properties": getattr(stream, "properties", {}),
    }


def _result_metadata(args, mode, summary, events, attempt_index):
    hashes, colors = _summary_metrics(summary, events)
    layout = next((event for event in reversed(events)
                   if event.get("type") == "format"), {})
    return {
        "passed": True,
        "mode_requested": args.mode,
        "mode_used": mode,
        "attempt": attempt_index,
        "format": summary.get("format"),
        "frames": summary.get("frames"),
        "buffer_type": summary.get("buffer_type"),
        "width": summary.get("width", layout.get("width")),
        "height": summary.get("height", layout.get("height")),
        "modifier": summary.get("modifier"),
        "hash_changes": summary.get("hash_changes"),
        "pattern": summary.get("pattern"),
        "zero_copy": summary.get("zero_copy", False),
        "gpu_import": summary.get("gpu_import"),
        "cpu_readback": summary.get("cpu_readback"),
        "pixel_hashes": hashes,
        "color_counts": colors,
    }


def execute(args, *, client_factory=None, consumer_runner=None):
    """Run a complete probe.  Always writes metadata JSON to args.output.

    Returns ``(metadata, passed)``.  Exceptions from the portal, consumer and
    cleanup are recorded; KeyboardInterrupt is re-raised after cleanup/output.
    """
    client_factory = default_client_factory if client_factory is None else client_factory
    consumer_runner = run_consumer if consumer_runner is None else consumer_runner
    selection_ms = max(1, int(float(args.selection_timeout) * 1000))
    capture_ms = max(1, int(float(args.timeout) * 1000))
    metadata = {
        "schema": SCHEMA,
        "created_at": now(),
        "finished_at": None,
        "request": {
            "consumer": str(args.consumer),
            "output": str(args.output),
            "mode": args.mode,
            "frames": int(args.frames),
            "timeout_seconds": float(args.timeout),
            "selection_timeout_seconds": float(args.selection_timeout),
            "source": args.source,
            "parent_window": args.parent_window or "",
            "expect_pattern": bool(args.expect_pattern),
            "render_node": str(args.render_node) if args.render_node else None,
            "bus": "explicit" if args.bus_address else "session",
            "bus_address": args.bus_address or None,
            "persist_mode": 0,
        },
        "selection": {
            "source": args.source,
            "types": SOURCE_WINDOW if args.source == "window" else SOURCE_MONITOR,
            "multiple": False,
            "persist_mode": 0,
        },
        "portal": {
            "version": None,
            "session_opened": False,
            "session_handle": None,
            "events": [],
            "warnings": [],
        },
        "sources": [],
        "phases": [],
        "attempts": [],
        "fallback": None,
        "errors": [],
        "warnings": [],
        "cleanup": {
            "client_closed": False,
            "session_closed": False,
            "remote_fds_closed": False,
            "open_remote_fds": [],
        },
        "result": {
            "passed": False,
            "mode_requested": args.mode,
            "mode_used": None,
            "attempt": None,
            "format": None,
            "frames": 0,
            "buffer_type": None,
            "width": None,
            "height": None,
            "gpu_import": None,
            "cpu_readback": None,
            "pixel_hashes": [],
            "color_counts": {},
        },
    }
    client = None
    passed = False
    try:
        _phase(metadata, "portal_connect", "started",
               bus=metadata["request"]["bus"])
        client = client_factory(args.bus_address)
        metadata["portal"]["version"] = getattr(client, "version", None)
        _phase(metadata, "portal_connect", "completed",
               version=metadata["portal"]["version"])

        _phase(metadata, "session_start", "started", source=args.source,
               parent_window=args.parent_window or "")
        streams = client.start(
            source=args.source,
            parent_window=args.parent_window or "",
            selection_timeout_ms=selection_ms)
        metadata["portal"]["session_opened"] = True
        metadata["portal"]["session_handle"] = getattr(client, "session_handle", None)
        metadata["portal"]["version"] = getattr(client, "version", None)
        metadata["selection"] = dict(getattr(client, "selection", None)
                                     or metadata["selection"])
        metadata["sources"] = [_scrub(_stream_metadata(stream))
                               for stream in (streams or [])]
        if not metadata["sources"]:
            raise ScreenCastError("portal Start returned no usable sources")
        _phase(metadata, "session_start", "completed",
               source_count=len(metadata["sources"]),
               persist_mode=metadata["selection"].get("persist_mode", 0))

        source = streams[0]
        node_id = getattr(source, "node_id", None)
        serial = getattr(source, "serial", None)
        if node_id is None and isinstance(source, dict):
            node_id = source.get("node_id")
            serial = source.get("pipewire_serial", source.get("serial"))
        if node_id is None:
            raise ScreenCastError("selected source has no PipeWire node id")

        modes = ["dmabuf", "memfd"] if args.mode == "auto" else [args.mode]
        for index, mode in enumerate(modes):
            attempt = {
                "index": index,
                "mode": mode,
                "requested_mode": args.mode,
                "remote_fd_opened": False,
                "fd_closed": None,
                "ok": False,
                "errors": [],
                "child": None,
            }
            metadata["attempts"].append(attempt)
            fd = None
            try:
                _phase(metadata, f"open_pipewire_remote:{mode}", "started")
                fd = client.open_pipewire_remote(timeout_ms=min(selection_ms, 10000))
                attempt["remote_fd_opened"] = True
                _phase(metadata, f"open_pipewire_remote:{mode}", "completed")
                _phase(metadata, f"consumer:{mode}", "started",
                       node_id=node_id, pipewire_serial=serial)
                child = consumer_runner(
                    args.consumer, fd, node_id,
                    serial=serial,
                    mode=mode,
                    frames=int(args.frames),
                    timeout_ms=capture_ms,
                    render_node=(str(args.render_node)
                                 if mode == "dmabuf" and args.render_node else None),
                    expect_pattern=bool(args.expect_pattern))
                attempt["child"] = _scrub(child)
                attempt["errors"] = _consumer_errors(
                    mode, int(args.frames), child)
                _phase(metadata, f"consumer:{mode}",
                       "completed" if not attempt["errors"] else "failed",
                       returncode=(child.get("returncode")
                                   if isinstance(child, dict) else None),
                       errors=attempt["errors"])
                if not attempt["errors"]:
                    attempt["ok"] = True
                    passed = True
                    metadata["result"] = _result_metadata(
                        args, mode, child.get("summary") or {},
                        child.get("events") or [], index)
                    _phase(metadata, f"attempt:{mode}", "completed")
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                attempt["errors"].append(message)
                _phase(metadata, f"attempt:{mode}", "failed", error=message)
            finally:
                if fd is not None:
                    closed = False
                    try:
                        closed = bool(client.close_remote(fd))
                    except Exception as exc:
                        attempt["errors"].append(
                            f"could not close PipeWire remote fd: {exc}")
                    attempt["fd_closed"] = _fd_is_closed(fd)
                    if not attempt["fd_closed"]:
                        attempt["errors"].append(
                            "PipeWire remote fd remained open after cleanup")
                    _phase(metadata, f"close_pipewire_remote:{mode}",
                           "completed" if attempt["fd_closed"] else "failed",
                           closed=attempt["fd_closed"])
                    if not closed and not attempt["fd_closed"]:
                        attempt["errors"].append(
                            "client did not own the PipeWire remote fd")
            if attempt["ok"]:
                break
            for error in attempt["errors"]:
                metadata["errors"].append(f"{mode}: {error}")
            if args.mode == "auto" and mode == "dmabuf":
                metadata["fallback"] = {
                    "from": "dmabuf",
                    "to": "memfd",
                    "reason": "; ".join(attempt["errors"])[:2000],
                }
                _phase(metadata, "fallback", "retrying",
                       **metadata["fallback"])
                continue
            break
    except KeyboardInterrupt:
        metadata["errors"].append("cancelled: probe interrupted")
        _phase(metadata, "probe", "cancelled")
        raise
    except Exception as exc:
        metadata["errors"].append(f"{type(exc).__name__}: {exc}")
        _phase(metadata, "probe", "failed", error=str(exc))
    finally:
        if client is not None:
            try:
                client.close()
            except Exception as exc:
                metadata["errors"].append(
                    f"client cleanup failed: {type(exc).__name__}: {exc}")
            metadata["portal"]["version"] = getattr(client, "version", None)
            metadata["portal"]["events"] = _scrub(
                list(getattr(client, "events", [])))
            metadata["portal"]["warnings"] = _scrub(
                list(getattr(client, "warnings", [])))
            open_fds = list(getattr(client, "open_remote_fds", ()) or ())
            metadata["cleanup"] = {
                "client_closed": bool(getattr(client, "closed", False)),
                "session_closed": getattr(client, "session_handle", None) is None,
                "remote_fds_closed": not open_fds,
                "open_remote_fds": _scrub(open_fds),
            }
            metadata["warnings"] = _scrub(
                list(getattr(client, "warnings", [])))
        else:
            metadata["cleanup"]["client_closed"] = False
            metadata["cleanup"]["session_closed"] = True
            metadata["cleanup"]["remote_fds_closed"] = True
        metadata["result"]["passed"] = passed
        _phase(metadata, "cleanup", "completed",
               **{key: value for key, value in metadata["cleanup"].items()
                  if key != "open_remote_fds"})
        metadata["finished_at"] = now()
        _write_metadata(args.output, metadata)
    return metadata, passed


def _positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def _positive_float(text):
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        description="Run one portal ScreenCast session and a native PipeWire consumer.")
    parser.add_argument("--consumer", required=True, type=Path,
                        help="compiled fixtures/screencast_consumer binary")
    parser.add_argument("--output", required=True, type=Path,
                        help="metadata-only JSON output path")
    parser.add_argument("--mode", choices=("memfd", "dmabuf", "auto"),
                        default="auto")
    parser.add_argument("--frames", type=_positive_int, default=1)
    parser.add_argument("--timeout", type=_positive_float, default=30.0,
                        help="per-consumer capture timeout in seconds")
    parser.add_argument("--selection-timeout", type=_positive_float,
                        default=120.0,
                        help="portal user-selection timeout in seconds")
    parser.add_argument("--source", choices=("window", "monitor"),
                        default="window")
    parser.add_argument("--parent-window", default="")
    parser.add_argument("--bus-address", default=None,
                        help="explicit D-Bus address (default: current session bus)")
    parser.add_argument("--render-node", default=DEFAULT_RENDER_NODE)
    parser.add_argument("--expect-pattern", action="store_true")
    return parser


def parse_args(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.consumer.is_file():
        parser.error(f"consumer does not exist: {args.consumer}")
    if not os.access(args.consumer, os.X_OK):
        parser.error(f"consumer is not executable: {args.consumer}")
    return args


def main(argv=None):
    args = parse_args(argv)
    try:
        metadata, passed = execute(args)
    except KeyboardInterrupt:
        print(json.dumps({"schema": SCHEMA, "passed": False,
                          "error": "interrupted", "output": str(args.output)},
                         ensure_ascii=False))
        return 130
    except Exception as exc:
        print(json.dumps({"schema": SCHEMA, "passed": False,
                          "error": f"{type(exc).__name__}: {exc}",
                          "output": str(args.output)}, ensure_ascii=False))
        return 1
    summary = {
        "schema": SCHEMA,
        "passed": passed,
        "output": str(args.output),
        "mode_requested": args.mode,
    }
    if passed:
        result = metadata.get("result") or {}
        summary.update({
            "mode": result.get("mode_used"),
            "frames": result.get("frames"),
            "buffer_type": result.get("buffer_type"),
        })
    else:
        summary["errors"] = metadata.get("errors", [])[:20]
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
