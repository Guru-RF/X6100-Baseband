/* Copyright (C) 2026 Guru-RF and the X6100-Baseband contributors.
 * Our contributions: GPL-3.0-or-later; see LICENSE and NOTICE for attribution.
 * Includes reference adaptations of r13 CESSB/compressor behavior; upstream
 * permission remains unresolved and is not supplied by our license grant.
 */
#include "tx_dsp.h"
#include "tx_math.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

#define PI 3.14159265358979323846

static float gain_at(const tx_dsp_state *s, double frequency) {
    double re = 0, im = 0;
    for (unsigned n = 0; n < TX_DSP_FIR_TAPS; ++n) {
        double phase = -2 * PI * frequency * n / s->config.sample_rate_hz;
        re += s->fir[n] * cos(phase); im += s->fir[n] * sin(phase);
    }
    return (float)hypot(re, im);
}

static void bypass_and_validation(void) {
    tx_dsp_state s = {0}, before;
    tx_dsp_config c = tx_dsp_defaults();
    assert(tx_dsp_configure(&s, &c));
    for (int n = -1000; n <= 1000; ++n) {
        float i = (float)n * 0.001f, q = -i, old_i = i, old_q = q;
        assert(tx_dsp_compress(&s, i) == i);
        tx_dsp_cessb(&s, false, &i, &q);
        assert(i == old_i && q == old_q);
    }
    before = s;
    c.filter_high_hz = c.filter_low_hz;
    assert(!tx_dsp_configure(&s, &c)); assert(memcmp(&s, &before, sizeof(s)) == 0);
    c = tx_dsp_defaults(); c.sample_rate_hz = NAN; assert(!tx_dsp_configure(&s, &c));
    c = tx_dsp_defaults(); c.cessb_threshold = 0; assert(!tx_dsp_configure(&s, &c));
    c = tx_dsp_defaults(); c.compressor_ratio = INFINITY; assert(!tx_dsp_configure(&s, &c));
    c = tx_dsp_defaults(); c.filter_low_hz = -1; assert(!tx_dsp_configure(&s, &c));
    assert(!tx_dsp_configure(NULL, &c)); assert(!tx_dsp_valid_config(NULL));
}

static void filters(void) {
    tx_dsp_state s;
    tx_dsp_config c = tx_dsp_defaults();
    assert(tx_dsp_configure(&s, &c));
    assert(fabsf(gain_at(&s, 0) - 1) < 1e-5f);
    assert(gain_at(&s, 800) > 0.98f);
    assert(gain_at(&s, 2400) < 0.01f);
    c.filter_high_hz = 1000;
    assert(tx_dsp_configure(&s, &c));
    assert(gain_at(&s, 100) > 0.97f);
    assert(gain_at(&s, 1000) < 0.01f);
}

static void filter_precision(void) {
    const float rates[] = {8000, 12500, 48000, 192000};
    double maximum = 0;
    for (unsigned rate = 0; rate < 4; ++rate) {
        for (unsigned width = 0; width < 17; ++width) {
            tx_dsp_config c = tx_dsp_defaults();
            c.sample_rate_hz = rates[rate]; c.filter_low_hz = 0;
            c.filter_high_hz = 100 + (rates[rate] * 0.5f - 200) * width / 16;
            tx_dsp_state s;
            assert(tx_dsp_configure(&s, &c));
            double expected[TX_DSP_FIR_TAPS], sum = 0;
            double cutoff = (double)c.filter_high_hz / (2 * c.sample_rate_hz);
            for (unsigned n = 0; n < TX_DSP_FIR_TAPS; ++n) {
                double offset = (double)n - TX_DSP_DELAY;
                double sinc = n == TX_DSP_DELAY ? 2 * cutoff :
                    sin(2 * PI * cutoff * offset) / (PI * offset);
                expected[n] = sinc * (0.54 - 0.46 * cos(2 * PI * n / 64));
                sum += expected[n];
            }
            for (unsigned n = 0; n < TX_DSP_FIR_TAPS; ++n) {
                double error = fabs(s.fir[n] - expected[n] / sum);
                if (error > maximum) maximum = error;
                assert(s.fir[n] == s.fir[TX_DSP_FIR_TAPS - 1 - n]);
            }
        }
    }
    printf("Correction filter vs double-precision design: max coefficient error %.9g\n", maximum);
    assert(maximum < 0.0000002);
}

