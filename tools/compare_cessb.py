#!/usr/bin/env python3
"""Compare ACTUAL r13 CESSB machine code with the newly built ARM DSP core.

Requires unicorn, pyelftools, numpy. No firmware output, radio access or flash.
The caller supplies their exact decrypted r13 image. OEM CMSIS calls, trig,
clipping and FIR code execute from that image, with no function stubbing.
The new side executes tx_dsp_configure/tx_dsp_cessb from the linked ARM ELF.
With --v119-bin, actual OEM8x interpolation coefficients are also compared
through a linear FIR model checked against both actual binary kernels.

Optional reconstruction is independent 8x, 65-tap windowed-sinc interpolation,
separate from the optional OEM interpolation comparison, DAC, ALC or RF stages. Results are limited
to the CESSB I/Q stage. Both startup and settled peaks are retained.
"""
import argparse
import ctypes
import hashlib
import json
import io
from pathlib import Path
import struct
import sys

try:
    import numpy as np
    from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_MCLASS,
                         UC_PROT_READ, UC_PROT_EXEC)
    from unicorn import arm_const as arm
    from elftools.elf.elffile import ELFFile
except ImportError as error:
    raise SystemExit('Install optional dependencies: unicorn pyelftools numpy') from error

ROOT = Path(__file__).resolve().parents[1]
R13_SHA = '42815a8514831e07bce46e1ddf27376af863488af3663e53d450cfb99713591e'
RATE, THRESHOLD = 12500.0, 10 ** (1 / 20)
R13_INIT, R13_FILTER, R13_PARAMS, R13_PROCESS = 0x08083548, 0x080836d4, 0x080837e4, 0x08083868
STACK, RETURN, SAMPLE, CONFIG, STATE = 0x20028000, 0x08000100, 0x20010000, 0x20010020, 0x10002000


class Config(ctypes.Structure):
    _fields_ = [('sample_rate_hz', ctypes.c_float), ('filter_low_hz', ctypes.c_float),
                ('filter_high_hz', ctypes.c_float), ('compressor_on', ctypes.c_bool),
                ('gate_on', ctypes.c_bool), ('compressor_ratio', ctypes.c_float),
                ('threshold_db', ctypes.c_float), ('makeup_db', ctypes.c_float),
                ('cessb_on', ctypes.c_bool), ('cessb_boost_db', ctypes.c_float),
                ('cessb_threshold', ctypes.c_float)]


def bits(value):
    return struct.unpack('<I', struct.pack('<f', value))[0]


class Machine:
    def __init__(self):
        self.cpu = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_MCLASS)
        self.cpu.ctl_set_cpu_model(arm.UC_CPU_ARM_CORTEX_M4)
        for address, size in ((0x08000000, 0x100000), (0x10000000, 0x10000),
                              (0x20000000, 0x40000), (0xE000E000, 0x1000), (0x40023000, 0x1000)):
            self.cpu.mem_map(address, size)
        self.cpu.mem_write(0xE000ED88, struct.pack('<I', 0xF00000))
        self.cpu.reg_write(arm.UC_ARM_REG_C1_C0_2, 0xF00000)
        self.cpu.mem_write(0x08000200, bytes.fromhex('b0ee400a'))
        self.cpu.emu_start(0x08000201, 0x08000204)

    def call(self, address, *args):
        cpu = self.cpu
        cpu.reg_write(arm.UC_ARM_REG_SP, STACK)
        cpu.reg_write(arm.UC_ARM_REG_LR, RETURN | 1)
        for index, value in enumerate(args):
            cpu.reg_write(getattr(arm, f'UC_ARM_REG_R{index}'), value)
        cpu.emu_start(address | 1, RETURN, count=1_000_000)
        if cpu.reg_read(arm.UC_ARM_REG_PC) != RETURN or cpu.reg_read(arm.UC_ARM_REG_SP) != STACK:
            raise RuntimeError(f'Function {address:#x} did not return with balanced stack')

    def run(self, samples):
        output = np.empty(len(samples), dtype=np.complex128)
        for n, value in enumerate(samples):
            self.cpu.mem_write(SAMPLE, struct.pack('<ff', value.real, value.imag))
            self.step()
            i, q = struct.unpack('<ff', self.cpu.mem_read(SAMPLE, 8))
            output[n] = complex(i, q)
        if not np.all(np.isfinite(output)):
            raise AssertionError('non-finite CESSB output')
        return output


