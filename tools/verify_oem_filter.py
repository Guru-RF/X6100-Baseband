#!/usr/bin/env python3
"""Run the exact OEM TX-filter constructor offline and inspect its response.

Requires unicorn. Takes only the plaintext V1.1.9 251127001 image accepted by
plan_patch.validate_oem. Executes OEM code in emulated RAM; never writes an image
or accesses hardware. Checks stock 300–3000 Hz and adjusted 300–2400 Hz filters.
This establishes relative DSP scale and adjustable table behavior, not RF power,
clock accuracy, interrupt timing, or suitability for installation.
"""
import argparse
import cmath
import json
import math
from pathlib import Path
import struct

from plan_patch import BASE, validate_oem

try:
    from unicorn import Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS, UC_PROT_READ, UC_PROT_EXEC
    from unicorn import arm_const as arm
except ImportError as error:
    raise SystemExit("Optional test dependency: pip install unicorn") from error

RATE = 12500.0
INSTANCES = (0x20007728, 0x200077F4)
FREQUENCIES = (100, 500, 1000, 2000, 2700, 3000, 4000)


def construct(image, low, high):
    cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
    cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
    cpu.mem_map(0x08000000, 0x100000)
    cpu.mem_map(0x20000000, 0x40000)
    cpu.mem_map(0xE000E000, 0x1000)
    cpu.mem_write(BASE, image)
    cpu.mem_protect(0x08000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
    # Reset-handler literal table identifies initialized RAM [0,0x3570),
    # loaded from flash 0x0807c5d0. Zero-filled emulated RAM supplies BSS.
    cpu.mem_write(0x20000000, image[0x5C5D0:0x5C5D0 + 0x3570])
    cpu.mem_write(0xE000ED88, struct.pack("<I", 0xF00000))
    cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xF00000)
    cpu.reg_write(arm.UC_ARM_REG_SP, 0x20028000)

    def call(address):
        cpu.reg_write(arm.UC_ARM_REG_LR, 0x08000101)
        cpu.emu_start(address | 1, 0x08000100, count=5_000_000)
        if cpu.reg_read(arm.UC_ARM_REG_PC) != 0x08000100:
            raise RuntimeError("OEM constructor exceeded instruction limit")

    cpu.reg_write(arm.UC_ARM_REG_R0, INSTANCES[0])
    call(0x08023668)  # Initialize the two linked filter instances.
    cpu.reg_write(arm.UC_ARM_REG_R0, INSTANCES[0])
    cpu.reg_write(arm.UC_ARM_REG_R1, 1)
    for register, value in zip((arm.UC_ARM_REG_S0, arm.UC_ARM_REG_S1, arm.UC_ARM_REG_S2),
                               (RATE, float(low), float(high))):
        cpu.reg_write(register, struct.unpack("<I", struct.pack("<f", value))[0])
    call(0x0802397C)
    cascades = []
    for address in INSTANCES:
        stages, state, coefficients = struct.unpack("<3I", cpu.mem_read(address, 12))
        assert stages == 5 and coefficients == address + 16 and state == address + 116
        values = struct.unpack("<25f", cpu.mem_read(coefficients, 100))
        gain = struct.unpack("<f", cpu.mem_read(address + 12, 4))[0]
        assert all(math.isfinite(v) for v in values) and math.isfinite(gain) and gain > 0
        cascades.append((gain, values))
    return cascades


def response(cascades, frequency):
    z = cmath.exp(-2j * math.pi * frequency / RATE)
    total = 1
    for gain, coefficients in cascades:
        total *= gain
        for stage in range(5):
            b0, b1, b2, a1, a2 = coefficients[stage * 5:stage * 5 + 5]
            total *= (b0 + b1 * z + b2 * z * z) / (1 - a1 * z - a2 * z * z)
    magnitude = abs(total)
    assert math.isfinite(magnitude) and magnitude > 0
    return magnitude


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("oem_bin", type=Path)
    args = parser.parse_args()
    image = args.oem_bin.read_bytes()
    validate_oem(image)
    results = []
    for low, high in ((300, 3000), (300, 2400)):
        cascades = construct(image, low, high)
        magnitudes = {hz: response(cascades, hz) for hz in FREQUENCIES}
        assert 0.9 < magnitudes[1000] < 1.1, "passband normalization changed"
        assert magnitudes[100] < 0.01 and magnitudes[4000] < 0.01
        results.append({"low_hz": low, "high_hz": high,
                        "gains": [c[0] for c in cascades],
                        "magnitude": magnitudes,
                        "response_db": {hz: 20 * math.log10(v) for hz, v in magnitudes.items()}})
    assert results[1]["magnitude"][2700] < results[0]["magnitude"][2700] / 10
    print(json.dumps({"status": "PASS: finite coefficients, normalized passband, adjustable cutoff",
                      "scope": "OEM DSP constructor only; no firmware output or hardware validation",
                      "filters": results}, indent=2))


if __name__ == "__main__":
    main()