/* Test every ring phase using arbitrary existing clipping-error histories.
 * The reference uses modulo addressing, independent of the production spans,
 * while keeping the specified symmetric accumulation order for exact checking. */
static void cessb_ring_boundaries(void) {
    tx_dsp_config c = tx_dsp_defaults(); c.cessb_on = true;
    tx_dsp_state initial;
    assert(tx_dsp_configure(&initial, &c));
    uint32_t random = 0xa609f713;
    for (unsigned repetition = 0; repetition < 128; ++repetition) {
        for (unsigned newest = 0; newest < TX_DSP_FIR_TAPS; ++newest) {
            tx_dsp_state s = initial;
            s.correction_pos = newest; s.sideband = -1;
            for (unsigned n = 0; n < TX_DSP_FIR_TAPS; ++n) {
                random ^= random << 13; random ^= random >> 17; random ^= random << 5;
                s.correction_i[n] = (float)(random & 65535) / 16384 - 2;
                random ^= random << 13; random ^= random >> 17; random ^= random << 5;
                s.correction_q[n] = (float)(random & 65535) / 16384 - 2;
            }
            /* A zero input replaces this slot with zero correction. */
            s.correction_i[newest] = s.correction_q[newest] = 0;
            unsigned center = (newest + TX_DSP_FIR_TAPS - TX_DSP_DELAY) % TX_DSP_FIR_TAPS;
            float expected_i = s.fir[TX_DSP_DELAY] * s.correction_i[center];
            float expected_q = s.fir[TX_DSP_DELAY] * s.correction_q[center];
            for (unsigned k = 0; k < TX_DSP_DELAY; ++k) {
                unsigned a = (newest + TX_DSP_FIR_TAPS - k) % TX_DSP_FIR_TAPS;
                unsigned b = (newest + 1 + k) % TX_DSP_FIR_TAPS;
                expected_i += s.fir[k] * (s.correction_i[a] + s.correction_i[b]);
                expected_q += s.fir[k] * (s.correction_q[a] + s.correction_q[b]);
            }
            float i = 0, q = 0;
            tx_dsp_cessb(&s, false, &i, &q);
            assert(i == expected_i && q == expected_q);
        }
    }
}

static void reconfiguration(void) {
    tx_dsp_state s = {0}, fresh = {0}, before;
    tx_dsp_config c = tx_dsp_defaults();
    c.cessb_on = c.compressor_on = true;
    assert(tx_dsp_configure(&s, &c));
    for (unsigned change = 0; change < 8; ++change) {
        for (unsigned n = 0; n < 200; ++n) {
            float i = 2, q = 1;
            tx_dsp_cessb(&s, false, &i, &q); (void)tx_dsp_compress(&s, 0.5f);
        }
        if (change == 0) c.makeup_db = 12;
        if (change == 1) c.threshold_db = -30;
        if (change == 2) c.cessb_boost_db = 6;
        if (change == 3) c.filter_low_hz = 300;
        if (change == 4) c.filter_high_hz = 2400;
        if (change == 5) c.sample_rate_hz = 48000;
        if (change == 6) c.cessb_on = false;
        /* The eighth iteration reuses an entirely unchanged configuration. */
        assert(tx_dsp_reconfigure(&s, &c));
        assert(tx_dsp_configure(&fresh, &c));
        assert(memcmp(&s, &fresh, sizeof(s)) == 0);
    }
    before = s; c.compressor_ratio = 0;
    assert(!tx_dsp_reconfigure(&s, &c));
    assert(memcmp(&s, &before, sizeof(s)) == 0);
    assert(!tx_dsp_reconfigure(NULL, &c));
}

static float compress_dc(tx_dsp_config c, float input) {
    tx_dsp_state s;
    assert(tx_dsp_configure(&s, &c));
    float out = 0;
    for (unsigned n = 0; n < 80000; ++n) out = tx_dsp_compress(&s, input);
    return out;
}

