#!/bin/bash
# One authorized retry. Same 512 MiB cgroup and the same pressure stop.
set -u
RUN=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$RUN/../.." && pwd)
UNIT=unifier-v004-retry
cd "$ROOT"
export RUN
python3 - <<'PY'
import hashlib, json, os, pathlib, subprocess, time
root = pathlib.Path(".").resolve()
run = pathlib.Path(os.environ["RUN"])
def read_text(path):
    try:
        return pathlib.Path(path).read_text()
    except OSError:
        return None
kwin = None
for name in ("kwin_wayland", "kwin_x11"):
    found = subprocess.run(["pidof", name], capture_output=True, text=True)
    if found.returncode == 0 and found.stdout.strip():
        pid = found.stdout.split()[0]
        stat = read_text(f"/proc/{pid}/stat") or ""
        start = stat.rsplit(")", 1)[-1].split()[19] if ")" in stat else None
        kwin = {"name": name, "pid": int(pid), "starttime": start}
        break
dirty = subprocess.run(["git", "status", "--short"], capture_output=True, text=True).stdout
files = {}
for line in dirty.splitlines():
    path = line[3:]
    target = root / path
    if target.is_file():
        files[path] = hashlib.sha256(target.read_bytes()).hexdigest()
info = {}
for line in pathlib.Path("/proc/meminfo").read_text().splitlines():
    key, _, rest = line.partition(":")
    if key in {"MemTotal", "MemAvailable", "SwapTotal", "SwapFree"}:
        info[key] = int(rest.split()[0])
payload = {
    "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "head": subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip(),
    "dirty": dirty,
    "files_sha256": files,
    "boot_id": read_text("/proc/sys/kernel/random/boot_id").strip(),
    "meminfo_kib": info,
    "psi": read_text("/proc/pressure/memory"),
    "kwin": kwin,
    "launcher": os.path.realpath("/home/omg/.local/bin/unifier"),
    "prior_attempt": "artifacts/release-v0.0.4-20261108T122119+0800",
    "containers": "not queried; none authorized",
    "desktop_session": "not operated",
}
(run / "before.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
PY
python3 "$RUN/monitor.py" >"$RUN/monitor.stdout" 2>"$RUN/monitor.stderr" &
echo $! >"$RUN/monitor.pid"
sleep 30
python3 - <<'PY'
import json, os, pathlib, sys
run = pathlib.Path(os.environ["RUN"])
need = 1024 * 1024 + 512 * 1024 + 128 * 1024
rows = [json.loads(line) for line in (run / "samples.jsonl").read_text().splitlines() if line.strip()]
window = [row for row in rows if row.get("t", 999) <= 30 and "mem_available_kib" in row]
ok = bool(window) and all(
    row["mem_available_kib"] >= need and row["psi_full_avg10"] < 0.5 and row["psi_some_avg10"] < 2
    for row in window)
summary = {
    "samples": len(window),
    "need_kib": need,
    "min_available_kib": min(row["mem_available_kib"] for row in window) if window else None,
    "max_psi_some": max(row["psi_some_avg10"] for row in window) if window else None,
    "max_psi_full": max(row["psi_full_avg10"] for row in window) if window else None,
    "admitted": ok,
}
(run / "admission.json").write_text(json.dumps(summary, indent=2) + "\n")
sys.exit(0 if ok else 2)
PY
admitted=$?
if [[ "$admitted" != 0 || -f "$RUN/abort.json" ]]; then
  touch "$RUN/monitor-stop"
  wait "$(cat "$RUN/monitor.pid")" || true
  echo "BLOCKED admission"
  exit 2
fi
systemd-run --user --unit="$UNIT" --collect \
  --property=MemoryMax=536870912 \
  --property=MemoryHigh=471859200 \
  --property=MemorySwapMax=0 \
  --property=TasksMax=96 \
  --property=CPUQuota=100% \
  --property=RuntimeMaxSec=300 \
  /bin/bash "$RUN/worker.sh"
started=$?
if [[ "$started" != 0 ]]; then
  touch "$RUN/monitor-stop"
  wait "$(cat "$RUN/monitor.pid")" || true
  echo "BLOCKED systemd-run failed"
  exit 3
fi
for _ in $(seq 1 50); do
  [[ -f "$RUN/worker.pid" ]] && break
  sleep 0.1
done
UNIT="$UNIT" python3 - <<'PY'
import json, os, pathlib, sys
run = pathlib.Path(os.environ["RUN"])
unit = os.environ["UNIT"]
pid_path = run / "worker.pid"
if not pid_path.is_file():
    (run / "cgroup-check.json").write_text(json.dumps({"ok": False, "reason": "no worker pid"}) + "\n")
    sys.exit(3)
pid = pid_path.read_text().strip()
cgroup = None
for line in pathlib.Path(f"/proc/{pid}/cgroup").read_text().splitlines():
    cgroup = line.split(":", 2)[-1]
base = pathlib.Path("/sys/fs/cgroup") / cgroup.lstrip("/")
def read(name):
    return (base / name).read_text().strip()
record = {
    "pid": int(pid),
    "cgroup": cgroup,
    "memory_max": read("memory.max"),
    "memory_high": read("memory.high"),
    "memory_swap_max": read("memory.swap.max"),
    "pids_max": read("pids.max"),
    "cpu_max": read("cpu.max"),
}
mem = int(record["memory_max"])
cpu_quota, cpu_period = record["cpu_max"].split()
record["ok"] = (
    0 < mem <= 536870912
    and record["memory_swap_max"] == "0"
    and int(record["pids_max"]) <= 96
    and int(cpu_quota) <= int(cpu_period)
    and unit in cgroup
)
(run / "cgroup-check.json").write_text(json.dumps(record, indent=2) + "\n")
sys.exit(0 if record["ok"] else 3)
PY
checked=$?
if [[ "$checked" != 0 ]]; then
  touch "$RUN/stop"
  systemctl --user stop "$UNIT" || true
  touch "$RUN/monitor-stop"
  wait "$(cat "$RUN/monitor.pid")" || true
  echo "BLOCKED cgroup limits were not applied"
  exit 3
fi
touch "$RUN/go"
while systemctl --user is-active --quiet "$UNIT"; do
  systemctl --user show "$UNIT" \
    -p ActiveState -p Result -p ExecMainStatus -p ExecMainCode -p MemoryCurrent -p TasksCurrent \
    >"$RUN/systemd-result.txt" || true
  if [[ -f "$RUN/abort.json" ]]; then
    systemctl --user stop "$UNIT" || true
    break
  fi
  sleep 0.5
done
systemctl --user show "$UNIT" \
  -p ActiveState -p Result -p ExecMainStatus -p ExecMainCode -p MemoryCurrent -p TasksCurrent \
  >"$RUN/systemd-result.txt" || true
touch "$RUN/monitor-stop"
wait "$(cat "$RUN/monitor.pid")" || true
if [[ -f "$RUN/abort.json" ]]; then
  echo "BLOCKED protection abort"
  exit 4
fi
echo "build unit finished"
exit 0
