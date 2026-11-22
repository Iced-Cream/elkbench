# SPDX-License-Identifier: GPL-2.0-or-later
"""Plan steps for the Drive Loader (the Rytm's, and the A4 MKII's): mods tested on a fixed base build
instead of a flashed build per test (decided 2026-11-09).

    mods-put "~T NAME" FILE...     make the ~T folder below /mods if it is not there and upload the
                                   files into it (paths relative to the plan); release removes them.
                                   Each file is saved in place or the step fails: a file in use (a
                                   0.12 loader's .status `payload` line) is refused before the upload,
                                   and an upload the unit kept as .new for the next boot fails it
                                   (use --pending for that; decided 2026-11-22)
    mods-put "~T NAME" --new FILE...
                                   the folder must not be in /mods; no mkdir: the upload names it,
                                   and the loader makes it (an upload into a missing folder)
    mods-put "~T NAME" --pending FILE...
                                   an upload over files in use (drive-loader-ar2 0.12: a running
                                   mod's file whose payload the unit keeps): each FILE must already
                                   be in the folder with no FILE.new beside it, the route must keep
                                   the upload as FILE.new for the next boot (`pending restart:`;
                                   modsgate exits 0 on a 0.12 pin, 2 on an older one), and both
                                   FILE and FILE.new must be listed after. A put that saves in
                                   place fails this form; the plain form is unchanged
    mods-active PATH...            write /mods/active: the mods the next boot links, in this order
                                   (paths below /mods, in ~T folders). The first one in a session
                                   saves the active that was there; release writes it back
    mods-active none               an empty /mods/active: the next boot links nothing
    mods-active restore            write the saved active back now (before a stock flash, say)
    expect-mods N loaded [ID...]   read /mods/.status: the last boot linked exactly N mods, all
                                   `loaded`, the IDs among them, nothing skipped, and a boot line.
                                   ID=K: that mod loaded with exactly K boot-linked sites applied
                                   (+Drive v2 `site` lines). A v2 `store:` line must not say
                                   `write failed`. Only `ID STATE CODE ARG [DETAIL]` lines are mod
                                   records; header, `boot:`, `store:`, `site`, the 0.12 payload
                                   lines and any line a later loader adds are not
    expect-status TEXT...          read /mods/.status: each TEXT is found inside one of its lines
                                   (a plain, case-sensitive substring; quote TEXT with spaces, e.g.
                                   expect-status "updates at boot: 1 applied" "pending /mods/~T PAY/x.elemod").
                                   On a failure the whole .status is shown
    mods-remove "~T NAME"          remove a ~T folder this session's mods-put made, with the files
                                   it put there, and list /mods to show it gone: before a stock
                                   flash, so stock never boots with a run's .elemod on /mods
    mods-clear-refused "~T NAME" FILE
                                   a Data RPC Clear (USB delete, modsgate rm) of a file in use that
                                   this session's mods-put made: passes only when the unit refuses
                                   it as in use, the file is still listed at the same size after,
                                   and its `payload` line is still in /mods/.status (drive-loader
                                   0.12: a running mod's file whose payload the unit keeps)

Both loaders serve /mods through the same Data RPC route, so the steps and modsgate are the same
on both units. The A4 has no +Drive file system: its loader keeps /mods (folders 3 deep, as the
Rytm's) in its own store in free +Drive sectors and writes an upload straight to them, so its file
limit is the mods' area in core_ddr rather than a staging buffer (MAX_FILE).

The loader links mods only at boot, so a new active takes effect after a reboot: a `power-cycle`
step, or the base build flashed over itself in a plan of its own. The steps run through
modsgate (the deployed write policy decides every write), on the unit under the claim or on the
session's emulator. testplan never sees them: elkbench runs the plan in parts, as at a power
cycle. Nothing here talks to a unit.
"""
import os
import re
import shlex

