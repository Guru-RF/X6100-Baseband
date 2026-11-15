/* Minimal private runtime: no OEM libc state, heap, file descriptors or syscall
 * stubs. All linked libm errno storage belongs to this payload in CCM. */
#include <stddef.h>
#include <stdint.h>

/* GCC's may_alias type permits word stores into float arrays and arbitrary
 * objects without violating effective-type aliasing rules. Volatile stores
 * also keep this private primitive from being turned into a recursive call. */
typedef uint32_t alias_word __attribute__((__may_alias__));
static int math_errno;
int *__errno(void) { return &math_errno; }
void *memset(void *dst, int value, size_t count) {
    volatile unsigned char *p = dst;
    unsigned char byte = (unsigned char)value;
    while (count && ((uintptr_t)p & (sizeof(alias_word) - 1))) {
        *p++ = byte;
        --count;
    }
    if (count >= sizeof(alias_word)) {
        uint32_t repeated = (uint32_t)byte * UINT32_C(0x01010101);
        volatile alias_word *words = (volatile alias_word *)p;
        do {
            *words++ = repeated;
            count -= sizeof(alias_word);
        } while (count >= sizeof(alias_word));
        p = (volatile unsigned char *)words;
    }
    while (count--) *p++ = byte;
    return dst;
}
void *memcpy(void *dst, const void *src, size_t count) {
    unsigned char *out = dst;
    const unsigned char *in = src;
    while (count--) *out++ = *in++;
    return dst;
}
