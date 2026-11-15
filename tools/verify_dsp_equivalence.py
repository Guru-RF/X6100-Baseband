#!/usr/bin/env python3
"""Compare the actual ARM CESSB state/output against a preserved payload ELF.

Intended for arithmetic-preserving optimization checks. The supplied baseline
must use the same 1620-byte DSP state ABI. Executes real linked DSP/libm code;
no radio, peripheral timing or firmware output. Requires the test dependencies.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

from compare_cessb import Port, Config, CONFIG, STATE, SAMPLE
from unicorn import arm_const as arm
from elftools.elf.elffile import ELFFile


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_size(path):
    with path.open('rb') as stream:
        elf = ELFFile(stream)
        return next(symbol['st_size'] for symbol in elf.get_section_by_name('.symtab').iter_symbols()
                    if symbol.name == 'dsp')


def verify(current_path, baseline_path):
    assert state_size(current_path) == state_size(baseline_path) == 1620, 'requires matching state ABI'
    current, baseline = [Port(path.read_bytes()) for path in (current_path, baseline_path)]
    generators = (current, baseline)
    random = 0x4b06a12f
    comparisons = 0
    configs = []
    for rate in (8000, 12500, 48000, 192000):
        for low in (0, 160, rate / 8):
            for high in (low + 100, (low + rate / 2 - 100) / 2, rate / 2 - 100):
                for boost in (0, 12):
                    config = Config(rate, low, high, False, True, 2, -57, 6, True, boost, 1.1220184543)
                    for machine in generators:
                        machine.cpu.mem_write(CONFIG, bytes(config))
                        machine.call(machine.symbols['tx_dsp_configure'], STATE, CONFIG)
                        assert machine.cpu.reg_read(arm.UC_ARM_REG_R0) == 1
                    assert bytes(current.cpu.mem_read(STATE, 1620)) == bytes(baseline.cpu.mem_read(STATE, 1620)), (
                        'configuration state differs', rate, low, high, boost)
                    for n in range(384):
                        values = []
                        for _ in range(2):
                            random ^= (random << 13) & 0xffffffff
                            random ^= random >> 17
                            random ^= (random << 5) & 0xffffffff
                            values.append((random & 65535) / 16384 - 2 if n % 128 >= 32 else 0)
                        sample = struct.pack('<ff', *values)
                        outputs = []
                        for machine in generators:
                            machine.cpu.mem_write(SAMPLE, sample)
                            # The sideband changes after sample191, exercising history reset.
                            machine.call(machine.symbols['tx_dsp_cessb'], STATE, n >= 192, SAMPLE, SAMPLE + 4)
                            outputs.append(bytes(machine.cpu.mem_read(SAMPLE, 8)))
                        assert outputs[0] == outputs[1], ('CESSB output differs', len(configs), n)
                        # Every ring phase appears before checking the full state, and
                        # each block also spans oscillator normalization boundaries.
                        assert bytes(current.cpu.mem_read(STATE, 1620)) == bytes(baseline.cpu.mem_read(STATE, 1620)), (
                            'CESSB state differs', len(configs), n)
                        comparisons += 1
                    configs.append(dict(rate_hz=rate, low_hz=low, high_hz=high, boost_db=boost))
    return dict(current_elf_sha256=digest(current_path), baseline_elf_sha256=digest(baseline_path),
                script_sha256=digest(Path(__file__)), state_bytes=1620,
                configuration_states_bit_identical=len(configs),
                samples_and_state_snapshots_bit_identical=comparisons,
                configurations=configs, input='xorshift32 seed0x4b06a12f, I/Q in[-2,2), periodic32-sample silences',
                sidebands='USB samples0..191 thenLSB192..383 for each configuration',
                limitations=['Finite deterministic input matrix, not proof for all possible inputs.',
                             'No hardware cycles, peripherals, RF output or installation test.'])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--elf', type=Path, default=Path('build/tx-port.elf'))
    parser.add_argument('--baseline-elf', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    result = verify(args.elf, args.baseline_elf)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + '\n')
    print(f"PASS: {result['configuration_states_bit_identical']} ARM configurations and "
          f"{result['samples_and_state_snapshots_bit_identical']} CESSB samples/full state snapshots are bit-identical")


if __name__ == '__main__':
    main()
