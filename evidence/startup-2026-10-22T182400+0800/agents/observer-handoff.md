# Startup observer handoff

Status: implemented and smoke-tested in the live KWin 6.7.5 session.
Implementation: `tools/startup_observer.py` (sha256
`42743d0bc48f74dc80bfa910c7fc2f35d81a93d0bbcff377892d47c1a3d08461`).
No existing project file was modified. No real application window was
created, activated, minimised, focused, or closed.

## Exact interface

```python
class KWinStartupObserver(directory: Path, marker_prefix: str):
    events: list[dict]
    def pump(self) -> None: ...
    def wait_for(self, marker: str, event: str = "added", timeout: float = 20) -> dict: ...
    def close_window(self, marker: str, window_id: str, timeout: float = 5) -> dict: ...
    def close(self) -> None: ...
    def __enter__(self) -> "KWinStartupObserver": ...
    def __exit__(self, exc_type, exc, tb) -> bool: ...
```

Construction:

- Creates `directory` if needed.
- Rejects an empty/non-string `marker_prefix` with `ValueError`.
- Imports `gi` lazily. Missing PyGObject raises `ImportError` with an explicit
  message; importing this module does not require PyGObject.
- Loads one transient observer script with a unique plugin name and private
  D-Bus name, then blocks in the default GLib context until that script sends
  an explicit `ready` ack (`READY_TIMEOUT = 10.0` seconds, otherwise
  `TimeoutError`).
- On any construction failure it unloads anything already loaded and releases
  D-Bus resources before re-raising.

`pump()` is non-blocking and drains currently pending GLib/D-Bus work.
`wait_for()` pumps while waiting. Existing events are eligible, so an event
that arrived during the launch call is not lost. It returns the first
chronological matching event and raises built-in `TimeoutError` on expiry.
Matching is a case-sensitive substring test against the stored `caption`, not
a regex.

`close_window()` writes and runs a separate short-lived KWin script. That
script searches `workspace.windowList()` for exactly one window where both
`caption.contains(marker)` and `String(internalId) == window_id`; only then
does it call `closeWindow()` and report `close_requested`. Zero matches,
more than one match, or a `closeWindow()` exception produce an internal
`close_error`; Python raises `RuntimeError`. A missing callback raises
`TimeoutError`. It never searches by pid, class, focus, or activation state.
The short close script is unloaded immediately in a `finally` block.

`close()` is idempotent: it unloads every still-owned KWin script, unregisters
the private object, and releases the private name. Normal smoke-test cleanup
was verified with `isScriptLoaded == False` and `NameHasOwner == False`.
Teardown I/O errors are suppressed so cleanup cannot mask a measurement
exception; use the context manager unless you need explicit close-control.

## Event dictionary

Every stored event has exactly these normalized keys:

```text
event              "ready" | "added" | "removed" | "close_requested"
marker             the observer's marker_prefix
caption            window caption, or None for "ready"
window_id          String(window.internalId), or None for "ready"
type               String(window), or None for "ready"
objectType         same value as type (compatibility alias)
pid                window.pid integer, or None
resource_class     String(window.resourceClass), or None
geometry           {"width": int|None, "height": int|None}  # frameGeometry
buffer_geometry    {"width": int|None, "height": int|None}  # bufferGeometry
ready_for_painting True/False only if KWin exposes a boolean; else None
received_ns        host time.monotonic_ns() when Python ingested the callback
sent_wall_ms       JS Date.now() in the KWin script; wall-clock milliseconds
```

`windowAdded`/`windowRemoved` are the only global signals connected. Both emit
events only for captions containing `marker_prefix`; a removal is also
accepted when its window id was previously seen as a matching `added` window.
The callback never emits an unrelated window's caption or pid.

`ready` is the first event in the normal construction path. Its
`ready_for_painting` is None and its geometry has null dimensions.
Pre-existing matching windows are NOT replayed as `added` events: the observer
is intended to be constructed before the application launch. A later removal
of such a pre-existing window is reported only if its caption still matches.

