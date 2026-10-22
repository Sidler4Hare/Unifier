"""A moving, native Wayland window containing only synthetic test content."""
import argparse
import json
import os
from pathlib import Path
import time

os.environ['GDK_BACKEND'] = 'wayland'
os.environ.pop('DISPLAY', None)
import gi
gi.require_version('Gtk', '3.0')
from gi.repository import Gtk, Gdk, GLib

parser = argparse.ArgumentParser()
parser.add_argument('--state', type=Path, required=True)
args = parser.parse_args()
window = Gtk.Window(title='Multilinux Screen Share Test')
window.set_default_size(720, 450)
grid = Gtk.Grid(column_homogeneous=True, row_homogeneous=True)
window.add(grid)
labels = []
for i, (name, color) in enumerate([('RED', '#b12b31'), ('GREEN', '#167c44'), ('BLUE', '#1d5bc4'), ('FRAME', '#26334b')]):
    box = Gtk.EventBox(); label = Gtk.Label()
    label.set_markup(f'<span foreground="white" size="24000" weight="bold">{name}</span>')
    box.add(label);box.override_background_color(Gtk.StateFlags.NORMAL, Gdk.RGBA(*[int(color[j:j+2],16)/255 for j in (1,3,5)],1))
    grid.attach(box,i%2,i//2,1,1);labels.append(label)
counter = 0
def update():
    global counter
    counter += 1
    labels[-1].set_markup(f'<span foreground="white" size="24000" weight="bold">FRAME {counter:05d}</span>')
    state = {'pid': os.getpid(), 'frame': counter, 'time': time.time(),
             'display_type': Gdk.Display.get_default().__gtype__.name,
             'content': 'Synthetic color blocks and moving frame counter only'}
    args.state.write_text(json.dumps(state, indent=2)+'\n')
    return True
window.connect('destroy', Gtk.main_quit)
window.show_all();GLib.timeout_add(250, update)
Gtk.main()
