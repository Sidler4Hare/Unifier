/* Freestanding x86_64 probe for same-binary resource comparisons.
 *
 * No libc: the binary is linked with -nostdlib and only uses direct syscalls,
 * so the same file runs on the host and inside every target user space.
 *
 *   probe short <input>              hash the input, exit
 *   probe burn <input> <iterations>  touch 16 MiB, run a fixed CPU loop
 *   probe active <input> <seconds>   touch 16 MiB, run until a deadline
 *
 * Deterministic output starts with "result" for short and burn. Active mode
 * prints "input" and "result" separately because its iteration count depends
 * on the measured time. The final line is always "timing cpu_ns=...".
 */

typedef unsigned long u64;
typedef long i64;
typedef unsigned char u8;

#define SYS_READ 0
#define SYS_WRITE 1
#define SYS_CLOSE 3
#define SYS_MMAP 9
#define SYS_OPENAT 257
#define SYS_CLOCK_GETTIME 228
#define SYS_EXIT_GROUP 231

#define AT_FDCWD (-100)
#define CLOCK_MONOTONIC 1
#define CLOCK_PROCESS_CPUTIME_ID 3
#define PROT_READ_WRITE 3
#define MAP_PRIVATE_ANONYMOUS 0x22
#define MEM_BYTES (16UL * 1024 * 1024)
#define INPUT_LIMIT (1024UL * 1024)
#define FNV_OFFSET 1469598103934665603UL
#define FNV_PRIME 1099511628211UL

struct timespec {
    i64 seconds;
    i64 nanoseconds;
};

static inline long syscall6(long number, long a, long b, long c,
                            long d, long e, long f) {
    long result;
    register long r10 __asm__("r10") = d;
    register long r8 __asm__("r8") = e;
    register long r9 __asm__("r9") = f;
    __asm__ volatile("syscall"
                     : "=a"(result)
                     : "a"(number), "D"(a), "S"(b), "d"(c),
                       "r"(r10), "r"(r8), "r"(r9)
                     : "rcx", "r11", "memory");
    return result;
}

void *memset(void *destination, int value, u64 length) {
    u8 *bytes = (u8 *)destination;
    for (u64 i = 0; i < length; i++) {
        bytes[i] = (u8)value;
    }
    return destination;
}

void *memcpy(void *destination, const void *source, u64 length) {
    u8 *to = (u8 *)destination;
    const u8 *from = (const u8 *)source;
    for (u64 i = 0; i < length; i++) {
        to[i] = from[i];
    }
    return destination;
}

static u64 text_length(const char *text) {
    u64 length = 0;
    while (text[length]) {
        length++;
    }
    return length;
}

static int text_equal(const char *left, const char *right) {
    while (*left && *right) {
        if (*left != *right) {
            return 0;
        }
        left++;
        right++;
    }
    return *left == *right;
}

static int parse_u64(const char *text, u64 *value) {
    u64 result = 0;
    int digits = 0;
    while (*text >= '0' && *text <= '9') {
        result = result * 10 + (u64)(*text - '0');
        text++;
        digits++;
    }
    if (!digits || *text) {
        return 0;
    }
    *value = result;
    return 1;
}

static char *put_text(char *cursor, const char *text) {
    while (*text) {
        *cursor++ = *text++;
    }
    return cursor;
}

static char *put_u64(char *cursor, u64 value) {
    char digits[20];
    int count = 0;
    if (value == 0) {
        *cursor++ = '0';
        return cursor;
    }
    while (value) {
        digits[count++] = (char)('0' + (value % 10));
        value /= 10;
    }
    while (count) {
        *cursor++ = digits[--count];
    }
    return cursor;
}

static char *put_hex(char *cursor, u64 value) {
    static const char table[] = "0123456789abcdef";
    *cursor++ = '0';
    *cursor++ = 'x';
    int started = 0;
    for (int shift = 60; shift >= 0; shift -= 4) {
        int digit = (int)((value >> shift) & 0xf);
        if (digit || started || shift == 0) {
            *cursor++ = table[digit];
            started = 1;
        }
    }
    return cursor;
}

static void emit(const char *text) {
    u64 length = text_length(text);
    long written = syscall6(SYS_WRITE, 1, (long)text, (long)length, 0, 0, 0);
    (void)written;
}

__attribute__((noreturn)) static void fail(int code) {
    syscall6(SYS_EXIT_GROUP, code, 0, 0, 0, 0, 0);
    for (;;) {
    }
}

static long read_file(const char *path, u8 *buffer, u64 capacity) {
    long descriptor = syscall6(SYS_OPENAT, AT_FDCWD, (long)path, 0, 0, 0, 0);
    if (descriptor < 0) {
        return descriptor;
    }
    u64 total = 0;
    while (total < capacity) {
        long count = syscall6(SYS_READ, descriptor, (long)(buffer + total),
                              (long)(capacity - total), 0, 0, 0);
        if (count < 0) {
            syscall6(SYS_CLOSE, descriptor, 0, 0, 0, 0, 0);
            return count;
        }
        if (count == 0) {
            break;
        }
        total += (u64)count;
    }
    syscall6(SYS_CLOSE, descriptor, 0, 0, 0, 0, 0);
    return (long)total;
}

