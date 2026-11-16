/* V1.1.9 251127001 ONLY. SHA256 plaintext:
 * 3fa11293e06951c21c9cfda04ea08adad85eaadda26b77a8e8bbcca8820c53c4
 * Used by the experimental image builder; target timing remains unvalidated.
 * Sample call sites have 8-byte aligned SP (DMA handler prologue allocates
 * 32 bytes GPR + 40 bytes VFP (d8-d12) + 104 bytes local = 176 bytes).
 */
.syntax unified
.cpu cortex-m4
.fpu fpv4-sp-d16
.thumb

/* Patch callbacks and every function they call use the bare-metal GNU
 * AAPCS32 hard-float ABI. The callee owns preservation of r4-r11 (including
 * the toolchain's ordinary r9/v6) and s16-s31. Save only call-clobbered
 * registers here, plus APSR/FPSCR because the interrupted OEM code can keep
 * their state live across this inserted call. No naked/non-AAPCS callback
 * may be introduced without re-auditing these wrappers.
 * https://github.com/ARM-software/abi-aa/blob/main/aapcs32/aapcs32.rst
 * Sections 6.1.1 and 6.1.2.1. Total frame: 24 + 64 + 8 = 96 bytes.
 */
.equ CONTEXT_BYTES, 96
.equ SAVED_R0, 72
.equ SAVED_R1, 76
.macro save_context
    stmdb sp!, {r0-r3, r12, lr} /* 24 */
    vpush {s0-s15}             /* 64 */
    mrs r0, APSR
    vmrs r1, FPSCR
    push {r0,r1}              /* 8; total 96 */
.endm
.macro restore_context
    pop {r0,r1}
    vmsr FPSCR, r1
    msr APSR_nzcvqg, r0
    vpop {s0-s15}
    ldmia sp!, {r0-r3, r12, lr}
.endm

/* Replace BL at 0x080247ca (11 f0 19 fc) with B.W here.
 * This is the FIRST TX BPF biquad: r0=0x20007728, r1=input at entry
 * SP+0x48, r2=output at entry SP+0x4c, r3=1. Compress before both stock
 * cascades so compression products are filtered before SSB modulation.
 * bb_tx_audio() must preserve bypass modes and input amplitude convention.
 * It must NOT apply the post-filter gain factors to this pre-filter sample.
 */
.section .text.v119_tx_audio_wrapper,"ax",%progbits
.global v119_tx_audio_wrapper
.thumb_func
v119_tx_audio_wrapper:
    save_context
    ldr r0, [sp, #SAVED_R1]
    bl bb_tx_audio
    restore_context
    ldr lr, =0x080247cf
    ldr pc, =0x08036001 /* original first biquad; returns into OEM */
.ltorg

/* Replace BL at 0x080267ce (0e f0 c9 fd) with B.W here.
 * Q at entry SP+0x50, I at SP+0x54; original r1 points Q.
 * bb_tx_iq() MUST bypass every mode except voice LSB=0 and USB=2.
 * Both calls retain the OEM interpolation kernel. Only r0 is redirected to
 * a private descriptor when CESSB voice TX is active.
 */
.section .text.v119_tx_iq_wrapper,"ax",%progbits
.global v119_tx_iq_wrapper
.thumb_func
v119_tx_iq_wrapper:
    save_context
    add r0, sp, #(CONTEXT_BYTES + 0x54) /* original SP+0x54 -> I */
    add r1, sp, #(CONTEXT_BYTES + 0x50) /* original SP+0x50 -> Q */
    bl bb_tx_iq
    ldr r0, [sp, #SAVED_R0] /* saved original interpolation descriptor */
    bl bb_tx_interpolator
    str r0, [sp, #SAVED_R0] /* sole intended register change at OEM tail */
    restore_context
    ldr lr, =0x080267d3
    ldr pc, =0x08035365 /* tail call original interpolation, returns OEM */
.ltorg

/* Replace BL at 0x08026866 (0e f0 7d fd), the second TX interpolation call.
 * Its input is I at entry SP+0x54. CESSB has already processed both channels
 * in the first wrapper; only select the matching independent FIR history. */
.section .text.v119_tx_iq_second_wrapper,"ax",%progbits
.global v119_tx_iq_second_wrapper
.thumb_func
v119_tx_iq_second_wrapper:
    save_context
    ldr r0, [sp, #SAVED_R0]
    bl bb_tx_interpolator
    str r0, [sp, #SAVED_R0]
    restore_context
    ldr lr, =0x0802686b
    ldr pc, =0x08035365
.ltorg

/* Replace BL at 0x0802459a (09 f0 1b fe). This is the second DMA
 * current-buffer read, after the interrupt-status early exit. Preserve r0:
 * OEM immediately compares it with 1 to choose the input/output buffers.
 * The block callback is intentionally outside the per-sample loop.
 */
.section .text.v119_begin_block_wrapper,"ax",%progbits
.global v119_begin_block_wrapper
.thumb_func
v119_begin_block_wrapper:
    ldr lr, =2f + 1
    ldr pc, =0x0802e1d5
2:
    save_context
    bl bb_begin_block
    restore_context
    ldr lr, =0x0802459f
    ldr pc, =0x0802459f
.ltorg

/* Replace BL at 0x08034a4a (ff f7 95 fb), the call to OEM main.
 * This runs after SystemInit (0x0803485c) and the C library initializer
 * (0x08037ed0), but before main configures/enables DSP peripherals.
 * patch_init owns only linker-reserved payload state; it must not clear
 * OEM RAM. In particular, deciding which CCM region is free is a separate
 * validation requirement, not established by this wrapper.
 */
.section .text.v119_init_wrapper,"ax",%progbits
.global v119_init_wrapper
.thumb_func
v119_init_wrapper:
    save_context
    bl patch_init
    restore_context
    ldr lr, =0x08034a4f
    ldr pc, =0x08034179
.ltorg
