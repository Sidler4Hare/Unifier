#!/usr/bin/env python3
"""Single-device Mesa GBM/EGL fixture. Run under the DRM controller deadline."""

import argparse
from contextlib import ExitStack
import ctypes as C
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import struct


NODE = Path('/dev/dri/renderD128')
DEVICE_NUMBER = (226, 128)
# PCI BDF -> sysfs vendor/device, kernel driver and Mesa driver.
TARGETS = {
    '0000:00:02.0': ('8086', '9a49', 'i915', 'iris'),
    '0000:01:00.0': ('1002', '6778', 'radeon', 'r600'),
}
SIZE = 256
FENCE_NS = 2_000_000_000
MIB = 1024 * 1024
PAYLOAD_LIMIT = 16 * MIB
EXIT_CODES = {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2, 'INVALID': 3, 'UNKNOWN': 4}

# Linux x86_64 UAPI: DRM_IOWR(DRM_COMMAND_BASE + command, structure).
DRM_IOCTL_RADEON_GEM_INFO = 0xC018645C  # drm_radeon_gem_info: 3 x u64
DRM_IOCTL_RADEON_INFO = 0xC0106467      # drm_radeon_info: u32, pad, u64
RADEON_INFO_VRAM_USAGE = 0x1E

EGL_NONE = 0x3038
EGL_VENDOR = 0x3053
EGL_EXTENSIONS = 0x3055
EGL_SURFACE_TYPE = 0x3033
EGL_RENDERABLE_TYPE = 0x3040
EGL_OPENGL_ES2_BIT = 0x0004
EGL_OPENGL_ES_API = 0x30A0
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_PLATFORM_GBM_KHR = 0x31D7
EGL_DEVICE_EXT = 0x322C
EGL_DRM_RENDER_NODE_FILE_EXT = 0x3377
EGL_SYNC_FENCE = 0x30F9
EGL_SYNC_FLUSH_COMMANDS_BIT = 0x0001
EGL_CONDITION_SATISFIED = 0x30F6
EGL_TIMEOUT_EXPIRED = 0x30F5
EGL_BAD_ALLOC = 0x3003

GL_VENDOR = 0x1F00
GL_RENDERER = 0x1F01
GL_VERSION = 0x1F02
GL_RENDERBUFFER = 0x8D41
GL_FRAMEBUFFER = 0x8D40
GL_FRAMEBUFFER_COMPLETE = 0x8CD5
GL_COLOR_ATTACHMENT0 = 0x8CE0
GL_RGBA4 = 0x8056
GL_RGBA = 0x1908
GL_UNSIGNED_BYTE = 0x1401
GL_DITHER = 0x0BD0
GL_SCISSOR_TEST = 0x0C11
GL_COLOR_BUFFER_BIT = 0x4000
GL_OUT_OF_MEMORY = 0x0505


class ProbeError(Exception):
    def __init__(self, status, reason, detail):
        super().__init__(detail)
        self.status = status
        self.reason = reason


def require(condition, status, reason, detail):
    if not condition:
        raise ProbeError(status, reason, detail)


def node_number(node_stat):
    require(stat.S_ISCHR(node_stat.st_mode), 'BLOCKED', 'wrong-device',
            f'{NODE} is not a character device')
    return os.major(node_stat.st_rdev), os.minor(node_stat.st_rdev)


def read_identity(number, pci):
    require(number == DEVICE_NUMBER, 'BLOCKED', 'wrong-device',
            f'expected {DEVICE_NUMBER}, got {number}')
    entry = Path('/sys/dev/char') / f'{number[0]}:{number[1]}'
    try:
        device = (entry / 'device').resolve(strict=True)
        driver = (device / 'driver').resolve(strict=True).name
        vendor = (device / 'vendor').read_text(encoding='ascii').strip().removeprefix('0x')
        model = (device / 'device').read_text(encoding='ascii').strip().removeprefix('0x')
    except OSError as exc:
        raise ProbeError('BLOCKED', 'sysfs-identity-unavailable', str(exc)) from exc
    expected = TARGETS[pci][:3]
    require(device.name == pci and (vendor, model, driver) == expected,
            'BLOCKED', 'wrong-device', f'got {device.name} {vendor}:{model} {driver}')
    return {'pci': pci, 'vendor': vendor, 'device': model, 'kernel_driver': driver}


