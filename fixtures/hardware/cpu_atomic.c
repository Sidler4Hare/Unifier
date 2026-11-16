/* Shared fixture for Linux x86-64. Native atomics are sufficient at this width. */
#if !defined(__linux__) || !defined(__x86_64__) || !defined(__LP64__)
#error This CPU fixture requires the Linux x86-64 LP64 ABI.
#endif

static unsigned long counter;

_Static_assert(__atomic_always_lock_free(sizeof(counter), 0),
               "64-bit atomics must be emitted without a library call");

void reset(void)
{
    __atomic_store_n(&counter, 0, __ATOMIC_RELAXED);
}

void add_fixed(void)
{
    for (unsigned int iteration = 0; iteration < 10000; ++iteration) {
        __atomic_fetch_add(&counter, 1, __ATOMIC_RELAXED);
    }
}

unsigned long load(void)
{
    return __atomic_load_n(&counter, __ATOMIC_RELAXED);
}
