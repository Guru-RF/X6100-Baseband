#!/usr/bin/env python3
"""Reproduce memory/rollback evidence without contacting or flashing a radio.

Requires capstone, unicorn, pyelftools. Inputs are exact plaintext OEM and r13
images plus the current payload ELF. Optional bench files are compared, never
trusted solely by their names. Pointer absence and emulated accesses are bounded
evidence, not proof of whole-program memory ownership or bootloader behavior.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct
from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB
from capstone.arm import ARM_INS_BL, ARM_INS_B, ARM_INS_MOV, ARM_INS_MOVW, ARM_INS_MOVT, ARM_OP_IMM, ARM_OP_REG
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_MEM_WRITE, UC_HOOK_MEM_READ
from unicorn import arm_const as arm
from elftools.elf.elffile import ELFFile
from plan_patch import validate_oem, BASE

R13_HASH = '42815a8514831e07bce46e1ddf27376af863488af3663e53d450cfb99713591e'
CCM = 0x10000000
AHB1ENR = 0x40023830
MANUAL = 'https://www.st.com/resource/en/reference_manual/rm0090-stm32f4xx-reference-manual-stmicroelectronics.pdf'


def digest(b):
    return hashlib.sha256(b).hexdigest()


def raw_ccm_candidates(image, start=0, end=None):
    end = len(image) if end is None else end
    return [{'location': hex(BASE + off), 'value': hex(struct.unpack_from('<I', image, off)[0])}
            for off in range(start, end - 3, 4)
            if CCM <= struct.unpack_from('<I', image, off)[0] < CCM + 0x10000]


def machine(image, data_source, data_size):
    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
    for address, size in ((0x08000000, 0x100000), (CCM, 0x10000),
                          (0x20000000, 0x40000), (0x40000000, 0x30000), (0xe000e000, 0x1000)):
        cpu.mem_map(address, size)
    cpu.mem_write(BASE, image)
    cpu.mem_write(0x20000000, image[data_source - BASE:data_source - BASE + data_size])
    cpu.mem_write(0xe000ed88, struct.pack('<I', 0xf00000))
    cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xf00000)
    cpu.reg_write(arm.UC_ARM_REG_SP, 0x20028000)
    cpu.reg_write(arm.UC_ARM_REG_LR, 0x08000101)
    return cpu


def call(cpu, function):
    cpu.reg_write(arm.UC_ARM_REG_LR, 0x08000101)
    cpu.emu_start(function | 1, 0x08000100, count=2_000_000)
    if cpu.reg_read(arm.UC_ARM_REG_PC) != 0x08000100:
        raise ValueError('emulation instruction budget exceeded')


def init_memory_trace(r13):
    cpu = machine(r13, 0x0807bba0, 0x3560)
    writes, reads = set(), set()
    outside = set()

    def track_write(cpu, access, addr, size, value, data):
        if CCM <= addr < CCM + 0x10000:
            writes.update(range(addr, addr + size))
        elif not 0x20000000 <= addr < 0x20040000:
            outside.add(addr)

    def track_read(cpu, access, addr, size, value, data):
        if CCM <= addr < CCM + 0x10000:
            reads.update(range(addr, addr + size))

    cpu.hook_add(UC_HOOK_MEM_WRITE, track_write)
    cpu.hook_add(UC_HOOK_MEM_READ, track_read)
    # Real r13 init function: startup branch0x8034a66->0x807f100 calls this.
    call(cpu, 0x0807fae4)
    assert not outside
    assert len(writes) == 16498 and min(writes) == CCM and max(writes) + 1 == 0x100086ed
    return {'entry': '0x0807fae4', 'distinct_ccm_bytes_written': len(writes),
            'written_span_start': hex(min(writes)), 'written_span_end_exclusive': hex(max(writes) + 1),
            'span_bytes': max(writes) + 1 - min(writes),
            'distinct_ccm_bytes_read': len(reads), 'writes_outside_SRAM_CCM': [],
            'scope': 'Actual r13 init executed; zero-initialized emulated CCM. This is an initialized span, not a bound on every runtime access.'}


def system_init_trace(image, source, size, function):
    outcomes = []
    for inherited in (0, 0x00100000):
        cpu = machine(image, source, size)
        cpu.mem_write(AHB1ENR, struct.pack('<I', inherited))
        writes = []
        cpu.hook_add(UC_HOOK_MEM_WRITE,
                     lambda cpu, access, addr, size, value, data: writes.append(addr))
        call(cpu, function)
        after = struct.unpack('<I', cpu.mem_read(AHB1ENR, 4))[0]
        assert AHB1ENR not in writes and after == inherited
        outcomes.append({'inherited_ahb1enr': hex(inherited), 'after_system_init': hex(after),
                         'system_init_writes_ahb1enr': False})
    return outcomes


def clock_calls(image):
    cs = Cs(CS_ARCH_ARM, CS_MODE_THUMB)
    cs.detail = True
    cs.skipdata = True
    # Linear disassembly, not exhaustive control flow reconstruction.
    instructions = list(cs.disasm(image[0x200:0x1cd6c], BASE + 0x200))
    calls = []
    for n, instruction in enumerate(instructions):
        if instruction.id not in (ARM_INS_BL, ARM_INS_B) or not instruction.operands:
            continue
        if instruction.operands[0].type != ARM_OP_IMM or instruction.operands[0].imm != 0x0802e900:
            continue
        registers = {}
        for ins in instructions[max(0, n - 8):n]:
            if ins.id == ARM_INS_BL:
                registers = {}
            elif ins.id in (ARM_INS_MOV, ARM_INS_MOVW, ARM_INS_MOVT) and len(ins.operands) == 2:
                dst, src = ins.operands
                if dst.type != ARM_OP_REG:
                    continue
                value = src.imm if src.type == ARM_OP_IMM else registers.get(src.reg)
                if value is not None:
                    if ins.id == ARM_INS_MOVT:
                        value = (value << 16) | (registers.get(dst.reg, 0) & 0xffff)
                    registers[dst.reg] = value
        # Capstone register ids differ from Unicorn; use names from cs.
        values = {cs.reg_name(reg): value for reg, value in registers.items()}
        calls.append({'site': hex(instruction.address),
                      'mask_from_local_context': hex(values['r0']) if 'r0' in values else None,
                      'enable_from_local_context': values.get('r1')})
    assert calls and all(c['enable_from_local_context'] == 1 for c in calls)
    return calls


def calibration_files(directory):
    before_path = directory / 'base-calibration-f000.bin'
    after_path = directory / 'base-calibration-f000-after-restore.bin'
    before, after = before_path.read_bytes(), after_path.read_bytes()
    identical = before == after
    before_zero = bool(before) and not any(before)
    after_zero = bool(after) and not any(after)
    expected_size = 816  # Record size read by the audited OEM calibration loader.
    valid_sizes = len(before) == len(after) == expected_size
    if not valid_sizes:
        interpretation = ('One or both snapshots do not have the expected 816-byte record size. '
                          'Equality describes files only; calibration preservation is not established.')
    elif identical and after_zero:
        interpretation = ('Snapshots are byte-identical and all zero. '
                          'This does not establish valid calibration data.')
    elif identical:
        interpretation = ('Snapshots are byte-identical. '
                          'File equality alone does not establish calibration validity or update recovery.')
    elif after_zero:
        interpretation = ('Snapshots differ and the after snapshot is all zero. '
                          'This comparison cannot distinguish a transient read, an invalid snapshot or erased calibration.')
    else:
        interpretation = ('Snapshots differ. File comparison alone does not establish '
                          'when or why the data changed.')
    return {'before': {'name': before_path.name, 'size': len(before), 'sha256': digest(before),
                       'nonzero_bytes': sum(x != 0 for x in before), 'all_zero': before_zero},
            'after': {'name': after_path.name, 'size': len(after), 'sha256': digest(after),
                      'nonzero_bytes': sum(x != 0 for x in after), 'all_zero': after_zero},
            'expected_record_bytes': expected_size,
            'record_sizes_valid': valid_sizes,
            'identical': identical,
            'differing_bytes': sum(x != y for x, y in zip(before, after)) + abs(len(before) - len(after)),
            'interpretation': interpretation}


def audit(args):
    oem, r13 = args.oem_bin.read_bytes(), args.r13_bin.read_bytes()
    validate_oem(oem)
    if digest(r13) != R13_HASH:
        raise ValueError('requires exact inspected r13 plaintext image')
    with args.payload_elf.open('rb') as file:
        elf = ELFFile(file)
        symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
        loads = [p for p in elf.iter_segments() if p['p_type'] == 'PT_LOAD' and p['p_filesz']]
        payload_end = max(p['p_paddr'] + p['p_filesz'] for p in loads)
        ccm_end = symbols['__bss_end']
    candidate_literals = raw_ccm_candidates(oem)
    initialized_candidates = raw_ccm_candidates(oem, 0x5c5d0, 0x5c5d0 + 0x3570)
    assert not initialized_candidates
    clock_result = clock_calls(oem)
    result = {
        'schema_version': 2,
        'status': 'offline application-memory audit; no hardware or bootloader validation',
        'images': {'oem_sha256': digest(oem), 'r13_sha256': digest(r13),
                   'oem_end': hex(BASE + len(oem)), 'r13_end': hex(BASE + len(r13))},
        'proposed_payload': {'elf_sha256': digest(args.payload_elf.read_bytes()),
                             'flash_start': hex(min(p['p_paddr'] for p in loads)),
                             'flash_end_exclusive': hex(payload_end),
                             'extended_application_span_bytes': payload_end - BASE,
                             'r13_application_span_bytes': len(r13),
                             'fits_within_supplied_r13_flash_span': payload_end <= BASE + len(r13),
                             'ccm_start': hex(symbols['__data_start']), 'ccm_end_exclusive': hex(ccm_end),
                             'ccm_state_bytes': ccm_end - symbols['__data_start']},
        'r13_initialized_memory': init_memory_trace(r13),
        'stock_ccm_scan': {'aligned_raw_word_candidates': candidate_literals,
                           'initialized_data_pointer_candidates': initialized_candidates,
                           'candidate_interpretation': 'Seven aligned words are instruction encodings;0x0803c800 is a float literal loaded into s0 at0x0803c7f8 (value2.524e-29), not a pointer dereference.',
                           'ownership_conclusion': 'No identified OEM direct CCM use, and no CCM pointer in initialized data. Indirect/computed pointers, interrupts not executed and bootloader state are not exhaustively excluded.'},
        'ccm_clock': {'reference': MANUAL, 'register': hex(AHB1ENR), 'enable_bit': 20,
                      'documented_reset_value': '0x00100000',
                      'oem_system_init': system_init_trace(oem, 0x0807c5d0, 0x3570, 0x0803485c),
                      'r13_oem_system_init': system_init_trace(r13, 0x0807bba0, 0x3560, 0x08034870),
                      'direct_ahb1_clock_calls': clock_result,
                      'conclusion': 'Normal reset enables CCM. Both SystemInit routines preserve its inherited clock bit; all found direct AHB1 helper calls enable other masks. This does not exclude a bootloader changing inherited RCC state or unrecognized writes.'},
        'flash_layout': {'settings_sector2': '0x08008000..0x0800c000',
                          'calibration_primary_sector1': '0x08004000..0x08008000',
                          'application_start_sector5': '0x08020000',
                          'calibration_backup_sector11': '0x080e0000..0x08100000',
                          'payload_avoids_known_settings_and_calibration': 0x08080000 <= payload_end < 0x080e0000},
        'update_rollback': {'bootloader_image_examined': False,
                            'updater_source': 'fventuri/x6100-base-experiments commit0627a333c9ea74cca1aa6bcdfb7097cc3d9610ca, base-flasher/x6100_base_flash.c',
                            'source_claim': 'Comment says IAP r erases sectors5-10; implementation sends r and waits for ACK. Host cannot establish or enforce the erased sector set.',
                            'guard_limit': 'That updater validates MSP/reset vector but does not bound image length below calibration sector11. Our planner bounds payload below0x080a0000; a future installer must enforce total encrypted/padded image extent too.',
                            'conclusion': 'The payload extent is compared with the supplied r13 application image only. Application images do not establish exact IAP erasure, power-failure recovery or V1.1.9 rollback behavior.'},
        'not_proven': ['Whole-program CCM ownership and nested-IRQ stack high-water',
                       'Actual bootloader erase/write bounds or interrupted-update recovery',
                       'Calibration validity and stable readback across an actual update/rollback'],
    }
    if args.bench_dir:
        result['update_rollback']['retained_calibration_files'] = calibration_files(args.bench_dir)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('oem_bin', type=Path)
    parser.add_argument('r13_bin', type=Path)
    parser.add_argument('--payload-elf', type=Path, default=Path('build/tx-port.elf'))
    parser.add_argument('--bench-dir', type=Path)
    args = parser.parse_args()
    try:
        result = audit(args)
    except (OSError, ValueError) as error:
        parser.exit(1, f'Refused: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
