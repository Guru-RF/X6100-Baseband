#!/usr/bin/env python3
"""Verify probe accounting with an emulated DWT. Does not measure hardware time."""
import argparse
import os
from pathlib import Path
import struct
import subprocess
import tempfile
from unicorn import UC_HOOK_CODE
from verify_adapter import Machine, ROOT, SAMPLES, RATE, COOKIE, float_bits

DWT, COUNTER, DEMCR = 0xe0001000, 0xe0001004, 0xe000edfc
STAT_WORDS = 6


def verify_arithmetic(cc):
    """A deterministic fake MMIO clock verifies wrap/accumulation exactly.

    Unicorn's M-class DWT implementation is not a target clock model. Keep
    exact arithmetic checks independent from its counter behavior.
    """
    source = r'''
#include <assert.h>
#include <stdint.h>
#include <string.h>
static uint32_t demcr, ctrl, counter;
static uint32_t *fake_register(uintptr_t address) {
    switch (address) {
    case 0xe000edfcu: return &demcr;
    case 0xe0001000u: return &ctrl;
    case 0xe0001004u: return &counter;
    default: assert(0); return 0;
    }
}
#define BB_REG32(a) (*fake_register(a))
#define BB_CYCLE_BARRIER() ((void)0)
#include "cycle_profile.h"
int main(void) {
    demcr = 0x80; ctrl = 0x10; counter = 0xfffffff0u;
    uint32_t start = bb_cycle_start();
    assert(start == 0xfffffff0u && counter == start);
    assert(demcr == 0x01000080u && ctrl == 0x11u);
    counter = 0x20; bb_cycle_finish(BB_CYCLE_AUDIO, start);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].last_cycles == 48);
    start = bb_cycle_start(); counter += 16;
    bb_cycle_finish(BB_CYCLE_AUDIO, start);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].calls == 2);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].minimum_cycles == 16);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].maximum_cycles == 48);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].total_cycles_low == 64);
    bb_cycle_stats[BB_CYCLE_AUDIO].total_cycles_low = 0xfffffff0u;
    bb_cycle_stats[BB_CYCLE_AUDIO].total_cycles_high = 5;
    start = bb_cycle_start(); counter += 32;
    bb_cycle_finish(BB_CYCLE_AUDIO, start);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].total_cycles_low == 16);
    assert(bb_cycle_stats[BB_CYCLE_AUDIO].total_cycles_high == 6);
    start = bb_cycle_block_start();
    BB_CYCLE_EVENT(BB_CYCLE_STATE_RESET);
    BB_CYCLE_EVENT(BB_CYCLE_CONFIG_APPLY);
    counter += 21; bb_cycle_block_finish(start, 0);
    assert(bb_cycle_stats[BB_CYCLE_BLOCK].calls == 1);
    assert(bb_cycle_stats[BB_CYCLE_RESET_PATH].last_cycles == 21);
    assert(bb_cycle_stats[BB_CYCLE_CONFIG_PATH].last_cycles == 21);
    assert(bb_cycle_stats[BB_CYCLE_UNCHANGED_RX].calls == 0);
    start = bb_cycle_block_start(); counter += 7;
    bb_cycle_block_finish(start, 0);
    start = bb_cycle_block_start(); counter += 9;
    bb_cycle_block_finish(start, 1);
    assert(bb_cycle_stats[BB_CYCLE_UNCHANGED_RX].last_cycles == 7);
    assert(bb_cycle_stats[BB_CYCLE_UNCHANGED_TX].last_cycles == 9);
    assert(bb_cycle_stats[BB_CYCLE_RESET_PATH].calls == 1);
    assert(bb_cycle_stats[BB_CYCLE_BLOCK].total_cycles_low == 37);
    memset((void *)bb_cycle_stats, 0, sizeof bb_cycle_stats);
    bb_cycle_probe_status = 0; ctrl = 1u << 25; counter = 123;
    assert(bb_cycle_block_start() == 0);
    assert(bb_cycle_probe_status == 2 && counter == 123);
    counter += 50; bb_cycle_block_finish(0, 0);
    bb_cycle_finish(BB_CYCLE_AUDIO, 0);
    for (unsigned n = 0; n < BB_CYCLE_STAT_COUNT; ++n)
        assert(bb_cycle_stats[n].calls == 0);
    return 0;
}
'''
    with tempfile.TemporaryDirectory(prefix='x6100-cycle-accounting-') as tmp:
        src, exe = Path(tmp) / 'accounting.c', Path(tmp) / 'accounting'
        src.write_text(source)
        subprocess.run([cc, '-std=c11', '-O2', '-Wall', '-Wextra', '-Werror',
                        '-I'+str(ROOT/'firmware'), str(src), '-o', str(exe)], check=True)
        subprocess.run([str(exe)], check=True)
    print('PASS: exact wrap, min/max, 64-bit accumulation, overlapping block paths, unavailable counter')

