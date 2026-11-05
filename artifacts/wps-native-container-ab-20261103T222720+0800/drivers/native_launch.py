import os,sys,json,subprocess,time
from pathlib import Path
P=Path(__file__).resolve().parent;cfg=json.loads((P/'native-launch.json').read_text())
env=dict(PATH='/usr/bin:/bin',USER='omg',LOGNAME='omg',**cfg['env']);children=[]
for a in cfg['commands']:children.append(subprocess.Popen(a,env=env))
(P/'native-started.json').write_text(json.dumps({'at':time.time(),'pids':[c.pid for c in children],'uid':os.getuid(),'tmp_mount':os.readlink('/proc/self/ns/mnt'),'ipc':os.readlink('/proc/self/ns/ipc')}))
while True:time.sleep(.25)
