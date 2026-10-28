"""Host session sockets, desktop entries and file associations.

The host keeps its own compositor, audio server and SELinux policy. A desktop
environment bind-mounts the current session sockets and does not relabel them.
container_t cannot connect to those sockets, so the desktop environment is the
one that runs with label=disable. Package environments stay on the default
container label.

This module does not import the environment service.
"""

import os
from pathlib import Path
import re
import stat
import subprocess

from multilinux.errors import MultilinuxError
from multilinux.runtime import project_token


APP_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
ENV_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
MIME_TYPE = re.compile(r"application/x-multilinux-[a-z0-9][a-z0-9-]{0,40}\Z")
EXTENSION = re.compile(r"[a-z0-9]{1,16}\Z")
XAUTHORITY_INSIDE = "/tmp/ml-xauthority"

SELINUX_DESKTOP_REASON = (
    "container_t cannot connect to the host session sockets. Relabeling those "
    "sockets would change the compositor's labels, so a desktop environment "
    "keeps the host enforcing and runs with label=disable."
)


def validate_app_id(app_id: str) -> str:
    if not isinstance(app_id, str) or not APP_ID.fullmatch(app_id) or app_id.endswith("-"):
        raise MultilinuxError(
            "application id must be 1-32 characters, start with a lowercase letter, "
            "and contain only lowercase letters, digits and hyphens")
    return app_id


def discover_session(environ=None, *, is_socket=None, is_file=None) -> dict:
    """Describe the current host session as container mounts and env values."""
    env = os.environ if environ is None else environ
    socket_ok = _is_socket if is_socket is None else is_socket
    file_ok = _is_file if is_file is None else is_file
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    mounts = []
    values = {}

    wayland = env.get("WAYLAND_DISPLAY", "")
    if wayland and "/" not in wayland and ".." not in wayland:
        sock = f"{runtime}/{wayland}"
        if socket_ok(sock):
            mounts.append(_mount(sock, sock))
            values["WAYLAND_DISPLAY"] = wayland
            values["XDG_RUNTIME_DIR"] = runtime

    display = env.get("DISPLAY", "")
    screen = _display_number(display)
    if screen is not None:
        sock = f"/tmp/.X11-unix/X{screen}"
        if socket_ok(sock):
            mounts.append(_mount(sock, sock))
            values["DISPLAY"] = display

    xauth = env.get("XAUTHORITY", "")
    if xauth and file_ok(xauth):
        mounts.append(_mount(xauth, XAUTHORITY_INSIDE))
        values["XAUTHORITY"] = XAUTHORITY_INSIDE

    pulse = f"{runtime}/pulse/native"
    if socket_ok(pulse):
        mounts.append(_mount(pulse, pulse))
        values["PULSE_SERVER"] = f"unix:{pulse}"

    pipewire = f"{runtime}/pipewire-0"
    if socket_ok(pipewire):
        mounts.append(_mount(pipewire, pipewire))
        values["PIPEWIRE_REMOTE"] = "pipewire-0"

    if "WAYLAND_DISPLAY" in values:
        values["XDG_SESSION_TYPE"] = "wayland"
    elif "DISPLAY" in values:
        values["XDG_SESSION_TYPE"] = "x11"
    if "WAYLAND_DISPLAY" not in values and "DISPLAY" not in values:
        raise MultilinuxError("desktop session has no Wayland or X11 socket")
    return validate_desktop_session({
        "selinux_label": "disable",
        "reason": SELINUX_DESKTOP_REASON,
        "env": values,
        "mounts": mounts,
    })


def validate_desktop_session(session: dict) -> dict:
    if session.get("selinux_label") != "disable":
        raise MultilinuxError("a desktop environment requires selinux label=disable")
    env = session.get("env") or {}
    if "WAYLAND_DISPLAY" not in env and "DISPLAY" not in env:
        raise MultilinuxError("desktop session has no Wayland or X11 socket")
    mounts = []
    seen = set()
    for item in session.get("mounts") or []:
        host = item["host"]
        container = item["container"]
        options = item.get("options", "")
        if options not in {"", None}:
            raise MultilinuxError("session sockets are mounted without a relabel")
        for path in (host, container):
            if not path.startswith("/") or ":" in path or "\n" in path or "\x00" in path:
                raise MultilinuxError(f"invalid session mount path: {path}")
        if host in seen:
            raise MultilinuxError(f"duplicate session mount: {host}")
        seen.add(host)
        mounts.append({"host": host, "container": container, "options": ""})
    if not mounts:
        raise MultilinuxError("desktop session has no sockets to mount")
    for key, value in env.items():
        if not ENV_KEY.fullmatch(key) or not isinstance(value, str) or "\n" in value or value == "":
            raise MultilinuxError(f"invalid desktop environment value: {key}")
    return {
        "selinux_label": "disable",
        "reason": session.get("reason") or SELINUX_DESKTOP_REASON,
        "env": dict(env),
        "mounts": mounts,
    }


