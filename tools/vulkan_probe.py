"""Host-side reference for the fixed Intel Vulkan probe."""

import hashlib
from pathlib import PurePosixPath
import re
import struct


ICD = '/usr/share/vulkan/icd.d/intel_icd.json'
PAYLOAD_BYTES = 16 * 1024 * 1024


def check_readback(output):
    if len(output) > 16 * 1024:
        raise ValueError('Vulkan probe output exceeds its expected size')
    data = {}
    for line in output.splitlines():
        name, separator, value = line.partition('=')
        if not separator or not value or name in data:
            raise ValueError('malformed Vulkan result')
        data[name] = value
    identity = {'vendor': '8086', 'device': '9a49', 'device_type': 'integrated',
                'pci': '0000:00:02.0', 'render': '226:128', 'driver': 'ANV',
                'device_destroyed': 'true'}
    if any(data.get(key) != value for key, value in identity.items()):
        raise ValueError('Vulkan physical device or cleanup differs from the Intel fixture')
    api = data.get('api', '')
    if (PurePosixPath(data.get('icd', '')).name != 'libvulkan_intel.so'
            or not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', api)
            or tuple(map(int, api.split('.'))) < (1, 2, 0)
            or not data.get('driver_info') or not data.get('device_name')):
        raise ValueError('Vulkan ANV driver evidence is incomplete')
    allocated = int(data['allocated_bytes'])
    if not 0 < allocated <= PAYLOAD_BYTES:
        raise ValueError('Vulkan probe reported an invalid device allocation')

    rgba = bytes.fromhex(data['rgba'])
    compute = bytes.fromhex(data['compute'])
    red, green = bytes((255, 0, 0, 255)), bytes((0, 255, 0, 255))
    reference_rgba = b''.join(red if 8 <= x < 24 and 4 <= y < 12 else green
                              for y in range(16) for x in range(32))
    integers = [((0xfffffff0 + i) * 13 + 29) & 0xffffffff for i in range(16)]
    floats = [(i - 3) * 0.375 - 2 for i in range(16)]
    reference_compute = struct.pack('<16I16f', *integers, *floats)
    if rgba != reference_rgba:
        raise ValueError('Vulkan graphics readback differs from the CPU reference')
    if compute != reference_compute:
        raise ValueError('Vulkan integer or float compute output differs from the CPU reference')
    return {
        **identity, 'icd': data['icd'], 'device_name': data['device_name'],
        'driver_info': data['driver_info'], 'api': data['api'], 'allocated_bytes': allocated,
        'image': {'width': 32, 'height': 16, 'rgba8_pixels': 512, 'red': 128, 'green': 384,
                  'sha256': hashlib.sha256(rgba).hexdigest()},
        'compute': {'uint32': integers, 'fp32': floats, 'sha256': hashlib.sha256(compute).hexdigest()},
    }
