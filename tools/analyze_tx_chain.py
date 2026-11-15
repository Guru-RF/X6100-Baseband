#!/usr/bin/env python3
"""Offline TX scaling and dynamic ARM instruction-count comparison.

Accepts the exact plaintext V1.1.9 and upstream r13 images and a built payload
ELF. Does not produce a firmware image or touch a radio. Optional dependencies:
unicorn and pyelftools. Counts are executed instructions, NOT Cortex-M4 cycles.
"""
import argparse
import cmath
import hashlib
import json
import math
from pathlib import Path
import statistics
import struct

from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_HOOK_CODE
from unicorn import arm_const as arm

from plan_patch import BASE, validate_oem
from verify_adapter import (Machine, RATE, SAMPLES, STACK, RETURN,
                            FILTER_SETUP, FILTER_INSTANCE, float_bits, bits_float)
from verify_oem_filter import FREQUENCIES, response

R13_SHA256 = "42815a8514831e07bce46e1ddf27376af863488af3663e53d450cfb99713591e"
FS = 12500.0


def sha(data):
    return hashlib.sha256(data).hexdigest()


class Binary:
    def __init__(self, image, old=False):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
        for address, size in ((0x08000000, 0x100000), (0x20000000, 0x40000),
                              (0x10000000, 0x10000), (0xE000E000, 0x1000)):
            self.cpu.mem_map(address, size)
        self.cpu.mem_write(BASE, image)
        source, size = (0x5BBA0, 0x3560) if old else (0x5C5D0, 0x3570)
        self.cpu.mem_write(0x20000000, image[source:source + size])
        self.cpu.mem_write(0xE000ED88, struct.pack("<I", 0xF00000))
        self.cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xF00000)
        self.count = 0
        self.cpu.hook_add(UC_HOOK_CODE, self.instruction)

    def instruction(self, *_):
        self.count += 1

    def call(self, address, *args, floats=()):
        self.count = 0
        self.cpu.reg_write(arm.UC_ARM_REG_SP, STACK)
        self.cpu.reg_write(arm.UC_ARM_REG_LR, RETURN | 1)
        for n, value in enumerate(args):
            self.cpu.reg_write(getattr(arm, f"UC_ARM_REG_R{n}"), value)
        for n, value in enumerate(floats):
            self.cpu.reg_write(getattr(arm, f"UC_ARM_REG_S{n}"), float_bits(value))
        self.cpu.emu_start(address | 1, RETURN, count=5_000_000)
        assert self.cpu.reg_read(arm.UC_ARM_REG_PC) == RETURN, hex(address)
        assert self.cpu.reg_read(arm.UC_ARM_REG_SP) == STACK
        return self.count


def filters(image, old):
    m = Binary(image, old)
    instances = (0x20009AB0, 0x20009B7C) if old else (0x20007728, 0x200077F4)
    if old:
        # This is the layout initialized by r13 setup. Force a first update.
        for address in instances:
            m.cpu.mem_write(address, struct.pack("<3I", 5, address + 116, address + 16))
            m.cpu.mem_write(address + 196, b"\xff" * 8)
    else:
        m.call(0x08023668, instances[0])
    constructor = 0x08021764 if old else 0x0802397C
    result = []
    for low, high in ((160, 3000), (300, 3000), (300, 2400)):
        count = m.call(constructor, instances[0], 1, floats=(FS, low, high))
        same_count = m.call(constructor, instances[0], 1, floats=(FS, low, high))
        cascades = []
        for address in instances:
            stages, state, coefficients = struct.unpack("<3I", m.cpu.mem_read(address, 12))
            assert stages == 5 and state == address + 116 and coefficients == address + 16
            gain = struct.unpack("<f", m.cpu.mem_read(address + 12, 4))[0]
            values = struct.unpack("<25f", m.cpu.mem_read(coefficients, 100))
            cascades.append((gain, values))
        magnitudes = {hz: response(cascades, hz) for hz in FREQUENCIES}
        assert 0.98 < magnitudes[1000] < 1.02
        result.append({"low_hz": low, "high_hz": high,
                       "external_stage_gains": [c[0] for c in cascades],
                       "external_gain_product": math.prod(c[0] for c in cascades),
                       "actual_cascade_magnitude": magnitudes,
                       "constructor_instructions_changed": count,
                       "constructor_instructions_unchanged": same_count})
    return result