static u64 hash_bytes(const u8 *data, u64 length, u64 state) {
    for (u64 i = 0; i < length; i++) {
        state ^= data[i];
        state *= FNV_PRIME;
    }
    return state;
}

static u8 *map_memory(void) {
    long address = syscall6(SYS_MMAP, 0, (long)MEM_BYTES, PROT_READ_WRITE,
                            MAP_PRIVATE_ANONYMOUS, -1, 0);
    if (address < 0 && address >= -4095) {
        return (u8 *)0;
    }
    return (u8 *)address;
}

static void touch_pages(u8 *memory, u64 seed) {
    for (u64 offset = 0; offset < MEM_BYTES; offset += 4096) {
        memory[offset] = (u8)(seed ^ (offset >> 12));
    }
}

static u64 work_fixed(u64 seed, u64 iterations, u8 *memory) {
    u64 state = seed | 1;
    for (u64 i = 0; i < iterations; i++) {
        state ^= state << 13;
        state ^= state >> 7;
        state ^= state << 17;
        if ((i & 0xfffff) == 0) {
            memory[(i >> 20) % (MEM_BYTES / 4096) * 4096] ^= (u8)state;
        }
    }
    return state;
}

static u64 monotonic_ns(void) {
    struct timespec now;
    if (syscall6(SYS_CLOCK_GETTIME, CLOCK_MONOTONIC, (long)&now, 0, 0, 0, 0) != 0) {
        return 0;
    }
    return (u64)now.seconds * 1000000000UL + (u64)now.nanoseconds;
}

static u64 cpu_ns(void) {
    struct timespec now;
    if (syscall6(SYS_CLOCK_GETTIME, CLOCK_PROCESS_CPUTIME_ID,
                 (long)&now, 0, 0, 0, 0) != 0) {
        return 0;
    }
    return (u64)now.seconds * 1000000000UL + (u64)now.nanoseconds;
}

static u64 work_until(u64 seed, u64 seconds, u8 *memory, u64 *iterations) {
    u64 deadline = monotonic_ns() + seconds * 1000000000UL;
    u64 state = seed | 1;
    u64 count = 0;
    for (;;) {
        for (int inner = 0; inner < 100000; inner++) {
            state ^= state << 13;
            state ^= state >> 7;
            state ^= state << 17;
        }
        count += 100000;
        memory[(count >> 20) % (MEM_BYTES / 4096) * 4096] ^= (u8)state;
        if (monotonic_ns() >= deadline) {
            break;
        }
    }
    *iterations = count;
    return state;
}

static u8 input_buffer[INPUT_LIMIT];

__attribute__((noreturn)) void start_c(u64 *stack) {
    u64 argc = stack[0];
    char **argv = (char **)&stack[1];
    if (argc < 3) {
        emit("usage: resource_probe short|burn|active <input> [value]\n");
        fail(2);
    }
    long read = read_file(argv[2], input_buffer, INPUT_LIMIT);
    if (read < 0) {
        emit("error: cannot read input\n");
        fail(3);
    }
    u64 bytes = (u64)read;
    u64 seed = hash_bytes(input_buffer, bytes, FNV_OFFSET);
    char line[512];
    char *cursor;
    u64 before = cpu_ns();

    if (text_equal(argv[1], "short")) {
        cursor = put_text(line, "result bytes=");
        cursor = put_u64(cursor, bytes);
        cursor = put_text(cursor, " hash=");
        cursor = put_hex(cursor, seed);
        *cursor++ = '\n';
        *cursor = 0;
        emit(line);
    } else if (text_equal(argv[1], "burn") || text_equal(argv[1], "active")) {
        u64 value = 0;
        if (argc < 4 || !parse_u64(argv[3], &value) || value == 0) {
            emit("error: value must be a positive integer\n");
            fail(2);
        }
        u8 *memory = map_memory();
        if (!memory) {
            emit("error: mmap failed\n");
            fail(4);
        }
        touch_pages(memory, seed);
        emit("ready\n");
        u64 iterations = 0;
        u64 state;
        if (text_equal(argv[1], "burn")) {
            iterations = value;
            state = work_fixed(seed, iterations, memory);
        } else {
            state = work_until(seed, value, memory, &iterations);
        }
        cursor = put_text(line, "result bytes=");
        cursor = put_u64(cursor, bytes);
        cursor = put_text(cursor, " hash=");
        cursor = put_hex(cursor, seed);
        cursor = put_text(cursor, " iterations=");
        cursor = put_u64(cursor, iterations);
        cursor = put_text(cursor, " work=");
        cursor = put_hex(cursor, state);
        *cursor++ = '\n';
        *cursor = 0;
        emit(line);
    } else {
        emit("error: unknown mode\n");
        fail(2);
    }

    u64 after = cpu_ns();
    cursor = put_text(line, "timing cpu_ns=");
    cursor = put_u64(cursor, after > before ? after - before : 0);
    *cursor++ = '\n';
    *cursor = 0;
    emit(line);
    fail(0);
}

__attribute__((naked, noreturn)) void _start(void) {
    __asm__ volatile(
        "xor %rbp, %rbp\n"
        "mov %rsp, %rdi\n"
        "and $-16, %rsp\n"
        "call start_c\n");
}