class ProbeMachine(Machine):
    def __init__(self, path, no_counter=False):
        super().__init__(path)
        self.cpu.mem_map(DWT, 0x1000)
        self.cpu.mem_write(DWT, struct.pack('<I', (1 << 25) if no_counter else 0))
        self.ticks = 0xffffff00
        self.cpu.hook_add(UC_HOOK_CODE, self.advance_counter)
        self.cpu.ctl_remove_cache(0x08000000, 0x08100000)
    def advance_counter(self, cpu, address, size, user):
        # Updating emulated MMIO inside a memory-read hook corrupts Thumb IT
        # progression in Unicorn 2.1.4 (a following BL may be skipped). Advance
        # this deliberately synthetic clock BETWEEN instructions instead.
        self.ticks = (self.ticks + 1000) & 0xffffffff
        cpu.mem_write(COUNTER, struct.pack('<I', self.ticks))
    def write(self, cpu, access, address, size, value, user):
        if address in (DWT, DEMCR) and size == 4:
            return
        super().write(cpu, access, address, size, value, user)

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--elf',type=Path,default=ROOT/'build/cycle-profile/tx-port-profile.elf')
p.add_argument('--cc',default=os.environ.get('CC','cc'))
a=p.parse_args()
verify_arithmetic(a.cc)
for unavailable in (False, True):
    m=ProbeMachine(a.elf,unavailable)
    m.call('patch_init');m.flags(tx=False)
    m.call('bb_begin_block');m.call('bb_tx_audio',SAMPLES);m.call('bb_tx_iq',SAMPLES,SAMPLES+4)
    assert m.u32('bb_cycle_probe_status') == (2 if unavailable else 1)
    for index in range(3):
        stat=struct.unpack('<6I',m.cpu.mem_read(m.symbols['bb_cycle_stats']+index*24,24))
        if unavailable:
            assert stat == (0,)*STAT_WORDS,stat
        else:
            assert stat[0] == 1 and stat[1] > 0 and stat[1:5] == (stat[1],)*4 and stat[5] == 0,stat

    # Exercise event classification in the compiled adapter, including paths
    # which overlap (disarm restores the filter and resets interpolation).
    m = ProbeMachine(a.elf, unavailable)
    m.call('patch_init')
    m.put32(RATE, float_bits(12500.0))
    m.registers(cessb=256 | (37 << 9), low=160)
    m.flags(tx=False)
    m.put32('bb_lab_arm', COOKIE)
    m.blocks()
    m.flags(tx=True)
    m.blocks(2)
    m.call('bb_tx_interpolator', 0x20007490)
    m.call('bb_tx_interpolator', 0x200075dc)
    m.flags(tx=False)
    m.blocks(1)
    m.registers(low=3000, high=100)
    m.blocks()
    m.put32('bb_lab_arm', 0)
    m.blocks(1)
    expected = {0: 10, 3: 3, 4: 1, 5: 3, 6: 1, 7: 1, 8: 1, 9: 1, 10: 2}
    for index in range(11):
        calls = struct.unpack('<I',m.cpu.mem_read(m.symbols['bb_cycle_stats']+index*24,4))[0]
        assert calls == (0 if unavailable else expected.get(index,0)), (index,calls,expected)
print('PASS: emulated callback counters, maxima and unavailable-counter handling')
print('PASS: compiled adapter event classification and both interpolator selector calls')
print('No hardware cycles or real-time guarantee measured.')
