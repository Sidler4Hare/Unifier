# Native consumer handoff (frozen after protocol repair)

## Changed files

- `fixtures/screencast_consumer.c` — bounded native PipeWire video consumer.
- `tests/test_screencast_native.py` — strict offline build/self-test/argument tests.
- This handoff file only.

No README, other modules, or parent files were edited.

## Implementation summary

- Connects only through the inherited portal FD:
  `pw_context_connect_fd()` + `pw_stream_new()`; no default-socket connect.
  Without `--target-serial`, the portal node ID is passed as the legacy
  `target_id`; with it, `PW_KEY_TARGET_OBJECT` is set and `target_id` is
  `PW_ID_ANY`.
- CLI: `--fd FD --node UINT32 [--target-serial STRING] --mode memfd|dmabuf
  --frames N --timeout-ms MS [--render-node PATH] [--expect-pattern]`,
  plus `--self-test`.
- NDJSON only: `format`, `frame`, `error`, `summary`. Pixel bytes are never
  emitted; output contains hashes, colour counts, dimensions/layout and
  buffer/import status only.
- MemFd CPU path validates offset/size/stride/chunk bounds and page-aligned
  mapoffset, ignores row padding, supports BGRx/BGRA/RGBx/RGBA, and reports
  the actual `MemFd` (also accepts explicit `MemPtr`) buffer type. Bounds
  checks are retained for CPU buffers.
- DMA-BUF path uses a headless EGL/GLES context on the requested GBM render
  node, imports each single-plane buffer with
  `EGL_EXT_image_dma_buf_import`, binds it to a texture/FBO, and validates
  real pixels with `glReadPixels`. `gpu_import=true`,
  `cpu_readback=true`, `zero_copy=false` are reported distinctly.
- Buffers are always requeued, including all error paths. Cleanup order is
  stream, core, context, loop, EGL, GBM, inherited FD.

## Protocol repairs applied for the verified PipeWire 1.4 text

1. **DMA-BUF sizes are ignored.** Removed `data.maxsize`/`chunk.size`
   rejection and linear visible-row size arithmetic from the EGL import.
   DMA-BUFs are no longer skipped when `chunk.size == 0`. Still validated:
   one plane (`n_datas == 1`), valid FD, positive pitch/stride, offset
   representable as `EGLint`, supported modifier, and actual EGL import/FBO
   completion. MemFd bounds checks remain unchanged.
2. **Modifier property flags/choice.** Advertised modifier now uses
   `SPA_POD_PROP_FLAG_MANDATORY | SPA_POD_PROP_FLAG_DONT_FIXATE`; the first
   modifier is written once as the Enum default and again in the allowed
   values.
3. **External-only / multi-plane filtering.** `eglQueryDmaBufModifiersEXT`
   now queries `external_only` and drops external-only modifiers. Each
   remaining modifier is conservatively probed with a small private GBM
   allocation (`256x256`) and advertised only when
   `gbm_bo_get_plane_count() == 1`. Formats with no usable modifier are not
   advertised; multi-plane buffers still fail explicitly. Linear is preferred
   as the Enum default when available.
4. **Summary dimensions.** Final summary now includes actual `width`/`height`
   (or `null` before format negotiation).

## Validations

- Strict build:
  `cc -std=gnu11 -O2 -Wall -Wextra -Werror -Wshadow -Wformat=2 ...
  $(pkg-config --cflags --libs libpipewire-0.3 libdrm gbm egl glesv2)`
  — clean.
- Assigned tests, run once after repairs:
  `python3 -m unittest tests.test_screencast_native -v`
  — **9 tests, OK**.
- `--self-test` — **23/23 checks pass**, using real
  `memfd_create`/`mmap` synthetic buffers (format interpretation, chunk
  offset/stride, padding/alpha ignoring, truncated/invalid layout rejection,
  hash change, pattern counts).
- DMA-BUF query smoke check on `/dev/dri/renderD128` after the modifier
  filter: EGL init/format+modifier probe completed and reached the expected
  invalid-FD connection error; no stderr noise.
- Earlier pre-repair synthetic private-bus validation (recorded, not rerun):
  MemFd and single-plane linear DMA-BUF both produced identical pixel hashes
  for BGRx and passed expected-pattern checks; DMA-BUF reported
  `gpu_import=true`, `cpu_readback=true`, `zero_copy=false`.
- Parent real Portal MemFd run: 60 BGRx frames, expected colours, 57 hash
  changes, cleanup passed.

## Limits / explicit failures

- Supported pixel layouts are only packed BGRx/BGRA/RGBx/RGBA. Other formats
  fail with `unsupported_format`; unsupported buffer types/layouts fail
  explicitly.
- DMA-BUF import is single-plane only. Multi-plane formats are rejected, not
  silently treated as supported. Advertised modifier list is conservative:
  external-only or non-single-plane-GBM combinations are omitted.
- DMA-BUF validation necessarily includes a CPU `glReadPixels` readback, so
  this is intentionally not claimed as zero-copy.
- No compositor/portal capture is performed by the consumer. The Python
  caller owns portal selection, fresh `OpenPipeWireRemote` FD per attempt,
  and dmabuf→memfd fallback.
- Live GPU portal capture after this repair is left to the parent's isolated
  environment. Exit codes: 0 success, 2 usage, 3 connect/setup, 4 timeout/
  insufficient frames/pattern failure, 5 unsupported format/layout/import.
