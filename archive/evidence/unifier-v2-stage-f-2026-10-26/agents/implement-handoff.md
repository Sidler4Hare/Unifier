# Stage F implementation handoff (local only)

Agent: dsh Command Code `deepseek/deepseek-v4.1-flash`. Branch:
`codex/unifier-v2-control`. Date: 2026-10-26 Asia/Shanghai.

Status: **tools implemented and self-tested; native execution is primary-owned
and initially pending.** I did not run prepare, native sessions, FUSE, soak or
cleanup. I did not modify `src/`, `tests/`, `README`, `HANDOFF`, old evidence,
`.lab/state`, daily FreeCAD, or any existing container. I did not commit, push,
reset or clean. No credentials were read.

## Where the implementation is

All new implementation files are under:

`evidence/unifier-v2-stage-f-2026-10-26/implementation/`

Runtime artifacts (fixture project, workspaces, guest os-release captures,
fixture manifest) are written by `prepare_fixtures.py` under that same
evidence area. `methodology.md` is at the evidence root.

The implementation directory already contains primary-owned pilot files at the
evidence root (`sessions-pilot1`, `sessions-pilot2`, `primary-*`,
`primary-resource-pilot`) and a prepared fixture (`fixtures.json`) from an
earlier primary run of the delivered prepare at 08:17. Those are preserved;
do not overwrite them.

## Tools

```bash
P=evidence/unifier-v2-stage-f-2026-10-26/implementation
E=evidence/unifier-v2-stage-f-2026-10-26

python3 $P/prepare_fixtures.py --help
python3 $P/probe_sessions.py --help
python3 $P/probe_projection.py --help
python3 $P/probe_soak.py --help
python3 $P/resource_sampler.py --help
python3 $P/cleanup_fixtures.py --help
python3 $P/cleanup_fixtures.py inspect --help
python3 $P/run_all.py --help
python3 $P/selftest.py
```

- `prepare_fixtures.py` is the only tool that creates/starts containers. It
  snapshots current `src/tools/config`, creates three stopped keep-id fixtures
  with `desktop=True` / existing `label=disable`, writes exact CIDs, UUIDs,
  project/root UUIDs, image digests, source hashes and dataset hashes. It runs
  only a fixture capability/capture step, no performance workload.
- `probe_sessions.py` uses the public CLI and real `ProjectSessions` for Fedora
  N=1/2/4/8 x 3 waves, mixed 2-per-distro waves, warm/cold end-to-end timing,
  and prepare/authorization timing.
- `probe_projection.py` uses the real `ViewManager` for five fresh view cycles
  per distro, the same workload on host direct/container bind/FUSE, mmap,
  inotify, stale replacement and open-FD semantics, and revoke cleanup checks.
- `probe_soak.py` is the bounded 180-300 s FUSE read-cache + host-bind atomic
  writer + watcher + resource-sampling soak.
- `resource_sampler.py` is reusable and can also be run standalone; it samples
  cgroup/PID-tree/host state and never starts a container.
- `cleanup_fixtures.py inspect|cleanup` is explicit and refuses unresolved
  states/live leases. It never runs `podman system prune`.
- `run_all.py` is an optional orchestrator; it stops on first failure and never
  cleans automatically.

## Exact primary execution order

Run from the repository root. Use a fresh timestamped `--run-dir` for every
probe; existing evidence is never reused. The current prepared fixture is
already stopped and may be used directly. If a fresh fixture is required, use
new paths (see below) rather than overwriting the existing ones.

