# SPDX-License-Identifier: GPL-2.0-or-later
"""Tests for modsteps.py (the Drive Loader plan steps) and their plan checker rules."""
import os
import tempfile
import unittest

import modsteps
import plancheck

STATUS = """drive-loader route
folder /mods
last: -
io: 1 6 1100399720 9761
check: 0/0 /mods
boot: 311705 us (checks 25127, link 279801, inits 6776)
dl256-001 loaded 0 0
dl256-002 loaded 0 0
"""


class Parse(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.f = os.path.join(self.d, 'a.elemod')
        with open(self.f, 'wb') as fh:
            fh.write(b'x' * 100)

    def test_put(self):
        st = modsteps.parse('mods-put "~T BASE" a.elemod', self.d)
        self.assertEqual(st['folder'], '~T BASE')
        self.assertEqual(st['files'], [self.f])
        self.assertEqual(modsteps.put_files([('s', ['mods-put "~T BASE" a.elemod'])], self.d), [self.f])

    def test_put_refusals(self):
        for line, why in [('mods-put BASE a.elemod', '~T'), ('mods-put "~T BASE" b.elemod', 'no such'),
                          ('mods-put "~T bad lower" a.elemod', '~T'), ('mods-put "~T X/Y" a.elemod', '~T'),
                          ('mods-put "~T BASE"', 'at least one')]:
            with self.assertRaises(ValueError, msg=line) as c:
                modsteps.parse(line, self.d)
            self.assertIn(why, str(c.exception))
        big = os.path.join(self.d, 'big.elemod')
        with open(big, 'wb') as fh:
            fh.write(b'x' * (modsteps.MAX_FILE + 1))
        with self.assertRaises(ValueError):
            modsteps.parse('mods-put "~T BASE" big.elemod', self.d)

    def test_active(self):
        self.assertEqual(modsteps.parse('mods-active "~T BASE/a.elemod"', self.d)['paths'], ['~T BASE/a.elemod'])
        self.assertEqual(modsteps.parse('mods-active restore', self.d)['what'], 'restore')
        self.assertEqual(modsteps.parse('mods-active none', self.d)['paths'], [])
        for line in ('mods-active owner.elemod', 'mods-active "MINE/a.elemod"', 'mods-active "~T A/../x"'):
            with self.assertRaises(ValueError, msg=line):
                modsteps.parse(line, self.d)

    def test_expect_parse(self):
        st = modsteps.parse('expect-mods 2 loaded dl256-001', self.d)
        self.assertEqual((st['n'], st['ids']), (2, ['dl256-001']))
        with self.assertRaises(ValueError):
            modsteps.parse('expect-mods two loaded', self.d)


class Expect(unittest.TestCase):
    def test_pass(self):
        ok, lines = modsteps.expect(STATUS, 2, ['dl256-002'])
        self.assertTrue(ok, lines)

    def test_count(self):
        self.assertFalse(modsteps.expect(STATUS, 3)[0])

    def test_missing_id(self):
        self.assertFalse(modsteps.expect(STATUS, 2, ['other'])[0])

    def test_skipped_after_a_crash(self):
        text = STATUS.replace('dl256-001 loaded 0 0\ndl256-002 loaded 0 0\n', 'skipped: after a failed start\n')
        ok, lines = modsteps.expect(text, 0)
        self.assertFalse(ok)
        self.assertTrue(any('failed start' in l for l in lines))

    def test_refused_record(self):
        text = STATUS.replace('dl256-002 loaded 0 0', 'dl256-002 refused 16 3 core_machines')
        self.assertFalse(modsteps.expect(text, 1)[0])

    def test_no_boot_line(self):
        self.assertFalse(modsteps.expect(STATUS.replace('boot:', 'xboot:'), 2)[0])


class Rules(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        with open(os.path.join(self.d, 'a.elemod'), 'wb') as fh:
            fh.write(b'x')

    def check(self, steps, **kw):
        return plancheck.check(steps, refs=os.path.join(self.d, 'refs'), here=self.d, **kw)

    def test_keys_after_reboot_need_expect_mods(self):
        steps = [('start', ['expect-screen s']),
                 ('load', ['say proves setup: mods', 'mods-put "~T T" a.elemod', 'mods-active "~T T/a.elemod"',
                           'power-cycle', 'tap PLAY'])]
        self.assertTrue(any('no expect-mods' in e for e in self.check(steps)['errors']))
        steps[1][1].insert(4, 'expect-mods 1 loaded')
        self.assertFalse(any('expect-mods' in e for e in self.check(steps)['errors']))

    def test_expect_mods_is_an_assertion(self):
        steps = [('start', ['expect-screen s']), ('works', ['say proves works: loaded', 'expect-mods 1 loaded'])]
        self.assertFalse(any('machine assertion' in e for e in self.check(steps)['errors']))

    def test_no_mods_after_stock(self):
        steps = [('after', ['expect-mods 0 loaded'])]
        r = self.check(steps, flash='stock.syx', stock=True, os_file='b.syx')
        self.assertTrue(any('stock has no /mods' in e for e in r['errors']))

    def test_active_before_start_check_is_a_write(self):
        steps = [('start', ['mods-active none', 'expect-screen s'])]
        self.assertTrue(any('before the first expect-screen' in e for e in self.check(steps)['errors']))

    def test_flag_without_reboot(self):
        steps = [('start', ['expect-screen s']),
                 ('set', ['say proves setup: x', 'mods-put "~T T" a.elemod', 'mods-active "~T T/a.elemod"'])]
        self.assertTrue(any('next boot' in f for f in self.check(steps)['flags']))

    def test_bad_step_is_an_error(self):
        steps = [('start', ['expect-screen s']), ('set', ['say proves setup: x', 'mods-put MINE a.elemod'])]
        self.assertTrue(any('~T' in e for e in self.check(steps)['errors']))


if __name__ == '__main__':
    unittest.main()
