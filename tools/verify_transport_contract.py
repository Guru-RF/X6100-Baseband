#!/usr/bin/env python3
"""Audit the exact OEM transport configuration, without hardware or firmware output.

Execute OEM initialization routines and the ISR's bank-selection prefix in
Unicorn. Follow the ISR's control flow with Capstone to find its returns. This
is configuration evidence, not a clock, timing, DMA, RF or concurrency test.
Optional dependencies: capstone and unicorn (loaded only after OEM validation).
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

try:
    from . import plan_patch
except ImportError:
    import plan_patch

BASE = plan_patch.BASE
STOP = 0x08010000
DATA_LOAD, DATA_SIZE = 0x0807c5d0, 0x3570
RCC, AIRCR = 0x40023800, 0xe000ed0c
ISR = 0x08024528
REGIONS = {
    'rate_initialization_prefix': (0x08021744, 0x080218a2),
    'transport_startup_caller': (0x080244b4, 0x08024528),
    'isr_with_mode_branches': (ISR, 0x08028d68),
    'application_clock': (0x08031480, 0x0803158c),
    'application_init_caller': (0x080317b4, 0x08031818),
    'i2s_dma_configure': (0x080320fc, 0x080322f8),
    'i2s_dma_enable': (0x080322f8, 0x08032368),
    'nvic_setup': (0x08032400, 0x08032470),
    'system_init_and_clock_calculation': (0x0803485c, 0x08034950),
}
STREAMS = {
    0: ('iq_rx', 0x40003c0c, 0x2000d2f4, 0x2000cef4, 0x00052d01),
    3: ('audio_rx', 0x4000380c, 0x2000e2f4, 0x2000def4, 0x00052d11),
    4: ('audio_tx', 0x4000340c, 0x2000daf4, 0x2000d6f4, 0x04062d41),
    5: ('iq_tx', 0x4000400c, 0x2000caf4, 0x2000c6f4, 0x04062d41),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def decode_dma(cr, ndtr):
    """STM32F4 stream CR/NDTR fields; no claim that transfers occurred."""
    psize, msize = (cr >> 11) & 3, (cr >> 13) & 3
    require(psize < 3 and msize < 3, 'reserved DMA transfer width')
    return {
        'channel': (cr >> 25) & 7, 'direction': (cr >> 6) & 3,
        'peripheral_bits': 8 << psize, 'memory_bits': 8 << msize,
        'memory_increment': bool(cr & (1 << 10)),
        'circular': bool(cr & (1 << 8)), 'double_buffer': bool(cr & (1 << 18)),
        'transfer_complete_interrupt': bool(cr & (1 << 4)),
        'enabled': bool(cr & 1), 'ndtr_halfwords': ndtr,
    }


def decode_priority(raw, prigroup, implemented_bits=4):
    preempt_bits = min(7 - prigroup, implemented_bits)
    sub_bits = max(prigroup + implemented_bits - 7, 0)
    logical = raw >> (8 - implemented_bits)
    return {'raw': raw, 'preemption': logical >> sub_bits,
            'subpriority': logical & ((1 << sub_bits) - 1),
            'preemption_bits': preempt_bits, 'subpriority_bits': sub_bits}


def _dependencies():
    try:
        import capstone
        from capstone import arm as csarm
        import unicorn
        from unicorn import arm_const as arm
    except ImportError as error:
        raise RuntimeError('optional audit dependencies: pip install capstone unicorn') from error
    return capstone, csarm, unicorn, arm


def _control_flow(image, cs, arm):
    """Walk instructions reachable within this ISR, treating calls as returning.

    Its sole table branch has six entries, bounded by the preceding cmp/bhi.
    Unsupported indirect branches fail closed, rather than silently dropping
    paths. Calls' bodies and interrupt nesting are outside this traversal.
    """
    decoder = cs.Cs(cs.CS_ARCH_ARM, cs.CS_MODE_THUMB)
    decoder.detail = True

    def instruction(address):
        decoded = list(decoder.disasm(image[address - BASE:address - BASE + 4],
                                      address, count=1))
        require(len(decoded) == 1, f'cannot decode instruction at {address:#x}')
        return decoded[0]

    expected_calls = {
        0x080244be: 0x08021744, 0x080244da: 0x080320fc,
        0x08024502: 0x080322f8, 0x080317b6: 0x08031480,
        0x080317ca: 0x08032400,
    }
    for site, target in expected_calls.items():
        i = instruction(site)
        require(i.id == arm.ARM_INS_BL and i.operands[0].imm == target,
                f'initialization call mismatch at {site:#x}')
    # Verify the bound and default edge before consuming the table as data.
    require(instruction(0x08024802).mnemonic == 'subs' and
            instruction(0x08024802).op_str == 'r3, #2' and
            instruction(0x08024804).mnemonic == 'cmp' and
            instruction(0x08024804).op_str == 'r3, #5' and
            instruction(0x08024806).mnemonic == 'bhi.w' and
            instruction(0x08024806).operands[0].imm == 0x08028d5c,
            'ISR table-branch bounds changed')
    pending, seen, returns, calls = [ISR], {}, set(), set()
    table_targets = []
    while pending:
        address = pending.pop()
        if address in seen:
            continue
        require(ISR <= address < 0x08028d68, f'ISR control flow escaped at {address:#x}')
        i = instruction(address)
        seen[address] = i
        following = address + i.size
        if i.id == arm.ARM_INS_POP and any(
                o.type == arm.ARM_OP_REG and o.reg == arm.ARM_REG_PC for o in i.operands):
            returns.add(address)
        elif i.id == arm.ARM_INS_TBH:
            require(address == 0x0802480a and i.op_str == '[pc, r3, lsl #1]',
                    'unsupported ISR table branch')
            table = struct.unpack_from('<6H', image, address + 4 - BASE)
            table_targets = [address + 4 + 2 * value for value in table]
            pending.extend(table_targets)
        elif i.id in (arm.ARM_INS_BL, arm.ARM_INS_BLX):
            calls.add(address)
            pending.append(following)
        elif i.group(cs.CS_GRP_JUMP):
            targets = [o.imm for o in i.operands if o.type == arm.ARM_OP_IMM]
            require(bool(targets), f'unsupported indirect ISR branch at {address:#x}')
            pending.append(targets[-1])
            if i.id != arm.ARM_INS_B or i.cc != arm.ARM_CC_AL:
                pending.append(following)
        else:
            require(arm.ARM_REG_PC not in i.regs_access()[1],
                    f'unsupported ISR PC write at {address:#x}')
            pending.append(following)
    require(returns == {0x08024552, 0x08027cc8}, 'unexpected ISR returns')
    require(struct.unpack_from('<I', image, (16 + 14) * 4)[0] == ISR | 1,
            'DMA1 Stream3 vector mismatch')
    push, vpush, reserve = (instruction(a) for a in (ISR, ISR + 4, 0x08024538))
    require(push.id == arm.ARM_INS_PUSH and len(push.operands) == 8 and
            vpush.id == arm.ARM_INS_VPUSH and len(vpush.operands) == 5 and
            all(o.type == arm.ARM_OP_REG and arm.ARM_REG_D8 <= o.reg <= arm.ARM_REG_D12
                for o in vpush.operands) and
            reserve.id == arm.ARM_INS_SUB and reserve.op_str == 'sp, #0x68',
            'unexpected ISR prologue stack layout')
    return {
        'vector_index': 30, 'entry': hex(ISR),
        'epilogue_entries': ['0x0802454c', '0x08027cc2'],
        'return_instructions': [hex(a) for a in sorted(returns)],
        'software_frame_bytes': len(push.operands) * 4 + len(vpush.operands) * 8 + 0x68,
        'software_frame_components': {'gpr_bytes': 32, 'vfp_bytes': 40, 'local_bytes': 104},
        'stack_limit': 'Excludes hardware exception frame, callees and nested interrupts.',
        'reachable_instructions': len(seen), 'call_sites_not_descended': len(calls),
        'last_reachable_instruction': hex(max(seen)),
        'table_branch': {'address': '0x0802480a', 'entries': 6,
                         'targets': [hex(a) for a in table_targets]},
        'initialization_calls': {hex(a): hex(b) for a, b in expected_calls.items()},
        'limit': 'Calls assumed to return; no ISR execution-time or exception-entry/exit measurement.',
    }


def audit(image, instruction_limit=100000):
    """Return JSON-serializable evidence for exact plaintext V1.1.9 bytes."""
    plan_patch.validate_oem(image)  # Fail before loading optional dependencies.
    cs, csarm, uc, arm = _dependencies()
    require(instruction_limit > 0, 'instruction limit must be positive')

    class Machine:
        def __init__(self):
            self.cpu = uc.Uc(uc.UC_ARCH_ARM, uc.UC_MODE_THUMB | uc.UC_MODE_MCLASS)
            self.cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
            for address, size in ((0x08000000, 0x100000), (0x20000000, 0x30000),
                                  (0x10000000, 0x10000), (0x40000000, 0x30000),
                                  (0xe000e000, 0x2000)):
                self.cpu.mem_map(address, size)
            self.cpu.mem_write(BASE, image)
            self.cpu.mem_write(0x20000000, image[DATA_LOAD - BASE:DATA_LOAD - BASE + DATA_SIZE])
            self.cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xf00000)
            self.writes, self.runs = [], []
            self.cpu.hook_add(uc.UC_HOOK_MEM_READ, self.read_hook)
            self.cpu.hook_add(uc.UC_HOOK_MEM_WRITE, self.write_hook)

        def word(self, address):
            return struct.unpack('<I', self.cpu.mem_read(address, 4))[0]

        def put(self, address, value):
            self.cpu.mem_write(address, struct.pack('<I', value))

        def read_hook(self, _cpu, _access, address, _size, _value, _user):
            # Explicit successful-lock model. No elapsed time or oscillator
            # accuracy is simulated. Hardware status bits follow enable bits.
            if address == RCC:
                v = self.word(address)
                self.put(address, (v & ~0x02020002) | ((v & 1) << 1) |
                         ((v & 0x10000) << 1) | ((v & 0x1000000) << 1))
            elif address == RCC + 8:
                v = self.word(address)
                self.put(address, (v & ~12) | ((v & 3) << 2))

        def write_hook(self, _cpu, _access, address, size, value, _user):
            if 0x40000000 <= address < 0x40030000 or 0xe000e000 <= address < 0xe0010000:
                row = {'pc': hex(self.cpu.reg_read(arm.UC_ARM_REG_PC)),
                       'address': hex(address), 'size': size, 'value': hex(value)}
                if address == RCC + 4:
                    row['pll_enabled_before_write'] = bool(self.word(RCC) & 0x1000000)
                    require(not row['pll_enabled_before_write'],
                            'OEM wrote PLLCFGR while PLL was enabled in model')
                self.writes.append(row)
            if address == 0xe000ed88:
                self.cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, value)

        def run(self, entry, stop=STOP, r0=None):
            self.cpu.reg_write(arm.UC_ARM_REG_SP, 0x20030000)
            self.cpu.reg_write(arm.UC_ARM_REG_LR, STOP | 1)
            if r0 is not None:
                self.cpu.reg_write(arm.UC_ARM_REG_R0, r0)
            self.cpu.emu_start(entry | 1, stop, count=instruction_limit)
            require(self.cpu.reg_read(arm.UC_ARM_REG_PC) == stop,
                    f'OEM routine {entry:#x} did not reach stop within instruction limit')
            self.runs.append({'entry': hex(entry), 'stop': hex(stop)})

    flow = _control_flow(image, cs, csarm)
    clocks = []
    for inherited in (False, True):
        m = Machine()
        if inherited:
            m.put(RCC, 0x01000001)
            m.put(RCC + 4, 0x07402d08)  # Arbitrary enabled HSI PLL, not an IAP capture.
        m.run(0x0803485c)
        m.run(0x08031480)
        require(m.word(RCC + 4) == 0x04403c0c and m.word(RCC + 8) == 0x940a,
                'unexpected OEM clock configuration')
        core = m.word(0x200033a4)
        require(core == 249999960, 'unexpected OEM integer SystemCoreClock calculation')
        clocks.append({'initial_state': 'synthetic_inherited_pll_on' if inherited else 'zeroed_mmio',
                       'runs': list(m.runs), 'register_writes': list(m.writes),
                       'pllcfgr': hex(m.word(RCC + 4)), 'cfgr': hex(m.word(RCC + 8)),
                       'pwr_cr': hex(m.word(0x40007000)),
                       'flash_acr': hex(m.word(0x40023c00)),
                       'system_core_clock_variable_hz': core})
    # Use the same initialized machine, and its real .data DMA descriptor.
    m.run(0x08032400)
    group = (m.word(AIRCR) >> 8) & 7
    priorities = {}
    for irq, name, expected in ((14, 'dma1_stream3', 0x80), (72, 'i2c3_event', 0xe0),
                                (73, 'i2c3_error', 0xf0)):
        raw = m.cpu.mem_read(0xe000e400 + irq, 1)[0]
        require(raw == expected and group == 5, 'unexpected NVIC configuration')
        priorities[name] = {'irq': irq, **decode_priority(raw, group)}
    m.run(0x080320fc, r0=0x200003c4)
    m.run(0x080322f8)
    dma = []
    for stream, (role, peripheral, m0, m1, expected_cr) in STREAMS.items():
        address = 0x40026010 + stream * 0x18
        cr, ndtr, par, bank0, bank1, fcr = struct.unpack('<6I', m.cpu.mem_read(address, 24))
        require((cr, ndtr, par, bank0, bank1, fcr) ==
                (expected_cr, 512, peripheral, m0, m1, 0), f'DMA stream {stream} changed')
        dma.append({'stream': stream, 'role': role, 'register_address': hex(address),
                    'cr': hex(cr), 'peripheral': hex(par), 'm0': hex(bank0), 'm1': hex(bank1),
                    'bank_bytes': ndtr * 2, **decode_dma(cr, ndtr)})
    i2s = []
    for address, expected in ((0x40003400, 0xc05), (0x40003800, 0xd05),
                              (0x40003c00, 0xd05), (0x40004000, 0xc05)):
        cfgr = m.word(address + 0x1c)
        require(cfgr == expected, 'unexpected I2S configuration')
        i2s.append({'peripheral_base': hex(address), 'i2scfgr': hex(cfgr),
                    'slave': True, 'data_bits': 32, 'channel_bits': 32,
                    'mode': 'receive' if cfgr & 0x100 else 'transmit',
                    'dma_request_cr2': hex(m.word(address + 4))})
    m.run(0x08021744, stop=0x080218a2)
    rates = [struct.unpack('<f', m.cpu.mem_read(a, 4))[0] for a in
             (0x2000ba40, 0x2000ba44, 0x2000ba48, 0x2000ba4c)]
    require(rates == [100000.0, 12500.0, 12500.0, 25000.0] and
            m.word(0x2000ba38) == 128 and m.word(0x2000ba3c) == 512,
            'unexpected OEM rate/block initialization')
    cases = []
    for rx_ct in (0, 1):
        for tx_ct in (0, 1):
            for stream, ct in ((3, rx_ct), (4, tx_ct)):
                address = 0x40026010 + stream * 0x18
                m.put(address, (m.word(address) & ~(1 << 19)) | ct << 19)
            m.put(0x40026000, 1 << 27)  # Synthetic Stream3 transfer-complete.
            m.run(ISR, stop=0x080245c6)
            actual = [m.word(a) for a in (0x200003cc, 0x200003d4, 0x200003c8, 0x200003d0)]
            expected = [STREAMS[s][2 if ct else 3] for s, ct in
                        ((3, rx_ct), (0, rx_ct), (4, tx_ct), (5, tx_ct))]
            require(actual == expected, 'OEM bank ownership selection mismatch')
            cases.append({'stream3_ct': rx_ct, 'stream4_ct': tx_ct,
                          'audio_rx_iq_rx_audio_tx_iq_tx': [hex(a) for a in actual]})
    m.put(0x40026000, 0)
    before = bytes(m.cpu.mem_read(0x200003c8, 16))
    m.run(ISR)
    require(bytes(m.cpu.mem_read(0x200003c8, 16)) == before,
            'spurious ISR changed bank ownership')
    return {
        'schema': 1, 'status': 'verified_configuration_only',
        'input': {'size': len(image), 'sha256': hashlib.sha256(image).hexdigest(),
                  'exact_oem_required': 'V1.1.9 251127001'},
        'tool': {'sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                 'capstone': cs.__version__, 'unicorn': uc.__version__},
        'guards': {name: {'start': hex(start), 'end_exclusive': hex(end),
                          'sha256': hashlib.sha256(image[start - BASE:end - BASE]).hexdigest()}
                   for name, (start, end) in REGIONS.items()},
        'model': {'instruction_limit_per_run': instruction_limit,
                  'routines_stubbed': False, 'full_boot_executed': False,
                  'rcc_ready_and_switch_status': 'immediate successful lock/switch on read',
                  'other_mmio': 'memory backed; no DMA, codec, interrupt nesting or bus timing'},
        'clock': {'runs': clocks, 'hse_compiled_assumption_hz': 25000000,
                  'nominal_core_hz': 250000000, 'nominal_pclk1_hz': 62500000,
                  'nominal_pclk2_hz': 125000000, 'physical_clocks_measured': False,
                  'note': 'OEM integer division computes 249999960 in SystemCoreClock; physical frequency is not inferred from that variable.'},
        'transport': {'dma': dma, 'i2s': i2s, 'bank_stereo_frames': 128,
                      'raw_rate_constant_hz': rates[0], 'primary_rate_constant_hz': rates[1],
                      'audio_rate_constant_hz': rates[2], 'alternate_rate_constant_hz': rates[3],
                      'nominal_bank_period_seconds': 128 / rates[0],
                      'nominal_cycles_per_bank_at_configured_clock': 320000,
                      'physical_rate_or_cadence_measured': False,
                      'bank_selection_cases': cases, 'spurious_irq_leaves_banks_unchanged': True},
        'nvic': {'prigroup': group, 'implemented_priority_bits_assumed': 4,
                 'priorities': priorities,
                 'implication': 'At these priorities I2C3 cannot preempt the DMA ISR; DMA can preempt I2C3. This does not prove transaction atomicity.'},
        'isr': flow, 'runs': m.runs, 'register_writes': m.writes,
        'limits': [
            'Configured constants and bounded emulation do not measure physical HSE, BCLK, WS, DMA cadence or ISR cycles.',
            'OEM SystemInit clears PLLON before PLLCFGR writes in the model; physical shutdown latency and inherited bootloader state still require a target RCC capture.',
            'Only selected initialization routines and the ISR bank-selection prefix execute; codec initialization and full boot are not modeled.',
            'Paired RX streams use Stream3 CT and paired TX streams use Stream4 CT; physical synchronization and transfer errors remain untested.',
            'Interrupt priorities do not prove that settings publish atomically or settle before a hand-mic PTT.',
            'No RF overshoot, spectrum, power, memory ownership or update/rollback conclusion follows from this audit.',
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--oem-bin', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = audit(args.oem_bin.read_bytes())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n')
    print(f'Wrote verified OEM configuration evidence: {args.output} (not hardware timing)')


if __name__ == '__main__':
    main()
