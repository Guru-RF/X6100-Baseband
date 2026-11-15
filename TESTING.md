# X6100 V1.1.9 encrypted test release

**v0.1.0-alpha.2 is TEST ONLY.** The `.xgf` is a real encrypted BASE update image
for the X6100, not the X6200. It has passed offline image and DSP checks; no
radio has been flashed or measured as part of this release. Actual interrupt
timing, RF overshoot/power, memory ownership and update/rollback still need
bench validation. Encryption is the OEM transport format, not a safety approval
or a cryptographic signature.

## Files and identity

- Firmware: `X6100_BBFW_V1.1.9_251127001_tx1_alpha2_TEST.xgf`.
- BASE version remains `V1.1.9`; build text is `Nov 27 2025,tx1a2-test`.
- `SHA256SUMS` covers the distributed files. `image-manifest.json` records the
  stock/payload/output hashes, allowed edits, memory extent and roundtrip checks.
- Matching source, build scripts, GPL text, NOTICE, newlib notices and validation
  results accompany the image. Read NOTICE for upstream permission and credits.

This release adds the CESSB, compressor and TX filter path. It retains OEM power
scaling, calibration, ALC, telemetry and codec code. It does not implement r13's
band-output calibration, FM emphasis, battery curve or other unrelated features.

## GUI compatibility and activation

Use a companion OpenX6X00 build with explicit `V1.1.9` / `tx1a2-test` support.
An older GUI will not activate the added processing. Seeing `V1.1.9` alone is
insufficient; do not enable the full r13 feature set for this image.

The test payload needs no debugger. While receiving, the GUI publishes every
configuration as follows, using unsigned 32-bit register words:

1. Write register 36 with its upper halfword clear, removing the opt-in token.
2. Write complete register 25 (compressor) and register 38 (TX filter) values.
3. Write register 36 last: `0x54580000 | cessb_low16`.

Register 25 bit 4 is **bypass**, as in r13. Ratio is low four bits plus two;
threshold/makeup offsets are signed six-bit half-dB fields starting at bits 5
and 11. Register 36 bit 8 enables CESSB; bits 9–14 encode boost in 0.1 dB steps.
Register 38 packs low/high filter edges in its low/high 16-bit halves in Hz;
zero means 300–3000 Hz. Start with compressor bypass set, CESSB off and the
300–3000 Hz filter. `src/tx_protocol.c` defines the complete parameter mapping.

The adapter requires stable RX snapshots before applying settings. It will not
first arm when its block-entry snapshot says TX, and that callback then skips
configuration updates. Removing the token disables the added sample processing
at the next callback; a block that observes the token clear restores the default
TX filter once receiving. This is a guarded test
protocol, not a completed atomic commit/ack protocol. Do not change settings
during TX. Only voice LSB/USB gets added compression/CESSB/interpolation; the
OEM TX bandpass is shared with other modes and needs separate validation.

Stock V1.1.9 parses compressor bit 4 with the opposite meaning. Static tracing
has not found a downstream user of those OEM compressor fields, so the test
image does not patch an assumed OEM compressor routine. Interaction still needs
measurement on the radio.

### Full-register refresh and PTT limits

An identical refresh of already-applied registers 0–55 is a configuration no-op,
including register 36 arriving before 38: it does not rebuild the filter or reset
private histories. Keep the token when compressor and CESSB are off if the GUI
is managing the TX filter. Private interpolation requires applied CESSB enable
and voice LSB/USB TX as well as the token; arming alone does not select it.

That guarantee does not cover a restarted/desynchronized BASE or a partial
transfer. A modeled pause after armed register 36 but before 38 can apply an
old/default filter before the intended filter arrives. Recovery requires the
explicit disarm/configure/publish sequence rather than assuming the cached
full-register block is already present on the BASE.

The alpha.2 image also has known timing limits when a settings write overlaps
PTT. Clearing and republishing the token between block callbacks can bypass and
resume the old DSP histories without a reset. Clearing it between the two I/Q
selectors can select different interpolation paths for that pair. A TX-state
transition injected after the initial RX snapshot does not cancel an in-flight
configuration update. These are reproduced code-level interleavings, not
measurements of the radio's interrupt schedule or RF output. GUI deferral based
on received TX packets does not close that window. Do not change DSP settings
while keying; coordinated TX/configuration commit and coherent I/Q switching
remain required before a stable release.

`tools/verify_gui_protocol.py --elf build/tx-test.elf` reproduces the refresh,
selection, PTT-boundary and recovery checks on the released payload. It models
decoded command-memory writes and callbacks, not the physical I2C ISR.

## Install and return to stock

Use the established X6100 stock-GUI workflow described by
[gdyuldin's installer documentation](https://github.com/gdyuldin/x6100_base_patch#option-b--manual-install-winscp--filezilla--scp).
The OpenX6X00 X6200 updater/catalog is not an X6100 installer.

1. Keep a known-good original BASE `.xgf`, your calibration/settings backup and
   access to the stock GUI available before testing. This release does not
   supply calibration data or a replacement bootloader.
2. Download the test `.xgf` and `SHA256SUMS`; verify the image checksum before
   copying it. For example: `sha256sum --ignore-missing -c SHA256SUMS`.
3. Boot the stock GUI without the OpenX6X00 SD card, connect external power, and
   copy the image to `/usr/firmware` using SCP/SFTP. Select this exact TEST file
   under System settings → Firmware upgrade. Keep power connected until the
   update finishes; do not interrupt it.
4. Reboot and check both BASE version and the `tx1a2-test` build identity.
   Then boot the companion OpenX6X00 build with the added controls initially off.
5. To return to stock, select your known-good original X6100 BASE `.xgf` through
   the stock-GUI update workflow and verify the original identity/settings.

The normal return-to-stock procedure and interrupted-update recovery have not
been validated for this image. An image that fails to boot may need a separate
recovery path; the file's bounded size does not prove bootloader erase behavior.

## Measurements to publish

Use a dummy load and suitable power/spectrum measurement equipment for initial
TX tests. Record the GUI build, BASE identity, radio hardware/version, supply,
band, power setting, mode, microphone/input level and DSP settings with results.

1. Establish stock and test-image bypass baselines: RX, telemetry/external
   voltage, power at low/normal settings and first TX after repeated band changes.
2. Enable the TX filter, compressor and CESSB separately, then together. Check
   both sidebands, RX return, changing modes and repeated PTT transitions.
3. Compare startup and steady-state envelope peaks, ALC, occupied spectrum and
   out-of-band energy against stock and r13 with matched stimulus/settings.
   Previous digital sweeps observed up to **+4.404 dB residual overshoot** after
   interpolation. Matching r13 does not eliminate overshoot or guarantee RF power.
4. Measure complete ISR execution time and nested-interrupt stack high-water,
   including a filter change, and verify CCM ownership on the target. Optional
   DWT callback profiling excludes the complete ISR and wrapper time.
5. Check settings/calibration preservation after a normal update and return to
   stock. Recovery and interrupted-update work require a proven recovery setup.

Publish findings with the source/release so the permission condition recorded
in NOTICE is met. Distinguish observed measurements from inferred behavior.
