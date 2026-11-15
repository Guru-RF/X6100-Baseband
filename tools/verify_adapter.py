#!/usr/bin/env python3
"""Exercise the actual ARM payload adapter in a synthetic Cortex-M4 machine.

Requires unicorn and pyelftools; first build `make payload`. This executes ELF
patch_init, configuration and sample callbacks including their real DSP/libm
code. The OEM filter designer is stubbed and records its hard-float ABI args.
No vendor image or radio is loaded. This does NOT validate OEM addresses,
peripheral timing, IRQ deadlines, memory ownership or RF quality.

    python tools/verify_adapter.py [--elf build/tx-port.elf]
"""
import argparse
from pathlib import Path
import struct

try:
    from unicorn import (Uc, UcError, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                         UC_HOOK_CODE, UC_HOOK_MEM_WRITE)
    from unicorn import arm_const as arm
    from elftools.elf.elffile import ELFFile
except ImportError as error:
    raise SystemExit("Optional test dependencies: pip install unicorn pyelftools") from error

ROOT = Path(__file__).resolve().parents[1]
MODE, TX, RATE, REGS = 0x200085AD, 0x200085AF, 0x2000BA44, 0x2000C4C8
FILTER_SETUP, FILTER_INSTANCE = 0x0802397C, 0x20007728
STACK, SAMPLES, RETURN = 0x20028000, 0x20010000, 0x08000100
COOKIE = 0x54584C42
TEST_TOKEN = 0x54580000


def float_bits(value):
    return struct.unpack("<I", struct.pack("<f", value))[0]


def bits_float(value):
    return struct.unpack("<f", struct.pack("<I", value))[0]


