import ctypes as C,sys
X=C.CDLL('libX11.so.6')
X.XOpenDisplay.restype=C.c_void_p;X.XOpenDisplay.argtypes=[C.c_char_p]
d=X.XOpenDisplay(None);assert d
X.XInternAtom.argtypes=[C.c_void_p,C.c_char_p,C.c_int];X.XInternAtom.restype=C.c_ulong
class Data(C.Union):_fields_=[('b',C.c_char*20),('s',C.c_short*10),('l',C.c_long*5)]
class Client(C.Structure):_fields_=[('type',C.c_int),('serial',C.c_ulong),('send_event',C.c_int),('display',C.c_void_p),('window',C.c_ulong),('message_type',C.c_ulong),('format',C.c_int),('data',Data)]
class Event(C.Union):_fields_=[('client',Client),('pad',C.c_long*24)]
e=Event();e.client.type=33;e.client.display=d;e.client.window=int(sys.argv[1]);e.client.message_type=X.XInternAtom(d,b'WM_PROTOCOLS',0);e.client.format=32;e.client.data.l[0]=X.XInternAtom(d,b'WM_DELETE_WINDOW',0)
X.XSendEvent.argtypes=[C.c_void_p,C.c_ulong,C.c_int,C.c_long,C.POINTER(Event)];assert X.XSendEvent(d,e.client.window,0,0,C.byref(e))
X.XFlush.argtypes=[C.c_void_p];X.XFlush(d)
