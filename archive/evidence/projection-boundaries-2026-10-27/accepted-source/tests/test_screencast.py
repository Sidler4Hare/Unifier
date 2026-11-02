"""Offline contracts for the portal ScreenCast client and probe.

These tests never open a real portal selection dialog and never capture screen
content.  Gio is imported only by one explicitly skipped-if-unavailable
serialization test; the rest use an injected transport/client/consumer.
"""

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")]

from multilinux import screencast
from multilinux.screencast import (
    ScreenCastCancelled,
    ScreenCastClient,
    ScreenCastProtocolError,
    ScreenCastRequestError,
    ScreenCastTimeout,
    ScreenCastUnavailable,
    SourceStream,
)
from tools import probe_screencast

SESSION = "/org/freedesktop/portal/desktop/session/1_42/ml_test_session"
STREAM = {
    "pipewire-serial": 99,
    "size": (800, 600),
    "source_type": 2,
}


class FakeTransport:
    """Portal transport seam with controllable response ordering."""

    def __init__(self, *, version=6, emit_early=False, response_code=0,
                 fail_method=None, timeout=False, timeout_method=None,
                 actual_overrides=None, streams=None):
        self.unique_name = ":1.42"
        self.version = version
        self.emit_early = emit_early
        self.response_code = response_code
        self.fail_method = fail_method
        self.timeout = timeout
        self.timeout_method = timeout_method
        self.actual_overrides = actual_overrides or {}
        self.streams = streams if streams is not None else [(7, dict(STREAM))]
        self.sequence = []
        self.calls = []
        self.callbacks = []
        self.subscriptions = []
        self.pending = None
        self.responses = []
        self.closed = False
        self.closed_requests = []
        self.closed_sessions = []
        self.fds = []
        self.fd_calls = []

    def subscribe(self, callback):
        self.sequence.append(("subscribe",))
        self.callbacks.append(callback)
        self.subscriptions.append(callback)
        return callback

    def unsubscribe(self, subscription):
        self.sequence.append(("unsubscribe",))
        if subscription in self.callbacks:
            self.callbacks.remove(subscription)
        if subscription in self.subscriptions:
            self.subscriptions.remove(subscription)

    def _emit(self, path, code, results):
        self.responses.append((path, code, results))
        for callback in list(self.callbacks):
            callback(path, code, results)

    def _handle_token(self, method, parameters):
        if method == "CreateSession":
            return parameters[0]["handle_token"]
        if method == "SelectSources":
            return parameters[1]["handle_token"]
        if method == "Start":
            return parameters[2]["handle_token"]
        raise AssertionError(method)

    def _actual(self, method, token):
        if method in self.actual_overrides:
            return self.actual_overrides[method]
        return screencast.predicted_handle("request", self.unique_name, token)

    def _result(self, method):
        if method == "CreateSession":
            return {"session_handle": SESSION}
        if method == "SelectSources":
            return {}
        return {"streams": list(self.streams)}

    def call(self, path, interface, method, parameters, timeout_ms):
        self.sequence.append(("call", method))
        self.calls.append((path, interface, method, parameters, timeout_ms))
        if method == "Get":
            return self.version
        token = self._handle_token(method, parameters)
        actual = self._actual(method, token)
        code = (self.response_code
                if self.fail_method is None or method == self.fail_method else 0)
        results = self._result(method) if code == 0 else {}
        if self.emit_early:
            self._emit(actual, code, results)
        else:
            self.pending = (actual, code, results, method)
        return actual

    def call_with_fd(self, path, interface, method, parameters, timeout_ms):
        self.fd_calls.append((path, interface, method, parameters, timeout_ms))
        read_fd, write_fd = os.pipe()
        self.fds.append((read_fd, write_fd))
        return read_fd

    def wait(self, predicate, timeout_ms):
        self.sequence.append(("wait",))
        if predicate():
            return True
        if self.pending and self.timeout:
            if self.timeout_method is None or self.pending[3] == self.timeout_method:
                return False
        if self.pending:
            path, code, results, _method = self.pending
            self.pending = None
            self._emit(path, code, results)
        return bool(predicate())

    def close_request(self, handle, timeout_ms=2000):
        self.sequence.append(("close_request", handle))
        self.closed_requests.append(handle)

    def close_session(self, handle, timeout_ms=2000):
        self.sequence.append(("close_session", handle))
        self.closed_sessions.append(handle)

    def close(self):
        self.closed = True


