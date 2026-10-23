
import json, os, sys, time
from pathlib import Path

def atomic(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".editor.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    os.write(fd, data)
    os.fsync(fd)
    os.close(fd)
    before = path.stat().st_ino if path.exists() else None
    os.replace(tmp, path)
    return {"protocol": "temp-fsync-replace", "temp": tmp.name,
            "inode_before": before, "inode_after": path.stat().st_ino}

def inplace(path, data):
    path = Path(path)
    before = path.stat().st_ino
    fd = os.open(path, os.O_WRONLY | os.O_TRUNC)
    os.write(fd, data)
    os.fsync(fd)
    os.close(fd)
    return {"protocol": "truncate-same-fd", "inode_before": before,
            "inode_after": path.stat().st_ino}

def wait_go(ready, go):
    Path(ready).write_text("ready\n")
    while not Path(go).exists():
        time.sleep(0.02)

def stage_replace(path, ready, go):
    path = Path(path)
    stale = path.read_bytes()
    tmp = path.with_name(path.name + ".stale.tmp")
    tmp.write_bytes(stale)
    wait_go(ready, go)
    os.replace(tmp, path)
    return {"protocol": "read-then-replace-after-host", "stale": stale.decode(),
            "final_inode": path.stat().st_ino}

def stage_fd(path, ready, go):
    path = Path(path)
    fd = os.open(path, os.O_RDWR)
    stale = os.read(fd, 4096)
    wait_go(ready, go)
    os.lseek(fd, 0)
    os.ftruncate(fd, 0)
    os.write(fd, "容器写到替换前打开的描述符\n".encode())
    os.fsync(fd)
    os.close(fd)
    return {"protocol": "write-old-fd-after-host-replace", "stale": stale.decode(),
            "final_text": path.read_text(), "final_inode": path.stat().st_ino}

def main():
    action = sys.argv[1]
    if action == "atomic":
        result = atomic(sys.argv[2], sys.argv[3].encode())
    elif action == "inplace":
        result = inplace(sys.argv[2], sys.argv[3].encode())
    elif action == "stage-replace":
        result = stage_replace(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "stage-fd":
        result = stage_fd(sys.argv[2], sys.argv[3], sys.argv[4])
    elif action == "try-inplace":
        try:
            result = inplace(sys.argv[2], sys.argv[3].encode())
            result["error"] = None
        except OSError as exc:
            result = {"protocol": "truncate-same-fd", "error": exc.__class__.__name__,
                      "errno": exc.errno, "message": os.strerror(exc.errno)}
    elif action == "try-atomic":
        try:
            result = atomic(sys.argv[2], sys.argv[3].encode())
            result["error"] = None
        except OSError as exc:
            result = {"protocol": "temp-fsync-replace", "error": exc.__class__.__name__,
                      "errno": exc.errno, "message": os.strerror(exc.errno)}
    else:
        raise SystemExit("unknown action")
    Path(sys.argv[-1]).write_text(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
