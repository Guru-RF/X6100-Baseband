/* DSP port research, based on the algorithms in gdyuldin/x6100_base_patch r13
 * (644ac675a1765ce69755870767b4cb00a3e6d7eb), Core/Src/cessb.c and compressor.c.
 * https://github.com/gdyuldin/x6100_base_patch/tree/r13
 * Copyright (C) 2026 Guru-RF and the X6100-Baseband contributors.
 * Our contributions: GPL-3.0-or-later; see LICENSE and NOTICE for attribution.
 * Upstream DSP permission remains unresolved; our grant does not license it.
 * Hardware integration and RF performance remain unvalidated.
 *
 * CESSB retains r13's 65-tap filtered clipping-error correction and 32-sample
 * delay. One correction stage REDUCES overshoot; it is not a hard peak limiter.
 * The mixer uses the conjugate on return (r13 also adds a fixed 90-degree phase
 * rotation). Symmetric FIR evaluation and a recursive oscillator avoid trig in
 * the sample loop. A configuration or sideband change clears old history.
 */
#include "tx_dsp.h"
#include "tx_math.h"
#include <math.h>
#include <stddef.h>
#include <string.h>

#define PI_F 3.14159265358979323846f

static float db_to_gain(float db) { return powf(10.0f, db * 0.05f); }
static bool finite_range(float v, float low, float high) {
    return isfinite(v) && v >= low && v <= high;
}

tx_dsp_config tx_dsp_defaults(void) {
    return (tx_dsp_config){
        .sample_rate_hz = 12500.0f,
        .filter_low_hz = 160.0f, .filter_high_hz = 3000.0f,
        .compressor_on = false, .gate_on = true,
        .compressor_ratio = 2.0f, .threshold_db = -57.0f, .makeup_db = 6.0f,
        .cessb_on = false, .cessb_boost_db = 3.7f,
        .cessb_threshold = 1.1220184543f,
    };
}

bool tx_dsp_valid_config(const tx_dsp_config *c) {
    return c && finite_range(c->sample_rate_hz, 8000.0f, 192000.0f)
        && finite_range(c->filter_low_hz, 0.0f, c->sample_rate_hz * 0.5f)
        && finite_range(c->filter_high_hz, c->filter_low_hz + 100.0f,
                        c->sample_rate_hz * 0.5f - 100.0f)
        && finite_range(c->compressor_ratio, 1.0f, 20.0f)
        && finite_range(c->threshold_db, -100.0f, 0.0f)
        && finite_range(c->makeup_db, -24.0f, 24.0f)
        && finite_range(c->cessb_boost_db, 0.0f, 12.0f)
        && finite_range(c->cessb_threshold, 0.000001f, 1000.0f);
}

static void reset_cessb(tx_dsp_state *s) {
    memset(s->correction_i, 0, sizeof(s->correction_i));
    memset(s->correction_q, 0, sizeof(s->correction_q));
    memset(s->delay_i, 0, sizeof(s->delay_i));
    memset(s->delay_q, 0, sizeof(s->delay_q));
    s->correction_pos = s->delay_pos = s->oscillator_count = 0;
    s->oscillator_re = 1.0f;
    s->oscillator_im = 0.0f;
    s->sideband = 0;
}

void tx_dsp_reset(tx_dsp_state *s) {
    reset_cessb(s);
    memset(s->audio, 0, sizeof(s->audio));
    memset(s->squares, 0, sizeof(s->squares));
    s->audio_pos = 0;
    s->square_sum = s->comp_reduction = s->gate_reduction = s->smooth_reduction = 0;
}

/* Fixed 65-tap Hamming window, first half. Values are binary32 results of
 * 0.54f - 0.46f*cosf(2*pi_f*n/64) in the audited ARM build. Keeping them in
 * flash removes 32 redundant cosine calls whenever the passband changes.
 * The center window is 1; the other half is symmetric by construction. */
static const float correction_window[TX_DSP_DELAY] = {
    0x1.47ae18p-4f, 0x1.50c0b8p-4f, 0x1.6be238p-4f, 0x1.98cfb0p-4f,
    0x1.d71a68p-4f, 0x1.131460p-3f, 0x1.429bf0p-3f, 0x1.79aeb0p-3f,
    0x1.b7c4d4p-3f, 0x1.fc4554p-3f, 0x1.2343a6p-2f, 0x1.4ae9d0p-2f,
    0x1.74b368p-2f, 0x1.a03970p-2f, 0x1.cd1092p-2f, 0x1.faca40p-2f,
    0x1.147ae2p-1f, 0x1.2b90a6p-1f, 0x1.426d7cp-1f, 0x1.58d90ep-1f,
    0x1.6e9c10p-1f, 0x1.8380dep-1f, 0x1.9753f4p-1f, 0x1.a9e470p-1f,
    0x1.bb0490p-1f, 0x1.ca8a18p-1f, 0x1.d84ec8p-1f, 0x1.e430acp-1f,
    0x1.ee1278p-1f, 0x1.f5dbccp-1f, 0x1.fb797ep-1f, 0x1.feddacp-1f,
};
_Static_assert(TX_DSP_FIR_TAPS == 65 && TX_DSP_DELAY == 32,
               "The stored Hamming window is for the 65-tap correction filter");

