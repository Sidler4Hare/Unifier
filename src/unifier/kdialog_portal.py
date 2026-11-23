#!/usr/bin/python3
"""Guest kdialog adapter for applications with a command-based KDE picker."""

import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
from urllib.parse import unquote_to_bytes, urlsplit
import uuid

if __package__:
    from .kdialog_args import parse_args
    from .zenity_args import parse_args as parse_zenity_args
else:
    from kdialog_args import parse_args
    from zenity_args import parse_args as parse_zenity_args


BUS = "org.freedesktop.portal.Desktop"
OBJECT = "/org/freedesktop/portal/desktop"
CHOOSER = "org.freedesktop.portal.FileChooser"
REQUEST = "org.freedesktop.portal.Request"
SHARES_ENV = "UNIFIER_KDIALOG_SHARES"


def absolute_path(value):
    if not isinstance(value, str) or not value.startswith("/") or value.startswith("//"):
        raise ValueError("file chooser requires an absolute local path")
    if any(char in value for char in "\0\n\r") or ".." in value.split("/"):
        raise ValueError("file chooser received an unsupported path")
    return PurePosixPath(value)


def translated_path(path, shares, *, reverse=False):
    source, destination = (1, 0) if reverse else (0, 1)
    matches = []
    for share in shares:
        root = absolute_path(share[source])
        if path.is_relative_to(root):
            matches.append((absolute_path(share[destination]) / path.relative_to(root), share[2]))
    if len(matches) != 1:
        raise ValueError("choose a file within an unambiguous Unifier shared directory")
    return matches[0]


def guest_paths(uris, shares, request):
    if (not isinstance(uris, (list, tuple)) or not 1 <= len(uris) <= 256
            or (not request.multiple and len(uris) != 1)):
        raise ValueError("unexpected number of files from the host picker")
    paths = []
    for uri in uris:
        if not isinstance(uri, str) or len(uri) > 32768 or any(ord(char) < 32 for char in uri):
            raise ValueError("invalid file URI from the host picker")
        parsed = urlsplit(uri)
        if (parsed.scheme != "file" or parsed.netloc not in ("", "localhost")
                or parsed.query or parsed.fragment or re.search(r"%(?![0-9a-fA-F]{2})", parsed.path)):
            raise ValueError("only local files from shared directories can be selected")
        # The common host broker already returned a guest URI. Translating it
        # again would deny snapshots and could route a valid path elsewhere.
        guest = Path(absolute_path(unquote_to_bytes(parsed.path).decode("utf-8")))
        matches = [(absolute_path(root), writable) for _host, root, writable in shares
                   if guest.is_relative_to(absolute_path(root))]
        depth = max((len(root.parts) for root, _ in matches), default=0)
        writable = bool(matches) and all(value for root, value in matches if len(root.parts) == depth)
        if guest.resolve(strict=request.mode != "save") != guest:
            raise ValueError("the selected path resolves through a symbolic link")
        if request.mode == "save":
            if (not writable or not guest.parent.is_dir()
                    or not os.access(guest.parent, os.W_OK)
                    or (guest.exists() and (not guest.is_file() or not os.access(guest, os.W_OK)))):
                raise ValueError("choose a destination in a writable Unifier shared directory")
        elif not (guest.is_dir() if request.mode == "folder" else guest.is_file()):
            raise ValueError("the selection is not available as the requested file type in the guest")
        elif not os.access(guest, os.R_OK | (os.X_OK if request.mode == "folder" else 0)):
            raise ValueError("the selection is not readable inside the guest")
        paths.append(str(guest))
    return paths


