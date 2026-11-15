# X6100 V1.1.9 TX DSP port

CESSB, adjustable TX filtering and compressor controls for **BASE V1.1.9
(251127001)**. [v0.1.0-alpha.2](https://github.com/Guru-RF/X6100-Baseband/releases/tag/v0.1.0-alpha.2)
provides an **encrypted XGF for experimental on-radio testing**, with source,
findings and checksums. **TEST ONLY: hardware timing, RF behavior and
update/rollback remain unvalidated.** Read [TESTING.md](TESTING.md) before use.

The separate `make payload` target still builds an analysis ELF with a
debugger-only interlock. Only the explicit `make test-payload` build is accepted
by the encrypted image builder. The earlier alpha.1 remains a source-only preview.

## Implementation

- CESSB using a 65-tap filtered clipping correction and a 32-sample delay.
- Compressor/gate with ratio, threshold and makeup controls.
- Adjustable OEM TX bandpass, applied during RX at a DMA block boundary.
- Private TX interpolation coefficients matching r13, with independent histories;
  stock RX descriptors and coefficients remain untouched.
- Five guarded assembly hooks, a private runtime without a heap, and optional
  DWT cycle profiling.

The test image reports build identity `Nov 27 2025,tx1a2-test`. A compatible
OpenX6X00 build must explicitly opt in using the protocol in [TESTING.md](TESTING.md).
Stock and legacy GUI commands leave the added processing bypassed. Initial GUI
settings must leave compressor and CESSB off, with a 300–3000 Hz TX filter.
Added compression, CESSB and interpolation correction run only in voice LSB/USB TX. The OEM TX
bandpass is shared with other modulation modes, so its settings need broader
mode validation. Configuration is frozen during TX; PTT/mode changes reset
private histories.

The port retains OEM power scaling, ALC, calibration, telemetry and codec setup.
It does not transplant the older V1.1.6 hardware-control code.

## Build and test

Requirements: a C11 host compiler, Python 3, and GNU `arm-none-eabi-gcc` with
binutils and newlib/libm for Cortex-M4 hard float. Packaging tests require
PyCryptodome or cryptography; ARM verification uses the other packages in
[tests/requirements.txt](tests/requirements.txt).

```sh
make test
make payload
make test-payload
# For an isolated toolchain:
make payload CROSS=/path/to/toolchain/bin/arm-none-eabi-

# Install optional Python dependencies in your own virtual environment:
/path/to/venv/bin/python -m pip install -r tests/requirements.txt
make test test-hooks test-adapter test-runtime PYTHON=/path/to/venv/bin/python
make test-adapter-test PYTHON=/path/to/venv/bin/python
```

Pass `CROSS=/path/to/toolchain/bin/arm-none-eabi-` to the test targets too if the
compiler is not on PATH. Outputs belong in ignored `build/`; toolchains,
reference firmware and Python environments are external dependencies.

The payload reserves `0x08080000..0x080a0000` for flash and
`0x10000000..0x10004000` for CCM. The analysis build uses 11,308 flash bytes and
1,736 state bytes. Linker bounds are enforced; whole-program CCM ownership still
requires target validation. The test build adds GUI opt-in and an identification
marker, using 11,376 flash bytes and the same 1,736 state bytes; its exact load
map is included in the release image manifest.

## Encrypted test image

Supply the exact stock encrypted V1.1.9 image and an external 32-byte AES key
file (or a file containing 64 hex digits). Keep the key outside the repository.
Build an explicit test payload first, then run:

```sh
mkdir -p build/release
python tools/build_test_image.py \
    --stock-xgf /path/to/X6100_BBFW_V1.1.9_251127001.xgf \
    --payload-elf build/tx-test.elf --key-file /external/xgf.key \
    --output build/release/X6100_BBFW_V1.1.9_251127001_tx1_alpha2_TEST.xgf \
    --manifest build/release/image-manifest.json
```

Output names must be new files. The builder validates both stock ciphertext and
plaintext hashes, all original hook instructions, ELF load bounds and the test
build marker. It preserves the vector table and every OEM byte except the five
hooks and the exact build-time string. It appends the payload, pads plaintext
with `0xff` to 1 KiB, and encrypts raw AES-256-ECB blocks in the XGF format used
by r13. The padding prevents an updater from adding unencrypted tail bytes.
It checks the complete encryption/decryption roundtrip and bounds both AES and
XMODEM extents below the calibration region. These checks validate the file;
they do not establish what the radio's bootloader erases or prove recovery.

## Firmware verification

Supply your own exact plaintext images. The V1.1.9 image must be 392,048 bytes
with SHA-256 `3fa11293e06951c21c9cfda04ea08adad85eaadda26b77a8e8bbcca8820c53c4`.
The comparison tools also require the original V1.1.6 r13 image and verify its
hash. Reference plaintext firmware and encryption keys are not included in the
source tree; encrypted test images and validation reports are release assets.

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

To verify the actual encrypted test image's assembled reset path:

```sh
python tools/verify_test_boot.py --stock-bin /path/to/V1.1.9.bin \
    --payload-elf build/tx-test.elf \
    --image-xgf build/release/X6100_BBFW_V1.1.9_251127001_tx1_alpha2_TEST.xgf \
    --key-file /external/xgf.key > build/reports/boot.json
```

This executes OEM startup and the payload initializer to OEM main entry,
checks preserved state, and rejects a missing startup hook. RCC/SCB are
memory-backed and the OEM FPU-enable write is mirrored into Unicorn's FPU
gate. It does not simulate the bootloader, interrupts or main's peripheral setup.

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

The alpha.2 test build checks the GUI token in every sample/selector callback.
Its warmed added path measures **952.148 instructions/sample**, about 24 above
the analysis build; a filter-edge callback measures 7,710 instructions. The
release validation reports cover the actual test ELF, including 108 r13 CESSB
comparisons, 1,577 callback ABI checks and 103,752 runtime fill cases.

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

Before a stable release; record these results during test-image evaluation:

1. Measure complete ISR timing, nested-interrupt stack use and CCM ownership on
   target. Callback profiling excludes assembly wrappers and the complete ISR.
   Full ISR coverage needs entry `0x08024528` and both observed exits
   `0x0802454c` and `0x08027cc2`.
2. Validate absolute RF power, ALC, spectrum, mic audio, bypass modes, RX recovery
   and repeated first transmissions after band changes. Matching r13 does not
   eliminate its overshoot.
3. Finish atomic I2C configuration publication and the command-collision audit.
   Stable snapshots currently reduce tearing but are not a commit/ack protocol.
4. Validate the companion OpenX6X00 test-identity detection and control sequence.
   Do not advertise an r12/r13 revision or enable features absent from this payload.
5. Verify bootloader erase boundaries, calibration preservation and rollback,
   including interrupted updates. The bootloader implementation is unavailable.

## License and provenance

Guru-RF's original contributions are licensed under **GPL-3.0-or-later** with
the attribution term in [NOTICE](NOTICE); the full GPL text is in
[LICENSE](LICENSE). Distributors of covered binaries must provide the matching
Corresponding Source under GPLv3 section 6 and retain the Guru-RF/project credit.
See NOTICE for the scope, upstream credits and distribution details.

The DSP derives from [gdyuldin/x6100_base_patch r13](https://github.com/gdyuldin/x6100_base_patch/tree/r13),
commit `644ac675a1765ce69755870767b4cb00a3e6d7eb`. The port uses a conjugate return
mixer instead of r13's fixed 90-degree rotation, a recursive oscillator, explicit
state ownership and optimized FIR traversal. The project maintainer has confirmed
gdyuldin's permission for use and distribution provided our findings are published.
That permission, its findings condition and upstream attribution are recorded in
NOTICE; our GPL grant covers our own contributions. The encrypted test image
contains the stock vendor base plus the documented changes. Source archives do
not include vendor plaintext firmware or encryption keys. Linked newlib notices
are in [LICENSES/newlib.txt](LICENSES/newlib.txt).
