"""Opt-in file chooser modes for a registered desktop application."""

import hashlib
import json
import os
from pathlib import Path
import uuid

from unifier.desktop import discover_session
from unifier.errors import UnifierError
from unifier.kdialog_portal import SHARES_ENV, absolute_path


MODE_KEY = "UNIFIER_FILE_CHOOSER"
PORTAL_MODES = ("gtk-portal", "gtk-native-portal", "qt-portal", "kdialog-portal", "zenity-portal")
MODES = ("native", *PORTAL_MODES, "headless")
DEFAULT_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
DISPLAY_KEYS = ("DISPLAY", "WAYLAND_DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS")


def configured_environment(record, environment, *, on_demand):
    mode = environment.get(MODE_KEY, "native")
    if mode not in MODES:
        raise UnifierError(f"unknown file chooser mode: {mode}")
    if mode == "headless":
        if record.get("desktop") or any(environment.get(key) for key in DISPLAY_KEYS):
            raise UnifierError("headless mode requires an environment and launch without desktop bindings")
    elif mode in PORTAL_MODES:
        desktop = record.get("desktop") or {}
        bridge = desktop.get("portal_proxy") or {}
        inside = bridge.get("container")
        if (not on_demand or not bridge.get("directory") or not inside
                or environment.get("DBUS_SESSION_BUS_ADDRESS") != f"unix:path={inside}/bus"):
            raise UnifierError("file chooser forwarding requires an on-demand application with the Unifier desktop portal bridge")
    return mode


def preflight_picker(record, launch, *, environ=None, is_socket=None):
    mode = configured_environment(record, launch["env"], on_demand=launch.get("on_demand"))
    if mode not in PORTAL_MODES:
        return
    host = os.environ if environ is None else environ
    message = "file chooser forwarding requires a graphical host session. On a headless host, pass the file path explicitly."
    if not host.get("DBUS_SESSION_BUS_ADDRESS"):
        raise UnifierError(message)
    try:
        discover_session(host, is_socket=is_socket)
    except UnifierError as exc:
        raise UnifierError(message) from exc


def verified_shares(record, mounts):
    """Intersect registered data shares with actual unshadowed runtime binds."""
    shares = []
    for item in record.get("shared_mounts") or []:
        try:
            host = absolute_path(item["host"])
            guest = absolute_path(item["container"])
            if host == Path("/") or guest == Path("/") or Path(host).resolve(strict=True) != host:
                continue
            if not Path(host).is_dir():
                continue
        except (KeyError, TypeError, ValueError, OSError, RuntimeError):
            continue
        actual = [(source, rw, kind) for source, destination, rw, kind in mounts
                  if destination == str(guest)]
        if len(actual) != 1 or actual[0][0] != str(host) or actual[0][2] != "bind":
            continue
        if any(destination.startswith(str(guest) + "/") for _, destination, _, _ in mounts):
            continue
        shares.append((str(host), str(guest), actual[0][1]))
    return list(dict.fromkeys(shares))


def publish_picker(directory, command):
    # The guest can read the bridge directory. Do not follow guest-writable
    # symlinks or execute any of its contents on the host.
    module = Path(__file__).parent
    parser = (module / "kdialog_args.py").read_bytes()
    zenity = (module / "zenity_args.py").read_bytes()
    adapter = (module / "kdialog_portal.py").read_bytes()
    if command not in ("kdialog", "zenity"):
        raise ValueError("unknown command-based file chooser")
    digest = hashlib.sha256(parser + b"\0" + zenity + b"\0" + adapter).hexdigest()[:16]
    name = f"chooser-{command}-{digest}"
    return _publish_payloads(directory, name, (
        (parser, "kdialog_args.py", 0o644), (zenity, "zenity_args.py", 0o644),
        (adapter, command, 0o755)))


def _native_launcher():
    payload = (Path(__file__).parent / "native_library_launcher.py").read_bytes()
    return "native-libraries-" + hashlib.sha256(payload).hexdigest()[:16], payload


def launch_argv(record, launch, argv):
    """Wrap only the opted-in app; argv is never interpreted by a shell."""
    if launch["env"].get(MODE_KEY) != "gtk-native-portal":
        return list(argv)
    name, _ = _native_launcher()
    inside = record["desktop"]["portal_proxy"]["container"]
    return ["/usr/bin/python3", "-I", f"{inside}/{name}/launcher.py",
            "gtk3-native", "--", *argv]


def _publish_payloads(directory, name, payloads):
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    parent = os.open(directory, flags)
    try:
        try:
            os.mkdir(name, 0o755, dir_fd=parent)
        except FileExistsError:
            pass
        child = os.open(name, flags, dir_fd=parent)
        try:
            for payload, destination, mode in payloads:
                temporary = ".picker-" + uuid.uuid4().hex
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC,
                             mode, dir_fd=child)
                try:
                    with os.fdopen(fd, "wb") as file:
                        os.fchmod(file.fileno(), mode)
                        file.write(payload)
                    os.replace(temporary, destination, src_dir_fd=child, dst_dir_fd=child)
                finally:
                    try:
                        os.unlink(temporary, dir_fd=child)
                    except FileNotFoundError:
                        pass
        finally:
            os.close(child)
    finally:
        os.close(parent)
    return name


def launch_environment(record, launch, mounts, root):
    env = dict(launch["env"])
    mode = configured_environment(record, env, on_demand=launch.get("on_demand"))
    if mode == "native":
        return env
    env.pop(MODE_KEY, None)
    if mode == "headless":
        return env
    bridge = record["desktop"]["portal_proxy"]
    directory = Path(bridge["directory"])
    inside = bridge["container"]
    expected = Path(root).resolve() / ".lab" / "desktop" / record["id"]
    if (directory != expected or directory.resolve() != expected or inside != "/run/multilinux-portal"
            or [(source, kind) for source, target, _, kind in mounts if target == inside]
            != [(str(directory), "bind")]):
        raise UnifierError("the file chooser portal bridge mount does not match the registered environment")
    shares = verified_shares(record, mounts)
    if mode in ("gtk-portal", "gtk-native-portal", "qt-portal"):
        # Native toolkit results pass through the selection delivery relay.
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={inside}/files-bus"
        env.update({"GTK_USE_PORTAL": "1"} if mode != "qt-portal"
                   else {"QT_QPA_PLATFORMTHEME": "xdgdesktopportal"})
        if mode == "gtk-native-portal":
            name, payload = _native_launcher()
            try:
                _publish_payloads(directory, name, ((payload, "launcher.py", 0o644),))
            except OSError as exc:
                raise UnifierError(f"could not prepare GTK Native compatibility: {exc}") from exc
        return env
    command = "zenity" if mode == "zenity-portal" else "kdialog"
    if not shares:
        raise UnifierError(f"{command} portal mode requires a verified data share")
    try:
        adapter = publish_picker(directory, command)
    except OSError as exc:
        raise UnifierError(f"could not prepare the {command} portal adapter: {exc}") from exc
    env["PATH"] = f"{inside}/{adapter}:{env.get('PATH') or DEFAULT_PATH}"
    if command == "kdialog":
        env["XDG_CURRENT_DESKTOP"] = "KDE"
        env["KDE_SESSION_VERSION"] = "5"
        env.pop("NO_CHROME_KDE_FILE_DIALOG", None)
    env[SHARES_ENV] = json.dumps(shares, ensure_ascii=True, separators=(",", ":"))
    return env
