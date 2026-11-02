/*
 * Bounded native PipeWire screen-cast consumer.
 *
 * This tool is deliberately only a consumer: the portal performs content
 * selection and opens the restricted PipeWire remote descriptor.  We never
 * connect to the default PipeWire socket.  The only connection path is
 *
 *     pw_context_connect_fd(context, inherited_fd, ...)
 *
 * followed by pw_stream_new()/pw_stream_connect().
 *
 * Output is NDJSON on stdout.  Image bytes never leave the process; only
 * dimensions, format metadata, FNV-1a hashes of visible pixels and colour
 * counts are reported.
 */
#define _GNU_SOURCE

#include <errno.h>
#include <fcntl.h>
#include <getopt.h>
#include <inttypes.h>
#include <limits.h>
#include <math.h>
#include <signal.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>

#include <pipewire/pipewire.h>
#include <spa/buffer/buffer.h>
#include <spa/param/buffers.h>
#include <spa/param/format-utils.h>
#include <spa/param/video/format-utils.h>
#include <spa/param/video/raw-utils.h>
#include <spa/pod/builder.h>
#include <spa/utils/defs.h>

#include <drm_fourcc.h>
#include <gbm.h>
#include <EGL/egl.h>
#include <EGL/eglext.h>
#include <GLES2/gl2.h>
#include <GLES2/gl2ext.h>

#define ARRAY_LEN(a) (sizeof(a) / sizeof((a)[0]))

#define MAX_FRAMES 100000u
#define MAX_TIMEOUT_MS 3600000u
#define MAX_DIMENSION 16384u
#define MAX_MODIFIERS_PER_FORMAT 48u
#define MAX_TOP_COLORS 8u
#define MAX_HASH_SAMPLES 16u
#define COLOR_TABLE_INITIAL 4096u
#define COLOR_TABLE_MAX 65536u
#define PATTERN_TOLERANCE 16
#define EXIT_USAGE 2
#define EXIT_CONNECT 3
#define EXIT_TIMEOUT 4
#define EXIT_UNSUPPORTED 5
#define EXIT_INTERRUPTED 130

#define FNV_OFFSET 1469598103934665603ULL
#define FNV_PRIME 1099511628211ULL
#define INVALID_RGB 0xffffffffu

struct options {
    int fd;
    uint32_t node;
    char target_serial[256];
    bool have_target_serial;
    const char *mode_name;
    bool dmabuf;
    uint32_t frames;
    uint32_t timeout_ms;
    const char *render_node;
    bool expect_pattern;
    bool self_test;
    bool help;
};

struct pixel_format {
    enum spa_video_format spa;
    uint32_t drm_fourcc;
    const char *name;
    const char *layout;
    bool bgr;
    bool has_alpha;
};

static const struct pixel_format pixel_formats[] = {
    /* DRM fourcc comments describe the little-endian byte order:
     * XRGB8888 is B,G,R,x in memory and therefore matches SPA BGRx.
     * ARGB8888 is B,G,R,A, XBGR8888 is R,G,B,x, ABGR8888 is R,G,B,A. */
    { SPA_VIDEO_FORMAT_BGRx, DRM_FORMAT_XRGB8888, "BGRx", "BGRX8888", true, false },
    { SPA_VIDEO_FORMAT_BGRA, DRM_FORMAT_ARGB8888, "BGRA", "BGRA8888", true, true },
    { SPA_VIDEO_FORMAT_RGBx, DRM_FORMAT_XBGR8888, "RGBx", "RGBX8888", false, false },
    { SPA_VIDEO_FORMAT_RGBA, DRM_FORMAT_ABGR8888, "RGBA", "RGBA8888", false, true },
};

static const uint32_t pattern_red = 0xb12b31u;
static const uint32_t pattern_green = 0x167c44u;
static const uint32_t pattern_blue = 0x1d5bc4u;
static const uint32_t pattern_frame = 0x26334bu;

struct color_entry {
    uint32_t rgb;
    uint32_t count;
};

struct color_table {
    struct color_entry *entries;
    uint32_t capacity;
    uint32_t used;
    uint64_t overflow_pixels;
};

struct color_count {
    uint32_t rgb;
    uint32_t count;
};

struct frame_stats {
    bool hash_set;
    uint64_t hash;
    uint32_t width;
    uint32_t height;
    uint32_t stride;
    uint64_t visible_pixels;
    uint32_t distinct_colors;
    uint64_t overflow_pixels;
    bool nonuniform;
    uint32_t dominant_rgb;
    uint32_t dominant_count;
    double dominant_fraction;
    uint32_t top_count;
    struct color_count top[MAX_TOP_COLORS];
    uint32_t pattern_red;
    uint32_t pattern_green;
    uint32_t pattern_blue;
    uint32_t pattern_other;
    bool pattern_accepted;
    const char *pattern_reason;
};

struct cpu_frame {
    const uint8_t *base;
    uint64_t maxsize;
    uint32_t offset;
    uint32_t size;
    int32_t stride;
};

struct egl_state {
    int gbm_fd;
    struct gbm_device *gbm;
    EGLDisplay display;
    EGLContext context;
    EGLSurface surface;
    EGLConfig config;
    bool initialized;
    bool dma_buf_import;
    bool dma_buf_modifiers;
    PFNEGLGETPLATFORMDISPLAYEXTPROC get_platform_display;
    PFNEGLCREATEIMAGEKHRPROC create_image;
    PFNEGLDESTROYIMAGEKHRPROC destroy_image;
    PFNEGLQUERYDMABUFFORMATSEXTPROC query_formats;
    PFNEGLQUERYDMABUFMODIFIERSEXTPROC query_modifiers;
    PFNGLEGLIMAGETARGETTEXTURE2DOESPROC image_target_texture;
};

struct supported_format {
    const struct pixel_format *pix;
    bool fourcc_supported;
    uint32_t n_modifiers;
    uint64_t modifiers[MAX_MODIFIERS_PER_FORMAT];
};

struct consumer {
    struct options opt;
    struct pw_main_loop *loop;
    struct pw_context *context;
    struct pw_core *core;
    struct pw_stream *stream;
    struct spa_hook stream_listener;
    struct spa_source *timer;
    struct spa_source *sigint_source;
    struct spa_source *sigterm_source;
    struct egl_state egl;
    struct supported_format formats[ARRAY_LEN(pixel_formats)];
    uint32_t n_formats;
    bool have_format;
    bool format_reported;
    const struct pixel_format *pix;
    struct spa_video_info_raw video;
    bool modifier_present;
    uint64_t modifier;
    bool dmabuf_negotiated;
    uint32_t frames;
    uint32_t frame_events;
    uint32_t buffers_seen;
    uint32_t skipped_uniform;
    uint32_t skipped_pattern;
    uint32_t skipped_empty;
    uint64_t last_hash;
    bool have_last_hash;
    uint32_t hash_changes;
    uint64_t hashes_first[MAX_HASH_SAMPLES];
    uint32_t n_hashes_first;
    uint64_t hashes_last[MAX_HASH_SAMPLES];
    uint32_t n_hashes_last;
    struct frame_stats last_stats;
    bool have_last_stats;
    struct color_table colors;
    uint64_t deadline_ns;
    bool deadline_hit;
    bool interrupted;
    bool finished;
    bool have_error;
    char error_code[64];
    char error_message[256];
    bool error_emitted;
    int exit_code;
    bool gpu_imported_any;
    bool cpu_readback_any;
    const char *buffer_type;
    const char *capture_mode;
    bool target_by_serial;
};

/* ------------------------------------------------------------------ */
/* small utilities                                                      */

static uint64_t monotonic_ns(void)
{
    struct timespec ts;

    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0)
        return 0;
    return (uint64_t)ts.tv_sec * 1000000000ULL + (uint64_t)ts.tv_nsec;
}