def stats(values):
    return {"samples": len(values), "minimum": min(values),
            "mean": statistics.mean(values), "maximum": max(values)}


class WrapperMachine(Machine):
    """Run real payload wrappers; omit the unchanged OEM tail routine.

    Unlike instruction cost, LDM/VLDM/STM/VSTM bus costs are not one cycle.
    This harness verifies how much code is added, not its real-time cost.
    """
    def __init__(self, elf):
        self.count = 0
        super().__init__(elf)

    def instruction(self, cpu, address, size, user):
        super().instruction(cpu, address, size, user)
        if address in (0x08036000, 0x08035364, 0x0802E1D4):
            if address == 0x0802E1D4:
                cpu.reg_write(arm.UC_ARM_REG_R0, 1)
            cpu.reg_write(arm.UC_ARM_REG_PC, cpu.reg_read(arm.UC_ARM_REG_LR))
            return
        self.count += 1

    def wrapped(self, kind, audio=None, iq=None):
        names = {"audio": ("v119_tx_audio_wrapper", 0x080247CE),
                 "iq": ("v119_tx_iq_wrapper", 0x080267D2),
                 "iq_second": ("v119_tx_iq_second_wrapper", 0x0802686A),
                 "block": ("v119_begin_block_wrapper", 0x0802459E)}
        name, continuation = names[kind]
        if name not in self.symbols:
            assert kind == "iq_second"  # Earlier baseline had only one I/Q hook.
            return 0
        stack = STACK - 0x100
        self.cpu.reg_write(arm.UC_ARM_REG_SP, stack)
        self.cpu.reg_write(arm.UC_ARM_REG_LR, RETURN | 1)
        if audio is not None:
            self.cpu.mem_write(stack + 0x48, audio)
        if iq is not None:
            self.cpu.mem_write(stack + 0x54, iq[:4])  # I
            self.cpu.mem_write(stack + 0x50, iq[4:])  # Q
        descriptor = 0x20007728 if kind == "audio" else (0x20007490 if kind == "iq" else 0x200075DC)
        source = 0x48 if kind == "audio" else (0x50 if kind == "iq" else 0x54)
        destination = stack + 0x4C if kind == "audio" else (0x200075BC if kind == "iq" else 0x20007708)
        for register, value in ((arm.UC_ARM_REG_R0, descriptor),
                                (arm.UC_ARM_REG_R1, stack + source),
                                (arm.UC_ARM_REG_R2, destination),
                                (arm.UC_ARM_REG_R3, 1)):
            self.cpu.reg_write(register, value)
        self.count = 0
        self.cpu.emu_start(self.symbols[name] | 1, continuation, count=2_000_000)
        assert self.cpu.reg_read(arm.UC_ARM_REG_PC) == continuation, name
        assert self.cpu.reg_read(arm.UC_ARM_REG_SP) == stack, name
        return self.count


class ActualFilterMachine(Machine):
    """Payload callbacks plus the hash-verified OEM filter implementation."""
    def __init__(self, elf, oem):
        super().__init__(elf)
        self.cpu.mem_write(BASE, oem)
        self.cpu.mem_write(0x20000000, oem[0x5C5D0:0x5C5D0 + 0x3570])
        self.symbols["oem_tx_filter_init"] = 0x08023668
        self.call("oem_tx_filter_init", FILTER_INSTANCE)
        self.filter_calls.clear()

    def instruction(self, cpu, address, _size, _user):
        self.stack_low = min(self.stack_low, cpu.reg_read(arm.UC_ARM_REG_SP))
        if address == FILTER_SETUP:
            assert cpu.reg_read(arm.UC_ARM_REG_R0) == FILTER_INSTANCE
            assert cpu.reg_read(arm.UC_ARM_REG_R1) == 1
            assert cpu.reg_read(arm.UC_ARM_REG_SP) % 8 == 0
            self.filter_calls.append(tuple(bits_float(cpu.reg_read(getattr(arm, f"UC_ARM_REG_S{n}")))
                                           for n in range(3)))

    def write(self, cpu, access, address, size, value, user):
        # The two adjacent 204-byte OEM TX biquad objects are intentionally
        # writable ONLY in this real-constructor harness.
        if 0x20007728 <= address and address + size <= 0x200078C0:
            return
        super().write(cpu, access, address, size, value, user)


