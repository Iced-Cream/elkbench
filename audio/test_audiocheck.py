"""Synthetic checks for audiocheck.py: py -3.14 -m unittest test_audiocheck (from this folder)."""
import unittest
import numpy as np
import audiocheck as ac

RATE = 48000
rng = np.random.default_rng(1)


def tone(seconds=2.0, f=440.0, level=0.3):
    t = np.arange(int(seconds * RATE)) / RATE
    return level * np.sin(2 * np.pi * f * t) + 1e-4 * rng.standard_normal(len(t))


def drums(seconds=4.0):
    """Kicks, hats and a pad at 120 BPM: sharp attacks, no faults."""
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    x = 0.1 * np.sin(2 * np.pi * 110 * t) * (0.6 + 0.4 * np.sin(2 * np.pi * 0.5 * t))
    for beat in np.arange(0, seconds, 0.5):
        s = int(beat * RATE)
        k = np.arange(min(int(0.3 * RATE), n - s)) / RATE
        x[s:s + len(k)] += 0.6 * np.sin(2 * np.pi * (50 + 150 * np.exp(-k * 30)) * k) * np.exp(-k * 12)
        h = int((beat + 0.25) * RATE)
        if h < n:
            m = min(int(0.05 * RATE), n - h)
            x[h:h + m] += 0.2 * rng.standard_normal(m) * np.exp(-np.arange(m) / RATE * 80)
    return x


class Dropouts(unittest.TestCase):
    def test_clean_tone_and_drums(self):
        self.assertEqual(ac.dropouts(tone(), RATE), [])
        self.assertEqual(ac.dropouts(drums(), RATE), [])

    def test_gap(self):
        x = tone()
        x[30000:30064] = 0
        kinds = [f["kind"] for f in ac.dropouts(x, RATE)]
        self.assertIn("gap", kinds)
        self.assertEqual([f for f in ac.find_gaps(x, RATE)][0]["samples"], 64)

    def test_click_in_drums(self):
        x = drums()
        x[50000:] = np.roll(x[50000:], 7)          # a repeated / skipped few samples
        found = ac.find_clicks(x, RATE)
        self.assertTrue(any(abs(f["t"] - 50000 / RATE) < 0.001 for f in found), found)

    def test_dip(self):
        x = tone()
        x[48000:48000 + 480] *= 0.001             # 10 ms, -60 dB, not exact zero
        self.assertTrue(any(f["kind"] == "dip" for f in ac.dropouts(x, RATE)))


class Compare(unittest.TestCase):
    def test_same_material_passes(self):
        a = drums()
        r = ac.compare(a, a * 0.5 + 1e-5 * rng.standard_normal(len(a)), RATE)
        self.assertTrue(r["pass"], r["worst_diff_db"])
        self.assertAlmostEqual(r["level_diff_db"], -6.02, places=1)
        self.assertFalse(ac.compare(a, a * 0.5, RATE, absolute=True)["pass"])

    def test_bit_reduced_fails(self):
        a = drums()
        crushed = np.round(a * 16) / 16              # 5-bit: broadband noise
        r = ac.compare(a, crushed, RATE)
        self.assertFalse(r["pass"])
        self.assertGreater(abs(r["worst_diff_db"]), 3)


if __name__ == "__main__":
    unittest.main()
