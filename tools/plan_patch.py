#!/usr/bin/env python3
"""Validate the exact OEM image and linked ELF; output a review plan, NEVER firmware.

Input is a lawfully obtained plaintext OEM .bin. No encryption key is stored or
needed here. The JSON records unresolved deployment gates even if checks pass.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

BASE = 0x08020000
OEM_SIZE = 392048
OEM_SHA256 = '3fa11293e06951c21c9cfda04ea08adad85eaadda26b77a8e8bbcca8820c53c4'
FLASH_START, FLASH_END = 0x08080000, 0x080a0000
CCM_START, CCM_END = 0x10000000, 0x10004000
HOOKS = (
    ('v119_init_wrapper', 0x08034a4a, 'fff795fb'),
    ('v119_begin_block_wrapper', 0x0802459a, '09f01bfe'),
    ('v119_tx_audio_wrapper', 0x080247ca, '11f019fc'),
    ('v119_tx_iq_wrapper', 0x080267ce, '0ef0c9fd'),
    ('v119_tx_iq_second_wrapper', 0x08026866, '0ef07dfd'),
)
BLOCKERS = [
    'Private TX interpolation matches r13 offline; target transition/spectrum and residual overshoot validation pending',
    'Whole-program CCM ownership and nested interrupt stack high-water not measured on target',
    'Sample scaling/sign compared offline; absolute RF scaling and output spectrum not measured on target',
    'No downstream OEM compressor consumer found statically; on-radio interaction remains unmeasured',
    'Worst-case ISR cycles (including configuration updates) not measured',
    'I2C multi-register commit/ack transaction and whole-firmware collision audit missing',
    'Companion OpenX6X00 tx1a2-test capability detection/control needs on-radio validation',
    'IAP erase policy and rollback not validated for the extended image',
    'RF spectrum, filter response, ALC and first-TX-after-band-change tests not performed',
]


def thumb_bw(source, target):
    """ARMv7-M unconditional B.W T4, relative to instruction address+4."""
    if source & 1 or target & 1:
        raise ValueError('branch addresses must be halfword-aligned')
    delta = target - (source + 4)
    if not -(1 << 24) <= delta < (1 << 24):
        raise ValueError('B.W target is out of range')
    bits = delta & ((1 << 25) - 1)
    s, i1, i2 = (bits >> 24) & 1, (bits >> 23) & 1, (bits >> 22) & 1
    j1, j2 = 1 ^ i1 ^ s, 1 ^ i2 ^ s
    return struct.pack('<HH', 0xf000 | s << 10 | ((bits >> 12) & 0x3ff),
                       0x9000 | j1 << 13 | j2 << 11 | ((bits >> 1) & 0x7ff))


def check_hooks(image):
    for name, address, expected in HOOKS:
        start = address - BASE
        if image[start:start + 4] != bytes.fromhex(expected):
            raise ValueError(f'{name}: original instruction mismatch at {address:#x}')


def validate_oem(image):
    if len(image) != OEM_SIZE or hashlib.sha256(image).hexdigest() != OEM_SHA256:
        raise ValueError('unsupported OEM image: exact plaintext V1.1.9 251127001 required')
    check_hooks(image)


def read_elf(blob):
    if len(blob) < 52 or blob[:7] != b'\x7fELF\x01\x01\x01':
        raise ValueError('expected ELF32 little-endian')
    h = struct.unpack_from('<HHIIIIIHHHHHH', blob, 16)
    kind, machine, version, entry, phoff, shoff, flags, ehsize, phsize, phnum, shsize, shnum, _ = h
    if kind != 2 or machine != 40 or flags & 0x600 != 0x400:
        raise ValueError('expected linked ARM hard-float executable')
    if version != 1 or ehsize != 52 or phsize != 32 or shsize != 40:
        raise ValueError('unexpected ELF header layout')
    if not phnum or not shnum or phoff < ehsize or shoff < ehsize:
        raise ValueError('missing or overlapping ELF header tables')
    if phoff + phsize * phnum > len(blob) or shoff + shsize * shnum > len(blob):
        raise ValueError('truncated ELF header tables')
    segments = []
    for index in range(phnum):
        typ, off, vaddr, paddr, filesz, memsz, segment_flags, _ = struct.unpack_from('<8I', blob, phoff + index * phsize)
        if typ != 1:
            continue
        if filesz > memsz or off + filesz > len(blob):
            raise ValueError('invalid load segment')
        in_flash = FLASH_START <= vaddr and vaddr + memsz <= FLASH_END
        in_ccm = CCM_START <= vaddr and vaddr + memsz <= CCM_END
        if not (in_flash or in_ccm):
            raise ValueError('load segment escapes reserved FLASH/CCM budgets')
        if in_flash and vaddr != paddr:
            raise ValueError('flash segment execution address differs from its load address')
        if filesz and not FLASH_START <= paddr < paddr + filesz <= FLASH_END:
            raise ValueError('file-backed load segment is outside payload flash')
        if filesz:
            segments.append({'address': paddr, 'virtual_address': vaddr,
                             'size': filesz, 'flags': segment_flags,
                             'sha256': hashlib.sha256(blob[off:off + filesz]).hexdigest()})
    if not segments:
        raise ValueError('empty payload')
    segments.sort(key=lambda s: s['address'])
    for prev, cur in zip(segments, segments[1:]):
        if prev['address'] + prev['size'] > cur['address']:
            raise ValueError('overlapping payload segments')
    sections = [struct.unpack_from('<10I', blob, shoff + i * shsize) for i in range(shnum)]
    for _, typ, _, _, off, size, _, _, _, _ in sections:
        if typ == 3:
            if not size or off + size > len(blob):
                raise ValueError('invalid string table bounds')
            if blob[off] != 0 or blob[off + size - 1] != 0:
                raise ValueError('string table is not NUL-terminated')
    symbols = {}
    for _, typ, _, _, off, size, link, _, _, entsize in sections:
        if typ in (4, 9) and size:
            raise ValueError('unresolved relocations in payload')
        if typ != 2:
            continue
        if entsize != 16 or size % 16 or link >= len(sections) or off + size > len(blob):
            raise ValueError('invalid symbol table')
        strings = sections[link]
        if strings[1] != 3:
            raise ValueError('symbol table does not reference a string table')
        names = blob[strings[4]:strings[4] + strings[5]]
        for pos in range(off, off + size, 16):
            name, value, _, _, _, section = struct.unpack_from('<IIIBBH', blob, pos)
            if name >= len(names):
                raise ValueError('invalid symbol name')
            end = names.find(b'\0', name)
            if end < 0:
                raise ValueError('unterminated symbol name')
            name = names[name:end].decode('ascii')
            if shnum <= section < 0xff00:
                raise ValueError('invalid symbol section index')
            if name and section == 0:
                raise ValueError(f'undefined symbol: {name}')
            if name:
                symbols[name] = value
    if (symbols.get('v119_init_wrapper', 0) | 1) != entry:
        raise ValueError('wrong payload entry point')
    return segments, symbols


def plan(image, elf):
    validate_oem(image)
    segments, symbols = read_elf(elf)
    writes = []
    for name, address, expected in HOOKS:
        if name not in symbols or not symbols[name] & 1:
            raise ValueError(f'missing Thumb hook symbol: {name}')
        target = symbols[name] & ~1
        if not any(s['flags'] & 1 and s['virtual_address'] == s['address'] and
                   s['address'] <= target and target + 2 <= s['address'] + s['size']
                   for s in segments):
            raise ValueError(f'hook outside executable loaded payload: {name}')
        writes.append({'name': name, 'address': hex(address), 'expected': expected,
                       'replacement': thumb_bw(address, target).hex(), 'target': hex(target)})
    return {'status': 'analysis-only; NOT approved for installation', 'image_generated': False,
            'oem_sha256': OEM_SHA256, 'elf_sha256': hashlib.sha256(elf).hexdigest(),
            'hooks': writes, 'payload_segments': segments, 'deployment_blockers': BLOCKERS}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('oem_bin', type=Path)
    parser.add_argument('payload_elf', type=Path)
    args = parser.parse_args()
    try:
        result = plan(args.oem_bin.read_bytes(), args.payload_elf.read_bytes())
    except (OSError, ValueError, struct.error) as error:
        parser.exit(1, f'Refused: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