def configuration_timing(elf, oem):
    m = ActualFilterMachine(elf, oem)
    counts = {"all": 0, "constructor": 0, "return": None}
    def hook(cpu, address, *_):
        counts["all"] += 1
        if address == FILTER_SETUP:
            counts["return"] = cpu.reg_read(arm.UC_ARM_REG_LR) & ~1
        elif address == counts["return"]:
            counts["return"] = None
        if counts["return"] is not None:
            counts["constructor"] += 1
    m.cpu.hook_add(UC_HOOK_CODE, hook)
    # OEM initialization executed before adding this hook. Invalidate Unicorn's
    # translated blocks so reused OEM code is counted on this new hook too.
    m.cpu.ctl_remove_cache(0x08000000, 0x08100000)
    m.call("patch_init")
    prepare(m)
    paths = {}
    def capture(name, blocks=1):
        values = []
        for _ in range(blocks):
            counts.update(all=0, constructor=0, **{"return": None})
            before = len(m.filter_calls)
            m.call("bb_begin_block")
            values.append({"callback_instructions": counts["all"],
                           "included_oem_constructor_instructions": counts["constructor"],
                           "oem_constructor_calls": m.filter_calls[before:]})
        paths[name] = values
    capture("initial_rx_stability_and_apply", 4)
    assert m.u32("bb_applied_configurations") == 1, "configuration timing tested bypass"
    capture("unchanged_rx")
    m.flags(tx=True)
    capture("first_tx")
    capture("unchanged_tx")
    m.flags(mode=0, tx=True)
    capture("tx_sideband_change")
    m.flags(tx=False)
    capture("return_to_rx")
    m.registers(compressor=1 << 11, cessb=256 | (37 << 9), low=160)
    capture("makeup_only_change", 3)
    m.registers(compressor=1 << 11, cessb=256 | (45 << 9), low=160)
    capture("cessb_boost_only_change", 3)
    m.registers(compressor=1 << 11, cessb=256 | (45 << 9), low=300, high=2400)
    capture("tx_filter_edges_change", 3)
    m.registers(low=3000, high=100)
    capture("invalid_configuration", 3)
    m.arm(False)
    capture("disarm_stock_filter_restore")
    return {"units": "executed ARM instructions, not cycles",
            "oem_filter": "Real V1.1.9 constructor; its nested instruction count is already included in callback_instructions, never add it again.",
            "paths": paths,
            "limits": "C callbacks only. Synthetic SRAM/registers; no DMA/peripherals, complete ISR, assembly wrapper or preemption. Configuration remains inside DMA context and has no proven deadline margin."}


def prepare(machine):
    machine.put32(RATE, float_bits(FS))
    machine.registers(cessb=256 | (37 << 9), low=160)
    machine.flags(tx=False)
    machine.arm()


