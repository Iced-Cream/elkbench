"""Tests for the example write policy (policy.py beside this file), against analoghealth's
fake unit:  ANALOGHEALTH=<your analoghealth checkout> python test_policy.py"""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
if not os.environ.get('ANALOGHEALTH'):
    raise SystemExit('set ANALOGHEALTH to your analoghealth checkout')
REPO = Path(os.environ['ANALOGHEALTH'])
os.environ['ANALOGHEALTH_POLICY'] = str(HERE / 'policy.py')
sys.path[:0] = [str(REPO / 'tools'), str(REPO / 'tests')]
import analogrun                                  # noqa: E402
import writepolicy                                # noqa: E402
from test_analogrun import FakeUnit, make_kit     # noqa: E402

policy = writepolicy.policy()


class Rig(unittest.TestCase):
    def setUp(self):
        self.unit = unit = FakeUnit()
        unit.kit = [make_kit('~T KIT 1')]

        class Port(FakeUnit):
            def __new__(cls, *_name):
                return unit
        saved, analogrun.open_port = analogrun.open_port, Port
        self.addCleanup(setattr, analogrun, 'open_port', saved)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.s = analogrun.Session(unit, self.tmp.name)

    def test_loaded(self):
        self.assertTrue(writepolicy.installed())
        self.assertIs(policy, writepolicy.policy())

    def test_names(self):
        self.assertEqual(policy.check_new_name('~t kit a'), '~T KIT A')
        for bad in ('KIT A', '~T', '~T ', '~T ' + 'X' * 13, '~T K_1', '~TKIT'):
            with self.assertRaises(writepolicy.Refused):
                policy.check_new_name(bad)
        self.assertTrue(policy.is_test_name('~T X'))
        self.assertFalse(policy.is_test_name('T X'))

    def test_paths(self):
        self.assertEqual(policy.split_path('/soundbanks/C/7'), ('/soundbanks/C', 7))
        for bad in ('/samples/x', '/projects', '/projects/a', '/soundbanks/Q/1', '/patterns/3'):
            with self.assertRaises(writepolicy.Refused):
                policy.split_path(bad)

    def test_send_step_checks_the_active_project(self):
        analogrun.save_syx(Path(self.tmp.name, 'k.syx'), [make_kit('~T KIT 1', 5)])
        self.unit.kit = [make_kit('KIT A')]
        with self.assertRaises(writepolicy.Refused):
            self.s.step('send k.syx')
        self.assertEqual(getattr(self.unit, 'received_kits', 0), 0)
        self.unit.kit = [make_kit('~T KIT 1')]
        self.s.step('send k.syx')
        self.assertEqual(self.unit.received_kits, 1)

    def test_kit_roundtrip_needs_a_test_project(self):
        out = Path(self.tmp.name, 'kit')
        self.assertEqual(analogrun.main(['kit-roundtrip', str(out), '--kit', '0', '--settle', '0']), 0)
        self.unit.kit, self.unit.received_kits = [make_kit('KIT A')], 0
        self.assertEqual(analogrun.main(['kit-roundtrip', str(out), '--kit', '0', '--settle', '0']), 2)
        self.assertEqual(self.unit.received_kits, 0)

    def test_a_save_writes_nothing_to_the_active_project(self):
        # a new project saved into a slot leaves the previous one active: never rename its kit
        self.unit.kit, self.unit.received_kits = [make_kit('KIT A')], 0
        note = policy.after_save_project(self.s, '/projects/126', '~T NEW')
        self.assertIn('not marked', note)
        self.assertEqual(self.unit.received_kits, 0)
        self.unit.kit = [make_kit('~T KIT 1')]
        self.assertIn('a test project', policy.after_save_project(self.s, '/projects/126', '~T NEW'))
        self.assertEqual(self.unit.received_kits, 0)

    def test_active_command(self):
        for name, verdict in (('~T KIT 1', 'a test project'), ('KIT A', 'not a test project')):
            self.unit.kit = [make_kit(name)]
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(analogrun.main(['active']), 0)
            self.assertIn('%r (%s)' % (name, verdict), out.getvalue())

    def test_data_commands(self):
        unit = self.unit
        out = Path(self.tmp.name, 'rt')
        run = lambda *a: analogrun.main(['data', *a])
        # Empty slots outside 121-128, and empty slots without a ~T name: refused.
        self.assertEqual(run('roundtrip', '/projects/1', str(out), '--to', '/projects/5', '--yes'), 2)
        self.assertEqual(run('roundtrip', '/projects/1', str(out), '--to', '/projects/121', '--yes'), 2)
        self.assertNotIn('/projects/121', unit.store)
        # Into an empty test slot, named ~T in the same command.
        self.assertEqual(run('roundtrip', '/projects/1', str(out), '--to', '/projects/121',
                             '--name', '~t copy', '--yes'), 0)
        self.assertEqual(unit.store['/projects/121'], unit.store['/projects/1'])
        self.assertEqual(unit.names['/projects/121'], '~T COPY')
        # A ~T item keeps its name; the person's project is never a target.
        self.assertEqual(run('roundtrip', '/projects/1', str(out), '--to', '/projects/121', '--yes'), 0)
        self.assertEqual(unit.names['/projects/121'], '~T COPY')
        self.assertEqual(run('roundtrip', '/projects/1', str(out), '--yes'), 2)
        self.assertEqual(run('roundtrip', '/projects/121', str(out), '--to', '/projects/1', '--yes'), 2)
        self.assertEqual(run('roundtrip', '/projects/1', str(out), '--to', '/projects/122',
                             '--name', 'COPY', '--yes'), 2)
        src = Path(self.tmp.name, 'p.bin')
        src.write_bytes(bytes(100))
        self.assertEqual(run('put', '/projects/1', str(src), '--yes'), 2)
        self.assertEqual(unit.store['/projects/1'], bytes(range(256)) * 50)
        self.assertEqual(run('put', '/projects/121', str(src), '--yes'), 0)
        self.assertEqual((unit.store['/projects/121'], unit.names['/projects/121']), (bytes(100), '~T COPY'))
        backup = Path(self.tmp.name, 'p121.bin')
        self.assertEqual(run('clear', '/projects/1', str(backup), '--yes'), 2)
        self.assertIn('/projects/1', unit.store)
        self.assertEqual(run('clear', '/projects/121', str(backup), '--yes'), 0)
        self.assertNotIn('/projects/121', unit.store)


if __name__ == '__main__':
    unittest.main()
