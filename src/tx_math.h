#ifndef X6100_TX_MATH_H
#define X6100_TX_MATH_H

#include <float.h>
#include <stdint.h>
#include <string.h>

/* Private compressor math for the bounded, finite values produced by tx_dsp.
 * No aliasing casts or signed shifts: Cortex-M4 and host tests use binary32.
 * The RMS helper receives 1e-12 < mean <= about 1e20. The gain helper receives
 * -400 <= dB <= 24 (including detector transients); neither needs subnormals.
 * Configuration-time CESSB gain and coefficients still use standard libm.
 */
_Static_assert(sizeof(float) == sizeof(uint32_t) && FLT_RADIX == 2 &&
               FLT_MANT_DIG == 24 && FLT_MAX_EXP == 128,
               "Compressor math requires IEEE binary32 float");

static inline float tx_rms_db(float mean) {
    uint32_t bits;
    memcpy(&bits, &mean, sizeof(bits));
    int exponent = (int)(bits >> 23) - 127;
    bits = (bits & UINT32_C(0x007fffff)) | UINT32_C(0x3f800000);
    float mantissa;
    memcpy(&mantissa, &bits, sizeof(mantissa));
    /* ln(m) = 2(z + z^3/3 + ...), z=(m-1)/(m+1) in [0,1/3].
     * Through z^11 the omitted natural-log tail is below 1.09e-7.
     * Float rounding dominates; the test sweeps every mantissa in [1,2).
     */
    float z = (mantissa - 1.0f) / (mantissa + 1.0f), zz = z * z;
    float log_m = z * (1.0f + zz * (1.0f / 3.0f + zz * (1.0f / 5.0f +
                  zz * (1.0f / 7.0f + zz * (1.0f / 9.0f + zz / 11.0f)))));
    return 3.01029995664f * (float)exponent + 8.68588963807f * log_m;
}

static inline float tx_compressor_gain(float db) {
    float x = db * 0.1660964047443681f; /* log2(10) / 20 */
    int exponent = (int)(x + (x >= 0.0f ? 0.5f : -0.5f));
    float fraction = x - (float)exponent; /* [-0.5,0.5] */
    /* Taylor expansion of exp(ln(2)*fraction), through degree 7.
     * The omitted term is below 5.4e-9 throughout this interval.
     * exponent is in [-66,4], so the constructed scale is a normal float.
     */
    float p = 1.0f + fraction * (0.69314718056f + fraction * (0.24022650696f +
              fraction * (0.055504108665f + fraction * (0.009618129108f +
              fraction * (0.001333355815f + fraction * (0.000154035304f +
              fraction * 0.000015252734f))))));
    uint32_t bits = (uint32_t)(exponent + 127) << 23;
    float scale;
    memcpy(&scale, &bits, sizeof(scale));
    return scale * p;
}

#endif
