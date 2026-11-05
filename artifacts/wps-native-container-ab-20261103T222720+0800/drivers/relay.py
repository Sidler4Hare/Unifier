import socket,selectors,threading
def forward(c):
 u=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
 try:
  u.connect('/tmp/.X11-unix/X199');s=selectors.DefaultSelector();s.register(c,selectors.EVENT_READ,u);s.register(u,selectors.EVENT_READ,c)
  while True:
   for k,e in s.select():
    b=k.fileobj.recv(65536)
    if not b:return
    k.data.sendall(b)
 except (OSError,ConnectionError):pass
 finally:c.close();u.close()
s=socket.socket();s.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1);s.bind(('127.0.0.1',6200));s.listen(64)
while True:
 c,_=s.accept();threading.Thread(target=forward,args=(c,),daemon=True).start()