static void compression(void) {
    tx_dsp_config c = tx_dsp_defaults();
    c.compressor_on = true; c.gate_on = false;
    c.threshold_db = -20; c.makeup_db = 0; c.compressor_ratio = 4;
    /* 0 dB input -> -15 dB at 4:1 above -20 dB threshold. */
    assert(fabsf(20 * log10f(compress_dc(c, 1.0f)) + 15.0f) < 0.02f);
    assert(fabsf(compress_dc(c, 0.01f) - 0.01f) < 1e-5f);
    c.makeup_db = 6;
    assert(fabsf(20 * log10f(compress_dc(c, 1.0f)) + 9.0f) < 0.02f);
    c.makeup_db = 0; c.gate_on = true;
    assert(compress_dc(c, 0.001f) < 0.000011f);
    tx_dsp_state s;
    assert(tx_dsp_configure(&s, &c));
    for (unsigned n = 0; n < 10000; ++n) assert(tx_dsp_compress(&s, 0) == 0);
    assert(tx_dsp_compress(&s, NAN) == 0);
    assert(isfinite(tx_dsp_compress(&s, 0.1f)));
}

static void compressor_math(void) {
    double max_db_error = 0, max_gain_error = 0;
    /* Exhaust the binary32 mantissa interval used by the logarithm polynomial. */
    for (uint32_t bits = UINT32_C(0x3f800000); bits < UINT32_C(0x40000000); ++bits) {
        float x;
        memcpy(&x, &bits, sizeof(x));
        double error = fabs(tx_rms_db(x) - 10.0 * log10((double)x));
        if (error > max_db_error) max_db_error = error;
    }
    /* Exercise exponent reconstruction across the entire accepted RMS domain,
     * including values adjacent to powers of two and the detector floor. */
    for (unsigned exponent = 87; exponent <= 193; ++exponent) {
        for (uint32_t mantissa = 0; mantissa < UINT32_C(0x800000); mantissa += 2047) {
            uint32_t bits = (exponent << 23) | mantissa;
            float x;
            memcpy(&x, &bits, sizeof(x));
            if (x <= 1e-12f || x > 1.0001e20f) continue;
            double error = fabs(tx_rms_db(x) - 10.0 * log10((double)x));
            if (error > max_db_error) max_db_error = error;
        }
    }
    for (unsigned n = 0; n <= 4240000; ++n) {
        float db = -400.0f + (float)n * 0.0001f;
        double reference = pow(10.0, (double)db / 20.0);
        double error = fabs(tx_compressor_gain(db) / reference - 1.0);
        if (error > max_gain_error) max_gain_error = error;
    }
    assert(tx_compressor_gain(0.0f) == 1.0f);
    printf("Compressor math: max level error %.9g dB, max gain relative error %.9g\n",
           max_db_error, max_gain_error);
    assert(max_db_error < 0.00004);
    assert(max_gain_error < 0.000006);
}

/* The previous compressor's libm path is an independent accuracy reference.
 * It intentionally preserves its window, detector and smoothing arithmetic. */
static float reference_compress(tx_dsp_state *s, float sample) {
    unsigned n = s->audio_pos;
    float delayed = s->audio[n], square = sample * sample;
    s->square_sum += square - s->squares[n];
    if (s->square_sum < 0) s->square_sum = 0;
    s->squares[n] = square; s->audio[n] = sample;
    s->audio_pos = (n + 1) % TX_DSP_COMP_WINDOW;
    if (s->audio_pos == 0) {
        s->square_sum = 0;
        for (unsigned k = 0; k < TX_DSP_COMP_WINDOW; ++k) s->square_sum += s->squares[k];
    }
    float mean = s->square_sum / TX_DSP_COMP_WINDOW;
    float level = mean > 1e-12f ? 10.0f * log10f(mean) : -120.0f;
    float comp = level > s->config.threshold_db
        ? (level - s->config.threshold_db) * (1.0f - 1.0f / s->config.compressor_ratio) : 0;
    float gate = s->config.gate_on && level < s->config.threshold_db
        ? fminf(40, (s->config.threshold_db - level) * 3.0f) : 0;
    float release = s->release_alpha;
    s->comp_reduction = fmaxf(comp, (1 - release) * s->comp_reduction + release * comp);
    s->gate_reduction = fminf(gate, (1 - release) * s->gate_reduction + release * gate);
    s->smooth_reduction += s->attack_alpha * (s->comp_reduction + s->gate_reduction - s->smooth_reduction);
    return delayed * powf(10.0f, (s->config.makeup_db - s->smooth_reduction) * 0.05f);
}

