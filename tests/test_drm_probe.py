"""Offline checks for the DRM fixture's result and cleanup boundaries."""

from contextlib import ExitStack
import ctypes
from pathlib import Path
import struct
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'fixtures' / 'hardware'))
import drm_egl_probe as probe


class DrmProbeTests(unittest.TestCase):
    def test_single_wrong_pixel_at_scissor_edge_fails(self):
        frame = bytearray().join(
            b'\x00\xff\x00\xff' if 64 <= x < 192 and 64 <= y < 192 else b'\xff\x00\x00\xff'
            for y in range(256) for x in range(256)
        )
        probe.verify_pixels(frame)
        frame[4 * (191 * 256 + 192)] = 0

        with self.assertRaises(probe.ProbeError) as raised:
            probe.verify_pixels(frame)
        self.assertEqual(raised.exception.reason, 'pixel-mismatch')
        self.assertIn('(192, 191)', str(raised.exception))

    def test_wrong_software_renderer_closes_gbm_egl_and_fd_before_reporting(self):
        cleanup = []
        fd, device, display, config, context = 51, 52, 53, 54, 55
        gbm = Mock()
        gbm.gbm_create_device.return_value = device
        gbm.gbm_device_get_fd.return_value = fd
        gbm.gbm_device_destroy.side_effect = lambda *_: cleanup.append('destroy-gbm')
        egl = Mock()
        egl.eglGetPlatformDisplay.return_value = display
        egl.eglCreateContext.return_value = context
        egl.eglDestroyContext.side_effect = lambda *_: cleanup.append('destroy-context') or 1
        egl.eglTerminate.side_effect = lambda *_: cleanup.append('terminate') or 1
        egl.eglReleaseThread.side_effect = lambda *_: cleanup.append('release-thread') or 1

        def initialize(_, major, minor):
            major._obj.value, minor._obj.value = 1, 5
            return 1

        def choose_config(_, attrs, handle, size, count):
            handle._obj.value, count._obj.value = config, 1
            return 1

        def make_current(dpy, draw, read, ctx):
            if ctx is None:
                cleanup.append('unbind')
            return 1

        egl.eglInitialize.side_effect = initialize
        egl.eglChooseConfig.side_effect = choose_config
        egl.eglMakeCurrent.side_effect = make_current

        def egl_string(handle, name):
            return {
                (None, probe.EGL_EXTENSIONS): b'EGL_KHR_platform_gbm',
                (display, probe.EGL_EXTENSIONS): b'EGL_MESA_query_driver',
                (display, probe.EGL_VENDOR): b'Mesa Project',
            }[handle, name]

        egl.eglQueryString.side_effect = egl_string
        gles = Mock()
        gles.glGetString.side_effect = {
            probe.GL_VENDOR: b'Mesa',
            probe.GL_RENDERER: b'llvmpipe (LLVM 19.1, 256 bits)',
            probe.GL_VERSION: b'OpenGL ES 3.2 Mesa 25',
        }.__getitem__

        def open_node(stack, pci, result):
            stack.callback(cleanup.append, 'close-fd')
            result['device'] = {'kernel_driver': 'i915'}
            return fd

        with (patch.object(probe, 'open_node', side_effect=open_node),
              patch.object(probe, 'load_gbm', return_value=gbm),
              patch.object(probe, 'load_egl', return_value=egl),
              patch.object(probe, 'load_gles', return_value=gles),
              patch.object(probe, 'egl_extension', return_value=lambda _: b'iris')):
            result = probe.run_probe('render', '0000:00:02.0')

        self.assertEqual((result['status'], result['reason']), ('BLOCKED', 'software-renderer'))
        gles.glGenFramebuffers.assert_not_called()
        self.assertEqual(cleanup, ['unbind', 'destroy-context', 'terminate',
                                   'release-thread', 'destroy-gbm', 'close-fd'])

    def test_fence_timeout_releases_draw_resources_without_readback(self):
        egl = Mock()
        egl.eglCreateSync.return_value = 123
        egl.eglClientWaitSync.return_value = probe.EGL_TIMEOUT_EXPIRED
        gl = Mock()
        gl.glGetError.return_value = 0
        gl.glCheckFramebufferStatus.return_value = probe.GL_FRAMEBUFFER_COMPLETE

        with self.assertRaises(probe.ProbeError) as raised, ExitStack() as stack:
            probe.draw(stack, [], egl, 99, gl, {})

        self.assertEqual((raised.exception.status, raised.exception.reason),
                         ('INVALID', 'gpu-fence-timeout'))
        egl.eglClientWaitSync.assert_called_once_with(99, 123, probe.EGL_SYNC_FLUSH_COMMANDS_BIT,
                                                     2_000_000_000)
        egl.eglDestroySync.assert_called_once_with(99, 123)
        gl.glDeleteFramebuffers.assert_called_once()
        gl.glDeleteRenderbuffers.assert_called_once()
        gl.glReadPixels.assert_not_called()

    def test_radeon_ioctl_abi_and_memory_reserve(self):
        mib = 1_048_576
        fd = 41
        gart = 5_368_709_120  # More than 32 bits.

        def open_node(stack, pci, result):
            result['device'] = {'kernel_driver': 'radeon'}
            return fd

        for mode, total, visible, used, reserve, outcome in (
            ('metadata', 1024 * mib, 256 * mib, 5 * mib + 39, 256 * mib, ('PASS', 'verified')),
            ('render', 256 * mib, 64 * mib, 112 * mib + 1, 128 * mib,
             ('BLOCKED', 'vram-reserve-insufficient')),
        ):
            with self.subTest(mode=mode):
                used_counter = ctypes.c_uint64()
                counter_address = ctypes.addressof(used_counter)
                commands = []

                def ioctl(received_fd, command, buffer):
                    self.assertEqual(received_fd, fd)
                    commands.append(command)
                    if command == 0xC018645C:
                        self.assertIsInstance(buffer, bytearray)
                        self.assertEqual(buffer, bytes(24))
                        buffer[:] = struct.pack('=QQQ', gart, total, visible)
                    else:
                        self.assertEqual(command, 0xC0106467)
                        self.assertEqual(struct.unpack('=IIQ', buffer),
                                         (0x1E, 0, counter_address))
                        ctypes.memmove(counter_address, struct.pack('=Q', used), 8)
                    return 0

                with (patch.object(probe.os, 'uname', return_value=SimpleNamespace(machine='x86_64')),
                      patch.object(probe.C, 'c_uint64', return_value=used_counter),
                      patch.object(probe.fcntl, 'ioctl', side_effect=ioctl),
                      patch.object(probe, 'open_node', side_effect=open_node),
                      patch.object(probe, 'render') as render,
                      patch.object(probe, 'load_library') as load_library):
                    result = probe.run_probe(mode, '0000:01:00.0')

                self.assertEqual(commands, [0xC018645C, 0xC0106467])
                self.assertEqual((result['status'], result['reason']), outcome)
                self.assertEqual(result['vram'], {
                    'source': 'radeon-drm-ioctl', 'gart_bytes': gart,
                    'total_bytes': total, 'visible_bytes': visible, 'used_bytes': used,
                    'free_bytes': total - used, 'reserve_bytes': reserve,
                })
                render.assert_not_called()
                load_library.assert_not_called()

    def test_failed_egl_cleanup_preserves_errors_and_overrides_pass(self):
        egl = Mock()
        egl.eglDestroyContext.return_value = 0
        egl.eglTerminate.return_value = 0
        egl.eglGetError.side_effect = [0x3006, 0x3008]
        close_fd = Mock()

        def open_node(stack, pci, result):
            result['device'] = {'kernel_driver': 'i915'}
            stack.callback(close_fd, 41)
            return 41

        def render(stack, errors, fd, pci, result):
            probe.cleanup_egl(stack, errors, egl, 'eglTerminate', 51)
            probe.cleanup_egl(stack, errors, egl, 'eglDestroyContext', 51, 61)

        with (patch.object(probe, 'open_node', side_effect=open_node),
              patch.object(probe, 'render', side_effect=render)):
            result = probe.run_probe('render', '0000:00:02.0')

        self.assertEqual((result['status'], result['reason']), ('UNKNOWN', 'cleanup-unknown'))
        self.assertEqual(result['outcome_before_cleanup'], 'PASS')
        self.assertEqual(result['cleanup_errors'], [
            'eglDestroyContext: EGL error 0x3006', 'eglTerminate: EGL error 0x3008',
        ])
        egl.eglDestroyContext.assert_called_once_with(51, 61)
        egl.eglTerminate.assert_called_once_with(51)
        close_fd.assert_called_once_with(41)


if __name__ == '__main__':
    unittest.main()
