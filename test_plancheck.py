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

    def test_an_on_fail_section_reads_last_and_needs_no_proves_line(self):
        readout = '[on fail: hang readout]\ndump 0x40000000 4 a.bin\nshot hang.png\nstats\n'
        r = check(GOOD + readout)
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['proves'][-1], ('on fail: hang readout', 'readout'))
        self.assertTrue(any('[on fail: hang readout] run only after a failure' in f for f in r['flags']), r['flags'])
        self.assertIn('readout: [on fail: hang readout]', plancheck.go_lines(r)[0])
        r = check(GOOD + '[on fail]\ntap NO\nnrpn 1 1 35 5376\n')
        self.assertTrue(any('[on fail] has nrpn, tap: an [on fail] section reads only' in e for e in r['errors']),
                        r['errors'])
        r = check(GOOD + readout + '[later]\nsay proves evidence: x\nshot x.png\n')
        self.assertTrue(any('[later] comes after an [on fail] section' in e for e in r['errors']), r['errors'])
        self.assertFalse(plancheck.on_fail('on failure'))
        self.assertTrue(plancheck.on_fail('On fail'))

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

    def test_short_capture_counts_with_a_matcher(self):
        base = '[start]\nexpect-screen home\n[c]\nsay proves works: knob out\nturn A 4\n'
        self.assertEqual(check(base + 'capture k.txt 2 nrpn=1:0:112 din\n')['errors'], [])
        self.assertEqual(check(base + 'capture k.txt 2 short cc=1:20\n')['errors'], [])
        self.assertEqual(check(base + 'capture k.txt 2 any short\n')['errors'], [])
        self.assertTrue(check(base + 'capture k.txt 2 short\n')['errors'])

    def test_a_flash_plan_needs_no_start_check(self):
        r = check('[home screen]\nwait 2\nshot home.png\n', flash='/os/stock.syx', stock=True,
                  os_file='/os/build.syx')
        self.assertEqual(r['errors'], [])
        self.assertTrue(any('flash only' in f for f in r['flags']))

    def test_a_flash_plan_names_the_os_it_flashes_over(self):
        r = check('[home screen]\nwait 2\nshot home.png\n', flash='/os/stock.syx', stock=True)
        self.assertTrue(any('names no OS it flashes over' in e for e in r['errors']), r['errors'])
        self.assertFalse(any('flashes over' in e for e in check(GOOD)['errors']))

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


class Contrasts(unittest.TestCase):
    STARTS = ('fresh', 'dirty', 'occupied', 'testslot')

    def test_a_start_check_needs_a_contrast_where_the_unit_has_starts(self):
        r = check(GOOD, starts=self.STARTS)
        self.assertTrue(any('[start] has no contrast' in e for e in r['errors']), r['errors'])
        r = check(GOOD)                                     # only `fresh`: a flag, not an error
        self.assertEqual(r['errors'], [])
        self.assertTrue(any('start check never seen to fail' in f for f in r['flags']), r['flags'])

    def test_contrasts_are_read_and_listed(self):
        text = GOOD.replace('tap NO\n', 'tap NO\nsay contrast fresh, testslot: a default project shows no value\n', 1)
        r = check(text, starts=self.STARTS)
        self.assertEqual(r['errors'], [])
        self.assertEqual(r['contrasts'], [('start', 'fresh', 'a default project shows no value'),
                                          ('start', 'testslot', 'a default project shows no value')])
        self.assertEqual(plancheck.expected_stops(parse(text)), {'fresh': 'start', 'testslot': 'start'})
        self.assertTrue(any('[set the value] never seen to fail' in f for f in r['flags']), r['flags'])
        self.assertIn('  contrast: from testslot it stops in [start] (a default project shows no value)',
                      plancheck.go_lines(r))

    def test_bad_contrasts_are_refused(self):
        for line, why in (('say contrast nowhere: x', 'the rytm emulator starts are'),
                          ('say contrast dirty', 'say why it stops there'),
                          ('say contrast: x', '(none named)')):
            r = check(GOOD.replace('tap NO\n', 'tap NO\n%s\n' % line, 1), starts=self.STARTS)
            self.assertTrue(any(why in e for e in r['errors']), (line, r['errors']))
        twice = GOOD.replace('tap NO\n', 'tap NO\nsay contrast dirty: a\n', 1).replace(
            'cc 1 10 64', 'say contrast dirty: b\ncc 1 10 64')
        r = check(twice, starts=self.STARTS)
        self.assertTrue(any('both name contrast dirty' in e for e in r['errors']), r['errors'])


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