static void prepare_filter(tx_dsp_state *s, const tx_dsp_config *c) {
    float cutoff = 0.5f * (c->filter_high_hz - c->filter_low_hz) / c->sample_rate_hz;
    float sum = 0.0f;
    for (unsigned n = 0; n < TX_DSP_DELAY; ++n) {
        float offset = (float)n - TX_DSP_DELAY;
        float value = sinf(2.0f * PI_F * cutoff * offset) / (PI_F * offset) * correction_window[n];
        s->fir[n] = s->fir[TX_DSP_FIR_TAPS - 1 - n] = value;
        sum += 2.0f * value;
    }
    s->fir[TX_DSP_DELAY] = 2.0f * cutoff;
    sum += 2.0f * cutoff;
    for (unsigned n = 0; n < TX_DSP_FIR_TAPS; ++n) s->fir[n] /= sum;
    float angle = PI_F * (c->filter_low_hz + c->filter_high_hz) / c->sample_rate_hz;
    s->step_re = cosf(angle);
    s->step_im = sinf(angle);
}

static bool configure(tx_dsp_state *s, const tx_dsp_config *c, bool reuse) {
    if (!s || !tx_dsp_valid_config(c)) return false;
    /* Short-circuit reuse=false keeps first configuration valid for an
     * uninitialized state. Only reconfigure may inspect the old config. */
    bool rate_changed = !reuse || c->sample_rate_hz != s->config.sample_rate_hz;
    bool filter_changed = rate_changed || c->filter_low_hz != s->config.filter_low_hz ||
                          c->filter_high_hz != s->config.filter_high_hz;
    bool boost_changed = !reuse || c->cessb_boost_db != s->config.cessb_boost_db;
    if (filter_changed) prepare_filter(s, c);
    if (boost_changed) s->cessb_gain = db_to_gain(c->cessb_boost_db);
    /* Preserve r13 time constants at 12.5 kHz; keep seconds at other rates. */
    if (rate_changed) {
        s->attack_alpha = 1.0f - powf(1.0f - 0.035957992f, 12500.0f / c->sample_rate_hz);
        s->release_alpha = 1.0f - powf(1.0f - 0.00061015395f, 12500.0f / c->sample_rate_hz);
    }
    s->config = *c;
    tx_dsp_reset(s);
    return true;
}

bool tx_dsp_configure(tx_dsp_state *s, const tx_dsp_config *c) {
    return configure(s, c, false);
}

bool tx_dsp_reconfigure(tx_dsp_state *s, const tx_dsp_config *c) {
    return configure(s, c, true);
}

float tx_dsp_compress(tx_dsp_state *s, float sample) {
    if (!s->config.compressor_on) return sample;
    if (!isfinite(sample) || fabsf(sample) > 1.0e10f) { tx_dsp_reset(s); return 0.0f; }
    unsigned n = s->audio_pos;
    float delayed = s->audio[n];
    float square = sample * sample;
    s->square_sum += square - s->squares[n];
    if (s->square_sum < 0) s->square_sum = 0;
    s->squares[n] = square;
    s->audio[n] = sample;
    s->audio_pos = n + 1 == TX_DSP_COMP_WINDOW ? 0 : n + 1;
    /* Rebase once per window so subtractive round-off cannot retain the level
     * of an earlier loud signal indefinitely. */
    if (s->audio_pos == 0) {
        s->square_sum = 0.0f;
        for (unsigned k = 0; k < TX_DSP_COMP_WINDOW; ++k) s->square_sum += s->squares[k];
    }
    float mean = s->square_sum / TX_DSP_COMP_WINDOW;
    float level = mean > 1.0e-12f ? tx_rms_db(mean) : -120.0f;
    float comp = level > s->config.threshold_db
        ? (level - s->config.threshold_db) * (1.0f - 1.0f / s->config.compressor_ratio) : 0.0f;
    float gate = s->config.gate_on && level < s->config.threshold_db
        ? fminf(40.0f, (s->config.threshold_db - level) * 3.0f) : 0.0f;
    float release = s->release_alpha;
    s->comp_reduction = fmaxf(comp, (1.0f - release) * s->comp_reduction + release * comp);
    s->gate_reduction = fminf(gate, (1.0f - release) * s->gate_reduction + release * gate);
    s->smooth_reduction += s->attack_alpha * (s->comp_reduction + s->gate_reduction - s->smooth_reduction);
    return delayed * tx_compressor_gain(s->config.makeup_db - s->smooth_reduction);
}

