#!/bin/bash
# Waits until the outer runner has checked this process's cgroup limits.
set -euo pipefail
RUN=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$RUN/../.." && pwd)
cd "$ROOT"
exec >>"$RUN/build.stdout" 2>>"$RUN/build.stderr"
echo "$$" >"$RUN/worker.pid"
deadline=$((SECONDS + 20))
while [[ ! -f "$RUN/go" ]]; do
  if [[ -f "$RUN/stop" ]]; then
    echo "stopped before go"
    exit 3
  fi
  if (( SECONDS >= deadline )); then
    echo "timed out waiting for go"
    exit 4
  fi
  sleep 0.1
done
exec python3 tools/build_release.py