def client_with(transport):
    return ScreenCastClient(transport=transport)


def test_client_start(transport, **kwargs):
    client = client_with(transport)
    streams = client.start(**kwargs)
    return client, streams


class LazyImport(unittest.TestCase):
    def test_module_and_probe_import_without_gi(self):
        code = textwrap.dedent(
            "import sys\n"
            f"sys.path[:0] = [{str(ROOT / 'src')!r}, {str(ROOT)!r}]\n"
            "import multilinux.screencast\n"
            "import tools.probe_screencast\n"
            "assert 'gi' not in sys.modules, sorted(k for k in sys.modules if k == 'gi')\n"
            "print('lazy-ok')\n")
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(ROOT / "src"), str(ROOT), str(ROOT / "tests")])
        result = subprocess.run([sys.executable, "-c", code], text=True,
                                capture_output=True, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "lazy-ok")


class ClientProtocol(unittest.TestCase):
    def test_full_flow_uses_actual_handles_v6_serial_and_closes_everything(self):
        transport = FakeTransport()
        client, streams = test_client_start(
            transport, source="window", parent_window="x11:0x123",
            selection_timeout_ms=5000)
        self.assertEqual(len(streams), 1)
        self.assertEqual(streams[0].node_id, 7)
        self.assertEqual(streams[0].serial, 99)
        self.assertEqual(client.version, 6)
        self.assertEqual(client.session_handle, SESSION)

        methods = [entry[1] for entry in transport.sequence if entry[0] == "call"]
        methods = [method for method in methods if method != "Get"]
        self.assertEqual(methods, ["CreateSession", "SelectSources", "Start"])
        subscribe_at = transport.sequence.index(("subscribe",))
        first_call_at = transport.sequence.index(("call", "CreateSession"))
        self.assertLess(subscribe_at, first_call_at)
        for method in ("CreateSession", "SelectSources", "Start"):
            call = next(call for call in transport.calls if call[2] == method)
            if method == "CreateSession":
                options = call[3][0]
                self.assertEqual(options["session_handle_token"][:14],
                                 "ml_screencast_")
                token = options["handle_token"]
            elif method == "SelectSources":
                options = call[3][1]
                self.assertEqual(options["types"], screencast.SOURCE_WINDOW)
                self.assertIs(options["multiple"], False)
                self.assertEqual(options["persist_mode"], 0)
                token = options["handle_token"]
            else:
                self.assertEqual(call[3][0], SESSION)
                self.assertEqual(call[3][1], "x11:0x123")
                token = call[3][2]["handle_token"]
            self.assertRegex(token, r"^ml_screencast_[0-9a-f]{32}$")
            self.assertIn(("subscribe",), transport.sequence)
        tokens = []
        for call in transport.calls:
            if call[2] == "CreateSession":
                tokens.append(call[3][0]["handle_token"])
            elif call[2] == "SelectSources":
                tokens.append(call[3][1]["handle_token"])
            elif call[2] == "Start":
                tokens.append(call[3][2]["handle_token"])
        self.assertEqual(len(set(tokens)), 3)
        self.assertEqual(transport.closed_requests, [])

        fd = client.open_pipewire_remote()
        try:
            os.fstat(fd)
        finally:
            client.close()
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertEqual(transport.closed_sessions, [SESSION])
        self.assertTrue(transport.closed)
        self.assertIsNone(client.session_handle)
        self.assertTrue(client.closed)
        self.assertEqual(client.open_remote_fds, ())
        for read_fd, write_fd in transport.fds:
            os.close(write_fd)

    def test_early_response_and_different_actual_handle_are_matched_exactly(self):
        expected = screencast.predicted_handle(
            "request", ":1.42", "ml_screencast_" + "0" * 32)
        actual = "/org/freedesktop/portal/desktop/request/1_42/actual_handle"
        transport = FakeTransport(
            emit_early=True,
            actual_overrides={"SelectSources": actual})
        client, streams = test_client_start(transport)
        self.assertEqual([stream.node_id for stream in streams], [7])
        self.assertEqual(len(transport.responses), 3)
        self.assertIn(actual, [item[0] for item in transport.responses])
        self.assertNotEqual(expected, actual)
        self.assertTrue(any(item.get("actual_handle") == actual
                            for item in client.warnings))
        self.assertEqual(client._responses, {})
        self.assertEqual(transport.closed_requests, [])
        client.close()
        self.assertEqual(transport.closed_sessions, [SESSION])

    def test_user_denial_is_explicit_and_closes_session(self):
        transport = FakeTransport(response_code=1, fail_method="Start")
        client = client_with(transport)
        with self.assertRaises(ScreenCastCancelled) as raised:
            client.start()
        message = str(raised.exception).lower()
        self.assertIn("cancel", message)
        self.assertIn("denied", message)
        self.assertEqual(raised.exception.response, 1)
        self.assertTrue(client.closed)
        self.assertEqual(transport.closed_sessions, [SESSION])
        self.assertTrue(transport.closed)

    def test_other_portal_error_is_explicit(self):
        transport = FakeTransport(response_code=2, fail_method="Start")
        client = client_with(transport)
        with self.assertRaises(ScreenCastRequestError) as raised:
            client.start()
        self.assertEqual(raised.exception.response, 2)
        self.assertIn("response code 2", str(raised.exception).lower())
        self.assertEqual(transport.closed_sessions, [SESSION])

    def test_timeout_closes_unanswered_request_and_session(self):
        transport = FakeTransport(timeout=True, timeout_method="Start")
        client = client_with(transport)
        with self.assertRaises(ScreenCastTimeout) as raised:
            client.start(selection_timeout_ms=1200)
        self.assertIn("Start", str(raised.exception))
        self.assertEqual(len(transport.closed_requests), 1)
        self.assertRegex(transport.closed_requests[0], r"/request/1_42/ml_screencast_")
        self.assertEqual(transport.closed_sessions, [SESSION])
        self.assertTrue(client.closed)
        self.assertTrue(transport.closed)

    def test_fd_extraction_uses_actual_fd_not_handle_index(self):
        class FDList:
            def __init__(self, value, length=1):
                self.value = value
                self.length = length
                self.indexes = []

            def get_length(self):
                return self.length

            def get(self, index):
                self.indexes.append(index)
                return self.value

        fd_list = FDList(42)
        self.assertEqual(screencast._extract_unix_fd(fd_list, 0), 42)
        self.assertEqual(fd_list.indexes, [0])
        with self.assertRaises(ScreenCastProtocolError):
            screencast._extract_unix_fd(FDList(42), 1)
        with self.assertRaises(ScreenCastProtocolError):
            screencast._extract_unix_fd(FDList(42), -1)
        with self.assertRaises(ScreenCastProtocolError):
            screencast._extract_unix_fd(FDList(42), True)
        with self.assertRaises(ScreenCastProtocolError):
            screencast._extract_unix_fd(FDList(-1), 0)

    def test_real_gi_serializes_each_portal_argument_tuple(self):
        try:
            from gi.repository import Gio, GLib
        except (ImportError, ValueError, OSError) as exc:
            self.skipTest(f"PyGObject is unavailable: {exc}")
        transport = object.__new__(screencast.GioPortalTransport)
        transport._gio, transport._glib = Gio, GLib

        create = transport._variant_parameters(
            "CreateSession", [{"handle_token": "abc"}])
        self.assertEqual(create.get_type_string(), "(a{sv})")
        self.assertEqual(create.unpack(), ({"handle_token": "abc"},))

        select = transport._variant_parameters(
            "SelectSources",
            [SESSION, {"types": 2, "multiple": False, "persist_mode": 0}])
        self.assertEqual(select.get_type_string(), "(oa{sv})")
        session, options = select.unpack()
        self.assertEqual(session, SESSION)
        self.assertEqual(options["types"], 2)
        self.assertIs(options["multiple"], False)
        self.assertEqual(options["persist_mode"], 0)

        start = transport._variant_parameters(
            "Start", [SESSION, "x11:0x1", {"handle_token": "def"}])
        self.assertEqual(start.get_type_string(), "(osa{sv})")
        self.assertEqual(start.unpack()[0:2], (SESSION, "x11:0x1"))

        remote = transport._variant_parameters(
            "OpenPipeWireRemote", [SESSION, {}])
        self.assertEqual(remote.get_type_string(), "(oa{sv})")
        self.assertEqual(remote.unpack(), (SESSION, {}))

        get = transport._variant_parameters(
            "Get", [screencast.SCREENCAST_INTERFACE, "version"])
        self.assertEqual(get.get_type_string(), "(ss)")
        self.assertEqual(get.unpack(), (screencast.SCREENCAST_INTERFACE, "version"))


