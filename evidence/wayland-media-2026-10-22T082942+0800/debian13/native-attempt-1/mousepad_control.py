#!/usr/bin/env python3
"""Control only Mousepad on its private experiment D-Bus session."""

import argparse
import json
import os
from pathlib import Path
import time
import xml.etree.ElementTree as ET

import gi
from gi.repository import Gio, GLib


def main():
    p = argparse.ArgumentParser()
    p.add_argument("bus_file", type=Path)
    p.add_argument("operation", choices=("list", "action"))
    p.add_argument("action", nargs="?")
    args = p.parse_args()
    os.environ["DBUS_SESSION_BUS_ADDRESS"] = args.bus_file.read_text().strip()
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    destination = "org.xfce.Mousepad"
    # Distribution versions use the same GApplication ID. Discover the exact
    # exported object paths and refuse to activate any unadvertised action.
    names = bus.call_sync("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "ListNames", None,
                          GLib.VariantType.new("(as)"), Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
    matches = [name for name in names if 'mousepad' in name.lower()]
    if len(matches) != 1:
        raise RuntimeError(f"expected one private Mousepad bus owner, found {matches}")
    destination = matches[0]
    actions = {}
    def visit(path):
        reply = bus.call_sync(destination, path, "org.freedesktop.DBus.Introspectable",
                              "Introspect", None, GLib.VariantType.new("(s)"),
                              Gio.DBusCallFlags.NONE, 5000, None).unpack()[0]
        node = ET.fromstring(reply)
        if any(i.get('name') == 'org.gtk.Actions' for i in node.findall('interface')):
            offered = bus.call_sync(destination, path, 'org.gtk.Actions', 'List', None,
                                    GLib.VariantType.new('(as)'), Gio.DBusCallFlags.NONE,
                                    5000, None).unpack()[0]
            actions[path] = list(offered)
        for child in node.findall('node'):
            visit(path.rstrip('/') + '/' + child.get('name'))
    visit('/')
    if args.operation == 'list':
        print(json.dumps({'destination': destination, 'actions': actions}, ensure_ascii=False))
        return
    paths = [path for path, names in actions.items() if args.action in names]
    if len(paths) != 1:
        raise RuntimeError(f"action {args.action} is not uniquely advertised: {paths}")
    bus.call_sync(destination, paths[0], 'org.gtk.Actions', 'Activate',
                  GLib.Variant('(sava{sv})', (args.action, [], {})), None,
                  Gio.DBusCallFlags.NONE, 5000, None)
    print(json.dumps({'action': args.action, 'path': paths[0], 'activated': True}))


if __name__ == '__main__':
    main()
