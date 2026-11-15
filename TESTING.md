# X6100 V1.1.9 encrypted test release

**v0.1.0-alpha.2 is TEST ONLY.** The `.xgf` is a real encrypted BASE update image
for the X6100, not the X6200. It has passed offline image and DSP checks and
limited tests on one radio on 15 November 2026, including a normal update and
return to stock using an experimental OpenX6X00 writer. The results below use
the radio's own meter. Actual interrupt timing, RF envelope/spectrum, absolute
power, memory ownership and interrupted-update recovery remain unvalidated.
Encryption is the OEM transport format, not a safety approval or a cryptographic
signature.

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

## Bench results — 15 November 2026

The OpenX6X00 bench session supplied an operator report and per-key telemetry
logs. The unchanged report and raw runs are published in
[bench-2026-11-15.tar.gz](https://github.com/Guru-RF/X6100-Baseband/releases/download/v0.1.0-alpha.2/bench-2026-11-15.tar.gz),
with [bench-2026-11-15-SHA256SUMS](https://github.com/Guru-RF/X6100-Baseband/releases/download/v0.1.0-alpha.2/bench-2026-11-15-SHA256SUMS).
The archive's README records qualifications found when checking the summary
against the raw runs. This section uses those narrower claims.

### Setup and update observations

- One X6100, 13.6 V external supply with battery fitted, dummy load, ATU on,
  indicated SWR 1.00–1.10. GUI: OpenX6X00 `gh78-flash`, commit `bd733aad`;
  reported BASE identity: `V1.1.9 Nov 27 2025,tx1a2-test`.
- The operator reports successful installation of alpha.2 and return to Xiegu
  V1.1.8 using OpenX6X00's own `tools/base_update` X6100 path, XMODEM-1K at
  1.5 Mbaud: 396 and 383 packets respectively, both exit 0. The stock GUI was
  not used. The radio was back on V1.1.8 at the end of the session.
- The operator reports the **816-byte calibration window at I2C `0xF000`**
  identical before, during and after the test, also matching the 24 October
  backup. This is evidence for that window and that normal update cycle,
  not a complete nonvolatile-memory or interrupted-update check. Version
  readbacks, writer logs and calibration bytes are not in the per-key logs;
  the report supplies these observations and only abbreviated image/backup
  hashes. They were not independently re-read for this documentation update.
- Stimulus: TCI TX audio, signed 16-bit mono at 24 kHz; single/two tones, plus
  FM carrier. Normally 5 W set, three-second keys with last-second means;
  short PTT runs used one-second keys separated by 0.7 seconds. Controls were
  changed **only in RX**. Compressor tests used 4:1, threshold/makeup 0.0 dB;
  CESSB boost was 3.7 dB.
- Measurements are the radio's forward-power, SWR and displayed ALC telemetry,
  approximately one reading per 200 ms. There was **no external power meter,
  oscilloscope or spectrum analyser**. ALC figures below are the displayed
  values, without an assumed calibrated gain-reduction scale.

### Observed response

All power figures in this table are **indicated watts**, not independently
measured RF power. USB tests used 14.2 MHz and LSB tests 7.1 MHz.

| Check | Observation | Scope |
| --- | --- | --- |
| FM bypass, 5 / 10 W set, 7.1 / 14.2 / 21.2 / 28.5 MHz | Alpha.2: 4.90 / 10.88 W; stock V1.1.8: 4.90 / 10.88–10.90 W | Close agreement on the same radio's meter; not an absolute power calibration. |
| In-band 1 kHz bypass | USB ALC within 0.05 of stock over the tested levels; at −40 dBFS, 2.92 W versus stock 2.96 W | Does not establish identical frequency response: at 250 Hz, stock reads 1.28 W versus bypass 0.50–0.76 W. |
| TX filter, 400–2500 Hz, −10 dBFS tone | At 300 / 2800 Hz: USB 0.62 / 0.06 W, LSB 0.70 / 0.12 W | Response follows the filter setting on both sidebands; not a measured spectral mask. |
| Compressor 4:1 with 400–2500 Hz filter | At 1 kHz, −40 dBFS USB: 4.12 W versus bypass 2.92 W; displayed ALC also changes at higher levels | Demonstrates operation; compression ratio, threshold and makeup calibration remain unmeasured. |
| CESSB with 400–2500 Hz filter, compressor off | At 1 kHz USB, −6 through −30 dBFS: ALC 5.70–5.78 and about 4.9 W | CESSB alone with the default filter spans ALC 5.78–6.10. Neither is a direct envelope measurement. |
| Above the selected filter edge | At 2600 / 2800 Hz: filter alone 0.26 / 0.06 W; with CESSB 0.76 / 0.50 W. Compressor plus CESSB reads 0.86 W at 2800 Hz | A reason to inspect the spectrum; the meter cannot separate the intended tone from unwanted products. |
| Selected maxima at 5 W set | CESSB 7.6 W in a one-second PTT run; stock 6.4 W on two-tone and 6.9 W at 3500 Hz; alpha.2 bypass 6.9 W on two-tone | Different stimuli and sparse telemetry: these neither establish nor exclude CESSB RF overshoot. |

The early logs' `max W` is the last-second maximum. Later logs also have a
`peak` column for the highest telemetry sample over the whole key. Neither
provides a time-resolved RF envelope; do not turn the selected maxima into an
overshoot percentage or a bound.

Successful short-PTT sequences returned to RX, and returning the controls to
compressor off, CESSB off and 300–3000 Hz restored the earlier bypass readings.
Some longer TCI-audio starts were refused on both stock and alpha.2; their
cause remains an OpenX6X00-side investigation. These runs do not close the
first-transmission-after-band-change issue or the settings/PTT race below.
The report also records a post-update version-read problem and a GUI retry fix
in `bd733aad`; this is not validation of every GUI boot/recovery path.

This was a **V1.1.8 comparison**, not a stock V1.1.9 or r13 RF comparison. No
ISR timing, stack high-water, CCM ownership, occupied spectrum, out-of-band
energy or RF envelope measurements were made. The release remains TEST ONLY.

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

The observed update-and-return cycle above used the experimental X6100 writer
in OpenX6X00 `gh78-flash` / `bd733aad`. It does not establish support in a
published stable GUI or in the X6200 firmware catalog.

The alternative stock-GUI workflow below comes from
[gdyuldin's installer documentation](https://github.com/gdyuldin/x6100_base_patch#option-b--manual-install-winscp--filezilla--scp).
It was **not exercised with alpha.2 in this bench session**.

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

One normal return to V1.1.8 with the experimental OpenX6X00 writer is reported
above. Stock-GUI installation/return and interrupted-update recovery remain
untested for this image. An image that fails to boot may need a separate
recovery path; the file's bounded size does not prove bootloader erase behavior.

## Measurements to publish

Use a dummy load and suitable power/spectrum measurement equipment for initial
TX tests. Record the GUI build, BASE identity, radio hardware/version, supply,
band, power setting, mode, microphone/input level and DSP settings with results.

The next priorities, informed by the 15 November results, are:

1. **RF envelope and first-TX behavior.** Compare stock **V1.1.9** with alpha.2
   bypass, compressor, CESSB and both enabled under matched input/settings.
   Use a suitably rated coupler/attenuator and a scope with RF/envelope capture
   or a peak-power instrument with enough time resolution. Trigger on PTT,
   save the first milliseconds and the settled envelope, and repeat after
   band changes as well as repeated keys. Include 1 kHz and 700 + 1900 Hz
   two-tone stimuli at low and 5 W settings. Record capture bandwidth, sample
   rate, attenuation, actual supply voltage and peak/settled RF power. Start
   with a known 50-ohm load and ATU off, recording that change from this session.
   The radio meter cannot settle the overshoot question. Previous digital
   sweeps observed up to **+4.404 dB residual overshoot** after interpolation;
   matching r13 does not eliminate it or establish an RF power bound.
2. **Spectrum at the filter edge, then two-tone IMD.** With 400–2500 Hz selected,
   inspect 2500, 2600 and 2800 Hz stimuli with CESSB off/on, then 700 + 1900 Hz
   two-tone and speech. Separate the wanted tone(s) from unwanted sideband,
   intermodulation and out-of-band products. Keep a matched-input comparison
   and a separate comparison at matched externally measured output power;
   log analyser attenuation, RBW/VBW, detector and level reference. Verify the
   analyser is not generating the distortion. Recheck the 250 Hz bypass
   difference against stock V1.1.9 as well. A later r13 comparison can use the
   same method; none has yet been measured at RF here.
3. **Processing time and memory on target.** Measure the complete ISR's worst
   execution time and nested-interrupt stack high-water with CESSB/compressor
   active, including RX filter changes, PTT/mode transitions and other interrupt
   load. Verify CCM ownership. Optional DWT callback profiling excludes the
   complete ISR and wrapper time; instruction counts are not timing results.
4. **Broader controls and update coverage.** Extend power/band/mode, microphone,
   RX return and external-voltage checks, plus exact GUI detection and recovery.
   Retain full image identities and before/after calibration comparisons for
   further normal updates. Interrupted-update work needs a proven recovery
   setup; RX-only settings tests do not validate atomic changes at PTT onset.

Measurement-method references: Keysight's
[triggered RF-envelope measurements](https://www.keysight.com/us/en/use-cases/characterize-pulsed-rf-signal-power.html)
and Rohde & Schwarz's
[two-tone intermodulation measurements](https://www.rohde-schwarz.com/us/applications/intermodulation-distortion-measurements-on-modern-spectrum-analyzers_56280-15423.html).

Publish findings with the source/release so the permission condition recorded
in NOTICE is met. Distinguish observed measurements from inferred behavior.