class Machine:
    def __init__(self, path):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
        for address, size in ((0x08000000, 0x100000), (0x10000000, 0x10000),
                              (0x20000000, 0x40000), (0x40023000, 0x1000), (0xE000E000, 0x1000)):
            self.cpu.mem_map(address, size)
        with path.open("rb") as stream:
            elf = ELFFile(stream)
            symbol_table = list(elf.get_section_by_name(".symtab").iter_symbols())
            self.symbols = {symbol.name: symbol["st_value"] for symbol in symbol_table}
            self.test_build = "bb_test_build_marker" in self.symbols
            self.test_protocol_enabled = False
            self.dsp_size = next(symbol["st_size"] for symbol in symbol_table if symbol.name == "dsp")
            for segment in elf.iter_segments():
                if segment["p_type"] == "PT_LOAD" and segment["p_filesz"]:
                    # Firmware startup copies .data from its flash LMA to CCM.
                    self.cpu.mem_write(segment["p_paddr"], segment.data())
        self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0xF00000))
        self.cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xF00000)
        self.cpu.mem_write(0x08000200, bytes.fromhex("b0ee400a"))
        self.cpu.emu_start(0x08000201, 0x08000204)
        self.filter_calls = []
        self.stack_low = STACK
        self.cpu.hook_add(UC_HOOK_CODE, self.instruction)
        self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.write)

    def instruction(self, cpu, address, _size, _user):
        self.stack_low = min(self.stack_low, cpu.reg_read(arm.UC_ARM_REG_SP))
        if address == FILTER_SETUP:
            args = tuple(bits_float(cpu.reg_read(getattr(arm, f"UC_ARM_REG_S{n}")))
                         for n in range(3))
            assert cpu.reg_read(arm.UC_ARM_REG_R0) == FILTER_INSTANCE
            assert cpu.reg_read(arm.UC_ARM_REG_R1) == 1
            assert cpu.reg_read(arm.UC_ARM_REG_SP) % 8 == 0
            self.filter_calls.append(args)
            cpu.reg_write(arm.UC_ARM_REG_PC, cpu.reg_read(arm.UC_ARM_REG_LR))

    def write(self, _cpu, _access, address, size, _value, _user):
        # The only expected writes outside payload CCM are its call stack and
        # the explicit sample slots passed by this harness. Any write into OEM
        # calibration, configuration, telemetry or filter memory is a failure.
        # The stubbed OEM filter function's own memory effects are not modeled.
        if address == 0x40023830 and size == 4:
            previous = struct.unpack("<I", self.cpu.mem_read(address, 4))[0]
            assert _value == previous | (1 << 20), "CCM enable changed other clocks"
            return
        allowed = ((self.symbols["__data_start"], self.symbols["__bss_end"]), (STACK - 0x4000, STACK),
                   (SAMPLES, SAMPLES + 8))
        assert any(start <= address and address + size <= end for start, end in allowed), hex(address)

    def call(self, name, *args):
        self.cpu.reg_write(arm.UC_ARM_REG_SP, STACK)
        self.cpu.reg_write(arm.UC_ARM_REG_LR, RETURN | 1)
        for n, arg in enumerate(args):
            self.cpu.reg_write(getattr(arm, f"UC_ARM_REG_R{n}"), arg)
        try:
            self.cpu.emu_start(self.symbols[name] | 1, RETURN, count=2_000_000)
        except UcError as error:
            raise AssertionError((name, hex(self.cpu.reg_read(arm.UC_ARM_REG_PC)), error)) from error
        assert self.cpu.reg_read(arm.UC_ARM_REG_PC) == RETURN, (name, "instruction budget exhausted")
        assert self.cpu.reg_read(arm.UC_ARM_REG_SP) == STACK

    def u32(self, address):
        if isinstance(address, str):
            address = self.symbols[address]
        return struct.unpack("<I", self.cpu.mem_read(address, 4))[0]

    def put32(self, address, value):
        if isinstance(address, str):
            address = self.symbols[address]
        self.cpu.mem_write(address, struct.pack("<I", value))

    def flags(self, mode=2, tx=False):
        self.cpu.mem_write(MODE, bytes([mode]))
        self.cpu.mem_write(TX, bytes([bool(tx)]))

    def registers(self, compressor=0, cessb=0, low=300, high=3000):
        if self.test_build and self.test_protocol_enabled:
            cessb |= TEST_TOKEN
        for reg, word in ((25, compressor), (36, cessb), (38, low | high << 16)):
            self.put32(REGS + reg * 4, word)

    def arm(self, enabled=True):
        if self.test_build:
            self.test_protocol_enabled = enabled
            word = self.u32(REGS + 36 * 4) & 0xffff
            self.put32(REGS + 36 * 4, word | (TEST_TOKEN if enabled else 0))
        else:
            self.put32("bb_lab_arm", COOKIE if enabled else 0)

    def blocks(self, count=3):
        for _ in range(count):
            self.call("bb_begin_block")

    def samples(self, i=0.5, q=-0.25):
        self.cpu.mem_write(SAMPLES, struct.pack("<ff", i, q))
        self.call("bb_tx_audio", SAMPLES)
        audio = bytes(self.cpu.mem_read(SAMPLES, 4))
        self.cpu.mem_write(SAMPLES, struct.pack("<ff", i, q))
        self.call("bb_tx_iq", SAMPLES, SAMPLES + 4)
        iq = bytes(self.cpu.mem_read(SAMPLES, 8))
        return audio, iq

    def bypass(self):
        before = bytes(self.cpu.mem_read(self.symbols["dsp"], self.dsp_size))
        audio, iq = self.samples()
        assert audio == struct.pack("<f", 0.5)
        assert iq == struct.pack("<ff", 0.5, -0.25)
        assert bytes(self.cpu.mem_read(self.symbols["dsp"], self.dsp_size)) == before


