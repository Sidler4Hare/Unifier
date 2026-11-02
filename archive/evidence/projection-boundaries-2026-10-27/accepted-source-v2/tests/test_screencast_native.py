"""Offline tests for the bounded native PipeWire screen-cast consumer.

The native binary is built from ``fixtures/screencast_consumer.c`` with strict
warnings enabled.  No screen, portal, camera or audio content is captured here:
the tests exercise argument handling, NDJSON framing and the synthetic
memfd/mmap self-test built into the consumer.
"""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "fixtures" / "screencast_consumer.c"
PKG_MODULES = "libpipewire-0.3 libdrm gbm egl glesv2"
STRICT_FLAGS = [
    "-std=gnu11",
    "-O2",
    "-Wall",
    "-Wextra",
    "-Werror",
    "-Wshadow",
    "-Wformat=2",
    "-Werror=implicit-function-declaration",
]
ALLOWED_EVENT_TYPES = {"format", "frame", "error", "summary"}


def _pkg_config(flag):
    if shutil.which("pkg-config") is None:
        raise unittest.SkipTest("pkg-config is not installed")
    result = subprocess.run(
        ["pkg-config", flag, *PKG_MODULES.split()],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise unittest.SkipTest(
            "native build dependencies are missing: " + result.stderr.strip()
        )
    return result.stdout.split()


def parse_events(output):
    """Parse strict NDJSON; reject blank/non-JSON lines and unknown types."""
    events = []
    for number, line in enumerate(output.splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise AssertionError(
                f"stdout line {number} is not valid NDJSON: {line!r}: {error}"
            ) from error
        if not isinstance(event, dict) or "type" not in event:
            raise AssertionError(f"stdout line {number} is not an event: {event!r}")
        if event["type"] not in ALLOWED_EVENT_TYPES:
            raise AssertionError(f"unexpected event type: {event['type']!r}")
        events.append(event)
    return events


class NativeConsumerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not SOURCE.is_file():
            raise unittest.SkipTest(f"missing source: {SOURCE}")
        if shutil.which("cc") is None:
            raise unittest.SkipTest("cc is not installed")
        cls.temporary = tempfile.TemporaryDirectory(prefix="screencast-native-")
        cls.binary = Path(cls.temporary.name) / "screencast_consumer"
        command = [
            "cc",
            *STRICT_FLAGS,
            *_pkg_config("--cflags"),
            str(SOURCE),
            "-o",
            str(cls.binary),
            *_pkg_config("--libs"),
        ]
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=120
        )
        if result.returncode != 0:
            raise AssertionError(
                "strict native build failed:\n"
                + result.stdout
                + "\n"
                + result.stderr
            )

    @classmethod
    def tearDownClass(cls):
        cls.temporary.cleanup()

    def run_consumer(self, *arguments, timeout=10):
        return subprocess.run(
            [str(self.binary), *map(str, arguments)],
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    def assert_failure_summary(self, result, code):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        events = parse_events(result.stdout)
        self.assertTrue(events, "consumer produced no NDJSON events")
        summary = events[-1]
        self.assertEqual(summary["type"], "summary")
        self.assertIs(summary["passed"], False)
        self.assertIn("error", summary)
        self.assertEqual(summary["error"]["code"], code)
        errors = [event for event in events if event["type"] == "error"]
        self.assertTrue(errors, "failure must emit an error event")
        self.assertEqual(errors[-1]["code"], code)
        return summary

    def test_self_test_passes_and_uses_real_memfd(self):
        result = self.run_consumer("--self-test")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        events = parse_events(result.stdout)
        self.assertTrue(events)
        self.assertTrue(all(event["type"] == "summary" for event in events))
        summary = events[-1]
        self.assertEqual(summary["type"], "summary")
        self.assertIs(summary["passed"], True)
        self.assertIs(summary["self_test"], True)
        self.assertEqual(summary["failed"], 0)
        self.assertGreaterEqual(summary["checks_total"], 20)
        self.assertGreater(summary["peak_memfd_bytes"], 0)
        checks = summary["checks"]
        self.assertEqual(len(checks), summary["checks_total"])
        self.assertTrue(all(check["passed"] for check in checks))
        names = {check["name"] for check in checks}
        for required in (
            "memfd_create",
            "memfd_mmap",
            "pixel_interpretation",
            "chunk_offset",
            "row_padding_ignored",
            "alpha_ignored",
            "reject_zero_stride",
            "reject_negative_stride",
            "reject_short_stride",
            "reject_truncated_chunk",
            "reject_offset_outside_buffer",
            "reject_chunk_past_buffer",
            "reject_visible_past_buffer",
            "reject_unsupported_format",
            "hash_changes_with_pixels",
            "pattern_counts",
        ):
            self.assertIn(required, names)

    def test_self_test_does_not_emit_image_bytes(self):
        result = self.run_consumer("--self-test")
        self.assertEqual(result.returncode, 0)
        for event in parse_events(result.stdout):
            self.assertNotIn("data", event)
            self.assertNotIn("pixels", event)
            self.assertNotIn("image", event)
            # Self-test metadata only; no base64 or byte-array payloads.
            for value in event.values():
                self.assertNotIsInstance(value, (bytes, bytearray))

    def test_help_is_plain_usage(self):
        result = self.run_consumer("--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("Usage:", result.stdout)
        self.assertIn("--self-test", result.stdout)
        self.assertEqual(result.stderr, "")

    def test_invalid_arguments_are_explicit(self):
        cases = [
            ((), "no arguments supplied"),
            (("--fd",), "--fd"),
            (("--fd", "x", "--node", "1", "--mode", "memfd",
              "--frames", "1", "--timeout-ms", "10"), "--fd"),
            (("--fd", "0", "--node", "x", "--mode", "memfd",
              "--frames", "1", "--timeout-ms", "10"), "--node"),
            (("--fd", "0", "--node", "0", "--mode", "memfd",
              "--frames", "1", "--timeout-ms", "10"), "--node"),
            (("--fd", "0", "--node", "1", "--mode", "bogus",
              "--frames", "1", "--timeout-ms", "10"), "--mode"),
            (("--fd", "0", "--node", "1", "--mode", "memfd",
              "--frames", "0", "--timeout-ms", "10"), "--frames"),
            (("--fd", "0", "--node", "1", "--mode", "memfd",
              "--frames", "1", "--timeout-ms", "0"), "--timeout-ms"),
            (("--fd", "0", "--node", "1", "--mode", "memfd"), "--mode"),
            (("--fd", "0", "--node", "1", "--frames", "1",
              "--timeout-ms", "10"), "--mode"),
            (("--bogus",), "unknown argument"),
            (("--self-test", "--fd", "0"), "--self-test"),
            (("--self-test", "--mode", "memfd"), "--self-test"),
        ]
        for arguments, expected_text in cases:
            with self.subTest(arguments=arguments):
                result = self.run_consumer(*arguments)
                self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
                events = parse_events(result.stdout)
                self.assertEqual(events[-1]["type"], "summary")
                self.assertIs(events[-1]["passed"], False)
                error = next(event for event in events if event["type"] == "error")
                self.assertEqual(error["code"], "invalid_arguments")
                self.assertIn(expected_text, error["message"])

    def test_invalid_inherited_fd_fails_without_default_socket(self):
        result = self.run_consumer(
            "--fd", "999999", "--node", "1", "--mode", "memfd",
            "--frames", "1", "--timeout-ms", "50",
        )
        summary = self.assert_failure_summary(result, "pipewire_connect")
        self.assertEqual(summary["mode"], "memfd")
        self.assertEqual(summary["frames"], 0)
        self.assertIsNone(summary["buffer_type"])
        self.assertIs(summary["gpu_import"], False)
        self.assertIs(summary["cpu_readback"], False)

    def test_missing_render_node_fails_explicitly(self):
        result = self.run_consumer(
            "--fd", "999999", "--node", "1", "--mode", "dmabuf",
            "--frames", "1", "--timeout-ms", "50",
            "--render-node", "/nonexistent/render-node",
        )
        summary = self.assert_failure_summary(result, "egl_unsupported")
        self.assertEqual(summary["mode"], "dmabuf")
        self.assertIs(summary["gpu_import"], False)
        self.assertIs(summary["cpu_readback"], False)
        self.assertNotEqual(result.returncode, 0)

    def test_target_serial_is_reported_in_summary(self):
        result = self.run_consumer(
            "--fd", "999999", "--node", "7", "--target-serial", "42",
            "--mode", "memfd", "--frames", "1", "--timeout-ms", "50",
        )
        summary = self.assert_failure_summary(result, "pipewire_connect")
        self.assertIs(summary["target_by_serial"], True)
        self.assertEqual(summary["node"], 7)

    def test_inherited_socket_receives_handshake_and_times_out_without_frames(self):
        inherited, peer = socket.socketpair()
        try:
            child = subprocess.Popen(
                [str(self.binary), '--fd', str(inherited.fileno()), '--node', '1',
                 '--mode', 'memfd', '--frames', '1', '--timeout-ms', '100'],
                pass_fds=(inherited.fileno(),), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True,
                env={**os.environ, 'PIPEWIRE_REMOTE': 'multilinux-nonexistent-test'},
            )
            inherited.close()
            try:
                peer.settimeout(3)
                self.assertTrue(peer.recv(65536), 'no handshake on inherited FD')
                stdout, stderr = child.communicate(timeout=3)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.communicate(timeout=3)
            result = subprocess.CompletedProcess(child.args, child.returncode,
                                                 stdout, stderr)
            summary = self.assert_failure_summary(result, 'timeout')
            self.assertEqual(summary['frames'], 0)
            self.assertFalse(summary['cpu_readback'])
        finally:
            inherited.close()
            peer.close()

    def test_frame_limit_and_timeout_are_bounded(self):
        result = self.run_consumer(
            "--fd", "0", "--node", "1", "--mode", "memfd",
            "--frames", "100001", "--timeout-ms", "10",
        )
        self.assertEqual(result.returncode, 2)
        self.assert_failure_summary(result, "invalid_arguments")
        result = self.run_consumer(
            "--fd", "0", "--node", "1", "--mode", "memfd",
            "--frames", "1", "--timeout-ms", "3600001",
        )
        self.assertEqual(result.returncode, 2)
        self.assert_failure_summary(result, "invalid_arguments")


if __name__ == "__main__":
    unittest.main()