static void compressor_reference(void) {
    const float rates[] = {8000, 12500, 48000, 192000};
    double maximum_relative = 0, maximum_db = 0;
    uint32_t random = 0x12345678;
    for (unsigned scenario = 0; scenario < 288; ++scenario) {
        tx_dsp_config c = tx_dsp_defaults();
        c.compressor_on = true; c.gate_on = (scenario / 48 % 2) != 0;
        c.sample_rate_hz = rates[scenario % 4];
        unsigned threshold = scenario / 4 % 3, ratio = scenario / 12 % 4, makeup = scenario / 96;
        c.threshold_db = threshold == 0 ? -100 : threshold == 1 ? -57 : 0;
        c.compressor_ratio = ratio == 0 ? 1 : ratio == 1 ? 2 : ratio == 2 ? 4 : 20;
        c.makeup_db = makeup == 0 ? -24 : makeup == 1 ? 6 : 24;
        tx_dsp_state s, reference;
        assert(tx_dsp_configure(&s, &c)); reference = s;
        for (unsigned n = 0; n < 24000; ++n) {
            random ^= random << 13; random ^= random >> 17; random ^= random << 5;
            float noise = (float)(random & 65535) / 32768.0f - 1;
            /* Silence, below gate, nominal audio, high input bound, and step
             * decay expose drift, gate transitions, saturation and resetting. */
            float amplitude = n < 1000 ? 0 : n < 4000 ? 1e-7f : n < 8000 ? 0.02f :
                              n < 10000 ? 1e10f : n < 13000 ? 0 : n < 18000 ? 1 : 0.001f;
            float sample = amplitude * noise;
            float actual = tx_dsp_compress(&s, sample), expected = reference_compress(&reference, sample);
            assert(isfinite(actual));
            double db_error = fabs((double)s.smooth_reduction - reference.smooth_reduction);
            if (db_error > maximum_db) maximum_db = db_error;
            if (expected != 0) {
                double relative = fabs((double)actual / expected - 1);
                if (relative > maximum_relative) maximum_relative = relative;
            } else assert(actual == 0);
        }
    }
    printf("Compressor vs previous libm: max relative output error %.9g; detector %.9g dB\n",
           maximum_relative, maximum_db);
    assert(maximum_relative < 0.0001);
    assert(maximum_db < 0.0007);
}

/* Independent direct FIR/mixer reference: double-precision phase, shift arrays,
 * no ring buffer/symmetric summation/recursive oscillator shared with module. */
static void state_transitions(void) {
    tx_dsp_state s;
    tx_dsp_config c = tx_dsp_defaults(); c.cessb_on = true; c.compressor_on = true;
    assert(tx_dsp_configure(&s, &c));
    for (unsigned n = 0; n < 200; ++n) {
        float i = 2, q = 1; tx_dsp_cessb(&s, false, &i, &q);
        (void)tx_dsp_compress(&s, 0.5f);
    }
    /* A sideband change must not output a previous USB transmission's tail. */
    float i = 0, q = 0;
    tx_dsp_cessb(&s, true, &i, &q); assert(i == 0 && q == 0);
    c.filter_high_hz = 2000;
    assert(tx_dsp_configure(&s, &c));
    for (unsigned n = 0; n < 100; ++n) {
        i = q = 0; tx_dsp_cessb(&s, false, &i, &q); assert(i == 0 && q == 0);
        assert(tx_dsp_compress(&s, 0) == 0);
    }
    c.compressor_on = true; c.gate_on = false; c.compressor_ratio = 1; c.makeup_db = 0;
    assert(tx_dsp_configure(&s, &c));
    float delayed[TX_DSP_COMP_WINDOW] = {0};
    for (unsigned n = 0; n < 10000; ++n) {
        float sample = sinf((float)n * 0.12f);
        unsigned slot = n % TX_DSP_COMP_WINDOW;
        assert(tx_dsp_compress(&s, sample) == delayed[slot]);
        delayed[slot] = sample;
    }
}

