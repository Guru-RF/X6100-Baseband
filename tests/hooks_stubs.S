.syntax unified
.cpu cortex-m4
.fpu fpv4-sp-d16
.thumb
/* verify_hooks.py intercepts these entries and simulates AAPCS32 callbacks:
 * call-clobbered registers/status change, callee-saved registers survive.
 * Separate negative controls deliberately violate that contract. */
.section .text.stub,"ax",%progbits
.global bb_tx_audio,bb_tx_iq,bb_tx_interpolator,bb_begin_block,patch_init
.thumb_func
bb_tx_audio: bx lr
.thumb_func
bb_tx_iq: bx lr
.thumb_func
bb_tx_interpolator: bx lr
.thumb_func
bb_begin_block: bx lr
.thumb_func
patch_init: bx lr
