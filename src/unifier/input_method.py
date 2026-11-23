"""Route a verified local X11 client's input method to its host session."""

import os
from pathlib import Path
import re

from unifier.errors import UnifierError


MODES = ("auto", "native", "host-xim")
_DISPLAY = re.compile(r":([0-9]+)(?:\.[0-9]+)?\Z")
_XMODIFIERS = re.compile(r"@im=[A-Za-z0-9_.-]+\Z")


def _socket_path(display):
    match = _DISPLAY.fullmatch(display)
    return f"/tmp/.X11-unix/X{match[1]}" if match else None


def validate_input_method(record, environment, mode):
    if mode not in MODES:
        raise UnifierError(f"unknown input method mode: {mode}")
    if mode == "host-xim":
        socket = _socket_path(environment.get("DISPLAY", ""))
        if not socket or not _registered_socket(record, socket):
            raise UnifierError("host-xim requires a registered local X11 display mount")


def _registered_socket(record, socket):
    desktop = record.get("desktop") or {}
    return any(mount.get("host") == mount.get("container") == socket
               for mount in desktop.get("mounts") or ())


def apply_input_method(record, environment, mounts, argv, *, mode="auto",
                       environ=None, is_socket=None):
    """Apply the input settings to the per-exec copy of the environment."""
    validate_input_method(record, environment, mode)
    if mode == "native":
        return
    socket = _socket_path(environment.get("DISPLAY", ""))
    if not socket or not _registered_socket(record, socket):
        return
    host = os.environ if environ is None else environ
    modifiers = host.get("XMODIFIERS", "")
    if (_socket_path(host.get("DISPLAY", "")) != socket or not _XMODIFIERS.fullmatch(modifiers)
            or modifiers in ("@im=none", "@im=local")
            or [(source, kind) for source, target, _, kind in mounts if target == socket]
            != [(socket, "bind")]):
        return
    if not (Path(socket).is_socket() if is_socket is None else is_socket(socket)):
        return

    if mode == "host-xim":
        environment.update(GTK_IM_MODULE="xim", XMODIFIERS=modifiers)
        return
    environment.setdefault("XMODIFIERS", modifiers)
    # With both displays present, native GTK apps must keep their Wayland
    # input context. Select xim only when the app is known to use X11.
    if (not environment.get("WAYLAND_DISPLAY")
            or environment.get("GDK_BACKEND") == "x11"
            or "--ozone-platform=x11" in argv):
        environment.setdefault("GTK_IM_MODULE", "xim")