def open_node(stack, pci, result):
    try:
        before = NODE.lstat()
        read_identity(node_number(before), pci)
        fd = os.open(NODE, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError as exc:
        raise ProbeError('BLOCKED', 'device-missing', str(exc)) from exc
    except PermissionError as exc:
        raise ProbeError('BLOCKED', 'permission-denied', str(exc)) from exc
    except OSError as exc:
        raise ProbeError('BLOCKED', 'device-open-failed', str(exc)) from exc

    stack.callback(os.close, fd)
    opened = os.fstat(fd)
    require(os.path.samestat(before, opened), 'BLOCKED', 'wrong-device',
            'node changed while opening')
    number = node_number(opened)
    result['device'] = {
        'node': str(NODE), 'major': number[0], 'minor': number[1],
        'euid': os.geteuid(), 'open_mode': 'rw', **read_identity(number, pci),
    }
    return fd


def radeon_memory(fd, result):
    require(os.uname().machine == 'x86_64', 'BLOCKED', 'unsupported-ioctl-abi',
            'the Radeon memory query is scoped to the approved x86_64 hosts')
    memory = result['vram'] = {'source': 'radeon-drm-ioctl', 'total_bytes': None,
                              'used_bytes': None}
    try:
        gem = bytearray(24)
        fcntl.ioctl(fd, DRM_IOCTL_RADEON_GEM_INFO, gem)
        gart, total, visible = struct.unpack('=QQQ', gem)
        memory.update(total_bytes=total, visible_bytes=visible, gart_bytes=gart)

        used = C.c_uint64()
        usage = struct.pack('=IIQ', RADEON_INFO_VRAM_USAGE, 0, C.addressof(used))
        fcntl.ioctl(fd, DRM_IOCTL_RADEON_INFO, usage)
        memory['used_bytes'] = used.value
    except OSError as exc:
        raise ProbeError('BLOCKED', 'vram-query-unavailable', str(exc)) from exc
    require(total > 0 and used.value <= total and visible <= total,
            'BLOCKED', 'vram-query-invalid', str(memory))
    reserve = max((total + 3) // 4, 128 * MIB)
    memory.update(free_bytes=total - used.value, reserve_bytes=reserve)
    return memory


def load_library(soname, signatures):
    try:
        lib = C.CDLL(soname)
        for name, result, *arguments in signatures:
            func = getattr(lib, name)
            func.restype = result
            func.argtypes = arguments
    except (OSError, AttributeError) as exc:
        raise ProbeError('BLOCKED', 'userspace-library-unavailable',
                         f'{soname}: {exc}') from exc
    return lib


def load_gbm():
    return load_library('libgbm.so.1', (
        ('gbm_create_device', C.c_void_p, C.c_int),
        ('gbm_device_get_fd', C.c_int, C.c_void_p),
        ('gbm_device_destroy', None, C.c_void_p),
    ))


def load_egl():
    handle, uint, integer = C.c_void_p, C.c_uint, C.c_int
    intp, handlep = C.POINTER(integer), C.POINTER(handle)
    # EGLAttrib is intptr_t; EGLTime is an unsigned 64-bit nanosecond count.
    attribp = C.POINTER(C.c_ssize_t)
    return load_library('libEGL.so.1', (
        ('eglQueryString', C.c_char_p, handle, integer),
        ('eglGetProcAddress', handle, C.c_char_p),
        ('eglGetError', integer),
        ('eglGetPlatformDisplay', handle, uint, handle, attribp),
        ('eglInitialize', uint, handle, intp, intp),
        ('eglTerminate', uint, handle),
        ('eglReleaseThread', uint),
        ('eglBindAPI', uint, uint),
        ('eglChooseConfig', uint, handle, intp, handlep, integer, intp),
        ('eglCreateContext', handle, handle, handle, handle, intp),
        ('eglDestroyContext', uint, handle, handle),
        ('eglMakeCurrent', uint, handle, handle, handle, handle),
        ('eglCreateSync', handle, handle, uint, attribp),
        ('eglClientWaitSync', integer, handle, handle, integer, C.c_uint64),
        ('eglDestroySync', uint, handle, handle),
    ))


def load_gles():
    uint, integer, float32 = C.c_uint, C.c_int, C.c_float
    uintp = C.POINTER(uint)
    return load_library('libGLESv2.so.2', (
        ('glGetError', uint),
        ('glGetString', C.c_char_p, uint),
        ('glGenRenderbuffers', None, integer, uintp),
        ('glDeleteRenderbuffers', None, integer, uintp),
        ('glBindRenderbuffer', None, uint, uint),
        ('glRenderbufferStorage', None, uint, uint, integer, integer),
        ('glGenFramebuffers', None, integer, uintp),
        ('glDeleteFramebuffers', None, integer, uintp),
        ('glBindFramebuffer', None, uint, uint),
        ('glFramebufferRenderbuffer', None, uint, uint, uint, uint),
        ('glCheckFramebufferStatus', uint, uint),
        ('glDisable', None, uint),
        ('glEnable', None, uint),
        ('glScissor', None, integer, integer, integer, integer),
        ('glClearColor', None, float32, float32, float32, float32),
        ('glClear', None, uint),
        ('glReadPixels', None, integer, integer, integer, integer, uint, uint, C.c_void_p),
    ))


def egl_extension(egl, name, result, *arguments):
    address = egl.eglGetProcAddress(name.encode('ascii'))
    require(address, 'BLOCKED', 'egl-extension-unavailable', name)
    return C.CFUNCTYPE(result, *arguments)(address)


def check_egl(egl, value, name, status='BLOCKED', reason='egl-backend-unavailable'):
    if not value:
        code = egl.eglGetError()
        if code == EGL_BAD_ALLOC:
            status, reason = 'INVALID', 'egl-allocation-failed'
        raise ProbeError(status, reason, f'{name}: EGL error 0x{code:04x}')
    return value


def check_gl(gl, stage):
    code = gl.glGetError()
    if code == GL_OUT_OF_MEMORY:
        raise ProbeError('INVALID', 'gl-allocation-failed', stage)
    require(code == 0, 'FAIL', 'gl-error', f'{stage}: GL error 0x{code:04x}')


def cleanup_egl(stack, errors, egl, name, *arguments):
    def cleanup():
        if not getattr(egl, name)(*arguments):
            errors.append(f'{name}: EGL error 0x{egl.eglGetError():04x}')

    stack.callback(cleanup)


def decode(value):
    return value.decode('ascii', errors='replace') if value else ''


def verify_backend(pci, backend):
    names = (backend['mesa_driver'].lower(), backend['gl_renderer'].lower())
    software_names = ('llvmpipe', 'softpipe', 'swrast', 'software', 'swiftshader', 'lavapipe')
    require(not any(marker in name for marker in software_names for name in names),
            'BLOCKED', 'software-renderer', backend['gl_renderer'])
    expected = TARGETS[pci][3]
    family = ('intel',) if expected == 'iris' else ('amd', 'ati', 'radeon', 'caicos')
    require('mesa' in backend['egl_vendor'].lower() and names[0] == expected
            and any(marker in names[1] for marker in family)
            and backend['gl_version'].startswith('OpenGL ES '),
            'BLOCKED', 'unexpected-renderer', str(backend))


def verify_egl_node(egl, display, client_extensions, fd, pci, backend):
    require(client_extensions.intersection({'EGL_EXT_device_base', 'EGL_EXT_device_query'}),
            'BLOCKED', 'egl-device-query-unavailable', 'EGL_EXT_device_query')
    query_display = egl_extension(egl, 'eglQueryDisplayAttribEXT', C.c_uint,
                                  C.c_void_p, C.c_int, C.POINTER(C.c_ssize_t))
    query_device = egl_extension(egl, 'eglQueryDeviceStringEXT', C.c_char_p,
                                 C.c_void_p, C.c_int)
    device = C.c_ssize_t()
    check_egl(egl, query_display(display, EGL_DEVICE_EXT, C.byref(device)),
              'eglQueryDisplayAttribEXT', reason='egl-device-query-unavailable')
    extensions = decode(query_device(device.value, EGL_EXTENSIONS)).split()
    require('EGL_EXT_device_drm_render_node' in extensions, 'BLOCKED',
            'egl-device-query-unavailable', 'EGL_EXT_device_drm_render_node')
    node = decode(query_device(device.value, EGL_DRM_RENDER_NODE_FILE_EXT))
    backend['egl_render_node'] = node
    require(node == str(NODE), 'BLOCKED', 'wrong-egl-device',
            f'EGL reported {node!r}, expected {NODE}')
    require(os.path.samestat(NODE.lstat(), os.fstat(fd)),
            'BLOCKED', 'wrong-egl-device', 'the EGL render node differs from the opened node')
    read_identity(node_number(os.fstat(fd)), pci)


def verify_pixels(pixels):
    require(len(pixels) == SIZE * SIZE * 4, 'FAIL', 'pixel-size-mismatch',
            f'RGBA8 readback contained {len(pixels)} bytes')
    red, green = b'\xff\x00\x00\xff', b'\x00\xff\x00\xff'
    row = red * SIZE
    inset_row = red * 64 + green * 128 + red * 64
    expected = row * 64 + inset_row * 128 + row * 64
    if pixels != expected:
        offset = next(i for i in range(0, len(expected), 4)
                      if pixels[i:i + 4] != expected[i:i + 4])
        x, y = (offset // 4) % SIZE, (offset // 4) // SIZE
        raise ProbeError('FAIL', 'pixel-mismatch',
                         f'({x}, {y}): got {pixels[offset:offset+4].hex()}, '
                         f'expected {expected[offset:offset+4].hex()}')


def draw(stack, errors, egl, display, gl, result):
    renderbuffer, framebuffer = C.c_uint(), C.c_uint()
    gl.glGenRenderbuffers(1, C.byref(renderbuffer))
    stack.callback(gl.glDeleteRenderbuffers, 1, C.byref(renderbuffer))
    gl.glBindRenderbuffer(GL_RENDERBUFFER, renderbuffer)
    gl.glRenderbufferStorage(GL_RENDERBUFFER, GL_RGBA4, SIZE, SIZE)
    gl.glGenFramebuffers(1, C.byref(framebuffer))
    stack.callback(gl.glDeleteFramebuffers, 1, C.byref(framebuffer))
    gl.glBindFramebuffer(GL_FRAMEBUFFER, framebuffer)
    gl.glFramebufferRenderbuffer(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                                 GL_RENDERBUFFER, renderbuffer)
    status = gl.glCheckFramebufferStatus(GL_FRAMEBUFFER)
    check_gl(gl, 'framebuffer setup')
    require(status == GL_FRAMEBUFFER_COMPLETE, 'BLOCKED', 'framebuffer-unavailable',
            f'GL framebuffer status 0x{status:04x}')

    gl.glDisable(GL_DITHER)
    gl.glClearColor(1, 0, 0, 1)
    gl.glClear(GL_COLOR_BUFFER_BIT)
    gl.glEnable(GL_SCISSOR_TEST)
    gl.glScissor(64, 64, 128, 128)
    gl.glClearColor(0, 1, 0, 1)
    gl.glClear(GL_COLOR_BUFFER_BIT)
    check_gl(gl, 'drawing')

    sync = check_egl(egl, egl.eglCreateSync(display, EGL_SYNC_FENCE, None),
                     'eglCreateSync', reason='egl-fence-unavailable')
    cleanup_egl(stack, errors, egl, 'eglDestroySync', display, sync)
    wait = egl.eglClientWaitSync(display, sync, EGL_SYNC_FLUSH_COMMANDS_BIT, FENCE_NS)
    if wait == EGL_TIMEOUT_EXPIRED:
        raise ProbeError('INVALID', 'gpu-fence-timeout', 'the two-second fence expired')
    check_egl(egl, wait == EGL_CONDITION_SATISFIED, 'eglClientWaitSync',
              status='FAIL', reason='egl-fence-wait-failed')

    # The independent controller deadline also covers driver calls and readback.
    pixels = (C.c_ubyte * (SIZE * SIZE * 4))()
    gl.glReadPixels(0, 0, SIZE, SIZE, GL_RGBA, GL_UNSIGNED_BYTE, pixels)
    check_gl(gl, 'readback')
    actual = bytes(pixels)
    result['frame'] = {'width': SIZE, 'height': SIZE, 'rgba8_bytes': len(actual),
                       'clear_count': 2, 'sha256': hashlib.sha256(actual).hexdigest()}
    verify_pixels(actual)


def render(stack, errors, fd, pci, result):
    gbm, egl, gl = load_gbm(), load_egl(), load_gles()
    device = gbm.gbm_create_device(fd)
    require(device, 'BLOCKED', 'gbm-device-unavailable', 'gbm_create_device failed')
    stack.callback(gbm.gbm_device_destroy, device)
    require(gbm.gbm_device_get_fd(device) == fd, 'BLOCKED', 'wrong-gbm-device',
            'GBM did not retain the verified DRM fd')
    cleanup_egl(stack, errors, egl, 'eglReleaseThread')
    client_extensions = set(decode(egl.eglQueryString(None, EGL_EXTENSIONS)).split())
    require('EGL_KHR_platform_gbm' in client_extensions, 'BLOCKED',
            'egl-gbm-unavailable', 'EGL_KHR_platform_gbm is missing')
    display = check_egl(egl, egl.eglGetPlatformDisplay(EGL_PLATFORM_GBM_KHR, device, None),
                        'eglGetPlatformDisplay')
    major, minor = C.c_int(), C.c_int()
    check_egl(egl, egl.eglInitialize(display, C.byref(major), C.byref(minor)), 'eglInitialize')
    cleanup_egl(stack, errors, egl, 'eglTerminate', display)
    backend = result['backend'] = {
        'platform': 'gbm', 'gbm_fd_matches': True,
        'egl_vendor': decode(egl.eglQueryString(display, EGL_VENDOR)),
        'egl_version': f'{major.value}.{minor.value}',
    }
    require((major.value, minor.value) >= (1, 5), 'BLOCKED',
            'egl-fence-unavailable', 'EGL 1.5 is required for bounded fence wait')
    require('EGL_MESA_query_driver' in decode(egl.eglQueryString(display, EGL_EXTENSIONS)).split(),
            'BLOCKED', 'egl-driver-query-unavailable', 'EGL_MESA_query_driver is missing')
    get_driver = egl_extension(egl, 'eglGetDisplayDriverName', C.c_char_p, C.c_void_p)
    backend['mesa_driver'] = decode(get_driver(display))

    check_egl(egl, egl.eglBindAPI(EGL_OPENGL_ES_API), 'eglBindAPI')
    config_attributes = (C.c_int * 5)(EGL_SURFACE_TYPE, 0, EGL_RENDERABLE_TYPE,
                                     EGL_OPENGL_ES2_BIT, EGL_NONE)
    config, count = C.c_void_p(), C.c_int()
    check_egl(egl, egl.eglChooseConfig(display, config_attributes, C.byref(config), 1,
                                      C.byref(count)), 'eglChooseConfig')
    require(count.value == 1 and config.value, 'BLOCKED', 'egl-config-unavailable',
            'no configuration for OpenGL ES 2')
    context_attributes = (C.c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE)
    context = check_egl(egl, egl.eglCreateContext(display, config, None, context_attributes),
                        'eglCreateContext')
    cleanup_egl(stack, errors, egl, 'eglDestroyContext', display, context)
    check_egl(egl, egl.eglMakeCurrent(display, None, None, context), 'eglMakeCurrent',
              reason='egl-surfaceless-unavailable')
    cleanup_egl(stack, errors, egl, 'eglMakeCurrent', display, None, None, None)
    backend.update(surfaceless=True, gl_renderer=decode(gl.glGetString(GL_RENDERER)),
                   gl_vendor=decode(gl.glGetString(GL_VENDOR)),
                   gl_version=decode(gl.glGetString(GL_VERSION)))
    verify_backend(pci, backend)
    verify_egl_node(egl, display, client_extensions, fd, pci, backend)
    draw(stack, errors, egl, display, gl, result)


def run_probe(mode, pci):
    result = {'mode': mode, 'requested_pci': pci}
    cleanup_errors = []
    try:
        with ExitStack() as stack:
            fd = open_node(stack, pci, result)
            if result['device']['kernel_driver'] == 'radeon':
                memory = radeon_memory(fd, result)
                if mode == 'render':
                    require(memory['free_bytes'] >= memory['reserve_bytes'] + PAYLOAD_LIMIT,
                            'BLOCKED', 'vram-reserve-insufficient', str(memory))
            if mode == 'render':
                render(stack, cleanup_errors, fd, pci, result)
    except ProbeError as exc:
        result.update(status=exc.status, reason=exc.reason, detail=str(exc))
    except OSError as exc:
        result.update(status='UNKNOWN', reason='os-error', detail=str(exc))
    else:
        result.update(status='PASS', reason='verified')
    if cleanup_errors:
        result.update(cleanup_errors=cleanup_errors, outcome_before_cleanup=result['status'],
                      status='UNKNOWN', reason='cleanup-unknown')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('metadata', 'render'))
    parser.add_argument('--pci', required=True, choices=TARGETS)
    args = parser.parse_args()
    result = run_probe(args.mode, args.pci)
    print(json.dumps(result, sort_keys=True))
    return EXIT_CODES[result['status']]


if __name__ == '__main__':
    raise SystemExit(main())
