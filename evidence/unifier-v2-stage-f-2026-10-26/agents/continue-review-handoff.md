# Stage F bounded measurement review

Agent: dsh Command Code `deepseek-v4.1-flash`.
Date: 2026-10-26 Asia/Shanghai.
Scope: one bounded review of the Stage F measurement tools. **No formal
sessions/projection/soak load was run by this review.** At review time the
primary's `sessions-formal-1` was running; I did not touch `probe_sessions.py`
or that process.

## Constraints honored

- Only `evidence/unifier-v2-stage-f-2026-10-26/implementation/probe_soak.py`
  was modified.
- `probe_sessions.py`, `probe_projection.py`, `resource_sampler.py`,
  `cleanup_fixtures.py`, `common.py`, `prepare_fixtures.py`, fixtures,
  `stage-f-project`, `.lab/state`, `src`, `tests`, README/HANDOFF and old
  evidence were read-only.
- No container was started/stopped/cleaned, no formal workload was run, no
  agent was started, and nothing was committed/pushed.
- `implement-handoff.md` was preserved unchanged.

## Exact change: `probe_soak.py`

### 1. Host watcher is now the ordinary bind path

Old code watched `fuse_dataset / "soak"` for both watchers, so the supposed
host control was actually the projection path.

Now:

- `host_watch_path = current_path.parent` — the host ordinary bind
  (`.../workspaces/<target>/dataset/soak`).
- `fuse_watch_path = fuse_current.parent` — the product FUSE projection
  (`<view-mountpoint>/.../dataset/soak`).
- `watch_paths_distinct` is evaluated before the view is revoked, while both
  paths still exist.
- New checks: `watch_paths_distinct`, `host_writer_event_observed`,
  `host_no_external_events`.

Output now records both real paths explicitly:

- `inotify.host_watch_path`
- `inotify.fuse_watch_path`
- `inotify.host_writer_path`
- `inotify.host_event_names`
- `inotify.host_writer_event_count`
- `inotify.host_internal_temp_event_names` (this probe's atomic-replace temp
  files)
- `inotify.host_external_event_names` (unrelated/external modifications)
- `inotify.host_event_masks`
- `inotify.fuse_event_count` / `fuse_verdict`

The soak now fails if no host writer event is observed, if a host event came
from an unrelated path, or if the two watcher paths are not distinct. It does
**not** treat FUSE watch creation without events as success: that remains
`NO_EVENT`/`UNSUPPORTED`.

### 2. Freshness / writer coverage

`current_reads` now includes:

- `fresh_ratio`
- `distinct_reader_sha256`
- `last_reader_sha256`
- `last_writer_sha256`
- `writer_versions`

Fresh vs stale/racy reads remain observations, not a cached-success claim.
The writer must now produce approximately the expected number of writes
(`writer_progressed`), not merely one write.

### 3. Sampling/statistics and empty-collection gaps

Added structural checks so a soak cannot pass with empty or missing evidence:

- `read_batches_present`
- `sequential_evidence_present`
- `current_read_evidence_present`
- `resource_samples_present` (expected sample count from actual duration and
  interval)
- `resource_coverage_present` (all three fixture targets represented with
  samples)
- `resource_sampling_not_aborted`
- `resource_sampling_no_errors`
- `writer_thread_stopped`
- `sampler_thread_stopped`
- `storage_journal_not_active`
- `environment_still_stopped`

`resource_summary` in the soak summary now also preserves
`resource_summary.errors`, not only sample count/coverage/abort.

### 4. Exception and cleanup evidence

- On exception, existing `failures.jsonl` plus failed `summary.json` remain.
- The `finally` revoke path now appends a `finally-revoke-view` failure record
  if the view cannot be revoked.
- If writer or sampler threads fail to stop after join timeouts, a
  `finally-writer`/`finally-sampler` failure record is appended.
- No failure path writes a passing summary.

### 5. Scope/naming note

The soak still records internal reader latencies and a fresh/stale ratio but
asserts no latency SLO. It remains a 180-300 s bounded local soak, not a 24 h
durability claim.

## Self-test commands and results

```bash
cd /home/omg/文档/多兼容linux机
python3 evidence/unifier-v2-stage-f-2026-10-26/implementation/probe_soak.py --help
# rc=0

python3 evidence/unifier-v2-stage-f-2026-10-26/implementation/selftest.py
# rc=0
# {"status": "passed", "tests": 9}
```