def timing(elf, r13, samples):
    m = Machine(elf)
    counter = [0]
    core = {"entry": None, "return": None, "active": False, "count": 0}
    def hook(cpu, address, *_):
        counter[0] += 1
        if core["entry"] == address:
            core["active"] = True
            core["return"] = cpu.reg_read(arm.UC_ARM_REG_LR) & ~1
        elif core["active"] and address == core["return"]:
            core["active"] = False
        if core["active"]:
            core["count"] += 1
    m.cpu.hook_add(UC_HOOK_CODE, hook)
    def call(name, *args, track=None):
        counter[0] = 0
        core.update({"entry": (m.symbols[track] & ~1) if track else None,
                     "active": False, "count": 0, "return": None})
        m.call(name, *args)
        return counter[0]
    new = {"patch_init": call("patch_init")}
    prepare(m)
    new["first_four_rx_blocks_oem_constructor_stubbed"] = [call("bb_begin_block") for _ in range(4)]
    assert m.u32("bb_applied_configurations") == 1, "sample timing tested bypass"
    m.flags(tx=True)
    new["first_tx_block"] = call("bb_begin_block")
    new["unchanged_tx_block"] = call("bb_begin_block")
    wrapped = WrapperMachine(elf)
    wrapped.call("patch_init")
    prepare(wrapped)
    new["wrapped_first_four_rx_blocks_oem_constructor_stubbed"] = [wrapped.wrapped("block") for _ in range(4)]
    assert wrapped.u32("bb_applied_configurations") == 1, "wrapper timing tested bypass"
    wrapped.flags(tx=True)
    new["wrapped_first_tx_block"] = wrapped.wrapped("block")
    new["wrapped_unchanged_tx_block"] = wrapped.wrapped("block")
    old_m = Binary(r13, True)
    old = {"full_r13_init": old_m.call(0x0807FAE4),
           "tx_coefficient_setup": old_m.call(0x0807F75C, floats=(5.0,))}
    old_m.cpu.mem_write(0x2000A8D5, b"\x02")
    old_m.cpu.mem_write(0x200000C4, b"\x00")
    old["cessb_set_params"] = old_m.call(0x080837E4, 1, floats=(3.7,))
    counts = {"new_audio": [], "new_iq": [], "new_audio_core": [], "new_iq_core": [],
              "r13_audio": [], "r13_iq": [], "new_wrapped_audio": [],
              "new_wrapped_iq_first": [], "new_wrapped_iq_second": [],
              "new_wrapped_sample_total": []}
    inputs = bytearray()
    for k in range(samples):
        audio = struct.pack("<f", .02 * math.sin(2 * math.pi * 1000 * k / FS))
        iq = .7 * cmath.exp(2j * math.pi * 800 * k / FS) + .6 * cmath.exp(2j * math.pi * 2100 * k / FS)
        iq = struct.pack("<ff", iq.real, iq.imag)
        inputs += audio + iq
        m.cpu.mem_write(SAMPLES, audio)
        counts["new_audio"].append(call("bb_tx_audio", SAMPLES, track="tx_dsp_compress"))
        counts["new_audio_core"].append(core["count"])
        m.cpu.mem_write(SAMPLES, iq)
        counts["new_iq"].append(call("bb_tx_iq", SAMPLES, SAMPLES + 4, track="tx_dsp_cessb"))
        counts["new_iq_core"].append(core["count"])
        counts["new_wrapped_audio"].append(wrapped.wrapped("audio", audio=audio))
        counts["new_wrapped_iq_first"].append(wrapped.wrapped("iq", iq=iq))
        counts["new_wrapped_iq_second"].append(wrapped.wrapped("iq_second"))
        counts["new_wrapped_sample_total"].append(sum(counts[name][-1] for name in
            ("new_wrapped_audio", "new_wrapped_iq_first", "new_wrapped_iq_second")))
        old_m.cpu.mem_write(SAMPLES, audio)
        counts["r13_audio"].append(old_m.call(0x0807FB8C, SAMPLES))
        old_m.cpu.mem_write(SAMPLES, iq)
        counts["r13_iq"].append(old_m.call(0x08083868, SAMPLES, SAMPLES + 4))
    assert min(counts["new_audio_core"]) > 0 and min(counts["new_iq_core"]) > 0, "DSP cores were never entered"
    return {"units": "executed ARM instructions, not cycles",
            "inputs": {"samples": samples, "sample_rate_hz": FS,
                       "audio": "0.02*sin(2*pi*1000*k/12500)",
                       "iq": "0.7*exp(j*2*pi*800*k/12500)+0.6*exp(j*2*pi*2100*k/12500)",
                       "packed_float32_sha256": sha(inputs)},
            "settings": "USB; ratio2:1, threshold-57dB, makeup6dB, CESSB3.7dB, filter160..3000Hz",
            "new_setup": new, "r13_setup": old,
            "all_samples": {name: stats(values) for name, values in counts.items()},
            "after_first_128_samples": {name: stats(values[128:]) for name, values in counts.items()},
            "limits": ["New sample counts include adapter callbacks; new_*_core counts track only core function entry through return, for comparison with direct r13 compressor/CESSB calls.",
                       "new_wrapped_* counts execute the complete added assembly wrappers and their C callees; unchanged OEM tail routines are stubbed and excluded. new_wrapped_sample_total sums mono and both I/Q wrapper paths per sample. Baselines without the second I/Q hook report zero added instructions for that hook.",
                       "Direct new_* counts exclude assembly. All counts exclude OEM filters, Hilbert/interpolation processing, DMA, peripheral waits and preemption.",
                       "New RX configuration count stubs OEM constructor; add changed constructor instructions minus one stub instruction.",
                       "An executed instruction can take multiple cycles. Flash wait states, FPU latency and memory stalls are not modeled.",
                       "These synthetic signals do not establish worst-case timing or an ISR deadline."]}


