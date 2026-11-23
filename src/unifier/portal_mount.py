"""Apply desktop mounts in a single OCI hook and verify the Portal directory."""

from dataclasses import asdict, dataclass
import json
import os
from pathlib import Path
import posixpath
import re
import stat
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from unifier.camera_device import CameraDevice, append_camera_mount
from unifier.render_node import FULL_CID, RenderNode, _require_oci_target, rewrite_spec as rewrite_render_spec
from unifier.wayland import WaylandRefresh, _rewrite_mount, start_with_hook


PORTAL_GUEST = "/run/multilinux-portal"
_DIRECTORY_FLAGS = os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_MOUNTINFO_LIMIT = 1024 * 1024
_MOUNT_ESCAPE = re.compile(rb"\\(040|011|012|134)")


@dataclass(frozen=True)
class PortalMount:
    path: str
    device: int
    inode: int
    uid: int


def _source_info(directory):
    path = Path(directory)
    try:
        resolved = path.resolve(strict=True) if path.is_absolute() else None
    except RuntimeError as exc:
        raise ValueError("Portal source path cannot be resolved") from exc
    if resolved is None or str(path) != os.fspath(directory) or str(resolved) != str(path):
        raise ValueError("Portal source must be an absolute canonical directory")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o700:
        raise ValueError("Portal source must be a real directory with mode 0700")
    return info


def resolve_portal_mount(directory: str | Path, uid: int) -> PortalMount:
    info = _source_info(directory)
    if info.st_uid != uid:
        raise ValueError("Portal source must be owned by the host user")
    return PortalMount(os.fspath(directory), info.st_dev, info.st_ino, uid)


def _require_source(mount: PortalMount, *, check_uid: bool = True):
    # Podman runs the OCI hook in its user namespace, which can map the UID.
    info = _source_info(mount.path)
    if ((info.st_dev, info.st_ino) != (mount.device, mount.inode)
            or (check_uid and info.st_uid != mount.uid)):
        raise ValueError("Portal source identity changed")


def _overlaps_guest(target: str) -> bool:
    target = posixpath.normpath("/" + target.lstrip("/"))
    return (target in {"/", "/run", PORTAL_GUEST}
            or target.startswith(PORTAL_GUEST + "/"))


def validate_saved_portal_mounts(mounts, allowed_source: str | Path | None = None) -> None:
    matches = [item for item in mounts if _overlaps_guest(item[1])]
    if matches and (allowed_source is None or len(matches) != 1
                    or matches[0] != (os.fspath(allowed_source), PORTAL_GUEST, True, "bind")
                    or matches[0][2] is not True):
        raise ValueError("container already has a conflicting Portal mount")


def rewrite_spec(spec, cid: str, mount: PortalMount | None, wayland: WaylandRefresh | None,
                 render_node: RenderNode | None, *, camera: CameraDevice | None = None) -> None:
    if not FULL_CID.fullmatch(cid):
        raise ValueError("OCI Portal hook requires a full container ID")
    _require_oci_target(spec, cid)
    if mount is not None:
        for entry in spec["mounts"]:
            if _overlaps_guest(entry["destination"]):
                raise ValueError(f"OCI Portal mount conflicts with {entry['destination']}")
        _require_source(mount, check_uid=False)
    if render_node is not None:
        rewrite_render_spec(spec, cid, render_node, wayland)
    elif wayland is not None:
        _rewrite_mount(spec, wayland)
    if camera is not None:
        append_camera_mount(spec, camera)
    if mount is not None:
        spec["mounts"].append({
            "destination": PORTAL_GUEST,
            "type": "bind",
            "source": mount.path,
            "options": ["bind", "private", "rw", "nosuid", "nodev"],
        })


