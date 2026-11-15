#ifndef TX_INTERPOLATION_H
#define TX_INTERPOLATION_H

#include <stdbool.h>

/* For the two verified V1.1.9 8x TX interpolation calls, blockSize=1 only.
 * The caller supplies the armed/configured CESSB voice-TX gate. Unknown OEM
 * instances and inactive calls pass through without reading or writing them.
 * All functions run in the same DMA processing context as the TX DSP. */
const void *tx_interpolation_select(const void *oem_instance, bool active);

/* Reset the private I/Q histories on configuration or TX/mode transitions.
 * Never resets or changes any OEM descriptor, coefficient or state buffer. */
void tx_interpolation_reset(void);

#endif
