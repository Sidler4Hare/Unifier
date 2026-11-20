"""Mount one opted-in DRM render node when Podman creates the OCI process."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import sys

if __name__ == "__main__":
    # The isolated hook interpreter only loads our installed package.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unifier.wayland import WaylandRefresh, _rewrite_mount, start_with_hook


RENDER_NODE = re.compile(r"/dev/dri/renderD([1-9][0-9]{2,8})")
FULL_CID = re.compile(r"[a-f0-9]{64}")
STOPPED = {"configured", "created", "exited", "stopped"}
MOUNT_OPTIONS = ("slave", "nosuid", "noexec", "rw", "rbind")


class RenderNodeAccessError(ValueError):
    """The render node exists but the host user lacks read/write access."""


@dataclass(frozen=True)
class RenderNode:
    path: str
    device: int
    inode: int
    rdev: int
    dev_directory: tuple[int, int]
    dri_directory: tuple[int, int]


def _directory_identity(path):
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"{path} must be a real directory")
    return info.st_dev, info.st_ino


def _identity(path: str) -> RenderNode:
    match = RENDER_NODE.fullmatch(path) if isinstance(path, str) else None
    if match is None or int(match[1]) < 128:
        raise ValueError("expected an exact /dev/dri/renderDN path with N >= 128")
    dev_directory = _directory_identity("/dev")
    dri_directory = _directory_identity("/dev/dri")
    info = os.lstat(path)
    if (not stat.S_ISCHR(info.st_mode) or os.major(info.st_rdev) != 226
            or os.minor(info.st_rdev) != int(match[1])):
        raise ValueError(f"{path} must be a DRM render character device matching its minor")
    return RenderNode(path, info.st_dev, info.st_ino, info.st_rdev,
                      dev_directory, dri_directory)


def resolve_render_node(path: str) -> RenderNode:
    node = _identity(path)
    if not os.access(path, os.R_OK | os.W_OK, effective_ids=True, follow_symlinks=False):
        raise RenderNodeAccessError(f"host read/write access is required for {path}")
    return node


def render_node_identity(node: RenderNode, *, sysfs_root: Path = Path("/sys")) -> dict:
    """Read the physical sysfs binding without opening the DRM device."""
    root = sysfs_root.resolve(strict=True)
    major, minor = os.major(node.rdev), os.minor(node.rdev)
    link = root / "dev/char" / f"{major}:{minor}" / "device"
    physical = link.resolve(strict=True)
    if not physical.is_relative_to(root / "devices"):
        raise ValueError(f"cannot verify the physical DRM identity of {node.path}")
    identity = {"sysfs_device": "/" + physical.relative_to(root).as_posix(),
                "major": major, "minor": minor}
    for key in ("vendor", "device", "subsystem_vendor", "subsystem_device"):
        try:
            with (physical / key).open(encoding="ascii") as stream:
                value = stream.read(32).strip()
        except FileNotFoundError:
            continue
        if not re.fullmatch(r"0x[0-9a-fA-F]{4,8}", value):
            raise ValueError(f"invalid DRM identity field {key} for {node.path}")
        identity[key] = value.lower()
    if link.resolve(strict=True) != physical or _identity(node.path) != node:
        raise ValueError(f"DRM render node {node.path} changed while selecting it")
    return identity


def _overlaps_dri(target):
    target = posixpath.normpath("/" + target.lstrip("/"))
    return target in {"/", "/dev", "/dev/dri"} or target.startswith("/dev/dri/")


def validate_saved_mounts(mounts, managed_path):
    # Podman can expose the previous OCI view while stopped. Allow one
    # effective mount left by this setting. Native devices are checked separately.
    matches = [(source, target, writable, kind)
               for source, target, writable, kind in mounts if _overlaps_dri(target)]
    if matches and matches != [(managed_path, managed_path, True, "bind")]:
        raise ValueError("container already has a conflicting DRM mount")


def require_stopped(info):
    if (not FULL_CID.fullmatch(info.container_id) or info.running
            or info.status not in STOPPED):
        raise ValueError("render-node configuration requires a stopped container with a full ID")


def require_no_native_drm(info):
    if any(_overlaps_dri(target) for target in info.device_destinations):
        raise ValueError("container already has a native DRM device")


def _require_oci_target(spec, cid):
    linux = spec["linux"]
    cgroup = linux.get("cgroupsPath", "")
    systemd = re.fullmatch(r"[^/:]+:libpod:" + cid, cgroup)
    cgroupfs = cgroup.startswith("/") and cgroup.rsplit("/", 1)[-1] == f"libpod-{cid}"
    if (spec.get("annotations", {}).get("io.container.manager") != "libpod"
            or not (systemd or cgroupfs)):
        raise ValueError("OCI target does not match the expected Podman container")


def rewrite_spec(spec, cid: str, node: RenderNode, wayland: WaylandRefresh | None):
    if not FULL_CID.fullmatch(cid):
        raise ValueError("OCI render-node hook requires a full container ID")
    _require_oci_target(spec, cid)
    for mount in spec["mounts"]:
        target = mount["destination"]
        if target == "/dev" and mount.get("type") == "tmpfs" and mount.get("source") == "tmpfs":
            continue  # The OCI runtime's standard private device tree.
        if _overlaps_dri(target):
            raise ValueError(f"OCI DRM mount conflicts with {target}")
    if any(_overlaps_dri(device["path"]) for device in spec["linux"].get("devices", ())):
        raise ValueError("OCI container already has a native DRM device")
    if wayland is not None:
        _rewrite_mount(spec, wayland)
    if _identity(node.path) != node:
        raise ValueError(f"DRM render node {node.path} changed before container start")
    spec["mounts"].append({
        "destination": node.path,
        "type": "bind",
        "source": node.path,
        "options": list(MOUNT_OPTIONS),
    })


def start_with_render_node(start, cid: str, node: RenderNode, wayland: WaylandRefresh | None):
    refresh = None if wayland is None else {
        **asdict(wayland), "old": str(wayland.old), "source": str(wayland.source),
    }
    start_with_hook(start, cid, script=Path(__file__), name="render-node",
                    arguments=[cid, json.dumps(asdict(node)), json.dumps(refresh)],
                    action="start with configured render node",
                    prefix="unifier-render-node-")


if __name__ == "__main__":
    try:
        cid, identity, refresh = sys.argv[1:]
        identity = json.loads(identity)
        for field in ("dev_directory", "dri_directory"):
            identity[field] = tuple(identity[field])
        refresh = json.loads(refresh)
        if refresh is not None:
            refresh = WaylandRefresh(Path(refresh["old"]), Path(refresh["source"]),
                                     refresh["device"], refresh["inode"])
        spec = json.load(sys.stdin)
        rewrite_spec(spec, cid, RenderNode(**identity), refresh)
        json.dump(spec, sys.stdout)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.exit(str(exc))
