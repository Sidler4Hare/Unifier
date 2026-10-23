/*
 * dmabuf_roundtrip.c - private synthetic GBM -> EGL -> GLES DMA-BUF round trip.
 *
 * This fixture does not capture a compositor, a screen, or any camera/audio
 * source.  It allocates its own linear XRGB8888 GBM buffer, fills generated
 * colour bars through the CPU mapping, exports the real DMA-BUF plane fd,
 * imports that fd as an EGLImage, attaches it to an FBO, reads it back with
 * glReadPixels and compares every visible RGB pixel with the generated bars.
 *
 * CPU readback is used for verification.  This is therefore NOT an
 * end-to-end zero-copy path and the JSON summary says so explicitly.
 *
 * Build:
 *   cc -std=c11 -O2 -Wall -Wextra -Werror \
 *      $(pkg-config --cflags gbm egl glesv2 libdrm) \
 *      fixtures/dmabuf_roundtrip.c -o dmabuf_roundtrip \
 *      $(pkg-config --libs gbm egl glesv2 libdrm)
 */

#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#include <gbm.h>
#include <drm_fourcc.h>

#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>

#define SCHEMA "multilinux.dmabuf-roundtrip.v1"
#define MAX_PLANES 4
#define MAX_CANDIDATES 64
#define DEFAULT_RENDER_NODE "/dev/dri/renderD128"
#define DEFAULT_WIDTH 127
#define DEFAULT_HEIGHT 61
#define DEFAULT_TOLERANCE 2

struct mod_candidate {
    uint64_t modifier;
    bool external_only;
    int gbm_plane_count;
};

struct plane_report {
    int fd;
    bool fd_open;
    uint32_t offset;
    uint32_t stride;
};

struct report {
    bool passed;
    const char *status;
    const char *stage;
    char error[1024];

    char render_node[PATH_MAX];
    char gbm_backend[128];
    int drm_fd;

    char gl_vendor[256];
    char gl_renderer[256];
    char gl_version[256];
    char egl_version[128];
    char egl_platform[128];

    bool ext_import;
    bool ext_import_modifiers;
    bool ext_surfaceless;
    bool xrgb8888_supported;
    bool format_query_performed;
    int egl_format_count;
    int egl_modifier_count;
    int candidate_count;
    struct mod_candidate candidates[MAX_CANDIDATES];

    int fourcc;
    int width;
    int height;
    int tolerance;
    int gbm_width;
    int gbm_height;
    int gbm_stride;
    int gbm_offset;
    int gbm_plane_count;
    uint64_t modifier;

    bool modifier_attrs_used;
    bool image_created;
    bool texture_created;
    bool fbo_complete;
    bool gpu_import;
    bool readback;
    bool resources_released;

    int plane_count;
    struct plane_report planes[MAX_PLANES];

    int total_pixels;
    int matching_pixels;
    int mismatched_pixels;
    int direct_matching_pixels;
    int flipped_matching_pixels;
    int max_channel_delta;
    bool orientation_flipped;
    int first_x;
    int first_y;
    int first_expected[3];
    int first_actual[3];
    uint64_t rgb_hash;
};

struct resources {
    int dev_fd;
    struct gbm_device *gbm;
    struct gbm_bo *bo;
    void *map;
    void *map_data;
    EGLDisplay dpy;
    EGLContext ctx;
    EGLSurface surface;
    EGLImageKHR image;
    bool egl_initialized;
    GLuint tex;
    GLuint fbo;
    PFNEGLDESTROYIMAGEKHRPROC destroy_image;
    int plane_fds[MAX_PLANES];
    int plane_fd_count;
    uint8_t *expected;
    uint8_t *actual;
};

struct options {
    const char *render_node;
    int width;
    int height;
    int tolerance;
    bool modifier_forced;
    uint64_t modifier;
};

static void copy_text(char *dst, size_t cap, const char *src)
{
    size_t n;

    if (dst == NULL || cap == 0)
        return;
    if (src == NULL)
        src = "";
    n = strlen(src);
    if (n >= cap)
        n = cap - 1;
    memcpy(dst, src, n);
    dst[n] = '\0';
}

static void json_string(FILE *out, const char *text)
{
    const unsigned char *p;

    fputc('"', out);
    if (text != NULL) {
        for (p = (const unsigned char *)text; *p != '\0'; p++) {
            switch (*p) {
            case '"':
                fputs("\\\"", out);
                break;
            case '\\':
                fputs("\\\\", out);
                break;
            case '\n':
                fputs("\\n", out);
                break;
            case '\r':
                fputs("\\r", out);
                break;
            case '\t':
                fputs("\\t", out);
                break;
            default:
                if (*p < 0x20)
                    fprintf(out, "\\u%04x", (unsigned int)*p);
                else
                    fputc((int)*p, out);
                break;
            }
        }
    }
    fputc('"', out);
}

static const char *modifier_name(uint64_t modifier)
{
    if (modifier == DRM_FORMAT_MOD_LINEAR)
        return "DRM_FORMAT_MOD_LINEAR";
#ifdef I915_FORMAT_MOD_X_TILED
    if (modifier == I915_FORMAT_MOD_X_TILED)
        return "I915_FORMAT_MOD_X_TILED";
#endif
#ifdef I915_FORMAT_MOD_Y_TILED
    if (modifier == I915_FORMAT_MOD_Y_TILED)
        return "I915_FORMAT_MOD_Y_TILED";
#endif
#ifdef I915_FORMAT_MOD_Yf_TILED
    if (modifier == I915_FORMAT_MOD_Yf_TILED)
        return "I915_FORMAT_MOD_Yf_TILED";
#endif
#ifdef I915_FORMAT_MOD_Y_TILED_CCS
    if (modifier == I915_FORMAT_MOD_Y_TILED_CCS)
        return "I915_FORMAT_MOD_Y_TILED_CCS";
#endif
#ifdef I915_FORMAT_MOD_4_TILED
    if (modifier == I915_FORMAT_MOD_4_TILED)
        return "I915_FORMAT_MOD_4_TILED";
#endif
    return NULL;
}

static void print_modifier_value(FILE *out, uint64_t modifier)
{
    const char *name = modifier_name(modifier);

    if (name != NULL)
        json_string(out, name);
    else
        fprintf(out, "\"0x%016" PRIx64 "\"", modifier);
}

static void print_modifier_number(FILE *out, uint64_t modifier)
{
    fprintf(out, "\"0x%016" PRIx64 "\"", modifier);
}

