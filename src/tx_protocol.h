#ifndef X6100_TX_PROTOCOL_H
#define X6100_TX_PROTOCOL_H

#include "tx_dsp.h"

/* tx1a2-test GUI opt-in. Used only by the explicitly selected BB_TEST_BUILD.
 * For every configuration change while receiving, publish reg36 with its
 * high halfword clear, then reg25 (compressor) and reg38 (TX filter), then
 * reg36 = TX_TEST_CONTROL_TOKEN | CESSB_low16 last. Keep compressor bit4 set
 * and CESSB bit8 clear for initial bypass. This is not an acknowledgement or
 * fully atomic I2C transaction; the adapter also requires stable snapshots.
 * Stock and legacy r13 command words cannot provide this explicit opt-in.
 */
#define TX_TEST_CONTROL_MASK 0xffff0000u
#define TX_TEST_CONTROL_TOKEN 0x54580000u
#define TX_TEST_BUILD_MARKER 0x54535432u

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
