/* Linux x86-64 ELF with no libc or dynamic loader. Build for the x86-64 baseline. */
#if !defined(__linux__) || !defined(__x86_64__) || !defined(__LP64__)
#error The CPU fixture requires the Linux x86-64 LP64 ABI.
#endif

#define ARRAY_LENGTH(array) (sizeof(array) / sizeof((array)[0]))
#define NOINLINE __attribute__((noinline))

struct cpuid_registers {
    unsigned int eax;
    unsigned int ebx;
    unsigned int ecx;
    unsigned int edx;
};

static struct cpuid_registers read_cpuid(unsigned int leaf)
{
    struct cpuid_registers result;
    __asm__ volatile("cpuid"
                     : "=a"(result.eax), "=b"(result.ebx),
                       "=c"(result.ecx), "=d"(result.edx)
                     : "0"(leaf), "2"(0u));
    return result;
}

static unsigned long read_xcr0(void)
{
    unsigned int low;
    unsigned int high;
    __asm__ volatile("xgetbv" : "=a"(low), "=d"(high) : "c"(0u));
    return ((unsigned long)high << 32) | low;
}

static NOINLINE int check_sse42(void)
{
    static const unsigned char input[] = "123456789";
    unsigned int crc = 0xffffffffu;
    for (unsigned int index = 0; index < sizeof(input) - 1; ++index) {
        __asm__ volatile("crc32b %1, %0" : "+r"(crc) : "qm"(input[index]));
    }
    return (crc ^ 0xffffffffu) == 0xe3069283u;
}

static NOINLINE int check_avx(void)
{
    static const float left[] = {1, 2, 3, 4, -1, -2, -3, -4};
    static const float right[] = {8, 6, 4, 2, 4, 3, 2, 1};
    static const float expected[] = {9, 8, 7, 6, 3, 1, -1, -3};
    float output[ARRAY_LENGTH(expected)];

    __asm__ volatile("vmovups %[left], %%ymm0\n\t"
                     "vaddps %[right], %%ymm0, %%ymm0\n\t"
                     "vmovups %%ymm0, %[output]\n\t"
                     "vzeroupper"
                     : [output] "=m"(output)
                     : [left] "m"(left), [right] "m"(right)
                     : "xmm0");
    for (unsigned int lane = 0; lane < ARRAY_LENGTH(expected); ++lane) {
        if (output[lane] != expected[lane]) {
            return 0;
        }
    }
    return 1;
}

static NOINLINE int check_avx2(void)
{
    static const int left[] = {11, -11, 42, -42, 23, -23, 50, -50};
    static const int right[] = {31, -30, -23, 24, 15, -14, -4, 3};
    static const int expected[] = {42, -41, 19, -18, 38, -37, 46, -47};
    int output[ARRAY_LENGTH(expected)];

    __asm__ volatile("vmovdqu %[left], %%ymm0\n\t"
                     "vpaddd %[right], %%ymm0, %%ymm0\n\t"
                     "vmovdqu %%ymm0, %[output]\n\t"
                     "vzeroupper"
                     : [output] "=m"(output)
                     : [left] "m"(left), [right] "m"(right)
                     : "xmm0");
    for (unsigned int lane = 0; lane < ARRAY_LENGTH(expected); ++lane) {
        if (output[lane] != expected[lane]) {
            return 0;
        }
    }
    return 1;
}

static NOINLINE int check_fma(void)
{
    /* Rounding the product first would give 1, and the final answer would be 0. */
    static const float left = 0x1.000002p0f;
    static const float right = 0x1.fffffcp-1f;
    static const float addend = -1.0f;
    float output;

    __asm__ volatile("vmovss %[left], %%xmm0\n\t"
                     "vmovss %[addend], %%xmm1\n\t"
                     "vfmadd231ss %[right], %%xmm0, %%xmm1\n\t"
                     "vmovss %%xmm1, %[output]"
                     : [output] "=m"(output)
                     : [left] "m"(left), [right] "m"(right), [addend] "m"(addend)
                     : "xmm0", "xmm1");
    return output == -0x1p-46f;
}