def desktop_id(project_root: Path, app_id: str) -> str:
    return f"multilinux-{project_token(project_root)}-{app_id}"


def render_desktop_entry(*, launcher: list[str], app_id: str, environment_id: str,
                         project: str, mime_type: str | None, name: str,
                         icon: str | None = None, wm_class: str | None = None) -> str:
    for value in (name, icon, wm_class):
        if value is not None and any(ch in value for ch in ('\n', '\r', '\x00')):
            raise MultilinuxError('desktop fields must be a single line')
    exec_line = " ".join(_quote_desktop(part) for part in launcher) + (" %f" if mime_type else "")
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={name}",
        f"Comment=Run application in environment {environment_id}",
        f"Exec={exec_line}",
        "Terminal=false",
        "Categories=Utility;",
        f"X-Multilinux-App={app_id}",
        f"X-Multilinux-Environment={environment_id}",
        f"X-Multilinux-Project={project}",
        "",
    ]
    if mime_type:
        lines.insert(6, f"MimeType={mime_type};")
    if icon:
        lines.insert(6, f"Icon={icon}")
    if wm_class:
        lines.insert(6, f"StartupWMClass={wm_class}")
    return "\n".join(lines)


def render_mime_xml(mime_type: str, extension: str) -> str:
    if not MIME_TYPE.fullmatch(mime_type):
        raise MultilinuxError(
            "file association type must look like application/x-multilinux-note")
    if not EXTENSION.fullmatch(extension):
        raise MultilinuxError("file association extension must be 1-16 lowercase letters or digits")
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<mime-info xmlns=\"http://www.freedesktop.org/standards/shared-mime-info\">\n"
        f"  <mime-type type=\"{mime_type}\">\n"
        "    <comment>Multilinux note</comment>\n"
        f"    <glob pattern=\"*.{extension}\"/>\n"
        "  </mime-type>\n"
        "</mime-info>\n"
    )


def add_mime_association(text: str | None, mime_type: str, desktop_name: str) -> str:
    """Add one default application line and leave every other line in place."""
    association = f"{mime_type}={desktop_name}"
    if text is None:
        return "[Default Applications]\n" + association + "\n"
    lines = text.splitlines()
    if association in (line.strip() for line in lines):
        return text if text.endswith("\n") else text + "\n"
    section = None
    insert_at = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if section == "Default Applications" and insert_at is None:
                insert_at = index
            section = stripped[1:-1].strip()
            if section == "Default Applications":
                insert_at = None
    if section == "Default Applications":
        lines.append(association)
    elif insert_at is not None:
        lines.insert(insert_at, association)
    else:
        if lines and lines[-1] != "":
            lines.append("")
        lines.extend(["[Default Applications]", association])
    body = "\n".join(lines)
    return body if body.endswith("\n") else body + "\n"


def remove_mime_association(text: str, mime_type: str, desktop_name: str) -> str:
    association = f"{mime_type}={desktop_name}"
    kept = [line for line in text.splitlines() if line.strip() != association]
    meaningful = [line.strip() for line in kept if line.strip()]
    if meaningful in ([], ["[Default Applications]"]):
        return ""
    return "\n".join(kept).rstrip("\n") + "\n"


class HostDesktop:
    def __init__(self, applications=None, mime_root=None):
        home = Path.home()
        self.applications = Path(applications or (home / ".local/share/applications"))
        self.mime_root = Path(mime_root or (home / ".local/share/mime"))

    @property
    def mimeapps(self) -> Path:
        return self.applications / "mimeapps.list"

    @property
    def mime_packages(self) -> Path:
        return self.mime_root / "packages"

    def refresh(self, runner=None) -> list[list[str]]:
        run = runner or _run_cache_tool
        commands = [
            ["update-mime-database", str(self.mime_root)],
            ["update-desktop-database", str(self.applications)],
        ]
        for argv in commands:
            result = run(argv)
            if result.returncode != 0:
                detail = ((result.stderr or "") + (result.stdout or ""))[-1500:]
                raise MultilinuxError(f"{argv[0]} failed: {detail}")
        return commands

    def refresh_applications(self, runner=None):
        run = runner or _run_cache_tool
        result = run(['update-desktop-database', str(self.applications)])
        if result.returncode:
            raise MultilinuxError('update-desktop-database failed')


def _mount(host: str, container: str) -> dict:
    return {"host": host, "container": container, "options": ""}


def _display_number(display: str):
    if not display.startswith(":") or len(display) < 2:
        return None
    number = display[1:].split(".", 1)[0]
    if not number.isdigit():
        return None
    return number


def _is_socket(path: str) -> bool:
    try:
        mode = os.stat(path).st_mode
    except OSError:
        return False
    return stat.S_ISSOCK(mode)


def _is_file(path: str) -> bool:
    return os.path.isfile(path)


def _quote_desktop(part: str) -> str:
    if any(character in part for character in " \t\n\"'\\$`"):
        escaped = part.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return part


def _run_cache_tool(argv: list[str]):
    return subprocess.run(argv, capture_output=True, text=True, check=False)