`selftest.py` uses temporary directories only; it does not import the product,
start a container, open a view, or touch desktop state. It includes workload
hash checks, shell syntax, memory guard, cgroup/smaps parsing, atomic replace,
inotify host control and fixture manifest shape.

## Read-only audit: residual risks and exact lines

These are findings only. I did not edit these files. Line numbers refer to the
current files at review time; primary edits may shift them.

### `probe_sessions.py`

1. **`same_cid_each_wave` is weaker than its name** — line 781:
   ```python
   "same_cid_each_wave": all(item["container_id"] for item in waves)
   ```
   This only checks non-empty CIDs. The per-wave `during_state`/session rows
   and `container_create_delta` checks do constrain the CID, but this check
   does not itself prove all waves used one CID.

2. **Empty timing lists are vacuously true** — lines 805-807:
   `timings_exit_zero` uses `all(...)` over `target[kind]`. With
   `--timing-reps 0` (or an implementation regression yielding empty lists),
   it returns true and `cold_seconds`/`warm_seconds` are `None` but unchecked.
   The current formal invocation uses `--timing-reps 30`, but the tool should
   assert non-empty lists and expected counts.

3. **Resource coverage is not checked in sessions** — lines 811 and 828-831:
   only `sampler.abort_event` is asserted. `sample_count`, per-target
   `coverage`, guest PSS coverage and sampling errors are reported but can be
   zero/UNKNOWN while the sessions check still passes.

4. **`guest_state is None` accepted as potentially live** — lines 130-138:
   `running_session_rows` accepts `guest_state in {"alive", None}`. Presence
   of `guest_pid`/`guest_start` is also required, but `None` is not positive
   liveness evidence. This is a narrow readiness-window risk.

5. **Metric label mismatch** — line 380:
   `first_exit_after_ready_seconds` is now measured from the `go` release,
   not from the readiness timestamp. The value is useful; the name is
   misleading.

6. **Unused parameter** — line 192: `run_wave(..., baseline_ids: set)` is not
   used inside the function. No behavioral effect.

7. **N=1 early-stop check is explicitly not applicable** — lines 790-795:
   for `level == 1`, the check short-circuits true. This is documented intent,
   not a false N>1 pass.

8. **`unresolved_for` is conservative** — lines 175-186: only `exited` and
   `retired` count as terminal; a resolved `failed` session is refused. This
   avoids a false pass.

9. **CID create/delete within one wave** — `ids_before`/`ids_after` around a
   wave (around lines 205 and 345) would not detect a container created and
   deleted entirely inside that window. The product path does not do this, but
   it is an ID-set, not event-log, check.

### `probe_projection.py`

1. **Host inotify control can be satisfied by an external modification** —
   lines 392 and 406-411: the watcher is on the host `dataset` directory and
   the loop breaks on any host event. `host_inotify_control_observed` line 429
   is `bool(host_events)`. There is no event-name filter ensuring the event is
   this probe's `notify.txt`. The events are recorded (lines 418-427), so a
   reviewer can inspect, but the check itself is vulnerable to a concurrent
   external writer in the authorized bind.

2. **FUSE inotify no-event is honestly non-success** — lines 405/416-431:
   `NO_EVENT` and `UNSUPPORTED` are accepted as a semantic limitation, not as
   a supported result. That is correct; do not convert it to a pass.

3. **Open-FD replacement is recorded but not asserted** — lines 350-377:
   `SAME_FD_READ_NEW_BYTES` versus `OLD_INODE_PRESERVED` is reported under
   `open_fd_across_replace`; it is not a pass/fail check. This is the correct
   treatment of the observed product projection FD semantic difference, as
   long as acceptance reads the field rather than assuming POSIX semantics.

4. **Cleanup excludes the journal field from the aggregate check** — lines
   666-668 and 687-692 exclude `storage_journal_state`; `verify_view_gone`
   sets `registry_empty` false only for active journal states. An
   `UNREADABLE` journal can therefore avoid the aggregate check. The raw
   `cleanup.storage_journal_state` remains in evidence.

5. **Writable mmap accepted but not written** — lines 301-307: if a writable
   mapping is accepted, no write is attempted and the note says so. The
   separate `write_open_refused` check normally fails earlier if O_RDWR was
   accepted; inspect the raw fields if this branch is reached.

