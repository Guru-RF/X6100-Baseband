#!/usr/bin/env python3
"""Exercise GUI refresh/activation/PTT interleavings against the actual TEST ELF.

Command bytes are deposited in the decoded OEM command array, with DMA callback
boundaries explicitly interleaved. This does not emulate the I2C ISR, electrical
failures, real interrupt scheduling, or RF transients. The OEM filter is stubbed
by verify_adapter.Machine. No radio access or firmware changes are performed.
"""
import argparse
import hashlib
import json
from pathlib import Path

from unicorn import UC_HOOK_CODE
from unicorn import arm_const as arm
from verify_adapter import Machine, RATE, REGS, TEST_TOKEN, float_bits

DESCRIPTORS = (0x20007490, 0x200075DC)


def prepared(path, compressor=True, cessb=True):
    m = Machine(path)
    assert m.test_build, 'explicit TEST ELF required'
    m.call('patch_init')
    m.put32(RATE, float_bits(12500.0))
    m.flags(tx=False)
    m.arm()
    m.registers(compressor=3 if compressor else 16,
                cessb=((1 << 8) if cessb else 0) | (37 << 9), low=250, high=2700)
    m.blocks()
    assert m.u32('bb_applied_configurations') == 1
    return m


def selected(m, descriptor):
    m.call('bb_tx_interpolator', descriptor)
    return m.cpu.reg_read(arm.UC_ARM_REG_R0)


def private_state(m):
    return bytes(m.cpu.mem_read(m.symbols['__data_start'],
                               m.symbols['__bss_end'] - m.symbols['__data_start']))


def identical_refreshes(path):
    results = []
    for tx in (False, True):
        m = prepared(path)
        m.flags(tx=tx); m.blocks()
        if tx:
            for _ in range(16):
                m.samples()
        # Guard private interpolation histories as well as the DSP state.
        m.cpu.mem_write(m.symbols['tx_interpolation_state'], bytes(range(64)))
        before, calls = private_state(m), list(m.filter_calls)
        words = bytes(m.cpu.mem_read(REGS, 56 * 4))
        # DMA may interrupt at any command byte. Values are identical, including
        # reg36's token arriving in address order before reg38.
        for offset, value in enumerate(words):
            m.cpu.mem_write(REGS + offset, bytes([value]))
            m.blocks(1)
            assert private_state(m) == before, (tx, offset, 'refresh changed private state')
            assert m.filter_calls == calls
        results.append({'tx': tx, 'byte_boundaries': len(words),
                        'state_filter_and_application_count_unchanged': True})
    return results


def interpolation_matrix(path):
    cases = 0
    for compressor in (False, True):
        for cessb in (False, True):
            m = prepared(path, compressor, cessb)
            assert m.filter_calls[-1] == (12500.0, 250.0, 2700.0)
            for tx, mode in [(False, 2)] + [(True, mode) for mode in range(8)]:
                m.flags(mode, tx); m.blocks(1)
                active = tx and mode in (0, 2) and cessb
                for descriptor in DESCRIPTORS:
                    assert (selected(m, descriptor) != descriptor) == active
                    cases += 1
                if tx and not compressor and not cessb:
                    m.bypass()
    return {'selector_checks': cases, 'token_alone_selects_private_interpolation': False,
            'selection_requires': 'armed/configured voice LSB/USB TX AND applied CESSB enable',
            'filter_only_250_2700_applies_with_both_effects_off': True}


def ptt_boundaries(path):
    # A GUI transaction already in flight when the BASE (not GUI packet) TX
    # flag rises. Check both callback-per-write and no-callback-between-writes.
    words = [(36, 45 << 9), (25, 16), (38, 300 | (2400 << 16)),
             (36, TEST_TOKEN | (45 << 9))]
    results = []
    for tick_each_write in (False, True):
        for key_before_write in range(len(words) + 1):
            m = prepared(path)
            for index in range(len(words) + 1):
                if index == key_before_write:
                    m.flags(tx=True)
                    applied_at_key = m.u32('bb_applied_configurations')
                    filters_at_key = list(m.filter_calls)
                if index < len(words):
                    register, word = words[index]
                    m.put32(REGS + register * 4, word)
                    if tick_each_write:
                        m.blocks(1)
                if index >= key_before_write:
                    assert m.u32('bb_applied_configurations') == applied_at_key
                    assert m.filter_calls == filters_at_key
            m.blocks(4)
            assert m.u32('bb_applied_configurations') == applied_at_key
            assert m.filter_calls == filters_at_key
            m.flags(tx=False); m.blocks(3)
            assert m.filter_calls[-1] == (12500.0, 300.0, 2400.0)
            results.append({'callback_after_each_write': tick_each_write,
                            'tx_before_write_index': key_before_write,
                            'new_config_deferred_until_rx': True})
    return results


