"""Offline tests for the plan checker (plancheck.py) and the plan templates in templates/.
No unit, emulator or analoghealth checkout is needed.

    python test_plancheck.py
"""
import os
import re
import shutil
import tempfile
import unittest

import plancheck

HERE = os.path.dirname(os.path.abspath(__file__))


def parse(text):
    """The sections of a plan, as testplan.parse reads them (comments after a space dropped)."""
    steps = []
    for raw in text.splitlines():
        line = re.sub(r'(^|\s)#.*$', '', raw).strip()
        if line.startswith('[') and line.endswith(']'):
            steps.append((line[1:-1].strip(), []))
        elif line and steps:
            steps[-1][1].append(line)
    return steps


GOOD = '''
[start]
tap NO
expect-screen home 2 rows=0-6
[set the value]
say proves set-and-kept: the value reads back
cc 1 10 64
expect-screen value 1 rows=8-31
[look]
say proves evidence: a shot for the person
shot look.png
'''


def check(text, **kw):
    return plancheck.check(parse(text), **kw)


class Rules(unittest.TestCase):
    def test_a_good_plan_passes_and_lists_what_it_proves(self):
        r = check(GOOD)
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['proves'], [('start', 'start'), ('set the value', 'set-and-kept'), ('look', 'evidence')])
        self.assertTrue(any('judged by eye' in f for f in r['flags']))
        lines = plancheck.go_lines(r)
        self.assertIn('evidence: [look]', lines[0])

    def test_no_start_check_is_refused(self):
        r = check(GOOD.replace('expect-screen home 2 rows=0-6', 'ping'))
        self.assertTrue(any('no start check' in e for e in r['errors']), r['errors'])

    def test_a_write_before_the_first_check_is_refused(self):
        r = check('[start]\nnrpn 1 1 35 5376\nexpect-screen home 2\n' + GOOD.split('[set the value]')[1].join(['[set the value]', '']))
        self.assertTrue(any('before the first expect-screen' in e for e in r['errors']), r['errors'])

    def test_a_section_must_say_what_it_proves(self):
        r = check(GOOD.replace('say proves evidence: a shot for the person\n', ''))
        self.assertTrue(any('[look] says nothing about what it proves' in e for e in r['errors']), r['errors'])
        r = check(GOOD.replace('proves evidence', 'proves vibes'))
        self.assertTrue(any("proves 'vibes'" in e for e in r['errors']), r['errors'])

    def test_a_claim_without_an_assertion_is_refused(self):
        # plan D's section 5: "still LCC", a shot judged by eye
        r = check(GOOD.replace('proves evidence', 'proves set-and-kept'))
        self.assertTrue(any('[look] says it proves set-and-kept but has no machine assertion' in e
                            for e in r['errors']), r['errors'])

    def test_capture_counts_only_with_any_or_none(self):
        base = '[start]\nexpect-screen home\n[c]\nsay proves works: replies\nsend /x.syx\n'
        self.assertEqual(check(base + 'capture got.syx 2 any\n')['errors'], [])
        self.assertTrue(check(base + 'capture got.syx 2\n')['errors'])

    def test_a_flash_plan_needs_no_start_check(self):
        r = check('[home screen]\nwait 2\nshot home.png\n', flash='/os/stock.syx', stock=True)
        self.assertEqual(r['errors'], [])
        self.assertTrue(any('flash only' in f for f in r['flags']))

    def test_keys_after_a_stock_flash_are_refused(self):
        r = check(GOOD, flash='/os/stock.syx', stock=True)
        self.assertTrue(any('stock has no remote keys' in e for e in r['errors']), r['errors'])
        self.assertFalse(any('stock' in e for e in check(GOOD, flash='/os/build.syx')['errors']))

    def test_keys_on_an_unknown_os_are_flagged(self):
        self.assertTrue(any('unit_state' in f for f in check(GOOD)['flags']))

    def test_rytm_notes_outside_0_to_11_are_flagged(self):
        plan = GOOD.replace('cc 1 10 64', 'note 14 60 100')
        self.assertTrue(any('note 60' in f for f in check(plan)['flags']))
        self.assertFalse(any('note 60' in f for f in check(plan, unit='a4')['flags']))
        self.assertFalse(any('note 1' in f for f in check(GOOD.replace('cc 1 10 64', 'note 14 1 100'))['flags']))

    def test_a_check_right_after_the_plans_own_save_is_flagged(self):
        # the TX plan: save the open project as ~T TXLIM, then look for that name in the list
        r = check(GOOD.replace('cc 1 10 64', 'save-project 121 "~T TXLIM" YES,YES\nkeys GLOBAL'))
        self.assertTrue(any("follows save-project '~T TXLIM'" in f for f in r['flags']), r['flags'])
        r = check(GOOD.replace('cc 1 10 64', 'save-project 121 "~T TXLIM" YES,YES\ncc 1 10 64'))
        self.assertFalse(any('follows save-project' in f for f in r['flags']))

    def test_listen_needs_three_parts_and_a_check_first(self):
        ok = ('[start]\nexpect-screen home\n[l]\nsay proves evidence: ears\ntap PLAY\nexpect-screen playing\n'
              'listen BD steady. allowed: nothing. needs: the pattern playing\n')
        self.assertEqual(check(ok)['errors'], [])
        self.assertTrue(any('three parts' in e for e in check(ok.replace(' allowed: nothing.', ''))['errors']))
        self.assertTrue(any('no expect-screen before it' in e
                            for e in check(ok.replace('expect-screen playing\n', ''))['errors']))

    def test_emulator_plans_are_not_checked(self):
        self.assertEqual(check('[x]\nshot a.png\n', emulator=True)['errors'], [])


