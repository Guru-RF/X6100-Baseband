# X6100 V1.1.9 TX DSP port

CESSB, adjustable TX filtering and compressor controls for **BASE V1.1.9
(251127001)**. This builds an **analysis ELF**, not an installable firmware image.
Hardware timing, RF behavior and update/rollback validation remain outstanding.

## Implementation

- CESSB using a 65-tap filtered clipping correction and a 32-sample delay.
- Compressor/gate with ratio, threshold and makeup controls.
- Adjustable OEM TX bandpass, applied during RX at a DMA block boundary.
- Private TX interpolation coefficients matching r13, with independent histories;
  stock RX descriptors and coefficients remain untouched.
- Five guarded assembly hooks, a private runtime without a heap, and optional
  DWT cycle profiling.

The adapter requires a debugger-only laboratory interlock before processing or
filter changes can occur. GUI/I2C commands cannot arm it. Added compression,
CESSB and interpolation correction run only in voice LSB/USB TX. The OEM TX
bandpass is shared with other modulation modes, so its settings need broader
mode validation. Configuration is frozen during TX; PTT/mode changes reset
private histories.

The port retains OEM power scaling, ALC, calibration, telemetry and codec setup.
It does not transplant the older V1.1.6 hardware-control code.

## Build and test

Requirements: a C11 host compiler, Python 3, and GNU `arm-none-eabi-gcc` with
binutils and newlib/libm for Cortex-M4 hard float. ARM verification additionally
uses the packages in [tests/requirements.txt](tests/requirements.txt).

```sh
make test
make payload
# For an isolated toolchain:
make payload CROSS=/path/to/toolchain/bin/arm-none-eabi-

# Install optional Python dependencies in your own virtual environment:
/path/to/venv/bin/python -m pip install -r tests/requirements.txt
make test test-hooks test-adapter test-runtime PYTHON=/path/to/venv/bin/python
```

Pass `CROSS=/path/to/toolchain/bin/arm-none-eabi-` to the test targets too if the
compiler is not on PATH. Outputs belong in ignored `build/`; toolchains,
reference firmware and Python environments are external dependencies.

The payload reserves `0x08080000..0x080a0000` for flash and
`0x10000000..0x10004000` for CCM. The validated build uses 11,308 flash bytes and
1,736 state bytes. Linker bounds are enforced; whole-program CCM ownership still
requires target validation.

## Firmware verification

Supply your own exact plaintext images. The V1.1.9 image must be 392,048 bytes
with SHA-256 `3fa11293e06951c21c9cfda04ea08adad85eaadda26b77a8e8bbcca8820c53c4`.
The comparison tools also require the original V1.1.6 r13 image and verify its
hash. Vendor firmware, encryption keys and generated reports are not included.

```sh
mkdir -p build/reports
python tools/plan_patch.py /path/to/V1.1.9.bin build/tx-port.elf \
    > build/reports/patch-plan.json
python tools/verify_hooks.py --payload-elf build/tx-port.elf \
    --oem-bin /path/to/V1.1.9.bin
python tools/verify_tx_interpolation.py /path/to/V1.1.9.bin /path/to/r13.bin \
    --output build/reports/interpolation.json
python tools/compare_cessb.py /path/to/r13.bin --v119-bin /path/to/V1.1.9.bin \
    --output build/reports/cessb.json
python tools/analyze_tx_chain.py /path/to/V1.1.9.bin /path/to/r13.bin \
    --output build/reports/tx-chain.json
python tools/audit_memory.py /path/to/V1.1.9.bin /path/to/r13.bin \
    > build/reports/memory.json
```

Use the Python environment containing the optional packages. The patch planner
checks the image hash, original instructions, ELF architecture and memory bounds.
It emits a JSON review plan, **never patched firmware**. Tests execute actual ARM
payload and OEM routines under Unicorn; this is not a cycle-accurate radio model.
[firmware/v119-evidence.json](firmware/v119-evidence.json) records the address map,
command encoding, provenance, validation summary and unresolved requirements.

For future optimization comparisons, retain a previous ELF outside this repo and
pass `--baseline-elf /path/to/previous.elf` to `tools/analyze_tx_chain.py` and
`tools/verify_dsp_equivalence.py`. The latter checks exact configuration, output
and DSP-state equivalence. Generated reports should stay under `build/reports/`.

## Current validation

- Host DSP/protocol tests, independent filter/reference checks, and ARM adapter,
  runtime and register-preservation tests pass.
- 72 configurations and 27,648 CESSB samples/full-state snapshots match the
  previous validated implementation bit-for-bit.
- Actual OEM interpolation and DMA packing pass; tested RX output/state remain
  unchanged. Private interpolation output matches r13 bit-for-bit.
- All 108 r13 overshoot comparisons pass. For the 300–3000 Hz, 3.7 dB boost
  two-tone case, normalized post-interpolation startup overshoot is **+2.632 dB**,
  matching r13. The finite sweep maximum is **+4.404 dB**. This algorithm still
  has residual overshoot; these are digital measurements, not RF power bounds.

| Measured operation | Executed ARM instructions |
| --- | ---: |
| Compressor core, warm mean per sample | 215 |
| CESSB core, warm mean per sample | 540 |
| Added sample path including wrappers, warm mean | 928 |
| TX filter-edge change callback | 7,700 |

These synthetic counts exclude the unchanged OEM signal-processing path and do
not establish CPU utilization or a real-time deadline. The wrappers use 96-byte
save frames and rely on the compiled callees' AAPCS32 preservation of `r4-r11`
and `s16-s31`. Actual callback checks and deliberate ABI-violation tests cover
this contract; introducing a naked/non-AAPCS callback requires another audit.

## Target timing and remaining work

```sh
python tools/build_cycle_profile.py --cross arm-none-eabi-
python tools/verify_cycle_profile.py
```

The optional profile build remains an analysis ELF. DWT counters record calls,
last/minimum/maximum cycles and 64-bit totals for block, audio, I/Q, selector and
configuration/reset paths. Halt the target to read a coherent snapshot. Event
categories overlap and measure the whole callback; do not sum them. See
[firmware/cycle_profile.h](firmware/cycle_profile.h) and the generated measurement
guide in `build/cycle-profile/` for layout and limits.

Before an installable build:

1. Measure complete ISR timing, nested-interrupt stack use and CCM ownership on
   target. Callback profiling excludes assembly wrappers and the complete ISR.
   Full ISR coverage needs entry `0x08024528` and both observed exits
   `0x0802454c` and `0x08027cc2`.
2. Validate absolute RF power, ALC, spectrum, mic audio, bypass modes, RX recovery
   and repeated first transmissions after band changes. Matching r13 does not
   eliminate its overshoot.
3. Finish atomic I2C configuration publication and the command-collision audit.
   Stable snapshots currently reduce tearing but are not a commit/ack protocol.
4. Integrate explicit GUI capability detection in OpenX6X00. Do not advertise an
   r12/r13 revision or enable features absent from this payload.
5. Verify bootloader erase boundaries, calibration preservation and rollback,
   including interrupted updates. The bootloader implementation is unavailable.

## Provenance

The DSP derives from [gdyuldin/x6100_base_patch r13](https://github.com/gdyuldin/x6100_base_patch/tree/r13),
commit `644ac675a1765ce69755870767b4cb00a3e6d7eb`. The port uses a conjugate return
mixer instead of r13's fixed 90-degree rotation, a recursive oscillator, explicit
state ownership and optimized FIR traversal. Upstream had no root license
statement in the inspected revision; this repository asserts no redistribution
license for the derived DSP code.
