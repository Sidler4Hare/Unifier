# Startup trace wrapper handoff

Status: implemented and help-smoke-tested. No product code, peer files,
containers, environments, windows, settings, or packages were touched.

- `tools/trace_startup_entry.py` (170 lines)
- sha256 `e0c7174780cd0c43d2266f88c140fccd5c6430d9a3337df312abbc5b922bd683`

## Invocation

```bash
python3 tools/trace_startup_entry.py --project ABS_PROJECT --output ABS_JSON \
  app open APP_ID -- NOTE
```

The leading wrapper options are consumed; every remaining argument is passed
unchanged as a list to `multilinux.cli.main(product_args)`, including `--`.
Both paths must be absolute. `--project=...` and `--output=...` are also
accepted. Plain `trace_startup_entry.py --help` prints wrapper usage and exits
0; `--help` after `--project/--output` is passed to the product CLI.

Before importing the product, the wrapper prepends `PROJECT/src` and
`PROJECT` to `sys.path`, enables `sys.dont_write_bytecode` to avoid writing
bytecode into the frozen snapshot, and replaces `subprocess.run` with a timing
wrapper. The imported `multilinux.cli.__file__` must resolve under
`PROJECT`; otherwise the run fails and the metadata records
`RuntimeError`. Product stdout/stderr are inherited, never captured or
redirected.

## Metadata JSON

Written atomically with a temporary file plus `os.replace` in the output
directory. It contains only timing and identifier metadata:

```json
{
  "schema_version": 1,
  "started_ns": 0,
  "finished_ns": 0,
  "elapsed_ms": 0.0,
  "returncode": 0,
  "exception": null,
  "project_root": "/abs/project",
  "product_cli_file": "/abs/project/src/multilinux/cli.py",
  "product_import_started_ns": 0,
  "product_import_finished_ns": 0,
  "product_import_ms": 0.0,
  "wrapper_file": "/abs/tools/trace_startup_entry.py",
  "wrapper_sha256": "...",
  "podman_call_count": 0,
  "podman_total_ms": 0.0,
  "podman_spans": [
    {"action": "info", "subaction": null,
     "started_ns": 0, "finished_ns": 0,
     "duration_ms": 0.0, "returncode": 0}
  ]
}
```

`action` is the top-level Podman subcommand (`info`, `create`, `start`,
`inspect`, `cp`, `exec`, `rm`, ...). `subaction` is the nested verb for group
commands (`container inspect` -> `container`/`inspect`, `image inspect` ->
`image`/`inspect`); unknown top-level commands become `other`, and unknown
nested verbs become `null`. No argv, argv elements, environment, credentials,
Podman output, application output, or unrelated process data is stored.

## Timing semantics

- `started_ns` and `finished_ns` are `time.monotonic_ns()` at wrapper `main()`
  entry/exit; `elapsed_ms` is their delta. Python interpreter start, wrapper
  imports, and the final metadata file write are excluded.
- `product_import_started_ns`/`product_import_finished_ns` bracket only
  `import multilinux.cli`; `product_import_ms` is their delta.
- Each Podman span starts immediately before the real saved
  `subprocess.run` and finishes after it returns or raises. It includes
  process launch and wait. This captures `tools.lab.require_rootless` ->
  `podman info` and the runtime methods that call `subprocess.run`.
- A span's `returncode` is null if the real `subprocess.run` raised before
  returning a completed process (for example `TimeoutExpired`); for
  `CalledProcessError` the exception's returncode is recorded.
- `podman_total_ms` is the sum of span durations. Concurrent Podman calls, if
  any, can make this sum exceed wall elapsed time.
- `exception` is the exception type name only. The message is deliberately
  omitted because product exceptions can contain app output. `returncode` is
  null when the product never returned normally; a normal `SystemExit(0)`
  such as `--help` records returncode 0 and exception null.

## Limits

- Only direct `subprocess.run` calls whose executable basename is exactly
  `podman` are observed. `Popen`, shell strings, `podman-remote`, wrappers
  with another executable name, child processes that call Podman themselves,
  and calls made after `main()` returns are not captured.
- Global Podman options placed before the subcommand are not parsed
  specially; conventional subcommand-first invocations are expected.
- The wrapper itself adds instrumentation overhead. It is intended only for
  separate diagnostic runs, never mixed into the normal latency cohort.
- `close`/teardown is not requested by this wrapper; it only runs the product
  CLI and records timing.
- SIGKILL or any abrupt termination cannot run Python's `finally`: metadata
  may be missing or stale. SIGTERM is not caught either.
- If the metadata write fails on a normal product return, the wrapper prints
  a diagnostic and exits 3. If the product is already raising (including
  `SystemExit` such as `--help`), the original status/exception propagates
  after the write attempt.
- Wrapper usage/argument-validation errors (missing/non-absolute paths) exit
  2 before product import and without metadata.

## Smoke performed

- `python3 tools/trace_startup_entry.py --help` printed wrapper usage.
- `--project ROOT --output /tmp/... --help` and the same against the frozen
  snapshot under `evidence/startup-2026-10-22T182400+0800/project` both
  imported the correct in-project CLI, exited 0, and wrote metadata with
  `podman_call_count == 0` and `podman_spans == []`.
- `_parse` was checked to preserve
  `["app","open","ID","--","NOTE"]` exactly. Action parsing was checked for
  `podman info`, `podman container inspect`, `podman image inspect`,
  `podman rm`, and a non-Podman executable.
- No app, environment, container, GUI, settings, or package command was run.
  The product CLI was only invoked with `--help`.