static void json_escape(FILE *out, const char *text)
{
    const unsigned char *p = (const unsigned char *)(text != NULL ? text : "");

    fputc('"', out);
    for (; *p != '\0'; p++) {
        switch (*p) {
        case '"':
            fputs("\\\"", out);
            break;
        case '\\':
            fputs("\\\\", out);
            break;
        case '\b':
            fputs("\\b", out);
            break;
        case '\f':
            fputs("\\f", out);
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
    fputc('"', out);
}

static void json_rgb(FILE *out, uint32_t rgb)
{
    fprintf(out, "\"#%06x\"", rgb & 0xffffffu);
}

static void json_string_or_null(FILE *out, const char *text)
{
    if (text == NULL)
        fputs("null", out);
    else
        json_escape(out, text);
}

static void emit_error_now(const char *code, const char *message)
{
    fputs("{\"type\":\"error\",\"code\":", stdout);
    json_escape(stdout, code);
    fputs(",\"message\":", stdout);
    json_escape(stdout, message);
    fputs("}\n", stdout);
    fflush(stdout);
}

static void set_error(struct consumer *c, const char *code, const char *fmt, ...)
{
    va_list ap;

    if (c->have_error)
        return;
    c->have_error = true;
    snprintf(c->error_code, sizeof(c->error_code), "%s", code);
    va_start(ap, fmt);
    vsnprintf(c->error_message, sizeof(c->error_message), fmt, ap);
    va_end(ap);
}

static void set_and_emit_error(struct consumer *c, const char *code,
                               const char *fmt, ...)
{
    va_list ap;

    if (c->have_error && c->error_emitted)
        return;
    c->have_error = true;
    snprintf(c->error_code, sizeof(c->error_code), "%s", code);
    va_start(ap, fmt);
    vsnprintf(c->error_message, sizeof(c->error_message), fmt, ap);
    va_end(ap);
    if (!c->error_emitted) {
        emit_error_now(c->error_code, c->error_message);
        c->error_emitted = true;
    }
}

static const struct pixel_format *find_pixel_format(enum spa_video_format spa)
{
    size_t i;

    for (i = 0; i < ARRAY_LEN(pixel_formats); i++) {
        if (pixel_formats[i].spa == spa)
            return &pixel_formats[i];
    }
    return NULL;
}

static const char *spa_data_type_name(uint32_t type)
{
    switch (type) {
    case SPA_DATA_MemFd:
        return "MemFd";
    case SPA_DATA_DmaBuf:
        return "DmaBuf";
    case SPA_DATA_MemPtr:
        return "MemPtr";
    case SPA_DATA_MemId:
        return "MemId";
    default:
        return "Unsupported";
    }
}

static void format_modifier(char *out, size_t size, uint64_t modifier)
{
    snprintf(out, size, "0x%016" PRIx64, modifier);
}

/* ------------------------------------------------------------------ */
/* colour table and pixel analysis                                      */

static void colors_init(struct color_table *table)
{
    memset(table, 0, sizeof(*table));
    table->capacity = COLOR_TABLE_INITIAL;
    table->entries = calloc(table->capacity, sizeof(*table->entries));
    if (table->entries == NULL) {
        table->capacity = 0;
        return;
    }
    for (uint32_t i = 0; i < table->capacity; i++)
        table->entries[i].rgb = INVALID_RGB;
}

static void colors_destroy(struct color_table *table)
{
    free(table->entries);
    memset(table, 0, sizeof(*table));
}

static bool colors_grow(struct color_table *table)
{
    uint32_t new_capacity;
    struct color_entry *entries;
    uint32_t i;

    if (table->capacity >= COLOR_TABLE_MAX)
        return false;
    new_capacity = table->capacity * 2u;
    if (new_capacity > COLOR_TABLE_MAX)
        new_capacity = COLOR_TABLE_MAX;
    entries = calloc(new_capacity, sizeof(*entries));
    if (entries == NULL)
        return false;
    for (i = 0; i < new_capacity; i++)
        entries[i].rgb = INVALID_RGB;
    for (i = 0; i < table->capacity; i++) {
        if (table->entries[i].rgb != INVALID_RGB) {
            uint32_t slot = (table->entries[i].rgb * 2654435761u) &
                            (new_capacity - 1u);

            while (entries[slot].rgb != INVALID_RGB)
                slot = (slot + 1u) & (new_capacity - 1u);
            entries[slot] = table->entries[i];
        }
    }
    free(table->entries);
    table->entries = entries;
    table->capacity = new_capacity;
    return true;
}

static void colors_reset(struct color_table *table)
{
    uint32_t i;

    if (table->entries == NULL)
        return;
    for (i = 0; i < table->capacity; i++)
        table->entries[i].rgb = INVALID_RGB;
    table->used = 0;
    table->overflow_pixels = 0;
}

static void colors_add(struct color_table *table, uint32_t rgb)
{
    uint32_t slot;

    if (table->entries == NULL || table->capacity == 0) {
        table->overflow_pixels++;
        return;
    }
    if ((uint64_t)(table->used + 1u) * 4u >= (uint64_t)table->capacity * 3u &&
        table->capacity < COLOR_TABLE_MAX) {
        (void)colors_grow(table);
    }
    slot = (rgb * 2654435761u) & (table->capacity - 1u);
    for (;;) {
        if (table->entries[slot].rgb == INVALID_RGB) {
            table->entries[slot].rgb = rgb;
            table->entries[slot].count = 1;
            table->used++;
            return;
        }
        if (table->entries[slot].rgb == rgb) {
            table->entries[slot].count++;
            return;
        }
        slot = (slot + 1u) & (table->capacity - 1u);
    }
}

static void colors_top(const struct color_table *table,
                       struct frame_stats *stats)
{
    uint32_t i;

    stats->top_count = 0;
    stats->dominant_rgb = 0;
    stats->dominant_count = 0;
    if (table->entries == NULL)
        return;
    for (i = 0; i < table->capacity; i++) {
        const struct color_entry *entry = &table->entries[i];
        uint32_t j;

        if (entry->rgb == INVALID_RGB)
            continue;
        if (entry->count > stats->dominant_count) {
            stats->dominant_count = entry->count;
            stats->dominant_rgb = entry->rgb;
        }
        for (j = 0; j < stats->top_count; j++) {
            if (entry->count > stats->top[j].count)
                break;
        }
        if (j >= MAX_TOP_COLORS)
            continue;
        if (stats->top_count < MAX_TOP_COLORS)
            stats->top_count++;
        for (uint32_t k = stats->top_count - 1u; k > j; k--)
            stats->top[k] = stats->top[k - 1u];
        stats->top[j].rgb = entry->rgb;
        stats->top[j].count = entry->count;
    }
    stats->distinct_colors = table->used;
    stats->overflow_pixels = table->overflow_pixels;
    stats->nonuniform = table->used > 1u;
    if (stats->visible_pixels > 0)
        stats->dominant_fraction =
            (double)stats->dominant_count / (double)stats->visible_pixels;
}

static inline uint32_t canonical_rgb(const uint8_t *pixel,
                                     const struct pixel_format *format)
{
    uint32_t r;
    uint32_t g;
    uint32_t b;

    if (format->bgr) {
        b = pixel[0];
        g = pixel[1];
        r = pixel[2];
    } else {
        r = pixel[0];
        g = pixel[1];
        b = pixel[2];
    }
    return (r << 16) | (g << 8) | b;
}

static inline void store_rgb(uint8_t *pixel, const struct pixel_format *format,
                             uint32_t rgb)
{
    uint8_t r = (uint8_t)((rgb >> 16) & 0xffu);
    uint8_t g = (uint8_t)((rgb >> 8) & 0xffu);
    uint8_t b = (uint8_t)(rgb & 0xffu);

    if (format->bgr) {
        pixel[0] = b;
        pixel[1] = g;
        pixel[2] = r;
    } else {
        pixel[0] = r;
        pixel[1] = g;
        pixel[2] = b;
    }
    pixel[3] = format->has_alpha ? 0xffu : 0x5au;
}

static inline uint64_t hash_rgb(uint64_t hash, uint32_t rgb)
{
    hash ^= (uint8_t)((rgb >> 16) & 0xffu);
    hash *= FNV_PRIME;
    hash ^= (uint8_t)((rgb >> 8) & 0xffu);
    hash *= FNV_PRIME;
    hash ^= (uint8_t)(rgb & 0xffu);
    hash *= FNV_PRIME;
    return hash;
}

static int channel_delta(uint32_t a, uint32_t b)
{
    int dr = (int)((a >> 16) & 0xffu) - (int)((b >> 16) & 0xffu);
    int dg = (int)((a >> 8) & 0xffu) - (int)((b >> 8) & 0xffu);
    int db = (int)(a & 0xffu) - (int)(b & 0xffu);

    if (dr < 0)
        dr = -dr;
    if (dg < 0)
        dg = -dg;
    if (db < 0)
        db = -db;
    if (dr < dg)
        dr = dg;
    if (dr < db)
        dr = db;
    return dr;
}

static bool validate_cpu_frame(const struct cpu_frame *frame, uint32_t width,
                               uint32_t height, const struct pixel_format *format,
                               char *error, size_t error_size)
{
    uint64_t row_bytes;
    uint64_t required;
    uint64_t end;

    if (format == NULL) {
        snprintf(error, error_size, "unsupported pixel format");
        return false;
    }
    if (width == 0 || height == 0 || width > MAX_DIMENSION ||
        height > MAX_DIMENSION) {
        snprintf(error, error_size, "invalid or excessive dimensions %ux%u",
                 width, height);
        return false;
    }
    if (frame->base == NULL) {
        snprintf(error, error_size, "buffer has no readable mapping");
        return false;
    }
    if (frame->maxsize == 0 || frame->maxsize > (uint64_t)SIZE_MAX) {
        snprintf(error, error_size, "invalid buffer size %" PRIu64,
                 frame->maxsize);
        return false;
    }
    if (frame->stride <= 0) {
        snprintf(error, error_size, "unsupported non-positive stride %" PRId32,
                 frame->stride);
        return false;
    }
    row_bytes = (uint64_t)width * 4u;
    if ((uint64_t)frame->stride < row_bytes) {
        snprintf(error, error_size,
                 "stride %" PRId32 " is smaller than visible row %" PRIu64,
                 frame->stride, row_bytes);
        return false;
    }
    required = (uint64_t)(height - 1u) * (uint64_t)frame->stride + row_bytes;
    if (required > (uint64_t)UINT32_MAX) {
        snprintf(error, error_size, "visible frame size overflows chunk size");
        return false;
    }
    if ((uint64_t)frame->offset >= frame->maxsize) {
        snprintf(error, error_size,
                 "chunk offset %u outside buffer size %" PRIu64,
                 frame->offset, frame->maxsize);
        return false;
    }
    if ((uint64_t)frame->size < required) {
        snprintf(error, error_size,
                 "truncated frame: chunk size %u < required %" PRIu64,
                 frame->size, required);
        return false;
    }
    end = (uint64_t)frame->offset + (uint64_t)frame->size;
    if (end > frame->maxsize) {
        snprintf(error, error_size,
                 "chunk range [%u,%" PRIu64 ") exceeds buffer size %" PRIu64,
                 frame->offset, end, frame->maxsize);
        return false;
    }
    if ((uint64_t)frame->offset + required > frame->maxsize) {
        snprintf(error, error_size,
                 "visible rows exceed buffer: offset %u + required %" PRIu64
                 " > %" PRIu64,
                 frame->offset, required, frame->maxsize);
        return false;
    }
    return true;
}

static bool analyze_cpu_frame(struct consumer *c, const struct cpu_frame *frame,
                              struct frame_stats *stats, char *error,
                              size_t error_size)
{
    const uint8_t *rows;
    uint64_t hash = FNV_OFFSET;
    uint32_t x;
    uint32_t y;
    const struct pixel_format *format = c->pix;

    memset(stats, 0, sizeof(*stats));
    if (!validate_cpu_frame(frame, c->video.size.width, c->video.size.height,
                            format, error, error_size))
        return false;
    colors_reset(&c->colors);
    rows = frame->base + frame->offset;
    stats->width = c->video.size.width;
    stats->height = c->video.size.height;
    stats->stride = (uint32_t)frame->stride;
    stats->visible_pixels = (uint64_t)stats->width * (uint64_t)stats->height;

    for (y = 0; y < stats->height; y++) {
        const uint8_t *row = rows + (uint64_t)y * (uint64_t)frame->stride;

        for (x = 0; x < stats->width; x++) {
            const uint8_t *pixel = row + (uint64_t)x * 4u;
            uint32_t rgb = canonical_rgb(pixel, format);
            int dr = channel_delta(rgb, pattern_red);
            int dg = channel_delta(rgb, pattern_green);
            int db = channel_delta(rgb, pattern_blue);

            hash = hash_rgb(hash, rgb);
            colors_add(&c->colors, rgb);
            if (dr <= PATTERN_TOLERANCE || dg <= PATTERN_TOLERANCE ||
                db <= PATTERN_TOLERANCE) {
                if (dr <= dg && dr <= db && dr <= PATTERN_TOLERANCE)
                    stats->pattern_red++;
                else if (dg <= db && dg <= PATTERN_TOLERANCE)
                    stats->pattern_green++;
                else if (db <= PATTERN_TOLERANCE)
                    stats->pattern_blue++;
                else
                    stats->pattern_other++;
            }
        }
    }
    stats->hash = hash;
    stats->hash_set = true;
    colors_top(&c->colors, stats);

    {
        uint64_t total = stats->visible_pixels;
        uint64_t matched = (uint64_t)stats->pattern_red +
                           (uint64_t)stats->pattern_green +
                           (uint64_t)stats->pattern_blue;
        uint64_t minimum = total / 50u; /* 2% */

        if (minimum < 1)
            minimum = 1;
        stats->pattern_accepted =
            stats->nonuniform &&
            (uint64_t)stats->pattern_red >= minimum &&
            (uint64_t)stats->pattern_green >= minimum &&
            (uint64_t)stats->pattern_blue >= minimum &&
            matched * 10u >= total &&
            stats->dominant_fraction <= 0.95;
        if (!stats->nonuniform)
            stats->pattern_reason = "uniform frame";
        else if ((uint64_t)stats->pattern_red < minimum ||
                 (uint64_t)stats->pattern_green < minimum ||
                 (uint64_t)stats->pattern_blue < minimum)
            stats->pattern_reason = "missing red/green/blue test blocks";
        else if (matched * 10u < total)
            stats->pattern_reason = "test block coverage too small";
        else if (stats->dominant_fraction > 0.95)
            stats->pattern_reason = "frame dominated by one colour";
        else
            stats->pattern_reason = NULL;
    }
    return true;
}

static void print_top_colors(FILE *out, const struct frame_stats *stats)
{
    uint32_t i;

    fputc('[', out);
    for (i = 0; i < stats->top_count; i++) {
        if (i != 0)
            fputc(',', out);
        fputs("{\"rgb\":", out);
        json_rgb(out, stats->top[i].rgb);
        fprintf(out, ",\"count\":%u}", stats->top[i].count);
    }
    fputc(']', out);
}

static void print_pattern(FILE *out, const struct frame_stats *stats)
{
    uint64_t total = stats->visible_pixels;
    double red_fraction = total > 0 ? (double)stats->pattern_red / (double)total : 0.0;
    double green_fraction = total > 0 ? (double)stats->pattern_green / (double)total : 0.0;
    double blue_fraction = total > 0 ? (double)stats->pattern_blue / (double)total : 0.0;

    fputs("{\"accepted\":", out);
    fputs(stats->pattern_accepted ? "true" : "false", out);
    fputs(",\"tolerance\":", out);
    fprintf(out, "%d", PATTERN_TOLERANCE);
    fputs(",\"counts\":{\"red\":", out);
    fprintf(out, "%u", stats->pattern_red);
    fputs(",\"green\":", out);
    fprintf(out, "%u", stats->pattern_green);
    fputs(",\"blue\":", out);
    fprintf(out, "%u", stats->pattern_blue);
    fputs(",\"other\":", out);
    fprintf(out, "%u", stats->pattern_other);
    fputs("},\"fractions\":{\"red\":", out);
    fprintf(out, "%.6f", red_fraction);
    fputs(",\"green\":", out);
    fprintf(out, "%.6f", green_fraction);
    fputs(",\"blue\":", out);
    fprintf(out, "%.6f", blue_fraction);
    fputs("},\"reason\":", out);
    if (stats->pattern_reason == NULL)
        fputs("null", out);
    else
        json_escape(out, stats->pattern_reason);
    fputc('}', out);
}

/* ------------------------------------------------------------------ */
/* self-test                                                            */

struct self_test_result {
    char name[64];
    char detail[192];
    bool passed;
};

struct self_test_ctx {
    int failed;
    int checks;
    struct self_test_result results[64];
};

static void self_check(struct self_test_ctx *ctx, const char *name, bool ok,
                       const char *detail)
{
    struct self_test_result *result;

    if (ctx->checks >= (int)ARRAY_LEN(ctx->results))
        return;
    result = &ctx->results[ctx->checks++];
    snprintf(result->name, sizeof(result->name), "%s", name);
    snprintf(result->detail, sizeof(result->detail), "%s",
             detail != NULL ? detail : "");
    result->passed = ok;
    if (!ok)
        ctx->failed++;
}

static bool fill_test_frame(uint8_t *base, uint64_t maxsize, uint32_t offset,
                            uint32_t width, uint32_t height, int32_t stride,
                            const struct pixel_format *format)
{
    uint32_t x;
    uint32_t y;

    if (base == NULL || offset > maxsize)
        return false;
    memset(base, 0xee, (size_t)maxsize);
    for (y = 0; y < height; y++) {
        uint8_t *row = base + offset + (uint64_t)y * (uint64_t)stride;

        for (x = 0; x < width; x++) {
            uint32_t rgb;

            if (x < width / 2u && y < height / 2u)
                rgb = pattern_red;
            else if (x >= width / 2u && y < height / 2u)
                rgb = pattern_green;
            else if (x < width / 2u)
                rgb = pattern_blue;
            else
                rgb = pattern_frame;
            store_rgb(row + (uint64_t)x * 4u, format, rgb);
        }
    }
    return true;
}

static bool synthetic_analysis(struct consumer *c, const struct pixel_format *format,
                               const uint8_t *base, uint64_t maxsize,
                               uint32_t offset, uint32_t size, int32_t stride,
                               struct frame_stats *stats, char *error,
                               size_t error_size)
{
    struct cpu_frame frame = {
        .base = base,
        .maxsize = maxsize,
        .offset = offset,
        .size = size,
        .stride = stride,
    };

    c->pix = format;
    c->video.size.width = 64;
    c->video.size.height = 32;
    return analyze_cpu_frame(c, &frame, stats, error, error_size);
}

static int run_self_test(void)
{
    struct self_test_ctx ctx = { .failed = 0, .checks = 0 };
    struct consumer consumer;
    uint64_t total = 64u * 1024u;
    uint32_t stride = 64u * 4u + 36u;
    uint32_t required = (32u - 1u) * stride + 64u * 4u;
    uint32_t offset = 128u;
    int fd = -1;
    uint8_t *base = NULL;
    struct frame_stats first;
    struct frame_stats second;
    struct frame_stats stats;
    char error[256];
    bool ok;
    size_t i;

    memset(&consumer, 0, sizeof(consumer));
    colors_init(&consumer.colors);
    if (consumer.colors.entries == NULL) {
        emit_error_now("self_test_allocation", "cannot allocate colour table");
        fputs("{\"type\":\"summary\",\"passed\":false,\"self_test\":true,"
              "\"checks\":0,\"failed\":1,\"mode\":\"self-test\","
              "\"frames\":0,\"buffer_type\":null,\"gpu_import\":false,"
              "\"cpu_readback\":false,\"hashes_first\":[],\"hashes_last\":[],"
              "\"top_colors\":[]}\n", stdout);
        fflush(stdout);
        return 1;
    }

    fd = memfd_create("screencast-consumer-self-test", MFD_CLOEXEC);
    self_check(&ctx, "memfd_create", fd >= 0,
               fd >= 0 ? NULL : "memfd_create failed");
    if (fd < 0)
        goto out;
    ok = ftruncate(fd, (off_t)total) == 0;
    self_check(&ctx, "memfd_ftruncate", ok,
               ok ? NULL : "ftruncate failed");
    if (!ok)
        goto out;
    base = mmap(NULL, (size_t)total, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    ok = base != MAP_FAILED;
    self_check(&ctx, "memfd_mmap", ok, ok ? NULL : "mmap failed");
    if (!ok) {
        base = NULL;
        goto out;
    }

    memset(error, 0, sizeof(error));
    for (i = 0; i < ARRAY_LEN(pixel_formats); i++) {
        if (!fill_test_frame(base, total, offset, 64, 32, (int32_t)stride,
                             &pixel_formats[i])) {
            self_check(&ctx, "synthetic_frame_fill", false, "fill failed");
            continue;
        }
        ok = synthetic_analysis(&consumer, &pixel_formats[i], base, total,
                                offset, required, (int32_t)stride, &stats,
                                error, sizeof(error));
        if (!ok) {
            self_check(&ctx, "pixel_interpretation", false, error);
            continue;
        }
        self_check(&ctx, "pixel_interpretation",
                   stats.hash_set && stats.nonuniform &&
                       stats.pattern_accepted,
                   pixel_formats[i].name);
    }

    /* The same visible pixels for a given format must hash identically with
     * and without chunk offset and row padding differences. */
    fill_test_frame(base, total, 0, 64, 32, (int32_t)stride, &pixel_formats[0]);
    ok = synthetic_analysis(&consumer, &pixel_formats[0], base, total, 0,
                            required, (int32_t)stride, &first, error,
                            sizeof(error));
    self_check(&ctx, "baseline_analysis", ok, ok ? NULL : error);
    fill_test_frame(base, total, offset, 64, 32, (int32_t)stride,
                    &pixel_formats[0]);
    ok = synthetic_analysis(&consumer, &pixel_formats[0], base, total, offset,
                            required, (int32_t)stride, &second, error,
                            sizeof(error));
    self_check(&ctx, "chunk_offset", ok && first.hash_set && second.hash_set &&
                                      first.hash == second.hash,
               "hash must ignore chunk offset changes");

    /* Different row padding must not contribute to the visible hash. */
    {
        uint32_t wide_stride = 64u * 4u + 128u;
        uint32_t wide_required = (32u - 1u) * wide_stride + 64u * 4u;
        struct frame_stats padded;

        if (wide_required <= total) {
            fill_test_frame(base, total, offset, 64, 32, (int32_t)wide_stride,
                            &pixel_formats[0]);
            ok = synthetic_analysis(&consumer, &pixel_formats[0], base, total,
                                    offset, wide_required,
                                    (int32_t)wide_stride, &padded, error,
                                    sizeof(error));
            self_check(&ctx, "row_padding_ignored",
                       ok && padded.hash_set &&
                           padded.hash == second.hash,
                       "visible hash must depend only on width*4 bytes per row");
        } else {
            self_check(&ctx, "row_padding_ignored", false,
                       "synthetic buffer too small");
        }
    }

    /* BGRA/RGBA alpha and x padding bytes do not affect canonical RGB. */
    {
        struct frame_stats rgba;
        struct frame_stats rgba_alpha;

        fill_test_frame(base, total, offset, 64, 32, (int32_t)stride,
                        &pixel_formats[3]);
        ok = synthetic_analysis(&consumer, &pixel_formats[3], base, total,
                                offset, required, (int32_t)stride, &rgba,
                                error, sizeof(error));
        for (uint32_t y = 0; y < 32; y++) {
            uint8_t *row = base + offset + (uint64_t)y * stride;
            for (uint32_t x = 0; x < 64; x++)
                row[x * 4u + 3u] = (uint8_t)(x * 7u + y);
        }
        {
            char error2[256];
            bool ok2 = synthetic_analysis(&consumer, &pixel_formats[3], base,
                                          total, offset, required,
                                          (int32_t)stride, &rgba_alpha, error2,
                                          sizeof(error2));
            self_check(&ctx, "alpha_ignored",
                       ok && ok2 && rgba.hash == rgba_alpha.hash,
                       "alpha must not affect visible RGB hash");
        }
    }

    /* Rejections.  Each must be an explicit validation failure; no zero
     * pixels are invented for malformed buffers. */
    {
        struct cpu_frame frame = {
            .base = base,
            .maxsize = total,
            .offset = offset,
            .size = required,
            .stride = (int32_t)stride,
        };
        struct cpu_frame broken;
        bool rejected;

        broken = frame;
        broken.stride = 0;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_zero_stride", rejected, error);

        broken = frame;
        broken.stride = -4;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_negative_stride", rejected, error);

        broken = frame;
        broken.stride = 64 * 4 - 4;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_short_stride", rejected, error);

        broken = frame;
        broken.size = required - 1u;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_truncated_chunk", rejected, error);

        broken = frame;
        broken.offset = (uint32_t)(total - 4u);
        broken.size = required;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_offset_outside_buffer", rejected, error);

        broken = frame;
        broken.size = (uint32_t)(total - offset + 4u);
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_chunk_past_buffer", rejected, error);

        broken = frame;
        broken.maxsize = (uint64_t)offset + required - 1u;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_visible_past_buffer", rejected, error);

        broken = frame;
        rejected = !validate_cpu_frame(&broken, 0, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_zero_width", rejected, error);

        rejected = find_pixel_format(SPA_VIDEO_FORMAT_I420) == NULL;
        self_check(&ctx, "reject_unsupported_format", rejected,
                   "I420 is intentionally unsupported");

        broken = frame;
        broken.base = NULL;
        rejected = !validate_cpu_frame(&broken, 64, 32, &pixel_formats[0],
                                       error, sizeof(error));
        self_check(&ctx, "reject_missing_mapping", rejected, error);
    }

    /* Hash must change when visible pixels change. */
    {
        struct frame_stats changed;

        fill_test_frame(base, total, offset, 64, 32, (int32_t)stride,
                        &pixel_formats[0]);
        base[offset] ^= 0x01u;
        ok = synthetic_analysis(&consumer, &pixel_formats[0], base, total,
                                offset, required, (int32_t)stride, &changed,
                                error, sizeof(error));
        self_check(&ctx, "hash_changes_with_pixels",
                   ok && changed.hash != second.hash, NULL);
    }

    /* The same synthetic quadrants must produce the expected large colour
     * counts; this exercises the pattern metadata path. */
    {
        fill_test_frame(base, total, offset, 64, 32, (int32_t)stride,
                        &pixel_formats[0]);
        ok = synthetic_analysis(&consumer, &pixel_formats[0], base, total,
                                offset, required, (int32_t)stride, &stats,
                                error, sizeof(error));
        self_check(&ctx, "pattern_counts",
                   ok && stats.pattern_red == 32u * 16u &&
                       stats.pattern_green == 32u * 16u &&
                       stats.pattern_blue == 32u * 16u,
                   "synthetic quadrants have exact block counts");
    }

out:
    if (base != NULL && base != MAP_FAILED)
        munmap(base, (size_t)total);
    if (fd >= 0)
        close(fd);
    colors_destroy(&consumer.colors);

    if (ctx.failed > 0) {
        char message[128];

        snprintf(message, sizeof(message), "%d self-test check(s) failed",
                 ctx.failed);
        emit_error_now("self_test_failed", message);
    }
    fputs("{\"type\":\"summary\",\"passed\":", stdout);
    fputs(ctx.failed == 0 ? "true" : "false", stdout);
    fputs(",\"self_test\":true,\"checks_total\":", stdout);
    fprintf(stdout, "%d", ctx.checks);
    fputs(",\"failed\":", stdout);
    fprintf(stdout, "%d", ctx.failed);
    fputs(",\"checks\":[", stdout);
    for (int j = 0; j < ctx.checks; j++) {
        if (j != 0)
            fputc(',', stdout);
        fputs("{\"name\":", stdout);
        json_escape(stdout, ctx.results[j].name);
        fputs(",\"passed\":", stdout);
        fputs(ctx.results[j].passed ? "true" : "false", stdout);
        if (ctx.results[j].detail[0] != '\0') {
            fputs(",\"detail\":", stdout);
            json_escape(stdout, ctx.results[j].detail);
        }
        fputc('}', stdout);
    }
    fputs("],\"mode\":\"self-test\",\"frames\":0,"
          "\"buffer_type\":null,\"gpu_import\":false,"
          "\"cpu_readback\":false,\"zero_copy\":false,"
          "\"hashes_first\":[],\"hashes_last\":[],\"top_colors\":[],"
          "\"peak_memfd_bytes\":", stdout);
    fprintf(stdout, "%" PRIu64, total);
    fputs("}\n", stdout);
    fflush(stdout);
    return ctx.failed == 0 ? 0 : 1;
}

/* ------------------------------------------------------------------ */
/* options                                                              */

static void print_usage(FILE *out)
{
    fputs(
        "Usage: screencast_consumer --fd FD --node UINT32 [--target-serial STRING]\n"
        "       --mode memfd|dmabuf --frames N --timeout-ms MS\n"
        "       [--render-node /dev/dri/renderD128] [--expect-pattern]\n"
        "       screencast_consumer --self-test\n"
        "\n"
        "The only PipeWire connection is the inherited portal fd.  No image bytes\n"
        "are written; output is NDJSON (format/frame/error/summary events).\n",
        out);
}

static bool parse_u64(const char *text, uint64_t max, uint64_t *value)
{
    char *end = NULL;
    unsigned long long parsed;

    if (text == NULL || *text == '\0' || *text == '-' || *text == '+')
        return false;
    errno = 0;
    parsed = strtoull(text, &end, 10);
    if (errno != 0 || end == text || *end != '\0' || parsed > max)
        return false;
    *value = (uint64_t)parsed;
    return true;
}

static bool is_option(const char *arg, const char *name, const char **inline_value)
{
    size_t length = strlen(name);

    if (strcmp(arg, name) == 0) {
        *inline_value = NULL;
        return true;
    }
    if (strncmp(arg, name, length) == 0 && arg[length] == '=') {
        *inline_value = arg + length + 1u;
        return true;
    }
    return false;
}

static bool take_value(int argc, char **argv, int *index,
                       const char *inline_value, const char **value)
{
    if (inline_value != NULL) {
        *value = inline_value;
        return true;
    }
    if (*index + 1 >= argc)
        return false;
    *index += 1;
    *value = argv[*index];
    return true;
}

static bool parse_options(int argc, char **argv, struct options *options,
                          char *error, size_t error_size)
{
    int i;
    bool capture_option = false;

    memset(options, 0, sizeof(*options));
    options->fd = -1;
    options->render_node = "/dev/dri/renderD128";

    for (i = 1; i < argc; i++) {
        const char *arg = argv[i];
        const char *inline_value = NULL;
        const char *value = NULL;

        if (strcmp(arg, "--help") == 0 || strcmp(arg, "-h") == 0) {
            options->help = true;
            continue;
        }
        if (strcmp(arg, "--self-test") == 0) {
            options->self_test = true;
            continue;
        }
        if (strcmp(arg, "--expect-pattern") == 0) {
            options->expect_pattern = true;
            capture_option = true;
            continue;
        }
        if (is_option(arg, "--fd", &inline_value)) {
            uint64_t parsed;

            if (!take_value(argc, argv, &i, inline_value, &value) ||
                !parse_u64(value, (uint64_t)INT_MAX, &parsed)) {
                snprintf(error, error_size, "--fd requires a valid non-negative integer");
                return false;
            }
            options->fd = (int)parsed;
            capture_option = true;
        } else if (is_option(arg, "--node", &inline_value)) {
            uint64_t parsed;

            if (!take_value(argc, argv, &i, inline_value, &value) ||
                !parse_u64(value, UINT32_MAX, &parsed) || parsed == 0) {
                snprintf(error, error_size, "--node requires a nonzero uint32 node id");
                return false;
            }
            options->node = (uint32_t)parsed;
            capture_option = true;
        } else if (is_option(arg, "--target-serial", &inline_value)) {
            size_t length;

            if (!take_value(argc, argv, &i, inline_value, &value) ||
                *value == '\0') {
                snprintf(error, error_size, "--target-serial requires a non-empty value");
                return false;
            }
            length = strlen(value);
            if (length >= sizeof(options->target_serial)) {
                snprintf(error, error_size, "--target-serial is too long");
                return false;
            }
            memcpy(options->target_serial, value, length + 1u);
            options->have_target_serial = true;
            capture_option = true;
        } else if (is_option(arg, "--mode", &inline_value)) {
            if (!take_value(argc, argv, &i, inline_value, &value) ||
                (strcmp(value, "memfd") != 0 && strcmp(value, "dmabuf") != 0)) {
                snprintf(error, error_size, "--mode must be memfd or dmabuf");
                return false;
            }
            options->mode_name = value;
            options->dmabuf = strcmp(value, "dmabuf") == 0;
            capture_option = true;
        } else if (is_option(arg, "--frames", &inline_value)) {
            uint64_t parsed;

            if (!take_value(argc, argv, &i, inline_value, &value) ||
                !parse_u64(value, MAX_FRAMES, &parsed) || parsed == 0) {
                snprintf(error, error_size,
                         "--frames requires an integer between 1 and %u",
                         MAX_FRAMES);
                return false;
            }
            options->frames = (uint32_t)parsed;
            capture_option = true;
        } else if (is_option(arg, "--timeout-ms", &inline_value)) {
            uint64_t parsed;

            if (!take_value(argc, argv, &i, inline_value, &value) ||
                !parse_u64(value, MAX_TIMEOUT_MS, &parsed) || parsed == 0) {
                snprintf(error, error_size,
                         "--timeout-ms requires an integer between 1 and %u",
                         MAX_TIMEOUT_MS);
                return false;
            }
            options->timeout_ms = (uint32_t)parsed;
            capture_option = true;
        } else if (is_option(arg, "--render-node", &inline_value)) {
            if (!take_value(argc, argv, &i, inline_value, &value) ||
                *value == '\0') {
                snprintf(error, error_size, "--render-node requires a path");
                return false;
            }
            options->render_node = value;
            capture_option = true;
        } else {
            snprintf(error, error_size, "unknown argument: %s", arg);
            return false;
        }
    }

    if (options->help)
        return true;
    if (options->self_test) {
        if (capture_option) {
            snprintf(error, error_size,
                     "--self-test cannot be combined with capture options");
            return false;
        }
        return true;
    }
    if (capture_option && options->fd < 0) {
        snprintf(error, error_size, "--fd is required for capture");
        return false;
    }
    if (!capture_option) {
        snprintf(error, error_size,
                 "no arguments supplied; use --help or --self-test");
        return false;
    }
    if (options->fd < 0 || options->node == 0 || options->mode_name == NULL ||
        options->frames == 0 || options->timeout_ms == 0) {
        snprintf(error, error_size,
                 "capture requires --fd, --node, --mode, --frames and --timeout-ms");
        return false;
    }
    return true;
}

/* ------------------------------------------------------------------ */
/* DMA-BUF / EGL                                                        */

static void *load_proc(const char *name)
{
    return (void *)eglGetProcAddress(name);
}

static void egl_cleanup(struct egl_state *egl)
{
    if (egl->display != EGL_NO_DISPLAY) {
        eglMakeCurrent(egl->display, EGL_NO_SURFACE, EGL_NO_SURFACE,
                       EGL_NO_CONTEXT);
        if (egl->surface != EGL_NO_SURFACE)
            eglDestroySurface(egl->display, egl->surface);
        if (egl->context != EGL_NO_CONTEXT)
            eglDestroyContext(egl->display, egl->context);
        eglTerminate(egl->display);
    }
    egl->surface = EGL_NO_SURFACE;
    egl->context = EGL_NO_CONTEXT;
    egl->display = EGL_NO_DISPLAY;
    if (egl->gbm != NULL)
        gbm_device_destroy(egl->gbm);
    egl->gbm = NULL;
    if (egl->gbm_fd >= 0)
        close(egl->gbm_fd);
    egl->gbm_fd = -1;
    egl->initialized = false;
}

static bool choose_egl_config(EGLDisplay display, const EGLint *attrs,
                              EGLConfig *config)
{
    EGLint count = 0;

    if (!eglChooseConfig(display, attrs, config, 1, &count) || count < 1)
        return false;
    return true;
}

static bool egl_initialize(struct consumer *c, char *error, size_t error_size)
{
    struct egl_state *egl = &c->egl;
    EGLint major = 0;
    EGLint minor = 0;
    EGLint pbuffer_config_attrs[] = {
        EGL_SURFACE_TYPE, EGL_PBUFFER_BIT,
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8,
        EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8,
        EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    };
    EGLint generic_config_attrs[] = {
        EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
        EGL_RED_SIZE, 8,
        EGL_GREEN_SIZE, 8,
        EGL_BLUE_SIZE, 8,
        EGL_ALPHA_SIZE, 8,
        EGL_NONE,
    };
    EGLint context_attrs[] = {
        EGL_CONTEXT_CLIENT_VERSION, 2,
        EGL_NONE,
    };
    EGLint pbuffer_attrs[] = {
        EGL_WIDTH, 1,
        EGL_HEIGHT, 1,
        EGL_NONE,
    };
    const char *extensions;
    bool have_pbuffer = false;
    bool surfaceless = false;

    memset(egl, 0, sizeof(*egl));
    egl->gbm_fd = -1;
    egl->display = EGL_NO_DISPLAY;
    egl->context = EGL_NO_CONTEXT;
    egl->surface = EGL_NO_SURFACE;

    egl->gbm_fd = open(c->opt.render_node, O_RDWR | O_CLOEXEC);
    if (egl->gbm_fd < 0) {
        snprintf(error, error_size, "cannot open render node %s: %s",
                 c->opt.render_node, strerror(errno));
        return false;
    }
    egl->gbm = gbm_create_device(egl->gbm_fd);
    if (egl->gbm == NULL) {
        snprintf(error, error_size, "gbm_create_device failed for %s",
                 c->opt.render_node);
        goto fail;
    }
    egl->get_platform_display =
        (PFNEGLGETPLATFORMDISPLAYEXTPROC)load_proc("eglGetPlatformDisplayEXT");
    if (egl->get_platform_display == NULL) {
        snprintf(error, error_size, "EGL has no eglGetPlatformDisplayEXT");
        goto fail;
    }
    egl->display = egl->get_platform_display(EGL_PLATFORM_GBM_KHR, egl->gbm,
                                             NULL);
    if (egl->display == EGL_NO_DISPLAY) {
        snprintf(error, error_size, "eglGetPlatformDisplayEXT returned no display");
        goto fail;
    }
    if (!eglInitialize(egl->display, &major, &minor)) {
        snprintf(error, error_size, "eglInitialize failed: 0x%x", eglGetError());
        goto fail;
    }
    egl->initialized = true;
    if (!eglBindAPI(EGL_OPENGL_ES_API)) {
        snprintf(error, error_size, "eglBindAPI(OpenGL ES) failed");
        goto fail;
    }
    extensions = eglQueryString(egl->display, EGL_EXTENSIONS);
    surfaceless = extensions != NULL &&
                  strstr(extensions, "EGL_KHR_surfaceless_context") != NULL;
    egl->dma_buf_import =
        extensions != NULL &&
        strstr(extensions, "EGL_EXT_image_dma_buf_import") != NULL;
    egl->dma_buf_modifiers =
        extensions != NULL &&
        strstr(extensions, "EGL_EXT_image_dma_buf_import_modifiers") != NULL;

    have_pbuffer = choose_egl_config(egl->display, pbuffer_config_attrs,
                                     &egl->config);
    if (!have_pbuffer &&
        !choose_egl_config(egl->display, generic_config_attrs, &egl->config)) {
        snprintf(error, error_size, "EGL has no suitable OpenGL ES2 config");
        goto fail;
    }
    egl->context = eglCreateContext(egl->display, egl->config,
                                    EGL_NO_CONTEXT, context_attrs);
    if (egl->context == EGL_NO_CONTEXT) {
        snprintf(error, error_size, "eglCreateContext failed: 0x%x",
                 eglGetError());
        goto fail;
    }
    if (have_pbuffer) {
        egl->surface = eglCreatePbufferSurface(egl->display, egl->config,
                                               pbuffer_attrs);
        if (egl->surface == EGL_NO_SURFACE) {
            snprintf(error, error_size, "eglCreatePbufferSurface failed: 0x%x",
                     eglGetError());
            goto fail;
        }
        if (!eglMakeCurrent(egl->display, egl->surface, egl->surface,
                            egl->context)) {
            snprintf(error, error_size, "eglMakeCurrent failed: 0x%x",
                     eglGetError());
            goto fail;
        }
    } else if (surfaceless) {
        if (!eglMakeCurrent(egl->display, EGL_NO_SURFACE, EGL_NO_SURFACE,
                            egl->context)) {
            snprintf(error, error_size,
                     "surfaceless eglMakeCurrent failed: 0x%x", eglGetError());
            goto fail;
        }
    } else {
        snprintf(error, error_size,
                 "EGL config lacks pbuffer and EGL_KHR_surfaceless_context");
        goto fail;
    }

    egl->create_image = (PFNEGLCREATEIMAGEKHRPROC)load_proc("eglCreateImageKHR");
    egl->destroy_image =
        (PFNEGLDESTROYIMAGEKHRPROC)load_proc("eglDestroyImageKHR");
    egl->image_target_texture =
        (PFNGLEGLIMAGETARGETTEXTURE2DOESPROC)load_proc(
            "glEGLImageTargetTexture2DOES");
    if (!egl->dma_buf_import) {
        snprintf(error, error_size,
                 "EGL_EXT_image_dma_buf_import is not supported by %s",
                 c->opt.render_node);
        goto fail;
    }
    if (egl->create_image == NULL || egl->destroy_image == NULL ||
        egl->image_target_texture == NULL) {
        snprintf(error, error_size, "required EGL/GLES DMA-BUF entry points missing");
        goto fail;
    }
    if (egl->dma_buf_modifiers) {
        egl->query_formats =
            (PFNEGLQUERYDMABUFFORMATSEXTPROC)load_proc(
                "eglQueryDmaBufFormatsEXT");
        egl->query_modifiers =
            (PFNEGLQUERYDMABUFMODIFIERSEXTPROC)load_proc(
                "eglQueryDmaBufModifiersEXT");
        if (egl->query_formats == NULL || egl->query_modifiers == NULL) {
            snprintf(error, error_size,
                     "EGL modifier query entry points missing");
            goto fail;
        }
    }
    return true;

fail:
    egl_cleanup(egl);
    return false;
}

static bool modifier_supported(const struct consumer *c, uint32_t fourcc,
                               uint64_t modifier)
{
    uint32_t i;
    uint32_t j;

    for (i = 0; i < c->n_formats; i++) {
        if (c->formats[i].pix->drm_fourcc != fourcc)
            continue;
        if (!c->egl.dma_buf_modifiers || c->formats[i].n_modifiers == 0) {
            /* Without modifier queries, or when a format exposes no explicit
             * modifiers, only the implicit/linear layout can be imported
             * without modifier attributes. */
            return modifier == DRM_FORMAT_MOD_LINEAR || modifier == 0;
        }
        for (j = 0; j < c->formats[i].n_modifiers; j++) {
            if (c->formats[i].modifiers[j] == modifier)
                return true;
        }
        return false;
    }
    return false;
}

static bool query_supported_formats(struct consumer *c, char *error,
                                    size_t error_size)
{
    uint32_t i;
    bool any = false;

    c->n_formats = 0;
    if (!c->egl.dma_buf_modifiers) {
        /* No query extension: advertise the four packed RGB layouts and
         * reject any non-linear modifier at import time. */
        for (i = 0; i < ARRAY_LEN(pixel_formats); i++) {
            c->formats[c->n_formats].pix = &pixel_formats[i];
            c->formats[c->n_formats].fourcc_supported = true;
            c->formats[c->n_formats].n_modifiers = 0;
            c->n_formats++;
            any = true;
        }
        return any;
    }

    {
        EGLint n_formats = 0;
        EGLint *drm_formats = NULL;

        if (!c->egl.query_formats(c->egl.display, 0, NULL, &n_formats) ||
            n_formats <= 0) {
            snprintf(error, error_size,
                     "eglQueryDmaBufFormatsEXT returned no formats");
            return false;
        }
        drm_formats = calloc((size_t)n_formats, sizeof(*drm_formats));
        if (drm_formats == NULL) {
            snprintf(error, error_size, "cannot allocate format query result");
            return false;
        }
        if (!c->egl.query_formats(c->egl.display, n_formats, drm_formats,
                                  &n_formats)) {
            snprintf(error, error_size,
                     "eglQueryDmaBufFormatsEXT failed: 0x%x", eglGetError());
            free(drm_formats);
            return false;
        }
        for (i = 0; i < ARRAY_LEN(pixel_formats); i++) {
            bool found = false;
            EGLint f;

            for (f = 0; f < n_formats; f++) {
                if ((uint32_t)drm_formats[f] == pixel_formats[i].drm_fourcc) {
                    found = true;
                    break;
                }
            }
            if (!found)
                continue;
            if (c->n_formats >= ARRAY_LEN(c->formats))
                break;
            c->formats[c->n_formats].pix = &pixel_formats[i];
            c->formats[c->n_formats].fourcc_supported = true;
            {
                EGLint n_modifiers = 0;
                EGLuint64KHR *modifiers = NULL;

                if (c->egl.query_modifiers(c->egl.display,
                                           (EGLint)pixel_formats[i].drm_fourcc,
                                           0, NULL, NULL, &n_modifiers) &&
                    n_modifiers > 0) {
                    modifiers = calloc((size_t)n_modifiers, sizeof(*modifiers));
                    if (modifiers != NULL &&
                        c->egl.query_modifiers(
                            c->egl.display,
                            (EGLint)pixel_formats[i].drm_fourcc,
                            n_modifiers, modifiers, NULL, &n_modifiers)) {
                        uint32_t m;
                        uint32_t limit = n_modifiers;

                        if (limit > MAX_MODIFIERS_PER_FORMAT)
                            limit = MAX_MODIFIERS_PER_FORMAT;
                        for (m = 0; m < limit; m++)
                            c->formats[c->n_formats].modifiers[m] =
                                (uint64_t)modifiers[m];
                        c->formats[c->n_formats].n_modifiers = limit;
                    }
                    free(modifiers);
                }
            }
            c->n_formats++;
            any = true;
        }
        free(drm_formats);
    }
    if (!any) {
        snprintf(error, error_size,
                 "EGL supports none of BGRx/BGRA/RGBx/RGBA DMA-BUF formats");
        return false;
    }
    return true;
}

static bool build_format_params(struct consumer *c, uint8_t *buffer,
                                size_t buffer_size, const struct spa_pod **params,
                                uint32_t *n_params, char *error,
                                size_t error_size)
{
    struct spa_pod_builder builder = SPA_POD_BUILDER_INIT(buffer, buffer_size);
    uint32_t i;
    uint32_t index = 0;

    for (i = 0; i < c->n_formats; i++) {
        struct spa_pod_frame frame;
        struct spa_rectangle minimum = SPA_RECTANGLE(1, 1);
        struct spa_rectangle maximum =
            SPA_RECTANGLE(MAX_DIMENSION, MAX_DIMENSION);
        struct spa_rectangle preferred = SPA_RECTANGLE(1920, 1080);

        spa_pod_builder_push_object(&builder, &frame, SPA_TYPE_OBJECT_Format,
                                    SPA_PARAM_EnumFormat);
        spa_pod_builder_add(&builder,
                            SPA_FORMAT_mediaType, SPA_POD_Id(SPA_MEDIA_TYPE_video),
                            SPA_FORMAT_mediaSubtype,
                            SPA_POD_Id(SPA_MEDIA_SUBTYPE_raw),
                            SPA_FORMAT_VIDEO_format,
                            SPA_POD_Id(c->formats[i].pix->spa),
                            SPA_FORMAT_VIDEO_size,
                            SPA_POD_CHOICE_RANGE_Rectangle(&preferred, &minimum,
                                                          &maximum),
                            0);
        if (c->opt.dmabuf && c->egl.dma_buf_modifiers &&
            c->formats[i].n_modifiers > 0) {
            struct spa_pod_frame choice;
            uint32_t m;

            spa_pod_builder_prop(&builder, SPA_FORMAT_VIDEO_modifier, 0);
            spa_pod_builder_push_choice(&builder, &choice, SPA_CHOICE_Enum, 0);
            for (m = 0; m < c->formats[i].n_modifiers; m++)
                spa_pod_builder_long(&builder,
                                     (int64_t)c->formats[i].modifiers[m]);
            spa_pod_builder_pop(&builder, &choice);
        }
        params[index++] = spa_pod_builder_pop(&builder, &frame);
        if (index >= 16u) {
            snprintf(error, error_size, "too many format parameters");
            return false;
        }
    }
    *n_params = index;
    if (index == 0) {
        snprintf(error, error_size, "no format parameters to negotiate");
        return false;
    }
    return true;
}

/* ------------------------------------------------------------------ */
/* frame emission and final summary                                     */

static void store_hash_sample(struct consumer *c, uint64_t hash)
{
    if (c->n_hashes_first < MAX_HASH_SAMPLES)
        c->hashes_first[c->n_hashes_first++] = hash;
    c->hashes_last[c->n_hashes_last % MAX_HASH_SAMPLES] = hash;
    c->n_hashes_last++;
}

static void emit_frame(struct consumer *c, const struct frame_stats *stats,
                       bool accepted)
{
    fputs("{\"type\":\"frame\",\"index\":", stdout);
    fprintf(stdout, "%u", c->frame_events);
    fputs(",\"accepted\":", stdout);
    fputs(accepted ? "true" : "false", stdout);
    fputs(",\"hash\":\"", stdout);
    fprintf(stdout, "%016" PRIx64, stats->hash);
    fputs("\",\"width\":", stdout);
    fprintf(stdout, "%u", stats->width);
    fputs(",\"height\":", stdout);
    fprintf(stdout, "%u", stats->height);
    fputs(",\"stride\":", stdout);
    fprintf(stdout, "%u", stats->stride);
    fputs(",\"buffer_type\":", stdout);
    json_string_or_null(stdout, c->buffer_type);
    fputs(",\"format\":", stdout);
    json_string_or_null(stdout, c->pix != NULL ? c->pix->name : NULL);
    fputs(",\"modifier\":", stdout);
    if (c->modifier_present) {
        char modifier[32];

        format_modifier(modifier, sizeof(modifier), c->modifier);
        json_escape(stdout, modifier);
    } else {
        fputs("null", stdout);
    }
    fputs(",\"nonuniform\":", stdout);
    fputs(stats->nonuniform ? "true" : "false", stdout);
    fputs(",\"distinct_colors\":", stdout);
    fprintf(stdout, "%u", stats->distinct_colors);
    fputs(",\"overflow_pixels\":", stdout);
    fprintf(stdout, "%" PRIu64, stats->overflow_pixels);
    fputs(",\"dominant\":", stdout);
    json_rgb(stdout, stats->dominant_rgb);
    fputs(",\"dominant_fraction\":", stdout);
    fprintf(stdout, "%.6f", stats->dominant_fraction);
    fputs(",\"top_colors\":", stdout);
    print_top_colors(stdout, stats);
    fputs(",\"pattern\":", stdout);
    print_pattern(stdout, stats);
    fputs(",\"gpu_import\":", stdout);
    fputs(c->opt.dmabuf ? "true" : "false", stdout);
    fputs(",\"cpu_readback\":true", stdout);
    fputs("}\n", stdout);
    fflush(stdout);
}

static void emit_format(struct consumer *c)
{
    char modifier[32];

    if (c->format_reported)
        return;
    c->format_reported = true;
    format_modifier(modifier, sizeof(modifier), c->modifier);
    fputs("{\"type\":\"format\",\"format\":", stdout);
    json_escape(stdout, c->pix != NULL ? c->pix->name : NULL);
    fputs(",\"pixel_layout\":", stdout);
    json_string_or_null(stdout, c->pix != NULL ? c->pix->layout : NULL);
    fputs(",\"width\":", stdout);
    fprintf(stdout, "%u", c->video.size.width);
    fputs(",\"height\":", stdout);
    fprintf(stdout, "%u", c->video.size.height);
    fputs(",\"modifier\":", stdout);
    if (c->modifier_present)
        json_escape(stdout, modifier);
    else
        fputs("null", stdout);
    fputs(",\"modifier_known\":", stdout);
    fputs(c->modifier_present ? "true" : "false", stdout);
    fputs(",\"mode\":", stdout);
    json_escape(stdout, c->capture_mode);
    fputs(",\"expected_buffer_type\":", stdout);
    json_escape(stdout, c->opt.dmabuf ? "DmaBuf" : "MemFd");
    fputs(",\"target\":\"spa/param/Format\"}\n", stdout);
    fflush(stdout);
}

static void emit_summary(struct consumer *c, bool passed)
{
    uint32_t i;
    bool emit_error = c->have_error && !c->error_emitted;

    if (emit_error) {
        fputs("{\"type\":\"error\",\"code\":", stdout);
        json_escape(stdout, c->error_code);
        fputs(",\"message\":", stdout);
        json_escape(stdout, c->error_message);
        fputs("}\n", stdout);
        c->error_emitted = true;
    }

    fputs("{\"type\":\"summary\",\"passed\":", stdout);
    fputs(passed ? "true" : "false", stdout);
    fputs(",\"frames\":", stdout);
    fprintf(stdout, "%u", c->frames);
    fputs(",\"requested_frames\":", stdout);
    fprintf(stdout, "%u", c->opt.frames);
    fputs(",\"buffers_seen\":", stdout);
    fprintf(stdout, "%u", c->buffers_seen);
    fputs(",\"skipped_uniform\":", stdout);
    fprintf(stdout, "%u", c->skipped_uniform);
    fputs(",\"skipped_pattern\":", stdout);
    fprintf(stdout, "%u", c->skipped_pattern);
    fputs(",\"skipped_empty\":", stdout);
    fprintf(stdout, "%u", c->skipped_empty);
    fputs(",\"mode\":", stdout);
    json_escape(stdout, c->capture_mode);
    fputs(",\"buffer_type\":", stdout);
    json_string_or_null(stdout, c->buffer_type);
    fputs(",\"format\":", stdout);
    json_string_or_null(stdout, c->pix != NULL ? c->pix->name : NULL);
    fputs(",\"modifier\":", stdout);
    if (c->have_format && c->modifier_present) {
        char modifier[32];

        format_modifier(modifier, sizeof(modifier), c->modifier);
        json_escape(stdout, modifier);
    } else {
        fputs("null", stdout);
    }
    fputs(",\"gpu_import\":", stdout);
    fputs(c->gpu_imported_any ? "true" : "false", stdout);
    fputs(",\"cpu_readback\":", stdout);
    fputs(c->cpu_readback_any ? "true" : "false", stdout);
    fputs(",\"zero_copy\":false", stdout);
    fputs(",\"hash_changes\":", stdout);
    fprintf(stdout, "%u", c->hash_changes);
    fputs(",\"hashes_first\":[", stdout);
    for (i = 0; i < c->n_hashes_first; i++) {
        if (i != 0)
            fputc(',', stdout);
        fprintf(stdout, "\"%016" PRIx64 "\"", c->hashes_first[i]);
    }
    fputs("],\"hashes_last\":[", stdout);
    {
        uint32_t count = c->n_hashes_last < MAX_HASH_SAMPLES
                             ? c->n_hashes_last
                             : MAX_HASH_SAMPLES;
        uint32_t start = c->n_hashes_last > MAX_HASH_SAMPLES
                             ? c->n_hashes_last % MAX_HASH_SAMPLES
                             : 0;

        for (i = 0; i < count; i++) {
            uint32_t slot = (start + i) % MAX_HASH_SAMPLES;

            if (i != 0)
                fputc(',', stdout);
            fprintf(stdout, "\"%016" PRIx64 "\"", c->hashes_last[slot]);
        }
    }
    fputs("],\"top_colors\":", stdout);
    if (c->have_last_stats)
        print_top_colors(stdout, &c->last_stats);
    else
        fputs("[]", stdout);
    fputs(",\"pattern\":", stdout);
    if (c->have_last_stats)
        print_pattern(stdout, &c->last_stats);
    else
        fputs("null", stdout);
    fputs(",\"expect_pattern\":", stdout);
    fputs(c->opt.expect_pattern ? "true" : "false", stdout);
    fputs(",\"target_by_serial\":", stdout);
    fputs(c->target_by_serial ? "true" : "false", stdout);
    fputs(",\"node\":", stdout);
    fprintf(stdout, "%u", c->opt.node);
    fputs(",\"render_node\":", stdout);
    json_string_or_null(stdout, c->opt.dmabuf ? c->opt.render_node : NULL);
    fputs(",\"error\":", stdout);
    if (c->have_error) {
        fputs("{\"code\":", stdout);
        json_escape(stdout, c->error_code);
        fputs(",\"message\":", stdout);
        json_escape(stdout, c->error_message);
        fputs("}", stdout);
    } else {
        fputs("null", stdout);
    }
    fputs("}\n", stdout);
    fflush(stdout);
}

/* ------------------------------------------------------------------ */
/* PipeWire stream                                                      */

static void finish_loop(struct consumer *c)
{
    if (c->finished)
        return;
    c->finished = true;
    if (c->loop != NULL)
        pw_main_loop_quit(c->loop);
}

static void record_error_and_finish(struct consumer *c, const char *code,
                                    const char *message)
{
    set_and_emit_error(c, code, "%s", message);
    finish_loop(c);
}

static void on_timer(void *data, uint64_t expirations)
{
    struct consumer *c = data;

    (void)expirations;
    if (c->finished)
        return;
    c->deadline_hit = true;
    finish_loop(c);
}

static void on_signal(void *data, int signal_number)
{
    struct consumer *c = data;

    c->interrupted = true;
    set_error(c, "interrupted", "caught signal %d", signal_number);
    finish_loop(c);
}

static void on_stream_state_changed(void *data, enum pw_stream_state old,
                                    enum pw_stream_state state,
                                    const char *error)
{
    struct consumer *c = data;

    (void)old;
    if (state == PW_STREAM_STATE_ERROR) {
        set_and_emit_error(c, "stream_error", "%s",
                           error != NULL ? error : "PipeWire stream error");
        finish_loop(c);
    }
}

static bool apply_buffers_param(struct consumer *c)
{
    uint8_t buffer[512];
    struct spa_pod_builder builder =
        SPA_POD_BUILDER_INIT(buffer, sizeof(buffer));
    const struct spa_pod *params[1];
    uint32_t data_type = c->opt.dmabuf ? (1u << SPA_DATA_DmaBuf)
                                       : (1u << SPA_DATA_MemFd);

    if (c->opt.dmabuf) {
        params[0] = spa_pod_builder_add_object(
            &builder, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
            SPA_PARAM_BUFFERS_buffers,
            SPA_POD_CHOICE_RANGE_Int(4, 2, 8),
            SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
            SPA_PARAM_BUFFERS_dataType,
            SPA_POD_CHOICE_FLAGS_Int((int32_t)data_type), 0);
    } else {
        uint32_t size = c->video.size.width * 4u;
        uint64_t total = (uint64_t)size * (uint64_t)c->video.size.height;

        if (total > (uint64_t)INT32_MAX) {
            set_and_emit_error(c, "buffer_too_large",
                               "visible frame is too large for a MemFd buffer");
            return false;
        }
        params[0] = spa_pod_builder_add_object(
            &builder, SPA_TYPE_OBJECT_ParamBuffers, SPA_PARAM_Buffers,
            SPA_PARAM_BUFFERS_buffers,
            SPA_POD_CHOICE_RANGE_Int(4, 2, 8),
            SPA_PARAM_BUFFERS_blocks, SPA_POD_Int(1),
            SPA_PARAM_BUFFERS_size, SPA_POD_Int((int32_t)total),
            SPA_PARAM_BUFFERS_stride, SPA_POD_Int((int32_t)size),
            SPA_PARAM_BUFFERS_align, SPA_POD_Int(16),
            SPA_PARAM_BUFFERS_dataType,
            SPA_POD_CHOICE_FLAGS_Int((int32_t)data_type), 0);
    }
    return pw_stream_update_params(c->stream, params, 1) >= 0;
}

static void on_stream_param_changed(void *data, uint32_t id,
                                    const struct spa_pod *param)
{
    struct consumer *c = data;
    struct spa_video_info info;
    const struct pixel_format *pix;
    uint32_t media_type = 0;
    uint32_t media_subtype = 0;

    if (id != SPA_PARAM_Format || param == NULL)
        return;
    if (spa_format_parse(param, &media_type, &media_subtype) < 0) {
        record_error_and_finish(c, "format_parse",
                                "could not parse negotiated SPA format");
        return;
    }
    if (media_type != SPA_MEDIA_TYPE_video ||
        media_subtype != SPA_MEDIA_SUBTYPE_raw) {
        record_error_and_finish(c, "unsupported_media_type",
                                "negotiated media type is not raw video");
        return;
    }
    memset(&info, 0, sizeof(info));
    if (spa_format_video_raw_parse(param, &info.info.raw) < 0) {
        record_error_and_finish(c, "format_parse",
                                "could not parse raw video format");
        return;
    }
    pix = find_pixel_format(info.info.raw.format);
    if (pix == NULL) {
        char message[128];

        snprintf(message, sizeof(message),
                 "unsupported negotiated pixel format %u", info.info.raw.format);
        record_error_and_finish(c, "unsupported_format", message);
        return;
    }
    if (info.info.raw.size.width == 0 || info.info.raw.size.height == 0 ||
        info.info.raw.size.width > MAX_DIMENSION ||
        info.info.raw.size.height > MAX_DIMENSION) {
        record_error_and_finish(c, "invalid_dimensions",
                                "negotiated dimensions are empty or excessive");
        return;
    }
    if (c->have_format) {
        if (c->pix != pix || c->video.size.width != info.info.raw.size.width ||
            c->video.size.height != info.info.raw.size.height) {
            record_error_and_finish(c, "format_changed",
                                    "stream changed format after the first frame");
        }
        return;
    }
    c->pix = pix;
    c->video = info.info.raw;
    c->modifier_present =
        (info.info.raw.flags & SPA_VIDEO_FLAG_MODIFIER) != 0;
    c->modifier = info.info.raw.modifier;
    c->dmabuf_negotiated = c->opt.dmabuf;
    c->have_format = true;

    if (c->opt.dmabuf) {
        if (!modifier_supported(c, pix->drm_fourcc, c->modifier)) {
            char message[192];

            snprintf(message, sizeof(message),
                     "negotiated modifier 0x%016" PRIx64
                     " is not importable for %s",
                     c->modifier, pix->name);
            record_error_and_finish(c, "unsupported_modifier", message);
            return;
        }
    } else if (c->modifier_present && c->modifier != 0) {
        record_error_and_finish(c, "unexpected_modifier",
                                "MemFd mode received a non-linear modifier");
        return;
    }

    emit_format(c);
    if (!apply_buffers_param(c)) {
        record_error_and_finish(c, "buffer_param",
                                "could not request the negotiated buffer type");
    }
}

static void store_frame_stats(struct consumer *c, const struct frame_stats *stats)
{
    uint64_t hash = stats->hash;

    c->frames++;
    c->last_stats = *stats;
    c->have_last_stats = true;
    store_hash_sample(c, hash);
    if (c->have_last_hash && c->last_hash != hash)
        c->hash_changes++;
    c->last_hash = hash;
    c->have_last_hash = true;
}

static bool should_stop_after_frame(const struct consumer *c)
{
    if (c->frames < c->opt.frames)
        return false;
    if (c->opt.expect_pattern && c->hash_changes == 0)
        return false;
    return true;
}

static void handle_cpu_buffer(struct consumer *c, struct pw_buffer *buffer)
{
    struct spa_buffer *spa = buffer->buffer;
    struct spa_data *data;
    struct spa_chunk *chunk;
    struct cpu_frame frame;
    struct frame_stats stats;
    char error[256];
    void *mapped = NULL;
    size_t map_size = 0;
    bool accepted;

    if (spa == NULL || spa->n_datas != 1u) {
        record_error_and_finish(c, "unsupported_layout",
                                "MemFd frame must have exactly one data plane");
        return;
    }
    data = &spa->datas[0];
    if (data->type != SPA_DATA_MemFd) {
        char message[160];

        snprintf(message, sizeof(message),
                 "MemFd mode rejected source buffer type %s (%u)",
                 spa_data_type_name(data->type), data->type);
        record_error_and_finish(c, "unsupported_buffer_type", message);
        return;
    }
    if (data->fd < 0) {
        record_error_and_finish(c, "invalid_memfd",
                                "MemFd buffer has no file descriptor");
        return;
    }
    if (data->maxsize == 0 || data->chunk == NULL) {
        record_error_and_finish(c, "invalid_buffer",
                                "MemFd buffer has no size or chunk");
        return;
    }
    chunk = data->chunk;
    if (chunk->size == 0) {
        c->skipped_empty++;
        return;
    }
    if (data->mapoffset != 0 &&
        (data->mapoffset % (uint32_t)sysconf(_SC_PAGESIZE)) != 0) {
        record_error_and_finish(c, "invalid_mapoffset",
                                "MemFd mapoffset is not page aligned");
        return;
    }
    if (data->data != NULL) {
        frame.base = data->data;
    } else {
        long page_size = sysconf(_SC_PAGESIZE);

        if (page_size <= 0 || data->maxsize > (uint32_t)SIZE_MAX) {
            record_error_and_finish(c, "invalid_mapping",
                                    "cannot map MemFd buffer");
            return;
        }
        mapped = mmap(NULL, data->maxsize, PROT_READ, MAP_SHARED, data->fd,
                      (off_t)data->mapoffset);
        if (mapped == MAP_FAILED) {
            record_error_and_finish(c, "mmap_failed",
                                    "mmap of MemFd buffer failed");
            return;
        }
        frame.base = mapped;
        map_size = data->maxsize;
    }
    frame.maxsize = data->maxsize;
    frame.offset = chunk->offset;
    frame.size = chunk->size;
    frame.stride = chunk->stride;
    c->buffer_type = "MemFd";

    if ((chunk->flags & SPA_CHUNK_FLAG_CORRUPTED) != 0) {
        record_error_and_finish(c, "corrupted_buffer",
                                "source marked the MemFd chunk corrupted");
        goto out;
    }
    if (!analyze_cpu_frame(c, &frame, &stats, error, sizeof(error))) {
        record_error_and_finish(c, "invalid_layout", error);
        goto out;
    }
    c->buffers_seen++;
    c->cpu_readback_any = true;
    accepted = true;
    if (c->opt.expect_pattern) {
        if (!stats.nonuniform) {
            c->skipped_uniform++;
            accepted = false;
        } else if (!stats.pattern_accepted) {
            c->skipped_pattern++;
            accepted = false;
        }
    }
    c->frame_events++;
    emit_frame(c, &stats, accepted);
    if (accepted)
        store_frame_stats(c, &stats);
    if (accepted && should_stop_after_frame(c))
        finish_loop(c);

out:
    if (mapped != NULL && mapped != MAP_FAILED)
        munmap(mapped, map_size);
}

static bool import_dmabuf_frame(struct consumer *c, struct spa_buffer *spa,
                                const struct spa_data *data,
                                const struct spa_chunk *chunk,
                                uint8_t **pixels_out, char *error,
                                size_t error_size)
{
    struct egl_state *egl = &c->egl;
    EGLImageKHR image = EGL_NO_IMAGE_KHR;
    GLuint texture = 0;
    GLuint framebuffer = 0;
    GLint attrs[64];
    int n_attrs = 0;
    uint64_t offset;
    uint64_t stride;
    uint64_t required;
    uint64_t row_bytes = (uint64_t)c->video.size.width * 4u;
    uint8_t *pixels = NULL;
    GLenum gl_error;
    EGLint egl_error;
    bool ok = false;

    (void)spa;
    if (data->type != SPA_DATA_DmaBuf) {
        snprintf(error, error_size, "DMA-BUF mode rejected source buffer type %s",
                 spa_data_type_name(data->type));
        return false;
    }
    if (data->fd < 0) {
        snprintf(error, error_size, "DMA-BUF buffer has no file descriptor");
        return false;
    }
    if (chunk->stride <= 0) {
        snprintf(error, error_size, "DMA-BUF has non-positive stride");
        return false;
    }
    stride = (uint64_t)chunk->stride;
    if (stride < row_bytes) {
        snprintf(error, error_size, "DMA-BUF stride is smaller than width*4");
        return false;
    }
    required = (uint64_t)(c->video.size.height - 1u) * stride + row_bytes;
    if (required > UINT32_MAX) {
        snprintf(error, error_size, "DMA-BUF visible size overflows");
        return false;
    }
    offset = (uint64_t)data->mapoffset + (uint64_t)chunk->offset;
    if (data->maxsize != 0 &&
        offset + required > (uint64_t)data->maxsize) {
        snprintf(error, error_size,
                 "DMA-BUF visible rows exceed reported buffer size");
        return false;
    }
    if (chunk->size < required) {
        snprintf(error, error_size, "DMA-BUF chunk is truncated");
        return false;
    }
    if (!modifier_supported(c, c->pix->drm_fourcc, c->modifier)) {
        snprintf(error, error_size, "DMA-BUF modifier is not supported");
        return false;
    }

    attrs[n_attrs++] = EGL_WIDTH;
    attrs[n_attrs++] = (GLint)c->video.size.width;
    attrs[n_attrs++] = EGL_HEIGHT;
    attrs[n_attrs++] = (GLint)c->video.size.height;
    attrs[n_attrs++] = EGL_LINUX_DRM_FOURCC_EXT;
    attrs[n_attrs++] = (GLint)c->pix->drm_fourcc;
    attrs[n_attrs++] = EGL_DMA_BUF_PLANE0_FD_EXT;
    attrs[n_attrs++] = data->fd;
    attrs[n_attrs++] = EGL_DMA_BUF_PLANE0_OFFSET_EXT;
    attrs[n_attrs++] = (GLint)offset;
    attrs[n_attrs++] = EGL_DMA_BUF_PLANE0_PITCH_EXT;
    attrs[n_attrs++] = (GLint)stride;
    if (egl->dma_buf_modifiers) {
        attrs[n_attrs++] = EGL_DMA_BUF_PLANE0_MODIFIER_LO_EXT;
        attrs[n_attrs++] = (GLint)(uint32_t)(c->modifier & 0xffffffffu);
        attrs[n_attrs++] = EGL_DMA_BUF_PLANE0_MODIFIER_HI_EXT;
        attrs[n_attrs++] = (GLint)(uint32_t)(c->modifier >> 32);
    }
    attrs[n_attrs++] = EGL_NONE;

    image = egl->create_image(egl->display, EGL_NO_CONTEXT,
                              EGL_LINUX_DMA_BUF_EXT, NULL, attrs);
    if (image == EGL_NO_IMAGE_KHR) {
        egl_error = eglGetError();
        snprintf(error, error_size,
                 "eglCreateImageKHR failed for %s modifier 0x%016" PRIx64
                 ": 0x%x",
                 c->pix->name, c->modifier, egl_error);
        goto out;
    }

    glGetError();
    glGenTextures(1, &texture);
    if (texture == 0) {
        snprintf(error, error_size, "glGenTextures failed");
        goto out;
    }
    glBindTexture(GL_TEXTURE_2D, texture);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MIN_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_MAG_FILTER, GL_NEAREST);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_S, GL_CLAMP_TO_EDGE);
    glTexParameteri(GL_TEXTURE_2D, GL_TEXTURE_WRAP_T, GL_CLAMP_TO_EDGE);
    egl->image_target_texture(GL_TEXTURE_2D, (GLeglImageOES)image);
    gl_error = glGetError();
    if (gl_error != GL_NO_ERROR) {
        snprintf(error, error_size,
                 "glEGLImageTargetTexture2DOES failed: 0x%x", gl_error);
        goto out;
    }
    glGenFramebuffers(1, &framebuffer);
    if (framebuffer == 0) {
        snprintf(error, error_size, "glGenFramebuffers failed");
        goto out;
    }
    glBindFramebuffer(GL_FRAMEBUFFER, framebuffer);
    glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0,
                           GL_TEXTURE_2D, texture, 0);
    gl_error = glCheckFramebufferStatus(GL_FRAMEBUFFER);
    if (gl_error != GL_FRAMEBUFFER_COMPLETE) {
        snprintf(error, error_size,
                 "EGL image framebuffer is incomplete: 0x%x", gl_error);
        goto out;
    }
    pixels = malloc((size_t)c->video.size.width *
                    (size_t)c->video.size.height * 4u);
    if (pixels == NULL) {
        snprintf(error, error_size, "cannot allocate glReadPixels buffer");
        goto out;
    }
    glPixelStorei(GL_PACK_ALIGNMENT, 4);
    glReadPixels(0, 0, (GLsizei)c->video.size.width,
                 (GLsizei)c->video.size.height, GL_RGBA, GL_UNSIGNED_BYTE,
                 pixels);
    gl_error = glGetError();
    if (gl_error != GL_NO_ERROR) {
        snprintf(error, error_size, "glReadPixels failed: 0x%x", gl_error);
        goto out;
    }
    *pixels_out = pixels;
    pixels = NULL;
    ok = true;

out:
    if (framebuffer != 0)
        glDeleteFramebuffers(1, &framebuffer);
    if (texture != 0)
        glDeleteTextures(1, &texture);
    if (image != EGL_NO_IMAGE_KHR)
        egl->destroy_image(egl->display, image);
    glBindFramebuffer(GL_FRAMEBUFFER, 0);
    free(pixels);
    return ok;
}

static void handle_dmabuf_buffer(struct consumer *c, struct pw_buffer *buffer)
{
    struct spa_buffer *spa = buffer->buffer;
    struct spa_data *data;
    struct spa_chunk *chunk;
    struct frame_stats stats;
    uint8_t *pixels = NULL;
    char error[256];
    bool accepted;

    if (spa == NULL || spa->n_datas != 1u) {
        record_error_and_finish(c, "unsupported_layout",
                                "DMA-BUF frame must have exactly one data plane");
        return;
    }
    data = &spa->datas[0];
    if (data->chunk == NULL) {
        record_error_and_finish(c, "invalid_buffer",
                                "DMA-BUF buffer has no chunk");
        return;
    }
    chunk = data->chunk;
    if (chunk->size == 0) {
        c->skipped_empty++;
        return;
    }
    if ((chunk->flags & SPA_CHUNK_FLAG_CORRUPTED) != 0) {
        record_error_and_finish(c, "corrupted_buffer",
                                "source marked the DMA-BUF chunk corrupted");
        return;
    }
    if (!c->egl.initialized) {
        record_error_and_finish(c, "egl_unavailable",
                                "DMA-BUF received without an EGL context");
        return;
    }
    memset(&stats, 0, sizeof(stats));
    if (!import_dmabuf_frame(c, spa, data, chunk, &pixels, error,
                             sizeof(error))) {
        record_error_and_finish(c, "dmabuf_import_failed", error);
        return;
    }
    c->gpu_imported_any = true;
    c->cpu_readback_any = true;
    c->buffer_type = "DmaBuf";

    {
        struct cpu_frame frame = {
            .base = pixels,
            .maxsize = (uint64_t)c->video.size.width *
                       (uint64_t)c->video.size.height * 4u,
            .offset = 0,
            .size = (uint32_t)((uint64_t)c->video.size.width *
                               (uint64_t)c->video.size.height * 4u),
            .stride = (int32_t)(c->video.size.width * 4u),
        };

        c->pix = &pixel_formats[3]; /* glReadPixels yields RGBA bytes */
        if (!analyze_cpu_frame(c, &frame, &stats, error, sizeof(error))) {
            record_error_and_finish(c, "dmabuf_readback_invalid", error);
            free(pixels);
            return;
        }
        c->pix = find_pixel_format(c->video.format);
    }
    free(pixels);
    c->buffers_seen++;
    c->cpu_readback_any = true;
    accepted = true;
    if (c->opt.expect_pattern) {
        if (!stats.nonuniform) {
            c->skipped_uniform++;
            accepted = false;
        } else if (!stats.pattern_accepted) {
            c->skipped_pattern++;
            accepted = false;
        }
    }
    c->frame_events++;
    emit_frame(c, &stats, accepted);
    if (accepted)
        store_frame_stats(c, &stats);
    if (accepted && should_stop_after_frame(c))
        finish_loop(c);
}

static void on_stream_process(void *data)
{
    struct consumer *c = data;
    struct pw_buffer *buffer;

    if (c->finished)
        return;
    if (monotonic_ns() >= c->deadline_ns) {
        c->deadline_hit = true;
        finish_loop(c);
        return;
    }
    buffer = pw_stream_dequeue_buffer(c->stream);
    if (buffer == NULL)
        return;
    if (!c->have_format) {
        record_error_and_finish(c, "missing_format",
                                "received a buffer before format negotiation");
        (void)pw_stream_queue_buffer(c->stream, buffer);
        return;
    }
    if (c->opt.dmabuf)
        handle_dmabuf_buffer(c, buffer);
    else
        handle_cpu_buffer(c, buffer);
    /* Always return the buffer, including on every error path. */
    (void)pw_stream_queue_buffer(c->stream, buffer);
}

static struct pw_stream_events stream_events = {
    PW_VERSION_STREAM_EVENTS,
    .state_changed = on_stream_state_changed,
    .param_changed = on_stream_param_changed,
    .process = on_stream_process,
};

/* ------------------------------------------------------------------ */
/* lifecycle                                                            */

static void consumer_cleanup(struct consumer *c)
{
    if (c->sigint_source != NULL && c->loop != NULL) {
        pw_loop_destroy_source(pw_main_loop_get_loop(c->loop),
                               c->sigint_source);
        c->sigint_source = NULL;
    }
    if (c->sigterm_source != NULL && c->loop != NULL) {
        pw_loop_destroy_source(pw_main_loop_get_loop(c->loop),
                               c->sigterm_source);
        c->sigterm_source = NULL;
    }
    if (c->timer != NULL && c->loop != NULL) {
        pw_loop_destroy_source(pw_main_loop_get_loop(c->loop), c->timer);
        c->timer = NULL;
    }
    if (c->stream != NULL) {
        spa_hook_remove(&c->stream_listener);
        pw_stream_destroy(c->stream);
        c->stream = NULL;
    }
    if (c->core != NULL) {
        pw_core_disconnect(c->core);
        c->core = NULL;
    }
    if (c->context != NULL) {
        pw_context_destroy(c->context);
        c->context = NULL;
    }
    if (c->loop != NULL) {
        pw_main_loop_destroy(c->loop);
        c->loop = NULL;
    }
    egl_cleanup(&c->egl);
    colors_destroy(&c->colors);
    if (c->opt.fd >= 0) {
        close(c->opt.fd);
        c->opt.fd = -1;
    }
    pw_deinit();
}

static bool consumer_connect(struct consumer *c, char *error,
                             size_t error_size)
{
    struct pw_properties *core_props;
    struct pw_properties *stream_props;
    uint8_t params_buffer[16384];
    const struct spa_pod *params[16];
    uint32_t n_params = 0;
    uint32_t target_id = PW_ID_ANY;
    int fd = c->opt.fd;

    if (fd < 0 || fcntl(fd, F_GETFD) < 0) {
        snprintf(error, error_size, "inherited fd %d is not valid: %s", fd,
                 strerror(errno));
        return false;
    }

    c->loop = pw_main_loop_new(NULL);
    if (c->loop == NULL) {
        snprintf(error, error_size, "pw_main_loop_new failed");
        return false;
    }
    c->context = pw_context_new(pw_main_loop_get_loop(c->loop), NULL, 0);
    if (c->context == NULL) {
        snprintf(error, error_size, "pw_context_new failed");
        return false;
    }

    core_props = pw_properties_new(PW_KEY_APP_NAME,
                                   "multilinux-screencast-consumer", NULL);
    if (core_props == NULL) {
        snprintf(error, error_size, "cannot allocate PipeWire core properties");
        return false;
    }
    c->core = pw_context_connect_fd(c->context, fd, core_props, 0);
    if (c->core == NULL) {
        snprintf(error, error_size,
                 "pw_context_connect_fd failed for inherited fd %d: %s", fd,
                 strerror(errno));
        /* connect_fd takes ownership only after success. */
        close(fd);
        c->opt.fd = -1;
        return false;
    }
    /* The core owns the inherited socket from this point on.  It is closed by
     * pw_core_disconnect() in consumer_cleanup(). */
    c->opt.fd = -1;

    stream_props = pw_properties_new(
        PW_KEY_MEDIA_TYPE, "Video",
        PW_KEY_MEDIA_CATEGORY, "Capture",
        PW_KEY_MEDIA_ROLE, "Screen",
        PW_KEY_MEDIA_NAME, "multilinux-screencast-consumer",
        PW_KEY_NODE_NAME, "multilinux-screencast-consumer",
        PW_KEY_APP_NAME, "multilinux-screencast-consumer", NULL);
    if (stream_props == NULL) {
        snprintf(error, error_size, "cannot allocate stream properties");
        return false;
    }
    if (c->opt.have_target_serial) {
        if (pw_properties_set(stream_props, PW_KEY_TARGET_OBJECT,
                              c->opt.target_serial) < 0) {
            pw_properties_free(stream_props);
            snprintf(error, error_size, "cannot set target.object property");
            return false;
        }
        c->target_by_serial = true;
    } else {
        /* Modern target.object accepts object.serial/node.name.  Without a
         * serial the legacy target id is the only portable way to select the
         * portal-provided node. */
        target_id = c->opt.node;
    }

    c->stream = pw_stream_new(c->core, "screencast-consumer", stream_props);
    if (c->stream == NULL) {
        snprintf(error, error_size, "pw_stream_new failed");
        return false;
    }
    pw_stream_add_listener(c->stream, &c->stream_listener, &stream_events, c);
    if (!build_format_params(c, params_buffer, sizeof(params_buffer), params,
                             &n_params, error, error_size))
        return false;
    if (pw_stream_connect(c->stream, PW_DIRECTION_INPUT, target_id,
                          PW_STREAM_FLAG_AUTOCONNECT |
                              PW_STREAM_FLAG_MAP_BUFFERS |
                              PW_STREAM_FLAG_DONT_RECONNECT,
                          params, n_params) < 0) {
        snprintf(error, error_size, "pw_stream_connect failed: %s",
                 strerror(errno));
        return false;
    }
    return true;
}

static bool setup_timer(struct consumer *c, char *error, size_t error_size)
{
    struct timespec value;
    uint64_t timeout = c->opt.timeout_ms;

    c->deadline_ns = monotonic_ns() + timeout * 1000000ULL;
    c->timer = pw_loop_add_timer(pw_main_loop_get_loop(c->loop), on_timer, c);
    if (c->timer == NULL) {
        snprintf(error, error_size, "cannot create capture deadline timer");
        return false;
    }
    value.tv_sec = (time_t)(timeout / 1000u);
    value.tv_nsec = (long)((timeout % 1000u) * 1000000u);
    if (pw_loop_update_timer(pw_main_loop_get_loop(c->loop), c->timer, &value,
                             NULL, false) < 0) {
        snprintf(error, error_size, "cannot arm capture deadline timer");
        return false;
    }
    c->sigint_source = pw_loop_add_signal(pw_main_loop_get_loop(c->loop),
                                          SIGINT, on_signal, c);
    c->sigterm_source = pw_loop_add_signal(pw_main_loop_get_loop(c->loop),
                                           SIGTERM, on_signal, c);
    return true;
}

static bool finalize_capture(struct consumer *c, bool *passed)
{
    bool ok = true;

    *passed = false;
    if (c->interrupted) {
        ok = false;
    } else if (c->have_error) {
        ok = false;
    } else if (c->frames < c->opt.frames) {
        if (c->deadline_hit)
            set_error(c, "timeout",
                      "deadline reached after %u of %u requested frames",
                      c->frames, c->opt.frames);
        else
            set_error(c, "insufficient_frames",
                      "capture ended after %u of %u requested frames",
                      c->frames, c->opt.frames);
        ok = false;
    } else if (c->opt.expect_pattern) {
        const struct frame_stats *stats = &c->last_stats;

        if (!c->have_last_stats || !stats->pattern_accepted) {
            set_error(c, "pattern_mismatch",
                      "final frame does not contain the large RED/GREEN/BLUE blocks");
            ok = false;
        } else if (c->hash_changes == 0) {
            set_error(c, "pattern_hash_unchanged",
                      "pixel hash did not change across %u frames", c->frames);
            ok = false;
        }
    }
    if (ok && c->gpu_imported_any && c->opt.dmabuf) {
        /* CPU readback was performed, so this is explicitly not zero-copy. */
        c->cpu_readback_any = true;
    }
    *passed = ok;
    return ok;
}

/* ------------------------------------------------------------------ */
/* main                                                                 */

int main(int argc, char **argv)
{
    struct options options;
    struct consumer consumer;
    char error[512];
    bool passed = false;
    int exit_code;

    memset(&consumer, 0, sizeof(consumer));
    consumer.opt.fd = -1;
    consumer.egl.gbm_fd = -1;
    consumer.egl.display = EGL_NO_DISPLAY;
    consumer.egl.context = EGL_NO_CONTEXT;
    consumer.egl.surface = EGL_NO_SURFACE;
    consumer.exit_code = EXIT_CONNECT;

    if (!parse_options(argc, argv, &options, error, sizeof(error))) {
        fputs("{\"type\":\"error\",\"code\":\"invalid_arguments\",\"message\":",
              stdout);
        json_escape(stdout, error);
        fputs("}\n{\"type\":\"summary\",\"passed\":false,\"mode\":\"invalid\","
              "\"frames\":0,\"buffer_type\":null,\"gpu_import\":false,"
              "\"cpu_readback\":false,\"hashes_first\":[],\"hashes_last\":[],"
              "\"top_colors\":[],\"pattern\":null}\n", stdout);
        fflush(stdout);
        return EXIT_USAGE;
    }
    if (options.help) {
        print_usage(stdout);
        return 0;
    }
    if (options.self_test)
        return run_self_test();

    consumer.opt = options;
    consumer.capture_mode = options.dmabuf ? "dmabuf" : "memfd";
    consumer.buffer_type = NULL;
    pw_init(&argc, &argv);
    colors_init(&consumer.colors);
    if (consumer.colors.entries == NULL) {
        emit_error_now("allocation_failed", "cannot allocate colour table");
        emit_summary(&consumer, false);
        pw_deinit();
        return EXIT_CONNECT;
    }

    if (options.dmabuf) {
        if (!egl_initialize(&consumer, error, sizeof(error))) {
            set_and_emit_error(&consumer, "egl_unsupported", "%s", error);
            emit_summary(&consumer, false);
            consumer_cleanup(&consumer);
            return EXIT_UNSUPPORTED;
        }
        if (!query_supported_formats(&consumer, error, sizeof(error))) {
            set_and_emit_error(&consumer, "dmabuf_formats_unsupported", "%s",
                               error);
            emit_summary(&consumer, false);
            consumer_cleanup(&consumer);
            return EXIT_UNSUPPORTED;
        }
    } else {
        for (size_t i = 0; i < ARRAY_LEN(pixel_formats); i++) {
            consumer.formats[consumer.n_formats].pix = &pixel_formats[i];
            consumer.formats[consumer.n_formats].fourcc_supported = true;
            consumer.formats[consumer.n_formats].n_modifiers = 0;
            consumer.n_formats++;
        }
    }

    if (!consumer_connect(&consumer, error, sizeof(error))) {
        set_and_emit_error(&consumer, "pipewire_connect", "%s", error);
        emit_summary(&consumer, false);
        consumer_cleanup(&consumer);
        return EXIT_CONNECT;
    }
    if (!setup_timer(&consumer, error, sizeof(error))) {
        set_and_emit_error(&consumer, "timer_setup", "%s", error);
        emit_summary(&consumer, false);
        consumer_cleanup(&consumer);
        return EXIT_CONNECT;
    }

    (void)pw_main_loop_run(consumer.loop);
    passed = finalize_capture(&consumer, &passed);
    emit_summary(&consumer, passed);
    if (passed) {
        exit_code = 0;
    } else if (consumer.interrupted) {
        exit_code = EXIT_INTERRUPTED;
    } else if (consumer.have_error &&
               (strcmp(consumer.error_code, "timeout") == 0 ||
                strcmp(consumer.error_code, "insufficient_frames") == 0 ||
                strcmp(consumer.error_code, "pattern_hash_unchanged") == 0)) {
        exit_code = EXIT_TIMEOUT;
    } else if (consumer.have_error &&
               (strcmp(consumer.error_code, "unsupported_format") == 0 ||
                strcmp(consumer.error_code, "unsupported_layout") == 0 ||
                strcmp(consumer.error_code, "unsupported_buffer_type") == 0 ||
                strcmp(consumer.error_code, "unsupported_modifier") == 0 ||
                strcmp(consumer.error_code, "dmabuf_import_failed") == 0)) {
        exit_code = EXIT_UNSUPPORTED;
    } else {
        exit_code = EXIT_CONNECT;
    }
    consumer_cleanup(&consumer);
    return exit_code;
}