OPS = ('mods-put', 'mods-active', 'expect-mods', 'expect-status', 'mods-remove', 'mods-clear-refused')
# the largest .elemod a mods-put uploads, per unit with a Drive Loader
MAX_FILE = {
    'rytm': 512 * 1024,               # the loader's staging buffer (DL_STAGING_SIZE)
    'a4': 0x47C00000 - 0x45650000,    # core-a4mk2's mods area in core_ddr (37.7 MB); uploads go
}                                     # straight to the loader's sector store, no staging buffer
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


def parse(line, here, unit='rytm'):
    """A mods step line -> dict(op, ...), or raises ValueError with the reason. `here` is the plan's
    folder, which relative file names are read from; `unit` the unit the plan runs on."""
    w = words(line)
    op = w[0].lower()
    if unit not in MAX_FILE:
        raise ValueError('%s: the %s has no Drive Loader (mods steps run on %s)'
                         % (op, unit, ' and '.join(sorted(MAX_FILE))))
    if op == 'mods-put':
        if len(w) < 3:
            raise ValueError('mods-put "~T NAME" FILE...: a ~T folder and at least one .elemod')
        folder = w[1].strip('/')
        flags = [x for x in w[2:] if x in ('--new', '--pending')]
        if len(flags) > 1 or any(x.startswith('--') and x not in ('--new', '--pending') for x in w[2:]):
            raise ValueError('mods-put "~T NAME" [--new | --pending] FILE...: one of --new or --pending at most')
        if flags and w[2] != flags[0]:
            raise ValueError('mods-put "~T NAME" %s FILE...: %s comes before the files' % (flags[0], flags[0]))
        new, pending = flags == ['--new'], flags == ['--pending']
        if flags:
            w = w[:2] + w[3:]
            if len(w) < 3:
                raise ValueError('mods-put "~T NAME" %s FILE...: at least one .elemod' % flags[0])
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
            if os.path.getsize(p) > MAX_FILE[unit]:
                raise ValueError('mods-put %s: %d bytes, over the %s loader\'s %d-byte %s'
                                 % (f, os.path.getsize(p), unit, MAX_FILE[unit],
                                    'mods area' if unit == 'a4' else 'staging buffer'))
            files.append(p)
        names = [os.path.basename(p) for p in files]
        if len(set(names)) != len(names):
            raise ValueError('mods-put: two files named the same: %s' % ', '.join(sorted(
                n for n in set(names) if names.count(n) > 1)))
        return dict(op=op, folder=folder, files=files, new=new, pending=pending)
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
        for i in w[3:]:
            name, eq, k = i.partition('=')
            if not name or (eq and not k.isdigit()):
                raise ValueError('expect-mods N loaded [ID...]: ID or ID=K (K sites applied)')
        return dict(op=op, n=int(w[1]), ids=w[3:])
    if op == 'expect-status':
        if len(w) < 2 or not all(t.strip() for t in w[1:]):
            raise ValueError('expect-status TEXT...: at least one non-empty text that a /mods/.status line holds')
        return dict(op=op, texts=w[1:])
    if op == 'mods-remove':
        folder = w[1].strip('/') if len(w) == 2 else ''
        if not folder or '/' in folder or not tname_ok(folder):
            raise ValueError('mods-remove "~T NAME": one ~T folder below /mods that a mods-put made')
        return dict(op=op, folder=folder)
    if op == 'mods-clear-refused':
        folder = w[1].strip('/') if len(w) == 3 else ''
        name = w[2] if len(w) == 3 else ''
        if not folder or '/' in folder or not tname_ok(folder) or not name.lower().endswith('.elemod')                 or '/' in name or name.startswith('.'):
            raise ValueError('mods-clear-refused "~T NAME" FILE: one .elemod file (a bare name) in a ~T folder '
                             'below /mods that a mods-put made')
        return dict(op=op, folder=folder, file=name)
    raise ValueError('%s is not a mods step' % w[0])


