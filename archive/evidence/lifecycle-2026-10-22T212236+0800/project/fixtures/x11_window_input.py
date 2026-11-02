"""Send one pointer click directly to a verified experiment X11 window.

Uses a targeted X event rather than moving/clicking the user's global cursor.
The caller must supply the window PID and class observed for its own app.
"""
import argparse
import ctypes as C
import ctypes.util
import subprocess


class Button(C.Structure):
    _fields_ = [('type', C.c_int), ('serial', C.c_ulong), ('send_event', C.c_int),
                ('display', C.c_void_p), ('window', C.c_ulong), ('root', C.c_ulong),
                ('subwindow', C.c_ulong), ('time', C.c_ulong), ('x', C.c_int),
                ('y', C.c_int), ('x_root', C.c_int), ('y_root', C.c_int),
                ('state', C.c_uint), ('button', C.c_uint), ('same_screen', C.c_int)]


class Event(C.Union):
    _fields_ = [('button', Button), ('pad', C.c_long * 24)]


def click(window, pid, wm_class, x, y):
    properties = subprocess.check_output(['xprop', '-id', str(window), '_NET_WM_PID', 'WM_CLASS'], text=True)
    if f'= {pid}\n' not in properties or wm_class not in properties:
        raise RuntimeError('window does not match the expected experiment PID/class')
    lib = C.CDLL(ctypes.util.find_library('X11'))
    lib.XOpenDisplay.restype = C.c_void_p
    lib.XDefaultRootWindow.argtypes = [C.c_void_p]; lib.XDefaultRootWindow.restype = C.c_ulong
    lib.XSendEvent.argtypes = [C.c_void_p, C.c_ulong, C.c_int, C.c_long, C.POINTER(Event)]
    lib.XSync.argtypes = [C.c_void_p, C.c_int]; lib.XCloseDisplay.argtypes = [C.c_void_p]
    display = lib.XOpenDisplay(None)
    if not display: raise RuntimeError('cannot open current X display')
    try:
        for kind, mask in ((4, 1 << 2), (5, 1 << 3)):
            event = Event()
            event.button = Button(kind, 0, 1, display, window, lib.XDefaultRootWindow(display),
                                  0, 0, x, y, 0, 0, 0, 1, 1)
            if not lib.XSendEvent(display, window, 1, mask, C.byref(event)):
                raise RuntimeError('XSendEvent rejected the experiment event')
        lib.XSync(display, 0)
    finally:
        lib.XCloseDisplay(display)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('window', type=int); parser.add_argument('pid', type=int)
    parser.add_argument('wm_class');parser.add_argument('x', type=int);parser.add_argument('y', type=int)
    args = parser.parse_args()
    click(args.window, args.pid, args.wm_class, args.x, args.y)
