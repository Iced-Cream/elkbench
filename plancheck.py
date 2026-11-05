# SPDX-License-Identifier: GPL-2.0-or-later
"""The plan checker: static rules for a hardware test plan, read before any emulator time.

check(steps, ...) -> dict(errors, flags, proves). An error makes approve_plan refuse the plan
(and check_plan say it would); a flag is shown in check_plan and in the go request, for the
person to weigh. Nothing here talks to a unit or an emulator.

What a plan must carry (see docs/testing-practices.md, "Plan checker and templates"):

- **A start check.** The first section proves the state the plan starts from with at least
  one `expect-screen`, before any step that writes or changes a value.
- **What each section proves.** Every later section says so with a `say proves KIND: how`
  line. KIND is `setup` (builds state for later checks), `set-and-kept` (a setting is made
  and read back), `works` (the feature's effect is measured) or `evidence` (shots for a
  person to judge). A `set-and-kept` or `works` section needs a machine assertion.
- **A fixed listen format.** `listen WHAT TO HEAR allowed: WHAT THE PERSON MAY DO BY HAND
  needs: THE AUDIBLE PRECONDITION`, with an `expect-screen` earlier in the same section that
  proves the precondition on screen.
- **NO to "save before loading".** An `expect-screen` whose reference name contains
  `save-before-load` must be followed by a NO press.
- **A flash names what it flashes over.** A plan that flashes names the OS on the unit before
  it (`os = FILE`): its emulator pass is of that one transition, run on an emulator booted on
  that file.
- **Checks that can fail (contrast runs).** A section names the starts from which it must
  stop the run: `say contrast START[,START]: why`. START is `fresh` (a fresh emulator: the card
  as shipped) or one of the unit's built-in starts (elkbench's `starts/` folder, such as
  `dirty`, `testslot`, `occupied`). Where the unit has built-in starts, the first section needs
  one: a start check that has never been seen to fail is not yet a check. Each start is named
  in one section at most.
"""
import os
import shlex

PROVES = ('setup', 'set-and-kept', 'works', 'evidence')
# steps whose failure stops the run: machine assertions
ASSERT_OPS = ('expect-screen', 'audio-dropouts', 'audio-compare', 'stats', 'ping')
# steps that change what the unit holds or a value on it (keys only move through the UI)
WRITE_OPS = ('save-project', 'save-kit', 'send', 'cc', 'nrpn', 'note', 'turn', 'pad', 'padplay')
# steps that need a remote-key path in the OS (analoghealth, or a remote-only build)
KEY_OPS = ('tap', 'hold', 'release', 'keys', 'turn', 'pad', 'padplay')
# steps that only move through menus or look: nothing between a save and a check but these
LOOK_OPS = ('tap', 'keys', 'wait', 'shot', 'hold', 'release', 'say')


def words(line):
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return line.split()


def is_assert(w):
    if not w:
        return False
    op = w[0].lower()
    if op == 'capture':                      # capture FILE S [any|none] [short] [nrpn=..] [cc=..] [din]
        return (len(w) > 3 and w[3].lower() in ('any', 'none')
                or any(x.lower().startswith(('nrpn=', 'cc=')) for x in w[3:]))
    return op in ASSERT_OPS


def proves_of(lines):
    """The KIND of a section's `say proves KIND: how` line, '' if it has none, or the bad word."""
    for l in lines:
        w = words(l)
        if len(w) >= 2 and w[0].lower() == 'say' and w[1].lower().rstrip(':') == 'proves':
            return w[2].lower().rstrip(':') if len(w) > 2 else '?'
    return ''


def contrasts_of(steps):
    """-> [(section, start, why)] from `say contrast START[,START]: why` lines, in plan order;
    a start written badly comes back as given, for check() to name."""
    out = []
    for name, lines in steps:
        for l in lines:
            w = words(l)
            if len(w) >= 2 and w[0].lower() == 'say' and w[1].lower().rstrip(':') == 'contrast':
                text = l.strip()[len(w[0]):].strip()[len(w[1]):].strip()
                head, _, why = text.partition(':') if not w[1].endswith(':') else ('', '', text)
                for start in head.split(','):
                    out.append((name, start.strip().lower(), why.strip()))
    return out