def put_files(steps, here, unit='rytm'):
    """-> sorted [(file, None)] of every file a plan's mods-put steps upload (the caller adds
    sha256s): part of what an emulator pass vouches for."""
    out = set()
    for _, lines in steps:
        for l in lines:
            if op_of(l) == 'mods-put':
                out.update(parse(l, here, unit)['files'])
    return sorted(out)


# a boot linker record's state word (drive-loader route.c dl_state_names; '?' for a state past them)
RECORD_STATES = ('-', 'loaded', 'refused', 'dropped', '?')


def _record(l):
    """A boot linker record `ID STATE CODE ARG [DETAIL]` (CODE and ARG decimal) -> its 5 fields, else None."""
    p = l.split(None, 4)
    if len(p) >= 4 and p[1] in RECORD_STATES and p[2].isdigit() and p[3].isdigit():
        return (p[0], p[1], p[2], p[3], p[4] if len(p) > 4 else '')
    return None


def status(text):
    """/mods/.status -> dict(records=[(id, state, code, arg, detail)], boot=str|None,
    skipped=str|None, check=str|None, store=str|None, sites={id: n}, payloads=dict, unknown=[line])."""
    out = dict(records=[], boot=None, skipped=None, check=None, store=None, sites={},
               payloads=dict(summary=None, updates=None, entries=[], pending=[]), unknown=[])
    # `stage:` (top-staging loaders): the live stage, `stage: N bytes at 0xADDR, free tail|floor`;
    # the A4 MKII's loader names itself `drive-loader-a4mk2 route` and gives its sector store's
    # `room: N bytes in one run, M free; directory generation G` in place of a stage
    head = ('drive-loader route', 'drive-loader-a4mk2 route', 'folder ', 'last:', 'io:', 'stage:', 'room:')
    pay = out['payloads']
    for l in text.splitlines():
        l = l.rstrip()
        if not l or l.startswith(head):
            continue
        # a record's shape decides first, so a mod whose id is a metadata word (`payload`, `pending`)
        # still counts as a mod; no metadata line has a state word second and two numbers after it
        r = _record(l)
        if r:
            out['records'].append(r)
            continue
        if l.startswith('check:'):
            out['check'] = l[6:].strip()
        elif l.startswith('boot:'):
            out['boot'] = l[5:].strip()
        elif l.startswith('skipped:'):
            out['skipped'] = l[8:].strip()
        elif l.startswith('store:'):              # +Drive v2 storage: id, fields, parked, kept|written|write failed
            out['store'] = l[6:].strip()
        elif l.startswith('site ') and len(l.split()) == 4:   # +Drive v2: site ID 0xADDR LEN, one per applied site
            sid = l.split()[1]
            out['sites'][sid] = out['sites'].get(sid, 0) + 1
        # drive-loader-ar2 0.12's payload table (payload.c dl_payload_status): `payloads: N kept, M lost`,
        # `updates at boot: A applied, B failed` (only after a boot that swapped a <name>.new in),
        # `payload ID8 LEN PATH` per entry and `pending PATH` per upload waiting for the next boot
        elif l.startswith('payloads:'):
            pay['summary'] = l[9:].strip()
        elif l.startswith('updates at boot:'):
            pay['updates'] = l[16:].strip()
        elif l.startswith('payload '):
            p = l.split(None, 3)
            pay['entries'].append(tuple(p[1:]))
        elif l.startswith('pending '):
            pay['pending'].append(l[8:].strip())
        else:                                     # a later loader's line: kept and shown, never a mod
            out['unknown'].append(l)
    return out


def expect_status(text, texts):
    """expect-status's verdict on /mods/.status text -> (ok, lines): each of `texts` inside one line
    (a plain, case-sensitive substring). A failure shows the whole .status."""
    have = [l.rstrip() for l in text.splitlines()]
    lines, ok = [], True
    for t in texts:
        hit = next((l for l in have if t in l), None)
        if hit is None:
            lines.append('FAIL: no /mods/.status line holds %r' % t)
            ok = False
        else:
            lines.append('found %r: %s' % (t, hit))
    if ok:
        lines.append('ok expect-status: %d text(s) found' % len(texts))
    else:
        lines += full_status(text)
    return ok, lines