static int emit_report(const struct report *r)
{
    int i;

    fputc('{', stdout);
    fputs("\"schema\":\"" SCHEMA "\",", stdout);
    fprintf(stdout, "\"passed\":%s,", r->passed ? "true" : "false");
    fputs("\"status\":", stdout);
    json_string(stdout, r->status != NULL ? r->status : "error");
    if (r->stage != NULL) {
        fputs(",\"stage\":", stdout);
        json_string(stdout, r->stage);
    }
    if (r->error[0] != '\0') {
        fputs(",\"error\":", stdout);
        json_string(stdout, r->error);
    }
    fputs(",\"render_node\":", stdout);
    json_string(stdout, r->render_node);

    fputs(",\"device\":{", stdout);
    fputs("\"gbm_backend\":", stdout);
    json_string(stdout, r->gbm_backend);
    fprintf(stdout, ",\"drm_fd\":%d", r->drm_fd);
    fprintf(stdout, ",\"gbm_width\":%d,\"gbm_height\":%d",
            r->gbm_width, r->gbm_height);
    fputc('}', stdout);

    if (r->gl_renderer[0] != '\0') {
        fputs(",\"gpu\":{", stdout);
        fputs("\"gl_vendor\":", stdout);
        json_string(stdout, r->gl_vendor);
        fputs(",\"gl_renderer\":", stdout);
        json_string(stdout, r->gl_renderer);
        fputs(",\"gl_version\":", stdout);
        json_string(stdout, r->gl_version);
        fputs(",\"egl_version\":", stdout);
        json_string(stdout, r->egl_version);
        fputs(",\"egl_platform\":", stdout);
        json_string(stdout, r->egl_platform);
        fputc('}', stdout);
    }

    fputs(",\"egl\":{", stdout);
    fprintf(stdout, "\"image_dma_buf_import\":%s",
            r->ext_import ? "true" : "false");
    fprintf(stdout, ",\"image_dma_buf_import_modifiers\":%s",
            r->ext_import_modifiers ? "true" : "false");
    fprintf(stdout, ",\"surfaceless_context\":%s",
            r->ext_surfaceless ? "true" : "false");
    fprintf(stdout, ",\"dma_buf_format_count\":%d", r->egl_format_count);
    fprintf(stdout, ",\"dma_buf_modifier_count\":%d",
            r->egl_modifier_count);
    fprintf(stdout, ",\"format_query_performed\":%s",
            r->format_query_performed ? "true" : "false");
    fprintf(stdout, ",\"xrgb8888_supported\":%s",
            r->xrgb8888_supported ? "true" : "false");
    fputs(",\"supported_modifiers\":[", stdout);
    for (i = 0; i < r->candidate_count; i++) {
        if (i != 0)
            fputc(',', stdout);
        fputc('{', stdout);
        fputs("\"modifier\":", stdout);
        print_modifier_value(stdout, r->candidates[i].modifier);
        fputs(",\"value\":", stdout);
        print_modifier_number(stdout, r->candidates[i].modifier);
        fprintf(stdout, ",\"external_only\":%s",
                r->candidates[i].external_only ? "true" : "false");
        fprintf(stdout, ",\"gbm_plane_count\":%d",
                r->candidates[i].gbm_plane_count);
        fputc('}', stdout);
    }
    fputs("]}", stdout);

    if (r->fourcc != 0) {
        fputs(",\"buffer\":{", stdout);
        fprintf(stdout, "\"format\":\"XRGB8888\",\"fourcc\":%d", r->fourcc);
        fprintf(stdout, ",\"width\":%d,\"height\":%d", r->width, r->height);
        fputs(",\"modifier\":", stdout);
        print_modifier_number(stdout, r->modifier);
        fputs(",\"modifier_name\":", stdout);
        print_modifier_value(stdout, r->modifier);
        fprintf(stdout, ",\"stride\":%d,\"offset\":%d,\"plane_count\":%d",
                r->gbm_stride, r->gbm_offset, r->gbm_plane_count);
        fputs(",\"planes\":[", stdout);
        for (i = 0; i < r->plane_count; i++) {
            if (i != 0)
                fputc(',', stdout);
            fprintf(stdout,
                    "{\"plane\":%d,\"fd\":%d,\"offset\":%u,\"stride\":%u,"
                    "\"fd_closed\":%s}",
                    i, r->planes[i].fd, (unsigned int)r->planes[i].offset,
                    (unsigned int)r->planes[i].stride,
                    r->planes[i].fd_open ? "false" : "true");
        }
        fputs("]}", stdout);
    }

    fputs(",\"import\":{", stdout);
    fprintf(stdout, "\"gpu_import_successful\":%s",
            r->gpu_import ? "true" : "false");
    fprintf(stdout, ",\"egl_image_created\":%s",
            r->image_created ? "true" : "false");
    fprintf(stdout, ",\"texture_created\":%s",
            r->texture_created ? "true" : "false");
    fprintf(stdout, ",\"fbo_complete\":%s",
            r->fbo_complete ? "true" : "false");
    fprintf(stdout, ",\"modifier_attributes_used\":%s",
            r->modifier_attrs_used ? "true" : "false");
    fputs(",\"method\":\"EGL_EXT_image_dma_buf_import + "
          "glEGLImageTargetTexture2DOES + FBO\"}", stdout);

    fputs(",\"readback\":{", stdout);
    fprintf(stdout, "\"cpu_readback_performed\":%s",
            r->readback ? "true" : "false");
    fprintf(stdout, ",\"width\":%d,\"height\":%d",
            r->width, r->height);
    fprintf(stdout, ",\"total_pixels\":%d", r->total_pixels);
    fprintf(stdout, ",\"matching_pixels\":%d", r->matching_pixels);
    fprintf(stdout, ",\"mismatched_pixels\":%d", r->mismatched_pixels);
    fprintf(stdout, ",\"direct_matching_pixels\":%d",
            r->direct_matching_pixels);
    fprintf(stdout, ",\"flipped_matching_pixels\":%d",
            r->flipped_matching_pixels);
    fprintf(stdout, ",\"tolerance\":%d", r->tolerance);
    fprintf(stdout, ",\"max_channel_delta\":%d", r->max_channel_delta);
    fputs(",\"orientation\":", stdout);
    json_string(stdout, r->orientation_flipped ? "y-flipped" : "direct");
    fprintf(stdout, ",\"rgb_fnv1a64\":\"0x%016" PRIx64 "\"", r->rgb_hash);
    if (r->mismatched_pixels > 0) {
        fprintf(stdout,
                ",\"first_mismatch\":{\"x\":%d,\"y\":%d,"
                "\"expected_rgb\":[%d,%d,%d],"
                "\"actual_rgb\":[%d,%d,%d]}",
                r->first_x, r->first_y,
                r->first_expected[0], r->first_expected[1],
                r->first_expected[2],
                r->first_actual[0], r->first_actual[1],
                r->first_actual[2]);
    }
    fputc('}', stdout);

    fprintf(stdout, ",\"zero_copy\":false,\"resources_released\":%s",
            r->resources_released ? "true" : "false");
    fputs(",\"notes\":[\"Private synthetic GBM buffer; no compositor "
          "capture and no image bytes saved.\",\"CPU glReadPixels readback "
          "was used for verification; this is not an end-to-end zero-copy "
          "path.\",\"The CPU-initialized buffer uses "
          "DRM_FORMAT_MOD_LINEAR; non-linear modifiers are reported but "
          "rejected explicitly.\"]", stdout);
    fputs("}\n", stdout);
    return 0;
}

