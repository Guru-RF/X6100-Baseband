/* V1.1.9 research adapter. See README: this is NOT a deployable port.
 * State/configuration are owned exclusively by the DMA callback. No writes to
 * OEM calibration, power coefficients, codec settings or flow packets.
 */
#include "tx_dsp.h"
#include "tx_protocol.h"
#include "tx_interpolation.h"
#include <math.h>
#include <stdint.h>

#ifdef BB_CYCLE_PROFILE
#include "cycle_profile.h"
#define bb_begin_block bb_begin_block_impl
#define bb_tx_audio bb_tx_audio_impl
#define bb_tx_iq bb_tx_iq_impl
#define bb_tx_interpolator bb_tx_interpolator_impl
#else
#define BB_CYCLE_EVENT(event) ((void)0)
#endif

#define READ8(a) (*(volatile uint8_t *)(uintptr_t)(a))
#define READ32(a) (*(volatile uint32_t *)(uintptr_t)(a))
#define READF(a) (*(volatile float *)(uintptr_t)(a))
#define LAB_COOKIE 0x54584c42u

/* Intentionally zero after reset. A debugger-only development interlock;
 * neither I2C traffic nor a legacy GUI can arm an unvalidated payload. */
volatile uint32_t bb_lab_arm;
volatile uint32_t bb_applied_configurations, bb_rejected_configurations;
static tx_dsp_state dsp;
static uint32_t last_words[3], stable_words[3];
static uint8_t previous_mode, previous_tx;
static unsigned stable_blocks;
static bool configured, filter_changed;

extern uint8_t __data_load[], __data_start[], __data_end[];
extern uint8_t __bss_start[], __bss_end[];

void patch_init(void) {
    /* Establish CCM clock before touching any payload state, even if IAP left
     * RCC in a non-reset state. Preserve every other AHB1 clock-enable bit. */
    volatile uint32_t *ahb1enr = (volatile uint32_t *)(uintptr_t)0x40023830u;
    *ahb1enr |= 1u << 20;
    (void)*ahb1enr;
    __asm volatile ("dsb" ::: "memory");
    /* Bootstrap our own runtime, never clear OEM .data/.bss. Volatile loops
     * avoid a compiler-generated libc call before its own .data is ready. */
    volatile uint8_t *dst = __data_start;
    const uint8_t *src = __data_load;
    while (dst < __data_end) *dst++ = *src++;
    dst = __bss_start;
    while (dst < __bss_end) *dst++ = 0;
    tx_dsp_config c = tx_dsp_defaults();
    c.filter_low_hz = 300.0f; /* OEM defaults, not r13's160 Hz */
    (void)tx_dsp_configure(&dsp, &c);
    previous_mode = 255;
}

static void set_oem_filter(float low, float high) {
    typedef void (*setup_fn)(float, float, float, void *, int);
    setup_fn setup = (setup_fn)(uintptr_t)0x0802397du;
    setup(12500.0f, low, high, (void *)(uintptr_t)0x20007728u, 1);
}

/* Called once per DMA block, before its sample loop. Configuration is frozen
 * during TX. Two equal block snapshots reduce bytewise I2C tearing; they do NOT
 * constitute an atomic multi-register protocol. A commit/ack protocol is still
 * required before installation. Expensive filter calculations here also need
 * cycle measurements against the receive DMA deadline. */
