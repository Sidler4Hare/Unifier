"""A single explicitly selected V4L2 capture device for a desktop guest."""

from dataclasses import dataclass
import hashlib
import os
import posixpath
import re
import stat
import subprocess


CAMERA_PATH = re.compile(r"/dev/video[0-9]+")


@dataclass(frozen=True)
class CameraDevice:
    path: str
    selection: str
    device: int
    inode: int
    rdev: int


def _stat_device(path: str):
    if not isinstance(path, str) or not CAMERA_PATH.fullmatch(path):
        raise ValueError("expected one exact /dev/videoN capture device")
    info = os.lstat(path)
    if not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 81:
        raise ValueError(f"{path} is not a Video4Linux character device")
    return info.st_dev, info.st_ino, info.st_rdev


def resolve_camera(path: str) -> CameraDevice:
    identity = _stat_device(path)
    if not os.access(path, os.R_OK | os.W_OK, effective_ids=True, follow_symlinks=False):
        raise ValueError(f"host read/write access is required for {path}")
    try:
        result = subprocess.run(
            ["udevadm", "info", "--query=property", f"--name={path}"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=4, check=True,
        )
    except (subprocess.SubprocessError, OSError) as exc:
        raise ValueError(f"cannot verify capture capabilities for {path} through udev") from exc
    properties = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if (properties.get("DEVNAME") != path or properties.get("SUBSYSTEM") != "video4linux"
            or not properties.get("DEVPATH", "").startswith("/devices/")
            or "capture" not in properties.get("ID_V4L_CAPABILITIES", "").split(":")):
        raise ValueError(f"{path} is not a verified V4L2 capture node")
    if _stat_device(path) != identity:
        raise ValueError(f"camera {path} changed while reading device properties")
    values = [properties.get(key, "") for key in ("DEVPATH", "ID_PATH", "ID_SERIAL")]
    selection = hashlib.sha256("\0".join(values).encode()).hexdigest()
    return CameraDevice(path, selection, *identity)


def _overlaps_camera(target):
    target = posixpath.normpath("/" + target.lstrip("/"))
    return target in {"/", "/dev", "/dev/v4l"} or target.startswith("/dev/v4l/") or (
        CAMERA_PATH.fullmatch(target) is not None)


def require_no_native_camera(info):
    if any(_overlaps_camera(target) for target in info.device_destinations):
        raise ValueError("container already has a native camera device")


def validate_saved_camera_mounts(mounts, path):
    matches = [(src, dst, writable, kind)
               for src, dst, writable, kind in mounts if _overlaps_camera(dst)]
    if matches and matches != [(path, path, True, "bind")]:
        raise ValueError("container already has a conflicting camera mount")


def append_camera_mount(spec, camera: CameraDevice) -> None:
    for mount in spec["mounts"]:
        target = mount["destination"]
        if target == "/dev" and mount.get("type") == "tmpfs" and mount.get("source") == "tmpfs":
            continue
        if _overlaps_camera(target):
            raise ValueError(f"OCI camera mount conflicts with {target}")
    if any(_overlaps_camera(entry["path"]) for entry in spec["linux"].get("devices", ())):
        raise ValueError("OCI container already has a native camera device")
    if _stat_device(camera.path) != (camera.device, camera.inode, camera.rdev):
        raise ValueError(f"camera {camera.path} changed before container start")
    spec["mounts"].append({
        "destination": camera.path,
        "type": "bind",
        "source": camera.path,
        "options": ["bind", "private", "nosuid", "noexec", "rw"],
    })
