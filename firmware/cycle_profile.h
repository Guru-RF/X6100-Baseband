/* Optional laboratory cycle measurements, compiled out of the default payload.
 * DWT CYCCNT measures CPU cycles, not wall time. Preempting interrupts are
 * intentionally included. Flash/cache effects and wrapper cost are not hidden:
 * these counters bracket C callbacks only, not the assembly or complete ISR.
 * Read a coherent snapshot while the target is halted. Statistics wrap after
 * 2^32 calls; each measured interval must be shorter than one CYCCNT wrap.
 */
#ifndef BB_CYCLE_PROFILE_H
#define BB_CYCLE_PROFILE_H
#include <stdint.h>
#ifndef BB_REG32
#define BB_REG32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#endif
#ifndef BB_CYCLE_BARRIER
#define BB_CYCLE_BARRIER() __asm volatile ("dsb\nisb" ::: "memory")
#endif
#define BB_CYCLE_COMPILER_BARRIER() __asm volatile ("" ::: "memory")
#define BB_DEMCR 0xe000edfcu
#define BB_DWT_CTRL 0xe0001000u
#define BB_DWT_CYCCNT 0xe0001004u

typedef struct {
    uint32_t calls;
    uint32_t last_cycles;
    uint32_t minimum_cycles;
    uint32_t maximum_cycles;
    uint32_t total_cycles_low;
    uint32_t total_cycles_high;
} bb_cycle_stat;
enum {
    BB_CYCLE_BLOCK, BB_CYCLE_AUDIO, BB_CYCLE_IQ,
    BB_CYCLE_UNCHANGED_RX, BB_CYCLE_UNCHANGED_TX,
    BB_CYCLE_RESET_PATH, BB_CYCLE_CONFIG_PATH, BB_CYCLE_REJECT_PATH,
    BB_CYCLE_RESTORE_PATH, BB_CYCLE_INTERPOLATION_PATH,
    BB_CYCLE_SELECTOR, BB_CYCLE_STAT_COUNT
};
enum {
    BB_CYCLE_STATE_RESET = 1u, BB_CYCLE_CONFIG_APPLY = 2u,
    BB_CYCLE_CONFIG_REJECT = 4u, BB_CYCLE_FILTER_RESTORE = 8u,
    BB_CYCLE_INTERPOLATION_SWITCH = 16u
};
/* Path categories overlap: each records the WHOLE block callback when that
 * event occurred. They must not be summed. Unchanged means no marked event,
 * so it also includes waiting for stable configuration snapshots.
 */
volatile bb_cycle_stat bb_cycle_stats[BB_CYCLE_STAT_COUNT];
volatile uint32_t bb_cycle_probe_status; /*0=not initialized,1=enabled,2=no counter*/
static uint32_t bb_cycle_block_events;
#define BB_CYCLE_EVENT(event) (bb_cycle_block_events |= (event))
static uint32_t bb_cycle_start(void) {
    if (!bb_cycle_probe_status) {
        BB_REG32(BB_DEMCR) |= 1u << 24; /* TRCENA */
        if (BB_REG32(BB_DWT_CTRL) & (1u << 25)) {
            bb_cycle_probe_status = 2; /* NOCYCCNT */
            return 0;
        }
        BB_REG32(BB_DWT_CTRL) |= 1u; /* CYCCNTENA; do not reset shared counter */
        BB_CYCLE_BARRIER();
        bb_cycle_probe_status = 1;
    }
    BB_CYCLE_COMPILER_BARRIER();
    uint32_t start = bb_cycle_probe_status == 1 ? BB_REG32(BB_DWT_CYCCNT) : 0;
    BB_CYCLE_COMPILER_BARRIER();
    return start;
}
static void bb_cycle_record(unsigned index, uint32_t elapsed) {
    volatile bb_cycle_stat *stat = &bb_cycle_stats[index];
    if (!stat->calls || elapsed < stat->minimum_cycles) stat->minimum_cycles = elapsed;
    stat->calls++;
    stat->last_cycles = elapsed;
    if (elapsed > stat->maximum_cycles) stat->maximum_cycles = elapsed;
    uint32_t before = stat->total_cycles_low;
    stat->total_cycles_low = before + elapsed;
    stat->total_cycles_high += stat->total_cycles_low < before;
}
static void bb_cycle_finish(unsigned index, uint32_t start) {
    BB_CYCLE_COMPILER_BARRIER();
    if (bb_cycle_probe_status != 1) return;
    bb_cycle_record(index, BB_REG32(BB_DWT_CYCCNT) - start);
}
static uint32_t bb_cycle_block_start(void) {
    bb_cycle_block_events = 0;
    return bb_cycle_start();
}
static void bb_cycle_block_finish(uint32_t start, unsigned tx) {
    BB_CYCLE_COMPILER_BARRIER();
    if (bb_cycle_probe_status != 1) return;
    uint32_t elapsed = BB_REG32(BB_DWT_CYCCNT) - start;
    bb_cycle_record(BB_CYCLE_BLOCK, elapsed);
    if (!bb_cycle_block_events)
        bb_cycle_record(tx ? BB_CYCLE_UNCHANGED_TX : BB_CYCLE_UNCHANGED_RX, elapsed);
    for (unsigned bit = 0; bit < 5; ++bit)
        if (bb_cycle_block_events & (1u << bit))
            bb_cycle_record(BB_CYCLE_RESET_PATH + bit, elapsed);
}
#endif