class References(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='plancheck-')

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_missing_and_blank_references_are_flagged(self):
        # analogrun's framebuffer: 128 columns of 8 bytes, bit y of column x (big-endian)
        screen = b''.join(((1 << 20) if x < 64 else 0).to_bytes(8, 'big') for x in range(128))
        with open(os.path.join(self.dir, 'value.bin'), 'wb') as fh:
            fh.write(screen)                          # row 20 lit in columns 0-63
        r = check(GOOD, refs=self.dir)
        self.assertTrue(any('home.bin not recorded yet' in f for f in r['flags']), r['flags'])
        self.assertFalse(any('blank area' in f for f in r['flags']))
        r = check(GOOD.replace('rows=8-31', 'rows=40-50'), refs=self.dir)
        self.assertTrue(any('rows=40-50 compares a blank area' in f for f in r['flags']), r['flags'])
        r = check(GOOD.replace('rows=8-31', 'rows=8-31 cols=64-127'), refs=self.dir)
        self.assertTrue(any('cols=64-127 compares a blank area' in f for f in r['flags']), r['flags'])
        r = check(GOOD.replace('rows=8-31', 'rows=8-31 cols=0-70'), refs=self.dir)
        self.assertFalse(any('blank area' in f for f in r['flags']))

    def test_plans_sharing_a_reference_file_are_refused(self):
        a = dict(name='sweep', refs=self.dir, steps=parse(GOOD))
        b = dict(name='release', refs=self.dir, steps=parse(GOOD.replace('expect-screen value', 'expect-screen other')))
        out = plancheck.shared_refs([a, b])
        self.assertEqual(len(out), 1)
        self.assertIn('home.bin', out[0])
        b['refs'] = os.path.join(self.dir, 'own')
        self.assertEqual(plancheck.shared_refs([a, b]), [])

    def test_save_before_load_is_answered_no(self):
        bad = GOOD + '''
[load]
say proves setup: loads 121
tap YES
expect-screen save-before-load 2 rows=20-40
tap YES
'''
        r = plancheck.check(parse(bad))
        self.assertTrue(any('BEFORE LOADING' in e for e in r['errors']), r['errors'])
        r = plancheck.check(parse(bad.replace('rows=20-40\ntap YES', 'rows=20-40\ntap NO')))
        self.assertFalse(any('BEFORE LOADING' in e for e in r['errors']), r['errors'])


class Templates(unittest.TestCase):
    def test_every_template_passes_the_checker(self):
        folder = os.path.join(HERE, 'templates')
        names = sorted(f for f in os.listdir(folder) if f.endswith('.txt'))
        self.assertGreaterEqual(len(names), 5)
        refs = set()
        for n in names:
            with open(os.path.join(folder, n), encoding='utf-8') as fh:
                text = fh.read()
            flash = re.search(r'^flash\s*=\s*(\S+)', text, re.M)
            r = plancheck.check(parse(text), flash=flash and flash.group(1))
            self.assertEqual(r['errors'], [], n)
            ref = re.search(r'^refs\s*=\s*(\S+)', text, re.M)
            if ref:
                self.assertNotIn(ref.group(1), refs, '%s shares its refs folder' % n)
                refs.add(ref.group(1))

    def test_every_template_step_parses(self):
        # analogrun splits each step with shlex: a stray quote mark (an apostrophe in a say or
        # listen line) stops the run at that step
        import shlex
        folder = os.path.join(HERE, 'templates')
        for n in sorted(f for f in os.listdir(folder) if f.endswith('.txt')):
            with open(os.path.join(folder, n), encoding='utf-8') as fh:
                for name, lines in parse(fh.read()):
                    for l in lines:
                        try:
                            shlex.split(l, comments=True)
                        except ValueError as e:
                            self.fail('%s [%s] %r: %s' % (n, name, l, e))


if __name__ == '__main__':
    unittest.main()
