#!/usr/bin/env python3
"""Exercise the actual ARM assembly wrappers with synthetic callees.

Requires arm-none-eabi binutils, unicorn, and pyelftools. Builds a temporary
ELF from firmware/hooks.S and tests/hooks_stubs.S; no vendor firmware, hardware,
or patched image is involved. Synthetic callees overwrite every call-clobbered
GPR/VFP register and status flags, respecting AAPCS32's callee-saved registers.
All GPR/VFP registers are still seeded and checked at OEM entry/continuation.
Unlike the original stronger-than-ABI harness, this explicitly relies on the
callback preserving r4-r11 and s16-s31. Deliberate ABI violations are detected.
With --payload-elf, also test the real compiled callbacks' callee-save contract;
--oem-bin executes the actual stock filter constructor in that callback audit.
This verifies calling mechanics, not timing, IRQ nesting or memory ownership.

  python tools/verify_hooks.py --cross /path/to/arm-none-eabi-
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import tempfile

try:
    from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                         UC_HOOK_CODE, UC_HOOK_MEM_READ, UC_HOOK_MEM_WRITE)
    from unicorn import arm_const as arm
    from elftools.elf.elffile import ELFFile
except ImportError as error:
    raise SystemExit("Optional test dependencies: pip install unicorn pyelftools") from error

ROOT = Path(__file__).resolve().parents[1]
GPRS = [getattr(arm, f"UC_ARM_REG_R{i}") for i in range(13)]
FPRS = [getattr(arm, f"UC_ARM_REG_S{i}") for i in range(32)]
CALLER_GPRS = [GPRS[n] for n in (0, 1, 2, 3, 12)]
CALLEE_GPRS = GPRS[4:12]
CALLEE_FPRS = FPRS[16:]
APSR_MASK = 0xF80F0000
CASES = [
    ("audio", "bb_tx_audio", 0x08036000, 0x080247CE),
    ("iq", "bb_tx_iq", 0x08035364, 0x080267D2),
    ("iq_second", None, 0x08035364, 0x0802686A),
    ("begin_block", "bb_begin_block", 0x0802E1D4, 0x0802459E),
    ("init", "patch_init", 0x08034178, 0x08034A4E),
]


def build(cross, directory, hooks=None):
    objects = []
    for source in (hooks or ROOT / "firmware/hooks.S", ROOT / "tests/hooks_stubs.S"):
        obj = directory / (source.stem + ".o")
        subprocess.run([cross + "as", "-mcpu=cortex-m4", "-mfpu=fpv4-sp-d16",
                        str(source), "-o", str(obj)], check=True)
        objects.append(str(obj))
    output = directory / "wrappers.elf"
    subprocess.run([cross + "ld", "-Ttext=0x08080000", "-e", "v119_init_wrapper",
                    *objects, "-o", str(output)], check=True)
    with output.open("rb") as stream:
        elf = ELFFile(stream)
        section = elf.get_section_by_name(".text")
        symbols = {symbol.name: symbol["st_value"]
                   for symbol in elf.get_section_by_name(".symtab").iter_symbols()}
        return section["sh_addr"], section.data(), symbols


def verify(image, case, redirect=False, violate=None, quiet=False):
    start, blob, symbols = image
    name, callback, original, continuation = case
    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
    cpu.mem_map(0x08000000, 0x100000)
    cpu.mem_map(0x20000000, 0x40000)
    cpu.mem_map(0xE000E000, 0x1000)
    cpu.mem_write(start, blob)
    cpu.mem_write(0xE000ED88, struct.pack("<I", 0xF00000))
    cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xF00000)
    # Activate the FPU before assigning synthetic FPSCR state. Otherwise the
    # first VFP instruction initializes a lazy context from reset FPDSCR,
    # replacing that artificial value. The real DSP handler already uses VFP.
    cpu.mem_write(0x08000100, bytes.fromhex("b0ee400a"))  # vmov.f32 s0,s0
    cpu.emu_start(0x08000101, 0x08000104)
    for i, register in enumerate(GPRS):
        cpu.reg_write(register, 0x100 + i)
    for i, register in enumerate(FPRS):
        cpu.reg_write(register, 0x3F000000 + i)
    stack = 0x20028000
    cpu.reg_write(arm.UC_ARM_REG_SP, stack)
    cpu.reg_write(arm.UC_ARM_REG_LR, 0x08022223)
    cpu.reg_write(arm.UC_ARM_REG_APSR, 0xA80B0000)
    cpu.reg_write(arm.UC_ARM_REG_FPSCR, 0x01000000)
    cpu.reg_write(arm.UC_ARM_REG_R1, stack + 0x48)
    cpu.reg_write(arm.UC_ARM_REG_R2, stack + 0x4C)
    if name in ("iq", "iq_second"):
        cpu.reg_write(arm.UC_ARM_REG_R0, 0x20007490 if name == "iq" else 0x200075DC)
        cpu.reg_write(arm.UC_ARM_REG_R1, stack + (0x50 if name == "iq" else 0x54))
        cpu.reg_write(arm.UC_ARM_REG_R2, 0x200075BC if name == "iq" else 0x20007708)
        cpu.reg_write(arm.UC_ARM_REG_R3, 1)
    elif name == "audio":
        cpu.reg_write(arm.UC_ARM_REG_R0, 0x20007728)
        cpu.reg_write(arm.UC_ARM_REG_R3, 1)

    def snapshot():
        return ([cpu.reg_read(r) for r in GPRS],
                [cpu.reg_read(r) for r in FPRS],
                cpu.reg_read(arm.UC_ARM_REG_APSR) & APSR_MASK,
                cpu.reg_read(arm.UC_ARM_REG_FPSCR),
                cpu.reg_read(arm.UC_ARM_REG_SP))

    def clobber(seed, all_registers=False):
        for i, register in enumerate(GPRS if all_registers else CALLER_GPRS):
            cpu.reg_write(register, seed + i)
        for i, register in enumerate(FPRS if all_registers else FPRS[:16]):
            cpu.reg_write(register, seed + 0x3F800000 + i)
        cpu.reg_write(arm.UC_ARM_REG_APSR, 0x50050000)
        cpu.reg_write(arm.UC_ARM_REG_FPSCR, 0x00C00000)
        cpu.reg_write(arm.UC_ARM_REG_LR, 0x08000B01)
        if violate is not None and not all_registers:
            cpu.reg_write(violate, 0xDEADBEEF)

    initial = snapshot()
    expected = initial
    seen = []
    completed = False
    frame_bytes = []
    stack_reads = stack_writes = 0

    def read_stack(_cpu, _access, address, size, _value, _user):
        nonlocal stack_reads
        if stack - 256 <= address < stack:
            stack_reads += size

    def write_stack(_cpu, _access, address, size, _value, _user):
        nonlocal stack_writes
        if stack - 256 <= address < stack:
            stack_writes += size

    def on_instruction(_cpu, pc, _size, _data):
        nonlocal expected, completed
        if pc == (symbols["bb_tx_interpolator"] & ~1):
            seen.append("select")
            assert name in ("iq", "iq_second")
            assert cpu.reg_read(arm.UC_ARM_REG_SP) % 8 == 0
            frame_bytes.append(stack - cpu.reg_read(arm.UC_ARM_REG_SP))
            assert cpu.reg_read(arm.UC_ARM_REG_R0) == initial[0][0]
            link = cpu.reg_read(arm.UC_ARM_REG_LR)
            selected = 0x0808F000 if redirect else initial[0][0]
            clobber(0xB00)
            cpu.reg_write(arm.UC_ARM_REG_R0, selected)
            expected = ([selected, *initial[0][1:]], *initial[1:])
            cpu.reg_write(arm.UC_ARM_REG_PC, link)
        elif callback is not None and pc == (symbols[callback] & ~1):
            seen.append("new")
            assert cpu.reg_read(arm.UC_ARM_REG_SP) % 8 == 0
            frame_bytes.append(stack - cpu.reg_read(arm.UC_ARM_REG_SP))
            if name == "audio":
                assert cpu.reg_read(arm.UC_ARM_REG_R0) == stack + 0x48
            if name == "iq":
                assert cpu.reg_read(arm.UC_ARM_REG_R0) == stack + 0x54
                assert cpu.reg_read(arm.UC_ARM_REG_R1) == stack + 0x50
            link = cpu.reg_read(arm.UC_ARM_REG_LR)
            clobber(0x900)
            cpu.reg_write(arm.UC_ARM_REG_PC, link)
        elif pc == original:
            seen.append("oem")
            assert cpu.reg_read(arm.UC_ARM_REG_SP) % 8 == 0
            if name in ("audio", "iq", "iq_second", "init"):
                assert snapshot() == expected, (name, "state before original differs")
                assert cpu.reg_read(arm.UC_ARM_REG_LR) == continuation + 1
            if name == "init":
                completed = True
                cpu.emu_stop()
                return
            if name == "begin_block":
                link = cpu.reg_read(arm.UC_ARM_REG_LR)
                # Model arbitrary effects of the ORIGINAL OEM call; the added
                # callback must preserve its resulting state, not initial state.
                clobber(0x400, all_registers=True)
                if name == "begin_block":
                    cpu.reg_write(arm.UC_ARM_REG_R0, 1)
                expected = (*snapshot()[:-1], stack)
                cpu.reg_write(arm.UC_ARM_REG_PC, link)
            else:
                cpu.reg_write(arm.UC_ARM_REG_PC, cpu.reg_read(arm.UC_ARM_REG_LR))
        elif pc == continuation:
            assert snapshot() == expected, (name, "state at continuation differs")
            assert cpu.reg_read(arm.UC_ARM_REG_LR) == continuation + 1
            completed = True
            cpu.emu_stop()

    cpu.hook_add(UC_HOOK_CODE, on_instruction)
    cpu.hook_add(UC_HOOK_MEM_READ, read_stack)
    cpu.hook_add(UC_HOOK_MEM_WRITE, write_stack)
    symbol = "v119_" + ("tx_" if name in ("audio", "iq", "iq_second") else "") + name + "_wrapper"
    try:
        cpu.emu_start(symbols[symbol], 0, count=500)
    except UcError as error:
        raise RuntimeError(name, hex(cpu.reg_read(arm.UC_ARM_REG_PC)), error) from error
    assert completed, (name, "did not reach OEM continuation")
    order = {"begin_block": ["oem", "new"], "iq": ["new", "select", "oem"],
             "iq_second": ["select", "oem"]}.get(name, ["new", "oem"])
    assert seen == order
    assert len(set(frame_bytes)) == 1 and frame_bytes[0] in (96, 192), frame_bytes
    extra_read = 4 if name in ("audio", "iq", "iq_second") else 0
    extra_write = 4 if name in ("iq", "iq_second") else 0
    assert stack_reads == frame_bytes[0] + extra_read, (name, stack_reads)
    assert stack_writes == frame_bytes[0] + extra_write, (name, stack_writes)
    if not quiet:
        print(f"{name} redirect={redirect}: PASS AAPCS preservation, arguments, order, alignment, LR; frame {frame_bytes[0]} bytes")
    return dict(wrapper=name, redirect=redirect, frame_bytes=frame_bytes[0],
                stack_read_bytes=stack_reads, stack_write_bytes=stack_writes)


def verify_callback_abi(elf, oem=None):
    """Seed all callee-saved registers around actual compiled callback paths.

    Includes initialization/libm, changing passbands, compressor and both
    sidebands. Memory/state behavior is covered by the dedicated adapter and
    interpolation tests; this independently audits the wrapper's ABI reliance.
    """
    from verify_adapter import Machine, COOKIE, RATE, SAMPLES, float_bits
    from plan_patch import read_elf
    # Enforce the same linked ARM hard-float contract as the patch planner.
    read_elf(elf.read_bytes())
    if oem is None:
        machine = Machine(elf)
    else:
        from analyze_tx_chain import ActualFilterMachine
        from plan_patch import validate_oem
        data = oem.read_bytes()
        validate_oem(data)
        machine = ActualFilterMachine(elf, data)
    saved = [*CALLEE_GPRS, *CALLEE_FPRS]
    calls = 0

    def call(name, *args):
        nonlocal calls
        for n, register in enumerate(saved):
            machine.cpu.reg_write(register, 0x3F000000 + n * 0x10203 + calls)
        expected = [machine.cpu.reg_read(register) for register in saved]
        machine.call(name, *args)
        actual = [machine.cpu.reg_read(register) for register in saved]
        assert actual == expected, (name, "callee-saved register changed", expected, actual)
        calls += 1

    call("patch_init")
    machine.put32(RATE, float_bits(12500.0))
    machine.put32("bb_lab_arm", COOKIE)
    for low, high in ((160, 3000), (300, 2400), (300, 3000)):
        machine.flags(tx=False)
        machine.registers(compressor=3, cessb=256 | (37 << 9), low=low, high=high)
        for _ in range(3):
            call("bb_begin_block")
        for mode in (0, 2):
            machine.flags(mode, True)
            call("bb_begin_block")
            for n in range(65):
                machine.cpu.mem_write(SAMPLES, struct.pack("<ff", 0.7 if n % 2 else 0.01, -0.6))
                call("bb_tx_audio", SAMPLES)
                call("bb_tx_iq", SAMPLES, SAMPLES + 4)
                call("bb_tx_interpolator", 0x20007490)
                call("bb_tx_interpolator", 0x200075DC)
    machine.flags(tx=False)
    machine.put32("bb_lab_arm", 0)
    call("bb_begin_block")
    print(f"Actual compiled callback ABI: PASS {calls} calls, r4-r11 (including r9) and s16-s31 unchanged")
    return dict(calls=calls, callee_saved_registers="r4-r11 including r9; s16-s31",
                oem_filter="actual V1.1.9 constructor" if oem else "stubbed",
                payload_elf_sha256=hashlib.sha256(elf.read_bytes()).hexdigest())


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cross", default=os.environ.get("CROSS", "arm-none-eabi-"))
    parser.add_argument("--payload-elf", type=Path)
    parser.add_argument("--oem-bin", type=Path)
    parser.add_argument("--baseline-hooks", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.oem_bin and not args.payload_elf:
        parser.error("--oem-bin requires --payload-elf")
    result = dict(abi_reference="https://github.com/ARM-software/abi-aa/blob/main/aapcs32/aapcs32.rst",
                  source_sha256=hashlib.sha256((ROOT / "firmware/hooks.S").read_bytes()).hexdigest(),
                  limitations=["Counts are transferred bytes, not hardware cycles.",
                               "Callbacks must obey AAPCS32; no naked or nonconforming callees.",
                               "Nested hardware interrupts and target stack high-water are not emulated."])
    with tempfile.TemporaryDirectory(prefix="x6100-hooks-") as directory:
        image = build(args.cross, Path(directory))
        result["current"] = []
        for case in CASES:
            result["current"].append(verify(image, case))
            if case[0] in ("iq", "iq_second"):
                result["current"].append(verify(image, case, redirect=True))
        # The harness must catch violations of either callee-saved bank. The
        # old all-register-save wrapper happened to mask such ABI violations;
        # it is not a requirement of normal C callees and is not claimed here.
        for register in (arm.UC_ARM_REG_R4, arm.UC_ARM_REG_R9, arm.UC_ARM_REG_S16, arm.UC_ARM_REG_S31):
            try:
                verify(image, CASES[0], violate=register, quiet=True)
            except AssertionError as error:
                assert "state before original differs" in str(error), error
            else:
                raise AssertionError("ABI-violating callee was not detected")
        result["deliberate_abi_violation_detection"] = ["r4", "r9", "s16", "s31"]
        print("Negative controls: PASS detected deliberately non-AAPCS r4/r9/s16/s31 clobbers")
        if args.baseline_hooks:
            baseline = build(args.cross, Path(directory), args.baseline_hooks)
            result["baseline"] = [verify(baseline, case, quiet=True) for case in CASES]
            result["baseline_source_sha256"] = hashlib.sha256(args.baseline_hooks.read_bytes()).hexdigest()
    if args.payload_elf:
        result["actual_callback_abi"] = verify_callback_abi(args.payload_elf, args.oem_bin)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")


if __name__ == "__main__":
    main()
