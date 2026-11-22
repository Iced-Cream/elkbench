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

    def test_put_new(self):
        """--new: an upload into a folder that isn't there (the loader makes it); no mkdir."""
        st = modsteps.parse('mods-put "~T MF" --new a.elemod', self.d)
        self.assertEqual((st['folder'], st['files'], st['new']), ('~T MF', [self.f], True))
        self.assertFalse(modsteps.parse('mods-put "~T MF" a.elemod', self.d)['new'])
        self.assertEqual(modsteps.put_files([('s', ['mods-put "~T MF" --new a.elemod'])], self.d), [self.f])
        with self.assertRaises(ValueError):
            modsteps.parse('mods-put "~T MF" --new', self.d)

    def test_put_refusals(self):
        for line, why in [('mods-put BASE a.elemod', '~T'), ('mods-put "~T BASE" b.elemod', 'no such'),
                          ('mods-put "~T bad lower" a.elemod', '~T'), ('mods-put "~T X/Y" a.elemod', '~T'),
                          ('mods-put "~T BASE"', 'at least one')]:
            with self.assertRaises(ValueError, msg=line) as c:
                modsteps.parse(line, self.d)
            self.assertIn(why, str(c.exception))
        big = os.path.join(self.d, 'big.elemod')
        with open(big, 'wb') as fh:
            fh.write(b'x' * (modsteps.MAX_FILE['rytm'] + 1))
        with self.assertRaises(ValueError):
            modsteps.parse('mods-put "~T BASE" big.elemod', self.d)

    def test_put_limit_per_unit(self):
        """The Rytm's loader stages an upload in 512 KB; the A4's writes it straight to its sector
        store, so its limit is the mods area in core_ddr."""
        big = os.path.join(self.d, 'big.elemod')
        with open(big, 'wb') as fh:
            fh.write(b'x' * (modsteps.MAX_FILE['rytm'] + 1))
        with self.assertRaisesRegex(ValueError, 'staging buffer'):
            modsteps.parse('mods-put "~T BASE" big.elemod', self.d, 'rytm')
        self.assertEqual(modsteps.parse('mods-put "~T BASE" big.elemod', self.d, 'a4')['files'], [big])
        e = plancheck.check([('s', ['mods-put "~T BASE" big.elemod'])], unit='rytm', here=self.d)['errors']
        self.assertTrue(any('staging buffer' in x for x in e), e)
        e = plancheck.check([('s', ['mods-put "~T BASE" big.elemod'])], unit='a4', here=self.d)['errors']
        self.assertFalse([x for x in e if 'big.elemod:' in x], e)
        with open(big, 'r+b') as fh:
            fh.truncate(modsteps.MAX_FILE['a4'] + 1)
        with self.assertRaisesRegex(ValueError, 'mods area'):
            modsteps.parse('mods-put "~T BASE" big.elemod', self.d, 'a4')
        with self.assertRaisesRegex(ValueError, 'no Drive Loader'):
            modsteps.parse('mods-active none', self.d, 'digitone')
        e, _f = modsteps.plan_rules([('s', ['mods-active none'])], unit='digitone')
        self.assertTrue(e)

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

    def test_put_pending_parse(self):
        st = modsteps.parse('mods-put "~T PAY" --pending a.elemod', self.d)
        self.assertEqual((st['pending'], st['new'], st['files']), (True, False, [os.path.join(self.d, 'a.elemod')]))
        self.assertFalse(modsteps.parse('mods-put "~T PAY" a.elemod', self.d)['pending'])
        self.assertTrue(modsteps.parse('mods-put "~T PAY" --new a.elemod', self.d)['new'])
        for line in ('mods-put "~T PAY" --pending', 'mods-put "~T PAY" --new --pending a.elemod',
                     'mods-put "~T PAY" a.elemod --pending', 'mods-put "~T PAY" --later a.elemod'):
            with self.assertRaises(ValueError, msg=line):
                modsteps.parse(line, self.d)
        # its file is part of what an emulator pass vouches for, as with the plain form
        self.assertEqual(modsteps.put_files([('s', ['mods-put "~T PAY" --pending a.elemod'])], self.d),
                         [os.path.join(self.d, 'a.elemod')])

    def test_expect_status_parse(self):
        st = modsteps.parse('expect-status "updates at boot: 1 applied" "pending /mods/~T PAY/x.elemod"', self.d)
        self.assertEqual(st['texts'], ['updates at boot: 1 applied', 'pending /mods/~T PAY/x.elemod'])
        for line in ('expect-status', 'expect-status ""'):
            with self.assertRaises(ValueError, msg=line):
                modsteps.parse(line, self.d)
        e = plancheck.check([('s', ['say proves works: the boot swapped the update in',
                                    'expect-status "updates at boot: 1 applied"'])], unit='rytm', here=self.d)['errors']
        self.assertFalse([x for x in e if 'machine assertion' in x], e)

    def test_remove_parse(self):
        self.assertEqual(modsteps.parse('mods-remove "~T BASE"', self.d), dict(op='mods-remove', folder='~T BASE'))
        for line in ('mods-remove MINE', 'mods-remove "~T A/B"', 'mods-remove', 'mods-remove "~T A" "~T B"'):
            with self.assertRaises(ValueError, msg=line):
                modsteps.parse(line, self.d)


    def test_clear_refused_parse(self):
        self.assertEqual(modsteps.parse('mods-clear-refused "~T BASE" "~T x-0.1.elemod"', self.d),
                         dict(op='mods-clear-refused', folder='~T BASE', file='~T x-0.1.elemod'))
        for line in ('mods-clear-refused "~T BASE"', 'mods-clear-refused MINE x.elemod',
                     'mods-clear-refused "~T BASE" a/x.elemod', 'mods-clear-refused "~T BASE" active',
                     'mods-clear-refused "~T BASE" x.elemod y.elemod'):
            with self.assertRaises(ValueError, msg=line):
                modsteps.parse(line, self.d)
        e = plancheck.check([('s', ['say proves works: the Clear of a file in use is refused',
                                    'mods-clear-refused "~T BASE" x.elemod'])], unit='rytm', here=self.d)['errors']
        self.assertFalse([x for x in e if 'machine assertion' in x], e)


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

    def test_v2_store_and_sites(self):
        text = STATUS + 'store: id 0xf3ef8186, 5 fields, 0 parked, written\n' + \
            ''.join('site dl256-002 0x401c55%02x 4\n' % (i * 4) for i in range(3))
        ok, lines = modsteps.expect(text, 2, ['dl256-001', 'dl256-002=3'])
        self.assertTrue(ok, lines)
        self.assertTrue(any(l.startswith('store: id 0xf3ef8186') for l in lines))
        self.assertFalse(modsteps.expect(text, 2, ['dl256-002=4'])[0])
        self.assertFalse(modsteps.expect(text, 2, ['dl256-001=1'])[0])
        self.assertFalse(modsteps.expect(text.replace('written', 'write failed'), 2)[0])
        stray = text.replace('dl256-002 loaded 0 0', 'dl256-002 refused 7 0 sites')
        self.assertFalse(modsteps.expect(stray, 1)[0])

    def test_stage_line_is_not_a_record(self):
        # a top-staging loader's .status names its live stage; it is a header, not a mod
        for stage in ('stage: 4853760 bytes at 0x47b5e000, free tail\n',
                      'stage: 65536 bytes at 0x47ffe000, floor\n'):
            ok, lines = modsteps.expect(STATUS + stage, 2)
            self.assertTrue(ok, lines)
            self.assertEqual(len(modsteps.status(STATUS + stage)['records']),
                             len(modsteps.status(STATUS)['records']))

    def test_a4_status_head_is_not_a_record(self):
        # drive-loader-a4mk2's .status (seen in the emulator 2026-11-15): its own route line and a
        # room: line in place of stage:; neither is a mod
        a4 = ('drive-loader-a4mk2 route\nfolder /mods\nlast: -\nio: 0 0 0 0\ncheck: -\n'
              'boot: 20178 us (checks 20170, link 7, inits 0)\n'
              'room: 249229312 bytes in one run, 249229312 free; directory generation 0\n')
        ok, lines = modsteps.expect(a4, 0)
        self.assertTrue(ok, lines)
        self.assertEqual(modsteps.status(a4)['records'], [])
        two = a4 + 'drive-hello loaded 0 0\ndrive-pay loaded 0 0\n'
        self.assertTrue(modsteps.expect(two, 2, ['drive-hello', 'drive-pay'])[0])
        self.assertFalse(modsteps.expect(two, 1)[0])

    # drive-loader-ar2 0.11's .status as the boot writes it (route.c at fork 8f39aca: stage and store,
    # no payload lines)
    STATUS_011 = ('drive-loader route\nfolder /mods\nlast: -\nio: 0 6 1100399900 6\ncheck: -\n'
                  'boot: 311705 us (checks 25127, link 279801, inits 6776)\n'
                  'stage: 4853760 bytes at 0x47b5e000, free tail\n'
                  'store: id 0xf3ef8186, 5 fields, 0 parked, kept\n'
                  'dl256-001 loaded 0 0\nduck loaded 0 0\n'
                  'site duck 0x401c5500 4\nsite duck 0x401c5504 4\n')
    # drive-loader-ar2 0.12 (fork drive-v2 d25b5ba), read word for word from the PCK2 emulator gate
    # (Projects\ARMKII\work\drive-loader\payload-hooks\evidence\pck2\mods-status-112733.txt): the boot
    # after an upload over a file in use, which swapped the update in
    STATUS_012 = ('drive-loader route\nfolder /mods\nlast: -\nio: 0 6 1100399900 6\ncheck: -\n'
                  'boot: 227939 us (checks 25320, link 202618, inits 0)\n'
                  'stage: 4707120 bytes at 0x47b81cd0, free tail\n'
                  'store: id 0xf869f328, 0 fields, 0 parked, kept\n'
                  'payloads: 1 kept, 0 lost\n'
                  'updates at boot: 1 applied, 0 failed\n'
                  'payload 163e1d60 204800 /mods/~T PAY/~T payload-check-0.1.elemod\n'
                  'payload-check loaded 0 0\n')
    # ... and evidence\mods-status-110558.txt: an upload over the file waiting as <name>.new
    STATUS_012_PENDING = ('drive-loader route\nfolder /mods\n'
                          'last: pending restart: ~T PAY/~T payload-check-0.1.elemod\n'
                          'io: 1 1459 1099941272 427443\ncheck: 1/1 /mods/~T PAY\n'
                          'boot: 214538 us (checks 25329, link 189208, inits 0)\n'
                          'stage: 4711216 bytes at 0x47b80cd0, free tail\n'
                          'store: id 0xf869f328, 0 fields, 0 parked, written\n'
                          'payloads: 1 kept, 0 lost\n'
                          'payload 163e1d60 230400 /mods/~T PAY/~T payload-check-0.1.elemod\n'
                          'pending /mods/~T PAY/~T payload-check-0.1.elemod\n'
                          'payload-check loaded 0 0\n')

    def test_011_status(self):
        st = modsteps.status(self.STATUS_011)
        self.assertEqual([r[0] for r in st['records']], ['dl256-001', 'duck'])
        self.assertEqual(st['sites'], {'duck': 2})
        self.assertEqual(st['payloads'], dict(summary=None, updates=None, entries=[], pending=[]))
        self.assertEqual(st['unknown'], [])
        ok, lines = modsteps.expect(self.STATUS_011, 2, ['duck=2'])
        self.assertTrue(ok, lines)
        self.assertFalse(any(l.startswith('payload') for l in lines), lines)

    def test_012_payload_lines_are_not_records(self):
        st = modsteps.status(self.STATUS_012)
        self.assertEqual(st['records'], [('payload-check', 'loaded', '0', '0', '')])
        self.assertEqual(st['payloads'], dict(
            summary='1 kept, 0 lost', updates='1 applied, 0 failed',
            entries=[('163e1d60', '204800', '/mods/~T PAY/~T payload-check-0.1.elemod')], pending=[]))
        self.assertEqual(st['unknown'], [])
        ok, lines = modsteps.expect(self.STATUS_012, 1, ['payload-check'])
        self.assertTrue(ok, lines)
        self.assertIn('payloads: 1 kept, 0 lost', lines)
        self.assertIn('updates at boot: 1 applied, 0 failed', lines)
        self.assertIn('1 records, 1 loaded', lines)
        self.assertFalse(modsteps.expect(self.STATUS_012, 2)[0])
        self.assertFalse(modsteps.expect(self.STATUS_012, 1, ['other'])[0])

    def test_012_pending(self):
        st = modsteps.status(self.STATUS_012_PENDING)
        self.assertEqual(len(st['records']), 1)
        self.assertEqual(st['payloads']['pending'], ['/mods/~T PAY/~T payload-check-0.1.elemod'])
        self.assertIsNone(st['payloads']['updates'])
        ok, lines = modsteps.expect(self.STATUS_012_PENDING, 1, ['payload-check'])
        self.assertTrue(ok, lines)
        self.assertIn('pending /mods/~T PAY/~T payload-check-0.1.elemod', lines)

    def test_012_failures_still_fail(self):
        refused = self.STATUS_012.replace('payload-check loaded 0 0', 'payload-check refused 16 3 core_machines')
        self.assertFalse(modsteps.expect(refused, 0)[0])
        self.assertFalse(modsteps.expect(refused, 1)[0])
        skipped = self.STATUS_012.replace('payload-check loaded 0 0\n', 'skipped: after a failed start\n')
        self.assertFalse(modsteps.expect(skipped, 0)[0])
        self.assertFalse(modsteps.expect(self.STATUS_012.replace('kept\n', 'write failed\n', 1), 1)[0])
        self.assertFalse(modsteps.expect(self.STATUS_012.replace('loaded 0 0', '? 0 0'), 1)[0])

    def test_mod_named_like_a_metadata_word_is_a_record(self):
        # shape decides before the payload words: a mod with the id `payload` or `pending` is a mod
        text = self.STATUS_012 + 'payload loaded 0 0\npending refused 7 0 sites\n'
        st = modsteps.status(text)
        self.assertEqual([r[:2] for r in st['records']],
                         [('payload-check', 'loaded'), ('payload', 'loaded'), ('pending', 'refused')])
        self.assertEqual(len(st['payloads']['entries']), 1)
        self.assertFalse(modsteps.expect(text, 2)[0])

    def test_unknown_lines_kept_not_judged(self):
        text = self.STATUS_012 + 'reads: 14 since boot, 0 damaged\n'
        st = modsteps.status(text)
        self.assertEqual(st['unknown'], ['reads: 14 since boot, 0 damaged'])
        self.assertEqual(len(st['records']), 1)
        ok, lines = modsteps.expect(text, 1)
        self.assertTrue(ok, lines)
        self.assertTrue(any(l.startswith('not mod records, not judged: reads:') for l in lines), lines)

    def test_boot_and_store_lines_are_not_records(self):
        st = modsteps.status(self.STATUS_012)
        self.assertEqual(st['boot'], '227939 us (checks 25320, link 202618, inits 0)')
        self.assertEqual(st['store'], 'id 0xf869f328, 0 fields, 0 parked, kept')
        self.assertFalse([r for r in st['records'] if r[0] in ('boot:', 'store:')])
        self.assertEqual(st['unknown'], [])

    def test_failed_expect_shows_the_status(self):
        ok, lines = modsteps.expect(self.STATUS_012, 2)
        self.assertFalse(ok)
        self.assertIn('/mods/.status as read:', lines)
        self.assertIn('  | payloads: 1 kept, 0 lost', lines)
        ok, lines = modsteps.expect(self.STATUS_012, 1)
        self.assertNotIn('/mods/.status as read:', lines)

    def test_expect_status(self):
        ok, lines = modsteps.expect_status(self.STATUS_012, ['updates at boot: 1 applied',
                                                             'payload 163e1d60 204800 /mods/~T PAY/'])
        self.assertTrue(ok, lines)
        self.assertIn("found 'updates at boot: 1 applied': updates at boot: 1 applied, 0 failed", lines)
        ok, lines = modsteps.expect_status(self.STATUS_012_PENDING, ['pending /mods/~T PAY/~T payload-check-0.1.elemod'])
        self.assertTrue(ok, lines)
        ok, lines = modsteps.expect_status(self.STATUS_012_PENDING, ['updates at boot:', 'payloads: 1 kept'])
        self.assertFalse(ok)
        self.assertIn("FAIL: no /mods/.status line holds 'updates at boot:'", lines)
        self.assertIn('  | pending /mods/~T PAY/~T payload-check-0.1.elemod', lines)
        self.assertFalse(modsteps.expect_status(self.STATUS_012, ['Updates at boot'])[0])   # case-sensitive

    def test_v2_expect_parse(self):
        self.assertEqual(modsteps.parse('expect-mods 2 loaded a duck=18', '.')['ids'], ['a', 'duck=18'])
        with self.assertRaises(ValueError):
            modsteps.parse('expect-mods 1 loaded duck=x', '.')


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

    def test_flag_put_without_remove(self):
        steps = [('start', ['expect-screen s']), ('set', ['say proves setup: x', 'mods-put "~T T" a.elemod'])]
        self.assertTrue(any('no mods-remove' in f for f in self.check(steps)['flags']))
        steps[1][1].append('mods-remove "~T T"')
        self.assertFalse(any('no mods-remove' in f for f in self.check(steps)['flags']))

    def test_remove_while_active_names_it(self):
        steps = [('start', ['expect-screen s']),
                 ('end', ['say proves setup: x', 'mods-active "~T T/a.elemod"', 'mods-remove "~T T"'])]
        self.assertTrue(any('names it' in e for e in self.check(steps)['errors']))
        steps[1][1].insert(2, 'mods-active restore')
        self.assertFalse(any('names it' in e for e in self.check(steps)['errors']))

    def test_remove_is_a_write(self):
        steps = [('start', ['mods-remove "~T T"', 'expect-screen s'])]
        self.assertTrue(any('before the first expect-screen' in e for e in self.check(steps)['errors']))

    def test_no_remove_after_stock(self):
        r = self.check([('after', ['mods-remove "~T T"'])], flash='stock.syx', stock=True, os_file='b.syx')
        self.assertTrue(any('stock has no /mods' in e for e in r['errors']))


class StartRunVerdict(unittest.TestCase):
    """daemon.start_run_verdict: a run_plan start run in parts."""
    def setUp(self):
        import daemon
        self.v = daemon.start_run_verdict

    def test_must_pass(self):
        self.assertTrue(self.v(None, True, [{'steps': [{'step': 'a', 'result': 'PASS'}]}])[0])
        ok, line = self.v(None, False, [{'steps': [{'step': 'a', 'result': 'PASS'}]},
                                        {'steps': [{'step': 'b', 'result': 'FAIL', 'detail': 'x'}]}])
        self.assertFalse(ok)
        self.assertIn('[b]', line)
        self.assertFalse(self.v(None, False, [])[0])          # the start or a mods step failed

    def test_must_stop(self):
        stop = [{'steps': [{'step': 'a', 'result': 'PASS'}]},
                {'steps': [{'step': 'b, after the power cycle', 'result': 'FAIL'}]}]
        self.assertTrue(self.v('b', False, stop)[0])
        self.assertFalse(self.v('c', False, stop)[0])
        self.assertFalse(self.v('b', True, [{'steps': [{'step': 'b', 'result': 'PASS'}]}])[0])


if __name__ == '__main__':
    unittest.main()