static int set_error(struct report *r, int exit_code, const char *status,
                     const char *stage, const char *fmt, ...)
{
    va_list ap;

    r->passed = false;
    r->status = status;
    r->stage = stage;
    va_start(ap, fmt);
    vsnprintf(r->error, sizeof(r->error), fmt, ap);
    va_end(ap);
    return exit_code;
}

#define FAIL_NOW(rp, code, status, stage, ...)                          \
    do {                                                                \
        exit_code = set_error((rp), (code), (status), (stage),          \
                              __VA_ARGS__);                             \
        goto done;                                                      \
    } while (0)

static void cleanup(struct resources *res)
{
    int i;

    if (res->fbo != 0) {
        glBindFramebuffer(GL_FRAMEBUFFER, 0);
        glDeleteFramebuffers(1, &res->fbo);
        res->fbo = 0;
    }
    if (res->tex != 0) {
        glDeleteTextures(1, &res->tex);
        res->tex = 0;
    }
    if (res->image != EGL_NO_IMAGE_KHR && res->dpy != EGL_NO_DISPLAY &&
        res->destroy_image != NULL) {
        res->destroy_image(res->dpy, res->image);
        res->image = EGL_NO_IMAGE_KHR;
    }
    if (res->dpy != EGL_NO_DISPLAY) {
        eglMakeCurrent(res->dpy, EGL_NO_SURFACE, EGL_NO_SURFACE,
                       EGL_NO_CONTEXT);
        if (res->surface != EGL_NO_SURFACE)
            eglDestroySurface(res->dpy, res->surface);
        if (res->ctx != EGL_NO_CONTEXT)
            eglDestroyContext(res->dpy, res->ctx);
        if (res->egl_initialized)
            eglTerminate(res->dpy);
        eglReleaseThread();
    }
    if (res->map != NULL && res->bo != NULL) {
        gbm_bo_unmap(res->bo, res->map_data);
        res->map = NULL;
        res->map_data = NULL;
    }
    if (res->bo != NULL)
        gbm_bo_destroy(res->bo);
    if (res->gbm != NULL)
        gbm_device_destroy(res->gbm);
    res->bo = NULL;
    res->gbm = NULL;
    for (i = 0; i < res->plane_fd_count; i++) {
        if (res->plane_fds[i] >= 0) {
            close(res->plane_fds[i]);
            res->plane_fds[i] = -1;
        }
    }
    if (res->dev_fd >= 0)
        close(res->dev_fd);
    free(res->expected);
    free(res->actual);
    res->expected = NULL;
    res->actual = NULL;
}