static NOINLINE int check_aes_ni(void)
{
    static const unsigned char input[] = {
        0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07,
        0x08, 0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x0e, 0x0f,
    };
    static const unsigned char key[] = {
        0x5a, 0x5a, 0x5a, 0x5a, 0x5a, 0x5a, 0x5a, 0x5a,
        0x5a, 0x5a, 0x5a, 0x5a, 0x5a, 0x5a, 0x5a, 0x5a,
    };
    static const unsigned char expected[] = {
        0x39, 0x31, 0x3d, 0x2c, 0xa8, 0x5b, 0xf1, 0x21,
        0x6a, 0x8d, 0x2d, 0x9f, 0xa4, 0x26, 0x35, 0x71,
    };
    unsigned char output[ARRAY_LENGTH(expected)];

    __asm__ volatile("movdqu %[input], %%xmm0\n\t"
                     "movdqu %[key], %%xmm1\n\t"
                     "aesenclast %%xmm1, %%xmm0\n\t"
                     "movdqu %%xmm0, %[output]"
                     : [output] "=m"(output)
                     : [input] "m"(input), [key] "m"(key)
                     : "xmm0", "xmm1");
    for (unsigned int byte = 0; byte < ARRAY_LENGTH(expected); ++byte) {
        if (output[byte] != expected[byte]) {
            return 0;
        }
    }
    return 1;
}

static NOINLINE int check_bmi1(void)
{
    const unsigned int inverted = 0x0f0f0f0fu;
    const unsigned int input = 0x33cc55aau;
    unsigned int output;
    __asm__ volatile("andn %2, %1, %0" : "=r"(output) : "r"(inverted), "r"(input) : "cc");
    return output == 0x30c050a0u;
}

static NOINLINE int check_bmi2(void)
{
    const unsigned int input = 0xbu;
    const unsigned int mask = 0x292u;
    unsigned int output;
    __asm__ volatile("pdep %2, %1, %0" : "=r"(output) : "r"(input), "r"(mask));
    return output == 0x212u;
}

static NOINLINE int check_avx512f(void)
{
    static const int left[] = {
        1, 2, 3, 4, 5, 6, 7, 8, -1, -2, -3, -4, -5, -6, -7, -8,
    };
    static const int right[] = {
        16, 16, 16, 16, 16, 16, 16, 16, 8, 7, 6, 5, 4, 3, 2, 1,
    };
    static const int expected[] = {
        17, 18, 19, 20, 21, 22, 23, 24, 7, 5, 3, 1, -1, -3, -5, -7,
    };
    int output[ARRAY_LENGTH(expected)];

    __asm__ volatile("vmovdqu32 %[left], %%zmm0\n\t"
                     "vpaddd %[right], %%zmm0, %%zmm0\n\t"
                     "vmovdqu32 %%zmm0, %[output]\n\t"
                     "vzeroupper"
                     : [output] "=m"(output)
                     : [left] "m"(left), [right] "m"(right)
                     : "xmm0");
    for (unsigned int lane = 0; lane < ARRAY_LENGTH(expected); ++lane) {
        if (output[lane] != expected[lane]) {
            return 0;
        }
    }
    return 1;
}

struct json_buffer {
    unsigned long length;
    char bytes[2048];
};

static void append_text(struct json_buffer *output, const char *value)
{
    while (*value && output->length < sizeof(output->bytes)) {
        output->bytes[output->length++] = *value++;
    }
}

static void append_hex(struct json_buffer *output, unsigned long value)
{
    static const char digits[] = "0123456789abcdef";
    char quoted[] = "\"0x0000000000000000\"";
    for (unsigned int index = 0; index < 16; ++index) {
        quoted[18 - index] = digits[value & 15u];
        value >>= 4;
    }
    append_text(output, quoted);
}

static int write_json(const struct json_buffer *output)
{
    unsigned long offset = 0;
    if (output->length >= sizeof(output->bytes)) {
        return 0;
    }
    for (unsigned int attempt = 0; attempt < 8 && offset < output->length; ++attempt) {
        long written;
        __asm__ volatile("syscall"
                         : "=a"(written)
                         : "0"(1L), "D"(1L), "S"(output->bytes + offset),
                           "d"(output->length - offset)
                         : "rcx", "r11", "memory", "cc");
        if (written == -4) { /* EINTR */
            continue;
        }
        if (written <= 0) {
            return 0;
        }
        offset += (unsigned long)written;
    }
    return offset == output->length;
}

static int has_flag(unsigned int value, unsigned int bit)
{
    return (value & (1u << bit)) != 0;
}

