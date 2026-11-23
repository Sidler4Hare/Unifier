"""Read the host's Portal preferences and check its running frontend."""

from configparser import ConfigParser, Error as ConfigError
import os
from pathlib import Path
import re
import stat
import subprocess

from unifier.desktop import discover_session
from unifier.errors import UnifierError


DESKTOP_PORTAL = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
BACKEND_INTERFACES = ("FileChooser", "ScreenCast")
PUBLIC_INTERFACES = (*BACKEND_INTERFACES, "Camera")
BACKEND_PREFIX = "org.freedesktop.impl.portal."
TOKEN = re.compile(r"[A-Za-z0-9_.-]+")
OWNER = re.compile(r's "(:[A-Za-z0-9_.-]+)"')


def _absolute_paths(value):
    return [Path(part) for part in value.split(":") if part.startswith("/")]


def _absolute_path(value, default):
    path = Path(value or "")
    return path if path.is_absolute() else default


def _key_file(path):
    # The user can override these files. Refuse special files and bound reads.
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC),
                   encoding="utf-8") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Portal configuration must be a regular file")
        data = stream.read(65537)
        if len(data) > 65536:
            raise ValueError("Portal configuration exceeds size limit")
    config = ConfigParser(interpolation=None, delimiters=("=",), strict=False)
    config.optionxform = str
    config.read_string(data)
    return config


def _configuration(environ, desktops, sysconfdir, datadir):
    home = Path(environ.get("HOME") or Path.home())
    config_home = _absolute_path(environ.get("XDG_CONFIG_HOME"), home / ".config")
    data_home = _absolute_path(environ.get("XDG_DATA_HOME"), home / ".local/share")
    roots = (
        config_home,
        *_absolute_paths(environ.get("XDG_CONFIG_DIRS") or "/etc/xdg"),
        sysconfdir,
        data_home,
        *_absolute_paths(environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"),
        datadir,
    )
    names = (*[f"{desktop.lower()}-portals.conf" for desktop in desktops], "portals.conf")
    for directory in dict.fromkeys(roots):
        for name in names:
            path = directory / "xdg-desktop-portal" / name
            try:
                return str(path), _key_file(path), "configured"
            except FileNotFoundError:
                continue
            except (ConfigError, OSError, ValueError):
                return str(path), None, "unreadable"
    return None, None, "unknown"


def _backend_candidate(directory, interface, preferred):
    """Report a configuration candidate, leaving actual selection to the host."""
    if "none" in preferred:
        return None
    for name in preferred:
        if name == "*":
            try:
                paths = sorted(directory.glob("*.portal"))
            except OSError:
                continue
        elif TOKEN.fullmatch(name):
            paths = (directory / f"{name}.portal",)
        else:
            continue
        for path in paths:
            try:
                section = _key_file(path)["portal"]
                interfaces = section.get("Interfaces", "").split(";")
                service = section.get("DBusName", "")
            except (ConfigError, KeyError, OSError, ValueError):
                continue
            if interface in interfaces and TOKEN.fullmatch(service):
                return {"name": path.stem, "service": service}
    return None


def _bus_query(address, *command):
    try:
        result = subprocess.run(
            ["busctl", f"--address={address}", "--auto-start=no", "--no-pager",
             "--timeout=1s", *command],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=2, check=False,
        )
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _frontend_versions(address):
    bus = "org.freedesktop.DBus"
    owner = _bus_query(address, "call", bus, "/org/freedesktop/DBus", bus,
                       "GetNameOwner", "s", DESKTOP_PORTAL)
    match = OWNER.fullmatch(owner)
    if not match:
        return None
    versions = {}
    for interface in PUBLIC_INTERFACES:
        output = _bus_query(address, "get-property", match[1], PORTAL_PATH,
                            f"org.freedesktop.portal.{interface}", "version")
        value = re.fullmatch(r"u ([0-9]{1,10})", output)
        versions[interface] = int(value[1]) if value else None
    return versions


def describe_host_portal(environ=None, *, sysconfdir=Path("/etc"), datadir=Path("/usr/share")):
    env = os.environ if environ is None else environ
    desktops = [name for name in env.get("XDG_CURRENT_DESKTOP", "").split(":")
                if TOKEN.fullmatch(name)]
    config_path, config, config_state = _configuration(env, desktops, sysconfdir, datadir)
    directory = _absolute_path(env.get("XDG_DESKTOP_PORTAL_DIR"),
                               datadir / "xdg-desktop-portal/portals")

    try:
        discover_session(env)
        graphical = True
    except UnifierError:
        graphical = False
    address = env.get("DBUS_SESSION_BUS_ADDRESS") if graphical else None
    versions = _frontend_versions(address) if address else None
    interfaces = {}
    for name in PUBLIC_INTERFACES:
        detail = {"frontend_version": (versions or {}).get(name)}
        if name in BACKEND_INTERFACES:
            backend_interface = BACKEND_PREFIX + name
            choices = config.get("preferred", backend_interface, fallback=None) if config else None
            if choices is None and config:
                choices = config.get("preferred", "default", fallback="")
            preferred = [item.strip() for item in (choices or "").split(";") if item.strip()]
            detail.update(preferred=preferred,
                          configured_candidate=_backend_candidate(directory, backend_interface, preferred))
        interfaces[name] = detail
    return {
        "desktops": desktops, "graphical_session": graphical,
        "configuration": config_path, "configuration_status": config_state,
        "frontend": "active" if versions is not None else "unverified" if address else "no-session",
        "interfaces": interfaces,
    }