def template_files():
    """Every template under templates/, its subfolders (one per unit) included, as (label, path)."""
    root = os.path.join(HERE, 'templates')
    out = []
    for d, subs, files in os.walk(root):
        subs.sort()
        for f in sorted(files):
            if f.endswith('.txt'):
                path = os.path.join(d, f)
                out.append((os.path.relpath(path, root).replace(os.sep, '/'), path))
    return out


def unit_starts(unit):
    """('fresh', and the unit's built-in starts in starts/UNIT/)."""
    folder = os.path.join(HERE, 'starts', unit)
    names = sorted(f[:-4] for f in os.listdir(folder) if f.endswith('.txt')) if os.path.isdir(folder) else []
    return ('fresh',) + tuple(names)


class Templates(unittest.TestCase):
    def test_every_template_passes_the_checker(self):
        found = template_files()
        self.assertGreaterEqual(len(found), 5)
        refs = {}
        for n, path in found:
            with open(path, encoding='utf-8') as fh:
                text = fh.read()
            flash = re.search(r'^flash\s*=\s*(\S+)', text, re.M)
            over = re.search(r'^os\s*=\s*(\S+)', text, re.M)
            unit = re.search(r'^unit\s*=\s*(\S+)', text, re.M)
            unit = unit.group(1) if unit else 'rytm'
            r = plancheck.check(parse(text), unit=unit, flash=flash and flash.group(1),
                                starts=unit_starts(unit), os_file=over and over.group(1))
            self.assertEqual(r['errors'], [], n)
            # one refs folder per template, across every unit's folder: a plan copied from one
            # keeps its refs line, and two plans in a run must never compare the same file
            ref = re.search(r'^refs\s*=\s*(\S+)', text, re.M)
            if ref:
                self.assertNotIn(ref.group(1), refs, '%s shares its refs folder with %s'
                                 % (n, refs.get(ref.group(1))))
                refs[ref.group(1)] = n

    def test_every_template_step_parses(self):
        # analogrun splits each step with shlex: a stray quote mark (an apostrophe in a say or
        # listen line) stops the run at that step
        import shlex
        for n, path in template_files():
            with open(path, encoding='utf-8') as fh:
                for name, lines in parse(fh.read()):
                    for l in lines:
                        try:
                            shlex.split(l, comments=True)
                        except ValueError as e:
                            self.fail('%s [%s] %r: %s' % (n, name, l, e))

    def test_every_start_parses(self):
        import shlex
        folder = os.path.join(HERE, 'starts')
        # the start files elkbench reads: <unit>/<start>.txt (a run of one leaves logs in <unit>/runs/)
        files = [os.path.join(folder, u, f) for u in os.listdir(folder) if os.path.isdir(os.path.join(folder, u))
                 for f in os.listdir(os.path.join(folder, u)) if f.endswith('.txt')]
        self.assertGreaterEqual(len(files), 3)
        for p in files:
            with open(p, encoding='utf-8') as fh:
                text = fh.read()
            self.assertRegex(text, r'(?m)^emulator = yes$', p)
            for name, lines in parse(text):
                for l in lines:
                    shlex.split(l, comments=True)


class BootState(unittest.TestCase):
    def test_what_a_plan_leaves_for_the_next_boot(self):
        m1 = [('mods', ['mods-put "~T BASE" a.elemod', 'mods-active "~T BASE/a.elemod"']),
              ('settings', ['say settings-changed: CLOCK SOURCE'])]
        left = plancheck.boot_state(m1)
        self.assertEqual(left, ['/mods/active', 'SETTINGS: CLOCK SOURCE', '~T BASE in /mods'])
        m2 = [('back', ['expect-mods 1 loaded a', 'mods-active restore', 'mods-remove "~T BASE"',
                        'say settings-back: CLOCK SOURCE'])]
        self.assertEqual(plancheck.boot_state(m2, left), [])
        self.assertEqual(plancheck.boot_state([('x', ['mods-active restore', 'mods-active none'])]), ['/mods/active'])

    def test_the_checker_flags_it(self):
        steps = parse(GOOD) + [('mods', ['say proves setup: a mod set', 'mods-active none'])]
        flags = plancheck.check(steps)['flags']
        self.assertTrue(any('leaves boot state changed (/mods/active)' in f for f in flags), flags)
        self.assertFalse(any('boot state' in f for f in plancheck.check(parse(GOOD))['flags']))


if __name__ == '__main__':
    unittest.main()
