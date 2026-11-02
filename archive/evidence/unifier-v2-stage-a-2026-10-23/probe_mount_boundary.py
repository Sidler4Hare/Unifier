from pathlib import Path
import sys, tempfile, subprocess, json
REPO=next(p for p in Path(__file__).resolve().parents if (p/"src/multilinux").is_dir())
sys.path.insert(0,str(REPO/"src"))
from multilinux.control import Control
from multilinux.errors import MultilinuxError
with tempfile.TemporaryDirectory(prefix="unifier-mount-check-") as tmp:
    base=Path(tmp); data=base/"data"; outside=base/"outside"; target=data/"nested"
    target.mkdir(parents=True); outside.mkdir(); (outside/"other.txt").write_text("outside registered root")
    control=Control(base/"state"); root=control.add_root(str(data))
    subprocess.run(["mount","--bind",str(outside),str(target)],check=True,capture_output=True)
    try:
        result={"same_device":data.stat().st_dev==target.stat().st_dev}
        try:
            control.resolve_file(root["root_uuid"],"nested/other.txt")
            result["refused"]=False
        except MultilinuxError as exc:
            result.update(refused=True,reason=str(exc))
        print(json.dumps(result))
    finally:
        subprocess.run(["umount",str(target)],check=True,capture_output=True)

if not (result.get("same_device") and result.get("refused") and "crosses a mount boundary" in result.get("reason", "")):
    raise SystemExit(1)