class FakeProbeClient:
    def __init__(self, bus_address=None, *, fail_start=False):
        self.bus_address = bus_address
        self.fail_start = fail_start
        self.events = [{"phase": "portal_request", "status": "completed"}]
        self.warnings = []
        self.version = 6
        self.session_handle = None
        self.selection = {}
        self.closed = False
        self.start_kwargs = None
        self.sequence = []
        self.open_fds = set()
        self.pipes = []

    def start(self, **kwargs):
        if self.fail_start:
            raise ScreenCastUnavailable("test portal unavailable")
        self.start_kwargs = kwargs
        self.session_handle = SESSION
        self.selection = {
            "source": kwargs["source"],
            "types": (screencast.SOURCE_WINDOW
                      if kwargs["source"] == "window"
                      else screencast.SOURCE_MONITOR),
            "multiple": False,
            "persist_mode": 0,
        }
        self.events.append({"phase": "session", "status": "started"})
        return [SourceStream(7, 99, dict(STREAM))]

    def open_pipewire_remote(self, *, timeout_ms=10000):
        read_fd, write_fd = os.pipe()
        self.pipes.append((read_fd, write_fd))
        self.open_fds.add(read_fd)
        self.sequence.append(("open", read_fd))
        return read_fd

    def close_remote(self, fd):
        self.sequence.append(("close", fd))
        if fd not in self.open_fds:
            return False
        self.open_fds.discard(fd)
        os.close(fd)
        return True

    @property
    def open_remote_fds(self):
        return tuple(sorted(self.open_fds))

    def close(self):
        self.closed = True
        for fd in list(self.open_fds):
            os.close(fd)
        self.open_fds.clear()
        self.session_handle = None
        for _read_fd, write_fd in self.pipes:
            try:
                os.close(write_fd)
            except OSError:
                pass
        self.pipes = []


