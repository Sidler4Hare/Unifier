#!/usr/bin/env python3
"""Run Mousepad on an experiment-private D-Bus bus and native Wayland."""
import os
from pathlib import Path
import sys

bus_file, note = map(Path, sys.argv[1:])
bus_file.write_text(os.environ['DBUS_SESSION_BUS_ADDRESS'])
bus_file.chmod(0o600)
os.environ.pop('DISPLAY', None)
os.environ.pop('XAUTHORITY', None)
os.environ['GDK_BACKEND'] = 'wayland'
os.environ['GSETTINGS_BACKEND'] = 'memory'
os.environ['WAYLAND_DEBUG'] = 'client'
os.execv('/usr/bin/mousepad', ['/usr/bin/mousepad', str(note)])
