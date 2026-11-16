"""Pure decoder tests; optional exact-OEM execution via X6100_V119_BIN."""
import importlib.util
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from tools import plan_patch, verify_transport_contract as transport

OEM_PATH = os.environ.get('X6100_V119_BIN') or os.environ.get('V119_OEM_BIN')


class TransportDecoderTests(unittest.TestCase):
    def test_bad_image_rejected_before_optional_dependencies(self):
        image = bytearray(plan_patch.OEM_SIZE)
        # Even all accepted hook instructions cannot authorize another image.
        for _, address, expected in plan_patch.HOOKS:
            image[address - plan_patch.BASE:address - plan_patch.BASE + 4] = bytes.fromhex(expected)
        with patch.object(transport, '_dependencies', side_effect=AssertionError('must not load')):
            with self.assertRaisesRegex(ValueError, 'unsupported OEM image'):
                transport.audit(image)

    def test_receive_and_transmit_dma_fields(self):
        receive = transport.decode_dma(0x00052d11, 512)
        transmit = transport.decode_dma(0x04062d41, 512)
        self.assertEqual((receive['channel'], receive['direction']), (0, 0))
        self.assertEqual((transmit['channel'], transmit['direction']), (2, 1))
        self.assertTrue(receive['transfer_complete_interrupt'])
        self.assertFalse(transmit['transfer_complete_interrupt'])
        for row in (receive, transmit):
            self.assertEqual((row['peripheral_bits'], row['memory_bits']), (16, 16))
            self.assertTrue(row['memory_increment'] and row['circular'] and row['double_buffer'])
        with self.assertRaisesRegex(ValueError, 'reserved'):
            transport.decode_dma(3 << 11, 512)

    def test_nvic_preemption_is_distinct_from_subpriority(self):
        dma = transport.decode_priority(0x80, 5)
        event = transport.decode_priority(0xe0, 5)
        error = transport.decode_priority(0xf0, 5)
        self.assertEqual((dma['preemption'], dma['subpriority']), (2, 0))
        self.assertEqual((event['preemption'], event['subpriority']), (3, 2))
        self.assertEqual((error['preemption'], error['subpriority']), (3, 3))
        # A grouping change can change the preemption relation.
        self.assertEqual(transport.decode_priority(0x80, 7)['preemption'], 0)
        self.assertEqual(transport.decode_priority(0xf0, 7)['preemption'], 0)


@unittest.skipUnless(OEM_PATH, 'set X6100_V119_BIN for exact-OEM emulation')
@unittest.skipUnless(importlib.util.find_spec('capstone') and importlib.util.find_spec('unicorn'),
                     'optional capstone/unicorn dependencies unavailable')
class ExactOemTransportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.image = Path(OEM_PATH).read_bytes()
        cls.report = transport.audit(cls.image)

    def test_configuration_does_not_claim_measured_timing(self):
        report = self.report
        self.assertEqual(report['status'], 'verified_configuration_only')
        self.assertFalse(report['clock']['physical_clocks_measured'])
        self.assertFalse(report['transport']['physical_rate_or_cadence_measured'])
        self.assertFalse(report['model']['full_boot_executed'])
        self.assertEqual(report['transport']['bank_stereo_frames'], 128)
        self.assertEqual(report['transport']['raw_rate_constant_hz'], 100000.0)
        self.assertAlmostEqual(report['transport']['nominal_bank_period_seconds'], .00128)
        self.assertEqual(report['transport']['nominal_cycles_per_bank_at_configured_clock'], 320000)
        json.dumps(report)  # Public embedding API is JSON serializable.

    def test_clock_reset_removes_synthetic_inherited_pll(self):
        cold, inherited = self.report['clock']['runs']
        self.assertEqual(cold['pllcfgr'], inherited['pllcfgr'])
        self.assertEqual(inherited['system_core_clock_variable_hz'], 249999960)
        for run in (cold, inherited):
            pll_writes = [w for w in run['register_writes'] if w['address'] == '0x40023804']
            self.assertEqual(len(pll_writes), 2)
            self.assertTrue(all(not w['pll_enabled_before_write'] for w in pll_writes))

    def test_bank_selection_and_full_isr_exit_enumeration(self):
        cases = self.report['transport']['bank_selection_cases']
        self.assertEqual(len(cases), 4)
        # CT=1 means M0 completed. RX and TX CT bits are independent.
        case = next(c for c in cases if c['stream3_ct'] == 1 and c['stream4_ct'] == 0)
        self.assertEqual(case['audio_rx_iq_rx_audio_tx_iq_tx'],
                         ['0x2000e2f4', '0x2000d2f4', '0x2000d6f4', '0x2000c6f4'])
        self.assertEqual(self.report['isr']['return_instructions'], ['0x8024552', '0x8027cc8'])
        self.assertEqual(self.report['isr']['software_frame_bytes'], 176)
        # The function's outlying mode branches extend past its return sites.
        self.assertGreater(int(self.report['isr']['last_reachable_instruction'], 16), 0x08027cc8)
        self.assertEqual(self.report['isr']['table_branch']['entries'], 6)

    def test_tiny_execution_budget_fails_closed(self):
        with self.assertRaisesRegex(ValueError, 'instruction limit'):
            transport.audit(self.image, instruction_limit=1)

    def test_any_changed_oem_byte_is_rejected(self):
        image = bytearray(self.image)
        image[0x11480] ^= 1
        with self.assertRaisesRegex(ValueError, 'unsupported OEM image'):
            transport.audit(image)


if __name__ == '__main__':
    unittest.main()