def child_result(mode, *, passed=True, frames=1, returncode=0,
                 buffer_type=None, gpu_import=False, cpu_readback=True,
                 error_event=False):
    if buffer_type is None:
        buffer_type = "MemFd" if mode == "memfd" else "DmaBuf"
    summary = {
        "type": "summary",
        "passed": passed,
        "frames": frames,
        "buffer_type": buffer_type,
        "mode": mode,
        "format": "BGRx",
        "gpu_import": gpu_import,
        "cpu_readback": cpu_readback,
        "pixel_hashes": ["sha256:abc"] if passed else [],
        "color_counts": {"#000000": 10} if passed else {},
    }
    events = [
        {"type": "format", "format": "BGRx", "width": 800, "height": 600},
        {"type": "frame", "index": 0, "pixel_hash": "sha256:abc",
         "color_counts": {"#000000": 10}},
        summary,
    ]
    if error_event:
        events.insert(1, {"type": "error", "message": "unsupported buffer"})
    return {
        "argv": ["consumer", "--mode", mode],
        "returncode": returncode,
        "timed_out": False,
        "events": events,
        "summary": summary,
        "malformed_events": 0,
        "oversized_events": 0,
        "frames_observed": frames if passed else 0,
        "stderr_tail": "",
    }


def probe_args(temporary, **overrides):
    values = {
        "consumer": Path(sys.executable),
        "output": Path(temporary) / "probe.json",
        "mode": "auto",
        "frames": 1,
        "timeout": 2.0,
        "selection_timeout": 3.0,
        "source": "window",
        "parent_window": "",
        "bus_address": None,
        "render_node": "/dev/dri/renderD128",
        "expect_pattern": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


class ProbeProbe(unittest.TestCase):
    def test_auto_falls_back_to_memfd_with_fresh_fd(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = probe_args(temporary)
            fake_client = FakeProbeClient()
            runs = []

            def runner(_consumer, fd, _node, **kwargs):
                runs.append((fd, kwargs))
                if kwargs["mode"] == "dmabuf":
                    return child_result("dmabuf", passed=False, frames=0,
                                        returncode=1, gpu_import=False,
                                        cpu_readback=False, error_event=True)
                return child_result("memfd")

            metadata, passed = probe_screencast.execute(
                args, client_factory=lambda _address: fake_client,
                consumer_runner=runner)
            self.assertTrue(passed)
            self.assertEqual(metadata["result"]["mode_used"], "memfd")
            self.assertEqual(metadata["result"]["pixel_hashes"], ["sha256:abc"])
            self.assertEqual(metadata["result"]["color_counts"], {"#000000": 10})
            self.assertEqual(len(metadata["attempts"]), 2)
            self.assertFalse(metadata["attempts"][0]["ok"])
            self.assertTrue(metadata["attempts"][1]["ok"])
            self.assertIn("dmabuf", metadata["fallback"]["reason"])
            self.assertTrue(all(attempt["fd_closed"] for attempt in metadata["attempts"]))
            self.assertEqual([step[0] for step in fake_client.sequence],
                             ["open", "close", "open", "close"])
            self.assertEqual(len(fake_client.sequence), 4)
            self.assertEqual(metadata["selection"]["persist_mode"], 0)
            self.assertEqual(metadata["request"]["persist_mode"], 0)
            self.assertTrue(metadata["cleanup"]["client_closed"])
            self.assertTrue(metadata["cleanup"]["session_closed"])
            self.assertTrue(metadata["cleanup"]["remote_fds_closed"])
            self.assertEqual(metadata["cleanup"]["open_remote_fds"], [])
            output = json.loads(Path(args.output).read_text())
            self.assertTrue(output["result"]["passed"])
            self.assertNotIn("data", json.dumps(output["result"]))
            self.assertTrue(any(phase["phase"] == "fallback"
                                for phase in metadata["phases"]))
            self.assertTrue(any(phase["phase"] == "cleanup"
                                for phase in metadata["phases"]))

    def test_fixed_dmabuf_failure_does_not_fall_back_and_exits_failed(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = probe_args(temporary, mode="dmabuf")
            metadata, passed = probe_screencast.execute(
                args,
                client_factory=lambda _address: FakeProbeClient(),
                consumer_runner=lambda *_a, **_kw: child_result(
                    "dmabuf", passed=False, frames=0, returncode=2,
                    gpu_import=False, cpu_readback=False, error_event=True))
            self.assertFalse(passed)
            self.assertIsNone(metadata["fallback"])
            self.assertEqual(len(metadata["attempts"]), 1)
            self.assertGreaterEqual(len(metadata["errors"]), 1)
            self.assertFalse(metadata["result"]["passed"])

    def test_monitor_source_and_parent_window_reach_portal_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = probe_args(temporary, mode="memfd", source="monitor",
                              parent_window="x11:0x55")
            fake_client = FakeProbeClient()
            metadata, passed = probe_screencast.execute(
                args, client_factory=lambda _address: fake_client,
                consumer_runner=lambda *_a, **_kw: child_result("memfd"))
            self.assertTrue(passed)
            self.assertEqual(fake_client.start_kwargs["source"], "monitor")
            self.assertEqual(fake_client.start_kwargs["parent_window"], "x11:0x55")
            self.assertEqual(metadata["selection"]["types"], screencast.SOURCE_MONITOR)
            self.assertEqual(metadata["sources"][0]["pipewire_serial"], 99)

    def test_portal_start_failure_still_writes_metadata_and_cleans_up(self):
        with tempfile.TemporaryDirectory() as temporary:
            args = probe_args(temporary)
            fake_client = FakeProbeClient(fail_start=True)
            metadata, passed = probe_screencast.execute(
                args, client_factory=lambda _address: fake_client,
                consumer_runner=lambda *_a, **_kw: child_result("memfd"))
            self.assertFalse(passed)
            self.assertTrue(fake_client.closed)
            self.assertTrue(metadata["cleanup"]["client_closed"])
            self.assertTrue(metadata["cleanup"]["session_closed"])
            self.assertTrue(Path(args.output).is_file())
            self.assertIn("unavailable", " ".join(metadata["errors"]).lower())

    def test_default_runner_inherits_only_the_consumer_fd(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "consumer.py"
            script.write_text(textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import argparse, json, os
                parser = argparse.ArgumentParser()
                parser.add_argument('--fd', type=int, required=True)
                parser.add_argument('--node', type=int, required=True)
                parser.add_argument('--target-serial', type=int)
                parser.add_argument('--mode', required=True)
                parser.add_argument('--frames', type=int, required=True)
                parser.add_argument('--timeout-ms', type=int, required=True)
                parser.add_argument('--render-node')
                parser.add_argument('--expect-pattern', action='store_true')
                args = parser.parse_args()
                os.fstat(args.fd)
                print(json.dumps({'type': 'format', 'format': 'BGRx'}))
                print(json.dumps({'type': 'frame', 'index': 0,
                                  'pixel_hash': 'sha256:abc',
                                  'color_counts': {'#000000': 4}}))
                print(json.dumps({'type': 'summary', 'passed': True,
                                  'frames': args.frames, 'buffer_type': 'MemFd',
                                  'mode': args.mode, 'format': 'BGRx',
                                  'gpu_import': False, 'cpu_readback': True,
                                  'pixel_hashes': ['sha256:abc'],
                                  'color_counts': {'#000000': 4}}))
                """))
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            read_fd, write_fd = os.pipe()
            try:
                child = probe_screencast.run_consumer(
                    script, read_fd, 7, serial=99, mode="memfd", frames=1,
                    timeout_ms=5000, render_node=None, expect_pattern=False)
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertEqual(child["returncode"], 0, child)
            self.assertEqual(child["summary"]["frames"], 1)
            self.assertEqual(child["summary"]["buffer_type"], "MemFd")
            self.assertEqual(child["frames_observed"], 1)
            self.assertEqual(probe_screencast._consumer_errors("memfd", 1, child), [])

    def test_default_runner_times_out_and_reaps_the_consumer(self):
        with tempfile.TemporaryDirectory() as temporary:
            script = Path(temporary) / "slow-consumer.py"
            script.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(5)\n")
            script.chmod(script.stat().st_mode | stat.S_IXUSR)
            read_fd, write_fd = os.pipe()
            try:
                child = probe_screencast.run_consumer(
                    script, read_fd, 7, serial=None, mode="memfd", frames=1,
                    timeout_ms=150)
            finally:
                os.close(read_fd)
                os.close(write_fd)
            self.assertTrue(child["timed_out"])
            self.assertNotEqual(child["returncode"], 0)
            self.assertTrue(probe_screencast._consumer_errors("memfd", 1, child))


if __name__ == "__main__":
    unittest.main()
