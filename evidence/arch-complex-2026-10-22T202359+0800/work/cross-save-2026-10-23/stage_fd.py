
import json, os, sys, time
from pathlib import Path
path, ready, go, dest = map(Path, sys.argv[1:])
fd = os.open(path, os.O_RDWR)
stale = os.read(fd, 4096)
Path(ready).write_text("ready\n")
while not Path(go).exists():
    time.sleep(0.02)
os.lseek(fd, 0, os.SEEK_SET)
os.ftruncate(fd, 0)
written = os.write(fd, "容器写到替换前打开的描述符\n".encode())
os.fsync(fd)
os.close(fd)
dest.write_text(json.dumps({
    "stale": stale.decode(), "bytes_written_to_old_fd": written,
    "path_text_after_old_fd_write": path.read_text(),
    "path_inode_after": path.stat().st_ino,
}, ensure_ascii=False, indent=2))
