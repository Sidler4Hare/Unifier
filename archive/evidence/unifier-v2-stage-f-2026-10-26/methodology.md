# Stage F local performance/stress probe methodology

Date: 2026-10-26, Asia/Shanghai. Implementation status: **local tools delivered;
real acceptance is pending primary execution and review.** The implementation
agent did not run prepare, native sessions, FUSE, soak or cleanup. Existing
primary-owned pilot files in this evidence directory are not part of this
methodology and are not accepted by it.

Scope: **local only.** Any host other than the current Fedora 44 host is
explicitly pending and cannot be claimed from this suite.

## 1. Local matrix

| Dimension | Local value / probe coverage |
|---|---|
| Host | Fedora 44 (KDE, Wayland), kernel 7.2.11, x86_64, ~8 GiB RAM |
| Runtime | rootless Podman 5.8.9, crun, overlay, cgroup v2 |
| Guests | Debian 13, Fedora 44, Arch; one keep-id environment per distro |
| Identity | guest document UID/GID 1000:1000 (host user mapped by keep-id) |
| Fixture source | frozen copy of current `src/`, `tools/` and `config/` inside the fixture project; probes import only that snapshot |
| Shared data | one same-path authorized host bind per distro, no active rootfs mutation |
| Fixture size | 2,000 x 4 KiB files + one 16 MiB sequential file + links/test files = 24,969,288 bytes per distro (limit 128 MiB) |
| Desktop convention | `desktop=True` with the existing `label=disable` session; host sockets are mounted, the host is not relabelled |
| Concurrency | Fedora 44 at N=1/2/4/8, three waves each; three-distro mixed, two sessions per distro |
| Projection | real `ViewManager` read-only product FUSE view of stopped environments |
| Soak | bounded 180-300 s, default 180 s; no cache drop and no 24 h claim |

The workload interpreter is `/bin/sh` in every path, with `stat`, `cat`,
`sha256sum`, `dd`, `id`, `cut`, `awk` and `sleep` from the guest/host
userspace. The same `workload.sh` file is invoked on the host direct bind,
through the product FUSE mount, and inside a live container bind. Internal
timing covers the same three phases in all three paths.

## 2. Stage 2: real project sessions

### 2.1 Structural checks (pass/fail)

For each Fedora wave:

* one container ID and one environment UUID are reused; no new container is
  created after prepare (`container_create_delta == 0`);
* exactly one cold start is observed for that wave (`StartedAt` changes once);
* N sessions reach `running` with N live leases and N distinct guest identities
  (`guest_pid` + `guest_start`), bound to the same CID;
* every session writes a `ready` marker and then waits at a shared `go` gate.
  The coordinator checks leases/identities and releases `go`; therefore the
  ~2 s workload window is genuinely overlapping and the first completion
  cannot occur before all sessions were live;
* after the first session exits, the container is still running and at least
  one other lease remains (for N>1); this is the no-early-stop check;
* after the last session exits, the environment auto-stops within the bounded
  wait; no lease, no unresolved/interrupted/failed session remains; each
  session is `exited` with the expected exit code, guest state `gone` and the
  expected 4 KiB zero hash and UID 1000;
* the three-distro mixed wave runs two sessions per distro concurrently and
  performs the same per-container checks.

`N=1` cannot have a remaining lease after the first exit; the early-stop check
is still recorded as `false/0` and is treated as not applicable.

### 2.2 Timing definitions (separate from concurrency)

* **Cold command end-to-end**: wall time from launching the public CLI
  (`tools/unifier.py project run ...`) to its exit, with the fixture stopped
  before the call. It includes CLI/worker/lease/cold-start/exec and the small
  `dd`+`sha256sum` command; it is not GUI first-frame and not an SLO.
* **Warm command end-to-end**: same measurement after a manual
  `EnvironmentService.start`; the manual start must be preserved and
  `StartedAt` must not change across the five runs.
* **Prepare/authorization**: in-process `ProjectSessions.prepare()` wall time
  for 20 calls per target (no start, bounded read-only validation), plus five
  public `project plan` CLI calls. The fixture must remain stopped and the
  binding must be unchanged.
* The resource sampler is paused while individual timing calls run; this is
  recorded, so timing overhead from sampling is not silently mixed in.

Distributions are reported as measured min/p50/p95/max/mean/range. No latency
threshold is asserted.

## 3. Stage 3: product FUSE projection

### 3.1 Paths and workload

Same `workload.sh`, same files, same interpreter:

1. **host direct** — the authorized shared bind on the host;
2. **container bind** — the same absolute path read inside the live container
   through the product runtime exec API;
3. **product FUSE** — the real `ViewManager` read-only projection of the
   stopped environment, mounted through the existing product FUSE server.

Per repetition the script measures:

* `stat` of all 2,000 small files via `stat(1)` (metadata phase);
* reading all 4,096 bytes of every small file and hashing the concatenation
  (per-file open/read phase, with byte-correctness verification);
* reading the 16 MiB sequential file and hashing it (sequential phase).

Five repetitions per path (five fresh view cycles x two repetitions for FUSE).
**first-observed** means the first access in a fresh view cycle; **warm** means
subsequent repetitions. Page caches are never dropped, so first-observed is
not a kernel-cold measurement.

### 3.2 Lifecycle and semantics

