"""Refresh a stopped container's managed Wayland bind at OCI creation."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import sys
from tempfile import TemporaryDirectory


WAYLAND_SOCKET = re.compile(r"wayland-[0-9]+\Z")


@dataclass(frozen=True)
class WaylandRefresh:
    old: Path
    source: Path
    device: int
    inode: int


def _socket(path: Path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    return info if stat.S_ISSOCK(info.st_mode) else None


def is_managed_wayland_bind(mounts, destination: Path) -> bool:
    """Podman inspect can report the previous OCI spec after a stop."""
    matches = [(Path(source), kind) for source, target, _, kind in mounts
               if target == str(destination)]
    if len(matches) != 1:
        return False
    source, kind = matches[0]
    return (kind == "bind" and source.parent == destination.parent
            and WAYLAND_SOCKET.fullmatch(source.name) is not None)


def plan_refresh(desktop, *, mounts, uid, environ=None, runtime_directory=None):
    """Return a refresh for a standard, individually mounted Wayland socket."""
    env = os.environ if environ is None else environ
    directory = Path(runtime_directory or f"/run/user/{uid}")
    desktop = desktop or {}
    saved = desktop.get("env") or {}
    name = saved.get("WAYLAND_DISPLAY", "")
    if (not WAYLAND_SOCKET.fullmatch(name) or saved.get("XDG_RUNTIME_DIR") != str(directory)):
        return None
    old = directory / name
    if not any(mount.get("host") == str(old) and mount.get("container") == str(old)
               for mount in desktop.get("mounts") or ()):
        return None
    if not is_managed_wayland_bind(mounts, old):
        return None

    current = env.get("WAYLAND_DISPLAY", "")
    old_info = _socket(old)
    if old_info is not None and old_info.st_uid == uid and current in ("", name):
        return None
    if (env.get("XDG_RUNTIME_DIR", str(directory)) != str(directory)
            or not WAYLAND_SOCKET.fullmatch(current)):
        raise ValueError(f"cannot refresh Wayland mount {old} without a current session socket")

    source = directory / current
    parent = directory.lstat()
    source_info = _socket(source)
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != uid or parent.st_mode & 0o077
            or source_info is None or source_info.st_uid != uid):
        raise ValueError(f"current Wayland socket {source} is unavailable or has unexpected ownership")
    return WaylandRefresh(old, source, source_info.st_dev, source_info.st_ino)


def start_with_refresh(start, container_id: str, refresh: WaylandRefresh) -> None:
    start_with_hook(start, container_id, script=Path(__file__), name="wayland-refresh",
                    arguments=[str(refresh.old), str(refresh.source),
                               str(refresh.device), str(refresh.inode)],
                    action="start with refreshed Wayland mount",
                    prefix="unifier-wayland-")


def start_with_hook(start, container_id, *, script, name, arguments, action, prefix):
    # A per-command module appends the hook to existing Podman configuration.
    # CONTAINERS_CONF_OVERRIDE can replace that list after modules are loaded.
    if os.environ.get("CONTAINERS_CONF_OVERRIDE"):
        raise ValueError(f"cannot {action} with CONTAINERS_CONF_OVERRIDE set")
    with TemporaryDirectory(prefix=prefix) as temporary:
        directory = Path(temporary).resolve()
        module = directory / "session.conf"
        module.write_text(f"[engine]\nhooks_dir = [{json.dumps(str(directory))}, {{append=true}}]\n")
        python = str(Path(sys.executable).resolve())
        (directory / f"99-unifier-{name}.json").write_text(json.dumps({
            "version": "1.0.0",
            "hook": {
                "path": python,
                "args": [python, "-I", str(script.resolve()), *arguments],
                "timeout": 5,
            },
            "when": {"always": True},
            "stages": ["precreate"],
        }))
        start(["podman", "--module", str(module), "start", container_id],
              timeout=40, action=action)


def _rewrite_mount(spec, refresh: WaylandRefresh) -> None:
    # Podman runs this inside its user namespace. Recheck the socket identity
    # validated by the caller, since session paths can change before OCI create.
    current = _socket(refresh.source)
    if current is None or (current.st_dev, current.st_ino) != (refresh.device, refresh.inode):
        raise ValueError(f"Wayland socket {refresh.source} changed before container start")
    old = str(refresh.old)
    matches = [mount for mount in spec["mounts"] if mount.get("destination") == old]
    if (len(matches) != 1 or matches[0].get("type") != "bind"
            or matches[0].get("source") != old):
        raise ValueError(f"expected a single Wayland bind mount for {old}")
    matches[0]["source"] = str(refresh.source)


if __name__ == "__main__":
    try:
        old, source, device, inode = sys.argv[1:]
        config = json.load(sys.stdin)
        _rewrite_mount(config, WaylandRefresh(Path(old), Path(source), int(device), int(inode)))
        json.dump(config, sys.stdout)
    except (OSError, KeyError, TypeError, ValueError) as exc:
        sys.exit(str(exc))
