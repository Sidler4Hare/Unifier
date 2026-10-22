"""Contract tests for the synthetic GBM/EGL/GLES DMA-BUF round trip fixture.

The C fixture owns its own GBM buffer, fills generated colour bars through a
CPU mapping, exports the real DMA-BUF plane fd, imports it into EGL/GLES and
verifies every visible pixel through glReadPixels.  No image bytes are stored
or written by the fixture.

Only this test file is run here; peer screencast tests are intentionally not
executed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "fixtures" / "dmabuf_roundtrip.c"
DEFAULT_RENDER_NODE = "/dev/dri/renderD128"
FOURCC_XRGB8888 = 875713112  # DRM_FORMAT_XRGB8888 ('XR24')
SCHEMA = "multilinux.dmabuf-roundtrip.v1"


class DmaBufRoundtripTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which("cc") is None:
            raise unittest.SkipTest("cc is not available")
        if shutil.which("pkg-config") is None:
            raise unittest.SkipTest("pkg-config is not available")
        if not SOURCE.is_file():
            raise unittest.SkipTest(f"fixture source not found: {SOURCE}")

        cls.tmpdir = tempfile.TemporaryDirectory(
            prefix="multilinux-dmabuf-roundtrip-"
        )
        cls.binary = Path(cls.tmpdir.name) / "dmabuf_roundtrip"
        cflags = cls._pkg_config("--cflags")
        libs = cls._pkg_config("--libs")
        command = [
            "cc",
            "-std=c11",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            *cflags,
            str(SOURCE),
            "-o",
            str(cls.binary),
            *libs,
        ]
        completed = subprocess.run(
            command,
            cwd=cls.tmpdir.name,
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode != 0:
            raise AssertionError(
                "strict C build failed\n"
                f"command: {' '.join(command)}\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        if not cls.binary.is_file():
            raise AssertionError("compiler returned success without a binary")

    @classmethod
    def tearDownClass(cls) -> None:
        tmpdir = getattr(cls, "tmpdir", None)
        if tmpdir is not None:
            tmpdir.cleanup()

    @staticmethod
    def _pkg_config(flag: str) -> list[str]:
        completed = subprocess.run(
            ["pkg-config", flag, "gbm", "egl", "glesv2", "libdrm"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if completed.returncode != 0:
            raise unittest.SkipTest(
                "pkg-config cannot resolve gbm egl glesv2 libdrm: "
                f"{completed.stderr.strip()}"
            )
        return completed.stdout.split()

    @staticmethod
    def _env() -> dict[str, str]:
        env = os.environ.copy()
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        return env

    def _run(self, *args: str, timeout: float = 90) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(self.binary), *args],
            cwd=self.tmpdir.name,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._env(),
        )

    def _parse_summary(self, completed: subprocess.CompletedProcess[str]) -> dict:
        self.assertNotIn(
            "\x00", completed.stdout, "stdout contains binary/image bytes"
        )
        self.assertLess(
            len(completed.stdout),
            100_000,
            "stdout is unexpectedly large for a metadata-only summary",
        )
        lines = completed.stdout.splitlines()
        self.assertEqual(len(lines), 1, f"expected exactly one JSON line, got {len(lines)}")
        try:
            summary = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            self.fail(f"stdout is not valid JSON: {exc}: {lines[0][:500]}")
        self.assertIsInstance(summary, dict)
        self.assertEqual(summary.get("schema"), SCHEMA)
        return summary

    def _require_render_node(self) -> str:
        node = os.environ.get("MULTILINUX_RENDER_NODE", DEFAULT_RENDER_NODE)
        if not os.path.exists(node):
            self.skipTest(f"render node not present: {node}")
        try:
            fd = os.open(node, os.O_RDWR | os.O_CLOEXEC)
        except OSError as exc:
            self.skipTest(f"render node not accessible: {node}: {exc}")
        else:
            os.close(fd)
        return node

    def test_invalid_arguments_are_explicit_errors(self) -> None:
        cases = [
            ("--definitely-unknown", "unknown argument"),
            ("--width", "missing value for --width"),
            ("--width", "0", "invalid --width"),
            ("--height", "4096", "invalid --height"),
            ("--tolerance", "99", "invalid --tolerance"),
            ("--modifier", "nonsense", "invalid --modifier"),
            ("--render-node", "missing value for --render-node"),
        ]
        for case in cases:
            *args, expected = case
            with self.subTest(args=args):
                completed = self._run(*args)
                self.assertEqual(
                    completed.returncode,
                    2,
                    f"expected argument error exit 2 for {args!r}, "
                    f"stderr={completed.stderr!r}",
                )
                summary = self._parse_summary(completed)
                self.assertFalse(summary["passed"])
                self.assertEqual(summary["status"], "error")
                self.assertEqual(summary["stage"], "args")
                self.assertIn(expected, summary["error"])
                self.assertTrue(summary["resources_released"])

    def test_missing_device_is_an_explicit_device_error(self) -> None:
        completed = self._run(
            "--render-node", "/nonexistent/multilinux-dmabuf-renderD999"
        )
        self.assertEqual(completed.returncode, 3, completed.stderr)
        summary = self._parse_summary(completed)
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["status"], "error")
        self.assertEqual(summary["stage"], "device")
        self.assertIn("cannot open render node", summary["error"])
        self.assertFalse(summary["import"]["gpu_import_successful"])
        self.assertFalse(summary["readback"]["cpu_readback_performed"])
        self.assertTrue(summary["resources_released"])

    def test_explicit_non_linear_modifier_is_unsupported_not_success(self) -> None:
        node = self._require_render_node()
        completed = self._run(
            "--render-node", node, "--modifier", "0x0100000000000002"
        )
        self.assertEqual(
            completed.returncode,
            4,
            f"expected explicit unsupported exit 4, stderr={completed.stderr!r}",
        )
        summary = self._parse_summary(completed)
        self.assertFalse(summary["passed"])
        self.assertEqual(summary["status"], "unsupported")
        self.assertFalse(summary["import"]["gpu_import_successful"])
        self.assertFalse(summary["readback"]["cpu_readback_performed"])
        self.assertTrue(summary["resources_released"])

    def test_gpu_dmabuf_roundtrip_when_render_node_is_accessible(self) -> None:
        node = self._require_render_node()
        width, height = 96, 53
        completed = self._run(
            "--render-node",
            node,
            "--width",
            str(width),
            "--height",
            str(height),
            "--tolerance",
            "2",
        )
        self.assertEqual(
            completed.returncode,
            0,
            "GPU DMA-BUF round trip failed\n"
            f"stdout:\n{completed.stdout}\n"
            f"stderr:\n{completed.stderr}",
        )
        summary = self._parse_summary(completed)
        self.assertTrue(summary["passed"])
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["render_node"], node)

        self.assertTrue(summary["gpu"]["gl_renderer"])
        self.assertTrue(summary["gpu"]["gl_vendor"])
        self.assertTrue(summary["egl"]["image_dma_buf_import"])
        self.assertTrue(summary["egl"]["xrgb8888_supported"])
        if summary["egl"]["format_query_performed"]:
            self.assertGreater(summary["egl"]["dma_buf_format_count"], 0)

        buffer_info = summary["buffer"]
        self.assertEqual(buffer_info["format"], "XRGB8888")
        self.assertEqual(buffer_info["fourcc"], FOURCC_XRGB8888)
        self.assertEqual(buffer_info["width"], width)
        self.assertEqual(buffer_info["height"], height)
        self.assertEqual(buffer_info["plane_count"], 1)
        self.assertGreaterEqual(buffer_info["stride"], width * 4)
        self.assertGreaterEqual(buffer_info["offset"], 0)
        self.assertIn("modifier", buffer_info)
        self.assertTrue(buffer_info["modifier_name"])
        self.assertEqual(len(buffer_info["planes"]), 1)
        plane = buffer_info["planes"][0]
        self.assertEqual(plane["plane"], 0)
        self.assertGreaterEqual(plane["stride"], width * 4)
        self.assertGreaterEqual(plane["offset"], 0)
        self.assertTrue(
            plane["fd_closed"],
            "the exported DMA-BUF fd must be closed after import/cleanup",
        )

        imported = summary["import"]
        self.assertTrue(imported["gpu_import_successful"])
        self.assertTrue(imported["egl_image_created"])
        self.assertTrue(imported["texture_created"])
        self.assertTrue(imported["fbo_complete"])
        self.assertEqual(
            imported["method"],
            "EGL_EXT_image_dma_buf_import + "
            "glEGLImageTargetTexture2DOES + FBO",
        )

        readback = summary["readback"]
        self.assertTrue(readback["cpu_readback_performed"])
        self.assertEqual(readback["total_pixels"], width * height)
        self.assertEqual(readback["matching_pixels"], width * height)
        self.assertEqual(readback["mismatched_pixels"], 0)
        self.assertLessEqual(readback["max_channel_delta"], 2)
        self.assertIn(readback["orientation"], {"direct", "y-flipped"})
        self.assertRegex(readback["rgb_fnv1a64"], r"^0x[0-9a-f]{16}$")

        self.assertFalse(summary["zero_copy"])
        self.assertTrue(summary["resources_released"])


if __name__ == "__main__":
    unittest.main()