class Original(Machine):
    def __init__(self, image):
        super().__init__()
        if hashlib.sha256(image).hexdigest() != R13_SHA:
            raise ValueError('Expected exact plaintext V1.1.6 230307001 r13 image')
        self.cpu.mem_write(0x08020000, image)
        self.cpu.mem_protect(0x08000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
        # Sites identified through r13 tx_amp wrapper and adjacent CESSB routines.
        for address, prefix in ((R13_INIT, '2de9f041'), (R13_FILTER, '091a'),
                                (R13_PARAMS, '10b5'), (R13_PROCESS, '70b5')):
            assert image[address - 0x08020000:address - 0x08020000 + len(prefix)//2] == bytes.fromhex(prefix)

    def configure(self, low, high, boost, lsb):
        self.call(R13_INIT)
        self.call(R13_FILTER, low, high)
        self.cpu.reg_write(arm.UC_ARM_REG_S0, bits(boost))
        self.call(R13_PARAMS, 1)
        self.cpu.mem_write(0x2000a8d5, bytes([0 if lsb else 2]))

    def step(self):
        self.call(R13_PROCESS, SAMPLE, SAMPLE + 4)


class Port(Machine):
    def __init__(self, elf_bytes):
        super().__init__()
        with io.BytesIO(elf_bytes) as stream:
            elf = ELFFile(stream)
            self.symbols = {s.name: s['st_value'] for s in elf.get_section_by_name('.symtab').iter_symbols()}
            for segment in elf.iter_segments():
                if segment['p_type'] == 'PT_LOAD' and segment['p_filesz']:
                    self.cpu.mem_write(segment['p_paddr'], segment.data())
        self.cpu.mem_protect(0x08000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
        self.call(self.symbols['patch_init'])

    def configure(self, low, high, boost, lsb):
        config = Config(RATE, low, high, False, True, 2, -57, 6, True, boost, THRESHOLD)
        self.cpu.mem_write(CONFIG, bytes(config))
        self.call(self.symbols['tx_dsp_configure'], STATE, CONFIG)
        if self.cpu.reg_read(arm.UC_ARM_REG_R0) != 1:
            raise AssertionError('Port rejected test configuration')
        self.lsb = lsb

    def step(self):
        self.call(self.symbols['tx_dsp_cessb'], STATE, self.lsb, SAMPLE, SAMPLE + 4)


class Interpolator(Machine):
    """Execute OEM CMSIS interpolation for a reference vector; then use its
    validated, linear FIR equivalent for the large comparison matrix."""
    def __init__(self, image, address, coefficient_address, elf_bytes=None):
        super().__init__()
        self.cpu.mem_write(0x08020000, image)
        if elf_bytes:
            with io.BytesIO(elf_bytes) as stream:
                for segment in ELFFile(stream).iter_segments():
                    if segment['p_type'] == 'PT_LOAD' and segment['p_filesz']:
                        self.cpu.mem_write(segment['p_paddr'], segment.data())
        self.cpu.mem_protect(0x08000000, 0x100000, UC_PROT_READ | UC_PROT_EXEC)
        self.address = address
        self.coefficients = np.array(struct.unpack('<64f', self.cpu.mem_read(coefficient_address, 256)))
        assert np.all(np.isfinite(self.coefficients))
        self.cpu.mem_write(0x20002000, struct.pack('<BBHII', 8, 0, 8, coefficient_address, 0x20002100))

    def verify(self):
        source = np.random.default_rng(0x6100).normal(0, .4, 128).astype(np.float32)
        source[:16] = 0; source[0] = 1
        actual = []
        for value in source:
            self.cpu.mem_write(SAMPLE, struct.pack('<f', value))
            self.call(self.address, 0x20002000, SAMPLE, SAMPLE+16, 1)
            actual.extend(struct.unpack('<8f', self.cpu.mem_read(SAMPLE+16, 32)))
        predicted = self.apply(source) / 8
        error = float(np.max(np.abs(actual - predicted)))
        assert error < 1e-7, ('actual interpolation kernel differs from FIR model', error)
        return error

    def apply(self, samples):
        up = np.zeros(len(samples)*8, dtype=samples.dtype)
        up[::8] = samples
        # FIR stored in reverse time order by CMSIS; these64-tap arrays are
        # symmetric. ×8 cancels zero-insertion gain for comparison to CESSB units.
        return np.convolve(up, self.coefficients[::-1])[:len(up)] * 8


def stimulus(name, count, low, high):
    t = np.arange(count) / RATE
    if name == 'single_tone':
        value = 1.3 * np.exp(2j * np.pi * 1500 * t)
    elif name in ('two_tone', 'smooth_burst', 'abrupt_rekey'):
        value = .65 * (np.exp(2j*np.pi*700*t) + np.exp(2j*np.pi*2300*t))
        if name == 'smooth_burst':
            value *= (1 - np.cos(2*np.pi*8*t)) / 2
        if name == 'abrupt_rekey':
            value *= (np.arange(count) % 1536 < 768)
    elif name == 'edge_tones':
        value = .65 * (np.exp(2j*np.pi*(low+100)*t) + np.exp(2j*np.pi*(high-100)*t))
    elif name == 'speech_like':
        # Deterministic band-limited harmonic complex with low-rate envelope;
        # synthetic, not a speech recording or a perceptual-quality test.
        value = np.zeros(count, dtype=np.complex128)
        for n in range(4, 18):
            value += np.exp(2j*np.pi*130*n*t + .71j*n*n) / (n ** .5)
        value *= (.6 + .2*np.cos(2*np.pi*4*t) + .2*np.cos(2*np.pi*10*t))
        value *= 1.3 / np.max(np.abs(value))
    else:
        raise ValueError(name)
    return value.astype(np.complex64).astype(np.complex128)


def reconstructed_peak(samples, warmup=0):
    """Independent 8x windowed-sinc estimate, excluding unknown trailing data.

    Leading zeros are real initialized-delay history. Trimming the last32
    samples avoids inventing a key-up at the file boundary. Not OEM interpolation.
    """
    half = 32
    taps = np.arange(-half, half + 1)
    peak = 0.0
    for fraction in np.arange(8) / 8:
        h = np.sinc(taps + fraction) * np.kaiser(2*half+1, 8.6)
        h /= np.sum(h)
        values = np.convolve(samples, h)[half + warmup:half + len(samples) - half]
        peak = max(peak, float(np.max(np.abs(values))))
    return peak


def metrics(samples, warmup=128):
    def part(values):
        peak = float(np.max(np.abs(values)))
        rms = float(np.sqrt(np.mean(np.abs(values)**2)))
        return dict(peak=peak, rms=rms, papr_db=20*np.log10(peak/rms),
                    peak_above_threshold_db=20*np.log10(peak/THRESHOLD))
    return dict(full=part(samples), settled=part(samples[warmup:]),
                reconstructed_8x_full_peak=reconstructed_peak(samples),
                reconstructed_8x_settled_peak=reconstructed_peak(samples, warmup))


def compare_case(original, port, name, low, high, boost, lsb, count, interpolators=None):
    source = stimulus(name, count, low, high)
    if lsb:
        source = source.conjugate()
    original.configure(low, high, boost, lsb)
    port.configure(low, high, boost, lsb)
    old, new = original.run(source), port.run(source)
    # r13's return mixer contributes a fixed +90 degree rotation. This has no
    # envelope effect. Remove it explicitly rather than fitting a favorable gain.
    aligned = old * -1j
    error = np.abs(aligned - new)
    row = dict(signal=name, low_hz=low, high_hz=high, boost_db=boost,
               sideband='LSB' if lsb else 'USB', count=count,
               input=metrics(source), boosted_input=metrics(source*10**(boost/20)),
               r13=metrics(old), port=metrics(new),
               phase_aligned_max_error=float(np.max(error)),
               phase_aligned_rms_error=float(np.sqrt(np.mean(error**2))),
               max_envelope_difference=float(np.max(np.abs(np.abs(old)-np.abs(new)))))
    if interpolators:
        for label, samples, interpolator in [('r13',old,interpolators[0]), ('port',new,interpolators[2]), ('port_stock_interpolator_counterfactual',new,interpolators[1])]:
            output = interpolator.apply(samples)
            settled = output[128*8:]
            peak = float(np.max(np.abs(output)))
            settled_peak = float(np.max(np.abs(settled)))
            row.setdefault(label,{})['oem_interpolated_normalized'] = dict(
                full_peak=peak, settled_peak=settled_peak,
                full_peak_above_threshold_db=20*np.log10(peak/THRESHOLD),
                settled_peak_above_threshold_db=20*np.log10(settled_peak/THRESHOLD))
    return row


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('r13_bin', type=Path)
    parser.add_argument('--elf', type=Path, default=ROOT/'build/tx-port.elf')
    parser.add_argument('--v119-bin', type=Path, help='optional exact stock image to compare actual OEM interpolation coefficients')
    parser.add_argument('--samples', type=int, default=8192)
    parser.add_argument('--quick', action='store_true', help='two-tone, stock filter,3.7dB,both sidebands')
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    if args.samples < 512:
        parser.error('--samples must be at least512')
    image = args.r13_bin.read_bytes()
    elf_bytes = args.elf.read_bytes()
    elf_sha = hashlib.sha256(elf_bytes).hexdigest()
    source_sha = hashlib.sha256((ROOT/'src/tx_dsp.c').read_bytes()).hexdigest()
    original, port = Original(image), Port(elf_bytes)
    interpolators = None
    interpolation_evidence = None
    if args.v119_bin:
        from plan_patch import validate_oem
        v119 = args.v119_bin.read_bytes()
        validate_oem(v119)
        private_coefficients = port.symbols['tx_interpolation_coefficients']
        interpolators = (Interpolator(image,0x08035388,0x0803d82c),
                        Interpolator(v119,0x08035364,0x0803e3e4),
                        Interpolator(v119,0x08035364,private_coefficients,elf_bytes))
        interpolation_evidence = dict(factor=8, taps=64,
            normalization='Multiply interpolation output by8 to compare to CESSB input units; NOT RF calibration',
            construction='Zero insertion convolved with actual coefficient arrays, checked against actual OEM kernels',
            kernel_model_max_error=[x.verify() for x in interpolators],
            coefficient_sums=[float(np.sum(x.coefficients)) for x in interpolators],
            order=['r13', 'stock_v119', 'private_tx_port'],
            private_tx_coefficients=hex(private_coefficients),
            private_tx_coefficient_sha256=hashlib.sha256(
                port.cpu.mem_read(private_coefficients,256)).hexdigest(),
            r13_coefficients='0x0803d82c', v119_coefficients='0x0803e3e4',
            v119_plaintext_sha256=hashlib.sha256(v119).hexdigest())
    signals = ['two_tone'] if args.quick else ['single_tone','two_tone','edge_tones','smooth_burst','abrupt_rekey','speech_like']
    filters = [(300,3000)] if args.quick else [(160,3000),(300,3000),(300,2400)]
    boosts = [3.7] if args.quick else [0.0,3.7,5.0]
    rows = []
    for low, high in filters:
        for boost in boosts:
            for name in signals:
                for lsb in [False,True]:
                    rows.append(compare_case(original, port, name, low, high, boost, lsb, args.samples, interpolators))
        print(f'Compared filter{low}..{high}: {len(rows)} cases', file=sys.stderr)
    result = dict(schema_version=2, scope='CESSB-stage I/Q and optional private TX interpolation; actual r13 binary vs actual ARM port ELF; not RF',
                  r13_plaintext_sha256=R13_SHA, port_elf_sha256=elf_sha,
                  source_tx_dsp_sha256=source_sha,
                  r13_functions={k:hex(v) for k,v in [('init',R13_INIT),('filter',R13_FILTER),('params',R13_PARAMS),('process',R13_PROCESS)]},
                  sample_rate_hz=RATE, threshold=THRESHOLD, warmup_samples=128,
                  rms_definition='sqrt(mean(I*I+Q*Q)); complex-envelope RMS, not measured RF power',
                  limitations=['No ALC, DAC, RF amplifier, hardware timing or speech recording; optional OEM interpolation uses validated linear FIR model',
                               '8x reconstruction is independent windowed sinc, not the installed firmware interpolation',
                               'abrupt_rekey intentionally injects discontinuous/non-bandlimited input; stress test only',
                               'settled excludes first128 samples only; later burst/keying transitions remain included',
                               'New state resets each case; cross-transmission retained-state differences require a separate test'],
                  summary=dict(cases=len(rows), maximum_envelope_difference=max(r['max_envelope_difference'] for r in rows),
                               maximum_phase_aligned_error=max(r['phase_aligned_max_error'] for r in rows),
                               maximum_r13_peak_db=max(r['r13']['full']['peak_above_threshold_db'] for r in rows),
                               maximum_port_peak_db=max(r['port']['full']['peak_above_threshold_db'] for r in rows)),
                  interpolation_evidence=interpolation_evidence, cases=rows)
    if interpolators:
        labels = ['r13','port','port_stock_interpolator_counterfactual']
        result['summary']['oem_interpolated_normalized'] = {
            label: dict(maximum_full_peak=max(r[label]['oem_interpolated_normalized']['full_peak'] for r in rows),
                        maximum_full_peak_above_threshold_db=max(r[label]['oem_interpolated_normalized']['full_peak_above_threshold_db'] for r in rows),
                        maximum_settled_peak_above_threshold_db=max(r[label]['oem_interpolated_normalized']['settled_peak_above_threshold_db'] for r in rows))
            for label in labels}
        result['summary']['maximum_new_vs_r13_interpolated_full_peak_delta_db'] = max(
            r['port']['oem_interpolated_normalized']['full_peak_above_threshold_db'] -
            r['r13']['oem_interpolated_normalized']['full_peak_above_threshold_db'] for r in rows)
        result['limitations'].append('Stock V1.1.9 interpolation counterfactual isolates the removed digital peak increase; private descriptor selection and state isolation are tested separately by verify_tx_interpolation.py')
    output = json.dumps(result, indent=2) + '\n'
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(output)
    else:
        print(output, end='')


if __name__ == '__main__':
    main()
