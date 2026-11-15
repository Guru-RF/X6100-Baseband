#!/usr/bin/env python3
"""Execute the assembled test image from its reset vector to OEM main in Unicorn.

This runs the real OEM startup, SystemInit, libc constructors, inserted branch,
wrapper and payload initializer. No function is stubbed. RCC/SCB registers are
memory-backed; writes to CPACR are mirrored into Unicorn's FPU access register.
Execution stops before OEM main configures peripherals. There are no interrupts,
bootloader execution, physical clocks, RF or cycle-accurate timing in this model.

Optionally decrypt the actual released XGF and require an exact match to the
image assembled from the separately verified stock image and payload ELF.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct

try:
    from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                         UC_HOOK_CODE, UC_HOOK_MEM_WRITE)
    from unicorn import arm_const as arm
except ImportError as error:
    raise SystemExit("Optional test dependency: pip install unicorn") from error

try:
    from . import build_test_image, plan_patch
except ImportError:
    import build_test_image
    import plan_patch

OEM_MAIN = 0x08034178
INIT_HOOK = 0x08034a4a
SYSTEM_INIT = 0x0803485c
LIBC_INIT = 0x08037ed0
OEM_DATA_START, OEM_DATA_END = 0x20000000, 0x20003570
OEM_BSS_END = 0x2000eb00
OEM_DATA_LOAD = 0x0807c5d0
STACK = 0x20030000
CCM = 0x10000000
AHB1ENR, CPACR, VTOR = 0x40023830, 0xe000ed88, 0xe000ed08
APSR_MASK = 0xf80f0000
GPRS = [getattr(arm, f"UC_ARM_REG_R{n}") for n in range(13)]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def execute_boot(image, stock, symbols, inherited_clock=0):
    """Run bytes already assembled/decrypted, not ELF-loaded function substitutes."""
    require(image[:432] == stock[:432], "OEM vectors or their alignment padding changed")
    msp, reset = struct.unpack_from('<II', image)
    require((msp, reset) == (STACK, 0x08034a19), "unexpected OEM reset vectors")
    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
    for address, size in ((0x08000000, 0x100000), (OEM_DATA_START, 0x30000),
                          (CCM, 0x10000), (0x40023000, 0x1000), (0xe000e000, 0x1000)):
        cpu.mem_map(address, size)
    cpu.mem_write(plan_patch.BASE, image)
    cpu.mem_write(OEM_DATA_START, b'\xa5' * 0x30000)
    cpu.mem_write(CCM, b'\x5a' * 0x10000)
    cpu.mem_write(AHB1ENR, struct.pack('<I', inherited_clock))
    cpu.reg_write(arm.UC_ARM_REG_SP, msp)
    cpu.reg_write(arm.UC_ARM_REG_PRIMASK, 1)  # IAP may enter with IRQs masked.
    seen, cpacr_writes = [], []
    before_patch = None
    before_patch_ram = None
    instructions = 0
    stack_low = STACK
    ccm_writes = set()
    patch_started = False
    patch_start, patch_end = symbols['__data_start'], symbols['__bss_end']
    checkpoints = {
        SYSTEM_INIT: 'oem_system_init', LIBC_INIT: 'oem_libc_init',
        INIT_HOOK: 'patched_call_site',
        symbols['v119_init_wrapper'] & ~1: 'payload_wrapper',
        symbols['patch_init'] & ~1: 'payload_initializer',
    }

    def u32(address):
        return struct.unpack('<I', cpu.mem_read(address, 4))[0]

    def registers():
        return ([cpu.reg_read(register) for register in GPRS],
                cpu.reg_read(arm.UC_ARM_REG_APSR) & APSR_MASK,
                cpu.reg_read(arm.UC_ARM_REG_FPSCR),
                cpu.reg_read(arm.UC_ARM_REG_SP),
                cpu.reg_read(arm.UC_ARM_REG_PRIMASK))

    def instruction(_cpu, address, _size, _user):
        nonlocal instructions, stack_low, before_patch, before_patch_ram, patch_started
        instructions += 1
        stack_low = min(stack_low, cpu.reg_read(arm.UC_ARM_REG_SP))
        if address in checkpoints:
            seen.append(checkpoints[address])
        if address == SYSTEM_INIT:
            # Verify the OEM's actual copy/clear loops before constructors run.
            data_size = OEM_DATA_END - OEM_DATA_START
            offset = OEM_DATA_LOAD - plan_patch.BASE
            require(bytes(cpu.mem_read(OEM_DATA_START, data_size)) == stock[offset:offset + data_size],
                    "OEM startup did not copy its exact data initializer")
            require(bytes(cpu.mem_read(OEM_DATA_END, OEM_BSS_END - OEM_DATA_END)) ==
                    bytes(OEM_BSS_END - OEM_DATA_END), "OEM BSS was not cleared")
        elif address == INIT_HOOK:
            require(u32(CPACR) & 0xf00000 == 0xf00000, "OEM did not enable the FPU")
            before_patch = registers()
            before_patch_ram = bytes(cpu.mem_read(OEM_DATA_START, OEM_BSS_END - OEM_DATA_START))
            patch_started = True

    def write(_cpu, _access, address, size, value, _user):
        if address == CPACR and size == 4:
            require(not patch_started, "payload unexpectedly writes CPACR")
            cpacr_writes.append(value)
            # Unicorn does not connect this memory-backed SCB address to the
            # execution engine's FPU gate automatically; mirror the OEM write.
            cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, value)
            return
        if not patch_started:
            require((OEM_DATA_START <= address and address + size <= OEM_BSS_END) or
                    (STACK - 0x4000 <= address and address + size <= STACK) or
                    (0x40023800 <= address and address + size <= 0x40023900) or
                    address == VTOR, f"unexpected OEM startup write at {address:#x}")
            return
        if address == AHB1ENR and size == 4:
            require(value == (u32(AHB1ENR) | (1 << 20)), "payload changed other RCC clock bits")
        elif patch_start <= address and address + size <= patch_end:
            ccm_writes.update(range(address, address + size))
        else:
            require(STACK - 0x4000 <= address and address + size <= STACK,
                    f"payload initializer wrote outside private CCM/stack at {address:#x}")

    cpu.hook_add(UC_HOOK_CODE, instruction)
    cpu.hook_add(UC_HOOK_MEM_WRITE, write)
    try:
        cpu.emu_start(reset, OEM_MAIN, count=250000)
    except UcError as error:
        raise ValueError(f"boot execution failed at {cpu.reg_read(arm.UC_ARM_REG_PC):#x}: {error}") from error
    require(cpu.reg_read(arm.UC_ARM_REG_PC) == OEM_MAIN, "boot did not reach OEM main within instruction limit")
    require(seen == ['oem_system_init', 'oem_libc_init', 'patched_call_site',
                     'payload_wrapper', 'payload_initializer'], "assembled image did not execute the complete startup hook")
    require(registers() == before_patch, "payload startup changed preserved OEM register state")
    require(cpu.reg_read(arm.UC_ARM_REG_LR) == INIT_HOOK + 5, "wrong OEM main return address")
    require(bytes(cpu.mem_read(OEM_DATA_START, OEM_BSS_END - OEM_DATA_START)) == before_patch_ram,
            "payload initialization changed OEM initialized RAM")
    require(u32(VTOR) == plan_patch.BASE, "OEM vector-table address was not retained")
    require(u32(AHB1ENR) == inherited_clock | (1 << 20), "CCM clock was not enabled correctly")
    require(ccm_writes == set(range(patch_start, patch_end)), "private initialization did not cover its reserved state")
    require(bytes(cpu.mem_read(patch_end, CCM + 0x10000 - patch_end)) ==
            b'\x5a' * (CCM + 0x10000 - patch_end), "initialization touched unreserved CCM")
    require(u32(symbols['bb_lab_arm']) == 0, "test build armed itself at boot")
    require(u32(symbols['bb_applied_configurations']) == 0 and
            u32(symbols['bb_rejected_configurations']) == 0, "unexpected boot configuration counters")
    require(bytes(cpu.mem_read(symbols['configured'], 1)) == b'\0', "boot applied a GUI configuration")
    require(bytes(cpu.mem_read(symbols['filter_changed'], 1)) == b'\0', "boot changed the OEM filter")
    return {
        'path': seen + ['oem_main_entry'], 'executed_instructions': instructions,
        'stack_low': hex(stack_low), 'startup_stack_span': STACK - stack_low,
        'inherited_ahb1enr': hex(inherited_clock), 'final_ahb1enr': hex(u32(AHB1ENR)),
        'cpacr_writes': [hex(value) for value in cpacr_writes],
        'initialized_ccm_bytes': len(ccm_writes), 'boot_armed': False,
        'oem_ram_preserved_by_payload': True, 'oem_registers_preserved_by_payload': True,
    }


def verify(stock, elf, image=None):
    expected, manifest = build_test_image.build_plaintext(stock, elf)
    if image is None:
        image = expected
    require(image == expected, "decrypted XGF differs from the independently assembled stock/ELF image")
    _, symbols = plan_patch.read_elf(elf)
    cases = [execute_boot(image, stock, symbols, inherited) for inherited in (0, 0x00400001)]
    # The identical OEM image with the original call restored must not pass:
    # merely reaching main is insufficient evidence of executing our payload.
    omitted = bytearray(image)
    offset = INIT_HOOK - plan_patch.BASE
    omitted[offset:offset + 4] = stock[offset:offset + 4]
    try:
        execute_boot(bytes(omitted), stock, symbols)
    except ValueError as error:
        require('complete startup hook' in str(error), f"wrong negative-control failure: {error}")
    else:
        raise ValueError('negative control failed to detect an omitted startup hook')
    return {
        'scope': 'assembled image reset-to-main emulation; not target hardware validation',
        'plaintext_sha256': hashlib.sha256(image).hexdigest(),
        'payload_elf_sha256': manifest['payload_elf_sha256'],
        'vector_bytes_preserved': 432, 'cases': cases,
        'omitted_startup_hook_negative_control': 'rejected',
        'environment': {'function_stubs': [], 'interrupts': 'not delivered',
                        'peripheral_model': 'RCC/SCB memory only; actual OEM CPACR writes mirrored to FPU gate',
                        'stop': 'OEM main entry, before peripheral setup',
                        'timing': 'instruction count only, not CPU cycles'},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stock-bin', type=Path, required=True)
    parser.add_argument('--payload-elf', type=Path, default=Path('build/tx-test.elf'))
    parser.add_argument('--image-xgf', type=Path)
    parser.add_argument('--key-file', type=Path, help='external key file; required only with --image-xgf')
    args = parser.parse_args()
    if bool(args.image_xgf) != bool(args.key_file):
        parser.error('--image-xgf and --key-file must be supplied together')
    try:
        image = None
        if args.image_xgf:
            image = build_test_image.aes_ecb(args.image_xgf.read_bytes(),
                                           build_test_image.read_key(args.key_file), decrypt=True)
        result = verify(args.stock_bin.read_bytes(), args.payload_elf.read_bytes(), image)
        result['verified_input'] = 'decrypted XGF' if args.image_xgf else 'assembled plaintext'
    except (OSError, ValueError, struct.error) as error:
        parser.exit(1, f'Refused: {error}\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
