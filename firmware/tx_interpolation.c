/* TX-only interpolation correction for the V1.1.9 research payload.
 * Copyright (C) 2026 Guru-RF and the X6100-Baseband contributors.
 * Our contributions: GPL-3.0-or-later; see LICENSE and NOTICE for attribution.
 * Upstream coefficient permission remains unresolved; our grant excludes it.
 *
 * Coefficients: gdyuldin/x6100_base_patch, fir_ssb_cw_8.txt at
 * 644ac675a1765ce69755870767b4cb00a3e6d7eb, reversed and rounded to float32
 * as upstream patch.py does. Exact bytes match r13 at 0x0803d82c:
 * SHA256 d6f29c698597e5a8f5ac831dfd45dee937e721e203ba961949ef16cb53769e93.
 * Hex float literals keep those bits independent of decimal conversion.
 *
 * The stock 0x0803e3e4 table also supplies RX filters. It and every OEM RAM
 * descriptor/history remain untouched: wrappers redirect only the two known
 * TX calls to these private descriptors while CESSB voice TX is active.
 */
#include "tx_interpolation.h"
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint8_t factor;
    uint8_t padding;
    uint16_t phase_length;
    const float *coefficients;
    float *state;
} interpolation_instance;

_Static_assert(sizeof(interpolation_instance) == 12, "OEM CMSIS ARM ABI");
_Static_assert(offsetof(interpolation_instance, coefficients) == 4, "CMSIS pCoeffs");
_Static_assert(offsetof(interpolation_instance, state) == 8, "CMSIS pState");

static const float tx_interpolation_coefficients[64] = {
    0x1.6978b2p-13f, 0x1.7e7142p-12f, 0x1.46ce14p-11f, 0x1.e54802p-11f,
    0x1.424214p-10f, 0x1.822d54p-10f, 0x1.9e5e5cp-10f, 0x1.80e7eap-10f,
    0x1.14efc4p-10f, 0x1.295ef8p-12f, -0x1.cc962cp-11f, -0x1.3bf5c2p-9f,
    -0x1.1580bep-8f, -0x1.96b8e4p-8f, -0x1.09fae6p-7f, -0x1.3deb64p-7f,
    -0x1.5d318ep-7f, -0x1.5d52a6p-7f, -0x1.345454p-7f, -0x1.b3ab0ap-8f,
    -0x1.209270p-9f, 0x1.059978p-8f, 0x1.84b70ap-7f, 0x1.5b52f6p-6f,
    0x1.03438cp-5f, 0x1.5e160cp-5f, 0x1.b9761cp-5f, 0x1.082486p-4f,
    0x1.2eb49cp-4f, 0x1.4e0878p-4f, 0x1.6420a0p-4f, 0x1.6f8cfap-4f,
    0x1.6f8cfap-4f, 0x1.6420a0p-4f, 0x1.4e0878p-4f, 0x1.2eb49cp-4f,
    0x1.082486p-4f, 0x1.b9761cp-5f, 0x1.5e160cp-5f, 0x1.03438cp-5f,
    0x1.5b52f6p-6f, 0x1.84b70ap-7f, 0x1.059978p-8f, -0x1.209270p-9f,
    -0x1.b3ab0ap-8f, -0x1.345454p-7f, -0x1.5d52a6p-7f, -0x1.5d318ep-7f,
    -0x1.3deb64p-7f, -0x1.09fae6p-7f, -0x1.96b8e4p-8f, -0x1.1580bep-8f,
    -0x1.3bf5c2p-9f, -0x1.cc962cp-11f, 0x1.295ef8p-12f, 0x1.14efc4p-10f,
    0x1.80e7eap-10f, 0x1.9e5e5cp-10f, 0x1.822d54p-10f, 0x1.424214p-10f,
    0x1.e54802p-11f, 0x1.46ce14p-11f, 0x1.7e7142p-12f, 0x1.6978b2p-13f
};

/* CMSIS requires phaseLength+blockSize-1=8 floats per channel. The instances
 * themselves are const in flash; only the histories consume private CCM. */
static float tx_interpolation_state[2][8];
static const interpolation_instance tx_interpolation_descriptors[2] = {
    {8, 0, 8, tx_interpolation_coefficients, tx_interpolation_state[0]},
    {8, 0, 8, tx_interpolation_coefficients, tx_interpolation_state[1]}
};

const void *tx_interpolation_select(const void *oem_instance, bool active) {
    if (active) {
        if ((uintptr_t)oem_instance == 0x20007490u)
            return &tx_interpolation_descriptors[0];
        if ((uintptr_t)oem_instance == 0x200075dcu)
            return &tx_interpolation_descriptors[1];
    }
    return oem_instance;
}

void tx_interpolation_reset(void) {
    for (unsigned channel = 0; channel < 2; ++channel)
        for (unsigned sample = 0; sample < 8; ++sample)
            tx_interpolation_state[channel][sample] = 0.0f;
}