def floats_at(image, address, count):
    return struct.unpack(f"<{count}f", image[address - BASE:address - BASE + count * 4])


def fir_evidence(oem, r13):
    hilbert_new = oem[0x1E130:0x1E130 + 404]
    hilbert_old = r13[0x1DA4C:0x1DA4C + 404]
    assert hilbert_new == hilbert_old
    values = struct.unpack("<101f", hilbert_new)
    responses = {}
    for hz in (500, 1000, 2500):
        w = 2 * math.pi * hz / FS
        # CMSIS stores FIR coefficients in time-reversed order.
        h = sum(value * cmath.exp(-1j * w * (100 - n)) for n, value in enumerate(values))
        relative = h / cmath.exp(-1j * w * 50)
        responses[hz] = {"relative_to_50_sample_delay_real": relative.real,
                         "relative_to_50_sample_delay_imaginary": relative.imag}
    interpolation = {}
    for name, image, address in (("v119", oem, 0x0803E3E4), ("r13", r13, 0x0803D82C)):
        coeff = floats_at(image, address, 64)
        interpolation[name] = {"coefficients_address": hex(address), "taps": 64,
                               "interpolation_factor": 8, "phase_length": 8,
                               "coefficient_sha256": sha(struct.pack("<64f", *coeff)),
                               "coefficient_sum": sum(coeff),
                               "magnitude_at_100kHz_output_rate": {
                                   hz: abs(sum(value * cmath.exp(-2j * math.pi * hz * n / 100000)
                                               for n, value in enumerate(coeff)))
                                   for hz in (0, 300, 1000, 2000, 3000, 4000, 6000)},
                               "dc_phase_gains_including_preceding_times_eight": [8 * sum(coeff[p::8]) for p in range(8)]}
    return {"hilbert": {"identical_101_float_coefficients": True, "sha256": sha(hilbert_new),
                        "v119_address": "0x0803e130", "r13_address": "0x0803da4c",
                        "responses": responses,
                        "mode0_LSB": "I=delayed cosine, Q=Hilbert cosine=-sine: negative complex frequency",
                        "mode2_USB": "I=Hilbert cosine=-sine, Q=delayed cosine: positive complex frequency",
                        "static_path_evidence": {"v119_LSB": "0x080280be..0x08028138", "v119_USB": "0x080268ee..0x08026968",
                                                 "r13_LSB": "0x08027504..0x0802757c", "r13_USB": "0x08027ff2..0x08028056"}},
            "interpolation": interpolation,
            "v119_interpolator_initialization": {"call_site": "0x080219ca..0x080219da", "wrapper": "0x080238dc",
                                                 "descriptors": ["0x20007490", "0x200075dc"], "process": "0x08035364",
                                                 "block_size": 1,
                                                 "evidence": "r1=0x0803e3e4,r0=0x20007490; wrapper copies64coeff and calls init0x0803532c with L8,numTaps64,blockSize1 for both channels"}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("oem_bin", type=Path)
    p.add_argument("r13_bin", type=Path)
    p.add_argument("--elf", type=Path, default=Path("build/tx-port.elf"))
    p.add_argument("--samples", type=int, default=1000)
    p.add_argument("--output", type=Path)
    p.add_argument("--baseline-elf", type=Path,
                   help="rerun identical inputs against a preserved pre-change payload")
    args = p.parse_args()
    assert args.samples > 128
    oem, r13 = args.oem_bin.read_bytes(), args.r13_bin.read_bytes()
    validate_oem(oem)
    assert sha(r13) == R13_SHA256, "not the audited upstream r13 image"
    result = {"status": "offline evidence only; not ready to flash",
              "inputs": {"v119_sha256": sha(oem), "r13_sha256": sha(r13), "payload_elf_sha256": sha(args.elf.read_bytes()),
                         "script_sha256": sha(Path(__file__).read_bytes())},
              "filter_scaling": {"v119": filters(oem, False), "r13": filters(r13, True),
                                 "conclusion": "Both full cascades have nearly unity passband gain. r13 absorbed normalization into coefficients; external stage gain fields alone do not measure audio scale.",
                                 "hook_difference": "New compressor is before first TX BPF at0x080247ca; r13 after second at0x0802539a. Passband normalization supports keeping nominal-57dB threshold; out-of-band compressor response and transient behavior are not equivalent.",
                                 "compressor_dynamics_difference": "r13 ring-buffer code uses capacity60, then immediately removes oldest after filling: effective59-sample delay and59-sample square sum divided by60. New core uses60-sample delay/window, updates detectors during initial fill and rebases square sum every60 samples. The optimized source uses bounded binary32 logarithm/gain approximations; the original baseline used libm. This is not a bit-identical compressor port.",
                                 "rf_limit": "Both stock sideband paths multiply audio by10 and IQ by8 before interpolation. New port retains OEM post-interpolation gain/ALC; r13 tx_amp adds sqrt(power/10)*db2lin(codec_gain+band_gain). Absolute RF calibration and ALC interaction remain unvalidated."},
              "fir_paths": fir_evidence(oem, r13),
              "timing": timing(args.elf, r13, args.samples),
              "configuration_timing": configuration_timing(args.elf, oem),
              "clock": {"v119_PLLCFGR_write": "0x0803157a", "r13_PLLCFGR_write": "0x08031762",
                        "same_startup_value": "0x04403c0c", "source": "HSE", "M": 12, "N": 240, "P": 2, "Q": 4,
                        "startup_SYSCLK_formula": "f_HSE*10", "startup_AHB_prescaler": 1,
                        "software_HSE_VALUE_hz": 25000000,
                        "limitation": "Software HSE_VALUE does not establish the board crystal or physical clock. Later clock overrides are not exhaustively excluded. No actual MHz, CPU utilization or cycle deadline is claimed."}}
    if args.baseline_elf:
        baseline = timing(args.baseline_elf, r13, args.samples)
        assert baseline["inputs"] == result["timing"]["inputs"]
        current = result["timing"]["after_first_128_samples"]
        before = baseline["after_first_128_samples"]
        result["timing_before_after"] = {
            "baseline_payload_elf_sha256": sha(args.baseline_elf.read_bytes()),
            "units": "executed ARM instructions, not cycles",
            "same_inputs_and_settings": True,
            "baseline": baseline,
            "baseline_configuration_timing": configuration_timing(args.baseline_elf, oem),
            "warm_mean_change_percent": {
                name: (current[name]["mean"] / before[name]["mean"] - 1) * 100
                for name in current if before[name]["mean"] != 0}}
    output = json.dumps(result, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
        print(f"Wrote {args.output}; executed-instruction counts only, not cycle timing")
    else:
        print(output, end="")


if __name__ == "__main__":
    main()
