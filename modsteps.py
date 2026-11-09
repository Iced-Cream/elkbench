# SPDX-License-Identifier: GPL-2.0-or-later
"""Plan steps for the Rytm's Drive Loader: mods tested on a fixed base build instead of a flashed
build per test (decided 2026-11-09).

    mods-put "~T NAME" FILE...     make the ~T folder below /mods if it is not there and upload the
                                   files into it (paths relative to the plan); release removes them
    mods-active PATH...            write /mods/active: the mods the next boot links, in this order
                                   (paths below /mods, in ~T folders). The first one in a session
                                   saves the active that was there; release writes it back
    mods-active none               an empty /mods/active: the next boot links nothing
    mods-active restore            write the saved active back now (before a stock flash, say)
    expect-mods N loaded [ID...]   read /mods/.status: the last boot linked exactly N mods, all
                                   `loaded`, the IDs among them, nothing skipped, and a boot line

The loader links mods only at boot, so a new active takes effect after a reboot: a `power-cycle`
step, or the base build flashed over itself in a plan of its own. The steps run through
modsgate (the deployed write policy decides every write), on the unit under the claim or on the
session's emulator. testplan never sees them: elkbench runs the plan in parts, as at a power
cycle. Nothing here talks to a unit.
"""
import os
import re
import shlex

OPS = ('mods-put', 'mods-active', 'expect-mods')
MAX_FILE = 512 * 1024                 # the loader's staging buffer (DL_STAGING_SIZE)
MAX_ACTIVE = 256                      # DL_MAX_MODS
NAME_CHARS = re.compile(r"[A-Z0-9 +\-=&/#@?%$~]*")


def words(line):
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def op_of(line):
    """-> the mods op of a step line, or None."""
    w = words(line)
    return w[0].lower() if w and w[0].lower() in OPS else None


def is_mods_step(line):
    return op_of(line) is not None


def tname_ok(name):
    """A ~T name (guidelines E1): `~T ` plus at most 15 characters of the editor set."""
    return name.startswith('~T ') and 0 < len(name) - 3 <= 15 and bool(NAME_CHARS.fullmatch(name[3:]))


def parse(line, here):
    """A mods step line -> dict(op, ...), or raises ValueError with the reason. `here` is the plan's
    folder, which relative file names are read from."""
    w = words(line)
    op = w[0].lower()
    if op == 'mods-put':
        if len(w) < 3:
            raise ValueError('mods-put "~T NAME" FILE...: a ~T folder and at least one .elemod')
        folder = w[1].strip('/')
        if '/' in folder or not tname_ok(folder):
            raise ValueError('mods-put %r: the folder is one ~T name below /mods (`~T ` and at most 15 '
                             'characters A-Z 0-9 space + - = & / # @ ? %% $ ~)' % w[1])
        files = []
        for f in w[2:]:
            p = os.path.normpath(f if os.path.isabs(f) else os.path.join(here, f))
            if not p.lower().endswith('.elemod'):
                raise ValueError('mods-put %s: only .elemod files go into /mods' % f)
            if not os.path.isfile(p):
                raise ValueError('mods-put %s: no such file' % p)
            if os.path.getsize(p) > MAX_FILE:
                raise ValueError('mods-put %s: %d bytes, over the loader\'s %d-byte staging buffer'
                                 % (f, os.path.getsize(p), MAX_FILE))
            files.append(p)
        names = [os.path.basename(p) for p in files]
        if len(set(names)) != len(names):
            raise ValueError('mods-put: two files named the same: %s' % ', '.join(sorted(
                n for n in set(names) if names.count(n) > 1)))
        return dict(op=op, folder=folder, files=files)
    if op == 'mods-active':
        if len(w) == 2 and w[1].lower() in ('restore', 'none'):
            return dict(op=op, what=w[1].lower(), paths=[])
        if len(w) < 2:
            raise ValueError('mods-active PATH... | none | restore')
        paths = [p.strip('/') for p in w[1:]]
        if len(paths) > MAX_ACTIVE:
            raise ValueError('mods-active: %d mods, over the loader\'s %d' % (len(paths), MAX_ACTIVE))
        for p in paths:
            if not tname_ok(p.split('/')[0]) or '/' not in p or '..' in p.split('/'):
                raise ValueError('mods-active %s: a test names mods in its own ~T folders (~T NAME/FILE.elemod)'
                                 % p)
        return dict(op=op, what='set', paths=paths)
    if op == 'expect-mods':
        if len(w) < 3 or not w[1].isdigit() or w[2].lower() != 'loaded':
            raise ValueError('expect-mods N loaded [ID...]')
        return dict(op=op, n=int(w[1]), ids=w[3:])
    raise ValueError('%s is not a mods step' % w[0])