def verify(path, test_build=False):
    m = Machine(path)
    assert m.test_build == test_build, "ELF test-build marker does not match requested verification"
    if test_build:
        assert m.u32("bb_test_build_marker") == 0x54535432
    # Verify startup owns only payload state, even if SRAM starts nonzero.
    m.cpu.mem_write(0x20000000, b"\xa5" * 0x10000)
    m.put32(0x40023830, 0x00400001)
    m.call("patch_init")
    assert m.u32(0x40023830) == 0x00500001, "CCM clock not explicitly enabled"
    assert bytes(m.cpu.mem_read(0x20000000, 0x10000)) == b"\xa5" * 0x10000
    assert m.u32("bb_lab_arm") == 0 and not m.filter_calls
    m.put32(RATE, float_bits(12500.0))
    m.registers(cessb=(1 << 8) | (37 << 9), low=160)
    m.flags(tx=True)
    m.blocks(); m.bypass()
    m.flags(tx=False)
    m.blocks(); m.bypass()
    assert not m.filter_calls
    assert m.u32("bb_applied_configurations") == 0
    print("startup/unarmed: PASS CCM clock enabled preserving other bits; SRAM/samples unchanged")

    m.arm()
    m.blocks(1)
    assert not m.filter_calls, "one unstable snapshot applied prematurely"
    m.blocks(2)
    assert m.filter_calls == [(12500.0, 160.0, 3000.0)]
    assert m.u32("bb_applied_configurations") == 1
    m.bypass()  # Even armed/configured, RX callbacks must be inert.
    m.blocks(3)
    assert m.u32("bb_applied_configurations") == 1
    print("RX configuration: PASS stable snapshots, one filter update, unchanged config skipped")

    for mode in (1, 3, 4, 5, 6, 7):
        m.flags(mode, True); m.blocks(1); m.bypass()
    for mode in (0, 2):
        m.flags(mode, True); m.blocks(1)
        audio, iq = m.samples()
        assert audio != struct.pack("<f", 0.5), "voice compressor did not run"
        assert iq != struct.pack("<ff", 0.5, -0.25), "voice CESSB did not run"
    print("mode gating: PASS both voice sidebands active; DATA/CW/AM/FM/RX bypass")

    m.registers(compressor=16, cessb=0, low=250, high=2700)
    m.blocks(4)
    assert len(m.filter_calls) == 1 and m.u32("bb_applied_configurations") == 1
    m.flags(tx=False); m.blocks()
    assert m.filter_calls[-1] == (12500.0, 250.0, 2700.0)
    assert m.u32("bb_applied_configurations") == 2
    m.flags(tx=True); m.blocks(1); m.bypass()  # Wire disabled both features.
    print("TX freeze: PASS deferred config; explicit compressor/CESSB disable bypasses")

    m.flags(tx=False)
    m.registers(low=3000, high=100)
    # First RX transition resets DSP histories; take snapshot after that block.
    m.blocks(1)
    before = bytes(m.cpu.mem_read(m.symbols["dsp"], m.dsp_size))
    m.blocks(2)
    assert bytes(m.cpu.mem_read(m.symbols["dsp"], m.dsp_size)) == before
    assert m.u32("bb_rejected_configurations") >= 1
    assert len(m.filter_calls) == 2 and m.u32("bb_applied_configurations") == 2
    print("invalid configuration: PASS rejection preserves current DSP config and OEM filter")

    # A lab disarm immediately bypasses processing, even during TX. Restoring
    # the stock filter is deferred until RX; no live coefficient rewrite.
    m.flags(tx=True)
    m.arm(False)
    m.blocks(1); m.bypass()
    assert bytes(m.cpu.mem_read(m.symbols["configured"], 1)) == b"\x00"
    assert len(m.filter_calls) == 2
    # Re-arming in the same TX must not resurrect the previous configuration.
    m.arm(); m.blocks(1); m.bypass()
    assert bytes(m.cpu.mem_read(m.symbols["configured"], 1)) == b"\x00"
    m.arm(False)
    m.flags(tx=False); m.blocks(1)
    assert m.filter_calls[-1] == (12500.0, 300.0, 3000.0)
    print("disarm: PASS immediate bypass, rearm in TX inert, stock filter restored on RX")

    m.arm()
    m.put32(RATE, float_bits(10000.0))
    m.registers(cessb=(1 << 8) | (37 << 9), low=160)
    old_calls, old_applied = len(m.filter_calls), m.u32("bb_applied_configurations")
    old_rejected = m.u32("bb_rejected_configurations")
    m.blocks()
    assert len(m.filter_calls) == old_calls
    assert m.u32("bb_applied_configurations") == old_applied
    assert m.u32("bb_rejected_configurations") > old_rejected
    m.flags(tx=True); m.blocks(1); m.bypass()
    m.put32(RATE, float_bits(12500.0))
    m.flags(tx=False); m.blocks()
    assert m.u32("bb_applied_configurations") == old_applied + 1
    print("sample rate: PASS unexpected rate blocks activation; validated rate permits new config")
    print(f"ARM adapter tests passed; deepest callback stack observed {STACK - m.stack_low} bytes.")
    print("Synthetic execution only; no hardware/RF/address/real-time validation.")

    # Changing compressor settings must not rerun the OEM filter constructor,
    # even when the selected passband differs from stock defaults.
    m = Machine(path)
    m.call("patch_init")
    m.arm()
    m.put32(RATE, float_bits(12500.0))
    m.flags(tx=False)
    m.registers(cessb=(1 << 8) | (37 << 9), low=160)
    m.blocks()
    m.registers(compressor=3, cessb=(1 << 8) | (37 << 9), low=160)
    m.blocks()
    assert m.u32("bb_applied_configurations") == 2
    assert m.filter_calls == [(12500.0, 160.0, 3000.0)]
    m.registers(compressor=3, cessb=(1 << 8) | (37 << 9), low=300)
    m.blocks()
    assert m.u32("bb_applied_configurations") == 3
    assert m.filter_calls[-1] == (12500.0, 300.0, 3000.0) and len(m.filter_calls) == 2
    print("configuration reuse: PASS compressor-only change skips OEM filter rebuild; edge change applies")

    if test_build:
        verify_test_activation(path)


