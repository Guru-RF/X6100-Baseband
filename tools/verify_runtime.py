#!/usr/bin/env python3
"""Execute the linked ARM memset over alignment, length and byte-value cases.

Checks payload machine code, exact written bytes, surrounding guards and the
returned pointer. Counts in the optional JSON output are instructions, not
cycles. Requires unicorn and pyelftools; never writes a firmware image.
"""
import argparse
import hashlib
import json
from pathlib import Path

from elftools.elf.elffile import ELFFile
from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
from unicorn import arm_const as arm

BUFFER, STACK, RETURN = 0x20010000, 0x20030000, 0x08000100


class Runtime:
    def __init__(self, path):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
        for address, size in ((0x08000000, 0x100000), (0x20000000, 0x40000),
                              (0x10000000, 0x10000)):
            self.cpu.mem_map(address, size)
        with path.open('rb') as stream:
            elf = ELFFile(stream)
            symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
            self.address = symbols['memset']
            for segment in elf.iter_segments():
                if segment['p_type'] == 'PT_LOAD' and segment['p_filesz']:
                    self.cpu.mem_write(segment['p_paddr'], segment.data())
        self.instructions = 0

    def call(self, address, value, length):
        cpu = self.cpu
        cpu.reg_write(arm.UC_ARM_REG_SP, STACK)
        cpu.reg_write(arm.UC_ARM_REG_LR, RETURN | 1)
        for register, argument in ((arm.UC_ARM_REG_R0, address),
                                   (arm.UC_ARM_REG_R1, value & 0xffffffff),
                                   (arm.UC_ARM_REG_R2, length)):
            cpu.reg_write(register, argument)
        self.instructions = 0
        cpu.emu_start(self.address | 1, RETURN, count=100000)
        assert cpu.reg_read(arm.UC_ARM_REG_PC) == RETURN, 'memset did not return'
        assert cpu.reg_read(arm.UC_ARM_REG_SP) == STACK, 'stack imbalance'
        assert cpu.reg_read(arm.UC_ARM_REG_R0) == address, 'incorrect return pointer'
        return self.instructions

    def count_instructions(self):
        def hook(*_):
            self.instructions += 1
        self.cpu.hook_add(UC_HOOK_CODE, hook)
        # Earlier functional calls have cached translations without hooks.
        self.cpu.ctl_remove_cache(0x08000000, 0x08100000)


def verify(path):
    runtime = Runtime(path)
    lengths = list(range(81)) + [95, 96, 97, 127, 128, 129, 239, 240, 241,
                                255, 256, 257, 259, 260, 261, 512, 1620, 4096]
    values = list(range(256)) + [-1, -128, -256, 256, 0x12345678, -0x80000000]
    cases = 0
    for alignment in range(4):
        for length in lengths:
            prefix = 32 + alignment
            canary = bytes((i * 29 + 37) & 255 for i in range(prefix + length + 32))
            for value in values:
                runtime.cpu.mem_write(BUFFER, canary)
                runtime.call(BUFFER + prefix, value, length)
                expected = canary[:prefix] + bytes([value & 255]) * length + canary[prefix + length:]
                actual = bytes(runtime.cpu.mem_read(BUFFER, len(canary)))
                assert actual == expected, (alignment, length, value, 'fill or guard mismatch')
                cases += 1
    runtime.count_instructions()
    counts = {}
    for length in (0, 1, 3, 4, 32, 64, 240, 260, 1620):
        counts[str(length)] = [runtime.call(BUFFER + alignment, 0, length)
                               for alignment in range(4)]
    return {'elf_sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'cases': cases, 'alignments': [0, 1, 2, 3], 'lengths': lengths,
            'values': 'all 256 bytes plus negative and wider int truncation cases',
            'guards': '32 bytes before and after each requested fill',
            'instruction_counts_by_length_and_alignment': counts}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=Path('build/tx-port.elf'))
    parser.add_argument('--compare-elf', type=Path)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = {'units': 'executed ARM instructions, not cycles', 'current': verify(args.elf)}
    if args.compare_elf:
        result['baseline'] = verify(args.compare_elf)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    for label in ('current', 'baseline'):
        if label in result:
            print(f"{label}: {result[label]['cases']} ARM memset cases passed, including guards and return pointer")
            print(f"  aligned 240-byte fill: {result[label]['instruction_counts_by_length_and_alignment']['240'][0]} instructions")


if __name__ == '__main__':
    main()
