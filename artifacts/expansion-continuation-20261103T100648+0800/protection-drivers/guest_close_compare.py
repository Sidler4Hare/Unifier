import ctypes as C,time,json
x=C.CDLL('libX11.so.6');x.XOpenDisplay.argtypes=[C.c_char_p];x.XOpenDisplay.restype=C.c_void_p;d=x.XOpenDisplay(None);assert d
x.XInternAtom.argtypes=[C.c_void_p,C.c_char_p,C.c_int];x.XInternAtom.restype=C.c_ulong
class Data(C.Union):_fields_=[('l',C.c_long*5)]
class Client(C.Structure):_fields_=[('type',C.c_int),('serial',C.c_ulong),('send_event',C.c_int),('display',C.c_void_p),('window',C.c_ulong),('message_type',C.c_ulong),('format',C.c_int),('data',Data)]
class Event(C.Union):_fields_=[('client',Client),('pad',C.c_long*24)]
e=Event();e.client.type=33;e.client.display=d;e.client.window=0x400007;e.client.message_type=x.XInternAtom(d,b'WM_PROTOCOLS',0);e.client.format=32;e.client.data.l[0]=x.XInternAtom(d,b'WM_DELETE_WINDOW',0)
x.XSendEvent.argtypes=[C.c_void_p,C.c_ulong,C.c_int,C.c_long,C.POINTER(Event)];assert x.XSendEvent(d,0x400007,0,0,C.byref(e))==1
x.XFlush.argtypes=[C.c_void_p];x.XFlush(d);x.XCloseDisplay.argtypes=[C.c_void_p];x.XCloseDisplay(d);print(json.dumps({'close_protocol':'WM_DELETE_WINDOW','window':'0x400007','forced_signal':False}));time.sleep(2)