void bb_begin_block(void) {
    uint8_t mode = READ8(0x200085ad), tx = READ8(0x200085af);
    if (mode != previous_mode || tx != previous_tx) {
        tx_dsp_reset(&dsp);
        tx_interpolation_reset();
        BB_CYCLE_EVENT(BB_CYCLE_STATE_RESET);
    }
    previous_mode = mode;
    previous_tx = tx;
    if (bb_lab_arm != LAB_COOKIE) {
        if (configured) {
            tx_interpolation_reset();
            BB_CYCLE_EVENT(BB_CYCLE_INTERPOLATION_SWITCH);
        }
        configured = false;
        if (!tx && filter_changed) {
            set_oem_filter(300.0f, 3000.0f);
            filter_changed = false;
            BB_CYCLE_EVENT(BB_CYCLE_FILTER_RESTORE);
        }
        stable_blocks = 0;
        return;
    }
    if (tx) return;
    if (READF(0x2000ba44) != 12500.0f) {
        configured = false;
        tx_interpolation_reset();
        ++bb_rejected_configurations;
        BB_CYCLE_EVENT(BB_CYCLE_CONFIG_REJECT);
        return;
    }
    const uint32_t words[3] = {
        READ32(0x2000c4c8 + 25 * 4),
        READ32(0x2000c4c8 + 36 * 4),
        READ32(0x2000c4c8 + 38 * 4)
    };
    bool same = true, applied = configured;
    for (unsigned n = 0; n < 3; ++n) {
        same = same && words[n] == stable_words[n];
        applied = applied && words[n] == last_words[n];
        stable_words[n] = words[n];
    }
    stable_blocks = same ? (stable_blocks < 2 ? stable_blocks + 1 : 2) : 0;
    if (stable_blocks < 2 || applied) return;
    tx_dsp_config candidate;
    tx_protocol_words wire = {words[0], words[1], words[2]};
    if (!tx_protocol_decode(&wire, &dsp.config, &candidate)) {
        ++bb_rejected_configurations;
        BB_CYCLE_EVENT(BB_CYCLE_CONFIG_REJECT);
        return;
    }
    /* Configure all patch state together in the processing context. */
    bool edges_changed = candidate.filter_low_hz != dsp.config.filter_low_hz ||
        candidate.filter_high_hz != dsp.config.filter_high_hz;
    if (!tx_dsp_reconfigure(&dsp, &candidate)) {
        ++bb_rejected_configurations;
        BB_CYCLE_EVENT(BB_CYCLE_CONFIG_REJECT);
        return;
    }
    tx_interpolation_reset();
    if (!configured || edges_changed) {
        set_oem_filter(candidate.filter_low_hz, candidate.filter_high_hz);
        filter_changed = candidate.filter_low_hz != 300.0f || candidate.filter_high_hz != 3000.0f;
    }
    for (unsigned n = 0; n < 3; ++n) last_words[n] = words[n];
    configured = true;
    ++bb_applied_configurations;
    BB_CYCLE_EVENT(BB_CYCLE_CONFIG_APPLY);
}

static bool voice_tx(void) {
    if (bb_lab_arm != LAB_COOKIE || !configured || !READ8(0x200085af)) return false;
    /* Take one mode snapshot per check; retain live interlock/PTT checks at
     * every callback rather than caching activation across a DMA block. */
    uint8_t mode = READ8(0x200085ad);
    return mode == 0 || mode == 2;
}

void bb_tx_audio(float *sample) {
    if (!voice_tx()) return;
    /* Keep r13's raw audio convention for investigation. The relationship
     * between its -57 dB threshold and stock V1.1.9 gain must be measured.
     * Do not apply r13 codec gain or TX output scaling here. */
    *sample = tx_dsp_compress(&dsp, *sample);
}

void bb_tx_iq(float *i, float *q) {
    if (!voice_tx()) return;
    /* Nominal r13 clipping units and I/Q sign require target validation. */
    tx_dsp_cessb(&dsp, READ8(0x200085ad) == 0, i, q);
}

/* Redirect only the two verified TX call arguments. Never write an OEM
 * descriptor: RX and all bypass modes retain their original FIR/state. */
const void *bb_tx_interpolator(const void *oem_instance) {
    return tx_interpolation_select(oem_instance, voice_tx() && dsp.config.cessb_on);
}

#ifdef BB_CYCLE_PROFILE
#undef bb_begin_block
#undef bb_tx_audio
#undef bb_tx_iq
#undef bb_tx_interpolator
void bb_begin_block(void) {
    uint32_t start = bb_cycle_block_start();
    bb_begin_block_impl();
    bb_cycle_block_finish(start, READ8(0x200085af) != 0);
}
void bb_tx_audio(float *sample) {
    uint32_t start = bb_cycle_start();
    bb_tx_audio_impl(sample);
    bb_cycle_finish(1, start);
}
void bb_tx_iq(float *i, float *q) {
    uint32_t start = bb_cycle_start();
    bb_tx_iq_impl(i, q);
    bb_cycle_finish(2, start);
}
const void *bb_tx_interpolator(const void *oem_instance) {
    uint32_t start = bb_cycle_start();
    const void *selected = bb_tx_interpolator_impl(oem_instance);
    bb_cycle_finish(BB_CYCLE_SELECTOR, start);
    return selected;
}
#endif
