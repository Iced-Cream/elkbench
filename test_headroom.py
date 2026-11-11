"""Tests for headroom.py: the CPU / free RAM / emulator count check before an emulator starts."""
import threading
import time
import unittest
from unittest import mock

import batch
import headroom


class Readings:
    """Stands in for the PC's measurements."""

    def __init__(self, cpu=10.0, mem=16.0, n=0):
        self.cpu, self.mem, self.n = cpu, mem, n

    def patch(self, test):
        for name, f in (('cpu_percent', lambda secs=0: self.cpu), ('free_gb', lambda: self.mem),
                        ('emulator_count', lambda images=(): self.n)):
            p = mock.patch.object(headroom, name, f)
            p.start()
            test.addCleanup(p.stop)
        headroom._recent.clear()
        test.addCleanup(headroom._recent.clear)


class TestConfig(unittest.TestCase):
    def test_off_without_table(self):
        self.assertIsNone(headroom.Limits.from_config({}))

    def test_table_defaults_and_overrides(self):
        d = headroom.Limits.from_config({'emulator_headroom': {}})
        self.assertEqual((d.max_cpu, d.min_free_gb, d.max_emulators), (60.0, 4.0, 10))
        o = headroom.Limits.from_config({'emulator_headroom': {'max_cpu': 50, 'max_emulators': 8}})
        self.assertEqual((o.max_cpu, o.min_free_gb, o.max_emulators), (50.0, 4.0, 8))


class TestMeasure(unittest.TestCase):
    def setUp(self):
        self.r = Readings()
        self.r.patch(self)
        self.lim = headroom.Limits(settle=60)

    def test_ok(self):
        self.assertTrue(headroom.measure(self.lim, 0)['ok'])

    def test_each_limit_refuses(self):
        for attr, val, word in (('cpu', 60.0, 'CPU'), ('mem', 3.9, 'GB free'), ('n', 10, 'emulators running')):
            r = Readings()
            setattr(r, attr, val)
            r.patch(self)
            m = headroom.measure(self.lim, 0)
            self.assertFalse(m['ok'])
            self.assertIn(word, m['why'])

    def test_settling_starts_count_as_load(self):
        with mock.patch('os.cpu_count', return_value=16):
            self.r.cpu = 50.0                  # 50 + 6.25 per settling start
            ok, _ = headroom.acquire(self.lim, 0, sample=0)
            self.assertTrue(ok)
            ok, m = headroom.acquire(self.lim, 0, sample=0)
            self.assertTrue(ok)                # 56.25 < 60
            ok, m = headroom.acquire(self.lim, 0, sample=0)
            self.assertFalse(ok)               # 62.5
            self.assertIn('settling', m['why'])

    def test_wait_then_go(self):
        self.r.n = 10
        threading.Timer(0.05, lambda: setattr(self.r, 'n', 3)).start()
        ok, m = headroom.acquire(self.lim, 5, poll=0.01, sample=0)
        self.assertTrue(ok)
        self.assertEqual(m['emulators'], 3)

    def test_stopped_ends_wait(self):
        self.r.n = 10
        stop = threading.Event()
        threading.Timer(0.05, stop.set).start()
        t0 = time.time()
        ok, _ = headroom.acquire(self.lim, None, stop, poll=0.01, sample=0)
        self.assertFalse(ok)
        self.assertLess(time.time() - t0, 2)

    def test_cli_exit_codes(self):
        self.assertEqual(headroom.main([]), 0)
        self.r.mem = 1.0
        self.assertEqual(headroom.main([]), 1)


class TestRealReadings(unittest.TestCase):
    def test_numbers_are_sane(self):
        self.assertTrue(0 <= headroom.cpu_percent(0.1) <= 100)
        self.assertGreater(headroom.free_gb(), 0)
        self.assertGreaterEqual(headroom.emulator_count(), 0)


class TestBatchWaits(unittest.TestCase):
    def test_batch_run_waits_for_room(self):
        r = Readings(n=10)
        r.patch(self)
        lim = headroom.Limits(settle=0)
        started = []

        def run_one(n, v):
            started.append(time.time())
            return dict(outcome='pass')
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(batch, 'HEADROOM', lim), \
                mock.patch.object(headroom, 'acquire', wraps=lambda l, w, s, real=headroom.acquire: real(
                    l, w, s, poll=0.01, sample=0)):
            b = batch.Batch([dict(name='A')], 1, 1, 1, d, run_one)
            t = threading.Thread(target=b.run)
            t.start()
            time.sleep(0.1)
            self.assertEqual(started, [])      # no room yet
            r.n = 2
            t.join(5)
            self.assertEqual(len(started), 1)

    def test_batch_off_without_headroom(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d, mock.patch.object(batch, 'HEADROOM', None):
            b = batch.Batch([dict(name='A')], 2, 2, 1, d, lambda n, v: dict(outcome='pass'))
            self.assertEqual([x['outcome'] for x in b.run()], ['pass', 'pass'])


if __name__ == '__main__':
    unittest.main()