def within_block_token_pulse(path):
    m = prepared(path)
    m.flags(tx=True); m.blocks(1)
    for _ in range(16):
        m.samples()
    before, count = private_state(m), m.u32('bb_applied_configurations')
    first_channel = selected(m, DESCRIPTORS[0])
    m.arm(False)
    m.bypass()
    assert all(selected(m, d) == d for d in DESCRIPTORS)
    assert first_channel != DESCRIPTORS[0] and selected(m, DESCRIPTORS[1]) == DESCRIPTORS[1]
    assert private_state(m) == before
    # No bb_begin_block observes the cleared token. Restoring it permits the
    # old applied state to resume in this TX; no new configuration is applied.
    m.arm()
    assert all(selected(m, d) != d for d in DESCRIPTORS)
    assert private_state(m) == before
    m.samples()
    assert private_state(m) != before
    assert m.u32('bb_applied_configurations') == count
    return {'observed': 'clear bypasses callbacks; republish before a block resumes old applied state',
            'clear_between_channel_selectors_can_mix_private_and_oem': True,
            'history_reset_before_resume': False, 'new_configuration_applied': False,
            'limit': 'does not prove transition continuity or bound RF overshoot'}


def transition_after_rx_snapshot(path):
    m = prepared(path)
    m.registers(compressor=16, low=160, high=3000)
    injected = False

    def key_at_reconfigure(cpu, address, _size, _user):
        nonlocal injected
        if address == (m.symbols['tx_dsp_reconfigure'] & ~1):
            m.flags(tx=True)
            injected = True

    m.cpu.hook_add(UC_HOOK_CODE, key_at_reconfigure)
    m.blocks(3)
    assert injected and m.u32('bb_applied_configurations') == 2
    assert m.filter_calls[-1] == (12500.0, 160.0, 3000.0)
    return {'injected_tx_transition': 'at reconfigure entry, after block RX snapshot',
            'configuration_still_applied': True,
            'limit': 'injected scheduling scenario, not proof of actual TX-writer interrupt timing'}


def refresh_after_state_loss(path):
    m = Machine(path)
    assert m.test_build
    m.call('patch_init'); m.put32(RATE, float_bits(12500.0)); m.flags(tx=False)
    m.put32(REGS + 25 * 4, 16)
    m.put32(REGS + 36 * 4, TEST_TOKEN)
    # Simulate a restart/desynchronization and a transfer pause before reg38.
    # This is deliberately NOT an identical refresh of already-applied words.
    m.blocks(3)
    assert m.filter_calls == [(12500.0, 300.0, 3000.0)]
    m.put32(REGS + 38 * 4, 160 | (3000 << 16)); m.blocks(3)
    assert m.filter_calls[-1] == (12500.0, 160.0, 3000.0)
    assert m.u32('bb_applied_configurations') == 2
    return {'pause_before_reg38_in_callback_snapshots': 3,
            'filter_sequence_hz': [[300, 3000], [160, 3000]],
            'requires_explicit_recovery_transaction': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=Path('build/tx-test.elf'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    report = {
        'scope': 'decoded-command-array and callback interleavings; not physical I2C/RF',
        'elf_sha256': hashlib.sha256(args.elf.read_bytes()).hexdigest(),
        'identical_refresh': identical_refreshes(args.elf),
        'interpolation': interpolation_matrix(args.elf),
        'ptt_write_boundaries': ptt_boundaries(args.elf),
        'within_block_token_pulse': within_block_token_pulse(args.elf),
        'transition_after_rx_snapshot': transition_after_rx_snapshot(args.elf),
        'refresh_after_state_loss': refresh_after_state_loss(args.elf),
        'limits': ['A full refresh is a no-op only when all words equal the already applied values.',
                   'Partial/failed writes are not covered by the identical refresh guarantee.',
                   'TX is sampled at block entry; this is not an atomic PTT/configuration transaction.',
                   'No physical I2C, interrupt schedule, audio-transition or RF measurement.'],
    }
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
