# GPU DMA-BUF roundtrip handoff

Status: implemented and verified on the host GPU. This agent added only these
exclusive implementation/test files and this handoff; no peer-owned files,
existing modules, config, containers, or git state were modified.

## Files

- `fixtures/dmabuf_roundtrip.c`
- `tests/test_dmabuf_roundtrip.py`

## What it does

The C fixture creates a private 127x61 (configurable) linear XRGB8888 GBM
buffer, CPU-fills generated colour bars through `gbm_bo_map`, exports the real
DMA-BUF fd with `gbm_bo_get_fd_for_plane`, and records the actual modifier,
stride, offset and plane count. It then creates an EGL/GLES context on the
same GBM device, queries
`EGL_EXT_image_dma_buf_import[_modifiers]`, imports the exported plane with
`eglCreateImageKHR`, binds it via `glEGLImageTargetTexture2DOES`, attaches it
to an FBO, and reads it back with `glReadPixels`. Every visible RGB pixel is
compared with the generated pattern (tolerance configurable, row orientation
checked).

The JSON summary explicitly reports `"zero_copy": false` because CPU readback
is used. No compositor/screen/audio/camera source is touched, no image file is
written, and stdout contains only one JSON object.

## Host validation

Run on 2026-10-22 against `/dev/dri/renderD128`:

- GL renderer: `Mesa Intel(R) Iris(R) Xe Graphics (TGL GT2)`
- EGL: 1.5, `EGL_EXT_image_dma_buf_import` and modifiers present
- DRM fourcc: XRGB8888 (`875713112`)
- EGL reports 65 DMA-BUF formats and 6 XRGB8888 modifiers (LINEAR, X_TILED,
  Y_TILED and three CCS variants; CCS plane counts reported as 2-3)
- Allocated modifier: `DRM_FORMAT_MOD_LINEAR` (`0x0000000000000000`)
- Plane 0: stride 508, offset 0, one plane; exported fd closed after import
- GPU import: EGLImage created, texture bound, FBO complete
- CPU readback: 7747 / 7747 visible RGB pixels matched, 0 mismatched,
  orientation `direct`, max channel delta 0, tolerance 2
- `resources_released: true`

The fixture deliberately uses `GBM_BO_USE_LINEAR` because the synthetic image
is initialized through a CPU mapping; writing a linear pattern into a tiled
layout would be invalid. Non-linear modifiers are queried and reported, but an
explicit request such as `--modifier 0x0100000000000002` returns
`status: "unsupported"`, exit 4, with no silent fallback to linear or CPU
success.

## Exact commands

Build with strict warnings:

```bash
cd /home/omg/文档/多兼容linux机
cc -std=c11 -O2 -Wall -Wextra -Werror \
  $(pkg-config --cflags gbm egl glesv2 libdrm) \
  fixtures/dmabuf_roundtrip.c -o /tmp/dmabuf_roundtrip \
  $(pkg-config --libs gbm egl glesv2 libdrm)
```

Run the synthetic GPU roundtrip:

```bash
/tmp/dmabuf_roundtrip --render-node /dev/dri/renderD128
```

Run only the new test (it compiles once into a temporary directory):

```bash
python3 -m unittest tests.test_dmabuf_roundtrip -v
```

Alternative discovery form:

```bash
python3 -m unittest discover -s tests -p 'test_dmabuf_roundtrip.py' -v
```

Without an accessible render node, the two GPU-dependent tests skip
explicitly; argument/device-error tests still run:

```bash
MULTILINUX_RENDER_NODE=/nonexistent/renderD0 \
  python3 -m unittest tests.test_dmabuf_roundtrip -v
```

## Exit codes

- `0`: GPU import and CPU readback succeeded and every visible pixel matched
- `1`: pixel mismatch
- `2`: invalid arguments
- `3`: render-node/device open failure
- `4`: explicit unsupported extension/format/modifier/allocation
- `5`: EGL/GL/readback failure

## Limitations

- Verification uses `glReadPixels`; this is not an end-to-end zero-copy path.
- Only `DRM_FORMAT_MOD_LINEAR` is initialized by this synthetic CPU-fill
  self-test. Non-linear modifiers are reported but explicitly rejected.
- No compositor/window capture is performed; the buffer is private synthetic
  colour bars.
