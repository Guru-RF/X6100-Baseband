#ifndef X6100_TX_PROTOCOL_H
#define X6100_TX_PROTOCOL_H

#include "tx_dsp.h"

typedef struct {
    uint32_t compressor; /* I2C register 25 */
    uint32_t cessb;      /* I2C register 36; lower CW-peak bits ignored */
    uint32_t tx_filter;  /* I2C register 38 */
} tx_protocol_words;

/* Decode the established r13/OpenX6X00 wire format into an existing DSP config.
 * Preserves its rate, gate option and calibrated CESSB clipping amplitude.
 * All-zero filter selects stock V1.1.9 300..3000 Hz. Zero CESSB boost means
 * zero dB, not the defaults' 3.7 dB. On invalid parameters leaves *result intact.
 * The DSP's experimental +24 dB makeup ceiling rejects some otherwise
 * representable r13 combinations; parameters are never silently clamped.
 *
 * WARNING: compressor register bit4=1 means BYPASS. OpenX6X00 inverts this
 * field in radio.c:on_change_comp_ratio. Zero registers therefore request
 * compression ON; these words alone are NOT an opt-in/arming protocol.
 */
bool tx_protocol_decode(const tx_protocol_words *words,
                        const tx_dsp_config *base, tx_dsp_config *result);

#endif