6. **`wrapper_wall_ns` is recorded but not used for the per-phase timing** —
   lines 522/562/652. The meaningful measurements are the internal
   `stat_ns/read_ns/seq_ns`; wrapper time includes process/exec overhead. This
   is a reporting distinction, not a false pass.

### `resource_sampler.py`

1. **RSS/thread/FD sums silently use only PSS-readable processes** — lines
   506-508 (guest) and 651-654 (host):
   ```python
   "rss_partial_bytes": sum(row.get("rss_bytes") or 0 for row in known),
   "threads": sum(row.get("threads") or 0 for row in known),
   "fds": sum(row.get("fds") or 0 for row in known),
   ```
   `known` means PSS was readable. Processes with denied/gone PSS are omitted
   from RSS, thread and FD totals, but the field names do not say partial and
   coverage only tracks PSS/cgroup (lines 721-737). This can under-report
   resource coverage while `pss_unknown_count` looks like only a PSS issue.

2. **Coverage has no RSS/FD/thread completeness or expected-process delta** —
   lines 721-737 track samples/running/cgroup/guest PSS only. A report can
   show complete PSS coverage but incomplete FD/thread totals.

3. **Guest PSS fallback is bounded to three attempts per env per sample** —
   lines 255-257. This prevents expensive `podman top` storms, but more than
   three denied processes remain UNKNOWN with only
   `fallback_attempts`/`unknown_pss_count`; the summary does not identify
   unattempted PIDs.

4. **5 s inspect cache can outlive PID reuse** — lines 399-409: refresh is
   time/PID-existence based, not start-time based. A recycled PID within the
   5 s window could be associated with a stale container inspect record.
   Low probability; PID+start identity is still used for process rows.

5. **Standalone sampler has no minimum sample-count check** — `main` samples
   for `--seconds` but status is based on abort/errors; a zero/very-short
   series is not structurally rejected there. `probe_soak` now checks sample
   count/coverage, but standalone output should be read with `sample_count`.

6. **PSS and cgroup are correctly kept separate** — lines 659-662/768-772;
   no additive double counting was found.

### `cleanup_fixtures.py`

1. **Named volumes outside the manifest are not checked as leftovers** —
   lines 174-179 collect container volumes, but lines 186-199 remove only
   `fixtures.get("volumes")`. After container removal, `after` (lines 201-206)
   compares only container IDs, and `leftovers` (lines 207-250) does not check
   volume inventory. Stage F prepare creates no named volumes, but a foreign
   or future volume could survive while cleanup reports pass.

2. **UNREADABLE storage journal is not added to leftovers** — lines 233-242
   add only mounted/mounting/releasing/release-failed/needs-reconciliation.
   `UNREADABLE` remains evidence in the inventory but does not fail cleanup.

3. **Preflight is appropriately conservative** — lines 105-123 refuse any
   session whose effective state is not `exited`/`retired`, any live worker,
   unreadable lease state, or remaining live lease. No blind cleanup of
   unresolved states.

4. **View/mount/process proof is real** — lines 222-250 check captured view
   mounts, worker/broker/launcher identities, active journals and command-line
   references after environment removal.

5. **Views are revoked before container removal** — lines 157-165; remaining
   view rows abort cleanup. Product lifecycle `service.remove` is used; no
   `podman system prune`.

## Acceptance guidance

- Do not infer a Stage F pass from this review or from tool code. The primary
  formal runs are the evidence.
- For `probe_sessions`, inspect `resources-summary.json` `coverage` even if
  the sessions status is passed; the sessions check only uses the sampler
  abort flag.
- For `probe_projection`, inspect `semantics.inotify.host_events` names/masks,
  `open_fd_across_replace`, and `cleanup.storage_journal_state`; do not treat
  NO_EVENT as FUSE support.
- For `probe_soak`, the corrected output must show distinct
  `host_watch_path`/`fuse_watch_path`, `host_writer_event_count > 0`,
  `host_external_event_names == []` for a clean isolated run, and non-zero
  resource samples/coverage.
- For cleanup, inspect `removed`/`leftovers` and independently list volumes;
  do not treat a passed cleanup as proof that no named volume survived until
  the volume inventory is checked.
- Cross-host work remains pending; no local run here claims Debian/Ubuntu or
  Arch host behavior.

`implement-handoff.md` remains unchanged.