## Timing and semantic limits

- `received_ns` is host monotonic time at Python callback ingestion. Compute
  durations with deltas of `received_ns`, not with `sent_wall_ms`.
- `sent_wall_ms` is the KWin script's `Date.now()`; it is wall-clock
  milliseconds and is suitable for cross-checking only.
- `windowAdded` is a compositor-level event. It is NOT evidence that the
  client buffer was attached/committed or that pixels were physically
  presented. Pair it with the parent's actual `WAYLAND_DEBUG` non-null
  attach/commit evidence before claiming presentation.
- `readyForPainting` is not documented in the KWin 6.7.5 official API and is
  expected to be absent. It was None in the live smoke test. The code records
  a boolean only when the property is actually a boolean and never assumes
  true; do not substitute it for captured-buffer evidence.
- A `removed` event is not a presentation or frame-completion event. Geometry
  may be null or stale during removal.
- `close_requested` means "the short close script invoked `closeWindow()` on
  the one verified match". It is a request, not proof of client destruction
  or compositor removal. The observer does not wait for `removed` inside
  `close_window()`; the parent must time removal separately. Removal and
  `close_requested` can arrive in either order, so read the existing `events`
  list after the call rather than assuming callback order. A window that
  closes itself without `close_window()` does not emit `close_requested`.
- The observer is single-threaded around the default GLib main context; do
  not call it concurrently from multiple threads.
- It talks to the `org.kde.KWin` instance on the caller's session bus. Run it
  in an environment where that bus and KWin are visible; it is not a probe for
  a different/nested compositor with its own private bus.
- Matching is case-sensitive and substring-based. The prefix must be unique
  enough not to occur in unrelated captions.
- The close path is safest when `window_id` came from an observed event. It
  will not touch a window unless both that exact internal id and the marker
  substring match, but a caller that deliberately supplies a broad marker and
  a foreign id is outside the intended use.
- Nothing is installed persistently or reconfigured. Each KWin script is
  transient and loaded through `/Scripting`; the private D-Bus name is random
  per instance and released on close. Only generated `startup-*.js` files are
  written under the supplied directory (observer and short close sources).

## Smoke test performed

Used a unique synthetic prefix (`dsh-observer-smoke-<uuid>`) that matched no
existing caption. No window was created or closed.

- Constructor returned after one `ready` event; `ready_for_painting` was None;
  geometry fields were null; `received_ns` and `sent_wall_ms` were integers.
- `close_window(prefix, "definitely-not-a-real-window-id", timeout=2)` failed
  with `RuntimeError: ... expected exactly one matching window, got 0`; no
  `close_requested` was stored and the short script was unloaded.
- `close()` called twice left `_owned_plugins` empty; both the observer plugin
  and the short close plugin reported `isScriptLoaded == False`, and the
  private D-Bus name reported `NameHasOwner == False`.
- The generated JS was also run under Node with mocked `workspace`/`callDBus`:
  a matching add and remove were emitted, an unrelated window's caption/pid
  was not, and the close success path called `closeWindow()` exactly once and
  emitted one `close_requested` for the selected internal id.

## Parent integration notes

Suggested sequence (parent owns launch and all measurements):

```python
with KWinStartupObserver(run_dir, marker_prefix) as observer:
    launch_application()
    added = observer.wait_for(marker, "added", timeout=20)
    observer.close_window(marker, added["window_id"], timeout=5)
    removed = observer.wait_for(marker, "removed", timeout=20)
```

`close_window()` already returns the `close_requested` record, so a separate
`wait_for(..., "close_requested")` is unnecessary and could return an earlier
historical close event for the same marker if the observer is reused.
For repeated waits on the same marker, correlate by `window_id` and keep your
own index/last-seen position in `observer.events`.
