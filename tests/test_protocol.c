#include "tx_protocol.h"
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>

static uint32_t pack_comp(bool bypass, unsigned ratio, int threshold_halfdb, int makeup_halfdb) {
    return (ratio - 2) | (bypass ? 16u : 0u)
        | (((uint32_t)threshold_halfdb & 63u) << 5)
        | (((uint32_t)makeup_halfdb & 63u) << 11);
}

int main(void) {
    tx_dsp_config base = tx_dsp_defaults(), out = {0};
    tx_protocol_words words = {0};
    assert(tx_protocol_decode(&words, &base, &out));
    assert(out.compressor_on); assert(!out.cessb_on);
    assert(out.compressor_ratio == 2); assert(out.threshold_db == -57);
    assert(out.makeup_db == 6); assert(out.cessb_boost_db == 0);
    assert(out.filter_low_hz == 300 && out.filter_high_hz == 3000);
    assert(out.sample_rate_hz == base.sample_rate_hz);
    assert(out.cessb_threshold == base.cessb_threshold);
    assert(out.gate_on == base.gate_on);

    words.compressor = pack_comp(true, 8, -13, 11);
    words.cessb = (1u << 8) | (37u << 9) | 255u; /* CW peak bits irrelevant */
    words.tx_filter = 160u | (3000u << 16);
    assert(tx_protocol_decode(&words, &base, &out));
    assert(!out.compressor_on && out.compressor_ratio == 8);
    assert(out.threshold_db == -63.5f);
    assert(fabsf(out.makeup_db - 23.1875f) < 0.00001f);
    assert(out.cessb_on && fabsf(out.cessb_boost_db - 3.7f) < 0.00001f);
    assert(out.filter_low_hz == 160 && out.filter_high_hz == 3000);

    /* Signed field endpoints and every six-bit threshold encoding, with the
     * gate disabled preserved through decode. No shift of negative integers. */
    base.gate_on = false;
    for (int halfdb = -32; halfdb <= 31; ++halfdb) {
        words.compressor = pack_comp(false, 2, halfdb, -32);
        assert(tx_protocol_decode(&words, &base, &out));
        assert(out.threshold_db == -57.0f + (float)halfdb * 0.5f);
        assert(out.compressor_on && !out.gate_on);
    }
    words.compressor = pack_comp(false, 2, 31, 31);
    assert(tx_protocol_decode(&words, &base, &out));
    assert(out.threshold_db == -41.5f && out.makeup_db == 13.75f);
    words.cessb = 1u << 8;
    assert(tx_protocol_decode(&words, &base, &out));
    assert(out.cessb_on && out.cessb_boost_db == 0);

    tx_dsp_config saved = out;
    words.compressor = pack_comp(false, 17, -32, 31); /* encoded +43.62 dB makeup */
    assert(!tx_protocol_decode(&words, &base, &out));
    assert(memcmp(&out, &saved, sizeof(out)) == 0);
    words.compressor = pack_comp(false, 2, 0, 0);
    words.tx_filter = 3000u | (160u << 16); /* reversed */
    assert(!tx_protocol_decode(&words, &base, &out));
    assert(memcmp(&out, &saved, sizeof(out)) == 0);
    words.tx_filter = 160u | (6500u << 16); /* beyond Nyquist */
    assert(!tx_protocol_decode(&words, &base, &out));
    words.tx_filter = 160u; /* only low set is not the default sentinel */
    assert(!tx_protocol_decode(&words, &base, &out));
    assert(!tx_protocol_decode(NULL, &base, &out));
    assert(!tx_protocol_decode(&words, NULL, &out));
    assert(!tx_protocol_decode(&words, &base, NULL));
    puts("TX protocol tests passed (raw COMP bit4 is bypass; no arming implied).");
    return 0;
}
