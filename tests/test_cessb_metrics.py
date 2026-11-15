"""Metric/reconstruction checks; skip unless optional comparison packages exist."""
import importlib.util
import unittest

AVAILABLE = all(importlib.util.find_spec(name) is not None
                for name in ('numpy', 'unicorn', 'elftools'))
if AVAILABLE:
    import numpy as np
    from tools.compare_cessb import THRESHOLD, metrics, reconstructed_peak, Original


@unittest.skipUnless(AVAILABLE, 'optional numpy/unicorn/pyelftools comparison dependencies')
class CessbComparisonMetrics(unittest.TestCase):
    def test_threshold_and_papr_units(self):
        x = np.full(1024, THRESHOLD, dtype=complex)
        out = metrics(x)['settled']
        self.assertAlmostEqual(out['peak_above_threshold_db'], 0)
        self.assertAlmostEqual(out['papr_db'], 0)
        doubled = metrics(x * 2)['settled']
        self.assertAlmostEqual(doubled['peak_above_threshold_db'], 6.020599913279624)

    def test_reconstruction_preserves_dc_and_tone(self):
        # Ignore startup: the reconstruction appropriately assumes leading zero
        # history, whose abrupt step itself has ringing.
        self.assertAlmostEqual(reconstructed_peak(np.ones(1024), 128), 1, places=12)
        t = np.arange(4096) / 12500
        x = np.exp(2j * np.pi * 1500 * t)
        self.assertLess(abs(reconstructed_peak(x, 128) - 1), 1e-4)

    def test_reject_unidentified_r13_image(self):
        with self.assertRaisesRegex(ValueError, 'exact plaintext'):
            Original(b'not the original firmware')


if __name__ == '__main__':
    unittest.main()