static void cessb_reference(void) {
    tx_dsp_config c = tx_dsp_defaults(); c.cessb_on = true;
    for (unsigned lsb = 0; lsb < 2; ++lsb) {
        tx_dsp_state s;
        assert(tx_dsp_configure(&s, &c));
        double history_i[65] = {0}, history_q[65] = {0};
        double delay_i[32] = {0}, delay_q[32] = {0};
        double max_error = 0, peak_in = 0, peak_out = 0;
        for (unsigned n = 0; n < 25000; ++n) {
            double sign = lsb ? -1 : 1;
            double p1 = sign * 2 * PI * 700 * n / c.sample_rate_hz;
            double p2 = sign * 2 * PI * 2300 * n / c.sample_rate_hz;
            float i = (float)(0.65 * (cos(p1) + cos(p2)));
            float q = (float)(0.65 * (sin(p1) + sin(p2)));
            double angle = -sign * PI * (c.filter_low_hz + c.filter_high_hz) * n / c.sample_rate_hz;
            double re = cos(angle), im = sin(angle), boost = pow(10, c.cessb_boost_db / 20.0);
            double bi = (i * re - q * im) * boost, bq = (i * im + q * re) * boost;
            double di = delay_i[31], dq = delay_q[31];
            memmove(delay_i + 1, delay_i, sizeof(double) * 31);
            memmove(delay_q + 1, delay_q, sizeof(double) * 31);
            delay_i[0] = bi; delay_q[0] = bq;
            double mag = hypot(bi, bq);
            double corr = mag > c.cessb_threshold ? c.cessb_threshold / mag - 1 : 0;
            memmove(history_i + 1, history_i, sizeof(double) * 64);
            memmove(history_q + 1, history_q, sizeof(double) * 64);
            history_i[0] = bi * corr; history_q[0] = bq * corr;
            for (unsigned k = 0; k < 65; ++k) { di += history_i[k] * s.fir[k]; dq += history_q[k] * s.fir[k]; }
            double reference_i = di * re + dq * im, reference_q = dq * re - di * im;
            tx_dsp_cessb(&s, lsb != 0, &i, &q);
            double error = hypot(i - reference_i, q - reference_q);
            if (error > max_error) max_error = error;
            if (n > 100) {
                if (mag > peak_in) peak_in = mag;
                if (hypot(i, q) > peak_out) peak_out = hypot(i, q);
            }
        }
        printf("%s reference max error %.8g; boosted input peak %.5f, CESSB peak %.5f (threshold %.5f)\n",
               lsb ? "LSB" : "USB", max_error, peak_in, peak_out, c.cessb_threshold);
        assert(max_error < 0.0002);
        assert(peak_out < peak_in * 0.85);
        /* A single correction stage is deliberately NOT asserted to be bounded
         * by the clipping threshold; the printed residual overshoot is real. */
        tx_dsp_reset(&s);
        for (unsigned n = 0; n < 200; ++n) { float i = 0, q = 0; tx_dsp_cessb(&s, lsb != 0, &i, &q); assert(i == 0 && q == 0); }
        float i = NAN, q = 0; tx_dsp_cessb(&s, lsb != 0, &i, &q); assert(i == 0 && q == 0);
    }
}

int main(void) {
    bypass_and_validation(); filters(); filter_precision(); reconfiguration(); compression(); compressor_math(); compressor_reference();
    state_transitions(); cessb_ring_boundaries(); cessb_reference();
    printf("DSP tests passed; state size %zu bytes. RF/timing unvalidated.\n", sizeof(tx_dsp_state));
    return 0;
}