struct instruction_check {
    const char *name;
    int advertised;
    int enabled;
    int (*check)(void);
};

__attribute__((used, noinline)) static int cpu_probe_main(void)
{
    const unsigned int max_leaf = read_cpuid(0).eax;
    struct cpuid_registers leaf1 = {0};
    struct cpuid_registers leaf7 = {0};
    if (max_leaf >= 1) {
        leaf1 = read_cpuid(1);
    }
    if (max_leaf >= 7) {
        leaf7 = read_cpuid(7);
    }

    const int xcr0_readable = has_flag(leaf1.ecx, 26) && has_flag(leaf1.ecx, 27);
    const unsigned long xcr0 = xcr0_readable ? read_xcr0() : 0;
    const int sse42 = has_flag(leaf1.ecx, 20);
    const int avx = has_flag(leaf1.ecx, 28);
    const int fma = has_flag(leaf1.ecx, 12);
    const int aes_ni = has_flag(leaf1.ecx, 25);
    const int bmi1 = has_flag(leaf7.ebx, 3);
    const int avx2 = has_flag(leaf7.ebx, 5);
    const int bmi2 = has_flag(leaf7.ebx, 8);
    const int avx512f = has_flag(leaf7.ebx, 16);
    const int avx_enabled = avx && (xcr0 & 0x6UL) == 0x6UL;
    const int avx512_enabled = avx_enabled && (xcr0 & 0xe0UL) == 0xe0UL;

    const struct instruction_check checks[] = {
        {"sse4_2", sse42, sse42, check_sse42},
        {"avx", avx, avx_enabled, check_avx},
        {"avx2", avx2, avx2 && avx_enabled, check_avx2},
        {"fma", fma, fma && avx_enabled, check_fma},
        {"aes_ni", aes_ni, aes_ni, check_aes_ni},
        {"bmi1", bmi1, bmi1, check_bmi1},
        {"bmi2", bmi2, bmi2, check_bmi2},
        {"avx512f", avx512f, avx512f && avx512_enabled, check_avx512f},
    };
    struct json_buffer output;
    output.length = 0;
    append_text(&output, "{\"schema\":1,\"arch\":\"x86_64\",\"max_basic_leaf\":");
    append_hex(&output, max_leaf);
    append_text(&output, ",\"leaf1_eax\":");
    append_hex(&output, leaf1.eax);
    append_text(&output, ",\"leaf1_ecx\":");
    append_hex(&output, leaf1.ecx);
    append_text(&output, ",\"leaf7_ebx\":");
    append_hex(&output, leaf7.ebx);
    append_text(&output, ",\"xcr0\":");
    if (xcr0_readable) {
        append_hex(&output, xcr0);
    } else {
        append_text(&output, "null");
    }
    append_text(&output, ",\"checks\":{");

    int mismatch = 0;
    for (unsigned int index = 0; index < ARRAY_LENGTH(checks); ++index) {
        const struct instruction_check *entry = &checks[index];
        const char *result = entry->advertised ? "os_disabled" : "unsupported";
        if (entry->enabled) {
            const int correct = entry->check();
            mismatch |= !correct;
            result = correct ? "pass" : "fail";
        }
        if (index != 0) {
            append_text(&output, ",");
        }
        append_text(&output, "\"");
        append_text(&output, entry->name);
        append_text(&output, "\":{\"cpuid\":");
        append_text(&output, entry->advertised ? "true" : "false");
        append_text(&output, ",\"enabled\":");
        append_text(&output, entry->enabled ? "true" : "false");
        append_text(&output, ",\"result\":\"");
        append_text(&output, result);
        append_text(&output, "\"}");
    }
    append_text(&output, "}}\n");
    if (!write_json(&output)) {
        return 2;
    }
    return mismatch;
}

/* Align the initial stack as a caller before entering the C ABI. */
__asm__(".text\n"
        ".globl _start\n"
        ".type _start, @function\n"
        "_start:\n\t"
        "xorl %ebp, %ebp\n\t"
        "andq $-16, %rsp\n\t"
        "call cpu_probe_main\n\t"
        "movl %eax, %edi\n\t"
        "movl $60, %eax\n\t"
        "syscall\n\t"
        "ud2\n"
        ".size _start, .-_start\n");