def expected_stops(steps):
    """{start: the section a run from that start must stop in} from the plan's contrasts."""
    return {start: name for name, start, _ in contrasts_of(steps) if start}


def listen_parts(text):
    """`WHAT allowed: HAND needs: PRECONDITION` -> (what, allowed, needs); None for a part
    that is missing."""
    low = text.lower()
    a, n = low.find('allowed:'), low.find('needs:')
    if a < 0 or n < 0 or n < a:
        return None
    return text[:a].strip(), text[a + 8:n].strip(), text[n + 6:].strip()


def ref_path(refs, name):
    name = name if name.endswith('.bin') else name + '.bin'
    return name if os.path.isabs(name) else os.path.join(refs or '', name)


def blank_rows(path, span, cols=None):
    """True when a 128x64 1-bit reference has no lit pixel in rows span (inclusive), within
    columns cols when given. The framebuffer is 128 columns of 8 bytes, bit y of column x."""
    try:
        with open(path, 'rb') as fh:
            px = fh.read()
    except OSError:
        return False
    if len(px) != 1024:
        return False
    a, b = span or (0, 63)
    c0, c1 = cols or (0, 127)
    for x in range(c0, c1 + 1):
        col = int.from_bytes(px[x * 8:x * 8 + 8], 'big')
        if any(col >> y & 1 for y in range(a, b + 1)):
            return False
    return True


def span_of(w, key):
    """The A-B of an expect-screen's rows= or cols= option, or None."""
    for x in w[2:]:
        if x.startswith(key + '='):
            try:
                a, b = (int(v) for v in x[len(key) + 1:].split('-'))
                return a, b
            except ValueError:
                return None
    return None


def rows_of(w):
    return span_of(w, 'rows')


