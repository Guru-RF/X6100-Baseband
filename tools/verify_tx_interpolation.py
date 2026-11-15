#!/usr/bin/env python3
"""Validate TX-only interpolation with exact V1.1.9/r13 ARM machine code.

Runs the real OEM constructors, interpolation and RX decimation kernels, and
the linked payload's selector/reset/adapter. Requires unicorn, pyelftools and
numpy. No radio access or firmware image output; instruction emulation is not
a hardware timing or RF validation.
"""
import argparse
import hashlib
import io
import json
from pathlib import Path
import struct

import numpy as np
from elftools.elf.elffile import ELFFile
from unicorn import UC_HOOK_MEM_WRITE, UC_HOOK_CODE, UC_PROT_READ, UC_PROT_EXEC
from unicorn import arm_const as arm

from compare_cessb import Machine, Interpolator, R13_SHA, SAMPLE, STACK
from plan_patch import BASE, validate_oem, thumb_bw
from verify_adapter import COOKIE, TEST_TOKEN, MODE, TX, RATE, REGS, float_bits

ROOT = Path(__file__).resolve().parents[1]
TX_INSTANCES = (0x20007490, 0x200075DC)
RX_INSTANCES = (0x2000665C, 0x200068A8)
OEM_COEFFICIENTS = 0x0803E3E4
KERNEL = 0x08035364
RX_KERNEL = 0x08035C88
OUTPUT = SAMPLE + 64
R13_COEFFICIENT_SHA = "d6f29c698597e5a8f5ac831dfd45dee937e721e203ba961949ef16cb53769e93"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Payload(Machine):
    def __init__(self, oem, elf_bytes, redirect_hooks=False):
        super().__init__()
        self.cpu.mem_write(BASE, oem)
        with io.BytesIO(elf_bytes) as stream:
            elf = ELFFile(stream)
            self.symbols = {s.name: s["st_value"]
                            for s in elf.get_section_by_name(".symtab").iter_symbols()}
            self.test_build = "bb_test_build_marker" in self.symbols
            for segment in elf.iter_segments():
                if segment["p_type"] == "PT_LOAD" and segment["p_filesz"]:
                    self.cpu.mem_write(segment["p_paddr"], segment.data())
        self.call(self.symbols["patch_init"])
        # These are the exact constructors called from OEM startup. They copy
        # the shared ROM coefficients to each filter's separate RAM storage.
        self.call(0x08023530, RX_INSTANCES[0], OEM_COEFFICIENTS)
        self.call(0x080238DC, TX_INSTANCES[0], OEM_COEFFICIENTS)
        self.call(0x08023668, 0x20007728)
        if redirect_hooks:
            # In-memory emulator code only; never emit a patched image.
            for address, symbol in ((0x080267CE, "v119_tx_iq_wrapper"),
                                    (0x08026866, "v119_tx_iq_second_wrapper")):
                self.cpu.mem_write(address, thumb_bw(address, self.symbols[symbol] & ~1))
        self.cpu.mem_protect(0x08000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
        self.guard = True
        self.extra_writes = ()
        self.stop_at = None
        self.cpu.hook_add(UC_HOOK_MEM_WRITE, self.write)
        self.cpu.hook_add(UC_HOOK_CODE, self.instruction)

    def instruction(self, cpu, address, _size, _user):
        if address == self.stop_at:
            cpu.emu_stop()

    def run_until(self, start, end):
        # An explicit instruction hook also stops inside already-cached
        # Unicorn translation blocks when successive runs change the end PC.
        self.stop_at = end
        try:
            self.cpu.emu_start(start | 1, 0, count=1000000)
            assert self.cpu.reg_read(arm.UC_ARM_REG_PC) == end
        finally:
            self.stop_at = None

    def write(self, _cpu, _access, address, size, _value, _user):
        if not self.guard:
            return
        allowed = ((self.symbols["__data_start"], self.symbols["__bss_end"]),
                   (STACK - 0x4000, STACK), (SAMPLE, SAMPLE + 128)) + self.extra_writes
        assert any(start <= address and address + size <= end
                   for start, end in allowed), ("unexpected memory write", hex(address), size,
                                               "pc", hex(self.cpu.reg_read(arm.UC_ARM_REG_PC)))

    def named(self, name, *args):
        self.call(self.symbols[name], *args)
        return self.cpu.reg_read(arm.UC_ARM_REG_R0)

    def put32(self, address, value):
        if isinstance(address, str):
            address = self.symbols[address]
        self.cpu.mem_write(address, struct.pack("<I", value))

    def arm(self, enabled=True):
        if self.test_build:
            word = struct.unpack("<I", self.cpu.mem_read(REGS + 36 * 4, 4))[0] & 0xffff
            self.put32(REGS + 36 * 4, word | (TEST_TOKEN if enabled else 0))
        else:
            self.put32("bb_lab_arm", COOKIE if enabled else 0)

    def flags(self, mode=2, tx=False):
        self.cpu.mem_write(MODE, bytes([mode]))
        self.cpu.mem_write(TX, bytes([bool(tx)]))

    def blocks(self, count=1):
        # Only configuration calls may legitimately update the two OEM BPFs.
        # Interpolation checks below do not include this permission.
        self.extra_writes = ((0x20007728, 0x200078C0),)
        try:
            for _ in range(count):
                self.named("bb_begin_block")
        finally:
            self.extra_writes = ()

    def configure(self, cessb=True):
        self.put32(RATE, float_bits(12500.0))
        for register, value in ((25, 0), (36, (256 | (37 << 9)) if cessb else 0),
                                (38, 300 | (3000 << 16))):
            self.put32(REGS + 4 * register, value)
        self.arm()
        self.flags(tx=False)
        self.blocks(3)
        applied = struct.unpack("<I", self.cpu.mem_read(self.symbols["bb_applied_configurations"], 4))[0]
        assert applied > 0, "interpolation harness never activated DSP"
        self.flags(tx=True)
        self.blocks()

    def select(self, original):
        return self.named("bb_tx_interpolator", original)

    def descriptor(self, address):
        return struct.unpack("<BBHII", self.cpu.mem_read(address, 12))

    def interpolate(self, values, original):
        result = []
        for value in values:
            selected = self.select(original)
            self.cpu.mem_write(SAMPLE, struct.pack("<f", value))
            self.call(KERNEL, selected, SAMPLE, OUTPUT, 1)
            result.extend(struct.unpack("<8f", self.cpu.mem_read(OUTPUT, 32)))
        return np.asarray(result)

    def rx(self, values):
        result = []
        self.guard = False
        try:
            for start in range(0, len(values), 8):
                self.cpu.mem_write(SAMPLE, struct.pack("<8f", *values[start:start + 8]))
                for instance in RX_INSTANCES:
                    self.call(RX_KERNEL, instance, SAMPLE, OUTPUT, 8)
                    result.append(bytes(self.cpu.mem_read(OUTPUT, 4)))
        finally:
            self.guard = True
        return b"".join(result)


def hook_chain(oem, elf_bytes):
    """Run both actual wrappers and intervening OEM DMA packing instructions.

    Compare with independently composed callback + explicitly selected kernels,
    preserving the true first-Q/second-I mapping and all live register values.
    This includes actual C callbacks; no selector or DSP function is stubbed.
    """
    registers = [getattr(arm, f"UC_ARM_REG_R{n}") for n in range(13)]
    floats = [getattr(arm, f"UC_ARM_REG_S{n}") for n in range(32)]
    dma = 0x20018C00
    # After the second kernel, OEM finishes interleaving the eight Q/I pairs
    # at0x080268bc. Stop before unrelated mode dispatch resumes at0x080268c0.
    end = 0x080268C0

    def snapshot(cpu):
        return ([cpu.reg_read(r) for r in registers], [cpu.reg_read(r) for r in floats],
                cpu.reg_read(arm.UC_ARM_REG_APSR) & 0xF80F0000,
                cpu.reg_read(arm.UC_ARM_REG_FPSCR), cpu.reg_read(arm.UC_ARM_REG_SP),
                cpu.reg_read(arm.UC_ARM_REG_LR))

    def seed(m, descriptor):
        cpu = m.cpu
        for n, register in enumerate(registers):
            cpu.reg_write(register, 0x100 + n)
        for n, register in enumerate(floats):
            cpu.reg_write(register, 0x3F000000 + n)
        for register, value in ((arm.UC_ARM_REG_R0, descriptor),
                                (arm.UC_ARM_REG_R1, STACK + 0x50),
                                (arm.UC_ARM_REG_R2, 0x200075BC),
                                (arm.UC_ARM_REG_R3, 1),
                                (arm.UC_ARM_REG_R4, dma - 0xC00),
                                (arm.UC_ARM_REG_R5, dma),
                                (arm.UC_ARM_REG_R6, 0x20007590),
                                (arm.UC_ARM_REG_R9, 0),
                                (arm.UC_ARM_REG_R11, 0x20008590),
                                (arm.UC_ARM_REG_SP, STACK),
                                (arm.UC_ARM_REG_LR, 0x080267D3),
                                (arm.UC_ARM_REG_APSR, 0xA80B0000),
                                (arm.UC_ARM_REG_FPSCR, 0x01000000)):
            cpu.reg_write(register, value)

    cases = []
    for mode, tx, cessb in ((0, True, True), (2, True, True), (2, True, False),
                            (1, True, True), (3, True, True), (4, True, True),
                            (5, True, True), (6, True, True), (7, True, True),
                            (2, False, True)):
        hooked, reference = Payload(oem, elf_bytes, True), Payload(oem, elf_bytes)
        entries = [[], []]
        for index, m in enumerate((hooked, reference)):
            m.configure(cessb)
            m.flags(mode, tx); m.blocks()
            # Allow precisely the known OEM output buffers, descriptor states
            # (only needed in bypass), live caller frame and DMA output slots.
            m.extra_writes = ((STACK, STACK + 0x80), (dma, dma + 64),
                              (0x200075BC, 0x200075DC), (0x20007708, 0x20007728))
            if not (tx and mode in (0, 2) and cessb):
                m.extra_writes += ((0x2000759C, 0x200075BC), (0x200076E8, 0x20007708))
            def on_instruction(cpu, address, _size, _user, capture=entries[index]):
                if address == KERNEL:
                    capture.append(snapshot(cpu))
            m.cpu.hook_add(UC_HOOK_CODE, on_instruction)
        selected = tuple(reference.select(instance) for instance in TX_INSTANCES)
        for sample in range(24):
            # Nonidentical channels expose swapped descriptors or a second
            # accidental CESSB pass, while retaining state across calls.
            iq = (0.8 * np.cos(sample * .51), 0.6 * np.sin(sample * .37))
            for m in (hooked, reference):
                m.cpu.mem_write(STACK + 0x50, struct.pack("<ff", iq[1], iq[0]))
            reference.named("bb_tx_iq", STACK + 0x54, STACK + 0x50)
            seed(reference, selected[0])
            seed(hooked, TX_INSTANCES[0])
            # Reference: first kernel directly, exact OEM code between calls,
            # redirect second argument explicitly, then execute its OEM BL.
            reference.run_until(KERNEL, 0x080267D2)
            reference.run_until(0x080267D2, 0x08026866)
            assert [reference.cpu.reg_read(r) for r in registers[:4]] == [
                TX_INSTANCES[1], STACK + 0x54, 0x20007708, 1]
            reference.cpu.reg_write(arm.UC_ARM_REG_R0, selected[1])
            reference.run_until(0x08026866, end)
            # Hooked: original first call site -> wrapper -> OEM packing ->
            # second wrapper -> second OEM kernel -> final interleaved output.
            hooked.run_until(0x080267CE, end)
            assert hooked.cpu.reg_read(arm.UC_ARM_REG_PC) == end
            assert reference.cpu.reg_read(arm.UC_ARM_REG_PC) == end
            assert snapshot(hooked.cpu) == snapshot(reference.cpu)
            assert entries[0] == entries[1], "wrapper altered live OEM kernel-entry state"
            assert len(entries[0]) == 2 * (sample + 1)
            for address, size in ((dma, 64), (STACK, 0x80), (0x20000000, 0x10000),
                                  (hooked.symbols["__data_start"],
                                   hooked.symbols["__bss_end"] - hooked.symbols["__data_start"])):
                assert bytes(hooked.cpu.mem_read(address, size)) == bytes(reference.cpu.mem_read(address, size)), hex(address)
        cases.append({"mode": mode, "tx": tx, "cessb": cessb, "input_pairs": 24,
                      "output_pairs": 192, "registers_samples_and_histories_identical": True})
    return {"execution": "actual linked wrappers/callbacks + exact OEM kernels/interleaving instructions",
            "memory_write_scope": "Allows the original interpolation output slots and interleaved DMA output writes; bypass additionally permits original interpolation history writes. All other OEM writes are rejected. The separate direct private-kernel test writes only its synthetic output buffer and preserves the first 64KiB of OEM SRAM.",
            "first_call": {"site": "0x080267ce", "descriptor": hex(TX_INSTANCES[0]),
                           "input": "Q at entry SP+0x50", "output": "0x200075bc"},
            "second_call": {"site": "0x08026866", "descriptor": hex(TX_INSTANCES[1]),
                            "input": "I at entry SP+0x54", "output": "0x20007708"},
            "all_live_gpr_vfp_apsr_fpscr_sp_lr_match_at_both_kernel_entries": True,
            "cases": cases}


def verify(oem, r13, elf_bytes):
    validate_oem(oem)
    assert sha(r13) == R13_SHA, "requires exact V1.1.6 r13 plaintext"
    m = Payload(oem, elf_bytes)
    original_coefficients = oem[OEM_COEFFICIENTS - BASE:OEM_COEFFICIENTS - BASE + 256]
    r13_coefficients = r13[0x1D82C:0x1D92C]
    assert sha(r13_coefficients) == R13_COEFFICIENT_SHA

    stock_descriptors = []
    for instance in TX_INSTANCES:
        factor, padding, length, coeff, state = m.descriptor(instance)
        assert (factor, padding, length, coeff, state) == (8, 0, 8, instance + 12, instance + 0x10C)
        assert bytes(m.cpu.mem_read(coeff, 256)) == original_coefficients
        stock_descriptors.append({"descriptor": hex(instance), "coefficients": hex(coeff),
                                  "state": hex(state), "block_size": 1})
    for instance in RX_INSTANCES:
        factor, padding, taps, coeff, state = m.descriptor(instance)
        assert (factor, padding, taps, coeff, state) == (8, 0, 64, instance + 12, instance + 0x10C)
        assert bytes(m.cpu.mem_read(coeff, 256)) == original_coefficients
    for original in TX_INSTANCES:
        assert m.select(original) == original, "unarmed selector changed OEM instance"
    m.configure()
    selected = tuple(m.select(instance) for instance in TX_INSTANCES)
    assert len(set(selected)) == 2
    private_states = []
    for descriptor in selected:
        factor, padding, length, coeff, state = m.descriptor(descriptor)
        assert (factor, padding, length) == (8, 0, 8)
        assert 0x08080000 <= descriptor < 0x080A0000
        assert 0x08080000 <= coeff < 0x080A0000
        assert m.symbols["__data_start"] <= state and state + 32 <= m.symbols["__bss_end"]
        assert bytes(m.cpu.mem_read(coeff, 256)) == r13_coefficients
        private_states.append(state)
    assert abs(private_states[0] - private_states[1]) >= 32, "I/Q histories overlap"

    # Never reinterpret another CMSIS instance, even if its shape is similar.
    unknown = (0, *RX_INSTANCES, 0x20006C30, 0x20007244, 0x20007494)
    for address in unknown:
        assert m.select(address) == address
        assert m.named("tx_interpolation_select", address, True) == address
    for address in TX_INSTANCES:
        assert m.named("tx_interpolation_select", address, False) == address

    # Every voice/nonvoice mode and RX/TX combination goes through the actual
    # adapter gate, with no OEM interpolation descriptor or coefficient writes.
    gating = []
    for tx in (False, True):
        for mode in range(8):
            m.flags(mode, tx)
            m.blocks()
            expected_active = tx and mode in (0, 2)
            actual = tuple(m.select(instance) for instance in TX_INSTANCES)
            assert actual == (selected if expected_active else TX_INSTANCES)
            gating.append({"mode": mode, "tx": tx, "corrected": expected_active})
    m.configure(False)
    assert tuple(m.select(instance) for instance in TX_INSTANCES) == TX_INSTANCES
    m.configure(True)

    # Independent complex channels, startup impulse and random data through
    # actual V1.1.9 kernel with the payload-selected descriptors, compared to
    # actual r13 interpolation and a separately evaluated FIR model.
    random = np.random.default_rng(0x6100)
    source = random.normal(0, .4, (2, 256)).astype(np.float32)
    source[:, :16] = 0
    source[0, 0], source[1, 4] = 1, -0.5
    snapshots = bytes(m.cpu.mem_read(0x20000000, 0x10000))
    errors, startup_outputs = [], []
    for channel, instance in enumerate(TX_INSTANCES):
        reference = Interpolator(r13, 0x08035388, 0x0803D82C)
        actual = m.interpolate(source[channel], instance)
        startup_outputs.append(actual)
        old = []
        for value in source[channel]:
            reference.cpu.mem_write(SAMPLE, struct.pack("<f", value))
            reference.call(reference.address, 0x20002000, SAMPLE, OUTPUT, 1)
            old.extend(struct.unpack("<8f", reference.cpu.mem_read(OUTPUT, 32)))
        old_error = float(np.max(np.abs(actual - old)))
        model_error = float(np.max(np.abs(actual - reference.apply(source[channel]) / 8)))
        assert old_error < 1e-7 and model_error < 1e-7
        errors.append({"channel": channel, "samples": len(source[channel]),
                       "r13_kernel_max_absolute_error": old_error,
                       "fir_model_max_absolute_error": model_error})
    assert bytes(m.cpu.mem_read(0x20000000, 0x10000)) == snapshots
    assert bytes(m.cpu.mem_read(OEM_COEFFICIENTS, 256)) == original_coefficients

    # A real receive decimation run after corrected TX must match a machine
    # that never ran TX, including its output bytes and complete RX state.
    control = Payload(oem, elf_bytes)
    rx_source = random.normal(0, .2, 256).astype(np.float32)
    actual_rx, control_rx = m.rx(rx_source), control.rx(rx_source)
    assert actual_rx == control_rx
    assert bytes(m.cpu.mem_read(RX_INSTANCES[0], 0x498)) == bytes(control.cpu.mem_read(RX_INSTANCES[0], 0x498))

    # Both histories are independently cleared by key-up and mode changes.
    m.flags(tx=False); m.blocks()
    for state in private_states:
        assert bytes(m.cpu.mem_read(state, 32)) == bytes(32)
    m.flags(tx=True); m.blocks()
    first = m.interpolate(source[0], TX_INSTANCES[0])
    assert np.array_equal(first, startup_outputs[0]), "second PTT used stale interpolation history"
    m.flags(mode=0, tx=True); m.blocks()
    for state in private_states:
        assert bytes(m.cpu.mem_read(state, 32)) == bytes(32)
    second = m.interpolate(source[0], TX_INSTANCES[0])
    assert np.array_equal(first, second), "fresh mode used stale interpolation history"
    m.arm(False)
    assert tuple(m.select(instance) for instance in TX_INSTANCES) == TX_INSTANCES
    m.blocks()
    m.arm()
    assert tuple(m.select(instance) for instance in TX_INSTANCES) == TX_INSTANCES
    m.blocks()
    assert tuple(m.select(instance) for instance in TX_INSTANCES) == TX_INSTANCES
    m.flags(tx=False)
    m.put32(RATE, float_bits(10000.0))
    m.blocks(3)
    m.flags(tx=True); m.blocks()
    assert tuple(m.select(instance) for instance in TX_INSTANCES) == TX_INSTANCES

    return {"oem_sha256": sha(oem), "r13_sha256": sha(r13), "elf_sha256": sha(elf_bytes),
            "coefficient_sha256": sha(r13_coefficients),
            "coefficient_identity": "payload float32 bytes equal exact r13 interpolation table",
            "oem_tx_descriptors": stock_descriptors,
            "private_descriptors": [hex(value) for value in selected],
            "private_states": [hex(value) for value in private_states], "history_bytes": 64,
            "gating": gating, "cessb_disabled_bypasses": True,
            "unknown_instances_bypass": [hex(value) for value in unknown],
            "real_kernel_comparisons": errors,
            "direct_private_kernel_test_preserves_all_oem_sram_first_64KiB": True,
            "shared_rom_coefficients_unchanged": True,
            "rx_decimation_kernel": hex(RX_KERNEL),
            "rx_output_and_filter_state_equal_untouched_control": True,
            "rx_output_sha256": sha(actual_rx), "rx_input_samples": len(rx_source),
            "rekey_and_mode_reset_private_histories": True,
            "disarm_and_rearm_during_tx_bypass": True,
            "unexpected_rx_sample_rate_prevents_tx_activation": True,
            "actual_hook_chain": hook_chain(oem, elf_bytes),
            "limits": ["Synthetic ARM execution, no peripheral/IRQ/hardware timing.",
                       "Hook chain covers the two interpolation calls and intervening OEM packing, not the complete ISR.",
                       "No ALC/DAC/RF validation or installable firmware."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oem_bin", type=Path)
    parser.add_argument("r13_bin", type=Path)
    parser.add_argument("--elf", type=Path, default=ROOT / "build/tx-port.elf")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = verify(args.oem_bin.read_bytes(), args.r13_bin.read_bytes(), args.elf.read_bytes())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    print("TX interpolation: PASS exact r13 coefficients, real kernels, mode gates, private histories and unchanged RX")
    print(json.dumps({"real_kernel_comparisons": result["real_kernel_comparisons"],
                      "rx_output_sha256": result["rx_output_sha256"]}, indent=2))


if __name__ == "__main__":
    main()