static bool parse_int(const char *text, int min_value, int max_value, int *out)
{
    char *end = NULL;
    long value;

    if (text == NULL || *text == '\0')
        return false;
    errno = 0;
    value = strtol(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0')
        return false;
    if (value < min_value || value > max_value)
        return false;
    *out = (int)value;
    return true;
}

static bool parse_modifier(const char *text, bool *forced, uint64_t *out)
{
    char *end = NULL;
    unsigned long long value;

    if (strcmp(text, "auto") == 0) {
        *forced = false;
        *out = DRM_FORMAT_MOD_LINEAR;
        return true;
    }
    if (strcmp(text, "linear") == 0) {
        *forced = true;
        *out = DRM_FORMAT_MOD_LINEAR;
        return true;
    }
    errno = 0;
    value = strtoull(text, &end, 0);
    if (errno != 0 || end == text || *end != '\0')
        return false;
    *forced = true;
    *out = (uint64_t)value;
    return true;
}

static void pattern_rgb(int x, int y, int width, int height, uint8_t rgb[3])
{
    static const uint8_t bars[8][3] = {
        { 224, 32, 32 },   /* red */
        { 32, 192, 64 },   /* green */
        { 32, 64, 224 },   /* blue */
        { 224, 192, 32 },  /* yellow */
        { 32, 192, 224 },  /* cyan */
        { 192, 32, 224 },  /* magenta */
        { 240, 240, 240 }, /* white */
        { 16, 16, 16 }     /* near black */
    };
    int bar;
    int scale_num = 4;
    int i;

    if (x < 0)
        x = 0;
    if (x >= width)
        x = width - 1;
    bar = (x * 8) / width;
    if (bar < 0)
        bar = 0;
    if (bar > 7)
        bar = 7;

    /* Asymmetric horizontal zones make a row flip detectable. */
    if (y < height / 4)
        scale_num = 2;
    else if (y >= (height * 3) / 4)
        scale_num = 3;

    for (i = 0; i < 3; i++)
        rgb[i] = (uint8_t)(((int)bars[bar][i] * scale_num) / 4);
}

static int max3_delta(const uint8_t actual[4], const uint8_t expected[3])
{
    int max_delta = 0;
    int i;

    for (i = 0; i < 3; i++) {
        int delta = (int)actual[i] - (int)expected[i];
        if (delta < 0)
            delta = -delta;
        if (delta > max_delta)
            max_delta = delta;
    }
    return max_delta;
}

static void usage(FILE *out)
{
    fprintf(out,
            "usage: dmabuf_roundtrip [--render-node PATH] [--width N]\n"
            "       [--height N] [--tolerance N] [--modifier auto|linear|"
            "0xMOD]\n"
            "auto/linear select DRM_FORMAT_MOD_LINEAR (CPU-filled buffer).\n"
            "A private synthetic GBM -> EGL -> GLES DMA-BUF round trip.\n"
            "Prints one JSON summary on stdout.  Exit 0 only on a full "
            "pixel match.\n");
}

int main(int argc, char **argv)
{
    struct options opt;
    struct report rep;
    struct resources res;
    EGLint major = 0;
    EGLint minor = 0;
    EGLConfig config = NULL;
    EGLint config_count = 0;
    EGLint ctx_attribs[] = { EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE };
    const char *client_ext;
    const char *egl_ext;
    const char *gl_ext;
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display = NULL;
    PFNEGLCREATEIMAGEKHRPROC create_image = NULL;
    PFNEGLQUERYDMABUFFORMATSEXTPROC query_formats = NULL;
    PFNEGLQUERYDMABUFMODIFIERSEXTPROC query_modifiers = NULL;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC image_target_texture = NULL;
    EGLint *formats = NULL;
    EGLuint64KHR *modifiers = NULL;
    EGLBoolean *external_only = NULL;
    EGLint format_count = 0;
    EGLint modifier_count = 0;
    EGLint i;
    GLenum gl_error;
    GLenum fbo_status;
    uint8_t *map = NULL;
    uint32_t map_stride = 0;
    void *map_data = NULL;
    int exit_code = 0;
    int j;

    memset(&opt, 0, sizeof(opt));
    opt.render_node = DEFAULT_RENDER_NODE;
    opt.width = DEFAULT_WIDTH;
    opt.height = DEFAULT_HEIGHT;
    opt.tolerance = DEFAULT_TOLERANCE;
    opt.modifier_forced = false;
    opt.modifier = DRM_FORMAT_MOD_LINEAR;

    memset(&rep, 0, sizeof(rep));
    rep.drm_fd = -1;
    rep.status = "error";
    rep.stage = "args";
    rep.width = opt.width;
    rep.height = opt.height;
    rep.tolerance = opt.tolerance;
    rep.first_x = -1;
    rep.first_y = -1;
    copy_text(rep.render_node, sizeof(rep.render_node), opt.render_node);

    memset(&res, 0, sizeof(res));
    res.dev_fd = -1;
    res.dpy = EGL_NO_DISPLAY;
    res.ctx = EGL_NO_CONTEXT;
    res.surface = EGL_NO_SURFACE;
    res.image = EGL_NO_IMAGE_KHR;
    for (i = 0; i < MAX_PLANES; i++)
        res.plane_fds[i] = -1;

    for (i = 1; i < argc; i++) {
        if (strcmp(argv[i], "--render-node") == 0) {
            if (i + 1 >= argc)
                FAIL_NOW(&rep, 2, "error", "args",
                                  "missing value for --render-node");
            opt.render_node = argv[++i];
            copy_text(rep.render_node, sizeof(rep.render_node),
                      opt.render_node);
        } else if (strcmp(argv[i], "--width") == 0) {
            if (i + 1 >= argc)
                FAIL_NOW(&rep, 2, "error", "args",
                                  "missing value for --width");
            if (!parse_int(argv[++i], 16, 1024, &opt.width))
                FAIL_NOW(&rep, 2, "error", "args",
                                  "invalid --width '%s' (expected 16..1024)",
                                  argv[i]);
            rep.width = opt.width;
        } else if (strcmp(argv[i], "--height") == 0) {
            if (i + 1 >= argc)
                FAIL_NOW(&rep, 2, "error", "args",
                                  "missing value for --height");
            if (!parse_int(argv[++i], 16, 1024, &opt.height))
                FAIL_NOW(&rep, 2, "error", "args",
                                  "invalid --height '%s' (expected 16..1024)",
                                  argv[i]);
            rep.height = opt.height;
        } else if (strcmp(argv[i], "--tolerance") == 0) {
            if (i + 1 >= argc)
                FAIL_NOW(&rep, 2, "error", "args",
                                  "missing value for --tolerance");
            if (!parse_int(argv[++i], 0, 32, &opt.tolerance))
                FAIL_NOW(&rep, 2, "error", "args",
                                  "invalid --tolerance '%s' (expected 0..32)",
                                  argv[i]);
            rep.tolerance = opt.tolerance;
        } else if (strcmp(argv[i], "--modifier") == 0) {
            if (i + 1 >= argc)
                FAIL_NOW(&rep, 2, "error", "args",
                                  "missing value for --modifier");
            if (!parse_modifier(argv[++i], &opt.modifier_forced,
                                &opt.modifier))
                FAIL_NOW(&rep, 2, "error", "args",
                                  "invalid --modifier '%s' (auto, linear or "
                                  "0xMOD)", argv[i]);
        } else if (strcmp(argv[i], "--help") == 0) {
            usage(stderr);
            return 0;
        } else {
            FAIL_NOW(&rep, 2, "error", "args",
                              "unknown argument '%s'", argv[i]);
        }
    }

    /* Device phase: a private GBM device on the requested render node. */
    res.dev_fd = open(opt.render_node, O_RDWR | O_CLOEXEC);
    if (res.dev_fd < 0) {
        int saved_errno = errno;
        FAIL_NOW(&rep, 3, "error", "device",
                          "cannot open render node '%s': %s",
                          opt.render_node, strerror(saved_errno));
    }
    res.gbm = gbm_create_device(res.dev_fd);
    if (res.gbm == NULL)
        FAIL_NOW(&rep, 3, "error", "device",
                          "gbm_create_device failed for '%s'",
                          opt.render_node);
    res.dev_fd = -1; /* owned by the GBM device */
    copy_text(rep.gbm_backend, sizeof(rep.gbm_backend),
              gbm_device_get_backend_name(res.gbm));
    rep.drm_fd = gbm_device_get_fd(res.gbm);

    res.bo = gbm_bo_create(res.gbm, (uint32_t)opt.width,
                           (uint32_t)opt.height, GBM_FORMAT_XRGB8888,
                           GBM_BO_USE_RENDERING | GBM_BO_USE_LINEAR);
    if (res.bo == NULL)
        FAIL_NOW(&rep, 4, "unsupported", "gbm_alloc",
                          "GBM cannot allocate XRGB8888 %dx%d with "
                          "GBM_BO_USE_LINEAR", opt.width, opt.height);

    rep.fourcc = (int)gbm_bo_get_format(res.bo);
    rep.gbm_width = (int)gbm_bo_get_width(res.bo);
    rep.gbm_height = (int)gbm_bo_get_height(res.bo);
    rep.gbm_stride = (int)gbm_bo_get_stride_for_plane(res.bo, 0);
    rep.gbm_offset = (int)gbm_bo_get_offset(res.bo, 0);
    rep.gbm_plane_count = gbm_bo_get_plane_count(res.bo);
    rep.modifier = gbm_bo_get_modifier(res.bo);
    rep.width = rep.gbm_width;
    rep.height = rep.gbm_height;
    rep.total_pixels = rep.width * rep.height;

    if (rep.fourcc != (int)DRM_FORMAT_XRGB8888)
        FAIL_NOW(&rep, 4, "unsupported", "gbm_alloc",
                          "GBM returned fourcc 0x%08x instead of XRGB8888",
                          (unsigned int)rep.fourcc);
    if (rep.gbm_plane_count != 1)
        FAIL_NOW(&rep, 4, "unsupported", "gbm_alloc",
                          "XRGB8888 allocation returned %d planes, expected 1",
                          rep.gbm_plane_count);
    if (rep.modifier != DRM_FORMAT_MOD_LINEAR)
        FAIL_NOW(&rep, 4, "unsupported", "gbm_alloc",
                          "GBM returned modifier 0x%016" PRIx64
                          " instead of linear; refusing to fill a non-linear "
                          "layout through a CPU mapping",
                          rep.modifier);
    if (rep.gbm_stride < rep.width * 4)
        FAIL_NOW(&rep, 5, "failed", "gbm_map",
                          "GBM stride %d is smaller than width*4=%d",
                          rep.gbm_stride, rep.width * 4);

    map = gbm_bo_map(res.bo, 0, 0, (uint32_t)rep.width,
                     (uint32_t)rep.height, GBM_BO_TRANSFER_WRITE,
                     &map_stride, &map_data);
    if (map == NULL)
        FAIL_NOW(&rep, 5, "failed", "gbm_map",
                          "gbm_bo_map failed");
    res.map = map;
    res.map_data = map_data;
    if (map_stride < (uint32_t)(rep.width * 4)) {
        FAIL_NOW(&rep, 5, "failed", "gbm_map",
                               "mapped stride %u is smaller than width*4=%d",
                               (unsigned int)map_stride, rep.width * 4);
    }

    res.expected = calloc((size_t)rep.width * (size_t)rep.height, 3);
    res.actual = calloc((size_t)rep.width * (size_t)rep.height, 4);
    if (res.expected == NULL || res.actual == NULL) {
        FAIL_NOW(&rep, 5, "failed", "memory",
                               "out of memory allocating pixel buffers");
    }

    for (j = 0; j < rep.height; j++) {
        uint8_t *row = (uint8_t *)map + (size_t)j * (size_t)map_stride;
        int k;

        memset(row, 0, map_stride);
        for (k = 0; k < rep.width; k++) {
            uint8_t rgb[3];
            size_t expected_off =
                ((size_t)j * (size_t)rep.width + (size_t)k) * 3;

            pattern_rgb(k, j, rep.width, rep.height, rgb);
            row[(size_t)k * 4 + 0] = rgb[2]; /* B in XRGB8888 memory */
            row[(size_t)k * 4 + 1] = rgb[1]; /* G */
            row[(size_t)k * 4 + 2] = rgb[0]; /* R */
            row[(size_t)k * 4 + 3] = 0xff;   /* X */
            res.expected[expected_off + 0] = rgb[0];
            res.expected[expected_off + 1] = rgb[1];
            res.expected[expected_off + 2] = rgb[2];
        }
    }
    gbm_bo_unmap(res.bo, res.map_data);
    res.map = NULL;
    res.map_data = NULL;
    map = NULL;

    /* Export the actual DMA-BUF fd(s) and inspect every plane. */
    rep.plane_count = rep.gbm_plane_count;
    for (i = 0; i < rep.plane_count; i++) {
        int fd = gbm_bo_get_fd_for_plane(res.bo, i);

        if (fd < 0) {
            FAIL_NOW(&rep, 5, "failed", "dmabuf_export",
                                   "gbm_bo_get_fd_for_plane(%d) failed: %s",
                                   i, strerror(errno));
        }
        res.plane_fds[res.plane_fd_count++] = fd;
        rep.planes[i].fd = fd;
        rep.planes[i].fd_open = true;
        rep.planes[i].offset = gbm_bo_get_offset(res.bo, i);
        rep.planes[i].stride = gbm_bo_get_stride_for_plane(res.bo, i);
        if (rep.planes[i].stride <
            (uint32_t)(rep.width * 4)) {
            FAIL_NOW(&rep, 5, "failed", "dmabuf_export",
                                   "plane %d stride %u is smaller than "
                                   "width*4=%d", i,
                                   (unsigned int)rep.planes[i].stride,
                                   rep.width * 4);
        }
    }

    /* EGL/GLES on the same GBM device. */
    client_ext = eglQueryString(EGL_NO_DISPLAY, EGL_EXTENSIONS);
    get_platform_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)eglGetProcAddress(
            "eglGetPlatformDisplayEXT");
    if (get_platform_display != NULL && client_ext != NULL &&
        (strstr(client_ext, "EGL_KHR_platform_gbm") != NULL ||
         strstr(client_ext, "EGL_MESA_platform_gbm") != NULL)) {
        res.dpy = get_platform_display(EGL_PLATFORM_GBM_KHR, res.gbm, NULL);
        copy_text(rep.egl_platform, sizeof(rep.egl_platform),
                  "eglGetPlatformDisplayEXT(EGL_PLATFORM_GBM_KHR)");
    } else {
        res.dpy = eglGetDisplay((EGLNativeDisplayType)res.gbm);
        copy_text(rep.egl_platform, sizeof(rep.egl_platform),
                  "eglGetDisplay(gbm_device)");
    }
    if (res.dpy == EGL_NO_DISPLAY) {
        FAIL_NOW(&rep, 4, "unsupported", "egl_display",
                               "cannot create an EGL display for the GBM "
                               "device (EGL error 0x%04x)",
                               (unsigned int)eglGetError());
    }
    if (!eglInitialize(res.dpy, &major, &minor)) {
        FAIL_NOW(&rep, 5, "failed", "egl_init",
                               "eglInitialize failed (EGL error 0x%04x)",
                               (unsigned int)eglGetError());
    }
    res.egl_initialized = true;
    snprintf(rep.egl_version, sizeof(rep.egl_version), "%d.%d", major, minor);
    if (!eglBindAPI(EGL_OPENGL_ES_API)) {
        FAIL_NOW(&rep, 5, "failed", "egl_init",
                               "eglBindAPI(EGL_OPENGL_ES_API) failed "
                               "(EGL error 0x%04x)",
                               (unsigned int)eglGetError());
    }

    egl_ext = eglQueryString(res.dpy, EGL_EXTENSIONS);
    rep.ext_import = egl_ext != NULL &&
                     strstr(egl_ext, "EGL_EXT_image_dma_buf_import") != NULL;
    rep.ext_import_modifiers =
        egl_ext != NULL &&
        strstr(egl_ext, "EGL_EXT_image_dma_buf_import_modifiers") != NULL;
    rep.ext_surfaceless =
        egl_ext != NULL &&
        strstr(egl_ext, "EGL_KHR_surfaceless_context") != NULL;
    if (!rep.ext_import) {
        FAIL_NOW(&rep, 4, "unsupported", "egl_extensions",
                               "EGL_EXT_image_dma_buf_import is not "
                               "advertised");
    }

    {
        EGLint cfg_attrs[] = {
            EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
            EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
            EGL_NONE
        };
        EGLint cfg_attrs_no_surface[] = {
            EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
            EGL_RED_SIZE, 8, EGL_GREEN_SIZE, 8, EGL_BLUE_SIZE, 8,
            EGL_NONE
        };

        if (!eglChooseConfig(res.dpy, cfg_attrs, &config, 1,
                             &config_count) ||
            config_count == 0) {
            config = NULL;
            config_count = 0;
            if (!eglChooseConfig(res.dpy, cfg_attrs_no_surface, &config, 1,
                                 &config_count) ||
                config_count == 0) {
                config = NULL;
                config_count = 0;
            }
        }
    }

    if (config == NULL) {
        FAIL_NOW(&rep, 4, "unsupported", "egl_config",
                               "no EGL config with OpenGL ES2 and RGB888");
    }

    res.ctx = eglCreateContext(res.dpy, config, EGL_NO_CONTEXT, ctx_attribs);
    if (res.ctx == EGL_NO_CONTEXT) {
        FAIL_NOW(&rep, 5, "failed", "egl_context",
                               "eglCreateContext failed (EGL error 0x%04x)",
                               (unsigned int)eglGetError());
    }

    if (rep.ext_surfaceless) {
        if (!eglMakeCurrent(res.dpy, EGL_NO_SURFACE, EGL_NO_SURFACE,
                            res.ctx)) {
            FAIL_NOW(&rep, 5, "failed", "egl_context",
                                   "surfaceless eglMakeCurrent failed "
                                   "(EGL error 0x%04x)",
                                   (unsigned int)eglGetError());
        }
    } else {
        EGLint pb_attrs[] = {
            EGL_WIDTH, 1, EGL_HEIGHT, 1, EGL_NONE
        };
        res.surface = eglCreatePbufferSurface(res.dpy, config, pb_attrs);
        if (res.surface == EGL_NO_SURFACE ||
            !eglMakeCurrent(res.dpy, res.surface, res.surface, res.ctx)) {
            FAIL_NOW(&rep, 5, "failed", "egl_context",
                                   "pbuffer eglMakeCurrent failed "
                                   "(EGL error 0x%04x)",
                                   (unsigned int)eglGetError());
        }
    }

    copy_text(rep.gl_vendor, sizeof(rep.gl_vendor),
              (const char *)glGetString(GL_VENDOR));
    copy_text(rep.gl_renderer, sizeof(rep.gl_renderer),
              (const char *)glGetString(GL_RENDERER));
    copy_text(rep.gl_version, sizeof(rep.gl_version),
              (const char *)glGetString(GL_VERSION));
    gl_ext = (const char *)glGetString(GL_EXTENSIONS);
    if (gl_ext == NULL || strstr(gl_ext, "GL_OES_EGL_image") == NULL) {
        FAIL_NOW(&rep, 4, "unsupported", "gles_extensions",
                               "GL_OES_EGL_image is not advertised");
    }
    image_target_texture =
        (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)eglGetProcAddress(
            "glEGLImageTargetTexture2DOES");
    if (image_target_texture == NULL) {
        FAIL_NOW(&rep, 5, "failed", "gles_extensions",
                               "cannot resolve glEGLImageTargetTexture2DOES");
    }
    create_image = (PFNEGLCREATEIMAGEKHRPROC)eglGetProcAddress(
        "eglCreateImageKHR");
    res.destroy_image = (PFNEGLDESTROYIMAGEKHRPROC)eglGetProcAddress(
        "eglDestroyImageKHR");
    if (create_image == NULL || res.destroy_image == NULL) {
        FAIL_NOW(&rep, 5, "failed", "egl_extensions",
                               "cannot resolve eglCreateImageKHR/"
                               "eglDestroyImageKHR");
    }

    query_formats = (PFNEGLQUERYDMABUFFORMATSEXTPROC)eglGetProcAddress(
        "eglQueryDmaBufFormatsEXT");
    query_modifiers = (PFNEGLQUERYDMABUFMODIFIERSEXTPROC)eglGetProcAddress(
        "eglQueryDmaBufModifiersEXT");

    if (rep.ext_import_modifiers && query_formats != NULL &&
        query_modifiers != NULL) {
        if (!query_formats(res.dpy, 0, NULL, &format_count) ||
            format_count <= 0) {
            FAIL_NOW(&rep, 4, "unsupported", "egl_formats",
                                   "eglQueryDmaBufFormatsEXT returned no "
                                   "formats");
        }
        formats = calloc((size_t)format_count, sizeof(*formats));
        if (formats == NULL) {
            FAIL_NOW(&rep, 5, "failed", "memory",
                                   "out of memory for format list");
        }
        if (!query_formats(res.dpy, format_count, formats, &format_count)) {
            FAIL_NOW(&rep, 5, "failed", "egl_formats",
                                   "eglQueryDmaBufFormatsEXT failed "
                                   "(EGL error 0x%04x)",
                                   (unsigned int)eglGetError());
        }
        rep.egl_format_count = format_count;
        rep.format_query_performed = true;
        for (i = 0; i < format_count; i++) {
            if (formats[i] == DRM_FORMAT_XRGB8888) {
                rep.xrgb8888_supported = true;
                break;
            }
        }
        if (!rep.xrgb8888_supported) {
            FAIL_NOW(&rep, 4, "unsupported", "egl_formats",
                                   "XRGB8888 is not importable by EGL");
        }

        if (!query_modifiers(res.dpy, DRM_FORMAT_XRGB8888, 0, NULL, NULL,
                             &modifier_count) ||
            modifier_count < 0) {
            FAIL_NOW(&rep, 5, "failed", "egl_modifiers",
                                   "eglQueryDmaBufModifiersEXT failed "
                                   "(EGL error 0x%04x)",
                                   (unsigned int)eglGetError());
        }
        rep.egl_modifier_count = modifier_count;
        if (modifier_count > 0) {
            modifiers = calloc((size_t)modifier_count, sizeof(*modifiers));
            external_only = calloc((size_t)modifier_count,
                                   sizeof(*external_only));
            if (modifiers == NULL || external_only == NULL) {
                FAIL_NOW(&rep, 5, "failed", "memory",
                                       "out of memory for modifier list");
            }
            if (!query_modifiers(res.dpy, DRM_FORMAT_XRGB8888,
                                 modifier_count, modifiers, external_only,
                                 &modifier_count)) {
                FAIL_NOW(&rep, 5, "failed", "egl_modifiers",
                                       "eglQueryDmaBufModifiersEXT failed "
                                       "(EGL error 0x%04x)",
                                       (unsigned int)eglGetError());
            }
            for (i = 0; i < modifier_count &&
                        rep.candidate_count < MAX_CANDIDATES; i++) {
                struct mod_candidate *cand =
                    &rep.candidates[rep.candidate_count];

                cand->modifier = (uint64_t)modifiers[i];
                cand->external_only = external_only[i] == EGL_TRUE;
                cand->gbm_plane_count =
                    gbm_device_get_format_modifier_plane_count(
                        res.gbm, GBM_FORMAT_XRGB8888,
                        (uint64_t)modifiers[i]);
                rep.candidate_count++;
            }
        }
    } else {
        /* Without the modifiers extension only the implicit linear layout is
         * importable; still report the format count when available. */
        if (query_formats != NULL &&
            query_formats(res.dpy, 0, NULL, &format_count) &&
            format_count > 0) {
            formats = calloc((size_t)format_count, sizeof(*formats));
            if (formats != NULL &&
                query_formats(res.dpy, format_count, formats,
                              &format_count)) {
                rep.egl_format_count = format_count;
                rep.format_query_performed = true;
                for (i = 0; i < format_count; i++) {
                    if (formats[i] == DRM_FORMAT_XRGB8888) {
                        rep.xrgb8888_supported = true;
                        break;
                    }
                }
            }
        }
        if (rep.format_query_performed && !rep.xrgb8888_supported) {
            FAIL_NOW(&rep, 4, "unsupported", "egl_formats",
                     "XRGB8888 is not importable by EGL");
        }
    }

    /*
     * The synthetic buffer is CPU-filled through a linear mapping.  A
     * non-linear modifier would make those writes meaningless, so it is an
     * explicit unsupported result rather than a silent fallback to linear.
     */
    if (opt.modifier_forced && opt.modifier != DRM_FORMAT_MOD_LINEAR) {
        bool advertised = false;

        for (i = 0; i < rep.candidate_count; i++) {
            if (rep.candidates[i].modifier == opt.modifier) {
                advertised = true;
                break;
            }
        }
        FAIL_NOW(&rep, 4, "unsupported", "modifier",
                               "modifier 0x%016" PRIx64 " (%s) is not "
                               "usable by this CPU-initialized synthetic "
                               "test; only DRM_FORMAT_MOD_LINEAR is "
                               "supported (EGL advertised: %s)",
                               opt.modifier,
                               modifier_name(opt.modifier) != NULL ?
                                   modifier_name(opt.modifier) : "unknown",
                               advertised ? "yes" : "no");
    }
    rep.modifier = DRM_FORMAT_MOD_LINEAR;

    if (rep.ext_import_modifiers) {
        bool linear_ok = false;

        for (i = 0; i < rep.candidate_count; i++) {
            if (rep.candidates[i].modifier == DRM_FORMAT_MOD_LINEAR &&
                rep.candidates[i].gbm_plane_count >= 1) {
                linear_ok = true;
                break;
            }
        }
        if (!linear_ok) {
            FAIL_NOW(&rep, 4, "unsupported", "modifier",
                                   "DRM_FORMAT_MOD_LINEAR is not reported as "
                                   "importable for XRGB8888");
        }
    }

    {
        EGLint attrs[32];
        int n = 0;

        attrs[n++] = EGL_WIDTH;
        attrs[n++] = rep.width;
        attrs[n++] = EGL_HEIGHT;
        attrs[n++] = rep.height;
        attrs[n++] = EGL_LINUX_DRM_FOURCC_EXT;
        attrs[n++] = DRM_FORMAT_XRGB8888;
        for (i = 0; i < rep.plane_count; i++) {
            /* EGL_EXT_image_dma_buf_import uses fixed PLANE0..PLANE2 tokens;
             * plane_count is validated as 1 above for this linear path. */
            if (i != 0) {
                FAIL_NOW(&rep, 4, "unsupported", "egl_import",
                                       "multi-plane import is not used for "
                                       "this linear XRGB8888 test");
            }
            attrs[n++] = EGL_DMA_BUF_PLANE0_FD_EXT;
            attrs[n++] = rep.planes[i].fd;
            attrs[n++] = EGL_DMA_BUF_PLANE0_OFFSET_EXT;
            attrs[n++] = (EGLint)rep.planes[i].offset;
            attrs[n++] = EGL_DMA_BUF_PLANE0_PITCH_EXT;
            attrs[n++] = (EGLint)rep.planes[i].stride;
            if (rep.ext_import_modifiers) {
                attrs[n++] = EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT;
                attrs[n++] = (EGLint)(rep.modifier & 0xffffffffu);
                attrs[n++] = EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT;
                attrs[n++] = (EGLint)(rep.modifier >> 32);
                rep.modifier_attrs_used = true;
            }
        }
        attrs[n++] = EGL_NONE;

        res.image = create_image(res.dpy, EGL_NO_CONTEXT,
                                 EGL_LINUX_DMA_BUF_EXT, NULL, attrs);
        if (res.image == EGL_NO_IMAGE_KHR) {
            FAIL_NOW(&rep, 5, "failed", "egl_import",
                                   "eglCreateImageKHR(EGL_LINUX_DMA_BUF_EXT) "
                                   "failed (EGL error 0x%04x)",
                                   (unsigned int)eglGetError());
        }
        rep.image_created = true;
    }

    /* The EGLImage holds its own reference; close the exported fds now. */
    for (i = 0; i < res.plane_fd_count; i++) {
        close(res.plane_fds[i]);
        res.plane_fds[i] = -1;
        rep.planes[i].fd_open = false;
    }
    res.plane_fd_count = 0;

    glGenTextures(1, &res.tex);
    if (res.tex == 0) {
        FAIL_NOW(&rep, 5, "failed", "gl_texture",
                               "glGenTextures failed");
    }
    glBindTexture(GL_TEXTURE_2D, res.tex);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    image_target_texture(GL_TEXTURE_2D, (GLeglImageOES)res.image);
    gl_error = glGetError();
    if (gl_error != GL_NO_ERROR) {
        FAIL_NOW(&rep, 5, "failed", "gl_texture",
                               "glEGLImageTargetTexture2DOES failed "
                               "(GL error 0x%04x)",
                               (unsigned int)gl_error);
    }
    rep.texture_created = true;

    glGenFramebuffers(1, &res.fbo);
    if (res.fbo == 0) {
        FAIL_NOW(&rep, 5, "failed", "gl_fbo",
                               "glGenFramebuffers failed");
    }
    glBindFramebuffer(GL_FRAMEBUFFER, res.fbo);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                           GL_TEXTURE_2D, res.tex, 0);
    fbo_status = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    if (fbo_status != GL_FRAMEBUFFER_COMPLETE) {
        FAIL_NOW(&rep, 5, "failed", "gl_fbo",
                               "framebuffer is incomplete (0x%04x)",
                               (unsigned int)fbo_status);
    }
    rep.fbo_complete = true;
    rep.gpu_import = true;
    /* A successful import is stronger evidence than the format query. */
    rep.xrgb8888_supported = true;

    glViewport(0, 0, rep.width, rep.height);
    glDisable(GL_DITHER);
    glPixelStorei(GL_PACK_ALIGNMENT, 1);
    glFinish();
    glReadPixels(0, 0, rep.width, rep.height, GL_RGBA, GL_UNSIGNED_BYTE,
                 res.actual);
    gl_error = glGetError();
    if (gl_error != GL_NO_ERROR) {
        FAIL_NOW(&rep, 5, "failed", "gl_readback",
                               "glReadPixels failed (GL error 0x%04x)",
                               (unsigned int)gl_error);
    }
    rep.readback = true;

    {
        int direct_match = 0;
        int flip_match = 0;
        int direct_max = 0;
        int flip_max = 0;
        uint64_t hash = 1469598103934665603ULL;
        int y;
        int x;

        for (y = 0; y < rep.height; y++) {
            for (x = 0; x < rep.width; x++) {
                const uint8_t *actual =
                    res.actual + ((size_t)y * (size_t)rep.width +
                                  (size_t)x) * 4;
                const uint8_t *exp_direct =
                    res.expected + ((size_t)y * (size_t)rep.width +
                                    (size_t)x) * 3;
                const uint8_t *exp_flip =
                    res.expected + ((size_t)(rep.height - 1 - y) *
                                    (size_t)rep.width + (size_t)x) * 3;
                int direct_delta = max3_delta(actual, exp_direct);
                int flip_delta = max3_delta(actual, exp_flip);
                int c;

                if (direct_delta <= rep.tolerance)
                    direct_match++;
                if (flip_delta <= rep.tolerance)
                    flip_match++;
                if (direct_delta > direct_max)
                    direct_max = direct_delta;
                if (flip_delta > flip_max)
                    flip_max = flip_delta;
                for (c = 0; c < 3; c++) {
                    hash ^= (uint64_t)actual[c];
                    hash *= 1099511628211ULL;
                }
            }
        }

        rep.direct_matching_pixels = direct_match;
        rep.flipped_matching_pixels = flip_match;
        rep.rgb_hash = hash;

        if (direct_match == rep.total_pixels) {
            rep.orientation_flipped = false;
            rep.matching_pixels = direct_match;
            rep.max_channel_delta = direct_max;
        } else if (flip_match == rep.total_pixels) {
            rep.orientation_flipped = true;
            rep.matching_pixels = flip_match;
            rep.max_channel_delta = flip_max;
        } else if (flip_match > direct_match) {
            rep.orientation_flipped = true;
            rep.matching_pixels = flip_match;
            rep.max_channel_delta = flip_max;
        } else {
            rep.orientation_flipped = false;
            rep.matching_pixels = direct_match;
            rep.max_channel_delta = direct_max;
        }
        rep.mismatched_pixels = rep.total_pixels - rep.matching_pixels;

        if (rep.mismatched_pixels > 0) {
            for (y = 0; y < rep.height && rep.first_x < 0; y++) {
                for (x = 0; x < rep.width; x++) {
                    const uint8_t *actual =
                        res.actual + ((size_t)y * (size_t)rep.width +
                                      (size_t)x) * 4;
                    int ey = rep.orientation_flipped ?
                                 (rep.height - 1 - y) : y;
                    const uint8_t *expected =
                        res.expected + ((size_t)ey * (size_t)rep.width +
                                        (size_t)x) * 3;

                    if (max3_delta(actual, expected) > rep.tolerance) {
                        rep.first_x = x;
                        rep.first_y = y;
                        rep.first_expected[0] = expected[0];
                        rep.first_expected[1] = expected[1];
                        rep.first_expected[2] = expected[2];
                        rep.first_actual[0] = actual[0];
                        rep.first_actual[1] = actual[1];
                        rep.first_actual[2] = actual[2];
                        break;
                    }
                }
            }
            rep.passed = false;
            rep.status = "mismatch";
            rep.stage = "verify";
            snprintf(rep.error, sizeof(rep.error),
                     "pixel verification failed: %d of %d visible pixels "
                     "differ (tolerance %d, orientation %s)",
                     rep.mismatched_pixels, rep.total_pixels, rep.tolerance,
                     rep.orientation_flipped ? "y-flipped" : "direct");
            exit_code = 1;
        } else {
            rep.passed = true;
            rep.status = "ok";
            rep.stage = "complete";
            exit_code = 0;
        }
    }

done:
    cleanup(&res);
    free(formats);
    free(modifiers);
    free(external_only);
    for (i = 0; i < rep.plane_count; i++)
        rep.planes[i].fd_open = false;
    rep.resources_released = true;
    emit_report(&rep);
    return exit_code;
}