def verify_test_activation(path):
    """Prove legacy/default words cannot activate a distributable TEST build."""
    m = Machine(path)
    m.call("patch_init")
    m.put32(RATE, float_bits(12500.0))
    m.flags(tx=False)
    # Stock command RAM starts all zero. Even TX callbacks stay bypassed.
    m.blocks()
    assert m.u32("bb_lab_arm") == 0 and not m.filter_calls
    m.flags(tx=True); m.blocks(); m.bypass()
    # Legacy r13's active bits/boost do not carry the test token.
    m.registers(cessb=(1 << 8) | (60 << 9), low=160)
    m.flags(tx=False); m.blocks()
    m.flags(tx=True); m.blocks(); m.bypass()
    assert not m.filter_calls and m.u32("bb_applied_configurations") == 0
    # A token received while already transmitting cannot start processing.
    m.arm(); m.blocks(); m.bypass()
    assert m.u32("bb_lab_arm") == 0
    # Initial GUI transaction opts in with both effects OFF and stock bandpass.
    m.arm(False)
    m.registers(compressor=16, cessb=0, low=300, high=3000)
    m.arm()
    m.flags(tx=False); m.blocks()
    assert m.u32("bb_lab_arm") == COOKIE
    assert m.filter_calls == [(12500.0, 300.0, 3000.0)]
    m.flags(tx=True); m.blocks(); m.bypass()
    # A partially written token must not arm; each high byte matters.
    for invalid in (0, 0x00580000, 0x54000000, 0x54590000, 0x55580000):
        m.put32(REGS + 36 * 4, invalid | (1 << 8))
        m.blocks(); m.bypass()
        assert m.u32("bb_lab_arm") == 0
    print("TEST activation: PASS stock/legacy/partial tokens bypass; RX-only arming; initial effects OFF")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elf", type=Path, default=ROOT / "build/tx-port.elf")
    parser.add_argument("--test-build", action="store_true",
                        help="require and exercise GUI-token activation in the TEST ELF")
    args = parser.parse_args()
    verify(args.elf, args.test_build)


if __name__ == "__main__":
    main()