def picker_options(request, shares, variant, token):
    result = {"handle_token": variant("s", token), "modal": variant("b", request.modal)}
    if request.mode != "save":
        result.update(
            directory=variant("b", request.mode == "folder"),
            multiple=variant("b", request.multiple),
            writable=variant("b", False),
        )
    if request.filters:
        result["filters"] = variant("a(sa(us))", [
            (label, [(1 if "/" in pattern else 0, pattern) for pattern in patterns])
            for label, patterns in request.filters
        ])
    start = Path(request.start)
    fallback = next((host for host, _, writable in shares
                     if request.mode != "save" or writable), None)
    if fallback:
        result["current_folder"] = variant("ay", os.fsencode(fallback) + b"\0")
    if start.is_absolute():
        folder = start if start.is_dir() else start.parent
        try:
            mapped, writable = translated_path(absolute_path(str(folder)), shares, reverse=True)
        except ValueError:
            pass
        else:
            if request.mode != "save" or writable:
                result["current_folder"] = variant("ay", os.fsencode(str(mapped)) + b"\0")
    if (request.mode == "save" and start.name not in ("", ".", "..")
            and (start.is_absolute() or start.name == request.start)
            and not start.is_dir()):
        absolute_path("/" + start.name)
        result["current_name"] = variant("s", start.name)
    return result


def select(connection, Gio, GLib, request, shares):
    response = []
    loop = GLib.MainLoop()
    handle = ""
    expired = False

    def completed(_connection, _sender, path, _interface, _signal, parameters):
        if path == handle and not response:
            response.append(parameters.unpack())
            loop.quit()

    def timeout():
        nonlocal expired
        expired = True
        loop.quit()
        return False

    subscription = connection.signal_subscribe(
        BUS, REQUEST, "Response", None, None, Gio.DBusSignalFlags.NONE, completed,
    )
    timer = None
    try:
        options = picker_options(request, shares, GLib.Variant, "unifier_" + uuid.uuid4().hex)
        reply = connection.call_sync(
            BUS, OBJECT, CHOOSER, "SaveFile" if request.mode == "save" else "OpenFile",
            GLib.Variant("(ssa{sv})", (request.parent, request.title, options)),
            GLib.VariantType.new("(o)"), Gio.DBusCallFlags.NONE, 10000, None,
        )
        handle, = reply.unpack()
        timer = GLib.timeout_add_seconds(600, timeout)
        loop.run()
        if expired:
            connection.call_sync(BUS, handle, REQUEST, "Close", None, None,
                                 Gio.DBusCallFlags.NO_AUTO_START, 2000, None)
            raise TimeoutError("the host file chooser did not complete")
    finally:
        if timer is not None and not expired:
            GLib.source_remove(timer)
        connection.signal_unsubscribe(subscription)
    status, results = response[0]
    if status == 1:
        return []
    if status != 0:
        raise ValueError("the host file chooser failed")
    return guest_paths(results.get("uris"), shares, request)


def main(argv, *, command="kdialog"):
    try:
        if os.environ.get('UNIFIER_FILE_ACCESS_PROTOCOL') != '2':
            raise ValueError('file chooser requires the unified file access protocol')
        request = (parse_zenity_args if command == "zenity" else parse_args)(argv)
        import gi

        gi.require_version("Gio", "2.0")
        from gi.repository import Gio, GLib

        connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        if request is None:
            reply = connection.call_sync(
                BUS, OBJECT, "org.freedesktop.DBus.Properties", "Get",
                GLib.Variant("(ss)", (CHOOSER, "version")), GLib.VariantType.new("(v)"),
                Gio.DBusCallFlags.NONE, 2000, None,
            )
            version, = reply.unpack()
            if version < 3:
                raise ValueError("the file chooser portal must support directory selection")
            # Chromium uses the description/glob format from KDialog 19.12.
            print("3.44.0" if command == "zenity" else "kdialog 19.12.0")
            return 0
        paths = select(connection, Gio, GLib, request, json.loads(os.environ[SHARES_ENV]))
        if not paths:
            return 1
        if request.multiple and any(request.separator in path for path in paths):
            raise ValueError("selection contains the requested output separator")
        print(request.separator.join(paths))
        return 0
    except Exception as error:
        print(f"Unifier file chooser: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:], command=Path(sys.argv[0]).name))