def start_with_portal_mount(start, cid: str, mount: PortalMount | None,
                            wayland: WaylandRefresh | None,
                            render_node: RenderNode | None, *, camera: CameraDevice | None = None) -> None:
    if mount is not None:
        _require_source(mount)
    refresh = None if wayland is None else {
        **asdict(wayland), "old": str(wayland.old), "source": str(wayland.source),
    }
    arguments = [cid, json.dumps(None if mount is None else asdict(mount)), json.dumps(refresh),
                 json.dumps(None if render_node is None else asdict(render_node))]
    if camera is not None:
        arguments.append(json.dumps(asdict(camera)))
    start_with_hook(start, cid, script=Path(__file__), name="portal-mount",
                    arguments=arguments, action="start with desktop mounts",
                    prefix="unifier-portal-mount-")


def _check_mountinfo(payload: bytes) -> None:
    if not payload.endswith(b"\n"):
        raise ValueError("container mount information is incomplete")
    exact = []
    for line in payload.split(b"\n")[:-1]:
        before, sep, after = line.partition(b" - ")
        entries, filesystem = before.split(), after.split()
        if not sep or len(entries) < 6 or len(filesystem) != 3:
            raise ValueError("container mount information is invalid")
        target = os.fsdecode(_MOUNT_ESCAPE.sub(
            lambda match: bytes([int(match[1], 8)]), entries[4]))
        if target == PORTAL_GUEST:
            exact.append((entries[5].split(b","), filesystem[2].split(b",")))
        elif target.startswith(PORTAL_GUEST + "/"):
            raise ValueError("container Portal mount has a nested mount")
    if len(exact) != 1 or any(b"rw" not in opts or b"ro" in opts for opts in exact[0]):
        raise ValueError("expected one active writable Portal mount")


def verify_active_portal_mount(pid: int, mount: PortalMount) -> None:
    if type(pid) is not int or pid <= 0:
        raise ValueError("Portal mount verification requires a running container PID")
    _require_source(mount)
    proc_fd = os.open(f"/proc/{pid}", _DIRECTORY_FLAGS)
    try:
        fd = os.open("mountinfo", os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=proc_fd)
        with os.fdopen(fd, "rb") as stream:
            payload = stream.read(_MOUNTINFO_LIMIT + 1)
        if len(payload) > _MOUNTINFO_LIMIT:
            raise ValueError("container mount information is too large")
        _check_mountinfo(payload)
        root_fd = os.open("root", os.O_PATH | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=proc_fd)
        try:
            run_fd = os.open("run", _DIRECTORY_FLAGS, dir_fd=root_fd)
            try:
                guest_fd = os.open(PORTAL_GUEST.rsplit("/", 1)[1], _DIRECTORY_FLAGS, dir_fd=run_fd)
                try:
                    info = os.fstat(guest_fd)
                finally:
                    os.close(guest_fd)
            finally:
                os.close(run_fd)
        finally:
            os.close(root_fd)
        if (info.st_dev, info.st_ino) != (mount.device, mount.inode):
            raise ValueError("active Portal mount source identity differs")
        _require_source(mount)
    finally:
        os.close(proc_fd)


if __name__ == "__main__":
    try:
        cid, identity, refresh, render, *camera = sys.argv[1:]
        if len(camera) > 1:
            raise ValueError("expected at most one configured camera")
        refresh = json.loads(refresh)
        if refresh is not None:
            refresh = WaylandRefresh(Path(refresh["old"]), Path(refresh["source"]),
                                     refresh["device"], refresh["inode"])
        render = json.loads(render)
        if render is not None:
            for field in ("dev_directory", "dri_directory"):
                render[field] = tuple(render[field])
            render = RenderNode(**render)
        spec = json.load(sys.stdin)
        mount = json.loads(identity)
        rewrite_spec(spec, cid, PortalMount(**mount) if mount is not None else None, refresh, render,
                     camera=CameraDevice(**json.loads(camera[0])) if camera else None)
        json.dump(spec, sys.stdout)
    except (OSError, KeyError, TypeError, ValueError, RuntimeError) as exc:
        sys.exit(str(exc))
