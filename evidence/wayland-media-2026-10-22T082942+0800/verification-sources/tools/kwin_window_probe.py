"""Inspect and focus only an experiment window through transient KWin scripts.

No persistent plugin/settings changes. A private D-Bus callback receives only
matching windows. Screenshots are accepted only if the selected experiment
window is active both before and after Spectacle captures the active window.
"""

import json
from pathlib import Path
import subprocess
import time
import uuid

import gi
from gi.repository import Gio, GLib


def window_info(directory, marker, *, activate=False, pids=(), resource_hint='', window_id=''):
    directory = Path(directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    name = "org.multilinux.WindowProbe.p" + token
    plugin = "multilinux-probe-" + token
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                  "RequestName", GLib.Variant("(su)", (name, 0)), None,
                  Gio.DBusCallFlags.NONE, 5000, None)
    received = []
    xml = '<node><interface name="org.multilinux.WindowProbe"><method name="Report"><arg type="s" direction="in"/></method></interface></node>'
    interface = Gio.DBusNodeInfo.new_for_xml(xml).interfaces[0]
    def callback(connection, sender, path, iface, method, parameters, invocation):
        received.append(json.loads(parameters.unpack()[0]))
        invocation.return_value(None)
    registration = bus.register_object("/org/multilinux/WindowProbe", interface, callback, None, None)
    body = f"""
const marker = {json.dumps(marker)};
const pids = {json.dumps(list(pids))};
const hint = {json.dumps(resource_hint)};
const windowId = {json.dumps(window_id)};
let found = [];
for (const w of workspace.windowList()) {{
    const owned = (marker.length > 0 && String(w.caption).indexOf(marker) !== -1) ||
                  (pids.indexOf(w.pid) !== -1 && (hint.length === 0 || String(w.resourceClass).toLowerCase().indexOf(hint) !== -1));
    if (owned && (windowId.length === 0 || String(w.internalId) === windowId)) {{
        if ({str(activate).lower()}) {{ w.minimized = false; workspace.activeWindow = w; }}
        found.push({{caption: String(w.caption), resourceClass: String(w.resourceClass),
                    pid: w.pid, objectType: String(w),
                    wayland: typeof w.waylandClient === 'boolean' ? w.waylandClient : null,
                    x11: typeof w.x11Client === 'boolean' ? w.x11Client : null,
                    width: w.frameGeometry.width, height: w.frameGeometry.height,
                    internalId: String(w.internalId), active: workspace.activeWindow === w}});
    }}
}}
callDBus({json.dumps(name)}, '/org/multilinux/WindowProbe', 'org.multilinux.WindowProbe', 'Report', JSON.stringify(found));
"""
    script = directory / ("kwin-" + token + ".js")
    script.write_text(body)
    try:
        script_id = bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting",
                                  "loadScript", GLib.Variant("(ss)", (str(script), plugin)),
                                  GLib.VariantType.new("(i)"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
        if script_id < 0:
            raise RuntimeError("KWin could not load the temporary window probe")
        bus.call_sync("org.kde.KWin", f"/Scripting/Script{script_id}", "org.kde.kwin.Script",
                      "run", None, None, Gio.DBusCallFlags.NONE, 5000, None)
        context = GLib.MainContext.default()
        deadline = time.monotonic() + 5
        while not received and time.monotonic() < deadline:
            while context.pending():
                context.iteration(False)
            time.sleep(0.01)
        if not received:
            raise RuntimeError("KWin window probe did not return a result")
        return received[0]
    finally:
        try:
            bus.call_sync("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "unloadScript",
                          GLib.Variant("(s)", (plugin,)), None, Gio.DBusCallFlags.NONE, 5000, None)
        finally:
            bus.unregister_object(registration)
            bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus",
                          "ReleaseName", GLib.Variant("(s)", (name,)), None,
                          Gio.DBusCallFlags.NONE, 5000, None)


def screenshot(directory, marker, output, *, pids=(), resource_hint='', window_id=''):
    before = window_info(directory, marker, activate=True, pids=pids, resource_hint=resource_hint, window_id=window_id)
    if len(before) != 1 or not before[0]["active"]:
        raise RuntimeError(f"expected one active experiment window, got {before}")
    result = subprocess.run(
        ["spectacle", "--new-instance", "--background", "--activewindow", "--nonotify",
         "--no-shadow", "--output", str(output)], capture_output=True, text=True, timeout=30)
    after = window_info(directory, marker, pids=pids, resource_hint=resource_hint, window_id=window_id)
    valid = len(after) == 1 and after[0]["active"] and after[0]["internalId"] == before[0]["internalId"]
    if not valid:
        Path(output).unlink(missing_ok=True)
        raise RuntimeError("active window changed during capture; rejected the screenshot")
    if result.returncode or not Path(output).is_file():
        raise RuntimeError("Spectacle did not capture the native window: " + result.stderr[-1000:])
    return {"before": before[0], "after": after[0], "path": str(output), "capture": "active experiment window only"}