def put_files(steps, here):
    """-> sorted [(file, None)] of every file a plan's mods-put steps upload (the caller adds
    sha256s): part of what an emulator pass vouches for."""
    out = set()
    for _, lines in steps:
        for l in lines:
            if op_of(l) == 'mods-put':
                out.update(parse(l, here)['files'])
    return sorted(out)


def status(text):
    """/mods/.status -> dict(records=[(id, state, code, arg, detail)], boot=str|None,
    skipped=str|None, check=str|None)."""
    out = dict(records=[], boot=None, skipped=None, check=None)
    head = ('drive-loader route', 'folder ', 'last:', 'io:')
    for l in text.splitlines():
        l = l.rstrip()
        if not l or l.startswith(head):
            continue
        if l.startswith('check:'):
            out['check'] = l[6:].strip()
        elif l.startswith('boot:'):
            out['boot'] = l[5:].strip()
        elif l.startswith('skipped:'):
            out['skipped'] = l[8:].strip()
        else:
            p = l.split(None, 4)
            if len(p) >= 2:
                out['records'].append((p[0], p[1], p[2] if len(p) > 2 else '', p[3] if len(p) > 3 else '',
                                       p[4] if len(p) > 4 else ''))
    return out


def expect(text, n, ids=()):
    """expect-mods's verdict on /mods/.status text -> (ok, lines)."""
    st = status(text)
    loaded = [r[0] for r in st['records'] if r[1] == 'loaded']
    other = ['%s %s %s %s %s' % r for r in st['records'] if r[1] != 'loaded']
    lines = ['boot: %s' % (st['boot'] or 'none'), '%d records, %d loaded' % (len(st['records']), len(loaded))]
    ok = True
    if st['skipped']:
        lines.append('FAIL: the boot skipped every mod: %s' % st['skipped'])
        ok = False
    if not st['boot']:
        lines.append('FAIL: no boot line: the loader did not run at this boot')
        ok = False
    if other:
        lines.append('FAIL: not loaded: %s' % '; '.join(other[:8]))
        ok = False
    if len(loaded) != n:
        lines.append('FAIL: %d loaded, expected %d' % (len(loaded), n))
        ok = False
    missing = [i for i in ids if i not in loaded]
    if missing:
        lines.append('FAIL: not among the loaded: %s' % ', '.join(missing))
        ok = False
    if ok:
        lines.append('ok expect-mods %d loaded%s' % (n, (' (' + ', '.join(ids) + ')') if ids else ''))
    return ok, lines


def plan_rules(steps, stock_flash=False, here='.'):
    """The plan checker's rules for mods steps -> (errors, flags). steps: [(section, [lines])]."""
    errors, flags = [], []
    seen_active, rebooted, expected = False, False, True
    for name, lines in steps:
        for l in lines:
            w = words(l)
            if not w:
                continue
            op = w[0].lower()
            if op in OPS:
                try:
                    parse(l, here)
                except ValueError as e:
                    errors.append('[%s] %s' % (name, e))
                if stock_flash:
                    errors.append('[%s] %s after a stock flash: stock has no /mods route' % (name, op))
            if op == 'mods-active' and len(w) > 1 and w[1].lower() != 'restore':
                seen_active = True
            elif op == 'power-cycle' and seen_active:
                rebooted, expected = True, False
            elif op == 'expect-mods':
                expected = True
            elif op in ('tap', 'hold', 'release', 'keys', 'turn', 'pad', 'padplay') and rebooted and not expected:
                errors.append('[%s] %s after a power cycle that loads a new /mods/active, with no expect-mods '
                              'before it: a mod the boot skipped would go unseen' % (name, op))
                expected = True
    if seen_active and not rebooted:
        flags.append('mods-active with no power-cycle after it in this plan: the new active loads at the next '
                     'boot (the base flashed over itself in its own plan), and the plan after that boot starts '
                     'with expect-mods')
    return errors, flags
