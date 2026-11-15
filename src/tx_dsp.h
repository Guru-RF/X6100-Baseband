#ifndef X6100_TX_DSP_H
#define X6100_TX_DSP_H

#include <stdbool.h>
#include <stdint.h>

/* No OEM addresses. The adapter owns this state and serializes configuration,
 * reset and processing in the audio interrupt context. No dynamic allocation.
 * CESSB runs AFTER SSB generation/filtering and BEFORE OEM output gain/ALC.
 * Compression runs on mono voice audio BEFORE SSB generation/filtering.
 * Neither entry point may process CW, DATA, AM or FM in this first port.
 */
#define TX_DSP_FIR_TAPS 65u
#define TX_DSP_DELAY 32u
#define TX_DSP_COMP_WINDOW 60u

typedef struct {
    float sample_rate_hz;       /* Rate of BOTH entry points; adapter must verify. */
    float filter_low_hz;
    float filter_high_hz;       /* Must match the OEM audio TX filter. */
    bool compressor_on;
    bool gate_on;
    float compressor_ratio;   /* >= 1 */
    float threshold_db;        /* dB relative to sample amplitude 1.0 */
    float makeup_db;
    bool cessb_on;
    float cessb_boost_db;
    float cessb_threshold;      /* Envelope amplitude in the verified OEM units. */
} tx_dsp_config;

typedef struct {
    tx_dsp_config config;
    float fir[TX_DSP_FIR_TAPS];
    float correction_i[TX_DSP_FIR_TAPS], correction_q[TX_DSP_FIR_TAPS];
    float delay_i[TX_DSP_DELAY], delay_q[TX_DSP_DELAY];
    uint32_t correction_pos, delay_pos;
    float oscillator_re, oscillator_im, step_re, step_im;
    uint32_t oscillator_count;
    float cessb_gain;
    int sideband;
    float audio[TX_DSP_COMP_WINDOW], squares[TX_DSP_COMP_WINDOW];
    uint32_t audio_pos;
    float square_sum, comp_reduction, gate_reduction, smooth_reduction;
    float attack_alpha, release_alpha;
} tx_dsp_state;

/* r13-compatible nominal DSP settings; both features are disabled by default.
 * Values are NOT evidence that a particular OEM hook uses these signal units. */
tx_dsp_config tx_dsp_defaults(void);
bool tx_dsp_valid_config(const tx_dsp_config *config);
/* Invalid configuration leaves state untouched. A valid change resets histories.
 * Filter coefficients are prepared here; do not invoke repeatedly per sample. */
bool tx_dsp_configure(tx_dsp_state *state, const tx_dsp_config *config);
/* For a successfully configured state only: reuse unchanged filter, gain and
 * time constants. Valid calls still reset all histories; invalid calls leave
 * state untouched. Use configure for the first call, not reconfigure. */
bool tx_dsp_reconfigure(tx_dsp_state *state, const tx_dsp_config *config);
/* Call at each voice-TX start to prevent previous transmissions leaking out. */
void tx_dsp_reset(tx_dsp_state *state);
float tx_dsp_compress(tx_dsp_state *state, float sample);
/* lower_sideband false = positive-frequency USB, true = negative-frequency LSB.
 * This sign convention MUST be verified against OEM I/Q order by the adapter. */
void tx_dsp_cessb(tx_dsp_state *state, bool lower_sideband, float *i, float *q);

#endif