def check(steps, unit='rytm', refs=None, flash=None, stock=False, emulator=False, starts=('fresh',),
          os_file=None):
    """steps: [(section name, [step lines])]. flash: the OS file the plan flashes, or None;
    stock: whether that file is the unit's stock OS; starts: the starts this unit's emulator
    runs (`fresh` and its built-in ones); os_file: the OS the plan runs on (`os =`).
    -> dict(errors=[...], flags=[...], proves=[(section, kind)], contrasts=[(section, start, why)])."""
    errors, flags, proves = [], [], []
    contrasts = contrasts_of(steps)
    if emulator:
        return dict(errors=errors, flags=flags, proves=proves, contrasts=contrasts)
    all_words = [(name, words(l)) for name, lines in steps for l in lines]
    asserts = [w for _, w in all_words if is_assert(w)]
    writes = [w for _, w in all_words if w and w[0].lower() in WRITE_OPS]
    # a flash plan: writes nothing after the flash and checks at most that the unit answers
    flash_only = flash and not writes and all(w[0].lower() in ('ping', 'stats') for w in asserts)

    # 1. the start check
    if flash_only:
        flags.append('flash only: nothing checked after the flash but that the unit answers (the flash itself '
                     'is gated as a transition)')
    elif steps:
        first, lines = steps[0]
        if not any(words(l)[:1] == ['expect-screen'] for l in lines):
            errors.append('[%s] has no start check: the first section proves the state the plan starts '
                          'from with an expect-screen' % first)
        for _, w in all_words:
            if not w:
                continue
            if w[0].lower() == 'expect-screen':
                break
            if w[0].lower() in WRITE_OPS:
                errors.append('%s comes before the first expect-screen: prove the start state before '
                              'anything is written' % ' '.join(w[:3]))
                break

    # 2. what each section proves
    for i, (name, lines) in enumerate(steps):
        if i == 0:
            proves.append((name, 'start'))
            continue
        kind = proves_of(lines)
        if flash_only and not kind:
            kind = 'evidence'
        proves.append((name, kind or '?'))
        has = any(is_assert(words(l)) for l in lines)
        if not kind:
            errors.append('[%s] says nothing about what it proves: add `say proves KIND: how` (KIND is '
                          '%s)' % (name, ', '.join(PROVES)))
        elif kind not in PROVES:
            errors.append('[%s] proves %r: KIND is one of %s' % (name, kind, ', '.join(PROVES)))
        elif kind in ('set-and-kept', 'works') and not has:
            errors.append('[%s] says it proves %s but has no machine assertion (expect-screen, '
                          'capture any/none, audio-compare, audio-dropouts, stats, ping): a shot alone is '
                          'judged by eye, so mark it evidence or add a check' % (name, kind))
        elif kind == 'evidence' and not flash_only:
            flags.append('[%s] judged by eye (evidence: shots only)' % name)

    # 3. a check right after the plan's own save
    for name, lines in steps:
        saved = None
        for l in lines:
            w = words(l)
            if not w:
                continue
            op = w[0].lower()
            if op == 'save-project' and len(w) > 2:
                saved = w[2]
            elif op == 'expect-screen' and saved:
                flags.append('[%s] expect-screen %s follows save-project %r with only menu steps between: '
                             'if its rows show that name, it passes on a copy of any project; prove content '
                             'the setup produced (e.g. a new project\'s title row)' % (name, w[1], saved))
                saved = None
            elif op not in LOOK_OPS:
                saved = None

    # 4. references: missing, or comparing blank rows
    for name, lines in steps:
        for l in lines:
            w = words(l)
            if len(w) > 1 and w[0].lower() == 'expect-screen':
                p = ref_path(refs, w[1])
                span, cols = rows_of(w), span_of(w, 'cols')
                if not os.path.isfile(p):
                    flags.append('[%s] reference %s not recorded yet (record_refs in the emulator)'
                                 % (name, os.path.basename(p)))
                elif (span or cols) and blank_rows(p, span, cols):
                    flags.append('[%s] expect-screen %s %s compares a blank area: it passes on any '
                                 'screen that is blank there' % (name, w[1], ' '.join(
                                     x for x in w[2:] if x.startswith(('rows=', 'cols=')))))

    # 5. the build: a flash names the OS it goes over; remote keys need a remote-key path; Rytm hits
    if flash and not os_file:
        errors.append('the plan flashes %s and names no OS it flashes over: add os = FILE (the OS on the unit '
                      'before the flash; its emulator pass is of that transition, booted on that file)'
                      % os.path.basename(flash))
    if flash and stock:
        bad = sorted({w[0].lower() for _, w in all_words if w and w[0].lower() in KEY_OPS + ('dump', 'stats')})
        if bad:
            errors.append('the plan flashes the stock OS and then uses %s: stock has no remote keys or '
                          'reads; flash a build with analoghealth, or a remote-only build' % ', '.join(bad))
    if not flash and any(w and w[0].lower() in KEY_OPS + ('dump',) for _, w in all_words):
        flags.append('key or dump steps on the OS the unit has: check with unit_state that it has a '
                     'remote-key path (analoghealth or a remote-only build)')
    if unit == 'rytm':
        for name, w in all_words:
            if len(w) > 2 and w[0].lower() == 'note':
                try:
                    n = int(w[2], 0)
                except ValueError:
                    continue
                if not 0 <= n <= 11:
                    flags.append('[%s] note %s: the Rytm starts a track only from notes 0-11 on its auto '
                                 'channel, so this plays nothing' % (name, w[2]))

    # 6. contrasts: each names a start the unit has, once; the start check has one
    seen = {}
    for name, start, why in contrasts:
        if not start or start not in starts:
            errors.append('[%s] say contrast %s: the %s emulator starts are %s' % (
                name, start or '(none named)', unit, ', '.join(starts)))
        elif start in seen and seen[start] != name:
            errors.append('[%s] and [%s] both name contrast %s: a run from one start stops in one section'
                          % (seen[start], name, start))
        else:
            seen.setdefault(start, name)
        if start and not why:
            errors.append('[%s] say contrast %s: say why it stops there (`say contrast %s: why`)'
                          % (name, start, start))
    if steps and not flash:                  # a flash plan starts on the OS before its flash
        first = steps[0][0]
        if not any(n == first for n, _, _ in contrasts):
            if len(starts) > 1:
                errors.append('[%s] has no contrast: name a start from which the start check stops the run '
                              '(`say contrast START: why`; starts: %s), so it is seen to fail'
                              % (first, ', '.join(starts)))
            else:
                flags.append('[%s] start check never seen to fail (no contrast; the %s emulator has only '
                             'the fresh start)' % (first, unit))
        for (name, kind) in proves[1:]:
            if kind in ('set-and-kept', 'works') and not any(n == name for n, _, _ in contrasts):
                flags.append('[%s] never seen to fail: no `say contrast` start stops the run there' % name)

    # 7. listening steps
    for name, lines in steps:
        seen_check = False
        for l in lines:
            w = words(l)
            if not w:
                continue
            if w[0].lower() == 'expect-screen':
                seen_check = True
            elif w[0].lower() == 'listen':
                text = l.strip()[len(w[0]):].strip()
                if not listen_parts(text):
                    errors.append('[%s] listen needs three parts: what to hear, then `allowed:` what %s may '
                                  'do by hand (or nothing), then `needs:` the audible precondition'
                                  % (name, 'the person'))
                if not seen_check:
                    errors.append('[%s] listen has no expect-screen before it in its section: prove its '
                                  'precondition (e.g. the play indicator) on screen first' % name)
    # 8. "SAVE ... BEFORE LOADING?" is always answered NO: a plan names that check save-before-load*
    for name, lines in steps:
        pending = None
        for l in lines:
            w = words(l)
            if not w:
                continue
            op = w[0].lower()
            if op == 'expect-screen' and len(w) > 1 and 'save-before-load' in w[1].lower():
                pending = w[1]
            elif pending and op in ('tap', 'keys'):
                first = (w[1].split(',')[0] if len(w) > 1 else '').upper()
                if first != 'NO':
                    errors.append('[%s] after expect-screen %s the plan presses %s: "SAVE ... BEFORE LOADING?" '
                                  'is always answered NO' % (name, pending, first or '?'))
                pending = None
    return dict(errors=errors, flags=flags, proves=proves, contrasts=contrasts)


def shared_refs(infos):
    """Plans given together that compare the same reference file -> error lines. Recording one
    plan's references would change the other's, and its pass would no longer count."""
    seen, out = {}, []
    for info in infos:
        for _, lines in info['steps']:
            for l in lines:
                w = words(l)
                if len(w) > 1 and w[0].lower() == 'expect-screen':
                    p = os.path.normcase(os.path.abspath(ref_path(info['refs'], w[1])))
                    other = seen.setdefault(p, info['name'])
                    if other != info['name']:
                        out.append('%s and %s both compare %s: give each plan its own references'
                                   % (other, info['name'], os.path.basename(p)))
    return sorted(set(out))


def go_lines(result):
    """What a go request says about a plan's checks: the sections by kind, and the flags."""
    by = {}
    for name, kind in result['proves']:
        by.setdefault(kind, []).append(name)
    out = ['  proves: ' + '; '.join('%s: %s' % (k, ', '.join('[%s]' % n for n in by[k]))
                                    for k in ('start',) + PROVES if k in by)] if by else []
    out += ['  contrast: from %s it stops in [%s] (%s)' % (start, name, why)
            for name, start, why in result.get('contrasts') or []]
    return out + ['  CHECK: ' + f for f in result['flags']]
