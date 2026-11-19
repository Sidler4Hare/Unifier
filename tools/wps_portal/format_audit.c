/* Compatibility for Linux Writer 12.1.2.29183 (x86-64). The two callers
 * below are pinned to the vendor picker checked by the build recipe.
 */
#define _GNU_SOURCE
#include <link.h>
#include <stdatomic.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define WPS_DIR "/opt/kingsoft/wps-office/office6/"
#define CLOUD_PICKER WPS_DIR "addons/kcloudfiledialog/libkcloudfiledialog.so"
#define QT_SPLIT "_ZNK6kso_qt7QString5splitERKS0_NS0_13SplitBehaviorENS_2Qt15CaseSensitivityE"
#define QT_TO_BOOL "_ZNK6kso_qt8QVariant6toBoolEv"

/* Qt 5.12.12 QArrayData and the static-literal layout on x86-64. */
struct string_data {
    int ref;
    int length;
    unsigned int allocation;
    ptrdiff_t offset;
};

struct filter_separator {
    struct string_data header;
    uint16_t characters[3];
};

static const struct filter_separator two_spaces = {
    {-1, 2, 0, offsetof(struct filter_separator, characters)}, {' ', ' ', 0}
};
_Static_assert(sizeof(struct string_data) == 24, "unexpected Qt array layout");
_Static_assert(offsetof(struct string_data, offset) == 16, "unexpected Qt string offset");
_Static_assert(offsetof(struct filter_separator, characters) == 24, "unexpected Qt literal layout");

typedef void *(*split_function)(void *, const struct string_data *const *,
                                const struct string_data *const *, int, int);
static _Atomic(uintptr_t) original_split;
static _Atomic(uintptr_t) original_to_bool;
static _Atomic(uintptr_t) picker_base;

static const uint16_t *characters(const struct string_data *data)
{
    return (const uint16_t *)((const char *)data + data->offset);
}

static int equal(const char *left, const char *right)
{
    while (*left && *left == *right) {
        ++left;
        ++right;
    }
    return *left == *right;
}

static int vendor_core(const char *name)
{
    const char *stem = WPS_DIR "libQt5CoreKso.so";
    while (*stem && *stem == *name) {
        ++stem;
        ++name;
    }
    return *stem == '\0' && (*name == '\0' || *name == '.');
}

static int is_semicolon_pair(const struct string_data *data)
{
    return data && data->length == 2 && characters(data)[0] == ';' && characters(data)[1] == ';';
}

/* Allow the vendor's format list only when its double spaces are entry
 * boundaries. Preserve ordinary Qt filters and unexpected input.
 */
static int has_filter_boundaries(const struct string_data *data)
{
    if (!data || data->length < 8 || data->length > 4096)
        return 0;
    const uint16_t *text = characters(data);
    int boundaries = 0;
    for (int i = 0; i + 1 < data->length; ++i) {
        if (text[i] == ';' && text[i + 1] == ';')
            return 0;
        if (text[i] != ' ' || text[i + 1] != ' ')
            continue;
        if (i == 0 || text[i - 1] != ')' || i + 2 >= data->length || text[i + 2] == ' ')
            return 0;
        ++boundaries;
        ++i;
    }
    return boundaries > 0 && text[data->length - 1] == ')';
}

static void *split_wps_filters(void *out, const struct string_data *const *source,
                               const struct string_data *const *separator, int behavior, int sensitivity)
{
    split_function next = (split_function)atomic_load(&original_split);
    uintptr_t caller = (uintptr_t)__builtin_return_address(0) - atomic_load(&picker_base);
    const struct string_data *replacement = &two_spaces.header;
    if ((caller == 0x1146da || caller == 0x1083c0)
            && is_semicolon_pair(*separator) && has_filter_boundaries(*source))
        separator = &replacement;
    return next(out, source, separator, behavior, sensitivity);
}

static bool system_picker_setting(const void *value)
{
    uintptr_t caller = (uintptr_t)__builtin_return_address(0) - atomic_load(&picker_base);
    /* Return site of toBool() on KPluginsSettings' forceShowSysFileDialog. */
    if (caller == 0x116144)
        return true;
    bool (*next)(const void *) = (bool (*)(const void *))atomic_load(&original_to_bool);
    return next(value);
}

static bool bind_address(_Atomic(uintptr_t) *slot, uintptr_t address)
{
    uintptr_t previous = 0;
    return atomic_compare_exchange_strong(slot, &previous, address) || previous == address;
}

unsigned int la_version(unsigned int version)
{
    return version >= LAV_CURRENT ? LAV_CURRENT : 0;
}

unsigned int la_objopen(struct link_map *map, Lmid_t namespace, uintptr_t *cookie)
{
    if (namespace != LM_ID_BASE)
        return 0;
    *cookie = (uintptr_t)map;
    if (equal(map->l_name, CLOUD_PICKER))
        return LA_FLG_BINDFROM;
    return vendor_core(map->l_name) ? LA_FLG_BINDTO : 0;
}

uintptr_t la_symbind64(Elf64_Sym *symbol, unsigned int index, uintptr_t *from, uintptr_t *to,
                        unsigned int *flags, const char *name)
{
    (void)index;
    uintptr_t address = symbol->st_value;
    if (*flags & (LA_SYMB_DLSYM | LA_SYMB_ALTVALUE))
        return address;
    bool split = equal(name, QT_SPLIT);
    if (!split && !equal(name, QT_TO_BOOL))
        return address;
    const struct link_map *source = (const struct link_map *)*from;
    const struct link_map *target = (const struct link_map *)*to;
    if (!equal(source->l_name, CLOUD_PICKER) || !vendor_core(target->l_name))
        return address;
    if (!bind_address(&picker_base, source->l_addr)
            || !bind_address(split ? &original_split : &original_to_bool, address))
        return address;
    *flags |= LA_SYMB_NOPLTENTER | LA_SYMB_NOPLTEXIT;
    return split ? (uintptr_t)split_wps_filters : (uintptr_t)system_picker_setting;
}