* View open, first stat/read after open (acquire), and revoke are timed
  separately per cycle.
* Absolute links: the raw container target must be rewritten to the view
  mountpoint; following the rewritten link must read the guest `/etc/os-release`
  bytes, not the host file. The live container bind is the control.
* Relative links must stay within the projection and read the expected bytes.
* Read-only checks: `O_RDWR` open and a writable mmap attempt must be refused;
  a read-only mmap must read the expected bytes.
* inotify: the host bind watcher is the control. A FUSE watch that cannot be
  created is `UNSUPPORTED`; a watch that is created but emits no event is
  `NO_EVENT`, not a pass. External writer events are not claimed as product
  cache invalidation.
* External replacement in the authorized bind is tested for both a closed
  path (reopen after the kernel attribute timeout) and an already-open FD.
  POSIX old-inode preservation and backend path-reopen behaviour are recorded
  as observations/limitations; they are not forced into a false pass.
* Revoke must leave no registered view, FUSE mount, worker, broker, launcher,
  control/storage socket or process reference for that view.

### 3.3 Soak

One 180-300 s bounded soak on the real view:

* reader repeatedly stats/reads the 2,000 files, checks every read against the
  deterministic fixture content, reads a rotating 1 MiB slice of the
  sequential file, and reads the atomically replaced 4 KiB file;
* a host-bind writer atomically replaces that 4 KiB file once per second and
  fsyncs; no rootfs is mutated;
* host-bind and FUSE inotify watchers are recorded separately;
* the resource sampler samples once per second;
* the view is revoked at the end and the same no-reference checks run.

This is explicitly a short bounded soak, not a 24 h durability test, and no
latency SLO or cache-eviction claim is made.

## 4. Stage 4: resource sampling

Identity is **PID + kernel start time** everywhere; recycled PIDs cannot be
mistaken for the old process. Categories are disjoint:

* guest processes from each container cgroup;
* project-session host workers and their `podman exec` proxies;
* FUSE worker, broker, `podman unshare` launcher and storage helper;
* conmon/runtime process;
* probe client where relevant.

Measurements:

* container cgroup: `memory.current`, `memory.peak`, `memory.swap.current`,
  `memory.stat`, `memory.events` (including `oom_kill`), `memory.pressure`,
  `cpu.stat`, `pids.current/peak`;
* per-process: PSS from host `smaps_rollup`, RSS, threads, open FD count, and
  identity; guest `/proc` via a `podman top` host-pid -> guest-pid mapping is
  attempted only when host PSS is denied;
* host: `MemAvailable`, swap, memory/cpu/io PSI, load average.

Rules:

* PSS categories are summed only when the category's PSS coverage is complete; otherwise the report gives `pss_partial_bytes` and coverage. Missing PSS is
  `UNKNOWN`, never zero.
* PSS is never added to `memory.current`; cgroup charge and process PSS answer
  different questions.
* If a host PID tree cannot be discovered or a file is denied/gone, the error
  and `UNKNOWN` are retained.
* Sampling reads `/proc` and cgroup files only. Individual timing paths pause
  the sampler; the reported mean sample duration is the observed overhead.

## 5. Safety and evidence rules

* Abort if `MemAvailable < 700 MiB` or if memory PSI `full avg10 >= 50` or
  `some avg10 >= 90`. Missing MemAvailable fails closed. OOM-kill growth in a
  fixture cgroup also stops the run.
* No global cache drop, no OOM injection, no sysctl change, no host package,
  no network download, no GUI focus stealing, no host relabel.
* Each fixture is <=128 MiB; the concurrency shell window is ~2 s plus bounded
  per-session spacing; the soak is 180-300 s.
* Run directories are exclusive and never reused. Failure records are
  append-only (`failures.jsonl`); a failed probe exits non-zero and is never
  converted into an accepted result.
* Cleanup is a separate explicit subcommand. It preflights every fixture and
  refuses to remove anything if a session is unresolved/interrupted, a lease
  is live, or a failed view remains. It removes only exact manifest CIDs via
  the product lifecycle; it never runs `podman system prune`.
* Unsupported semantics are `UNSUPPORTED`; unavailable metrics are `UNKNOWN`.
  Structural correctness (exit status, bytes, CID, leases, cleanup) is kept
  separate from performance observations.

## 6. Pending cross-host matrix (not claimable locally)

The following require another machine or a different host environment and are
**pending**, not passed:

| Dimension | Pending cases |
|---|---|
| Host distribution | Debian/Ubuntu host, Arch host; second Fedora host |
| MAC policy | AppArmor host vs SELinux host; enforcing/permissive differences |
| Desktop | KDE Wayland, GNOME Wayland, X11 fallback; compositor/session socket differences |
| UID/namespace | rootless default vs keep-id; subuid/subgid ranges; userns restrictions |
| Storage/runtime | overlay vs fuse-overlayfs/vfs; cgroup v2 delegation differences; Podman/crun versions |
| Mounts | propagation, ro/rw bind, secret/credential mounts, SELinux label interaction |
| Packages | host package/glibc/FUSE helper differences; kernel PSI availability |
| Hardware | CPU count, RAM, disk speed, thermal/power policy |

Only the local host matrix above is exercised by these tools. A second host
must run the same suite and preserve its own baseline; a local pass cannot be
extrapolated.
