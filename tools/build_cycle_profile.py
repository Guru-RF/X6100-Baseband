#!/usr/bin/env python3
"""Build an analysis-only DWT-instrumented payload; NEVER make firmware."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--cross', default='arm-none-eabi-')
parser.add_argument('--output-dir', type=Path, default=ROOT/'build/cycle-profile')
args = parser.parse_args()
out = args.output_dir.resolve()
out.mkdir(parents=True, exist_ok=True)
flags = ['-std=c11','-Os','-g3','-Wall','-Wextra','-Werror','-Isrc',
         '-mcpu=cortex-m4','-mthumb','-mfpu=fpv4-sp-d16','-mfloat-abi=hard',
         '-ffunction-sections','-fdata-sections','-fno-common','-fno-strict-aliasing',
         '-DBB_CYCLE_PROFILE']
objects = []
sources = ['src/tx_dsp.c','src/tx_protocol.c','firmware/tx_interpolation.c',
           'firmware/adapter.c','firmware/runtime.c','firmware/hooks.S']
for src in sources:
    obj = out / (Path(src).stem + '.o')
    runtime_flags = ['-ffreestanding'] if src == 'firmware/runtime.c' else []
    subprocess.run([args.cross+'gcc', *flags, *runtime_flags, '-c',src,'-o',str(obj)],cwd=ROOT,check=True)
    objects.append(str(obj))
elf = out / 'tx-port-profile.elf'
subprocess.run([args.cross+'gcc',*flags,'-nostdlib','-Wl,--gc-sections',
                '-Wl,-Map='+str(out/'tx-port-profile.map'),'-Tfirmware/port.ld',*objects,
                '-Wl,--start-group','-lm','-lgcc','-Wl,--end-group','-o',str(elf)],cwd=ROOT,check=True)
subprocess.run([args.cross+'size',str(elf)],check=True)
(out/'measurement-guide.json').write_text(json.dumps({
    'status': 'measurement support only; no hardware cycles measured',
    'elf_sha256': hashlib.sha256(elf.read_bytes()).hexdigest(),
    'source_sha256': {src: hashlib.sha256((ROOT/src).read_bytes()).hexdigest()
                      for src in sources + ['firmware/cycle_profile.h','firmware/tx_interpolation.h',
                                             'src/tx_dsp.h','src/tx_math.h','src/tx_protocol.h']},
    'compiler': subprocess.check_output([args.cross+'gcc','--version'],text=True).splitlines()[0],
    'compiler_flags': flags,
    'runtime_only_flags': ['-ffreestanding'],
    'counter': 'ARM DWT CYCCNT; free-running shared counter is never reset',
    'scope': 'Counter reads bracket C callback body including preemption and probe boundary instructions. Excludes statistics bookkeeping, assembly wrappers and complete DMA ISR. Instrumentation adds load and perturbs scheduling.',
    'reading': 'Halt target before reading bb_cycle_probe_status and bb_cycle_stats. Status 1 means enabled; status 2 means unavailable. Enable does not prove the counter advances on this target.',
    'stat_words': ['calls','last_cycles','minimum_cycles','maximum_cycles','total_cycles_low','total_cycles_high'],
    'stat_indices': ['block','audio','iq','unchanged_rx','unchanged_tx','state_reset_path',
                     'configuration_apply_path','configuration_reject_path','filter_restore_path',
                     'interpolation_reset_path','interpolator_selector'],
    'categories': 'Path categories overlap and each includes the entire block callback. Do not add category totals. Unchanged means no marked event, including snapshot-stability waiting.',
    'arithmetic': 'mean=((total_cycles_high<<32)+total_cycles_low)/calls. Valid before calls wraps at 2^32 and if each interval is shorter than one CYCCNT wrap.',
    'required_target_checks': ['Record actual core clock and DMA block length/rate.',
                               'Exercise first TX, both sidebands, band/mode changes, cold and changed configuration, worst input and preempting interrupts.',
                               'Measure complete ISR entry through all exits separately before claiming DMA deadline margin.']
}, indent=2)+'\n')
print('Analysis ELF only:',elf)