```bash
cd /home/omg/文档/多兼容linux机
P=evidence/unifier-v2-stage-f-2026-10-26/implementation
E=evidence/unifier-v2-stage-f-2026-10-26

# 0. pure self-test only (no product, no container, temp dirs)
python3 $P/selftest.py

# 1. existing fixture inspection (no product execution)
python3 - <<'PY'
import json
d=json.load(open('evidence/unifier-v2-stage-f-2026-10-26/fixtures.json'))
print(d['prepared'], d['all_stopped'], d['new_container_ids'])
for t,v in d['targets'].items():
    print(t, v['environment']['id'], v['container_id'], v['environment_uuid'],
          v['root_uuid'], v['project_uuid'], v['workspace'])
PY

# 2. Fedora concurrency + mixed + timings (about 2-3 min)
python3 $P/probe_sessions.py \
  --fixtures $E/fixtures.json \
  --run-dir $E/runs/sessions-$(date +%Y%m%dT%H%M%S)

# 3. FUSE projection performance/semantics (about 2-3 min)
python3 $P/probe_projection.py \
  --fixtures $E/fixtures.json \
  --run-dir $E/runs/projection-$(date +%Y%m%dT%H%M%S)

# 4. bounded soak (default 180 s; allowed 180-300 s)
python3 $P/probe_soak.py \
  --fixtures $E/fixtures.json \
  --run-dir $E/runs/soak-$(date +%Y%m%dT%H%M%S) \
  --target debian13 --seconds 180

# 5. optional focused resource series (does not start anything)
python3 $P/resource_sampler.py \
  --fixtures $E/fixtures.json \
  --run-dir $E/runs/resources-$(date +%Y%m%dT%H%M%S) \
  --seconds 30

# 6. inspect before any cleanup
python3 $P/cleanup_fixtures.py inspect \
  --fixtures $E/fixtures.json \
  --run-dir $E/runs/cleanup-inspect-$(date +%Y%m%dT%H%M%S)

# 7. explicit cleanup only after primary accepts/compares the evidence
python3 $P/cleanup_fixtures.py cleanup \
  --fixtures $E/fixtures.json \
  --run-dir $E/runs/cleanup-$(date +%Y%m%dT%H%M%S)
```

Optional one-command sequence (still no cleanup):

```bash
python3 $P/run_all.py \
  --fixtures $E/fixtures.json \
  --run-root $E/runs/all-$(date +%Y%m%dT%H%M%S) \
  --soak-seconds 180
```

Fresh fixture alternative (do not overwrite the existing one):

```bash
python3 $P/prepare_fixtures.py \
  --output $E/fixtures-fresh.json \
  --project-dir $P/stage-f-project-fresh \
  --workspaces $P/workspaces-fresh
# then pass --fixtures $E/fixtures-fresh.json to each probe
```

Expected local native total: roughly 10-15 minutes including prepare, sessions,
projection and the 180 s soak. The prepared fixture itself took about 31 s on
this host. Guest `/etc/os-release` captures from a fresh prepare are written
into that fixture project, so a new `--project-dir` does not collide with the
existing captures.

## Fixes made from primary pilot observations

The earlier primary pilots used an intermediate revision. The delivered files
now include:

- a shared `go` gate after all sessions are parked, so the ~2 s concurrency
  window cannot start on a cold-start stagger (this was the N=4 lease race in
  `sessions-pilot1`); a bounded lease retry is also present;
- corrected timing aggregation from `{wall_ms: ...}` records (this was the
  `TypeError` in `sessions-pilot2`);
- resource cgroup resolution from the live `/proc/<pid>/cgroup` (the earlier
  relative `State.CgroupPath` handling produced `FileNotFoundError` in the
  resource pilot);
- explicit open-FD replacement observation, matching the primary cache probe's
  honest `same_fd`/reopen distinction.

Primary remains free to fix probe issues and accept independently. Any
unresolved/failed probe output is evidence, not acceptance.

## Honest limitations / expected observations

- Page caches are not dropped; "first-observed" is not kernel-cold.
- No latency SLO is asserted. Reports are min/p50/p95/max/mean samples.
- Product FUSE inotify may be `UNSUPPORTED` or `NO_EVENT` because the current
  read-only FUSE server does not emit notifications; that is recorded, not
  called success.
- An already-open FD may read new bytes if the backend reopens by path; the
  probe records `SAME_FD_READ_NEW_BYTES` rather than asserting POSIX inode
  preservation.
- The soak is explicitly 180-300 s; it is not a 24 h or durability claim.
- Command completion is not GUI first-frame. No GUI is launched by these tools.
- Cross-host work (Debian/Ubuntu host, Arch host, AppArmor vs SELinux, Wayland
  desktops, UID/namespace/storage/runtime differences) remains **pending** and
  cannot be claimed from this host. See `methodology.md` section 6.
- Fixture removal is intentionally not automatic. `cleanup_fixtures.py`
  refuses unresolved sessions/live leases and only removes exact manifest CIDs
  through the product lifecycle. Old containers are never touched and no
  global prune is used.
