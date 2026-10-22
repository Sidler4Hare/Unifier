# ScreenCast portal handoff (2026-10-22)

Status: implemented and offline-validated. Branch `codex/screencast-protocol`.
Only the three assigned files plus this report were written.

## Changed files

- `src/multilinux/screencast.py` — reusable Gio/GLib portal client with lazy `gi` import, injectable transport, CreateSession → SelectSources → Start → OpenPipeWireRemote, v5 node IDs and v6 `pipewire-serial`.
- `tools/probe_screencast.py` — metadata-only probe/consumer driver with memfd/dmabuf/auto, `pass_fds`, timeouts and fresh remote FD per attempt.
- `tests/test_screencast.py` — 14 offline tests plus a real-Gio serialization test (skipped only if PyGObject is unavailable).

## Review defect fixed

`portal-review.md` reported real host `GioPortalTransport._vardict()` returning a
`GLib.Variant("a{sv}")` that was then nested in a Python tuple passed to
`GLib.Variant("(a{sv})", ...)`, causing `KeyError: 0` before dispatch.

Fix: all portal argument tuples now use `GLib.Variant.new_tuple(...)` with the
options variant as a distinct child. Real Gio verification produced:

- CreateSession `(a{sv})`
- SelectSources `(oa{sv})`
- Start `(osa{sv})`
- OpenPipeWireRemote `(oa{sv})`
- Properties.Get `(ss)`

## Validation performed

- `python3 -m unittest tests.test_screencast -v` → **14 tests, OK**.
  Covers early Response before handle return, actual-handle mismatch, denial,
  error Response, timeout with Request.Close, FD index vs OS fd, session/FD
  cleanup, probe auto fallback with two OpenPipeWireRemote calls, monitor source,
  parent window, child `pass_fds`, timeout kill/reap, lazy import without gi.
- Real host portal **CreateSession → Session.Close only** (no SelectSources, no
  selection dialog, no capture):
  `{"ok": true, "version": 5, "session_created": true,
  "request_handle_matched": true, "closed": true,
  "session_handle_after_close": null, "open_remote_fds": [], "warnings": []}`.
- `python3 tools/probe_screencast.py --help` exits 0 without importing `gi`.
- `python3 -m py_compile` passes for all three files.

## Behavior notes for integration

- Request responses are matched by the **exact handle returned by the portal**,
  not the token prediction. A broad Request.Response watch is installed before
  every portal call; the token prediction is only a diagnostic, so old portals
  and early responses work without a re-subscribe race.
- `persist_mode` is always 0 and no restore token is stored or reused.
- `Gio.UnixFDList` handle values are treated as indices; `.get(index)` returns
  the owned duplicate OS fd. The list's original descriptors are drained/closed.
- Probe metadata is mode 0600 JSON with phases, portal events/warnings, sources,
  every attempt's child NDJSON events/summary/return code, explicit errors,
  cleanup state and whitelisted pixel hashes/color counts. No image bytes are
  written by the probe.
- Auto mode tries dmabuf, records the failure, closes that remote FD, calls
  OpenPipeWireRemote again, then tries memfd.
- Consumer contract expected by probe success checks: `summary.passed == true`,
  `frames >= requested`, `mode`/`buffer_type` matching the attempt (`MemFd` or
  `DmaBuf`), `cpu_readback == true`, `gpu_import == true` for dmabuf, plus pixel
  hashes and color counts.

## Limitations / pending live checks

- Host portal reports version **5**; the v6 `pipewire-serial` path is covered by
  injected tests (fake v6 stream), not by a live v6 portal.
- Per constraints, no live screen selection, OpenPipeWireRemote, PipeWire
  consumer, or dmabuf/EGL import was exercised by this sub-agent. The parent
  must compile `fixtures/screencast_consumer.c` and run live selection.
- `xdg-dbus-proxy` policy was not changed. Existing
  `--talk=org.freedesktop.portal.Desktop` is assumed to cover Request/Session
  objects; this needs live proof with the proxy.
- The Gio session-bus connection is the shared singleton and is not force-closed
  (only explicit `--bus-address` connections are closed); all subscriptions and
  portal-owned FDs are released by `ScreenCastClient.close()`.
