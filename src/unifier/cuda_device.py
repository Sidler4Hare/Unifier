"""Resolve one full NVIDIA GPU UUID to a headless CUDA attachment."""

from dataclasses import dataclass
import os
from pathlib import Path
import re
import stat

from unifier.errors import UnifierError


CUDA_DIRECTORY = '/run/unifier-cuda'
CUDA_LIBRARY = CUDA_DIRECTORY + '/libcuda.so.1'
CUDA_LABEL = 'io.unifier.cuda'
GPU_UUID = re.compile(r'GPU-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}')
PCI_BDF = re.compile(r'[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]')
DRIVER_VERSION = re.compile(r'[0-9]+(?:\.[0-9]+){1,2}')
GPU_ROOT = Path('/proc/driver/nvidia/gpus')
DRIVER_VERSION_FILE = Path('/sys/module/nvidia/version')
LIBRARY_ROOTS = (
    Path('/usr/lib/x86_64-linux-gnu'),
    Path('/usr/lib64'),
    Path('/usr/lib'),
)


@dataclass(frozen=True)
class CudaDevice:
    uuid: str
    pci: str
    minor: int
    library: str

    @property
    def nodes(self):
        return (f'/dev/nvidia{self.minor}', '/dev/nvidiactl', '/dev/nvidia-uvm')

    def identity(self):
        return {'uuid': self.uuid, 'pci': self.pci, 'minor': self.minor, 'library': self.library}


def _trusted_library(version):
    for root in LIBRARY_ROOTS:
        path = root / f'libcuda.so.{version}'
        try:
            metadata = path.stat()
        except FileNotFoundError:
            continue
        if (path.resolve(strict=True) == path and stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == 0 and not metadata.st_mode & 0o022
                and all((info := directory.stat()).st_uid == 0 and not info.st_mode & 0o022
                        for directory in path.parents)):
            return str(path)
    raise UnifierError('CUDA requires the root-owned libcuda matching the loaded NVIDIA driver')


def _verify_nodes(cuda):
    for path, expected in zip(cuda.nodes, ((195, cuda.minor), (195, 255), None)):
        node = os.lstat(path)
        if not stat.S_ISCHR(node.st_mode) or node.st_uid != 0:
            raise UnifierError(f'CUDA requires a root-owned character device at {path}')
        actual = (os.major(node.st_rdev), os.minor(node.st_rdev))
        if (expected is not None and actual != expected
                or expected is None and (actual[0] == 195 or actual[1] != 0)):
            raise UnifierError(f'CUDA device identity differs at {path}')
        if not os.access(path, os.R_OK | os.W_OK):
            raise UnifierError(f'CUDA requires read/write access to {path}')


def resolve_cuda_device(uuid):
    if not isinstance(uuid, str) or not GPU_UUID.fullmatch(uuid):
        raise UnifierError('CUDA requires a full NVIDIA GPU UUID such as GPU-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx')
    try:
        matches = []
        for gpu in GPU_ROOT.iterdir():
            if not PCI_BDF.fullmatch(gpu.name):
                continue
            fields = dict(line.split(':', 1) for line in (gpu / 'information').read_text().splitlines()
                          if ':' in line)
            if fields.get('GPU UUID', '').strip() == uuid:
                minor = fields.get('Device Minor', '').strip()
                if not re.fullmatch(r'[0-9]{1,3}', minor) or int(minor) >= 255:
                    raise UnifierError('CUDA GPU has an invalid device minor')
                matches.append((gpu.name, int(minor)))
        if len(matches) != 1:
            raise UnifierError(f'CUDA UUID {uuid} did not resolve to exactly one NVIDIA PCI device')
        version = DRIVER_VERSION_FILE.read_text().strip()
        if not DRIVER_VERSION.fullmatch(version):
            raise UnifierError('cannot determine the loaded NVIDIA driver version')
        cuda = CudaDevice(uuid, *matches[0], _trusted_library(version))
        _verify_nodes(cuda)
    except OSError as exc:
        raise UnifierError(f'CUDA attachment is unavailable: {exc}') from exc
    return cuda


def require_cuda_identity(identity):
    if not isinstance(identity, dict):
        raise UnifierError('CUDA attachment identity is invalid')
    cuda = resolve_cuda_device(identity.get('uuid'))
    if cuda.identity() != identity:
        raise UnifierError('CUDA attachment changed since creation; check the GPU and driver version')
    return cuda