static void correction_filter(const float *coeff, const float *history_i,
                              const float *history_q, unsigned newest,
                              float *out_i, float *out_q) {
    unsigned center = newest >= TX_DSP_DELAY ? newest - TX_DSP_DELAY
                                            : newest + TX_DSP_FIR_TAPS - TX_DSP_DELAY;
    float result_i = coeff[TX_DSP_DELAY] * history_i[center];
    float result_q = coeff[TX_DSP_DELAY] * history_q[center];
    /* Each symmetric pair walks in opposite directions. With 32 pairs in a
     * 65-slot ring, at most one side wraps. Split there once, rather than
     * checking both indices after every tap; retain the exact sum order. */
    unsigned a = newest, b = newest + 1 == TX_DSP_FIR_TAPS ? 0 : newest + 1;
    unsigned span = TX_DSP_DELAY;
    if (a + 1 < span) span = a + 1;
    if (TX_DSP_FIR_TAPS - b < span) span = TX_DSP_FIR_TAPS - b;
    for (unsigned k = 0; k < span; ++k) {
        result_i += coeff[k] * (history_i[a - k] + history_i[b + k]);
        result_q += coeff[k] * (history_q[a - k] + history_q[b + k]);
    }
    a = a + 1 == span ? TX_DSP_FIR_TAPS - 1 : a - span;
    b = b + span == TX_DSP_FIR_TAPS ? 0 : b + span;
    for (unsigned k = span; k < TX_DSP_DELAY; ++k) {
        unsigned offset = k - span;
        result_i += coeff[k] * (history_i[a - offset] + history_i[b + offset]);
        result_q += coeff[k] * (history_q[a - offset] + history_q[b + offset]);
    }
    *out_i = result_i; *out_q = result_q;
}

void tx_dsp_cessb(tx_dsp_state *s, bool lower_sideband, float *i, float *q) {
    if (!s->config.cessb_on) return;
    if (!isfinite(*i) || !isfinite(*q) || fabsf(*i) > 1.0e10f || fabsf(*q) > 1.0e10f) {
        tx_dsp_reset(s); *i = *q = 0.0f; return;
    }
    int sideband = lower_sideband ? 1 : -1;
    if (s->sideband != sideband) {
        /* sideband==0 is already clean after configure/reset. */
        if (s->sideband != 0) reset_cessb(s);
        s->sideband = sideband;
    }
    float re = s->oscillator_re, im = s->oscillator_im;
    float centered_i = (*i * re - *q * im) * s->cessb_gain;
    float centered_q = (*i * im + *q * re) * s->cessb_gain;
    unsigned delay_pos = s->delay_pos, fir_pos = s->correction_pos;
    float delayed_i = s->delay_i[delay_pos], delayed_q = s->delay_q[delay_pos];
    s->delay_i[delay_pos] = centered_i; s->delay_q[delay_pos] = centered_q;
    s->delay_pos = (delay_pos + 1) % TX_DSP_DELAY;
    float magnitude_squared = centered_i * centered_i + centered_q * centered_q;
    float threshold = s->config.cessb_threshold;
    float correction = magnitude_squared > threshold * threshold
        ? threshold / sqrtf(magnitude_squared) - 1.0f : 0.0f;
    s->correction_i[fir_pos] = centered_i * correction;
    s->correction_q[fir_pos] = centered_q * correction;
    float correction_i, correction_q;
    correction_filter(s->fir, s->correction_i, s->correction_q, fir_pos, &correction_i, &correction_q);
    centered_i = delayed_i + correction_i;
    centered_q = delayed_q + correction_q;
    s->correction_pos = fir_pos + 1 == TX_DSP_FIR_TAPS ? 0 : fir_pos + 1;
    *i = centered_i * re + centered_q * im;
    *q = centered_q * re - centered_i * im;
    float step_im = (float)sideband * s->step_im;
    s->oscillator_re = re * s->step_re - im * step_im;
    s->oscillator_im = im * s->step_re + re * step_im;
    if (++s->oscillator_count == 256) {
        float norm = 1.0f / sqrtf(s->oscillator_re * s->oscillator_re + s->oscillator_im * s->oscillator_im);
        s->oscillator_re *= norm; s->oscillator_im *= norm; s->oscillator_count = 0;
    }
}
