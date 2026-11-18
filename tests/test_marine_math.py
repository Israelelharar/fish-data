"""Unit tests for the pure maths in copernicus_marine.py (no network, no Copernicus account).
Run: python -m unittest discover -s tests"""
import importlib.util
import sys
import types
import unittest
from pathlib import Path

import numpy as np

sys.modules.setdefault("copernicusmarine", types.ModuleType("copernicusmarine"))  # stub: not needed for maths
spec = importlib.util.spec_from_file_location("cm_script", Path(__file__).resolve().parent.parent / "scripts/copernicus_marine.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class LaggedChange(unittest.TestCase):
    def test_linear_warming(self):
        src = np.arange(0, 400, 1.0)  # hours
        sst = 20 + 0.01 * src  # +0.24 C per day
        dst = np.array([200.0, 300.0])
        self.assertEqual(m.lagged_change(src, sst, dst, 24), [0.24, 0.24])
        self.assertEqual(m.lagged_change(src, sst, dst, 168), [1.68, 1.68])

    def test_none_without_history(self):
        src = np.arange(100, 300, 1.0)
        sst = np.full(src.shape, 22.0)
        # 168 h before hour 150 is outside the data -> None, never extrapolated.
        self.assertEqual(m.lagged_change(src, sst, np.array([150.0]), 168), [None])
        self.assertEqual(m.lagged_change(src, sst, np.array([290.0]), 168), [0.0])

    def test_nan_gaps(self):
        src = np.arange(0, 10, 1.0)
        sst = np.array([np.nan] * 10)
        self.assertEqual(m.lagged_change(src, sst, np.array([5.0]), 1), [None])


class Interp(unittest.TestCase):
    def test_direction_wraps(self):
        out = m.interp(np.array([0.0, 2.0]), [350, 10], np.array([1.0]), direction=True)
        self.assertIn(out[0], (0, 360))


if __name__ == "__main__":
    unittest.main()