def full_status(text):
    """-> the whole .status, indented, for a failed step's output."""
    return ['/mods/.status as read:'] + ['  | ' + l.rstrip() for l in text.splitlines()]


def expect(text, n, ids=()):
    """expect-mods's verdict on /mods/.status text -> (ok, lines)."""
    st = status(text)
    want_sites = {}
    plain = []
    for i in ids:
        name, eq, k = i.partition('=')
        if eq:
            want_sites[name] = int(k)
        plain.append(name)
    ids = plain
    loaded = [r[0] for r in st['records'] if r[1] == 'loaded']
    other = ['%s %s %s %s %s' % r for r in st['records'] if r[1] != 'loaded']
    lines = ['boot: %s' % (st['boot'] or 'none'), '%d records, %d loaded' % (len(st['records']), len(loaded))]
    if st['store']:
        lines.append('store: %s' % st['store'])
    if st['sites']:
        lines.append('sites applied: %s' % ', '.join('%s %d' % kv for kv in sorted(st['sites'].items())))
    pay = st['payloads']
    if pay['summary'] is not None:
        lines.append('payloads: %s' % pay['summary'])
    if pay['updates'] is not None:
        lines.append('updates at boot: %s' % pay['updates'])
    for e in pay['entries']:
        lines.append('payload %s' % ' '.join(e))
    for p in pay['pending']:
        lines.append('pending %s' % p)
    if st['unknown']:
        lines.append('not mod records, not judged: %s' % '; '.join(st['unknown'][:8]))
    ok = True
    if st['store'] and st['store'].endswith('write failed'):
        lines.append('FAIL: /mods/.store write failed')
        ok = False
    stray = sorted(i for i in st['sites'] if i not in loaded)
    if stray:
        lines.append('FAIL: sites applied for a mod not loaded: %s' % ', '.join(stray))
        ok = False
    for i, k in sorted(want_sites.items()):
        if st['sites'].get(i, 0) != k:
            lines.append('FAIL: %s has %d sites applied, expected %d' % (i, st['sites'].get(i, 0), k))
            ok = False
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
    else:
        lines += full_status(text)
    return ok, lines


def plan_rules(steps, stock_flash=False, here='.', unit='rytm'):
    """The plan checker's rules for mods steps -> (errors, flags). steps: [(section, [lines])]."""
    errors, flags = [], []
    seen_active, rebooted, expected = False, False, True
    put, active = {}, set()          # ~T folders put and not removed (-> section); folders the active names
    for name, lines in steps:
        for l in lines:
            w = words(l)
            if not w:
                continue
            op = w[0].lower()
            step = None
            if op in OPS:
                try:
                    step = parse(l, here, unit)
                except ValueError as e:
                    errors.append('[%s] %s' % (name, e))
                if stock_flash:
                    errors.append('[%s] %s after a stock flash: stock has no /mods route' % (name, op))
            if op == 'mods-active' and len(w) > 1 and w[1].lower() != 'restore':
                seen_active = True
            if step and step['op'] == 'mods-put':
                put.setdefault(step['folder'], name)
            elif step and step['op'] == 'mods-active':
                active = set() if step['what'] in ('restore', 'none') else {p.split('/')[0] for p in step['paths']}
            elif step and step['op'] == 'mods-remove':
                if step['folder'] in active:
                    errors.append('[%s] mods-remove %s while /mods/active names it: mods-active restore (or none) '
                                  'first, so the next boot does not look for a removed mod' % (name, step['folder']))
                put.pop(step['folder'], None)
            if op == 'power-cycle' and seen_active:
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
    for folder, name in sorted(put.items()):
        flags.append('mods-put %s [%s] with no mods-remove after it in this plan: a later plan removes it '
                     '(mods-remove "%s") before the stock flash, or stock boots with the .elemod on /mods'
                     % (folder, name, folder))
    return errors, flags
