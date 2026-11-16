#!/usr/bin/env python3
"""Audit the exact stock V1.1.9 flow producer in a bounded ARM emulator.

Uses a caller-supplied plaintext image; never contacts a radio or creates an
image. The actual packet producer, CRC reset/feed helpers and HKEY setter
execute. IQ copying and DMA start are stubbed; CRC MMIO is modeled. This verifies
addresses, lengths and writes, not IRQ races, live DMA ownership or timing.

Independent CRC vectors were obtained by compiling OpenX6100/X6100Control
7a5bb8e5367f59003ef03af221361e186da10bf0 src/low/crc32.c. That commit's
include/aether_radio/x6100_control/low/flow.h independently fixes the ABI.
No upstream implementation or vendor image is included here.

    python tools/verify_oem_flow.py --oem-bin /path/to/V1.1.9.bin
"""
import argparse
import hashlib
import json
from pathlib import Path
import stat
import struct

OEM_SHA256 = '3fa11293e06951c21c9cfda04ea08adad85eaadda26b77a8e8bbcca8820c53c4'
OEM_SIZE = 392048
FLASH = 0x08020000
PACKET = 0x200003F8
PACKET_SIZE = 0x1028
CRC_OFFSET = 0x1024
PRODUCER = 0x080337FC
IQ_COPY = 0x08023C88
CRC_RESET = 0x0802E0BC
CRC_FEED = 0x0802E0CC
DMA_START = 0x08032FC8
HKEY_SETTER = 0x08033968
STACK, RETURN = 0x20028000, 0x08000100
INSTRUCTION_LIMIT = 20000


def crc_words(words):
    """STM32 fixed CRC: native LE words, MSB first, no final XOR."""
    result = 0xFFFFFFFF
    for word in words:
        result ^= word
        for _ in range(32):
            carry = result & 0x80000000
            result = (result << 1) & 0xFFFFFFFF
            if carry:
                result ^= 0x04C11DB7
    return result


def load_oem(path):
    path = Path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError('stock image must be a regular file')
    if info.st_size != OEM_SIZE:
        raise ValueError(f'wrong stock image size: expected {OEM_SIZE} bytes')
    with path.open('rb') as source:
        image = source.read(OEM_SIZE + 1)
    if len(image) != OEM_SIZE or hashlib.sha256(image).hexdigest() != OEM_SHA256:
        raise ValueError('stock image SHA-256 does not match exact V1.1.9')
    return image


def audit_case(image, seed):
    # Import only after the image guard, so ordinary tests need no emulator.
    try:
        from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                             UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE,
                             UC_MEM_READ, UC_MEM_WRITE)
        from unicorn import arm_const as arm
    except ImportError as error:
        raise RuntimeError('optional dependency missing: pip install unicorn') from error

    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
    for address, size in ((0x08000000, 0x100000), (0x20000000, 0x40000),
                          (0x40000000, 0x30000)):
        cpu.mem_map(address, size)
    cpu.mem_write(FLASH, image)
    initial = bytearray((index * 37 + seed) & 255 for index in range(PACKET_SIZE))
    struct.pack_into('<I', initial, 0, 0xAA5555AA)
    cpu.mem_write(PACKET - 16, b'\x5a' * 16 + initial)
    telemetry = bytes((index + seed) & 255 for index in range(16))
    cpu.mem_write(0x20001420, telemetry)
    crc_input, packet_writes, calls = [], [], []
    instructions = 0
    crc_resets = 0

    def code(uc, address, _size, _user):
        nonlocal instructions
        instructions += 1
        if address in (IQ_COPY, CRC_RESET, CRC_FEED, DMA_START):
            calls.append((address, uc.reg_read(arm.UC_ARM_REG_R0),
                          uc.reg_read(arm.UC_ARM_REG_R1)))
        if address in (IQ_COPY, DMA_START):
            uc.reg_write(arm.UC_ARM_REG_PC, uc.reg_read(arm.UC_ARM_REG_LR))

    def memory(uc, access, address, size, value, _user):
        nonlocal crc_resets
        if access == UC_MEM_WRITE:
            if address == 0x40023008:
                assert size == 4 and value == 1, 'unexpected CRC reset'
                crc_resets += 1
                crc_input.clear()
            elif address == 0x40023000:
                assert size == 4, 'CRC feed is not a 32-bit word'
                crc_input.append(value)
            elif PACKET <= address < PACKET + PACKET_SIZE:
                packet_writes.append((address - PACKET, size))
        elif access == UC_MEM_READ and address == 0x40023000:
            assert size == 4
            uc.mem_write(address, struct.pack('<I', crc_words(crc_input)))

    cpu.hook_add(UC_HOOK_CODE, code)
    cpu.hook_add(UC_HOOK_MEM_READ | UC_HOOK_MEM_WRITE, memory)

    def execute(address, argument=0):
        cpu.reg_write(arm.UC_ARM_REG_SP, STACK)
        cpu.reg_write(arm.UC_ARM_REG_LR, RETURN | 1)
        cpu.reg_write(arm.UC_ARM_REG_R0, argument)
        cpu.emu_start(address | 1, RETURN, count=INSTRUCTION_LIMIT)
        assert cpu.reg_read(arm.UC_ARM_REG_PC) == RETURN, 'instruction budget exhausted'
        assert cpu.reg_read(arm.UC_ARM_REG_SP) == STACK, 'unbalanced OEM stack'

    execute(PRODUCER)
    producer_instructions = instructions
    packet = bytes(cpu.mem_read(PACKET, PACKET_SIZE))
    expected_writes = [(0x1004, 4), (0x1008, 4), (0x100C, 4),
                       (0x1010, 4), (CRC_OFFSET, 4)]
    assert packet_writes == expected_writes, packet_writes
    assert bytes(cpu.mem_read(PACKET - 16, 16)) == b'\x5a' * 16
    assert packet[:0x1004] == initial[:0x1004], 'stubbed IQ/header changed'
    assert packet[0x1004:0x1008] == telemetry[:4], 'flag layout changed'
    assert packet[0x1008] == initial[0x1008], 'reserved byte changed'
    assert packet[0x1009:0x1010] == telemetry[5:12], 'common telemetry layout changed'
    assert packet[0x1010:0x1013] == bytes((telemetry[14], telemetry[15], telemetry[13]))
    assert packet[0x1013:0x1024] == initial[0x1013:0x1024], 'reserved/HKEY bytes changed'
    assert crc_resets == 1
    assert crc_input == list(struct.unpack('<1033I', packet[:CRC_OFFSET]))
    assert struct.unpack_from('<I', packet, CRC_OFFSET)[0] == crc_words(crc_input)
    assert [(a, r0) for a, r0, _ in calls if a == IQ_COPY] == [(IQ_COPY, PACKET + 4)]
    assert [(r0, r1) for a, r0, r1 in calls if a == CRC_FEED] == [(PACKET, 1033)]
    assert [(r0, r1) for a, r0, r1 in calls if a == DMA_START] == [(PACKET, PACKET_SIZE)]
    # HKEY is written directly after the producer returns, without refreshing
    # the trailer. This demonstrates a potential raw-capture CRC mismatch;
    # it does not emulate a concurrent UART transfer or prove its incidence.
    packet_writes.clear()
    hkey = struct.unpack_from('<I', packet, 0x1020)[0] ^ 0x11223344
    execute(HKEY_SETTER, hkey)
    updated = bytes(cpu.mem_read(PACKET, PACKET_SIZE))
    assert packet_writes == [(0x1020, 4)]
    assert struct.unpack_from('<I', updated, 0x1020)[0] == hkey
    assert updated[:0x1020] == packet[:0x1020] and updated[CRC_OFFSET:] == packet[CRC_OFFSET:]
    assert crc_words(struct.unpack('<1033I', updated[:CRC_OFFSET])) != struct.unpack_from('<I', updated, CRC_OFFSET)[0]
    return {'seed': seed, 'producer_instructions': producer_instructions,
            'producer_write_offsets': [hex(offset) for offset, _ in expected_writes],
            'write_sizes_bytes': [size for _, size in expected_writes],
            'crc_feed_words': len(crc_input), 'dma_requested_bytes': PACKET_SIZE,
            'extended_reserved_0x1014_to_0x101f_unchanged': True,
            'hkey_setter_updates_word_without_crc': True}


def verify(path):
    if not __debug__:
        raise RuntimeError('verification requires assertions: do not use python -O')
    image = load_oem(path)
    return {'stock_sha256': OEM_SHA256,
            'tool_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'addresses': {name: hex(value) for name, value in (
                ('packet', PACKET), ('producer', PRODUCER), ('crc_reset', CRC_RESET),
                ('crc_feed', CRC_FEED), ('dma_start', DMA_START), ('hkey_setter', HKEY_SETTER))},
            'instruction_budget_per_call': INSTRUCTION_LIMIT,
            'cases': [audit_case(image, seed) for seed in (0, 11, 239)],
            'limits': ['IQ copy is stubbed; samples are sentinel bytes, not measured IQ.',
                       'DMA start is stubbed; its source/count arguments are checked, not peripheral setup.',
                       'CRC peripheral is modeled; actual reset/feed code executes.',
                       'No radio, UART, I2C, live DMA, interrupt concurrency or cycle timing is modeled.',
                       'Reserved bytes are shown unchanged by this producer; this is not a whole-program write audit.',
                       'HKEY mutation shows a possible raw CRC mismatch, not its occurrence rate.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--oem-bin', '--stock-bin', dest='oem_bin', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        report = verify(args.oem_bin)
    except (OSError, ValueError, RuntimeError) as error:
        parser.exit(2, f'error: {error}\n')
    rendered = json.dumps(report, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered)
    print(rendered, end='')


if __name__ == '__main__':
    main()
