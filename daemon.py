"""elkbench daemon: the one owner of the Elektron units' test toolchain on this PC.

Every MCP client session reaches it through its own small launcher (launcher.py, an MCP stdio
server); the daemon runs once per PC, so one process owns each MIDI port, the Overbridge
connection and the audio interface at a time. It wraps the existing tools without changing
them: analogrun.py / testplan.py (analoghealth), elkflash, the bundled audio scripts (audio/:
asio_*, audiocheck.py), obtr.py and elklock.

Rules it enforces:
- every unit-touching tool refuses unless the calling session holds the unit's elklock claim
  (Overbridge Total Recall: all three units, since the Engine is shared); emulator and offline
  tools need none;
- writes on a unit go through analogrun, so a local write policy installed for analoghealth
  (writepolicy.py) applies; with require_write_policy, hardware tools refuse without one;
  files land in the unit's own evidence folder;
- emulator first: a hardware plan or flash needs a passing emulator run of the same steps,
  reference screens and OS file, recorded by this daemon;
- one go per run: a hardware plan, flash or recall restore needs an approval token, which only
  a click on a dialog on this PC's desktop creates (approve_plan); each item is used once;
- release writes the elklock handoff note and a row in the unit's runs\\test-log.md, and asks
  for the stock OS back first;
- never restarts Overbridge, never breaks a claim, never power-cycles a unit;
- keeps the PC from sleeping while a unit is claimed, a run or emulator is going or a go is
  being asked (keep_awake), so a run or a remote session isn't cut off halfway.

Stdlib only. Windows and Linux (no Overbridge Total Recall on Linux: there is no Engine).

    python daemon.py              (started by launcher.py; one per PC)
    python daemon.py --check      (exit 0: this code imports and reads the config)
"""
import ctypes as C
import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import socket
import socketserver
import struct
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import batch
import plancheck

WINDOWS = os.name == 'nt'
if WINDOWS:
    import ctypes.wintypes as W

VERSION = 1
HERE = os.path.dirname(os.path.abspath(__file__))
# Windows: %LOCALAPPDATA%\elkbench holds both; Linux: XDG state and config folders
STATE = os.environ.get('ELKBENCH_DIR') or os.path.join(
    os.environ.get('LOCALAPPDATA') if WINDOWS else
    os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'), 'elkbench')
CONFIG = os.environ.get('ELKBENCH_CONFIG') or (os.path.join(STATE, 'config.toml') if WINDOWS else os.path.join(
    os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'elkbench', 'config.toml'))
NOWIN = {'creationflags': 0x08000000} if WINDOWS else {}     # CREATE_NO_WINDOW
EXE = '.exe' if WINDOWS else ''
PLAN_FORMATS = ('1',)                   # `format = N` in a plan; none means 1
APPROVAL_TTL = 2 * 3600
DIALOG_TIMEOUT = 30 * 60
SNAPSHOT_DAYS, SNAPSHOT_MAX = 7, 20             # setup snapshots: unused days, how many at most
OB_CHANNELS = ('Main L', 'Main R', 'Track 1 L', 'Track 1 R', 'Track 2 L', 'Track 2 R', 'Track 3 L',
               'Track 3 R', 'Track 4 L', 'Track 4 R', 'Input L', 'Input R')   # the Digitone's Overbridge order
DIGI_PARALLEL_MAX = 3                          # digiemu runs one batch may have going at once
AUDIO_WAIT_EMULATOR = 0.5                       # s an emulator twin waits for `wait N audio`
POWER_OFF_WAIT = 60                             # s analog-emu has to save and exit at a power-cycle
POWER_OFF_MARK = 'power off: working state saved'    # its log line after a power-off save
# in the binary of an analog-emu that has --power-off (the line above is put together at run time)
POWER_OFF_BUILD_MARK = b'power off: no --card, nothing kept'
GATE_PLANS_MAX = 16                             # plans one gate batch may take
KNOWN_STARTS = ('a fresh emulator', 'a setup snapshot')    # pass starts a gate batch may keep
# Built-in starts (proposal B): a hardware plan's go needs a run from each of these the unit has
# (in the starts folder), which passes or stops where the plan's `say contrast` says. A unit whose
# release empties its test slots (`clear_at_release`) is never found with them full, but always
# the way a release leaves it: its plans run from `testslot-cleared` instead of `occupied`.
REQUIRED_STARTS = ('dirty', 'occupied')
REQUIRED_STARTS_CLEARED = ('dirty', 'testslot-cleared')
STATUS_WAIT, STATUS_WAIT_MAX = 300, 600     # s that status run_id waits for a run to end
IDLE_EXIT = 30 * 60
# What the units are; config.toml can override any key, and adds where evidence goes.
UNIT_FACTS = {
    'rytm': dict(port='Analog Rytm', emu='rytm-emu usb out,rytm-emu usb in', emu_exe='rytm' + EXE,
                 asio='Elektron Analog Rytm MKII', engine='Analog Rytm', runner=True, stock=[], no_stats=[],
                 din=None),
    'a4': dict(port='Analog Four', emu='a4-emu usb out,a4-emu usb in', emu_exe='a4' + EXE,
               asio='Elektron Analog Four MKII', engine='Analog Four', runner=True, stock=[], no_stats=[],
               din=None),
    'digitone': dict(port='Digitone', emu='digiemu', asio='Elektron Digitone', engine='Digitone',
                     runner='digirun', stock=[], no_stats=[], din=None,   # digirun: steps and screens, no plans
                     emulator='digiemu'),              # digiemu over TCP; without a build, emu.fwcheck
    # Untested on hardware. digirun drives it as it does the Digitone, and digiemu runs it over
    # TCP the same way (it needs a digiemu that resolves the Digitakt mk1 1.54 USB symbols).
    # flash_chunk: in digiemu, OS 1.54 halts during an upgrade sent in elkflash's default 30 KB
    # chunks (the OS stops before committing it) and finishes with 4 KB ones, so
    # every Digitakt flash, emulator and hardware, sends 4 KB chunks. digihealth turns encoders
    # on the Digitone only; no Overbridge audio name or Total Recall.
    'digitakt': dict(port='Digitakt', emu='digiemu', asio=None, engine=None, runner='digirun', stock=[],
                     no_stats=[], din=None, emulator='digiemu', no_steps=['turn'], flash_chunk=4096,
                     no_emu_audio='its digiemu stream was silent with notes playing, and its Overbridge '
                                  'channels are not the Digitone\'s twelve'),
}
DATA_OPS = ('ls', 'get', 'put', 'clear', 'roundtrip')
# Digitone (digirun) steps that do not exist: no key-driven kit save, as a test project there is
# marked by its pattern A01, not a kit. send and save-project ask the write policy as analogrun does.
# capture and ping need a digirun that has them (digihealth's runner-capture-ping); an older one
# lacks them, and then they are refused here too. din is its --din option (send/capture ... din).
DIGI_NO_STEPS = {'save-kit'}
DIGI_NEWER_STEPS = {'capture': '    capture FILE', 'ping': '    ping ',    # step -> its line in digirun's help
                    'din': "'--din'"}                                       # option -> its argparse name


def digirun_lacks():
    """The DIGI_NEWER_STEPS this digihealth's digirun.py does not have (all, without one)."""
    try:
        with open(os.path.join(DIGIHEALTH or '', 'tools', 'digirun.py'), encoding='utf-8', errors='replace') as fh:
            text = fh.read()
    except OSError:
        return set(DIGI_NEWER_STEPS)
    return {k for k, line in DIGI_NEWER_STEPS.items() if line not in text}


def emulated_units():
    """The units the emulator tool can start: analog-emu's (emu_exe) and digiemu's."""
    return [n for n, u in UNITS.items() if u.get('emu_exe') or u.get('emulator') == 'digiemu']


def chunk_args(unit):
    """elkflash's --chunk-size for `unit`, when its facts set one (flash_chunk)."""
    n = (UNITS.get(unit) or {}).get('flash_chunk')
    return ['--chunk-size', str(int(n))] if n else []


def no_steps(unit):
    """The steps `unit`'s runner lacks: digirun's gaps, and any the unit's facts add."""
    u = UNITS.get(unit) or {}
    return (((DIGI_NO_STEPS | digirun_lacks()) & STEP_OPS if u.get('runner') == 'digirun' else set())
            | set(u.get('no_steps') or ()))


def din_refusal(unit):
    """Why `unit`'s runner cannot take DIN steps (send/capture ... din), or None when it can:
    analogrun (Rytm, A4) always, digirun (Digitone, Digitakt) when its digirun.py has --din."""
    runner = (UNITS.get(unit) or {}).get('runner')
    if runner is True or runner == 'digirun' and 'din' not in digirun_lacks():
        return None
    if runner == 'digirun':
        return ('DIN steps (send/capture ... din) on the %s need a digirun.py with --din (tools.digihealth)'
                % unit)
    return 'DIN steps (send/capture ... din) have no runner on the %s' % unit


STEP_OPS = {'tap', 'hold', 'release', 'keys', 'turn', 'shot', 'cc', 'nrpn', 'note', 'wait', 'stats',
            'dump', 'send', 'capture', 'ping', 'save-project', 'save-kit', 'say'}


def din_steps(lines):
    """The send/capture steps among `lines` that use the unit's DIN MIDI (a trailing din)."""
    out = []
    for l in lines:
        w = shlex.split(l, comments=True)
        if w and w[-1].lower() == 'din' and (w[0].lower() == 'send' and len(w) > 2 or
                                             w[0].lower() == 'capture' and len(w) > 3):
            out.append(l)
    return out


def hand_steps(steps):
    """What an emulator run of a plan leaves untested: its `listen` steps (the person, by hand)
    and its `(hardware)` sections -> lines naming them, for run results and the go request."""
    out = []
    for name, lines in steps:
        if name.endswith('(hardware)'):
            out.append('[%s] (hardware section)' % name)
            continue
        for l in lines:
            w = shlex.split(l, comments=True)
            if w and w[0].lower() == 'listen':
                out.append('[%s] listen %s' % (name, ' '.join(w[1:])))
    return out


def is_audio_wait(line):
    """`wait N audio`: a wait that only lets the unit make sound (a recording, a tone, a
    listening pause). The runner reads the first number and waits N s in full; an emulator
    twin shortens it, since the emulator makes no voice sound to wait for."""
    w = shlex.split(line, comments=True)
    return len(w) == 3 and w[0].lower() == 'wait' and w[2].lower() == 'audio'


def audio_waits(steps):
    """-> lines naming every `wait N audio` of a plan, for run results and the go request."""
    return ['[%s] %s' % (name, l.split('#')[0].strip()) for name, lines in steps for l in lines
            if is_audio_wait(l)]


def emulator_steps(steps):
    """The steps an emulator twin runs: each `wait N audio` becomes a short wait. The pass key
    is still made from the steps as written, so the hardware run waits in full."""
    short = lambda l: 'wait %g  # elkbench: %s, short in the emulator' % (
        AUDIO_WAIT_EMULATOR, l.split('#')[0].strip()) if is_audio_wait(l) else l
    return [(name, [short(l) for l in lines]) for name, lines in steps]


def is_power_cycle(line):
    """`power-cycle`: the unit is switched off and on again. In the emulator, elkbench quits
    analog-emu started with --power-off (the OS runs its power-fail save and the card image is
    written), then cold-boots it on that card; on the unit, the run waits until the person has
    done it by hand and said so. testplan never sees the step: elkbench runs the plan in parts."""
    w = shlex.split(line, comments=True)
    return bool(w) and w[0].lower() == 'power-cycle'


def power_cycles(steps):
    """-> lines naming every `power-cycle` step of a plan, for run results and the go request."""
    return ['[%s] power-cycle' % name for name, lines in steps for l in lines if is_power_cycle(l)]


def power_cycle_parts(steps):
    """A plan's sections -> its parts between `power-cycle` steps (a list of section lists, one
    more than there are power cycles; the first may be empty). A section a power cycle splits
    goes on in the next part as 'NAME, after the power cycle'."""
    parts, cur = [], []
    for name, lines in steps:
        body, piece = [], name
        for l in lines:
            if is_power_cycle(l):
                if any(x.strip() and not x.strip().startswith('#') for x in body):
                    cur.append((piece, body))
                parts.append(cur)
                cur, body, piece = [], [], name + ', after the power cycle'
            else:
                body.append(l)
        if any(x.strip() and not x.strip().startswith('#') for x in body) or piece == name:
            cur.append((piece, body))
    parts.append(cur)
    return parts


def saved_slots(steps):
    """The project slots a plan saves into (save-project N ...): the only saved projects an
    emulator run of it may change."""
    out = set()
    for _, lines in steps:
        for l in lines:
            w = shlex.split(l, comments=True)
            if len(w) > 1 and w[0].lower() == 'save-project' and w[1].isdigit():
                out.add(int(w[1]))
    return out


def slot_numbers(spec):
    """A unit's `clear_at_release` setting -> sorted project slot numbers: a list of numbers, or
    text such as '121-128' or '121-124, 127'."""
    if not spec:
        return []
    out = set()
    for part in ([spec] if isinstance(spec, int) else spec if isinstance(spec, list) else str(spec).split(',')):
        part = str(part).strip()
        m = re.fullmatch(r'(\d+)(?:\s*-\s*(\d+))?', part)
        if not m:
            raise SystemExit('config.toml: clear_at_release takes slot numbers or ranges such as 121-128, not %r'
                             % part)
        lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
        out.update(range(lo, hi + 1))
    return sorted(out)


def occupied_slots(ls_text):
    """`analogrun data ls /projects` output -> the slot numbers that hold a project."""
    return {int(m.group(2)) for m in re.finditer(r'^(.*?)\s+id\s+(\d+)\s+\d+ B(.*)$', ls_text, re.M)
            if 'no valid data' not in m.group(3)}


def slot_text(slots):
    """[121, 122, ..., 128, 130] -> '121-128, 130'."""
    out, run = [], []
    for n in sorted(slots):
        if run and n == run[-1] + 1:
            run.append(n)
            continue
        if run:
            out.append('%d-%d' % (run[0], run[-1]) if len(run) > 1 else str(run[0]))
        run = [n]
    if run:
        out.append('%d-%d' % (run[0], run[-1]) if len(run) > 1 else str(run[0]))
    return ', '.join(out)


def unit_starts(unit):
    """-> {start: its start plan, or None for `fresh`} that this unit's emulator runs from:
    `fresh` (a fresh emulator, the card as shipped) and the start plans in STARTS/<unit>/."""
    out = {'fresh': None}
    folder = os.path.join(STARTS, unit)
    if os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.endswith('.txt') and f[:-4] != 'fresh':
                out[f[:-4].lower()] = os.path.join(folder, f)
    return out


def start_data(text, here, start):
    """`data = OP PATH [FILE]; ...` in a start file -> [(op, path, file or None)]: data store
    steps run on the emulator before the start's own sections. `clear PATH` empties a slot,
    `put PATH FILE` writes an object saved by `data get` (FILE relative to the start file)."""
    out = []
    for part in text.split(';'):
        try:
            w = shlex.split(part)
        except ValueError as e:
            raise Refused('start %s: data step %r does not parse (%s)' % (start, part.strip(), e))
        if not w:
            continue
        op = w[0].lower()
        if op == 'clear' and len(w) == 2 and w[1].startswith('/'):
            out.append(('clear', w[1], None))
        elif op == 'put' and len(w) == 3 and w[1].startswith('/'):
            f = os.path.normpath(w[2] if os.path.isabs(w[2]) else os.path.join(here, w[2]))
            if not os.path.isfile(f):
                raise Refused('start %s: data put %s: no file %s' % (start, w[1], f))
            out.append(('put', w[1], f))
        else:
            raise Refused('start %s: data step %r is `clear PATH` or `put PATH FILE` (PATH a data '
                          'store path starting with \'/\')' % (start, part.strip()))
    return out


def start_chain(unit, start, seen=()):
    """-> [(start, start file, data steps)] that build the built-in start `start`, its bases
    first. A start file may build on another start of the same unit (`base = START`: that start
    first, on the same emulator) and run data store steps (`data = ...`, see start_data) after
    its base and before its own sections, so a start can reach states the front panel cannot
    (a project loaded from a test slot that was then cleared, as a run's backup and clear leaves it)."""
    have = unit_starts(unit)
    if not have.get(start):
        raise Refused('the %s has no built-in start %r (starts: %s)' % (unit, start, ', '.join(sorted(have))))
    if start in seen:
        raise Refused('built-in start %s builds on itself (base = ... %s)' % (start, ' -> '.join(seen + (start,))))
    path = have[start]
    runner()
    try:
        settings, _ = testplan.parse(path)
    except (ValueError, OSError) as e:
        raise Refused('built-in start %s does not parse: %s' % (start, e))
    if settings.get('unit', 'rytm') != unit:
        raise Refused('built-in start %s is for unit %s, not %s' % (start, settings.get('unit', 'rytm'), unit))
    base = (settings.get('base') or '').strip().lower()
    chain = start_chain(unit, base, seen + (start,)) if base and base != 'fresh' else []
    return chain + [(start, path, start_data(settings.get('data') or '', os.path.dirname(path), start))]


def required_starts(unit):
    """The built-in starts every hardware plan of this unit runs from (where the unit has them)."""
    return REQUIRED_STARTS_CLEARED if UNITS.get(unit, {}).get('clear_at_release') else REQUIRED_STARTS


def starts_needed(info):
    """-> {start: the section a run from it must stop in, or None: it must pass} for a hardware
    plan: the required starts its unit has (not for a plan that flashes), and every start its
    contrasts name."""
    have = unit_starts(info['unit'])
    stops = plancheck.expected_stops(info['steps'])
    # a plan that flashes starts on the OS before its flash (often stock, with no remote keys to
    # build a start with), and the flash itself is gated as a transition: only its own contrasts
    want = {} if info.get('flash') else {st: stops.get(st) for st in required_starts(info['unit']) if st in have}
    want.update(stops)
    if info.get('home') and want.get(info['home'], 0) is None:
        del want[info['home']]           # the plan's own run starts there: its pass covers it
    return want


def start_verdict(expect, rc, run_dir):
    """-> (as expected, line) for a run from a start: it passes, or it stops in the section
    `expect` (its first failure there, not earlier)."""
    summ = read_json(os.path.join(run_dir, 'summary.json'), {}) if run_dir else {}
    first = next((x for x in summ.get('steps') or [] if x.get('result') == 'FAIL'), None)
    if not summ:
        return False, 'no verdict from the run (exit %d)' % rc
    if expect is None:
        if rc == 0 and summ.get('pass'):
            return True, 'passed'
        return False, 'stopped in [%s]: %s' % (first['step'], first.get('detail', '')) if first else 'failed'
    if not first:
        return False, 'passed, so [%s] cannot tell this start apart: its contrast is not seen to fail' % expect
    if first['step'] != expect:
        return False, 'stopped in [%s] before [%s] (%s): the contrast never reached its section' % (
            first['step'], expect, first.get('detail', ''))
    return True, 'stopped in [%s] as its contrast says: %s' % (expect, first.get('detail', ''))


def project_changes(before, after, allowed):
    """{slot: (name, sha256)} before and after a run -> lines naming every saved project that
    was added, removed or changed outside `allowed`."""
    out = []
    for n in sorted(set(before) | set(after)):
        if n in allowed or before.get(n) == after.get(n):
            continue
        b, a = before.get(n), after.get(n)
        out.append('project %d %s' % (n, 'added (%r)' % a[0] if not b else 'removed (was %r)' % b[0]
                                      if not a else 'renamed %r -> %r' % (b[0], a[0]) if a[0] != b[0]
                                      else 'changed (%r)' % a[0]))
    return out


def run_files(run_dir, limit=24):
    """The screenshots and other pictures a run saved, by name, in the order they were saved."""
    try:
        names = [n for n in os.listdir(run_dir) if n.lower().endswith('.png')]
    except OSError:
        return []
    names.sort(key=lambda n: os.path.getmtime(os.path.join(run_dir, n)))
    return names[:limit] + (['(%d more)' % (len(names) - limit)] if len(names) > limit else [])


def load_config(path=CONFIG):
    """config.toml (see config.example.toml); every key is optional. Tools left out are off and
    their MCP tools say so; evidence defaults to Documents\\elkbench\\<unit>."""
    import tomllib
    try:
        with open(path, 'rb') as fh:
            c = tomllib.load(fh)
    except FileNotFoundError:
        c = {}
    t = c.get('tools', {})
    root = c.get('evidence_root') or os.path.join(os.path.expanduser('~'), 'Documents', 'elkbench')
    units = {}
    for name, facts in UNIT_FACTS.items():
        u = dict(facts, **c.get('units', {}).get(name, {}))
        u.setdefault('evidence', os.path.join(root, name, 'runs'))
        u.setdefault('refs', os.path.join(root, name, 'refs'))
        # test slots the release empties (backed up first), so every run finds them empty
        u['clear_at_release'] = slot_numbers(u.get('clear_at_release'))
        units[name] = u
    # analoghealth's local write policy (its writepolicy.py): ANALOGHEALTH_POLICY, else policy.py
    # in its config folder
    policy = os.environ.get('ANALOGHEALTH_POLICY') if 'ANALOGHEALTH_POLICY' in os.environ else \
        os.path.join(os.environ.get('LOCALAPPDATA', '') if WINDOWS else
                     os.path.join(os.path.expanduser('~'), '.config'), 'analoghealth', 'policy.py')
    return dict(
        person=c.get('person') or 'the person at this PC',
        elklock=t.get('elklock') or os.path.join(HERE, 'elklock', 'elklock.py'),
        analoghealth=t.get('analoghealth'), digihealth=t.get('digihealth'),
        analog_emu=t.get('analog_emu'), digiemu=t.get('digiemu'), digiemu_python=t.get('digiemu_python') or [sys.executable], elkflash=t.get('elkflash'), usb_audio=t.get('usb_audio') or os.path.join(HERE, 'audio'),
        obtr=os.path.join(HERE, 'obtr.py'), audio_python=t.get('audio_python') or [sys.executable],
        approval_fallback=c.get('approval_fallback') or 'popup',
        batch_slots=int(c.get('batch_max_emulators') or batch.SLOTS_DEFAULT),
        write_policy=(policy or '(ANALOGHEALTH_POLICY is empty)') if c.get('require_write_policy') else None,
        starts=c.get('starts') or os.path.join(HERE, 'starts'),
        keep_awake=c.get('keep_awake', True) is not False,
        units=units)


CFG = load_config()
UNITS = CFG['units']
PERSON = CFG['person']
# what the person does at a hardware run's power-cycle step: unit, then ' (n of m)' or ''
POWER_CYCLE_HAND = ('power cycle the %s%s by hand: switch it off, wait 10 s, switch it on, and let it '
                    'boot to its start screen; nothing is sent to it meanwhile')
FALLBACK = CFG['approval_fallback']       # when the client cannot show the prompt: popup, thread or both
if FALLBACK not in ('popup', 'thread', 'both'):
    raise SystemExit('config.toml: approval_fallback must be popup, thread or both, not %r' % FALLBACK)
ELKLOCK = CFG['elklock']
ANALOGHEALTH = CFG['analoghealth']
RUNNER = os.path.join(ANALOGHEALTH, 'tools', 'analogrun.py') if ANALOGHEALTH else None
DIGIHEALTH = CFG['digihealth']            # a digihealth tree with tools/digirun.py (Digitone)
ANALOG_EMU = CFG['analog_emu']            # folder with analog-emu's rytm.exe and a4.exe
DIGIEMU = CFG['digiemu']                  # a digiemu checkout (emu.fwcheck)
DIGIEMU_PY = CFG['digiemu_python']        # its Python; ['wsl', '~/venv/bin/python'] runs it in WSL
ELKFLASH = CFG['elkflash']                # a checkout to run `-m elkflash` in, or None: installed
USB_AUDIO = CFG['usb_audio']
OBTR = CFG['obtr']
AUDIO_PY = CFG['audio_python']            # a Python with numpy + scipy
WRITE_POLICY = CFG['write_policy']      # must exist before hardware calls, or None
STARTS = CFG['starts']                    # <folder>/<unit>/<start>.txt: the built-in emulator starts
batch.set_slots(CFG['batch_slots'])      # emulators all batches on this PC may run at once

sys.path[:0] = [os.path.dirname(ELKLOCK)] + ([os.path.join(ANALOGHEALTH, 'tools')] if ANALOGHEALTH else [])
import elklock    # noqa: E402
testplan = None


class Path(str):
    """A local path in a command that may run in WSL (digiemu_cmd converts it)."""


def need(value, key, what):
    """A tool that is not configured turns its MCP tools off."""
    if not value or not os.path.exists(value):
        raise Refused('%s is off: set tools.%s in %s' % (what, key, CONFIG))
    return value


def runner():
    global testplan
    need(ANALOGHEALTH, 'analoghealth', 'the test runner (analoghealth)')
    if testplan is None:
        import testplan as tp
        testplan = tp
    return RUNNER


class Refused(Exception):
    """A call the rules do not allow; its text is the reason, shown to the session."""


def reply_id(mid):
    """A chat message's id or link as the client knows it (any chat), or '' when it is not one:
    empty, spaced, or a bare word like "yes", which is an answer, not the reply's id."""
    mid = (mid or '').strip()
    return '' if not mid or re.search(r'\s', mid) or re.fullmatch(r'[A-Za-z]+', mid) else mid


def flash_key(unit, digest, over=None):
    """The emulator pass key of a flash: `digest` (sha256 of the OS file) flashed over the OS
    `over` (sha256). Without over, the key passes had before it carried the OS flashed over."""
    return 'flash:%s:%s' % (unit, digest) + (':over:%s' % over if over else '')


def sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def fwd(p):
    """A path for analogrun's shlex-split steps: forward slashes, quoted."""
    return shlex.quote(p.replace('\\', '/'))


def read_json(p, default):
    try:
        with open(p, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return default


def write_json(p, data):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, indent=1)
    os.replace(tmp, p)


def stamp(t=None):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(t or time.time()))


# ---- sessions, jobs, approvals ------------------------------------------------------------

class Session:
    def __init__(self, sid, pid, cwd):
        self.sid, self.pid, self.cwd = sid, pid, cwd
        self.purpose = {}                 # unit -> claim purpose
        self.done = {}                    # unit -> ['plan X PASS (dir)', ...] since the claim
        self.os_left = {}                 # unit -> (image name, stock?) after a hardware flash

    def folder(self, unit):
        """This session's evidence folder in the unit's evidence folder."""
        d = os.path.join(UNITS[unit]['evidence'], 'mcp-%s-%s' % (time.strftime('%Y%m%d'), self.sid[:8]))
        os.makedirs(d, exist_ok=True)
        return d

    def env(self):
        # Both names, so tools that read Claude Code's (and scripts written for it) still see them
        e = dict(os.environ, ELKBENCH_SESSION_ID=self.sid, CLAUDE_CODE_SESSION_ID=self.sid,
                 PYTHONIOENCODING='utf-8')
        for k in ('ELKBENCH_CLIENT_PID', 'CLAUDE_PID'):     # not the daemon's own
            e.pop(k, None)
        if self.pid:
            e['ELKBENCH_CLIENT_PID'] = e['CLAUDE_PID'] = str(self.pid)
        return e


class PortLock:
    """One user per port. It remembers when it was last let go and by what (`freed`, `what`),
    and busy_port keeps those of the user before the current one in `before`: a flash logs how
    long the port had been quiet when it began."""

    def __init__(self):
        self.lk = threading.Lock()
        self.holder = self.what = self.freed = None
        self.before = (None, None)

    def acquire(self, blocking=True):
        return self.lk.acquire(blocking)

    def locked(self):
        return self.lk.locked()

    def release(self):
        self.freed, self.what = time.time(), self.holder
        self.lk.release()


def flash_start_line(job, port, now=None):
    """The line a flash's log starts with: when, and how long the unit's port had been quiet
    (a reply lost right after a plan ended is easier to place with it)."""
    now = now or time.time()
    freed, what = port.before
    if freed is None:
        quiet = 'no earlier use of this port since the daemon started'
    else:
        quiet = '%.1f s after the last use of this port ended (%s)' % (now - freed, what or 'a short call')
    return 'elkbench: %s %s on %s starts %s, %s' % (job.id, job.desc, job.unit,
                                                   time.strftime('%H:%M:%S', time.localtime(now)), quiet)


class Held:
    """Port locks taken together, released together (the unit's port and its DIN interface)."""

    def __init__(self, *locks):
        self.locks = locks

    def release(self):
        for lk in self.locks:
            lk.release()


class Job:
    def __init__(self, sess, kind, unit, target, desc):
        self.id = 'r-' + secrets.token_hex(3)
        self.sess, self.kind, self.unit, self.target, self.desc = sess, kind, unit, target, desc
        self.state = 'running'            # running | pass | fail | approved | refused
        self.lines = []
        self.started, self.ended = time.time(), None
        self.log = os.path.join(STATE, 'jobs', self.id + '.log')
        self.listen_file = os.path.join(STATE, 'jobs', self.id + '.listen.json')   # a plan's listen step
        self.listening = None             # what the person listens for while the plan waits
        self.power_cycle = False          # ... and whether it waits for a power cycle by hand
        self.batch = None                 # a batch's batch.Batch: its progress while it runs

    def summary(self):
        took = (self.ended or time.time()) - self.started
        head = '%s: %s %s (%s, %s) %.0f s' % (self.state, self.id, self.desc, self.unit, self.target, took)
        return [head] + ([self.batch.progress()] if self.batch and self.state == 'running' else self.lines)


class Daemon:
    def __init__(self):
        self.lock = threading.RLock()
        self.sessions = {}
        self.jobs = {}
        # token -> dict, kept on disk: the daemon restarts for newer code at the first idle moment,
        # which can fall between the two flashes of one go
        self.approvals_path = os.path.join(STATE, 'approvals.json')
        self.approvals = read_json(self.approvals_path, {})
        self.ports = {}                   # (unit, target) -> PortLock: one user per port
        self.emulators = {}               # (session, unit) -> dict(proc, session, build, address)
        self.dialog = threading.Lock()
        self.passes_path = os.path.join(STATE, 'emulator-passes.json')
        self.starts_path = os.path.join(STATE, 'emulator-starts.json')
        self.emu_help_text = {}           # (exe, mtime) -> its --help output
        self.last_use = time.time()
        self.connections = 0
        self.restarting = False
        self.checked = {}                 # code mtime -> None (it starts) or why it does not

    # -- helpers --

    def session(self, hello):
        with self.lock:
            s = self.sessions.get(hello['session'])
            if s is None:
                s = self.sessions[hello['session']] = Session(hello['session'], hello.get('pid'),
                                                              hello.get('cwd', ''))
            elif hello.get('pid') and hello['pid'] != s.pid:
                # The session came back in a new client process (an app restart, a resume): its
                # claims watch that process, or they would end at once ("claiming session ended").
                s.pid = hello['pid']
            return s

    def port_lock(self, unit, target):
        with self.lock:
            return self.ports.setdefault((unit, 'emulator' if target == 'emulator' else 'hw'),
                                         PortLock())

    def busy_port(self, unit, target, what=None):
        lk = self.port_lock(unit, target)
        if not lk.acquire(blocking=False):
            running = [j.id + ' ' + j.desc for j in self.jobs.values()
                       if j.state == 'running' and j.unit == unit and
                       (j.target == 'emulator') == (target == 'emulator')]
            raise Refused('the %s %s port is in use (%s); try again when it ends'
                          % (unit, target, ', '.join(running) or 'a short call'))
        lk.before, lk.holder = (lk.freed, lk.what), what
        return lk

    def hold_din(self, din, port):
        """`port`, plus the DIN interface's own lock when `din` (analogrun's ['--din', NAME]) names
        one on hardware: no two runs share an interface, whichever unit it is cabled to."""
        if not din or din[1].startswith('tcp:'):
            return port
        lk = self.port_lock('din:' + din[1].lower(), 'hardware')
        if not lk.acquire(blocking=False):
            port.release()
            raise Refused('the DIN interface %r is in use by another run; try again when it ends' % din[1])
        return Held(port, lk)

    def unit(self, name, need_runner=False):
        if name not in UNITS:
            raise Refused('unknown unit %r (%s)' % (name, ', '.join(UNITS)))
        have = UNITS[name]['runner']     # True: analogrun (plans too); 'digirun': steps and screens
        if need_runner is True and have is not True:
            raise Refused('test plans run on the Rytm and A4 only so far; use steps and '
                          'expect_screen on the %s' % name)
        if need_runner and not have:
            raise Refused('no runner for %s' % name)
        return UNITS[name]

    def check_hardware(self, s, units):
        """The session holds every claim, the required write policy is there, the units are."""
        if WRITE_POLICY and not os.path.isfile(WRITE_POLICY):
            raise Refused('require_write_policy is set but there is no analoghealth write policy '
                          '(%s); hardware tools stay off until it is back' % WRITE_POLICY)
        for u in units:
            h = elklock.holder(u)
            if not elklock.mine(h, session=s.sid):
                raise Refused('claim %s first (claim tool): %s' % (u, elklock.describe(u, h)))
            if h.get('listening'):
                raise Refused('%s is listening to the %s (%s): nothing may drive it until they say in '
                              'the thread that they have finished; then listen_done'
                              % (PERSON, u, h['listening'].get('what', '')))

    def emulator_procs(self, unit):
        """PIDs of this unit's emulator processes, ours or started by hand (a window)."""
        exe = UNITS[unit].get('emu_exe')
        if not exe:
            return []
        if not WINDOWS:
            out = []
            for pid in filter(str.isdigit, os.listdir('/proc')):
                try:
                    with open('/proc/%s/comm' % pid) as fh:
                        if fh.read().strip() == exe[:15]:
                            out.append(int(pid))
                except OSError:
                    pass
            return out
        r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq %s' % exe, '/FO', 'CSV', '/NH'],
                           capture_output=True, text=True, **NOWIN)
        return [int(l.split('","')[1]) for l in r.stdout.splitlines() if l.startswith('"')]

    def emu_running(self, s, unit):
        e = self.emulators.get((s.sid, unit))
        return bool(e) and e['proc'].poll() is None

    def emu_port(self, s, unit):
        """Where this session's own emulator of `unit` listens (`tcp:127.0.0.1:PORT`, from the
        emulator tool). Never the shared loopMIDI ports: an emulator found there belongs to
        someone else, and driving it would change their test."""
        e = self.emulators.get((s.sid, unit))
        if e and e['proc'].poll() is None:
            return e['address']
        if not UNITS[unit]['emu']:
            raise Refused('there is no %s emulator with USB MIDI' % unit)
        raise Refused('this session has no %s emulator: give build (the OS .syx to boot), or start '
                      'one with the emulator tool (emulators started by hand are not used)' % unit)

    def emu_ready(self, s, unit, build, snapshot=None, power_off=False):
        """-> (address, lines): with `build`, a fresh emulator of this session boots it first
        (any it runs is stopped), saving the separate emulator calls; else the running one.
        power_off: it starts with --power-off, for a plan's power-cycle steps."""
        if not build:
            if power_off and not (self.emulators.get((s.sid, unit)) or {}).get('power_off'):
                raise Refused('this plan has power-cycle steps: give build, so that its emulator starts '
                              'with the power-off save (the running one did not)')
            return self.emu_port(s, unit), []
        mine = self.emulators.get((s.sid, unit))
        if mine and mine['proc'].poll() is None:
            self.stop_emulator(mine)
        lines = self.t_emulator(s, {'action': 'start', 'unit': unit, 'build': build}, snapshot, power_off)
        addr = self.emu_port(s, unit)
        if UNITS[unit].get('emulator') != 'digiemu':   # digiemu_start waits for its OS itself
            self.wait_for_os(addr)
        return addr, lines[:1]

    def wait_for_os(self, addr, tries=30):
        """analog-emu's port listens before the OS boots: a flash sent at once got no reply to
        its first command. -> whether the OS answered Ping within about `tries` s."""
        for _ in range(tries):
            try:
                r = subprocess.run([sys.executable, '-m', 'elkflash', 'info', '--port', addr,
                                    '--timeout', '1'], cwd=ELKFLASH, capture_output=True,
                                   timeout=10, **NOWIN)
                if r.returncode == 0:
                    return True
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1)
        return False

    def power_cycle(self, s, unit, save=True):
        """A power cycle of this session's analog-emu (started with --power-off): `quit` on its
        stdin switches it off, so the OS runs its power-fail save and the card image is written;
        then a cold boot on that card and flash, without the snapshot it may have resumed (the
        power-off saves none), on new ports. save=False kills it instead, with no save: a
        contrast in which a check of what had to be kept must fail. -> (rc, report line)."""
        e = self.emulators.get((s.sid, unit))
        if not e or e['proc'].poll() is not None:
            return 1, 'fail: power-cycle: this session\'s %s emulator is not running\n' % unit
        if not e.get('power_off'):
            return 1, 'fail: power-cycle: the %s emulator did not start with --power-off\n' % unit
        with open(e['log'], encoding='utf-8', errors='replace') as fh:
            seen = len(fh.read())
        if save:
            try:
                e['proc'].stdin.write(b'quit\n')
                e['proc'].stdin.close()
            except (OSError, ValueError):
                pass
            try:
                e['proc'].wait(POWER_OFF_WAIT)
            except subprocess.TimeoutExpired:
                self.stop_emulator(e)
                return 1, 'fail: power-cycle: the emulator did not switch off within %d s (%s)\n' % (
                    POWER_OFF_WAIT, e['log'])
            with open(e['log'], encoding='utf-8', errors='replace') as fh:
                said = fh.read()[seen:]
            off = [l.strip() for l in said.splitlines() if l.startswith('power off:')]
            if POWER_OFF_MARK not in said or 'wrote card' not in said:
                return 1, 'fail: power-cycle: the OS did not finish its power-off save (%s; %s)\n' % (
                    '; '.join(off) or 'no power-off line', e['log'])
            how = off[0][len('power off: '):]
        else:
            self.stop_emulator(e)
            how = 'killed, no save'
        if e['proc'].stdin and not e['proc'].stdin.closed:
            e['proc'].stdin.close()
        cmd, n = list(e['cmd']), e.get('boots', 1) + 1
        if '--snapshot' in cmd:                # the power-off saves no snapshot: a cold boot
            i = cmd.index('--snapshot')
            del cmd[i:i + 2]
            if '--stdin-commands' not in cmd:
                cmd.append('--stdin-commands')
        ports = {}
        for flag in ('--usb-midi-tcp', '--din-midi-tcp'):
            if flag in cmd:
                with socket.socket() as probe:
                    probe.bind(('127.0.0.1', 0))
                    ports[flag] = probe.getsockname()[1]
                cmd[cmd.index(flag) + 1] = '127.0.0.1:%d' % ports[flag]
        log_path = os.path.join(e['work'], 'emulator-boot%d.log' % n)
        with open(log_path, 'w', encoding='utf-8') as log:
            proc = subprocess.Popen(cmd, cwd=e['work'], stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.PIPE, **NOWIN)
        addr = 'tcp:127.0.0.1:%d' % ports['--usb-midi-tcp']
        e.update(proc=proc, cmd=cmd, log=log_path, snapshot=None, boots=n, address=addr,
                 din='tcp:127.0.0.1:%d' % ports['--din-midi-tcp'] if '--din-midi-tcp' in ports else None)
        din = '--din-midi-tcp' in ports
        for _ in range(50):
            time.sleep(0.2)
            with open(log_path, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            if ('usb midi tcp: listening' in text and (not din or 'din midi tcp: listening' in text)
                    or proc.poll() is not None):
                break
        if proc.poll() is not None or 'usb midi tcp: listening' not in text:
            return 1, 'fail: power-cycle: the cold boot did not open its USB MIDI port (%s)\n' % log_path
        if not self.wait_for_os(addr):
            return 1, 'fail: power-cycle: the OS did not answer after the cold boot (%s)\n' % log_path
        return 0, 'ok power-cycle: switched off (%s), cold boot on its card: up again at %s (%s)\n' % (
            how, addr, log_path)

    def check_connected(self, unit, target):
        try:
            ports = elklock.midi_inputs()
        except OSError:
            return
        if target == 'emulator':
            if not UNITS[unit]['emu']:
                raise Refused('there is no %s emulator with USB MIDI' % unit)
            if UNITS[unit].get('emu_exe') and not self.emulator_procs(unit):
                # the loopMIDI ports exist whether or not an emulator holds them
                raise Refused('no %s emulator is running: start one with the emulator tool' % unit)
            want = UNITS[unit]['emu'].split(',')[0].lower()
            if not any(want in p.lower() for p in ports):
                raise Refused('the %s emulator is not running (no %r MIDI port): start the analog-emu '
                              'window first' % (unit, want))
        elif not elklock.connected(unit, ports):
            raise Refused('%s is not on USB (no %r MIDI port)' % (unit, UNITS[unit]['port']))

    def target(self, t):
        if t not in ('emulator', 'hardware'):
            raise Refused("target is 'emulator' or 'hardware'")
        return t

    def is_stock(self, unit, digest):
        for p in UNITS[unit]['stock']:
            if os.path.exists(p) and sha256(p) == digest:
                return True
        return False

    def check_stats(self, unit, digest, ops, where):
        """Refuses a `stats` step on an OS file listed in the unit's no_stats (builds whose
        channel answers "unknown command" to it)."""
        if digest and 'stats' in ops and any(
                os.path.exists(p) and sha256(p) == digest for p in UNITS[unit].get('no_stats', [])):
            raise Refused('%s has no stats command (no_stats in config.toml): leave the stats '
                          'step out' % where)

    # -- emulator passes --

    def passes(self):
        return read_json(self.passes_path, {})

    def record_pass(self, key, entry):
        with self.lock:
            p = self.passes()
            p[key] = dict(entry, time=time.time())
            write_json(self.passes_path, p)

    def find_pass(self, key, flash_sha=None):
        e = self.passes().get(key)
        if not e:
            return None
        if e.get('run_dir'):          # a plan pass: its summary must still say so
            s = read_json(os.path.join(e['run_dir'], 'summary.json'), {})
            if not (s.get('emulator') and s.get('pass')):
                return None
            if flash_sha and s.get('flash_sha256') != flash_sha:
                return None           # testplan's own gate would refuse it on the unit
        return e

    def plan_pass(self, info):
        """-> the emulator pass that vouches for a plan, or None. A plan that names its OS
        (`os = FILE`) is keyed with that file's sha256; a pass recorded before the key carried it
        still counts when the build it booted was that file."""
        ep = self.find_pass(info['key'], info['flash_sha'])
        if ep or not info['os_sha']:
            return ep
        old = self.find_pass(info['old_key'], info['flash_sha'])
        return old if old and old.get('os') == info['os_sha'] else None

    def flash_pass(self, unit, digest, over):
        """-> the emulator pass of one flash transition: `digest` flashed over the OS `over`
        (sha256s), or None. A plan pass recorded before flash keys carried the OS flashed over
        still counts when its run booted `over` and flashed `digest`; a Digitone/Digitakt
        firmware check (digiemu fwcheck, which flashes over no OS) counts whatever `over` is."""
        e = self.find_pass(flash_key(unit, digest, over))
        if e:
            return e
        old = self.find_pass(flash_key(unit, digest))
        if old and old.get('fwcheck'):
            return old
        for k, e in self.passes().items():
            if (not k.startswith('flash:') and e.get('unit') == unit and e.get('os') == over
                    and e.get('run_dir') and self.find_pass(k, digest)):
                return e
        return None

    def os_on_unit(self, s, unit, over=None):
        """-> sha256 of the OS on the unit now: the file `over` names, else what this session
        flashed last. Refused when neither says (or a flash of this session failed)."""
        if over:
            over = os.path.abspath(over)
            if not os.path.isfile(over):
                raise Refused('over names no file: %s' % over)
            return sha256(over)
        left = s.os_left.get(unit)
        if left and len(left) > 2:
            return left[2]
        if left:
            raise Refused('the OS on %s is %s: stop and check the unit' % (unit, left[0]))
        raise Refused('give over: the OS file on %s now (a flash\'s emulator pass is of one transition, '
                      'this file flashed over that OS)' % unit)

    def record_start(self, key, os_sha, start, entry):
        """A run of a hardware plan from a start (contrast or built-in), on the build os_sha."""
        with self.lock:
            p = read_json(self.starts_path, {})
            p['%s|%s|%s' % (key, os_sha, start)] = dict(entry, start=start, os=os_sha, time=time.time())
            write_json(self.starts_path, p)

    def find_start(self, key, os_sha, start, expect):
        """The run from `start` that went as `expect` says, on build os_sha (None: on any build,
        the newest), or None."""
        p = read_json(self.starts_path, {})
        if os_sha:
            e = p.get('%s|%s|%s' % (key, os_sha, start))
            got = [e] if e else []
        else:
            got = sorted((e for k, e in p.items() if k.startswith(key + '|') and k.endswith('|' + start)),
                         key=lambda e: e.get('time', 0), reverse=True)[:1]
        for e in got:
            if e.get('ok') and e.get('expect') == expect:
                return e
        return None

    def start_lines(self, info, os_sha):
        """-> (missing, lines): the start runs a go for this plan needs, and what each did."""
        missing, lines = [], []
        for st, expect in sorted(starts_needed(info).items()):
            e = self.find_start(info['key'], os_sha, st, expect)
            want = 'pass' if expect is None else 'stop in [%s]' % expect
            if e:
                lines.append('  start %s: %s (%s)' % (st, e.get('line') or want, e.get('run_dir') or stamp(e['time'])))
            else:
                missing.append('%s (must %s)' % (st, want))
        return missing, lines

    def note_emu(self, s, unit, what, taints):
        """Something done on this session's emulator, which a later pass on it ran after. taints:
        it wrote state outside any gated plan (an emulator-only plan, steps or data writes)."""
        e = self.emulators.get((s.sid, unit))
        if e is not None:
            e.setdefault('history', []).append(dict(what=what, taints=bool(taints)))

    # -- plans --

    def plan_info(self, path):
        """-> dict(settings, steps, unit, key, flash, flash_sha, os_sha, refs) for a plan file. The
        key names what an emulator pass vouches for: unit, steps, reference screens, the OS file
        it flashes and the OS it runs on (`os = FILE`, the build its emulator boots)."""
        runner()
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise Refused('no plan file %s' % path)
        try:
            settings, steps = testplan.parse(path)
        except (ValueError, OSError) as e:
            raise Refused('plan does not parse: %s' % e)
        for name, lines in steps:
            for l in lines:
                try:
                    shlex.split(l, comments=True)
                except ValueError as e:
                    raise Refused('[%s] %r does not parse (%s): quote or drop the stray quote mark' % (name, l, e))
        fmt = settings.get('format', '1')
        if fmt not in PLAN_FORMATS:
            raise Refused('plan format %s; this server reads format %s' % (fmt, ', '.join(PLAN_FORMATS)))
        here = os.path.dirname(path)
        rel = lambda p: os.path.normpath(p if os.path.isabs(p) else os.path.join(here, p))
        unit = settings.get('unit', 'rytm')
        self.unit(unit, need_runner='plan')
        if UNITS[unit]['runner'] == 'digirun':
            need(DIGIHEALTH, 'digihealth', 'the Digitone/Digitakt runner (digihealth)')
            bad = sorted({l.split()[0].lower() for _, lines in steps for l in lines if l.split()}
                         & no_steps(unit))
            if bad:
                raise Refused('a %s plan cannot %s (digirun has no such steps there)' % (unit, ', '.join(bad)))
        din = din_steps([l for _, lines in steps for l in lines])
        if din and din_refusal(unit):
            raise Refused('%s: %s' % (din[0], din_refusal(unit)))
        flash = rel(settings['flash']) if settings.get('flash') else None
        if flash and UNITS[unit].get('flash_chunk'):
            raise Refused('a %s plan cannot flash: testplan sends elkflash\'s default chunks, and the %s '
                          'needs %d-byte ones. Flash with the flash tool, then run the plan'
                          % (unit, unit, UNITS[unit]['flash_chunk']))
        if flash and not os.path.isfile(flash):
            raise Refused('the plan flashes %s, which does not exist' % flash)
        flash_sha = sha256(flash) if flash else None
        # os = FILE: the OS on the unit when the plan starts, so the build its emulator boots. A
        # flash plan names it (its pass is of one transition: this OS, then its flash).
        os_file = rel(settings['os']) if settings.get('os') else None
        if os_file and not os.path.isfile(os_file):
            raise Refused('the plan runs on %s (os =), which does not exist' % os_file)
        os_sha = sha256(os_file) if os_file else None
        refs = rel(settings.get('refs', 'refs'))
        ref_files = sorted(f for f in os.listdir(refs) if f.endswith('.bin')) if os.path.isdir(refs) else []
        ref_sums = [(f, sha256(os.path.join(refs, f))) for f in ref_files]
        # send in a plan: a file an earlier capture step saved in the run folder, or an absolute
        # path, which must hold dumps only (as steps' send) and is part of what a pass vouches for
        captured, send_sums = set(), []
        for _, lines in steps:
            for l in lines:
                w = shlex.split(l, comments=True)
                if not w:
                    continue
                if w[0].lower() == 'capture' and len(w) > 1:
                    captured.add(w[1])
                elif w[0].lower() == 'send' and len(w) > 1 and w[1] not in captured:
                    if not os.path.isabs(w[1]):
                        raise Refused('send %s: a plan sends an absolute path, or a file an earlier '
                                      'capture step saved' % w[1])
                    self.check_dumps(w[1], here)
                    send_sums.append((w[1], sha256(w[1])))
        # start = START: the plan's own run starts from that built-in start, not a fresh emulator
        # (a plan for a state only a start leaves, such as a project loaded from a cleared slot)
        home = (settings.get('start') or '').strip().lower() or None
        if home == 'fresh':
            home = None
        if home:
            if settings.get('emulator', 'no').lower() == 'yes':
                raise Refused('start = %s is for hardware plans (an emulator plan runs where it is run)' % home)
            if flash:
                raise Refused('start = %s: a plan that flashes starts on the OS before its flash, where no '
                              'built-in start is built' % home)
            start_chain(unit, home)
            if home in plancheck.expected_stops(steps):
                raise Refused('start = %s, and a contrast says the run from %s stops: the plan\'s own run '
                              'must pass from its start' % (home, home))
        vouched = {'unit': unit, 'steps': steps, 'flash': flash_sha, 'refs': ref_sums}
        if send_sums:                 # left out otherwise, so earlier passes still count
            vouched['sends'] = send_sums
        if home:
            vouched['start'] = home
        old_key = hashlib.sha256(json.dumps(vouched, sort_keys=True).encode()).hexdigest()
        if os_sha:                    # left out otherwise, so earlier passes still count
            vouched['os'] = os_sha
        key = hashlib.sha256(json.dumps(vouched, sort_keys=True).encode()).hexdigest()
        writes = [l for _, lines in steps for l in lines
                  if l.split()[0].lower() in ('save-project', 'save-kit', 'send')]
        self.check_stats(unit, flash_sha, {l.split()[0].lower() for _, lines in steps for l in lines
                                           if l.split()}, 'the OS this plan flashes')
        # snapshot-after = SECTION: the steps up to and including SECTION are setup, which an
        # emulator run may skip by resuming a snapshot taken after a passing run of them.
        after = settings.get('snapshot-after')
        setup = None
        if after:
            names = [n for n, _ in steps]
            if after not in names:
                raise Refused('snapshot-after names no section of the plan: %r' % after)
            setup = names.index(after) + 1
        cycles = power_cycles(steps)
        if cycles:
            self.check_power_cycles(unit, steps, flash, setup)
        # the plan checker's rules (plancheck.py): errors refuse the go, flags go into it
        check = plancheck.check(steps, unit=unit, refs=refs, flash=flash,
                                stock=bool(flash_sha) and self.is_stock(unit, flash_sha), os_file=os_file,
                                emulator=settings.get('emulator', 'no').lower() == 'yes',
                                starts=tuple(unit_starts(unit)))
        return dict(path=path, here=here, settings=settings, steps=steps, unit=unit, key=key, check=check,
                    old_key=old_key, os_file=os_file, os_sha=os_sha, flash=flash, flash_sha=flash_sha, refs=refs, writes=writes, setup=setup, din=bool(din),
                    audio_waits=audio_waits(steps), power_cycles=cycles, home=home,
                    name=settings.get('name') or os.path.splitext(os.path.basename(path))[0],
                    emulator=settings.get('emulator', 'no').lower() == 'yes', rel=rel)

    def check_power_cycles(self, unit, steps, flash, setup):
        """Refused unless this plan's `power-cycle` steps can run: on the Rytm and A4 (analog-emu's
        --power-off), alone on their line, outside (hardware) sections and the setup, in a plan
        that flashes nothing, and each followed by steps (the readback the power cycle is for)."""
        if UNITS[unit].get('emulator') == 'digiemu' or not UNITS[unit].get('emu_exe'):
            raise Refused('power-cycle runs on the Rytm and A4 only so far (analog-emu\'s --power-off); '
                          'the %s emulator has no power-off save' % unit)
        if flash:
            raise Refused('a plan with power-cycle cannot flash: flash in a plan of its own, then power cycle')
        for i, (name, lines) in enumerate(steps):
            for l in lines:
                if not is_power_cycle(l):
                    continue
                if len(shlex.split(l, comments=True)) > 1 or ';' in l:
                    raise Refused('[%s] %r: power-cycle takes nothing more, on a line of its own' % (name, l))
                if name.endswith('(hardware)'):
                    raise Refused('[%s] power-cycle in a (hardware) section: the emulator skips those, so its '
                                  'pass would not cover the power cycle' % name)
                if setup and i < setup:
                    raise Refused('[%s] power-cycle in the setup (snapshot-after): a setup snapshot cannot '
                                  'hold a power cycle; put it after the setup' % name)
        for n, part in enumerate(power_cycle_parts(steps)[1:], 1):
            if not any(plancheck.is_assert(plancheck.words(l)) for _, ls in part for l in ls):
                raise Refused('power cycle %d has no machine assertion after it (an expect-screen of what it '
                              'had to keep, before the next power cycle or the end): a readback judged by eye '
                              'proves nothing about the card' % n)

    def snapshot_key(self, info, build):
        """What a setup snapshot vouches for: the unit, the emulator binary, the build and the
        card it booted, and the setup steps with the reference screens they check."""
        u = UNITS[info['unit']]
        exe = os.path.join(ANALOG_EMU or '', u.get('emu_exe') or '')
        setup = info['steps'][:info['setup']]
        refs = sorted({shlex.split(l)[1] for _, ls in setup for l in ls
                       if l.split()[0].lower() == 'expect-screen'})
        ref_sums = [(r, sha256(os.path.join(info['refs'], r if r.endswith('.bin') else r + '.bin')))
                    for r in refs if os.path.isfile(os.path.join(info['refs'], r if r.endswith('.bin') else r + '.bin'))]
        card = u.get('emu_card')
        return hashlib.sha256(json.dumps({
            'unit': info['unit'], 'emulator': sha256(exe) if os.path.isfile(exe) else None,
            'build': sha256(build), 'card': sha256(card) if card and os.path.isfile(card) else None,
            'setup': setup, 'refs': ref_sums}, sort_keys=True).encode()).hexdigest()

    def twin(self, info, record_refs, sid, emulator_run=None, sections=None, tag='', hardware=False):
        """An emulator twin of a hardware plan, with every path setting made absolute. Its runs
        go in a folder of the session's own (emulator runs of several sessions may start in the
        same second, and testplan names a run folder by the second). With emulator_run, a
        hardware twin instead: the plan as written, plus the `emulator-run` its flash needs (the
        recorded emulator pass), so nobody has to add that line by hand. With hardware (and
        sections), a hardware twin of those sections only: one part of a plan with power-cycle."""
        g, rel = info['settings'].get, info['rel']
        hardware = hardware or bool(emulator_run)
        if hardware:
            lines = ['# written by elkbench for a hardware run of %s' % info['path'],
                     'include = %s' % info['path']] + (
                ['emulator-run = %s' % emulator_run] if emulator_run else []) + [
                     'refs = %s' % info['refs'], 'out = %s' % rel(g('out', 'runs'))]
        else:
            lines = ['# written by elkbench for an emulator run of %s' % info['path'],
                     'include = %s' % info['path'], 'emulator = yes',
                     'port = %s' % UNITS[info['unit']]['emu'],
                     'din =',                  # the unit's interface is not the emulator's
                     'record-refs = %s' % ('yes' if record_refs else 'no'),
                     'refs = %s' % info['refs'],
                     'out = %s' % os.path.join(rel(g('out', 'runs')), 'emu-%s' % sid[:8])]
        for k in ('flash', 'os', 'audio-tools', 'elkflash'):
            if g(k):
                lines.append('%s = %s' % (k, rel(g(k))))
        if g('flash') and not g('elkflash') and ELKFLASH:       # the plan names none: config's checkout
            lines.append('elkflash = %s' % ELKFLASH)
        if not hardware:                               # `wait N audio` is short in the emulator
            if sections is None and info.get('audio_waits'):
                sections = info['steps']
            if sections is not None:
                sections = emulator_steps(sections)
        if sections is not None:                       # only these sections (setup / the rest)
            lines.append('name = %s%s' % (info['name'], tag))
            for name, body in sections:
                lines += ['', '[%s]' % name] + body
        os.makedirs(os.path.join(STATE, 'twins'), exist_ok=True)
        p = os.path.join(STATE, 'twins', '%s-%s-%s%s%s.txt' % (
            re.sub(r'\W+', '-', info['name']), info['key'][:8], sid[:8], '-hw' if hardware else '',
            re.sub(r'\W+', '-', tag)))
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
        return p

    # -- approvals --

    def save_approvals(self):
        """An expired go is kept a day, so using it says it expired rather than unknown."""
        with self.lock:
            for t in [t for t, a in self.approvals.items() if time.time() > a['expires'] + 86400]:
                del self.approvals[t]
            write_json(self.approvals_path, self.approvals)

    def use_approval(self, s, token, item, label):
        with self.lock:
            a = self.approvals.get(token or '')
            if not a and token:
                raise Refused('%s: approval %s is unknown (not a token approve_plan gave, or a day past '
                              'its expiry); ask %s again with approve_plan' % (label, token, PERSON))
            if not a:
                raise Refused('%s needs a go from %s: approve_plan, then pass its token' % (label, PERSON))
            if a['session'] != s.sid:
                raise Refused('that approval belongs to another session')
            if a.get('ended'):
                raise Refused('that go was ended at %s (end_go): %s' % (stamp(a['ended']), a.get('ended_why', '')))
            if time.time() > a['expires']:
                raise Refused('that approval expired at %s; ask again' % stamp(a['expires']))
            # A go may list the same item more than once (one build flashed again): each
            # listing runs once, so take the first one not yet used.
            same = [it for it in a['items'] if tuple(it['item']) == item]   # lists once through the file
            for it in same:
                if not it['used']:
                    it['used'] = True
                    self.save_approvals()
                    return
            if same:
                raise Refused('%s was approved %s and has run; a new run needs a new go'
                              % (label, 'once' if len(same) == 1 else '%d times' % len(same)))
            raise Refused('the approval does not cover %s (it covers: %s)' % (
                label, '; '.join(it['label'] for it in a['items'])))

    def open_goes(self):
        """-> {token: go} for the gos still usable: not ended, not expired, an item not yet run."""
        with self.lock:
            return {t: a for t, a in self.approvals.items() if not a.get('ended')
                    and time.time() < a['expires'] and not all(it['used'] for it in a['items'])}

    def asking(self, s):
        """This session's go requests still waiting for an answer (client prompt, popup or thread)."""
        return [j for j in self.jobs.values()
                if j.kind == 'approval' and j.sess is s and j.state in ('asking', 'running')]

    def one_go_at_a_time(self, s):
        """A second go next to an unused one (a request asked twice, or answered both in the
        thread and on the popup) only holds off a restart: refuse while one is open or asked."""
        for j in self.asking(s):
            raise Refused('your go request %s is still waiting for %s\'s answer: status with run_id %s '
                          'gives it, or end_go with request %s withdraws it' % (j.id, PERSON, j.id, j.id))
        mine = sorted(t for t, a in self.open_goes().items() if a['session'] == s.sid)
        if mine:
            raise Refused('you have an open go (%s): use it, or end_go it before asking for a new one'
                          % ', '.join(mine))

    def t_end_go(self, s, a):
        """End a go nobody will use, so it no longer holds off a restart for newer code. Ending only
        takes a go away, so any session may end any go, but never one whose session has a hardware
        run going. With `request`, withdraw this session's own go request still waiting for an
        answer. With neither, list the open gos and requests."""
        why = (a.get('note') or '').strip() or 'no reason given'
        rid, token = a.get('request'), a.get('approval')
        if rid:
            job = self.jobs.get(rid)
            if not job or job not in self.asking(s):
                raise Refused('no go request %s of this session is waiting for an answer' % rid)
            out = job.settle(False, 'end_go (withdrawn by the session: %s)' % why)
            note = ' (the popup on the PC stays up until it is answered or times out; its answer is ignored)' \
                if self.dialog.locked() else ''
            return ['ok: request %s withdrawn%s' % (rid, note)] + out[1:]
        if not token:
            with self.lock:
                goes = self.open_goes()
                asked = [j for j in self.jobs.values() if j.kind == 'approval' and j.state in ('asking', 'running')]
                lines = ['%s: session %s, covers %s, %d of %d run, expires %s' % (
                    t, g['session'][:8], '; '.join(it['label'] for it in g['items']),
                    sum(it['used'] for it in g['items']), len(g['items']), stamp(g['expires']))
                    for t, g in sorted(goes.items())]
                lines += ['%s: go request of session %s still waiting for an answer' % (j.id, j.sess.sid[:8])
                          for j in asked]
            return ['ok: %d open go(s), %d request(s) waiting' % (len(goes), len(asked))] + lines
        with self.lock:
            g = self.approvals.get(token)
            if not g:
                raise Refused('no go %s (unknown, or a day past its expiry)' % token)
            if g.get('ended'):
                raise Refused('go %s was already ended at %s' % (token, stamp(g['ended'])))
            if time.time() > g['expires']:
                raise Refused('go %s already expired at %s' % (token, stamp(g['expires'])))
            going = [j for j in self.running() if j.sess.sid == g['session'] and j.target == 'hardware'
                     and j.kind not in ('approval', 'heard')]
            if going:
                raise Refused('go %s has a run going (%s): let it end first' % (
                    token, ', '.join('%s %s' % (j.id, j.desc) for j in going)))
            g['ended'], g['ended_why'] = time.time(), why
            g['ended_by'] = s.sid
            self.save_approvals()
        self.log_line({'session': s.sid, 'end_go': token, 'of': g['session'], 'why': why,
                       'unused': [it['label'] for it in g['items'] if not it['used']]})
        return ['ok: go %s ended (%s); it no longer holds off a restart' % (
            token, 'your own' if g['session'] == s.sid else 'of session %s' % g['session'][:8])]

    def give_back(self, token, item):
        """A run that never started (refused after the approval was checked) keeps its go."""
        with self.lock:
            for it in reversed(self.approvals.get(token, {}).get('items', [])):
                if tuple(it['item']) == item and it['used']:      # the one use_approval took
                    it['used'] = False
                    self.save_approvals()
                    return

    # -- jobs --

    def start(self, job, cmd, cwd, done, port=None):
        """Run cmd in the background; done(job, rc, output) sets the verdict lines."""
        os.makedirs(os.path.dirname(job.log), exist_ok=True)
        with self.lock:
            self.jobs[job.id] = job

        def work():
            try:
                with open(job.log, 'w', encoding='utf-8') as log:
                    log.write('$ %s\n(cwd %s)\n' % ('; '.join(
                        getattr(c, '__name__', None) or subprocess.list2cmdline(c)
                        for c in (cmd if isinstance(cmd[0], (list, tuple)) or callable(cmd[0]) else [cmd])), cwd))
                    log.flush()
                    # A plan's `listen` step writes ANALOGRUN_LISTEN_FILE and waits until it is
                    # gone: listen_done removes it once the person has said they have finished.
                    env = dict(job.sess.env(), ANALOGRUN_LISTEN_FILE=job.listen_file)
                    if DIGIHEALTH:                # testplan finds digirun.py for Digitone plans here
                        env['DIGIRUN_PATH'] = os.path.join(DIGIHEALTH, 'tools')
                    if job.target == 'hardware':
                        threading.Thread(target=self.watch_listen, args=(job,), daemon=True).start()
                    # cmd is one command, or stages: commands and callables (rc, out) -> (rc,
                    # out), run in order until one fails (a setup run, its snapshot, the rest)
                    rc, out = 0, ''
                    for stage in (cmd if cmd and isinstance(cmd[0], (list, tuple)) or callable(cmd[0])
                                  else [cmd]):
                        if callable(stage):
                            rc, more = stage(rc, out)
                        else:
                            p = subprocess.run(stage, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                               stderr=subprocess.STDOUT, text=True, encoding='utf-8',
                                               errors='replace', **NOWIN)
                            rc, more = p.returncode, p.stdout
                        log.write(more)
                        log.flush()
                        out += more
                        if rc:
                            break
                done(job, rc, out)
            except Exception as e:                # noqa: BLE001 - becomes the job's verdict
                job.state, job.lines = 'fail', ['%s: %s (log %s)' % (type(e).__name__, e, job.log)]
            finally:
                job.ended = time.time()
                self.last_use = time.time()
                if port:
                    port.release()
                self.log_line({'session': job.sess.sid, 'job': job.id, 'end': job.state,
                               'lines': job.lines[:3]})

        threading.Thread(target=work, daemon=True).start()
        return ['running: %s %s; status with run_id %s waits for its end' % (job.id, job.desc, job.id)]

    def watch_listen(self, job):
        """While a hardware run goes on: when it stops at a `listen` step, mark the unit as
        listened to in elklock (nothing may drive it, the claim cannot time out)."""
        while job.state == 'running' and not job.ended:
            if job.listening is None and os.path.exists(job.listen_file):
                flag = read_json(job.listen_file, {})
                what = flag.get('what') or '(see the plan)'
                job.power_cycle = bool(flag.get('power_cycle'))
                job.listening = what
                subprocess.run([sys.executable, ELKLOCK, 'listen', job.unit, '--for', what],
                               env=job.sess.env(), capture_output=True, timeout=30, **NOWIN)
                self.log_line({'session': job.sess.sid, 'job': job.id, 'listen': what})
            time.sleep(0.5)

    def log_line(self, rec):
        rec = dict(rec, time=stamp(), t=round(time.time(), 1))
        with self.lock, open(os.path.join(STATE, 'calls.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(rec) + '\n')

    def running(self):
        return [j for j in self.jobs.values() if j.state == 'running']

    # -- newer code on disk --

    def newer_code(self):
        """The newest mtime of the daemon's own code if it changed since this daemon started."""
        t = max(os.path.getmtime(f) for f in CODE)
        return t if t > STARTED else None

    def restart_waits_for(self):
        """What newer code on disk waits for: everything a restart would cut short or forget.
        Empty once elklock has every unit free (no claim, queued claim or listening), no go is
        open and this daemon has nothing going, so sessions can land elkbench code at any time
        without looking at claims themselves."""
        why = []
        with self.lock:
            if self.running():
                why.append('a run is going')
            if any(j.state == 'asking' for j in self.jobs.values()):
                why.append('a go is being asked')
            if any(e['proc'].poll() is None for e in self.emulators.values()):
                why.append('an emulator runs')      # keep stop working
            goes = self.open_goes()
            if goes:
                why.append('a go is open (until %s at the latest; end_go ends an unused one: %s)' % (
                    stamp(max(a['expires'] for a in goes.values())), ', '.join(sorted(goes))))
            if self.connections > 1:            # this call counts one
                why.append('another call is in progress')
        if self.dialog.locked():
            why.append('an approval dialog is open')
        for u in UNITS:
            if elklock.holder(u) is not None:
                why.append('%s is claimed' % u)
            elif elklock.tickets(u):
                why.append('%s has a claim queued' % u)
        return why

    def keeps_awake(self):
        """Why the PC must not sleep now (empty: it may): a unit claimed or queued for, a run, an
        emulator, a go being asked or its dialog open. Sleep would cut a flash or a listening test
        short and drop the client's remote session."""
        with self.lock:
            why = (['a run is going'] if self.running() else []) + \
                  (['a go is being asked'] if any(j.state == 'asking' for j in self.jobs.values()) else []) + \
                  (['an emulator runs'] if any(e['proc'].poll() is None for e in self.emulators.values()) else [])
        if self.dialog.locked():
            why.append('an approval dialog is open')
        why += ['%s is claimed' % u for u in UNITS if elklock.holder(u) is not None or elklock.tickets(u)]
        return why

    def code_starts(self, t):
        """None if the code on disk imports and loads the config, else why not (once per change):
        broken code stays on disk and this daemon keeps serving."""
        if t not in self.checked:
            try:
                r = subprocess.run([sys.executable, os.path.join(HERE, 'daemon.py'), '--check'], cwd=HERE,
                                   capture_output=True, text=True, timeout=60, **NOWIN)
                self.checked[t] = None if r.returncode == 0 else \
                    (r.stderr.strip().splitlines() or ['exit %d' % r.returncode])[-1]
            except (OSError, subprocess.TimeoutExpired) as e:
                self.checked[t] = str(e)
            if self.checked[t]:
                self.log_line({'session': 'daemon', 'daemon': 'newer code does not start',
                               'reason': self.checked[t]})
        return self.checked[t]

    def save_jobs(self):
        """Before a restart: each run's summary, so status run_id still answers afterwards."""
        os.makedirs(os.path.join(STATE, 'jobs'), exist_ok=True)
        with self.lock:
            for j in self.jobs.values():
                write_json(os.path.join(STATE, 'jobs', j.id + '.json'), {'summary': j.summary()})

    # ---- the tools ----------------------------------------------------------------------

    def units(self, a):
        try:
            return elklock.devices([a['unit']] if isinstance(a.get('unit'), str) else a.get('unit') or [])
        except SystemExit as e:
            raise Refused(str(e).replace('elklock: ', ''))

    def t_claim(self, s, a):
        units = self.units(a)
        purpose = (a.get('purpose') or '').strip()
        if not purpose:
            raise Refused('say what the claim is for (purpose)')
        wait = min(int(a.get('wait_minutes') or 0), 10)
        cmd = [sys.executable, ELKLOCK, 'claim'] + units + ['--for', purpose]
        if wait:
            cmd += ['--wait', '--timeout', '%dm' % wait]
        r = subprocess.run(cmd, env=s.env(), capture_output=True, text=True, timeout=wait * 60 + 60,
                           **NOWIN)
        out = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        if r.returncode:
            raise Refused('BUSY, nothing claimed: ' + ' | '.join(out[:3]) +
                          ' (wait, or tell the person who holds it; never break it)')
        for u in units:
            s.purpose[u] = purpose
            s.done.setdefault(u, [])
        handoff = [l for l in out if l.startswith(('last handoff', 'os:', 'active_project:'))]
        return ['ok: claimed %s for "%s"' % (', '.join(units), purpose)] + handoff[:3]

    def t_release(self, s, a):
        units = self.units(a)
        note = (a.get('note') or '').strip()
        if not note:
            raise Refused('say what state you leave (note): OS, project, menus')
        mine = [u for u in units if elklock.mine(elklock.holder(u), session=s.sid)]
        if not mine:
            raise Refused('this session holds none of %s' % ', '.join(units))
        for u in mine:
            h = elklock.holder(u) or {}
            if h.get('listening'):
                raise Refused('%s is still listening to the %s (%s): release only after they say they '
                              'have finished (listen_done)' % (PERSON, u, h['listening'].get('what', '')))
        if any(j.sess is s and j.unit in mine and j.state == 'running' and j.target == 'hardware'
               for j in self.running()):
            raise Refused('a hardware run of yours is still going; release when it ends (status)')
        off = [u for u in mine if s.os_left.get(u) and not s.os_left[u][1]]
        if off and not a.get('accept_non_stock'):
            raise Refused('%s is left on %s, not stock: run the stock-restore plan first, or say why '
                          'in the note and pass accept_non_stock' % (off[0], s.os_left[off[0]][0]))
        # Every run leaves the test slots empty, so the next one finds them so (a plan still
        # checks): what they hold is backed up into the session's evidence folder, then erased.
        cleared = {}
        for u in mine:
            if not (UNITS[u].get('clear_at_release') and UNITS[u]['runner'] is True):
                continue
            ok, lines = self.empty_test_slots(s, u)
            if not ok and not a.get('accept_slots_left'):
                raise Refused('%s: the test slots are not empty, so the claim is kept: %s. Fix it and release '
                              'again, or say why in the note and pass accept_slots_left' % (u, '; '.join(lines)))
            cleared[u] = (ok, lines)
        out = ['ok: released %s' % ', '.join(mine)]
        for u in mine:
            if u in cleared:
                ok, lines = cleared[u]
                out += ['%s: %s' % (u, l) for l in lines]
                note_u = '%s; test slots %s %s' % (note, slot_text(UNITS[u]['clear_at_release']),
                                                   'empty (data ls)' if ok else 'NOT EMPTY: ' + '; '.join(lines))
            else:
                note_u = note
            done = s.done.get(u, [])
            os_text = ('%s (%s)' % (s.os_left[u][0], 'stock' if s.os_left[u][1] else 'NOT STOCK')
                       if u in s.os_left else 'not flashed by this session')
            full = '%s; runs: %s; OS: %s' % (note_u, '; '.join(done) or 'none', os_text)
            # a scratch ELKLOCK_DIR is a test: no device reads for the snapshot then
            snap = ['--no-snapshot'] if os.environ.get('ELKLOCK_DIR') else []
            r = subprocess.run([sys.executable, ELKLOCK, 'release', u, '--note', full[:900]] + snap, env=s.env(),
                               capture_output=True, text=True, timeout=180, **NOWIN)
            if r.returncode:
                out[0] = 'fail: elklock release %s: %s' % (u, (r.stdout + r.stderr).strip()[-200:])
                continue
            row = '| %s | %s | %s | %s | %s | %s |' % (
                stamp(), s.sid[:8], s.purpose.get(u, '?').replace('|', '/'),
                ('<br>'.join(done) or 'none').replace('|', '/'), os_text, note.replace('|', '/'))
            log = os.path.join(UNITS[u]['evidence'], 'test-log.md')
            os.makedirs(os.path.dirname(log), exist_ok=True)
            new = not os.path.exists(log)
            with open(log, 'a', encoding='utf-8') as fh:
                if new:
                    fh.write('# Test log (%s)\n\nWritten by elkbench on each release.\n\n'
                             '| released | session | purpose | runs | OS left | note |\n'
                             '|---|---|---|---|---|---|\n' % u)
                fh.write(row + '\n')
            out.append('%s: handoff note written, test log %s' % (u, log))
            s.done.pop(u, None)
            s.purpose.pop(u, None)
            s.os_left.pop(u, None)
        return out

    def empty_test_slots(self, s, u):
        """At release: back up and erase every project in the unit's `clear_at_release` slots
        (analogrun `data clear`, which asks the local write policy and keeps the copy), then list
        the projects to show them empty. -> (ok, lines)."""
        slots = UNITS[u]['clear_at_release']
        try:
            self.check_connected(u, 'hardware')
        except Refused as e:
            return False, ['not emptied: %s' % e]
        script, folder = runner(), os.path.join(s.folder(u), 'release-clear-%s' % time.strftime('%H%M%S'))
        port = self.busy_port(u, 'hardware', 'release: empty the test slots')
        try:
            def run(*args):
                return subprocess.run([sys.executable, ELKLOCK, 'run', u, '--', sys.executable, script, '--port',
                                       UNITS[u]['port'], 'data'] + list(args),
                                      cwd=os.path.dirname(os.path.dirname(script)), env=s.env(),
                                      capture_output=True, text=True, encoding='utf-8', errors='replace',
                                      timeout=300, **NOWIN)
            os.makedirs(folder, exist_ok=True)
            log, lines = [], []
            r = run('ls', '/projects')
            log.append('$ data ls /projects\n' + r.stdout + r.stderr)
            if r.returncode:
                return False, ['data ls /projects failed: %s' % (r.stdout + r.stderr).strip()[-200:]]
            for n in sorted(set(slots) & occupied_slots(r.stdout)):
                backup = os.path.join(folder, 'slot%d-backup.bin' % n)
                c = run('clear', '/projects/%d' % n, backup, '--yes')
                log.append('$ data clear /projects/%d %s\n%s%s' % (n, backup, c.stdout, c.stderr))
                if c.returncode:
                    lines.append('slot %d not cleared (%s)' % (
                        n, 'the write policy refused' if c.returncode == 2 else 'exit %d' % c.returncode))
                else:
                    lines.append('slot %d backed up (%s) and cleared' % (n, backup))
            r = run('ls', '/projects')
            log.append('$ data ls /projects\n' + r.stdout + r.stderr)
            if r.returncode:
                return False, lines + ['data ls /projects after the clear failed']
            left = sorted(set(slots) & occupied_slots(r.stdout))
            listing = [l.rstrip() for l in r.stdout.splitlines() if l.strip()]
            os.makedirs(os.path.join(STATE, 'unit-state'), exist_ok=True)       # for unit_state
            write_json(os.path.join(STATE, 'unit-state', u + '.json'),
                       {'time': time.time(), 'session': s.sid, 'projects': listing,
                        'listing': os.path.join(folder, 'steps.log')})
            if left:
                return False, lines + ['slot(s) %s still hold a project' % slot_text(left)]
            return True, lines + ['test slots %s empty (data ls)' % slot_text(slots)]
        finally:
            port.release()
            with open(os.path.join(folder, 'steps.log'), 'a', encoding='utf-8') as fh:
                fh.write('\n'.join(log) + '\n')

    def t_status(self, s, a):
        rid = a.get('run_id')
        if rid:
            j = self.jobs.get(rid)
            if not j:
                kept = read_json(os.path.join(STATE, 'jobs', os.path.basename(rid) + '.json'), None)
                if kept:                          # it ended before a restart for newer code
                    return kept['summary']
                raise Refused('no run %s' % rid)
            # Wait here for the end rather than have the session poll: one call per run.
            deadline = time.time() + max(0, min(STATUS_WAIT_MAX, float(a.get('wait_s', STATUS_WAIT))))
            while j.state in ('running', 'asking') and time.time() < deadline and not j.listening:
                time.sleep(0.5)
            if j.state == 'running' and j.listening and j.power_cycle:
                return j.summary() + [
                    'power cycle: the run waits for a power cycle by hand: %s. Ask %s in your thread to '
                    'do exactly that and to say when it is done, then wait for their reply, however long it '
                    'takes; send nothing to the unit. When they say it is done, call listen_done with the '
                    'id of that reply, then status again.' % (j.listening, PERSON)]
            if j.state == 'running' and j.listening:
                return j.summary() + [
                    'listening: the run waits at a listen step: %s. Tell %s in your thread exactly what '
                    'to listen for, then wait for their reply, however long it takes; send nothing to '
                    'the unit. When they say they have finished, call listen_done with the id of that '
                    'reply, then status again.' % (j.listening, PERSON)]
            return j.summary()
        out = []
        for u in UNITS:
            h = elklock.holder(u)
            if elklock.mine(h, session=s.sid):
                out.append('held: %s for "%s"' % (u, h.get('purpose', '')))
        mine = [j for j in self.jobs.values() if j.sess is s]
        out += [j.summary()[0] for j in mine if j.state == 'running']
        out += [j.summary()[0] for j in mine if j.state != 'running'][-3:]
        t = self.newer_code()
        if t and self.checked.get(t):
            out.append('newer elkbench code (%s) does not start, so this one keeps serving: %s'
                       % (stamp(t), self.checked[t]))
        elif t:
            why = self.restart_waits_for()
            out.append('newer elkbench code (%s) starts %s' % (
                stamp(t), 'with the next call' if not why else 'when nothing holds it: ' + '; '.join(why)))
        return ['ok: %d claim(s), %d run(s) going' % (
            sum(l.startswith('held') for l in out), sum(j.state == 'running' for j in mine))] + out

    def go_request(self, s, a):
        """-> (items, body): what approve_plan would ask for these plans, flashes and recall
        restores, word for word, or Refused with the reason it would refuse. Asks nobody."""
        items, text, infos = [], [], []
        for p in a.get('plans') or []:
            info = self.plan_info(p)
            if info['emulator']:
                raise Refused('%s is an emulator plan; approve the hardware plan' % p)
            if info['check']['errors']:
                raise Refused('%s is refused by the plan checker: %s' % (
                    info['name'], '; '.join(info['check']['errors'])))
            infos.append(info)
            ep = self.plan_pass(info)
            if not ep:
                raise Refused('%s has no passing emulator run of the same steps, refs and OS file: '
                              'run_plan target emulator first' % info['name'])
            missing, start_lines = self.start_lines(info, ep.get('os'))
            if missing:
                raise Refused('%s has no emulator run from start %s on the build of its pass: batch gate '
                              'true with this plan runs them' % (info['name'], ', '.join(missing)))
            items.append(dict(item=('plan', info['key']), label='plan ' + info['name'], used=False))
            hand = hand_steps(info['steps'])
            text.append('PLAN %s on %s\n  %s\n  steps: %s%s%s\n  emulator pass: %s%s%s%s%s' % (
                info['name'], info['unit'], info['path'], ', '.join(n for n, _ in info['steps']),
                '\n  FLASH %s\n    sha256 %s%s' % (os.path.basename(info['flash']), info['flash_sha'],
                                                   ' (stock)' if self.is_stock(info['unit'], info['flash_sha'])
                                                   else '') if info['flash'] else '',
                ('\n  runs on %s (sha256 %s)' % (os.path.basename(info['os_file']), info['os_sha'])
                 if info['os_file'] else '') +
                ('\n  writes: ' + '; '.join(info['writes']) if info['writes'] else ''),
                ep.get('run_dir') or stamp(ep['time']),
                ' (from %s)' % ep['start'] if ep.get('start') else '',
                '\n  ran after: ' + ', '.join(ep['after']) if ep.get('after') else '',
                ''.join('\n' + l for l in start_lines + plancheck.go_lines(info['check'])),
                ''.join('\n  NOT TESTED IN THE EMULATOR: ' + h for h in hand) +
                ''.join('\n  POWER CYCLE BY HAND (the run waits until you say it is done; the emulator pass '
                        'ran its power-off save and a cold boot): ' + c for c in info['power_cycles']) +
                ''.join('\n  SHORT IN THE EMULATOR, IN FULL ON THE UNIT: ' + w for w in info['audio_waits'])))
        shared = plancheck.shared_refs(infos)
        if shared:
            raise Refused('; '.join(shared))
        for kind in ('flashes', 'recall_restores'):
            for f in a.get(kind) or []:
                self.unit(f.get('unit'))
                img = os.path.abspath(f.get('image', ''))
                if not os.path.isfile(img):
                    raise Refused('no file %s' % img)
                d = sha256(img)
                if kind == 'flashes':
                    over = self.os_on_unit(s, f['unit'], f.get('over'))
                    if not self.flash_pass(f['unit'], d, over):
                        raise Refused('%s has not flashed over %s and booted in the %s emulator: flash target '
                                      'emulator first, on an emulator booted on that OS' % (
                                          os.path.basename(img), os.path.basename(f['over']) if f.get('over')
                                          else 'the OS this session flashed last', f['unit']))
                    items.append(dict(item=('flash', f['unit'], d), used=False,
                                      label='flash %s on %s' % (os.path.basename(img), f['unit'])))
                    text.append('FLASH %s on %s\n  sha256 %s%s\n  over %s (sha256 %s)' % (
                        os.path.basename(img), f['unit'], d, ' (stock)' if self.is_stock(f['unit'], d) else '',
                        os.path.basename(f['over']) if f.get('over') else 'the OS this session flashed last',
                        over))
                else:
                    items.append(dict(item=('restore', f['unit'], d), used=False,
                                      label='recall restore %s on %s' % (os.path.basename(img), f['unit'])))
                    text.append('TOTAL RECALL RESTORE %s on %s (replaces the active project)\n  sha256 %s'
                                % (os.path.basename(img), f['unit'], d))
        if not items:
            raise Refused('nothing to approve: give plans, flashes or recall_restores')
        for u in sorted({i['unit'] for i in infos} | {f.get('unit') for f in a.get('flashes') or []}
                        | {f.get('unit') for f in a.get('recall_restores') or []}):
            if UNITS.get(u, {}).get('clear_at_release') and UNITS[u]['runner'] is True:
                text.append('AT RELEASE on %s, however the run ends: project slots %s, whatever they hold, '
                            'backed up to the run folder and erased (data clear), then listed to show them empty'
                            % (u, slot_text(UNITS[u]['clear_at_release'])))
        purpose = (a.get('purpose') or '').strip() or '(no purpose given)'
        body = ('An AI session asks for one go for this hardware run.\n\nSession %s (%s)\n'
                'Purpose: %s\n\n%s\n\nEach item runs once; the go expires in %d h.\n'
                'Approve?' % (s.sid[:8], s.cwd, purpose, '\n\n'.join(text), APPROVAL_TTL // 3600))
        return items, body

    def idle_units(self, s):
        """Units this session holds with nothing going on them (no run, nobody listening)."""
        return [u for u in UNITS if elklock.mine(elklock.holder(u), session=s.sid)
                and not (elklock.holder(u) or {}).get('listening')
                and not any(j.sess is s and j.unit == u and j.target == 'hardware' for j in self.running())]

    def t_approval_text(self, s, a):
        """The go request approve_plan would make, word for word, with its text id: post it in the
        thread, and after the person's yes call approve_plan with that text_id. Asks nobody."""
        items, body = self.go_request(s, a)
        tid = hashlib.sha256(body.encode()).hexdigest()[:12]
        out = ['ok: approve_plan would ask for %d item(s); text_id %s' % (len(items), tid)]
        idle = self.idle_units(s)
        if idle:
            out.append('note: approve_plan refuses while you hold %s idle: release first' % ', '.join(idle))
        try:
            self.one_go_at_a_time(s)
        except Refused as e:
            out.append('note: approve_plan refuses now: %s' % e)
        return out + ['', body]

    def t_check_plan(self, s, a):
        """The plan checker on each plan, and whether a go for it would be accepted now (its
        emulator pass, its OS file's flash pass, references shared with the other plans)."""
        plans = a.get('plans') or []
        if not plans:
            raise Refused('give plans: hardware plan files')
        out, infos, bad = [], [], 0
        for p in plans:
            try:
                info = self.plan_info(p)
            except Refused as e:
                out.append('refused: %s: %s' % (p, e))
                bad += 1
                continue
            c = info['check']
            gate = []
            if info['emulator']:
                gate.append('an emulator plan: check the hardware plan')
            else:
                ep = self.plan_pass(info)
                if not ep:
                    gate.append('no emulator pass of these steps, refs and OS file yet')
                missing, lines = self.start_lines(info, (ep or {}).get('os'))
                gate += ['no run from start %s yet (batch gate runs it)' % m for m in missing]
            infos.append(info)
            head = 'refused' if c['errors'] or info['emulator'] else 'ok'
            bad += head == 'refused'
            out.append('%s: %s (%s)%s' % (head, info['name'], info['path'],
                                          ': the plan checker would refuse it' if c['errors'] else ''))
            out += ['  ERROR: ' + e for e in c['errors']] + plancheck.go_lines(c) + ['  gate: ' + g for g in gate]
        for e in plancheck.shared_refs(infos):
            out.append('refused: ' + e)
            bad += 1
        return ['%s: %d plan(s) checked, %d refused' % ('ok' if not bad else 'refused', len(plans), bad)] + out

    def t_unit_state(self, s, a):
        """What is known about a unit without touching it, each with its age: the claim now, the
        last handoff note (OS, active project), the last project listing a hardware `data ls`
        saw, and the last test-log row."""
        u = a.get('unit')
        self.unit(u)
        def age(t):
            if not t:
                return 'time unknown'
            d = time.time() - t
            return '%s, %s ago' % (stamp(t), '%d min' % (d // 60) if d < 7200 else '%.1f h' % (d / 3600)
                                    if d < 172800 else '%d days' % (d // 86400))
        h = elklock.holder(u)
        out = ['ok: %s as known now (%s); nothing was read from the unit' % (u, stamp()),
               'claim: ' + elklock.describe(u, h).split(None, 1)[1]]
        ho = read_json(elklock.path(u, '.handoff.json'), {})
        if ho:
            out.append('last handoff (%s, %s): %s' % (age(ho.get('time')), ho.get('ended', '?'), ho.get('note', '')))
            out += ['  %s: %s' % (k, ho[k]) for k in ('os', 'active_project') if ho.get(k)]
        else:
            out.append('last handoff: none recorded')
        ls = read_json(os.path.join(STATE, 'unit-state', u + '.json'), {})
        if ls.get('projects'):
            out.append('projects (hardware data ls, %s):' % age(ls.get('time')))
            out += ['  ' + l for l in ls['projects'][:40]]
        else:
            out.append('projects: no hardware listing recorded yet (data ls /projects target hardware)')
        log = os.path.join(UNITS[u]['evidence'], 'test-log.md')
        try:
            with open(log, encoding='utf-8') as fh:
                rows = [l.strip() for l in fh if l.startswith('| 2')]
            out.append('last test-log row: ' + (rows[-1] if rows else 'none'))
        except OSError:
            out.append('last test-log row: no test log yet')
        return out + ['A claim and a fresh look at the unit (OS, data ls) still come before any write.']

    def t_approve_plan(self, s, a):
        items, body = self.go_request(s, a)
        self.one_go_at_a_time(s)
        tid = a.get('text_id')
        if tid and hashlib.sha256(body.encode()).hexdigest()[:12] != tid:
            raise Refused('the go request changed since approval_text gave text_id %s (a plan, pass or file '
                          'changed): fetch it again, post the new text and ask again' % tid)
        # Ask first, claim after the yes (owner, 2026-10-24): a unit held while the person
        # decides is idle and blocks everyone queued for it. Held only for a run that is going
        # or a listening test, it may stay.
        idle = self.idle_units(s)
        if idle:
            raise Refused('release %s first: ask for the go before claiming, then claim after the yes '
                          '(your go lasts %d h) and check the unit\'s state before writing'
                          % (', '.join(idle), APPROVAL_TTL // 3600))
        job = Job(s, 'approval', ', '.join(sorted({i['label'].split(' on ')[-1] for i in items})),
                  'hardware', 'approval request')
        job.body = body

        def settle(ok, how):
            if job.state in ('approved', 'refused'):   # answered already, the other way (popup or thread)
                return ['%s: %s' % ('ok' if job.state == 'approved' else 'refused', job.lines[0])]
            if ok:
                token = 'ap-' + secrets.token_hex(6)
                with self.lock:
                    self.approvals[token] = dict(session=s.sid, items=items, created=time.time(),
                                                 expires=time.time() + APPROVAL_TTL, text=body)
                    self.save_approvals()
                job.state, job.lines = 'approved', ['token: %s (covers %s; expires %s)' % (
                    token, '; '.join(i['label'] for i in items), stamp(time.time() + APPROVAL_TTL))]
            else:
                job.state, job.lines = 'refused', ['%s said no in %s, or did not answer' % (PERSON, how)]
            job.ended = time.time()
            self.log_line({'session': s.sid, 'job': job.id, 'end': job.state, 'via': how,
                           'items': [i['label'] for i in items]})
            return ['%s: %s' % ('ok' if ok else 'refused', job.lines[0])]

        job.settle = settle
        with self.lock:
            self.jobs[job.id] = job
        if a.get('_elicit'):
            # The launcher says its client can ask the person itself (MCP elicitation): it
            # shows `body` in the client and comes back with the answer (Handler, method 'answer').
            job.state = 'asking'
            return {'elicit': {'request': job.id, 'message': body},
                    'lines': ['asking: %s, the client asks %s to approve %d item(s)' % (job.id, PERSON, len(items))]}
        return self.popup(job)

    def popup(self, job):
        """The fallback: a dialog on this PC's desktop."""
        job.state = 'running'

        def ask():
            with self.dialog:
                ok = ask_person(getattr(job, 'title', 'Hardware test run: approve?'), job.body)
            job.settle(ok, 'the popup on the PC')

        threading.Thread(target=ask, daemon=True).start()
        out = ['waiting: %s, a dialog on the PC asks %s to approve; status with run_id %s waits for '
               'the token' % (job.id, PERSON, job.id)]
        if getattr(job, 'thread_ok', False):         # approval_fallback both
            out.append('%s may answer in your thread instead: post this request there word for word, then '
                       'answer_in_thread with their reply; the first answer counts\n\n%s' % (PERSON, job.body))
        return out

    def in_thread(self, job):
        """approval_fallback thread: no popup; the session asks in the chat it reports to (a
        thread, a channel) and passes the person's reply on with answer_in_thread. No answer in DIALOG_TIMEOUT is a No."""
        job.state = 'running'

        def expire():
            time.sleep(DIALOG_TIMEOUT)
            job.settle(False, 'the thread')

        threading.Thread(target=expire, daemon=True).start()
        return ['waiting: %s, the client could not show the prompt: post this request in your thread word for '
                'word, then answer_in_thread with %s\'s reply (no answer in %d min is a No)\n\n%s'
                % (job.id, PERSON, DIALOG_TIMEOUT // 60, job.body)]

    def answer(self, s, req):
        """The person's answer to an approval asked in the client, relayed by that session's launcher."""
        job = self.jobs.get(req.get('request'))
        if not job or job.kind not in ('approval', 'heard') or job.sess is not s or job.state != 'asking':
            raise Refused('no approval of this session is waiting for an answer')
        if req.get('fallback'):
            # the client could not show the prompt (Remote Control): approval_fallback decides
            job.thread_ok = FALLBACK in ('thread', 'both')
            return self.in_thread(job) if FALLBACK == 'thread' else self.popup(job)
        return job.settle(bool(req.get('accept')), 'the client')

    def t_answer_in_thread(self, s, a):
        """The person's answer in the session's chat thread, relayed by the session, for an approval its
        client could not show. Weaker than the popup: the daemon cannot tell who wrote it, so the
        log keeps the message id for the person to check."""
        job = self.jobs.get(a.get('request'))
        if not job or job.kind not in ('approval', 'heard') or job.sess is not s or job.state != 'running' \
                or not getattr(job, 'thread_ok', False):
            raise Refused('no approval of this session is waiting for an answer in the thread')
        mid = reply_id(a.get('message_id'))
        if not mid:
            raise Refused('message_id must name %s\'s reply in the thread (its id or link)' % PERSON)
        return job.settle(a.get('approve') is True, 'the thread (message %s)' % mid)

    def leave_hardware(self, s, name, a):
        """Emulator work while holding a unit gives the unit back (owner, 2026-10-24): a session
        that has to change a test and go back to the emulator must not keep the hardware from
        everyone meanwhile. Kept only while one of its hardware runs is going or the person is
        listening. -> lines for the reply."""
        if not (a.get('target') == 'emulator' or (name == 'emulator' and a.get('action') == 'start')
                or (name == 'batch' and not a.get('stop'))):
            return []
        out = []
        for u in UNITS:
            h = elklock.holder(u)
            if not elklock.mine(h, session=s.sid):
                continue
            if h.get('listening'):
                out.append('kept your %s claim: %s is listening to it' % (u, PERSON))
                continue
            if any(j.sess is s and j.unit == u and j.target == 'hardware' for j in self.running()):
                out.append('kept your %s claim: a hardware run of yours is still going' % u)
                continue
            r = self.t_release(s, {'unit': u, 'accept_non_stock': True,
                                   'note': 'released by elkbench: the session went back to emulator testing'})
            out.append('%s your %s claim, as emulator work while holding a unit gives it back; claim it again '
                       'for the next hardware run%s' % ('released' if r[0].startswith('ok') else 'could not release',
                                                         u, '' if r[0].startswith('ok') else ': ' + r[0]))
        return out

    # -- listening tests --

    def t_listen(self, s, a):
        """The person listens to a unit now (a listening test outside a plan): the claim cannot
        time out and nothing may drive the unit until listen_done."""
        u = a.get('unit')
        self.unit(u)
        what = (a.get('what') or '').strip()
        if not what:
            raise Refused('say what %s listens for (what)' % PERSON)
        self.check_hardware(s, [u])
        if any(j.sess is s and j.unit == u and j.target == 'hardware' for j in self.running()):
            raise Refused('a hardware run of yours on %s is still going; a listen step in its plan does '
                          'this' % u)
        r = subprocess.run([sys.executable, ELKLOCK, 'listen', u, '--for', what], env=s.env(),
                           capture_output=True, text=True, timeout=30, **NOWIN)
        if r.returncode:
            raise Refused('elklock listen %s: %s' % (u, (r.stdout + r.stderr).strip()[-200:]))
        self.log_line({'session': s.sid, 'listen': what, 'unit': u})
        return ['listening: the %s is held for %s and cannot time out; nothing may drive it now' % (u, PERSON),
                'tell them in your thread exactly what to listen for, then wait for their reply, however '
                'long it takes; when they say they have finished, call listen_done with the id of that reply']

    def listening_on(self, s, u):
        """-> (what, the paused plan job or None) if the person is listening to `u` for `s`."""
        for j in self.running():
            if j.sess is s and j.unit == u and j.listening:
                return j.listening, j
        h = elklock.holder(u) or {}
        if elklock.mine(h, session=s.sid) and h.get('listening'):
            return h['listening'].get('what', ''), None
        return None, None

    def t_listen_done(self, s, a):
        """End a listening test, only on the person's word: the id of their reply in the thread
        saying they have finished, or, without one, the same prompt as an approval."""
        u = a.get('unit')
        self.unit(u)
        what, plan_job = self.listening_on(s, u)
        if what is None:
            raise Refused('no listening test of yours is going on on %s' % u)
        job = Job(s, 'heard', u, 'hardware', 'listening confirmation')
        job.title = 'Listening test: finished?'
        job.body = ('An AI session holds the %s for your listening test:\n\n  %s\n\nHave you finished '
                    'listening? Yes gives the unit back to the session; No keeps it waiting.' % (u, what))
        if plan_job and plan_job.power_cycle:
            job.title = 'Power cycle: done?'
            job.body = ('An AI session\'s test plan waits for you to %s.\n\nHave you done it, and is the '
                        'unit on its start screen? Yes lets the plan go on; No keeps it waiting.' % what)

        def settle(ok, how):
            if job.state in ('approved', 'refused'):
                return ['%s: %s' % ('ok' if job.state == 'approved' else 'refused', job.lines[0])]
            if ok:
                subprocess.run([sys.executable, ELKLOCK, 'heard', u, '--by', how], env=s.env(),
                               capture_output=True, timeout=30, **NOWIN)
                if plan_job:
                    plan_job.listening = None
                    try:
                        os.remove(plan_job.listen_file)           # the plan's listen step goes on
                    except FileNotFoundError:
                        pass
                job.state, job.lines = 'approved', ['%s has finished listening (%s); %s' % (
                    PERSON, how, 'the plan goes on: status with run_id %s' % plan_job.id if plan_job
                    else 'the unit is yours again')]
            else:
                job.state, job.lines = 'refused', ['%s has not finished listening (%s): keep waiting, '
                                                   'send nothing to the unit' % (PERSON, how)]
            job.ended = time.time()
            self.log_line({'session': s.sid, 'job': job.id, 'heard': job.state, 'via': how, 'unit': u})
            return ['%s: %s' % ('ok' if ok else 'refused', job.lines[0])]

        job.settle = settle
        with self.lock:
            self.jobs[job.id] = job
        mid = (a.get('message_id') or '').strip()
        if mid:
            if not reply_id(mid):
                raise Refused('message_id must name %s\'s reply in the thread (its id or link)' % PERSON)
            return settle(True, 'the thread (message %s)' % mid)
        if a.get('_elicit'):
            job.state = 'asking'
            return {'elicit': {'request': job.id, 'message': job.body},
                    'lines': ['asking: %s, the client asks %s whether they have finished listening' % (job.id, PERSON)]}
        return self.popup(job)

    def t_emulator(self, s, a, snapshot=None, power_off=False):
        """Start, stop or list the emulators: always headless (no window) and each session its
        own, its USB MIDI port on a TCP port of its own (analog-emu --usb-midi-tcp, digiemu's
        usbrun), so emulators need no loopMIDI and never queue for one another."""
        act = a.get('action') or 'status'
        if act == 'status':
            out = []
            for (sid, u), e in self.emulators.items():
                if e['proc'].poll() is None:
                    out.append('%s emulator of session %s on %s (%s)%s' % (
                        u, sid[:8], e['address'], e['build'], ' <- yours' if sid == s.sid else ''))
            ours = {e['proc'].pid for e in self.emulators.values()}
            for u in UNITS:
                for pid in set(self.emulator_procs(u)) - ours:
                    out.append('%s emulator pid %d started outside elkbench (loopMIDI ports)' % (u, pid))
            return ['ok: %d emulator(s) running' % len(out)] + out
        u = self.unit(a.get('unit'))
        if u.get('emulator') == 'fwcheck':
            raise Refused('there is no %s emulator with USB MIDI yet: flash target emulator (no build) '
                          'runs digiemu\'s emu.fwcheck as the gate' % a['unit'])
        if not u.get('emu_exe') and u.get('emulator') != 'digiemu':
            raise Refused('there is no %s emulator' % a['unit'])
        mine = self.emulators.get((s.sid, a['unit']))
        if act == 'stop':
            if not mine or mine['proc'].poll() is not None:
                raise Refused('this session has no %s emulator running' % a['unit'])
            self.stop_emulator(mine)
            return ['ok: stopped your %s emulator' % a['unit']]
        if act != 'start':
            raise Refused("action is 'start', 'stop' or 'status'")
        if mine and mine['proc'].poll() is None:
            raise Refused('this session already runs a %s emulator on %s; stop it first'
                          % (a['unit'], mine['address']))
        minutes = min(max(int(a.get('minutes') or 60), 1), 240)
        # power_off: started with --power-off, so that a later plan with power-cycle steps runs on
        # it as it is (after the plans that set up its state) instead of on a fresh boot
        power_off = power_off or bool(a.get('power_off'))
        if power_off and u.get('emulator') == 'digiemu':
            raise Refused('power_off is for the Rytm and A4 emulators (analog-emu --power-off)')
        if u.get('emulator') == 'digiemu':
            return self.digiemu_start(s, a['unit'], a.get('build'), minutes)
        exe =os.path.join(need(ANALOG_EMU, 'analog_emu', 'the analog-emu emulators'), u['emu_exe'])
        work =os.path.join(STATE, 'emulators', '%s-%s' % (a['unit'], s.sid[:8]))
        os.makedirs(work, exist_ok=True)
        flash = os.path.join(work, 'flash.img')
        flashed = os.path.join(work, 'flash.json')   # which OS file the flash holds
        keep = bool(a.get('keep_flash'))
        if keep:
            # analog-emu boots a FIRMWARE it is given, and the OS in --flash only when given none
            if a.get('build'):
                raise Refused('keep_flash boots what an upgrade left in the flash: leave build out')
            if not os.path.isfile(flash):
                raise Refused('this session\'s %s emulator has no flash to keep: start it with build'
                              % a['unit'])
            build = None
            label = read_json(flashed, {}).get('image') or 'the OS in its flash'
            sha = read_json(flashed, {}).get('sha')
        else:
            build = os.path.abspath(a.get('build') or '')
            if not os.path.isfile(build):
                raise Refused('build is the OS .syx to boot (or keep_flash to boot what was flashed into it)')
            if os.path.exists(flash):
                os.remove(flash)                      # a new flash boots the build given
            label = os.path.basename(build)
            sha = sha256(build)
            write_json(flashed, {'image': label, 'sha': sha})
        card = os.path.join(work, 'card.img')
        src = a.get('card') or u.get('emu_card')
        if src and (a.get('fresh_card', not keep) or not os.path.exists(card)):
            shutil.copyfile(src, card)               # a copy: the emulator writes to its card
        with socket.socket() as probe:               # a free local port for its USB MIDI
            probe.bind(('127.0.0.1', 0))
            tcp = probe.getsockname()[1]
        cmd = [exe] + ([build] if build else []) + [
            '--flash', flash, '--no-audio', '--headless', '--no-midi',
            '--usb-midi-tcp', '127.0.0.1:%d' % tcp, '--after', str(minutes * 60)]
        din = None
        if self.emu_has_din(a['unit']):              # and its DIN MIDI, for send/capture ... din
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                din = probe.getsockname()[1]
            cmd += ['--din-midi-tcp', '127.0.0.1:%d' % din]
        if os.path.exists(card):
            cmd[1:1] = ['--card', card]
        log_path = os.path.join(work, 'emulator.log')
        log = open(log_path, 'w', encoding='utf-8')
        if snapshot:                    # resume it if it exists; `save` on stdin writes it
            cmd += ['--snapshot', snapshot, '--stdin-commands']
        if power_off:                   # `quit` on stdin switches it off: the OS saves to the card
            cmd += ['--power-off'] + ([] if snapshot else ['--stdin-commands'])
        proc = subprocess.Popen(cmd, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if snapshot or power_off else None, **NOWIN)
        # what ran on this card before: nothing on a fresh copy; the old list on a card kept
        history = list((mine or {}).get('history') or []) if keep and not a.get('fresh_card') else []
        if keep:
            history.append(dict(what='a restart from its flash', taints=False))
        self.emulators[(s.sid, a['unit'])] = dict(proc=proc, session=s.sid, build=label, sha=sha, history=history,
                                                 address='tcp:127.0.0.1:%d' % tcp, flashed=flashed,
                                                 snapshot=snapshot, log=log_path,
                                                 din='tcp:127.0.0.1:%d' % din if din else None,
                                                 cmd=cmd, work=work, power_off=power_off, boots=1)
        for _ in range(30):                           # until it listens (an older build never does)
            time.sleep(0.2)
            with open(log_path, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            if ('usb midi tcp: listening' in text and (not din or 'din midi tcp: listening' in text)
                    or proc.poll() is not None):
                break
        if proc.poll() is not None or 'usb midi tcp: listening' not in text:
            proc.terminate()
            raise Refused('the emulator did not open its USB MIDI port (needs an analog-emu with '
                          '--usb-midi-tcp): see %s' % log_path)
        return ['ok: %s emulator running headless on %s%s, USB MIDI at tcp:127.0.0.1:%d%s (%s), ends by itself '
                'after %d min' % (a['unit'], label, ' (from its flash)' if keep else '', tcp,
                                  ', DIN MIDI at tcp:127.0.0.1:%d' % din if din else '',
                                  ('its card as left' if keep and not a.get('fresh_card') else 'card copy')
                                  if os.path.exists(card) else 'blank card', minutes)] + (
                [] if keep else ['its first boot shows two prompts (missing calibration): answer NO twice, '
                                 'as the plans do'])

    def stop_emulator(self, e):
        if e.get('wsl_tag'):              # stop it inside WSL first, so it cleans up its work folder
            subprocess.run(['wsl', '-e', 'pkill', '-f', 'digiemu_usb.py .* %s( |$)' % e['wsl_tag']],
                           capture_output=True, timeout=20, **NOWIN)
            try:
                e['proc'].wait(20)
            except subprocess.TimeoutExpired:
                pass
        e['proc'].terminate()
        try:
            e['proc'].wait(10)
        except subprocess.TimeoutExpired:
            e['proc'].kill()

    def digiemu_cmd(self, args):
        """digiemu's Python running `args` in the digiemu checkout -> (command, path converter).
        digiemu_python = ['wsl', PY] runs it in WSL, with Windows paths as /mnt/<drive>/..."""
        repo = need(DIGIEMU, 'digiemu', 'the Digitone/Digitakt emulator (digiemu)')
        wsl = DIGIEMU_PY[:1] == ['wsl']
        conv = (lambda p: '/mnt/%s%s' % (p[0].lower(), p[2:].replace('\\', '/'))) if wsl else (lambda p: p)
        args = [conv(x) if isinstance(x, Path) else x for x in args]
        if wsl:
            return ['wsl', '-e', 'bash', '-lc', 'cd %s && %s %s' % (
                shlex.quote(conv(repo)), ' '.join(DIGIEMU_PY[1:]), ' '.join(shlex.quote(x) for x in args))]
        return DIGIEMU_PY + args

    def digiemu_start(self, s, unit, build, minutes):
        """The Digitone or Digitakt emulator: digiemu boots `build` headless (digiemu_usb.py: set
        up once, then a work folder per session), its USB MIDI link on a TCP port of its own."""
        need(DIGIEMU, 'digiemu', 'the Digitone/Digitakt emulator (digiemu)')
        build = os.path.abspath(build or '')
        if not os.path.isfile(build) or not build.lower().endswith('.syx'):
            raise Refused('build is the OS .syx to boot')
        tag = '%s-%s' % (unit, s.sid[:8])
        work = os.path.join(STATE, 'emulators', tag)
        os.makedirs(work, exist_ok=True)
        # OS files flashed into it: its reboot boots one. In the evidence folder, not under STATE:
        # the Claude app's LocalAppData is virtualized, so WSL would not see them there
        firmware = os.path.join(s.folder(unit), 'firmware')
        e, text = self.digiemu_launch(build, minutes, tag, work, firmware,
                                      lambda e: self.emulators.__setitem__((s.sid, unit), dict(e, session=s.sid)))
        e = self.emulators[(s.sid, unit)]
        fresh = 'already set up' not in text
        return ['ok: %s emulator running headless on %s, USB MIDI at %s%s (%s), ends by '
                'itself after %d min' % (unit, os.path.basename(build), e['address'],
                                         ', DIN MIDI at %s' % e['din'] if e.get('din') else '',
                                         'first boot of this build' if fresh else 'its settled boot', minutes)]

    def digiemu_has(self, option):
        """Whether this digiemu's tools/usbrun.py takes `option` (--ob-audio-port, --din-port)."""
        try:
            with open(os.path.join(DIGIEMU, 'tools', 'usbrun.py'), encoding='utf-8', errors='replace') as fh:
                return option in fh.read()
        except OSError:
            return False

    def digiemu_has_ob_audio(self):
        """Whether this digiemu's usbrun serves Overbridge audio (--ob-audio-port)."""
        return self.digiemu_has('--ob-audio-port')

    def digiemu_launch(self, build, minutes, tag, work, firmware, started=None):
        """digiemu_usb.py booting `build` (work folder `tag` in WSL), its link on a free port,
        until its OS answers -> (emulator dict, its log text). `firmware` (WSL must see it) gets
        the OS files flashed into it; started(e) runs as soon as the process is up. Its DIN MIDI
        gets a port of its own (e['din']) when this digiemu serves one. Refused, with the emulator
        stopped, when it does not come up."""
        with socket.socket() as probe:               # a free local port for its USB MIDI link
            probe.bind(('127.0.0.1', 0))
            tcp = probe.getsockname()[1]
        ob = None
        if self.digiemu_has_ob_audio():
            with socket.socket() as probe:           # and one for its Overbridge audio
                probe.bind(('127.0.0.1', 0))
                ob = probe.getsockname()[1]
        din = None
        if self.digiemu_has('--din-port'):
            with socket.socket() as probe:           # and one for its DIN MIDI
                probe.bind(('127.0.0.1', 0))
                din = probe.getsockname()[1]
        shutil.rmtree(firmware, ignore_errors=True)
        os.makedirs(firmware)
        cmd = self.digiemu_cmd([Path(os.path.join(HERE, 'digiemu_usb.py')), Path(DIGIEMU), Path(build),
                                str(tcp), str(minutes), tag, '--firmware-dir', Path(firmware)]
                               + (['--ob-audio-port', str(ob)] if ob else [])
                               + (['--din-port', str(din)] if din else []))
        log_path = os.path.join(work, 'emulator.log')
        log = open(log_path, 'w', encoding='utf-8')
        proc = subprocess.Popen(cmd, cwd=work, stdout=log, stderr=subprocess.STDOUT, **NOWIN)
        e = dict(proc=proc, build=os.path.basename(build), sha=sha256(build),
                 address='tcp:127.0.0.1:%d' % tcp, din='tcp:127.0.0.1:%d' % din if din else None,
                 wsl_tag=tag if cmd[0] == 'wsl' else None, firmware=firmware, ob_audio=ob)
        if started:
            started(e)
        text, end = '', time.time() + 300            # a build seen for the first time boots first
        while proc.poll() is None and time.time() < end and '[usbrun] USB MIDI link on' not in text:
            time.sleep(0.5)
            with open(log_path, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
        if '[usbrun] USB MIDI link on' in text:
            for port in [tcp] + ([din] if din else []):
                for _ in range(20):                  # WSL forwards the port to Windows a moment later
                    try:
                        socket.create_connection(('127.0.0.1', port), timeout=1).close()
                        break
                    except OSError:
                        time.sleep(0.5)
                else:
                    text = ''
        if text and DIGIHEALTH and os.path.exists(DIGIHEALTH):
            script = os.path.join(DIGIHEALTH, 'tools', 'digirun.py')
            for _ in range(12):                      # the OS answers its RPC a moment after the link
                try:
                    r = subprocess.run([sys.executable, script, '--port', e['address'], 'info'],
                                       cwd=os.path.dirname(script), capture_output=True, text=True, timeout=5,
                                       **NOWIN)
                    if r.returncode == 0:
                        break
                except subprocess.TimeoutExpired:   # a first connection through WSL's forwarding can stall
                    pass
                time.sleep(1)
        if proc.poll() is not None or '[usbrun] USB MIDI link on' not in text:
            self.stop_emulator(e)
            last = [l for l in text.splitlines() if l.strip()][-1:] or ['no output']
            raise Refused('the digiemu emulator did not start (%s): see %s' % (last[0].strip(), log_path))
        return e, text

    def setup_snapshot(self, s, info, a):
        """-> dict(key, store, live, have) when this emulator run can use a setup snapshot: a
        hardware plan with snapshot-after, booted from `build`, no flash in the plan, not
        recording references. `live` is the emulator's own copy (a stored one copied in to
        resume, or where `save` writes a new one)."""
        if not (info['setup'] and a.get('build') and not info['emulator'] and not info['flash']
                and not a.get('record_refs') and UNITS[info['unit']].get('emu_exe')
                and UNITS[info['unit']].get('emulator') != 'digiemu'):
            return None
        if not self.emu_has_snapshots(info['unit']):
            return None
        key = self.snapshot_key(info, os.path.abspath(a['build']))
        store = os.path.join(STATE, 'snapshots', key + '.snap')
        work = os.path.join(STATE, 'emulators', '%s-%s' % (info['unit'], s.sid[:8]))
        os.makedirs(work, exist_ok=True)
        live = os.path.join(work, 'setup.snap')
        if os.path.exists(live):
            os.remove(live)
        have = os.path.isfile(store) and os.path.isfile(store + '.json')
        if have:
            shutil.copyfile(store, live)
            os.utime(store + '.json')                  # last use, for prune_snapshots
        self.prune_snapshots(keep=store)
        return dict(key=key, store=store, live=live, have=have)

    def prune_snapshots(self, keep=None):
        """Setup snapshots go when unused for SNAPSHOT_DAYS, and beyond SNAPSHOT_MAX the least
        recently used go first (about 13 MB each). A snapshot's .json is touched on every use."""
        folder = os.path.join(STATE, 'snapshots')
        if not os.path.isdir(folder):
            return
        snaps = []
        for f in os.listdir(folder):
            p = os.path.join(folder, f)
            if f.endswith('.snap') and p != keep:
                meta = p + '.json'
                snaps.append((os.path.getmtime(meta if os.path.exists(meta) else p), p))
        snaps.sort(reverse=True)                       # most recently used first
        cutoff = time.time() - SNAPSHOT_DAYS * 86400
        room = SNAPSHOT_MAX - (1 if keep else 0)
        for n, (used, p) in enumerate(snaps):
            if used < cutoff or n >= room:
                for q in (p, p + '.json'):
                    try:
                        os.remove(q)
                    except OSError:
                        pass

    def emu_help(self, unit):
        """This unit's analog-emu --help text ('' without one), read once per binary."""
        exe = os.path.join(ANALOG_EMU or '', UNITS[unit].get('emu_exe') or '')
        if not os.path.isfile(exe):
            return ''
        stamp = (exe, os.path.getmtime(exe))
        if stamp not in self.emu_help_text:
            try:
                r = subprocess.run([exe, '--help'], capture_output=True, text=True, timeout=20, **NOWIN)
                self.emu_help_text[stamp] = r.stdout + r.stderr
            except (OSError, subprocess.TimeoutExpired):
                self.emu_help_text[stamp] = ''
        return self.emu_help_text[stamp]

    def emu_has_snapshots(self, unit):
        """Whether this analog-emu build takes --stdin-commands (headless snapshot saves)."""
        return '--stdin-commands' in self.emu_help(unit)

    def emu_has_power_off(self, unit):
        """Whether this analog-emu build has --power-off. Its --help does not list it, so this
        looks in the binary for a line only its power-off prints."""
        exe = os.path.join(ANALOG_EMU or '', UNITS[unit].get('emu_exe') or '')
        if not os.path.isfile(exe):
            return False
        stamp = ('power-off', exe, os.path.getmtime(exe))
        if stamp not in self.emu_help_text:
            try:
                with open(exe, 'rb') as fh:
                    self.emu_help_text[stamp] = POWER_OFF_BUILD_MARK in fh.read()
            except OSError:
                self.emu_help_text[stamp] = False
        return self.emu_help_text[stamp]

    def emu_has_din(self, unit):
        """Whether this unit's emulator serves its DIN MIDI over TCP: analog-emu's --din-midi-tcp,
        digiemu's usbrun.py --din-port."""
        if UNITS[unit].get('emulator') == 'digiemu':
            return self.digiemu_has('--din-port')
        return '--din-midi-tcp' in self.emu_help(unit)

    def din_args(self, s, unit, tgt, lines):
        """['--din', PORT] for analogrun or digirun when `lines` use the unit's DIN MIDI
        (send/capture ... din), else []: on hardware the interface named in config
        (units.<unit>.din), which must be connected; in the emulator, this session's emulator's
        DIN MIDI over TCP."""
        if not din_steps(lines):
            return []
        if din_refusal(unit):
            raise Refused(din_refusal(unit))
        if tgt == 'hardware':
            name = UNITS[unit].get('din')
            if not name:
                raise Refused('these steps use DIN MIDI, and no interface is set for the %s: din = the MIDI '
                              'interface cabled to its DIN ports, in [units.%s] of %s' % (unit, unit, CONFIG))
            try:
                ports = elklock.midi_inputs()
            except OSError:
                ports = None
            want = name.split(',')[0].strip().lower()
            if ports is not None and not any(want in p.lower() for p in ports):
                raise Refused('the %s\'s DIN interface %r is not connected (no such MIDI port)' % (unit, name))
            return ['--din', name]
        e = self.emulators.get((s.sid, unit)) or {}
        if e.get('din'):
            return ['--din', e['din']]
        raise Refused(self.no_emu_din(unit) if not self.emu_has_din(unit) else
                      'this session\'s %s emulator started without its DIN MIDI port: start it again '
                      '(emulator stop, then start or give build)' % unit)

    def no_emu_din(self, unit):
        if UNITS[unit].get('emulator') == 'digiemu':
            return ('these steps use DIN MIDI, and this digiemu\'s tools/usbrun.py has no --din-port: DIN '
                    'steps need a digiemu that serves its DIN MIDI over TCP (tools.digiemu)')
        return ('these steps use DIN MIDI, and this analog-emu %s has no --din-midi-tcp: DIN steps need '
                'an analog-emu that serves its DIN MIDI over TCP (tools.analog_emu)' % UNITS[unit]['emu_exe'])

    def saved_projects(self, addr, folder):
        """{slot: (name, sha256)} of every saved project on the emulator at `addr`, read with
        analogrun data ls / get (read only)."""
        run = lambda *a: subprocess.run([sys.executable, RUNNER, '--port', addr, 'data'] + list(a),
                                        cwd=ANALOGHEALTH, capture_output=True, text=True, encoding='utf-8',
                                        errors='replace', timeout=120, **NOWIN)
        r = run('ls', '/projects')
        if r.returncode:
            raise RuntimeError('data ls /projects: %s' % (r.stdout + r.stderr).strip()[-200:])
        os.makedirs(folder, exist_ok=True)
        out = {}
        for m in re.finditer(r'^(.*?)\s+id\s+(\d+)\s+\d+ B(.*)$', r.stdout, re.M):
            if 'no valid data' in m.group(3):
                continue
            n, f = int(m.group(2)), os.path.join(folder, 'project-%d.bin' % int(m.group(2)))
            g = run('get', '/projects/%d' % n, f)
            if g.returncode:
                raise RuntimeError('data get /projects/%d: %s' % (n, (g.stdout + g.stderr).strip()[-200:]))
            out[n] = (m.group(1).strip(), sha256(f))
        return out

    def guard_projects(self, info, addr, cmd, folder):
        """`cmd` (one command or stages) between a read of the emulator's saved projects and a
        second read: a project outside the slots the plan saves into that was added, removed,
        renamed or changed fails the run (a write the plan did not ask for, such as one into
        the project that was active). `addr` may be a function giving it (a power cycle moves
        the emulator to new ports)."""
        where = addr if callable(addr) else (lambda: addr)
        stages = list(cmd) if cmd and (isinstance(cmd[0], (list, tuple)) or callable(cmd[0])) else [cmd]
        seen = {}
        allowed = saved_slots(info['steps'])

        def before(rc, out):
            try:
                seen['before'] = self.saved_projects(where(), os.path.join(folder, 'before'))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return 1, 'fail: could not read the saved projects before the run: %s\n' % e
            return 0, ''

        def after(rc, out):
            try:
                now = self.saved_projects(where(), os.path.join(folder, 'after'))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return 1, 'fail: could not read the saved projects after the run: %s\n' % e
            bad = project_changes(seen['before'], now, allowed)
            if bad:
                return 1, ''.join('fail: write outside the plan\'s slots: %s (copies in %s)\n' % (b, folder)
                                  for b in bad)
            return 0, 'saved projects outside slot(s) %s unchanged (%d checked)\n' % (
                ', '.join(map(str, sorted(allowed))) or '-', len(set(now) - allowed))
        before.__name__ = 'read the saved projects'
        after.__name__ = 'check no other saved project changed'
        return [before] + stages + [after]

    def snapshot_stages(self, s, info, snap, addr, started, din=(), job=None):
        """The runner stages of a snapshot run. Resuming: the steps after setup only. Fresh: the
        setup steps; when they pass, the emulator saves the snapshot (kept for later runs with
        the run that proved it); then the rest on the same emulator. A setup failure ends the
        run with the setup's own report, and no snapshot is kept."""
        cut = info['setup']
        run = lambda plan: [sys.executable, RUNNER, '--port', addr] + list(din) + ['--quiet', 'run', plan]
        if info['power_cycles']:               # the rest in parts, a power cycle between them
            rest = self.plan_stages(s, info, job, 'emulator', info['steps'][cut:], tag=' after setup')
        else:
            rest = [run(self.twin(info, False, s.sid, sections=info['steps'][cut:], tag=' after setup'))]
        if snap['have']:
            meta = read_json(snap['store'] + '.json', {})
            started.append('resumed a setup snapshot: %s passed in %s' % (
                info['steps'][cut - 1][0], meta.get('run_dir')))
            return rest
        setup = run(self.twin(info, False, s.sid, sections=info['steps'][:cut], tag=' setup'))
        e = self.emulators[(s.sid, info['unit'])]

        def setup_then_save(rc, out):
            p = subprocess.run(setup, cwd=ANALOGHEALTH, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', **NOWIN)
            if p.returncode:
                return p.returncode, p.stdout
            m = re.search(r': PASS \(.* - (.+)$', p.stdout, re.M)
            e['proc'].stdin.write(b'save\n')
            e['proc'].stdin.flush()
            for _ in range(150):
                time.sleep(0.2)
                with open(e['log'], encoding='utf-8', errors='replace') as fh:
                    if 'saved ' + snap['live'] in fh.read():
                        break
            else:
                return 1, p.stdout + 'fail: the emulator did not save the setup snapshot (%s)\n' % e['log']
            os.makedirs(os.path.dirname(snap['store']), exist_ok=True)
            shutil.copyfile(snap['live'], snap['store'] + '.tmp')
            os.replace(snap['store'] + '.tmp', snap['store'])
            write_json(snap['store'] + '.json', {'plan': info['path'], 'setup_until': info['steps'][cut - 1][0],
                                                 'run_dir': m.group(1).strip() if m else None,
                                                 'time': time.time()})
            return 0, ''
        setup_then_save.__name__ = 'setup, then save its snapshot'
        started.append('setup runs first; a pass is kept as a snapshot for later runs')
        return [setup_then_save] + rest

    def plan_stages(self, s, info, job, tgt, steps, record_refs=False, tag=''):
        """The stages that run `steps` (a plan's sections) in parts, a power cycle between
        each two: each part is a twin of its sections run on its own; in the emulator, the
        power cycle switches analog-emu off with its power-off save and cold-boots it (each part
        then runs on the emulator's ports of that moment); on the unit, the run waits until the
        person has power cycled it by hand and said so (listen_done), as at a listen step."""
        unit, stages = info['unit'], []
        parts = power_cycle_parts(steps)
        for i, part in enumerate(parts):
            if i:
                stages.append(self.power_cycle_stage(s, unit, job, tgt, i, len(parts) - 1))
            if not part:
                continue
            plan = self.twin(info, record_refs and tgt == 'emulator', s.sid, sections=part,
                             tag='%s part %d of %d' % (tag, i + 1, len(parts)), hardware=tgt == 'hardware')
            lines = [l for _, ls in part for l in ls]
            if tgt == 'hardware':
                stages.append([sys.executable, ELKLOCK, 'run', unit, '--', sys.executable, RUNNER] +
                              self.din_args(s, unit, tgt, lines) + ['--quiet', 'run', plan])
            else:
                stages.append(self.emulator_part(s, unit, plan, lines, i + 1))
        return stages

    def emulator_part(self, s, unit, plan, lines, n):
        """A stage running one part of a plan on this session's emulator where it is now."""
        def part(rc, out):
            cmd = [sys.executable, RUNNER, '--port', self.emu_port(s, unit)] + \
                self.din_args(s, unit, 'emulator', lines) + ['--quiet', 'run', plan]
            env = dict(s.env(), **({'DIGIRUN_PATH': os.path.join(DIGIHEALTH, 'tools')} if DIGIHEALTH else {}))
            p = subprocess.run(cmd, cwd=ANALOGHEALTH, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, encoding='utf-8', errors='replace', **NOWIN)
            return p.returncode, p.stdout
        part.__name__ = 'part %d of the plan' % n
        return part

    def power_cycle_stage(self, s, unit, job, tgt, n, of):
        """A stage for the plan's power cycle n (of `of`)."""
        if tgt == 'emulator':
            def cycle(rc, out):
                return self.power_cycle(s, unit)
        else:
            def cycle(rc, out):
                what = POWER_CYCLE_HAND % (unit, ' (%d of %d)' % (n, of) if of > 1 else '')
                write_json(job.listen_file, {'what': what, 'since': time.time(), 'power_cycle': True})
                while os.path.exists(job.listen_file):
                    time.sleep(0.5)
                return 0, 'ok power-cycle by hand: %s said it is done\n' % PERSON
        cycle.__name__ = 'power cycle %d%s' % (n, ' (emulator: power-off save, cold boot)' if tgt == 'emulator'
                                               else ' (by hand, waits for the person)')
        return cycle

    def t_run_plan(self, s, a):
        tgt = self.target(a.get('target'))
        info = self.plan_info(a.get('plan', ''))
        unit = info['unit']
        job = Job(s, 'plan', unit, tgt, 'plan ' + info['name'])
        cycles = info['power_cycles']
        if tgt == 'hardware':
            if info['emulator']:
                raise Refused('this is an emulator plan; give the hardware plan')
            if a.get('record_refs'):
                raise Refused('references are recorded in the emulator only')
            self.check_hardware(s, [unit])
            ep = self.plan_pass(info)
            if not ep:
                raise Refused('no passing emulator run of these steps, refs and OS file: run_plan '
                              'target emulator first')
            left = s.os_left.get(unit)
            if info['os_sha'] and left and (len(left) < 3 or left[2] != info['os_sha']):
                raise Refused('this plan runs on %s (os =), and this session left %s on %s: stop and check '
                              'the run\'s order' % (os.path.basename(info['os_file']), left[0], unit))
            self.check_connected(unit, tgt)
            din = self.din_args(s, unit, tgt, [l for _, ls in info['steps'] for l in ls])
            item = ('plan', info['key'])
            self.use_approval(s, a.get('approval'), item, 'plan ' + info['name'])
            try:
                port = self.hold_din(din, self.busy_port(unit, tgt, 'plan %s' % info['name']))
            except Refused:
                self.give_back(a.get('approval'), item)
                raise
            if info['flash']:                   # its flash log is testplan's: the quiet time goes here
                self.log_line({'session': s.sid, 'job': job.id,
                               'flash_starts': flash_start_line(job, self.port_lock(unit, tgt))})
            # A plan that flashes needs `emulator-run` for testplan's gate: the recorded pass.
            plan = self.twin(info, False, s.sid, emulator_run=ep['run_dir']) \
                if info['flash'] and ep.get('run_dir') else info['path']
            cmd = self.plan_stages(s, info, job, tgt, info['steps']) if cycles else [
                sys.executable, ELKLOCK, 'run', unit, '--', sys.executable, RUNNER] + din + ['--quiet', 'run', plan]
            started = []
        else:
            if cycles:
                need(ANALOG_EMU, 'analog_emu', 'the analog-emu emulators')
                if not self.emu_has_power_off(unit):
                    raise Refused('this plan has power-cycle steps, and this analog-emu %s has no --power-off '
                                  '(tools.analog_emu needs a build with the power-off save)' % UNITS[unit]['emu_exe'])
            if info['din'] and not self.emu_has_din(unit):
                raise Refused(self.no_emu_din(unit))
            snap = self.setup_snapshot(s, info, a)
            addr, started = self.emu_ready(s, unit, a.get('build'), snap and snap['live'], bool(cycles))
            booted = (self.emulators.get((s.sid, unit)) or {}).get('sha')
            if info['os_sha'] and booted != info['os_sha']:
                raise Refused('this plan runs on %s (os =), and the emulator runs %s: start it on that file '
                              '(build)' % (os.path.basename(info['os_file']),
                                           (self.emulators.get((s.sid, unit)) or {}).get('build') or 'an unknown OS'))
            din = self.din_args(s, unit, tgt, [l for _, ls in info['steps'] for l in ls])
            port = self.busy_port('%s@%s' % (unit, addr), tgt, 'plan %s' % info['name'])
            emu = self.emulators.get((s.sid, unit)) or {}
            if info['flash'] and emu.get('firmware'):  # digiemu reboots from the file the flash was written from
                shutil.copyfile(info['flash'], os.path.join(emu['firmware'], os.path.basename(info['flash'])))
            began = 'a fresh emulator' if a.get('build') else 'the session\'s running emulator, as earlier calls left it'
            before = list((self.emulators.get((s.sid, unit)) or {}).get('history') or [])
            if snap:
                began = 'a setup snapshot' if snap['have'] else began
                cmd = self.snapshot_stages(s, info, snap, addr, started, din, job)
            elif cycles:
                cmd = self.plan_stages(s, info, job, tgt, info['steps'], bool(a.get('record_refs')))
            else:
                plan = info['path'] if info['emulator'] else self.twin(info, bool(a.get('record_refs')), s.sid)
                cmd = [sys.executable, RUNNER, '--port', addr] + din + ['--quiet', 'run', plan]
            if UNITS[unit]['runner'] is True:          # analogrun's data store: Rytm and A4
                cmd = self.guard_projects(info, (lambda: self.emu_port(s, unit)) if cycles else addr, cmd, os.path.join(
                    s.folder(unit), 'projects-%s' % time.strftime('%H%M%S')))
        record = not info['emulator'] or info['settings'].get('record-refs', 'no') != 'yes'
        record = record and not a.get('record_refs')
        tainted = [h['what'] for h in before if h['taints']] if tgt == 'emulator' else []

        def done(job, rc, out):
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            # one verdict line per part a plan with power-cycle runs in, else one
            ms = [re.search(r' - (.+)$', l) for l in lines if re.search(r': (PASS|FAIL) \(', l)]
            run_dirs = [m.group(1).strip() for m in ms if m]
            run_dir = run_dirs[-1] if run_dirs else None
            summs = [read_json(os.path.join(d, 'summary.json'), {}) for d in run_dirs]
            summ = summs[-1] if summs else {}
            parts = sum(1 for p in power_cycle_parts(info['steps']) if p) if cycles else 1
            ok = rc == 0 and len(summs) >= parts and all(x.get('pass') for x in summs)
            job.state = 'pass' if ok else 'fail'
            kept = [l for l in lines if not l.startswith(('claimed', 'already yours', 'last handoff',
                                                          'released', 'not yours', '  os:', 'os:'))]
            # Verdict line, then only what did not pass: a cap on all lines once cut off the
            # FAIL line of a long plan. Passing steps become a count; the first failure's
            # screen-diff picture (+ lit only now, - lit only in the reference) stays, up to
            # 12 rows, as reading it costs less than opening the diff PNG.
            for d in run_dirs:                                    # the evidence line names them
                kept = [l[:-len(' - ' + d)] if l.endswith(' - ' + d) and re.search(r': (PASS|FAIL) \(', l)
                        else l for l in kept]
            oks = sum(l.startswith('ok ') for l in kept[1:])
            art = lambda l: re.fullmatch(r'[-+#.]+( \(saved .*)?', l)
            rest = [l for l in kept[1:] if not l.startswith('ok ') and not art(l)]
            pic = [l for l in kept[1:] if art(l)]
            pic = pic[:12] + (['(%d more rows)' % (len(pic) - 12)] if len(pic) > 12 else [])
            job.lines = kept[:1] + (['%d step(s) ok' % oks] if oks else []) + rest[:12] + pic
            job.lines.append('evidence: %s' % (', '.join(run_dirs) or job.log))
            for d in run_dirs:
                job.lines.append('log: %s' % os.path.join(d, 'log.txt'))
                shots = run_files(d)
                if shots:
                    job.lines.append('shots: ' + ', '.join(shots))
            if cycles:
                job.lines += [('power cycled here (power-off save, cold boot on its card): ' if tgt == 'emulator'
                               else 'power cycled by hand: ') + c for c in cycles]
            if tgt == 'emulator':
                job.lines += ['not tested here (skipped in the emulator): ' + h for h in hand_steps(info['steps'])]
                job.lines += ['short here (%g s in the emulator, in full on the unit): %s' % (AUDIO_WAIT_EMULATOR, w)
                              for w in info['audio_waits']]
            verdict = '%s %s (%s)' % (info['name'], 'PASS' if ok else 'FAIL', run_dir or job.log)
            if tgt == 'emulator' and any(x.get('flash_sha256') for x in summs):
                e = self.emulators.get((s.sid, unit)) or {}      # it runs the plan's flash now
                if e.get('flashed'):
                    write_json(e['flashed'], {'image': os.path.basename(info['flash']), 'sha': info['flash_sha']})
                    e['build'] = os.path.basename(info['flash'])
                if e:
                    e['sha'] = info['flash_sha']
            if tgt == 'hardware':
                s.done.setdefault(unit, []).append('plan ' + verdict)
                if summ.get('flash_sha256'):
                    s.os_left[unit] = (os.path.basename(info['flash']),
                                       self.is_stock(unit, summ['flash_sha256']), summ['flash_sha256'])
            else:
                self.note_emu(s, unit, ('emulator plan ' if info['emulator'] else 'plan ') + info['name'],
                              info['emulator'] and info['writes'])
            if tgt == 'hardware':
                pass
            elif ok and record and info.get('home'):
                job.lines.append('not recorded as the emulator pass: the plan starts from built-in start %s '
                                 '(start = %s), which a gate batch builds; run it with batch gate true'
                                 % (info['home'], info['home']))
            elif ok and record and tainted:
                # proposal B.3: a pass may not stand on state an ungated step wrote
                job.lines.append('not recorded as the emulator pass: it ran after %s, which wrote state outside '
                                 'any gated plan; boot a fresh emulator (build) or use a gate batch'
                                 % ', '.join(tainted))
            elif ok and record:
                self.record_pass(info['key'], dict({'plan': info['path'], 'run_dir': run_dir, 'unit': unit,
                                                    'start': began, 'os': booted,
                                                    'after': [h['what'] for h in before]},
                                                   **({'run_dirs': run_dirs, 'power_cycles': len(cycles)}
                                                      if cycles else {})))
                if info['flash_sha'] and booted:
                    self.record_pass(flash_key(unit, info['flash_sha'], booted),
                                     {'image': info['flash'], 'unit': unit, 'run_dir': run_dir, 'over': booted})
                job.lines.append('recorded as the emulator pass for this plan')
            elif ok:
                job.lines.append('references recorded; run again without record_refs for the pass')

        return started + self.start(job, cmd, ANALOGHEALTH, done, port)

    def t_flash(self, s, a):
        tgt = self.target(a.get('target'))
        u = self.unit(a.get('unit'))
        img = os.path.abspath(a.get('image', ''))
        if not os.path.isfile(img) or not img.lower().endswith('.syx'):
            raise Refused('no OS .syx file %s' % img)
        d = sha256(img)
        over = None
        folder = s.folder(a['unit'])
        stamp = time.strftime('%H%M%S')
        # --verbose: every request and reply goes to the log too (--quiet sends all output there)
        tail = ['--yes', '--no-confirm', '--wait', '--quiet', '--verbose', '--log', os.path.join(folder, 'elkflash.log'),
                '--screenshot', os.path.join(folder, 'after-flash-%s.png' % stamp)]
        base = [sys.executable, '-m', 'elkflash', 'upgrade', img] + chunk_args(a['unit'])
        if tgt == 'hardware':
            self.check_hardware(s, [a['unit']])
            if not self.flash_pass(a['unit'], d, self.os_on_unit(s, a['unit'], a.get('over'))):
                raise Refused('this file has not flashed over the OS on the unit and booted in the %s emulator: '
                              'flash target emulator first, on an emulator booted on that OS' % a['unit'])
            self.check_connected(a['unit'], tgt)
            item = ('flash', a['unit'], d)
            self.use_approval(s, a.get('approval'), item, 'flash %s' % os.path.basename(img))
            try:
                port = self.busy_port(a['unit'], tgt, 'flash %s' % os.path.basename(img))
            except Refused:
                self.give_back(a.get('approval'), item)
                raise
            cmd = [sys.executable, ELKLOCK, 'run', a['unit'], '--'] + base + ['--port', u['port']] + tail
        elif (u.get('emulator') in ('digiemu', 'fwcheck') and not a.get('build')
              and not self.emu_running(s, a['unit'])):
            return self.digiemu_check(s, a['unit'], img, d, folder)
        else:
            addr, started = self.emu_ready(s, a['unit'], a.get('build'))
            port = self.busy_port('%s@%s' % (a['unit'], addr), tgt, 'flash %s' % os.path.basename(img))
            emu = self.emulators[(s.sid, a['unit'])]
            over = emu.get('sha')                   # what this flash goes over: its pass is of that transition
            if emu.get('firmware'):                 # digiemu reboots from the file the flash was written from
                shutil.copyfile(img, os.path.join(emu['firmware'], os.path.basename(img)))
                tail = tail + ['--wait-timeout', '400']       # a reboot is a cold boot and a settle
            if addr.startswith('tcp:'):
                cmd = base + ['--port', addr] + tail
            else:
                i, o = addr.split(',')
                cmd = base + ['--in-port', i, '--out-port', o] + tail
        job = Job(s, 'flash', a['unit'], tgt, 'flash ' + os.path.basename(img))
        flog = os.path.join(folder, 'elkflash.log')
        quiet = flash_start_line(job, port)
        self.log_line({'session': s.sid, 'job': job.id, 'flash_starts': quiet})
        os.makedirs(folder, exist_ok=True)
        with open(flog, 'a', encoding='utf-8') as fh:
            fh.write(quiet + '\n')
        at = os.path.getsize(flog)                                      # elkflash appends to it

        def done(job, rc, out):
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            job.state = 'pass' if rc == 0 else 'fail'
            # --quiet prints only the last line; the lines that say what happened are in the log
            try:
                with open(flog, encoding='utf-8', errors='replace') as fh:
                    fh.seek(at)
                    said = [l.strip() for l in fh.read().replace('\r', '\n').splitlines()
                            if re.match(r'\s*(sent \d|Committed|back after|note:|Press YES|SUCCESS|no reply to PING)', l)]
            except OSError:
                said = []
            job.lines = [quiet] + [l for l in said if not any(l in x for x in lines[-3:])] + lines[-3:] + \
                ['log: %s' % flog]
            if tgt == 'hardware':
                if rc == 0:
                    s.os_left[a['unit']] = (os.path.basename(img), self.is_stock(a['unit'], d), d)
                else:
                    s.os_left[a['unit']] = ('unknown after a failed flash of %s' % os.path.basename(img), False)
                s.done.setdefault(a['unit'], []).append('flash %s %s' % (os.path.basename(img), job.state.upper()))
            elif rc == 0 and over:
                self.record_pass(flash_key(a['unit'], d, over), {'image': img, 'unit': a['unit'], 'over': over})
                job.lines.append('recorded as the emulator pass for this file over the OS the emulator ran')
            elif rc == 0:
                job.lines.append('not recorded as an emulator pass: which OS the emulator ran before is unknown')
            if tgt == 'emulator' and rc == 0:
                self.note_emu(s, a['unit'], 'flash ' + os.path.basename(img), False)
                e = self.emulators.get((s.sid, a['unit'])) or {}
                if e.get('flashed'):                  # a restart with keep_flash boots this
                    write_json(e['flashed'], {'image': os.path.basename(img), 'sha': d})
                    e['build'] = os.path.basename(img)
                e['sha'] = d
            elif tgt == 'emulator':
                # the next emulator start overwrites its log: keep this one with the evidence
                e = self.emulators.get((s.sid, a['unit'])) or {}
                src = os.path.join(os.path.dirname(e['flashed']), 'emulator.log') if e.get('flashed') else None
                if src and os.path.isfile(src):
                    kept = os.path.join(folder, 'emulator-flash-%s.log' % stamp)
                    shutil.copyfile(src, kept)
                    job.lines.append('emulator log: %s' % kept)

        return (started if tgt == 'emulator' else []) + self.start(job, cmd, ELKFLASH, done, port)

    def digiemu_check(self, s, unit, img, digest, folder):
        """The Digitone's emulator gate: digiemu's own firmware check (emu.fwcheck: the
        container, the bootstrap finding and unpacking the OS from flash, a cold boot under
        strict mode, a scripted run), compared with the stock build, when the flash gives no
        build to upgrade from; a pass is recorded as this file's emulator pass. With a build
        (or a running Digitone emulator), elkflash upgrades the emulator over USB instead."""
        repo = need(DIGIEMU, 'digiemu', 'the Digitone/Digitakt emulator check (digiemu)')
        out = os.path.join(folder, 'fwcheck-%s-%s' % (os.path.splitext(os.path.basename(img))[0], digest[:8]))
        stock = next((p for p in UNITS[unit]['stock'] if os.path.isfile(p) and sha256(p) != digest), None)
        args = ['-m', 'emu.fwcheck', Path(img), '--out', Path(out), '--no-timing']
        if stock:
            args += ['--baseline', Path(stock)]
        cmd = self.digiemu_cmd(args)
        port = self.busy_port(unit, 'emulator', 'fwcheck %s' % os.path.basename(img))
        job = Job(s, 'flash', unit, 'emulator', 'fwcheck ' + os.path.basename(img))

        def done(job, rc, out_text):
            lines = [l.strip() for l in out_text.splitlines() if l.strip()]
            job.state = 'pass' if rc == 0 else 'fail'
            summary = read_json(os.path.join(out, 'report.json'), {}).get('summary') or lines[-6:]
            ref = next((i for i, l in enumerate(summary) if l.startswith('stock build')), len(summary))
            # the build's own verdict and the comparison; the stock build's run is only a reference
            job.lines = [l for l in summary[:ref] if not l.strip().startswith(('!', 'snap'))][:10]
            if ref < len(summary):
                job.lines.append(summary[ref] + ' (its quirks do not count against the build)')
            job.lines.append('report: %s' % os.path.join(out, 'report.json'))
            shutil.rmtree(os.path.join(out, 'work'), ignore_errors=True)    # over a gigabyte per build
            if rc == 0:
                self.record_pass('flash:%s:%s' % (unit, digest), {'image': img, 'unit': unit,
                                                                  'fwcheck': out})
                job.lines.append('recorded as the emulator pass for this file (digiemu fwcheck%s)'
                                 % (' against stock' if stock else ''))

        return self.start(job, cmd, repo, done, port)

    def device_do(self, s, unit, tgt, steps, timeout=180, need='steps'):
        """analogrun (or digirun) do STEPS on the unit or its emulator -> (ok, one line, folder)."""
        self.unit(unit, need_runner=need)
        if UNITS[unit]['runner'] == 'digirun':
            return self.digirun_do(s, unit, tgt, steps, timeout)
        runner()
        if tgt == 'hardware':
            self.check_hardware(s, [unit])
            self.check_connected(unit, tgt)
            where = UNITS[unit]['port']
            din = self.din_args(s, unit, tgt, steps.split(';'))
            port = self.hold_din(din, self.busy_port(unit, tgt, need))
        else:
            where = self.emu_port(s, unit)
            din = self.din_args(s, unit, tgt, steps.split(';'))
            port = self.busy_port('%s@%s' % (unit, where), tgt, need)
        folder = s.folder(unit)
        try:
            log = os.path.join(folder, 'steps.log')
            at = os.path.getsize(log) if os.path.exists(log) else 0
            cmd = [sys.executable, RUNNER, '--port', where] + din + ['--quiet', '--log', log, 'do', steps,
                                                                     '--out', folder]
            if tgt == 'hardware':
                cmd = [sys.executable, ELKLOCK, 'run', unit, '--'] + cmd
            r = subprocess.run(cmd, cwd=ANALOGHEALTH, env=s.env(), capture_output=True, text=True,
                               timeout=timeout, **NOWIN)
            lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
            line = lines[-1] if lines else 'exit %d' % r.returncode
            if r.returncode == 0:
                # what the steps reported (stats, say, shot sizes), not the step echoes
                with open(log, encoding='utf-8', errors='replace') as fh:
                    fh.seek(at)
                    said = [l.strip() for l in fh.read().splitlines() if re.match(r'  \D', l)]
                line = '%d step(s) done%s' % (steps.count(';') + 1, ''.join('\n  ' + l for l in said[-4:]))
            return r.returncode == 0, line, folder
        finally:
            port.release()

    def digirun_do(self, s, unit, tgt, steps, timeout):
        """digihealth's digirun.py do STEPS (no quiet mode: its output goes to steps.log here)."""
        script = os.path.join(need(DIGIHEALTH, 'digihealth', 'the Digitone/Digitakt runner (digihealth)'),
                              'tools', 'digirun.py')
        if tgt == 'hardware':
            self.check_hardware(s, [unit])
            self.check_connected(unit, tgt)
            where = UNITS[unit]['port']
            din = self.din_args(s, unit, tgt, steps.split(';'))
            port = self.hold_din(din, self.busy_port(unit, tgt, 'steps'))
        else:
            where = self.emu_port(s, unit)           # digiemu's USB MIDI link (digirun --port tcp:)
            din = self.din_args(s, unit, tgt, steps.split(';'))
            port = self.busy_port('%s@%s' % (unit, where), tgt, 'steps')
        folder = s.folder(unit)
        try:
            cmd = [sys.executable, script, '--port', where] + din + ['do', steps, '--out', folder]
            if tgt == 'hardware':
                cmd = [sys.executable, ELKLOCK, 'run', unit, '--'] + cmd
            r = subprocess.run(cmd, cwd=os.path.dirname(script), env=s.env(), capture_output=True,
                               text=True, timeout=timeout, **NOWIN)
            with open(os.path.join(folder, 'steps.log'), 'a', encoding='utf-8') as fh:
                fh.write('$ digirun do %s\n%s%s' % (steps, r.stdout, r.stderr))
            lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
            if r.returncode:
                return False, lines[-1] if lines else 'exit %d' % r.returncode, folder
            said = [l.strip() for l in r.stdout.splitlines() if re.match(r'  \D', l)]
            return True, '%d step(s) done%s' % (steps.count(';') + 1,
                                                ''.join('\n  ' + l for l in said[-4:])), folder
        finally:
            port.release()

    def t_steps(self, s, a):
        tgt = self.target(a.get('target'))
        text = a.get('steps', '')
        parts = [p.strip() for line in text.splitlines() for p in line.split(';')]
        parts = [p for p in parts if p and not p.startswith('#')]
        if not parts:
            raise Refused('no steps')
        total = 0.0
        for p in parts:
            w = shlex.split(p, comments=True)
            if not w:
                continue
            op = w[0].lower()
            allowed = STEP_OPS - no_steps(a.get('unit'))
            if op not in allowed:
                raise Refused('step %r is not allowed here (allowed: %s; screens: expect_screen)'
                              % (op, ', '.join(sorted(allowed))))
            if op in ('shot', 'dump', 'capture') and re.search(r'[\\/:]', w[1 if op == 'capture' else -1]):
                raise Refused('%s writes into this session\'s evidence folder: give a bare file name' % op)
            if op == 'capture':
                total += float(w[2])
            if op == 'send':
                self.check_dumps(w[1], s.folder(a.get('unit')) if a.get('unit') in UNITS else '.')
            if op == 'wait':
                total += float(w[1])
        if total > 120:
            raise Refused('steps longer than 2 min belong in a plan (run_plan)')
        if a.get('unit') in UNITS:
            if tgt == 'hardware':
                left = s.os_left.get(a['unit'])
                digest = left[2] if left and len(left) > 2 else None
            else:
                digest = (self.emulators.get((s.sid, a['unit'])) or {}).get('sha')
            self.check_stats(a['unit'], digest, {shlex.split(p, comments=True)[0].lower()
                                                 for p in parts if shlex.split(p, comments=True)},
                             'the OS on this %s' % tgt)
        ok, line, folder = self.device_do(s, a.get('unit'), tgt, '; '.join(parts))
        if tgt == 'emulator' and a.get('unit') in UNITS:
            ops = {shlex.split(p, comments=True)[0].lower() for p in parts if shlex.split(p, comments=True)}
            self.note_emu(s, a['unit'], 'steps (%s)' % ', '.join(sorted(ops)), ops & set(plancheck.WRITE_OPS))
        return ['%s: %s' % ('ok' if ok else 'fail', line), 'folder: %s' % folder]

    def check_dumps(self, name, folder):
        """`send` only carries kit/sound/pattern dumps, never RPC traffic such as an OS upgrade."""
        runner()
        import analogrun
        path = name if os.path.isabs(name) else os.path.join(folder, name)
        if not os.path.isfile(path):
            raise Refused('no file %s' % path)
        msgs = analogrun.load_syx(path)
        if not msgs or not all(analogrun.is_dump(m) for m in msgs):
            raise Refused('%s holds messages other than Elektron dumps; send refuses it' % path)

    def t_shot(self, s, a):
        tgt = self.target(a.get('target'))
        name = a.get('name') or 'shot-%s.png' % time.strftime('%H%M%S')
        if re.search(r'[\\/:]', name) or not name.endswith(('.png', '.bin')):
            raise Refused('name is a bare .png or .bin file name')
        ok, line, folder = self.device_do(s, a.get('unit'), tgt, 'shot %s' % fwd(name), timeout=30, need='screen')
        return ['%s: %s' % ('ok' if ok else 'fail', os.path.join(folder, name) if ok else line)]

    def t_expect_screen(self, s, a):
        tgt = self.target(a.get('target'))
        u = self.unit(a.get('unit'), need_runner='screen')
        ref = a.get('ref', '')
        ref = ref if ref.endswith('.bin') else ref + '.bin'
        ref = ref if os.path.isabs(ref) else os.path.join(u['refs'], ref)
        wait = min(float(a.get('wait_s') or 0), 30)
        if a.get('record'):
            if tgt != 'emulator':
                raise Refused('references are recorded in the emulator only')
            os.makedirs(os.path.dirname(ref), exist_ok=True)
            ok, line, _ = self.device_do(s, a['unit'], tgt, 'shot %s; shot %s' % (
                fwd(ref), fwd(os.path.splitext(ref)[0] + '.png')), timeout=30, need='screen')
            return ['%s: %s' % ('ok' if ok else 'fail', 'recorded ' + ref if ok else line)]
        if not os.path.isfile(ref):
            raise Refused('no reference %s (record it in the emulator: record true)' % ref)
        ok, line, folder = self.device_do(s, a['unit'], tgt, 'expect-screen %s %g' % (fwd(ref), wait),
                                          timeout=wait + 30, need='screen')
        if ok:
            return ['pass: the screen equals %s' % os.path.basename(ref)]
        return ['fail: %s' % line.replace('failed: AssertionError: ', ''), 'folder: %s' % folder]

    def t_data(self, s, a):
        """The data store on the unit or its emulator, into or from the evidence folder: ls and
        get (analogrun, or digirun on the Digitone), and put, clear and roundtrip through
        analogrun's `data`, which asks the local write policy first (on the Digitone too: the
        data store RPC is the same) and keeps a copy of what it replaces."""
        tgt = self.target(a.get('target'))
        op = a.get('op')
        path = a.get('path') or ''
        if op not in DATA_OPS:
            raise Refused('op is one of %s' % ', '.join(DATA_OPS))
        if not path.startswith('/'):
            raise Refused("path is a data store path starting with '/' ('/' lists the roots)")
        u = self.unit(a.get('unit'), need_runner='data')
        write = op in ('put', 'clear', 'roundtrip')
        if a.get('to') and op != 'roundtrip':
            raise Refused('to is for roundtrip')
        if u['runner'] == 'digirun' and not write:
            script = os.path.join(need(DIGIHEALTH, 'digihealth', 'the Digitone/Digitakt runner (digihealth)'),
                                  'tools', 'digirun.py')
        else:
            script = runner()
        slug = re.sub(r'[^\w-]+', '_', path.strip('/'))[:40] or 'root'
        now = time.strftime('%H%M%S')
        folder = s.folder(a['unit'])
        args = [op, path]
        if not write:
            name = a.get('name') or 'data-%s-%s-%s.%s' % (op, slug, now, 'bin' if op == 'get' else 'txt')
            if re.search(r'[\\/:]', name):
                raise Refused('name is a bare file name')
            out = os.path.join(folder, name)
            args += [out] if op == 'get' else []
        else:
            if op == 'put':
                src = a.get('file') or ''
                src = src if os.path.isabs(src) else os.path.join(folder, src)
                if not os.path.isfile(src):
                    raise Refused('put needs file: an object from data get (a bare name in this session\'s '
                                  'evidence folder, or a full path); no file %s' % src)
                args += [src]
            elif op == 'clear':
                args += [os.path.join(folder, 'data-clear-%s-%s-before.bin' % (slug, now))]
            else:
                args += [os.path.join(folder, 'data-roundtrip-%s-%s' % (slug, now))]
                args += ['--to', a['to']] if a.get('to') else []
            args += (['--name', a['slot_name']] if a.get('slot_name') else []) + ['--yes']
        if tgt == 'hardware':
            self.check_hardware(s, [a['unit']])
            self.check_connected(a['unit'], tgt)
            where = u['port']
            port = self.busy_port(a['unit'], tgt, 'data %s' % op)
        else:
            where = self.emu_port(s, a['unit'])
            port = self.busy_port('%s@%s' % (a['unit'], where), tgt, 'data %s' % op)
        try:
            cmd = [sys.executable, script, '--port', where, 'data'] + args
            if tgt == 'hardware':
                cmd = [sys.executable, ELKLOCK, 'run', a['unit'], '--'] + cmd
            r = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(script)), env=s.env(),
                               capture_output=True, text=True, timeout=300 if write else 120, **NOWIN)
        finally:
            port.release()
        if tgt == 'emulator' and write:
            self.note_emu(s, a['unit'], 'data %s %s' % (op, path), True)
        text = r.stdout + r.stderr
        with open(os.path.join(folder, 'steps.log'), 'a', encoding='utf-8') as fh:
            fh.write('$ data %s\n%s' % (' '.join(args), text))
        lines = [l.rstrip() for l in text.splitlines() if l.strip() and not re.fullmatch(r'\s*[\d.]+ %', l)]
        if r.returncode:
            last = lines[-1].strip() if lines else 'exit %d' % r.returncode
            # analogrun exits 2 when the write policy (or a data store rule) refuses
            return ['%s: %s' % ('refused' if r.returncode == 2 else 'fail', last), 'folder: %s' % folder]
        if op == 'get':
            return ['ok: %s (%d bytes)' % (out, os.path.getsize(out) if os.path.isfile(out) else 0)]
        if write:
            return ['ok: ' + l.strip() for l in lines[-2:]] + ['folder: %s' % folder]
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(text)
        if tgt == 'hardware' and path.rstrip('/') == '/projects':     # for unit_state
            os.makedirs(os.path.join(STATE, 'unit-state'), exist_ok=True)
            write_json(os.path.join(STATE, 'unit-state', a['unit'] + '.json'),
                       {'time': time.time(), 'session': s.sid, 'projects': lines, 'listing': out})
        cut = len(lines) > 40
        return ['ok: %d line(s) for %s%s' % (len(lines), path, ', the first 40:' if cut else ':')] + \
            ['  ' + l for l in lines[:40]] + ['listing: %s' % out]

    def emu_audio_record(self, s, a):
        """The Digitone emulator's twelve Overbridge channels (digiemu's --ob-audio-port:
        frames of 12 big-endian int32 at 48 kHz) into a 32-bit float WAV, as asio_rec
        writes one, with its .channels.txt."""
        unit = a.get('unit')
        if (UNITS.get(unit) or {}).get('no_emu_audio'):
            raise Refused('no emulator audio for %s: %s' % (unit, UNITS[unit]['no_emu_audio']))
        e = self.emulators.get((s.sid, unit)) or {}
        if not e or e['proc'].poll() is not None:
            raise Refused('no %s emulator of this session is running: emulator start first' % unit)
        if not e.get('ob_audio'):
            raise Refused('this %s emulator gives no audio (its digiemu has no --ob-audio-port)' % unit)
        if a.get('tone'):
            raise Refused('the emulator has no audio input to play a tone into')
        secs = float(a.get('seconds') or 0)
        if not 0 < secs <= 120:
            raise Refused('seconds is 1..120')
        name = a.get('name') or 'rec-%s' % time.strftime('%H%M%S')
        if re.search(r'[\\/:]', name):
            raise Refused('name is a bare file name')
        wav = os.path.join(s.folder(unit), name if name.endswith('.wav') else name + '.wav')
        frames, want = 12 * 4, int(secs * 48000)
        data = bytearray()
        try:
            with socket.create_connection(('127.0.0.1', e['ob_audio']), timeout=10) as sock:
                sock.settimeout(30)                  # the emulator may run slower than real time
                while len(data) < want * frames:
                    got = sock.recv(1 << 16)
                    if not got:
                        break
                    data += got
        except OSError as exc:
            raise Refused('the emulator\'s audio link: %s' % exc)
        n = min(len(data) // frames, want)
        samples = struct.unpack('>%di' % (n * 12), bytes(data[:n * frames]))
        body = struct.pack('<%df' % len(samples), *(v / 2147483648.0 for v in samples))
        with open(wav, 'wb') as fh:
            fh.write(b'RIFF' + struct.pack('<I', 36 + len(body)) + b'WAVEfmt ' +
                     struct.pack('<IHHIIHH', 16, 3, 12, 48000, 48000 * 48, 48, 32) +
                     b'data' + struct.pack('<I', len(body)) + body)
        with open(os.path.splitext(wav)[0] + '.channels.txt', 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(OB_CHANNELS) + '\n')
        peaks = [max((abs(v) for v in samples[c::12]), default=0) / 2147483648.0 for c in range(12)]
        loud = ['%s %.3f' % (OB_CHANNELS[c], p) for c, p in enumerate(peaks) if p > 0.001]
        return ['%s: %s (%.2f s, 12 channels)' % ('ok' if n == want else 'fail', wav, n / 48000.0),
                'peaks: %s' % (', '.join(loud) or 'all silent')]

    def t_audio_record(self, s, a):
        if self.target(a.get('target') or 'hardware') == 'emulator':
            return self.emu_audio_record(s, a)
        u = self.unit(a.get('unit'))
        need(USB_AUDIO, 'usb_audio', 'audio recording')
        if not u['asio']:
            raise Refused('no ASIO driver known for %s' % a.get('unit'))
        secs = float(a.get('seconds') or 0)
        if not 0 < secs <= 120:
            raise Refused('seconds is 1..120')
        name = a.get('name') or 'rec-%s' % time.strftime('%H%M%S')
        if re.search(r'[\\/:]', name):
            raise Refused('name is a bare file name')
        self.check_hardware(s, [a['unit']])
        folder = s.folder(a['unit'])
        wav = os.path.join(folder, name if name.endswith('.wav') else name + '.wav')
        tool = (['asio_tone.py', u['asio'], wav, '%g' % secs, '0'] if a.get('tone')
                else ['asio_rec.py', 'rec', u['asio'], '%g' % secs, wav])
        tool[0] = os.path.join(USB_AUDIO, tool[0])
        port = self.busy_port('asio', 'hardware')
        try:
            r = subprocess.run([sys.executable, ELKLOCK, 'run', a['unit'], '--'] + AUDIO_PY + tool, cwd=USB_AUDIO,
                               env=s.env(), capture_output=True, text=True, timeout=secs + 60,
                               **NOWIN)
        finally:
            port.release()
        lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        if r.returncode:
            return ['fail: %s' % (lines[-1] if lines else 'exit %d' % r.returncode)]
        return ['ok: %s (%g s%s)' % (wav, secs, ', 997 Hz tone into the unit' if a.get('tone') else '')]

    def t_audio_check(self, s, a):
        need(USB_AUDIO, 'usb_audio', 'audio checks')
        op = a.get('op')
        if op not in ('dropouts', 'compare'):
            raise Refused("op is 'dropouts' or 'compare'")
        base = a.get('folder') or os.getcwd()
        path = lambda p: p if os.path.isabs(p) else os.path.join(base, p)
        wav = path(a.get('wav', ''))
        cmd = AUDIO_PY + [os.path.join(USB_AUDIO, 'audiocheck.py'), op]
        if op == 'compare':
            cmd += [path(a.get('ref', '')), wav, '--png', os.path.splitext(wav)[0] + '-compare.png']
        else:
            cmd += [wav]
        cmd += ['--quiet', '--log', os.path.splitext(wav)[0] + '-%s.log' % op]
        if a.get('channels'):
            cmd += ['--ch', str(a['channels'])]
        r = subprocess.run(cmd, cwd=USB_AUDIO, capture_output=True, text=True, timeout=300,
                           **NOWIN)
        lines = [l.strip() for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        verdict = lines[-1] if lines else 'exit %d' % r.returncode
        return ['%s: %s' % ('pass' if r.returncode == 0 else 'fail', verdict)]

    def engine_job(self, s, a, verb, extra, label, approval_item=None):
        if not WINDOWS and sys.platform != 'darwin':
            raise Refused('Total Recall needs the Overbridge Engine, which Elektron makes for Windows '
                          'and macOS only')
        need(OBTR, 'obtr', 'Overbridge Total Recall (obtr.py)')
        u = self.unit(a.get('unit'))
        if not u.get('engine'):
            raise Refused('no Total Recall for %s (obtr.py does not know its state types)' % a['unit'])
        self.check_hardware(s, list(UNITS))       # the Engine is shared by every unit
        if approval_item:
            self.use_approval(s, a.get('approval'), approval_item, label)
        try:
            port = self.busy_port('engine', 'hardware')
        except Refused:
            if approval_item:
                self.give_back(a.get('approval'), approval_item)
            raise
        cmd = [sys.executable, ELKLOCK, 'run'] + list(UNITS) + ['--', sys.executable, OBTR, verb] + extra + \
              ['--device', u['engine']]
        job = Job(s, 'recall', a['unit'], 'hardware', label)

        def done(job, rc, out):
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            job.state = 'pass' if rc == 0 else 'fail'
            if any('refused' in l.lower() or '10061' in l for l in lines):
                lines.append('the Overbridge Engine is not answering; restarting it needs %s\'s OK' % PERSON)
            job.lines = lines[-4:]
            s.done.setdefault(a['unit'], []).append('%s %s' % (label, job.state.upper()))

        return self.start(job, cmd, os.path.dirname(OBTR), done, port)

    def t_recall_take(self, s, a):
        name = a.get('name') or 'recall-%s.bin' % time.strftime('%H%M%S')
        if re.search(r'[\\/:]', name):
            raise Refused('name is a bare file name')
        self.unit(a.get('unit'))
        out = os.path.join(s.folder(a['unit']), name if name.endswith('.bin') else name + '.bin')
        return self.engine_job(s, a, 'take', [out], 'recall take ' + os.path.basename(out))

    def t_recall_restore(self, s, a):
        self.unit(a.get('unit'))
        img = os.path.abspath(a.get('image', ''))
        if not os.path.isfile(img) or not os.path.isfile(os.path.splitext(img)[0] + '.json'):
            raise Refused('no image %s with its session .json beside it (from recall_take)' % img)
        return self.engine_job(s, a, 'restore', [img, '--answer-timeout', '120'],
                               'recall restore ' + os.path.basename(img),
                               ('restore', a['unit'], sha256(img)))

    # -- batches: one plan or flash run many times in the emulator, across variants --

    def t_batch(self, s, a):
        """Each run boots a fresh headless emulator of its own (not the session's emulator, which
        stays as it is), runs the plan or flash, and ends it; batch.py orders, caps and sums them
        up. A counting batch never records an emulator pass. A gate batch (gate) does: it runs
        each of several hardware plans once, side by side, each on a fresh emulator (resuming its
        setup snapshot where it has snapshot-after), and a run that passes is recorded like
        run_plan's. Plans whose pass still stands are left out (unless force)."""
        if a.get('stop'):
            j = self.jobs.get(a['stop'])
            if not j or not j.batch or j.sess is not s:
                raise Refused('no batch %s of this session' % a['stop'])
            j.batch.stop()
            return ['ok: %s starts no more runs; the %s' % (j.id, j.batch.progress())]
        gate = bool(a.get('gate'))
        given = a.get('variants') or []
        if a.get('plans'):
            if not gate:
                raise Refused('plans (a list) is for a gate batch: add gate true, or give plan and variants')
            if given or a.get('plan'):
                raise Refused('give plans, or plan/variants, not both')
            if not isinstance(a['plans'], list) or not all(isinstance(p, str) for p in a['plans']):
                raise Refused('plans is a list of plan files')
            given = [{'plan': p} for p in a['plans']]
        if gate:
            if a.get('image') or any(isinstance(v, dict) and v.get('image') for v in given):
                raise Refused('a gate batch runs plans; a flash on its own is gated with flash target emulator')
            if a.get('runs') not in (None, 1):
                raise Refused('a gate batch runs each plan once: leave runs out')
            given = given or ([{}] if a.get('plan') else [])
            if not 1 <= len(given) <= GATE_PLANS_MAX or not all(isinstance(v, dict) for v in given):
                raise Refused('a gate batch takes plans: a list of 1-%d plan files (or variants with a plan '
                              'each), each run once' % GATE_PLANS_MAX)
            if not all(v.get('plan') or a.get('plan') for v in given):
                raise Refused('a gate batch takes plans: give plans, or a plan for every variant')
        else:
            if bool(a.get('plan')) == bool(a.get('image')):
                raise Refused('give plan (a plan file) or image (an OS .syx to flash), and variants')
            if not 1 <= len(given) <= 6 or not all(isinstance(v, dict) for v in given):
                raise Refused('variants is a list of 1-6 objects {name, build, image or plan}: each boots '
                              'build (else the batch\'s build), then flashes its image or runs its plan')
        runs = 1 if gate else int(a.get('runs') or 10)
        parallel = int(a.get('parallel') or batch.PARALLEL_DEFAULT)
        if not 1 <= runs <= 200 or not 1 <= parallel <= 8:
            raise Refused('runs (per variant) is 1-200, parallel 1-8')
        seed = int(a['seed']) if a.get('seed') is not None else secrets.randbelow(10 ** 6)
        flash = bool(a.get('image'))
        minutes = min(max(int(a.get('minutes') or (10 if flash else 20)), 1), 60)
        variants, units, notes = [], set(), []
        for i, v in enumerate(given):
            info = None if flash else self.plan_info(v.get('plan') or a['plan'])
            # a gate plan that names its OS (os = FILE) boots that file unless its variant gives a
            # build, so plans on different OS files gate in one batch
            build = v.get('build') or (gate and info['os_file']) or a.get('build') or ''
            build = os.path.abspath(build)
            if not os.path.isfile(build) or not build.lower().endswith('.syx'):
                raise Refused('%s %d: build is the OS .syx its emulator boots' % ('plan' if gate else 'variant', i + 1))
            x = dict(name=str(v.get('name') or chr(65 + i)), build=build)
            if flash:
                x['image'] = os.path.abspath(v.get('image') or a['image'])
                if not os.path.isfile(x['image']) or not x['image'].lower().endswith('.syx'):
                    raise Refused('variant %s: no OS .syx file %s' % (x['name'], x['image']))
                units.add(a.get('unit'))
            else:
                x['info'] = info
                if info['power_cycles'] and not gate:
                    raise Refused('%s has power-cycle steps: batches do not power cycle yet, run it with '
                                  'run_plan target emulator (a gate batch runs only its start runs)'
                                  % info['name'])
                units.add(info['unit'])
            if gate:
                info = x['info']
                if info['emulator']:
                    raise Refused('%s is an emulator plan: a gate batch runs hardware plans (each run gets '
                                  'its emulator twin)' % info['path'])
                if not v.get('name'):
                    x['name'] = info['name']
                    while any(y['name'] == x['name'] for y in variants):
                        x['name'] += '+'
                x['gate'] = True
                x['os'] = sha256(build)
                if info['os_sha'] and x['os'] != info['os_sha']:
                    raise Refused('%s runs on %s (os =), and its gate run would boot %s: give that file as '
                                  'its build' % (info['name'], os.path.basename(info['os_file']),
                                                 os.path.basename(build)))
            variants.append(x)
        if len({v['name'] for v in variants}) < len(variants):
            raise Refused('variant names must differ')
        if gate and len({(v['info']['key'], v['os']) for v in variants}) < len(variants):
            raise Refused('the same plan twice on the same build: a gate batch runs each plan once')
        if gate:
            # proposal A and B: besides its own run, each plan runs from every start its go needs
            # (the unit's required built-in starts, and those its `say contrast` lines name),
            # each on a fresh emulator of its own; a plan whose contrast names `fresh` stops
            # there, so its own pass comes from run_plan after the plan that leaves its start
            # A plan with power-cycle steps: batches do not power cycle yet, so its own pass comes
            # from run_plan target emulator, and the gate runs only its start runs, each up to its
            # first power cycle: a start run that must stop in a section before it never reaches it.
            runs_of = []
            for v in variants:
                u_, home = v['info']['unit'], v['info'].get('home')
                want, have = starts_needed(v['info']), unit_starts(u_)
                if v['info']['power_cycles']:
                    name = v['info']['name']
                    if home:
                        raise Refused('%s has power-cycle steps and start = %s: its own pass needs a gate batch, '
                                      'and batches do not power cycle yet' % (name, home))
                    before = set()           # the sections wholly before the first power cycle
                    for n, ls in v['info']['steps']:
                        if any(is_power_cycle(l) for l in ls):
                            break
                        before.add(n)
                    late = ['%s (must %s)' % (st, 'pass' if e is None else 'stop in [%s]' % e)
                            for st, e in sorted(want.items()) if e not in before]
                    if late:
                        raise Refused('%s: its run from start %s would reach the power cycle, and batches do not '
                                      'power cycle yet: only start runs that stop before its first power cycle '
                                      'can be gated' % (name, ', '.join(late)))
                    notes.append('%s: start runs only, each up to its first power cycle; its own pass comes '
                                 'from run_plan target emulator (batches do not power cycle yet)' % name)
                    runs_of += [dict(v, name='%s @%s' % (v['name'], st), start=st, expect=expect, power_cut=True,
                                     start_plan=have.get(st),
                                     start_chain=start_chain(u_, st) if have.get(st) else None)
                                for st, expect in sorted(want.items())]
                    continue
                if home:                 # start = START: its own run (and pass) from that start
                    runs_of.append(dict(v, name='%s (from %s)' % (v['name'], home), home=home,
                                        start_plan=have[home], start_chain=start_chain(u_, home)))
                elif 'fresh' not in want:
                    runs_of.append(v)
                for st, expect in sorted(want.items()):
                    runs_of.append(dict(v, name='%s @%s' % (v['name'], st), start=st, expect=expect,
                                        start_plan=have.get(st),
                                        start_chain=start_chain(u_, st) if have.get(st) else None))
            variants = runs_of
        if len(units) != 1:
            raise Refused('every variant runs on one unit' if flash or len(units) > 1 else 'give unit')
        if gate and not variants:
            return ['ok: nothing to gate here: no start runs needed'] + notes
        unit = units.pop()
        u = self.unit(unit)
        digi = u.get('emulator') == 'digiemu'
        if digi:
            # each run copies a settled card (~300 MB) and boots digiemu in WSL: a few at a time
            need(DIGIEMU, 'digiemu', 'the Digitone/Digitakt emulator (digiemu)')
            if not a.get('parallel'):
                parallel = min(parallel, DIGI_PARALLEL_MAX)
            if parallel > DIGI_PARALLEL_MAX:
                raise Refused('%s batches run at most %d at a time (each digiemu copies ~300 MB '
                              'and takes a CPU core)' % (unit, DIGI_PARALLEL_MAX))
        elif not u.get('emu_exe'):
            raise Refused('no emulator for %s' % unit)
        else:
            need(ANALOG_EMU, 'analog_emu', 'the analog-emu emulators')
        need(ELKFLASH, 'elkflash', 'elkflash')
        flash or runner()
        if not flash and any(v['info']['din'] for v in variants) and not self.emu_has_din(unit):
            raise Refused(self.no_emu_din(unit))
        kept = []
        if gate and not a.get('force'):
            # Re-gate only what changed: a plan whose pass still stands (same steps, refs, OS file
            # it flashes, the same build booted, from a known start: a fresh emulator or a setup
            # snapshot) keeps it and is not run again.
            for v in list(variants):
                if v.get('start'):
                    e = self.find_start(v['info']['key'], v['os'], v['start'], v['expect'])
                    if e:
                        kept.append('kept: %s, unchanged since it %s (%s)' % (v['name'], e.get('line', 'ran'),
                                                                                e.get('run_dir')))
                        variants.remove(v)
                    continue
                ep = self.plan_pass(v['info'])
                known = ('built-in start ' + v['home'],) if v.get('home') else KNOWN_STARTS
                if ep and ep.get('os') == v['os'] and ep.get('start') in known:
                    kept.append('kept: %s, unchanged since its pass (%s)' % (
                        v['name'], ep.get('run_dir') or stamp(ep['time'])))
                    variants.remove(v)
            if not variants:
                return ['ok: nothing to gate: every plan\'s pass still stands (force runs them again)'] + kept + notes
        snapshot = bool(a.get('snapshot'))
        if snapshot and digi and not gate:
            raise Refused('no setup snapshots on the %s emulator (digiemu resumes only its settled boot)' % unit)
        if gate:
            # setup inside each plan (snapshot-after): its setup runs once per build and is
            # resumed, as run_plan does; plans with the same setup share one snapshot
            snap_ok = lambda v: v['info']['setup'] and not v['info']['flash'] and not v.get('start_plan')
            can_snap = not digi and self.emu_has_snapshots(unit)
            snapshot = a.get('snapshot') is not False and any(snap_ok(v) for v in variants) and can_snap
            for v in variants:
                v['snapshot'] = bool(snapshot and snap_ok(v))
                # a built-in start is built once per build and kept as a snapshot where it can be
                v['start_snap'] = bool(v.get('start_plan') and can_snap)
            if snapshot:
                self.prune_snapshots()
        elif snapshot:
            # Every resumed run starts at the same emulated moment: for steps whose timing does not
            # matter, never to measure something timing-dependent (that is why it is off by default)
            if flash:
                raise Refused('snapshot is for plan batches (a flash batch boots each run fresh)')
            for v in variants:
                i = v['info']
                if not i['setup'] or i['emulator'] or i['flash']:
                    raise Refused('variant %s: snapshot needs a hardware plan with snapshot-after and no '
                                  'flash (%s)' % (v['name'], i['name']))
                v['snapshot'] = True
            if not self.emu_has_snapshots(unit):
                raise Refused('this analog-emu has no --stdin-commands, so no snapshots')
            self.prune_snapshots()
        # each variant's label names only what differs between them
        parts = lambda v: [('build', os.path.basename(v['build'])),
                           ('what', os.path.basename(v['image']) if flash else v['info']['name'])]
        vary = {k for k, _ in parts(variants[0]) if len({dict(parts(v))[k] for v in variants}) > 1} or {'build'}
        for v in variants:
            v['label'] = ' + '.join(x for k, x in parts(v) if k in vary)
        if gate:
            n_plans = len({v['info']['path'] for v in variants})
            desc = 'gate %d plan(s)' % n_plans + (', %d run(s)' % len(variants) if len(variants) > n_plans else '')
        else:
            desc = 'batch %s x%d' % (os.path.basename(a['image']) if flash else variants[0]['info']['name'], runs)
        job = Job(s, 'batch', unit, 'emulator', desc)
        folder = os.path.join(s.folder(unit), ('gate-' if gate else 'batch-') + job.id)
        os.makedirs(folder)
        write_json(os.path.join(folder, 'batch.json'), dict(
            unit=unit, kind='gate' if gate else 'flash' if flash else 'plan', runs=runs, parallel=parallel,
            seed=seed, minutes=minutes, snapshot=snapshot, kept=kept,
            variants=[{k: (v[k] if k != 'info' else v[k]['path']) for k in v} for v in variants]))
        locks, locks_guard = {}, threading.Lock()

        def gate_one(n, v):
            # A gate run takes (or waits for) its setup snapshot itself, so plans without one
            # start at once instead of waiting for every setup; one setup run per snapshot key
            if v.get('start_snap'):
                with locks_guard:
                    lock = locks.setdefault('start:' + self.start_key(unit, v['build'], v['start_chain']),
                                            threading.Lock())
                with lock:
                    why = self.batch_start(s, unit, v, minutes, folder)
                if why:
                    return dict(outcome='error', line=why)
            if v.get('snapshot'):
                with locks_guard:
                    lock = locks.setdefault(self.snapshot_key(v['info'], v['build']), threading.Lock())
                with lock:
                    why = self.batch_snapshot(s, unit, v, minutes, folder)
                if why:
                    return dict(outcome='fail', line=why)
            return self.batch_run(s, unit, flash, minutes, folder, n, v)
        job.batch = b = batch.Batch(variants, runs, parallel, seed, folder, gate_one if gate else
                                    lambda n, v: self.batch_run(s, unit, flash, minutes, folder, n, v))
        with self.lock:
            self.jobs[job.id] = job

        def work():
            try:
                snaps = [v for v in variants if v.get('snapshot')]
                if snaps and not gate:
                    # each setup once, a few at a time: the first plan of each setup takes (or finds)
                    # the snapshot, the others with the same setup then find it stored
                    first, rest, seen = [], [], set()
                    for v in snaps:
                        k = self.snapshot_key(v['info'], v['build'])
                        (rest if k in seen else first).append(v)
                        seen.add(k)
                    why = []
                    for group in (first, rest):
                        with ThreadPoolExecutor(parallel) as ex:
                            why += [w for w in ex.map(lambda v: self.batch_snapshot(s, unit, v, minutes, folder),
                                                      group) if w]
                        if why:
                            break
                    if why:
                        job.state, job.lines = 'fail', why[:3] + ['no runs made: results %s' % folder]
                        return
                b.run()
                job.state = 'stopped' if b.stopped.is_set() else 'done'
                if gate:
                    job.lines = self.gate_lines(variants, b.results, parallel, len(snaps),
                                                b.stopped.is_set()) + kept + notes
                    if any(r['outcome'] != 'pass' for r in b.results) or b.stopped.is_set():
                        job.state = 'fail' if not b.stopped.is_set() else 'stopped'
                    job.lines.append('results: %s' % folder)
                else:
                    job.lines = ['%d runs, %d variant(s), %d at a time, seed %d%s%s' % (
                        len(b.results), len(variants), parallel, seed,
                        ', resumed from setup snapshots' if snapshot else '',
                        ' (stopped early: no p-value means much)' if b.stopped.is_set() else '')]
                    job.lines += batch.summarize(variants, b.results) + [
                        'results: %s (a run that did not pass keeps its rNN folder)' % folder]
                with open(os.path.join(folder, 'summary.txt'), 'w', encoding='utf-8') as fh:
                    fh.write('\n'.join(job.lines) + '\n')
            except Exception as e:                # noqa: BLE001 - becomes the batch's verdict
                job.state, job.lines = 'fail', ['%s: %s (results so far: %s)' % (type(e).__name__, e, folder)]
            finally:
                job.ended = time.time()
                self.last_use = time.time()
                self.log_line({'session': s.sid, 'job': job.id, 'end': job.state, 'lines': job.lines[:3]})

        threading.Thread(target=work, daemon=True).start()
        if gate:
            return ['running: %s, each once on a fresh emulator, %d at a time%s; status with run_id %s '
                    'waits for its end' % (job.desc, parallel, ', setups from snapshots' if snapshot else '',
                                           job.id)] + kept + notes
        return ['running: %s, %d runs (%d variant(s) x %d), %d at a time, seed %d%s; status with run_id %s '
                'waits for its end' % (job.desc, len(b.plan), len(variants), runs, parallel, seed,
                                       ', each variant\'s setup snapshot first' if snapshot else '', job.id)]

    def gate_lines(self, variants, results, parallel, snaps, stopped):
        """A gate batch's verdict: a line per plan (recorded, or why not), then what an emulator
        run leaves untested or shortens, as run_plan says it."""
        by = {r['variant']: r for r in results}
        main = [v for v in variants if not v.get('start')]
        starts = [v for v in variants if v.get('start')]
        good = lambda vs: sum((by.get(v['name']) or {}).get('outcome') == 'pass' for v in vs)
        lines = ['%d of %d plan(s) passed and recorded as their emulator pass%s, %d at a time%s%s' % (
            good(main), len(main), '; %d of %d start run(s) went as their plans say' % (good(starts), len(starts))
            if starts else '', parallel, ', %d resumed a setup snapshot' % snaps if snaps else '',
            ' (stopped early)' if stopped else '')]
        for v in variants:
            r = by.get(v['name'])
            if not r:
                lines.append('%s: not run' % v['name'])
            elif r['outcome'] == 'pass':
                lines.append('%s: %s, recorded (%s)' % (v['name'], 'PASS' if not v.get('start') else r.get('line'),
                                                       r.get('run_dir')))
            else:
                lines.append('%s: %s: %s (%s)' % (v['name'], r['outcome'].upper(), r.get('line') or '',
                                                   r.get('run_dir') or r.get('folder')))
        seen = set()
        for v in variants:
            if v['info']['path'] in seen:
                continue
            seen.add(v['info']['path'])
            lines += ['%s: not tested here (skipped in the emulator): %s' % (v['info']['name'], h)
                      for h in hand_steps(v['info']['steps'])]
            lines += ['%s: short here (%g s in the emulator, in full on the unit): %s' % (v['info']['name'], AUDIO_WAIT_EMULATOR, w)
                      for w in v['info']['audio_waits']]
        return lines

    def batch_emulator(self, unit, build, work, minutes, snapshot=None, din=False):
        """A fresh headless emulator of `build` for one batch run, in `work`, on a port of its own;
        with `snapshot`, resuming that file (or, if it does not exist, saving to it on `save`);
        with `din`, its DIN MIDI on a port of its own too. -> (proc, address, None, DIN address or
        None), or (proc or None, None, why, None) when it did not come up. For the Digitone, proc
        is digiemu's emulator dict (end_emulator stops either)."""
        if UNITS[unit].get('emulator') == 'digiemu':
            try:
                e, _ = self.digiemu_launch(build, minutes, 'b-%s' % hashlib.sha256(work.encode()).hexdigest()[:12],
                                           work, os.path.join(work, 'firmware'))
            except Refused as why:
                return None, None, str(why), None
            if din and not e.get('din'):
                self.end_emulator(e)
                return None, None, self.no_emu_din(unit), None
            return e, e['address'], None, e.get('din') if din else None
        exe = os.path.join(ANALOG_EMU, UNITS[unit]['emu_exe'])
        cmd = [exe, build, '--flash', os.path.join(work, 'flash.img'), '--no-audio', '--headless',
               '--no-midi', '--after', str(minutes * 60 + 120)]
        if UNITS[unit].get('emu_card'):
            shutil.copyfile(UNITS[unit]['emu_card'], os.path.join(work, 'card.img'))
            cmd[1:1] = ['--card', os.path.join(work, 'card.img')]
        if snapshot:
            cmd += ['--snapshot', snapshot, '--stdin-commands']
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0))
            tcp = probe.getsockname()[1]
        cmd += ['--usb-midi-tcp', '127.0.0.1:%d' % tcp]
        addr = 'tcp:127.0.0.1:%d' % tcp
        din_addr = None
        if din:
            with socket.socket() as probe:
                probe.bind(('127.0.0.1', 0))
                din_addr = 'tcp:127.0.0.1:%d' % probe.getsockname()[1]
            cmd += ['--din-midi-tcp', din_addr[len('tcp:'):]]
        log_path = os.path.join(work, 'emulator.log')
        with open(log_path, 'w', encoding='utf-8') as log:
            proc = subprocess.Popen(cmd, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                    stdin=subprocess.PIPE if snapshot else None, **NOWIN)
        for _ in range(50):                       # until its ports listen
            time.sleep(0.2)
            with open(log_path, encoding='utf-8', errors='replace') as fh:
                text = fh.read()
            if ('usb midi tcp: listening' in text and (not din or 'din midi tcp: listening' in text)
                    or proc.poll() is not None):
                break
        if 'usb midi tcp: listening' not in text or din and 'din midi tcp: listening' not in text:
            return proc, None, 'the emulator did not open its port%s' % ('s' if din else ''), None
        for _ in range(30):                       # until the OS answers (the port listens first)
            try:
                if subprocess.run([sys.executable, '-m', 'elkflash', 'info', '--port', addr, '--timeout', '1'],
                                  cwd=ELKFLASH, capture_output=True, timeout=10, **NOWIN).returncode == 0:
                    return proc, addr, None, din_addr
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1)
        return proc, None, 'the emulator\'s OS never answered', None

    def end_emulator(self, proc):
        if isinstance(proc, dict):                # a Digitone emulator: stop it inside WSL too
            self.stop_emulator(proc)
        elif proc:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def batch_plan(self, info, work, title, sections=None, out=None):
        """A twin of the plan for one batch run (all of it, or only `sections`), its run folder in
        `work` (or `out`): runs going at once must not share testplan's per-second run folder
        names. `wait N audio` is short here, as in every emulator twin."""
        g = info['settings'].get
        lines = ['# written by elkbench for %s of %s' % (title, info['path']),
                 'include = %s' % info['path'], 'emulator = yes', 'din =', 'record-refs = no',
                 'refs = %s' % info['refs'], 'out = %s' % (out or work)]
        lines += ['%s = %s' % (k, info['rel'](g(k))) for k in ('flash', 'os', 'audio-tools', 'elkflash') if g(k)]
        if g('flash') and not g('elkflash') and ELKFLASH:
            lines.append('elkflash = %s' % ELKFLASH)
        if sections is None and info.get('audio_waits'):
            sections = info['steps']
        if sections is not None:
            sections = emulator_steps(sections)
            lines.append('name = %s (%s)' % (info['name'], title))
            for name, body in sections:
                lines += ['', '[%s]' % name] + body
        plan = os.path.join(work, 'plan.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
        return plan

    def plan_run_dir(self, out):
        """-> the run folder an analogrun run's verdict line names, or None."""
        m = next((re.search(r' - (.+)$', l) for l in out.splitlines() if re.search(r': (PASS|FAIL) \(', l)), None)
        return m.group(1).strip() if m else None

    def plan_outcome(self, rc, out):
        """-> (passed, line) of an analogrun run: its first failure, else its last line."""
        out = [l.strip() for l in out.splitlines() if l.strip()]
        m = next((re.search(r' - (.+)$', l) for l in out if re.search(r': (PASS|FAIL) \(', l)), None)
        summ = read_json(os.path.join(m.group(1).strip(), 'summary.json'), {}) if m else {}
        first = next((l[len('first failure, '):] for l in out if l.startswith('first failure, ')), None)
        return rc == 0 and bool(summ.get('pass')), first or next(
            (l for l in out if l.startswith('FAIL ')), None) or (out or ['exit %d' % rc])[-1]

    def batch_snapshot(self, s, unit, v, minutes, folder):
        """Before a snapshot batch's runs: v's setup snapshot, from the store when one of the
        same key is there, else taken now by a setup run that has to pass (kept like run_plan's,
        so later runs and batches reuse it). -> None, or why the batch cannot go on."""
        info, cut = v['info'], v['info']['setup']
        v['store'] = store = os.path.join(STATE, 'snapshots', self.snapshot_key(info, v['build']) + '.snap')
        if os.path.isfile(store) and os.path.isfile(store + '.json'):
            os.utime(store + '.json')                  # last use, for prune_snapshots
            return None
        work = os.path.join(folder, 'setup-%s' % re.sub(r'\W+', '-', v['name']))
        os.makedirs(work)
        live = os.path.join(work, 'setup.snap')
        proc, addr, why, din = self.batch_emulator(unit, v['build'], work, minutes, snapshot=live,
                                                   din=info['din'])
        try:
            if why:
                return 'variant %s: %s (%s)' % (v['name'], why, work)
            plan = self.batch_plan(info, work, 'its setup', sections=info['steps'][:cut])
            guard = v.get('gate') and UNITS[unit]['runner'] is True
            if guard:                    # a gate's setup may write only the plan's slots too
                before = self.saved_projects(addr, os.path.join(work, 'projects', 'before'))
            p = subprocess.run([sys.executable, RUNNER, '--port', addr] + (['--din', din] if din else []) +
                               ['--quiet', 'run', plan], cwd=ANALOGHEALTH,
                               env=s.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding='utf-8', errors='replace', timeout=minutes * 60, **NOWIN)
            ok, line = self.plan_outcome(p.returncode, p.stdout)
            if not ok:
                return 'variant %s: its setup did not pass: %s (%s)' % (v['name'], line, work)
            if guard:
                bad = project_changes(before, self.saved_projects(addr, os.path.join(work, 'projects', 'after')),
                                      saved_slots(info['steps']))
                if bad:
                    return 'variant %s: its setup wrote outside the plan\'s slots: %s (%s)' % (
                        v['name'], '; '.join(bad), work)
            proc.stdin.write(b'save\n')
            proc.stdin.flush()
            for _ in range(150):
                time.sleep(0.2)
                with open(os.path.join(work, 'emulator.log'), encoding='utf-8', errors='replace') as fh:
                    if 'saved ' + live in fh.read():
                        break
            else:
                return 'variant %s: the emulator did not save its setup snapshot (%s)' % (v['name'], work)
            m = re.search(r': PASS \(.* - (.+)$', p.stdout, re.M)
            os.makedirs(os.path.dirname(store), exist_ok=True)
            shutil.copyfile(live, store + '.tmp')
            os.replace(store + '.tmp', store)
            write_json(store + '.json', {'plan': info['path'], 'setup_until': info['steps'][cut - 1][0],
                                         'run_dir': m.group(1).strip() if m else None, 'time': time.time()})
            return None
        except subprocess.TimeoutExpired:
            return 'variant %s: its setup had no end after %d min (%s)' % (v['name'], minutes, work)
        except (RuntimeError, OSError) as e:
            return 'variant %s: could not read its saved projects: %s (%s)' % (v['name'], e, work)
        finally:
            self.end_emulator(proc)

    def start_key(self, unit, build, chain):
        """What a built-in start's snapshot vouches for: the unit, the emulator binary, the build
        and card it booted, and the text of the start file and of each base it builds on, with
        the files its data steps put."""
        u = UNITS[unit]
        exe = os.path.join(ANALOG_EMU or '', u.get('emu_exe') or '')
        card = u.get('emu_card')
        parts = []
        for name, path, data in chain:
            with open(path, 'rb') as fh:
                text = fh.read()
            parts.append([name, hashlib.sha256(text).hexdigest(),
                          [[op, where, sha256(f) if f else None] for op, where, f in data]])
        start = parts[0][1] if len(parts) == 1 and not parts[0][2] else parts   # one plain start: as before
        return hashlib.sha256(json.dumps({
            'unit': unit, 'emulator': sha256(exe) if os.path.isfile(exe) else None, 'build': sha256(build),
            'card': sha256(card) if card and os.path.isfile(card) else None,
            'start': start}, sort_keys=True).encode()).hexdigest()

    def start_data_step(self, s, addr, op, path, src, work):
        """One data store step of a start on the emulator at addr, through analogrun's `data`
        (the local write policy is asked, as for the data tool). -> (ok, output)."""
        script = runner()
        slug = re.sub(r'[^\w-]+', '_', path.strip('/'))[:40] or 'root'
        args = [op, path, src if op == 'put' else os.path.join(work, 'data-clear-%s-before.bin' % slug), '--yes']
        r = subprocess.run([sys.executable, script, '--port', addr, 'data'] + args,
                           cwd=os.path.dirname(os.path.dirname(script)), env=s.env(), capture_output=True,
                           text=True, encoding='utf-8', errors='replace', timeout=300, **NOWIN)
        return r.returncode == 0, '$ data %s\n%s%s' % (' '.join(args), r.stdout, r.stderr)

    def start_steps(self, s, unit, v, addr, work, minutes):
        """Build v's built-in start on the emulator at addr: for each start of its chain, base
        first, its data steps, then its own sections. -> None, or why the start was not built."""
        chain = v.get('start_chain') or start_chain(unit, v.get('start') or v['home'])
        os.makedirs(work, exist_ok=True)
        log = []
        try:
            for name, path, data in chain:
                sub = work if len(chain) == 1 else os.path.join(work, name)
                os.makedirs(sub, exist_ok=True)
                for op, where, src in data:
                    ok, text = self.start_data_step(s, addr, op, where, src, sub)
                    log.append(text)
                    if not ok:
                        last = [l for l in text.splitlines() if l.strip()][-1:] or ['failed']
                        return 'start %s was not built: data %s %s: %s (%s)' % (name, op, where, last[0].strip(), sub)
                sinfo = self.plan_info(path)
                plan = self.batch_plan(sinfo, sub, 'start %s' % name, out=sub)
                p = subprocess.run([sys.executable, RUNNER, '--port', addr, '--quiet', 'run', plan], cwd=ANALOGHEALTH,
                                   env=s.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   encoding='utf-8', errors='replace', timeout=minutes * 60, **NOWIN)
                log.append(p.stdout)
                ok, line = self.plan_outcome(p.returncode, p.stdout)
                if not ok:
                    return 'start %s was not built: %s (%s)' % (name, line, sub)
            return None
        finally:
            with open(os.path.join(work, 'start.log'), 'w', encoding='utf-8') as fh:
                fh.write('\n'.join(log))

    def batch_start(self, s, unit, v, minutes, folder):
        """Before a run from a built-in start: its snapshot, from the store when one of the same
        key is there, else built now by its start plan on a fresh emulator and saved.
        -> None, or why the run cannot go on."""
        st = v.get('start') or v['home']
        v['start_chain'] = v.get('start_chain') or start_chain(unit, st)
        key = self.start_key(unit, v['build'], v['start_chain'])
        v['start_store'] = store = os.path.join(STATE, 'snapshots', 'start-%s-%s.snap' % (st, key[:32]))
        if os.path.isfile(store) and os.path.isfile(store + '.json'):
            os.utime(store + '.json')
            return None
        work = os.path.join(folder, 'start-%s-%s' % (st, key[:8]))
        os.makedirs(work, exist_ok=True)
        live = os.path.join(work, 'start.snap')
        proc, addr, why, _ = self.batch_emulator(unit, v['build'], work, minutes, snapshot=live)
        try:
            if why:
                return 'start %s: %s (%s)' % (st, why, work)
            why = self.start_steps(s, unit, v, addr, work, minutes)
            if why:
                return why
            proc.stdin.write(b'save\n')
            proc.stdin.flush()
            for _ in range(150):
                time.sleep(0.2)
                with open(os.path.join(work, 'emulator.log'), encoding='utf-8', errors='replace') as fh:
                    if 'saved ' + live in fh.read():
                        break
            else:
                return 'start %s: the emulator did not save its snapshot (%s)' % (st, work)
            os.makedirs(os.path.dirname(store), exist_ok=True)
            shutil.copyfile(live, store + '.tmp')
            os.replace(store + '.tmp', store)
            write_json(store + '.json', {'start': st, 'plan': v['start_plan'], 'run_dir': work,
                                         'time': time.time()})
            return None
        except subprocess.TimeoutExpired:
            return 'start %s had no end after %d min (%s)' % (st, minutes, work)
        finally:
            self.end_emulator(proc)

    def batch_run(self, s, unit, flash, minutes, folder, n, v):
        """One run of a batch in its own folder: a fresh emulator of v['build'] on a port of its
        own (resuming v's setup snapshot in a snapshot batch), then the flash or plan (only its
        steps after setup when resumed), then the emulator ends. A passing run's folder goes."""
        work = os.path.join(folder, 'r%02d' % n)
        os.makedirs(work)
        snap = None
        if v.get('start_store'):                       # a built-in start, resumed
            snap = os.path.join(work, 'start.snap')
            shutil.copyfile(v['start_store'], snap)
        elif v.get('store'):
            snap = os.path.join(work, 'setup.snap')
            shutil.copyfile(v['store'], snap)
        proc, addr, why, din = self.batch_emulator(unit, v['build'], work, minutes, snapshot=snap,
                                                   din=not flash and v['info']['din'])
        out, before = None, None
        try:
            if why:
                return dict(outcome='error', line=why, folder=work)
            image = v['image'] if flash else v['info']['flash']
            if isinstance(proc, dict) and image:       # digiemu reboots from the file the flash was written from
                shutil.copyfile(image, os.path.join(proc['firmware'], os.path.basename(image)))
            if v.get('start_plan') and not v.get('start_store'):    # no snapshots: build the start here
                why = self.start_steps(s, unit, v, addr, os.path.join(work, 'start'), minutes)
                if why:
                    return dict(outcome='error', line=why, folder=work)
            if v.get('gate'):
                # a gate run's evidence goes where run_plan's would (the plan's runs folder), not
                # into the batch folder, which loses its passing runs; and, as run_plan, it fails
                # on a write to a saved project outside the plan's slots
                g = v['info']['settings'].get
                out = os.path.join(v['info']['rel'](g('out', 'runs')), 'emu-%s' % s.sid[:8],
                                   '%s-r%02d%s' % (os.path.basename(folder), n,
                                                   '-' + (v.get('start') or v['home']) if v.get('start_plan') else ''))
                if UNITS[unit]['runner'] is True:
                    try:
                        before = self.saved_projects(addr, os.path.join(work, 'projects', 'before'))
                    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                        return dict(outcome='fail', line='could not read the saved projects before the run: %s' % e,
                                    folder=work)
            if flash:
                cmd, cwd = [sys.executable, '-m', 'elkflash', 'upgrade', v['image'], *chunk_args(unit), '--port', addr, '--yes',
                            '--no-confirm', '--wait', '--quiet', '--log', os.path.join(work, 'elkflash.log'),
                            '--screenshot', os.path.join(work, 'after.png')], ELKFLASH
                if isinstance(proc, dict):
                    cmd += ['--wait-timeout', '400']     # a reboot is a cold boot and a settle
            else:
                info = v['info']
                secs = None
                if v.get('power_cut'):             # a start run of a power-cycle plan: up to its first power cycle
                    secs = power_cycle_parts(info['steps'])[0]
                if v.get('store') and snap:        # resumed after its setup
                    secs = (secs or info['steps'])[info['setup']:]
                plan = self.batch_plan(info, work, '%s run %d' % ('gate' if v.get('gate') else 'batch', n),
                                       sections=secs, out=out)
                cmd, cwd = [sys.executable, RUNNER, '--port', addr] + (['--din', din] if din else []) + [
                    '--quiet', 'run', plan], ANALOGHEALTH
            env = dict(s.env(), **({'DIGIRUN_PATH': os.path.join(DIGIHEALTH, 'tools')} if DIGIHEALTH else {}))
            try:
                p = subprocess.run(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', timeout=minutes * 60, **NOWIN)
            except subprocess.TimeoutExpired:
                return dict(outcome='hang', line='no end after %d min' % minutes, folder=work)
            with open(os.path.join(work, 'run.log'), 'w', encoding='utf-8') as fh:
                fh.write(p.stdout)
            if flash:
                out = [l.strip() for l in p.stdout.splitlines() if l.strip()]
                ok, line = p.returncode == 0, (out or ['exit %d' % p.returncode])[-1]
            elif v.get('start'):                       # passes, or stops where its contrast says
                ok, line = start_verdict(v['expect'], p.returncode, self.plan_run_dir(p.stdout))
            else:
                ok, line = self.plan_outcome(p.returncode, p.stdout)
            if ok and before is not None:
                try:
                    now = self.saved_projects(addr, os.path.join(work, 'projects', 'after'))
                    bad = project_changes(before, now, saved_slots(v['info']['steps']))
                except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                    bad = ['could not read the saved projects after the run: %s' % e]
                if bad:
                    ok, line = False, 'write outside the plan\'s slots: %s' % '; '.join(bad)
        finally:
            self.end_emulator(proc)
        run_dir = None if flash else self.plan_run_dir(p.stdout)
        if ok and v.get('start'):
            info = v['info']
            self.record_start(info['key'], v['os'], v['start'], {
                'plan': info['path'], 'expect': v['expect'], 'ok': True, 'line': line, 'run_dir': run_dir,
                'via': 'gate batch %s' % os.path.basename(folder)})
        elif ok and v.get('gate'):
            info = v['info']
            self.record_pass(info['key'], {'plan': info['path'], 'run_dir': run_dir, 'unit': unit, 'os': v['os'],
                                           'start': 'built-in start ' + v['home'] if v.get('home') else
                                                    'a setup snapshot' if snap else 'a fresh emulator',
                                           'via': 'gate batch %s' % os.path.basename(folder)})
            if info['flash_sha']:
                self.record_pass(flash_key(unit, info['flash_sha'], v['os']),
                                 {'image': info['flash'], 'unit': unit, 'run_dir': run_dir, 'over': v['os']})
        if ok:
            shutil.rmtree(work, ignore_errors=True)
            return dict(outcome='pass', line=line, run_dir=run_dir)
        return dict(outcome='fail', line=line, folder=work, run_dir=run_dir)

# ---- the person's go ----------------------------------------------------------------------

def ask_person(title, text):
    """A Yes/No dialog on this PC's desktop, No by default; True only on a click on Yes.
    A session cannot answer it: that is what makes the token the person's. Linux: zenity on the
    desktop session; with no zenity or no desktop the answer is No (approval_fallback = 'thread'
    suits a headless machine)."""
    if not WINDOWS:
        if not shutil.which('zenity') or not (os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY')):
            return False
        try:
            r = subprocess.run(['zenity', '--question', '--default-cancel', '--title', title, '--text', text,
                                '--timeout', str(DIALOG_TIMEOUT)], capture_output=True,
                               timeout=DIALOG_TIMEOUT + 30)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0
    u = C.WinDLL('user32')
    flags = 0x4 | 0x20 | 0x100 | 0x1000 | 0x10000 | 0x40000   # YESNO QUESTION DEFBUTTON2 SYSTEMMODAL SETFOREGROUND TOPMOST
    try:
        fn = u.MessageBoxTimeoutW
        fn.argtypes = [W.HWND, W.LPCWSTR, W.LPCWSTR, W.UINT, W.WORD, W.DWORD]
        rc = fn(None, text, title, flags, 0, DIALOG_TIMEOUT * 1000)
    except AttributeError:
        rc = u.MessageBoxW(None, text, title, flags)
    return rc == 6                                    # IDYES


# ---- tool list -----------------------------------------------------------------------------

S = lambda **p: {'type': 'object', 'properties': p, 'required': [k for k, v in p.items() if v.pop('req', False)]}
UNIT = lambda: {'type': 'string', 'enum': list(UNITS), 'req': True}
TARGET = lambda: {'type': 'string', 'enum': ['emulator', 'hardware'], 'req': True,
                  'description': 'emulator (this session\'s headless emulator, no claim) or hardware (needs your claim)'}
OVER = lambda: {'type': 'string', 'description': 'hardware: the OS .syx on the unit when it is flashed '
                                            '(the flash\'s emulator pass is of that transition); left out, the '
                                            'file this session flashed last'}
EMU_BUILD = lambda: {'type': 'string', 'description': 'emulator only: boot a fresh emulator of this OS '
                                                     '.syx first (replaces yours), instead of an emulator call'}
TOOLS = [
    ('claim', 'Claim units through elklock before any hardware call (prints the last handoff). '
              'Overbridge Total Recall needs all three.',
     S(unit={'type': ['string', 'array'], 'items': {'type': 'string'}, 'req': True,
             'description': 'rytm, a4, digitone, digitakt, all, or a list'},
       purpose={'type': 'string', 'req': True}, wait_minutes={'type': 'integer', 'description': '0-10, queue for a busy unit'})),
    ('release', 'Release units: writes the elklock handoff note and a test-log row in the unit\'s runs folder. '
                'Asks for the stock OS back first. Where the unit sets clear_at_release, backs up and erases '
                'those test slots first and lists them empty.',
     S(unit={'type': ['string', 'array'], 'items': {'type': 'string'}, 'req': True},
       note={'type': 'string', 'req': True, 'description': 'state left: OS, project, menus'},
       accept_non_stock={'type': 'boolean', 'description': 'leave a non-stock OS on purpose (say why in note)'},
       accept_slots_left={'type': 'boolean', 'description': 'release although the test slots could not be '
                                                             'emptied (say why in note)'})),
    ('status', 'This session\'s claims and runs, or one run (run_id): waits until that run ends '
               '(up to wait_s) and gives its verdict. Call it again if it still answers running.',
     S(run_id={'type': 'string'},
       wait_s={'type': 'number', 'description': 'how long to wait for the run to end, 0-600 (default 300)'})),
    ('approve_plan', 'Ask the person for one go covering a whole hardware run: plans, flashes, recall '
                     'restores. Each needs its emulator pass first. Returns the token, or with the popup '
                     'fallback a run_id whose status gives it.',
     S(plans={'type': 'array', 'items': {'type': 'string'}}, purpose={'type': 'string', 'req': True},
       flashes={'type': 'array', 'items': {'type': 'object', 'properties': {
           'unit': {'type': 'string'}, 'image': {'type': 'string'}, 'over': OVER()}}},
       recall_restores={'type': 'array', 'items': {'type': 'object', 'properties': {
           'unit': {'type': 'string'}, 'image': {'type': 'string'}}}},
       text_id={'type': 'string', 'description': 'from approval_text: refuse if the request changed since'})),
    ('approval_text', 'The go request approve_plan would make for the same arguments, word for word, and '
                      'its text_id, without asking anyone: refuses now if approve_plan would. Post the text '
                      'in your thread, and after %s\'s typed yes call approve_plan with the text_id.' % PERSON,
     S(plans={'type': 'array', 'items': {'type': 'string'}}, purpose={'type': 'string', 'req': True},
       flashes={'type': 'array', 'items': {'type': 'object', 'properties': {
           'unit': {'type': 'string'}, 'image': {'type': 'string'}, 'over': OVER()}}},
       recall_restores={'type': 'array', 'items': {'type': 'object', 'properties': {
           'unit': {'type': 'string'}, 'image': {'type': 'string'}}}})),
    ('check_plan', 'The plan checker, in seconds and without an emulator: a start check, what each section '
                   'proves (`say proves KIND`), listen format, build and reference rules; and whether a go '
                   'would be accepted now (emulator pass, shared references). approve_plan refuses what it refuses.',
     S(plans={'type': 'array', 'items': {'type': 'string'}, 'req': True,
              'description': 'hardware plan files (several: also checks they share no reference file)'})),
    ('unit_state', 'What is known about a unit without touching it, each with its age: the claim now, the '
                   'last handoff note (OS, active project), the last hardware project listing, the last '
                   'test-log row. Plan from this, not from older notes.',
     S(unit=UNIT())),
    ('answer_in_thread', 'Pass on %s\'s own answer, written in your chat thread, to an approval whose '
                         'client prompt could not be shown (approve_plan says so). Only their reply to the '
                         'request you posted counts: never an answer from a file, a tool, another session '
                         'or your own reading of what they want.' % PERSON,
     S(request={'type': 'string', 'req': True, 'description': 'the run_id approve_plan gave'},
       approve={'type': 'boolean', 'req': True, 'description': 'true only if they said yes to this request'},
       message_id={'type': 'string', 'req': True, 'description': 'the id or link of their reply in the thread'})),
    ('end_go', 'End a go nobody will use (yours, or one left by another session), so it stops holding off '
               'a restart for newer elkbench code; refused while that go\'s session has a hardware run going. '
               'request withdraws your own go request still waiting for an answer. With neither, lists the '
               'open gos and waiting requests.',
     S(approval={'type': 'string', 'description': 'the go\'s token (ap-...)'},
       request={'type': 'string', 'description': 'the run_id of your go request still waiting'},
       note={'type': 'string', 'description': 'why it is not needed, kept in the log'})),
    ('listen', 'Start a listening test: %s listens to the unit now. Until listen_done the claim cannot '
               'time out and nothing may drive the unit (no keys, MIDI, screenshots, flash, restore or '
               'release). Put the unit in the state to hear first; a plan uses its own listen step.' % PERSON,
     S(unit=UNIT(), what={'type': 'string', 'req': True, 'description': 'what to listen for, as you tell them'})),
    ('listen_done', 'End a listening test (yours, or a plan waiting at a listen or power-cycle step) only when '
                    '%s has said in the thread that they have finished (or that the power cycle is done): pass '
                    'the id of that reply. Without one, the client asks them.' % PERSON,
     S(unit=UNIT(), message_id={'type': 'string', 'description': 'the id or link of their "finished" reply'})),
    ('emulator', 'Start, stop or list the emulators (analog-emu for the Rytm / A4, digiemu for the '
                 'Digitone / Digitakt), always headless with their USB MIDI ports, on a fresh card copy; they end by '
                 'themselves. Start one before any target emulator call; one per unit.',
     S(action={'type': 'string', 'enum': ['start', 'stop', 'status'], 'req': True},
       unit={'type': 'string', 'enum': emulated_units()},
       build={'type': 'string', 'description': 'the OS .syx to boot (start); leave out with keep_flash'},
       minutes={'type': 'integer', 'description': 'how long it runs, 1-240 (default 60)'},
       card={'type': 'string', 'description': 'card image to copy (default: the configured one)'},
       keep_flash={'type': 'boolean', 'description': 'boot what an earlier flash left in this session\'s '
                                                     'emulator (no build), on its card as left'},
       power_off={'type': 'boolean', 'description': 'Rytm / A4: start it with the power-off save, so that a '
                                                    'plan with power-cycle steps can run on it later without '
                                                    'build (after the plans that set up its state)'})),
    ('run_plan', 'Run a test plan (analogrun run, format 1) in the background; returns a run_id. target '
                 'emulator records the pass a hardware run needs; hardware needs the claim and an approval token. '
                 'A plan\'s power-cycle steps power the emulator off (its OS saves to the card) and cold-boot it; '
                 'on the unit the run waits at each until the person has done it by hand (status says so).',
     S(plan={'type': 'string', 'req': True, 'description': 'the hardware plan file (an emulator twin is made for you)'},
       target=TARGET(), approval={'type': 'string'}, build=EMU_BUILD(),
       record_refs={'type': 'boolean', 'description': 'emulator only: save the reference screens'})),
    ('flash', 'Flash an OS .syx with elkflash (--no-confirm --wait) in the background; returns a run_id. '
              'Hardware: claim, a passing emulator flash of the same file over the same OS, and an approval token.',
     S(unit=UNIT(), image={'type': 'string', 'req': True}, target=TARGET(), approval={'type': 'string'},
       over=OVER(), build=EMU_BUILD())),
    ('batch', 'Emulator only (Rytm, A4; Digitone, Digitakt at most 3 at a time): run a plan or a flash many times across variants, each run on a '
              'fresh emulator of its own, a few at a time, round by round in shuffled order; returns a '
              'run_id. status gives progress, then a line per variant (pass count, 95% range), Fisher p '
              'against the first variant and the first failures; per-run detail stays in results.jsonl. '
              'Such a batch never counts as the emulator pass a hardware run needs. With gate: the emulator '
              'gate for several hardware plans at once: each plan once, side by side, each on a fresh emulator '
              '(resuming its setup snapshot when it has snapshot-after); a pass is recorded as run_plan\'s is. '
              'Each plan also runs from every start its go needs (the unit\'s built-in dirty and occupied, or testslot-cleared '
              'for occupied where the unit sets clear_at_release, '
              'and the starts its `say contrast` lines name), each run passing or stopping where the contrast '
              'says. Runs whose result still stands (same steps, refs, OS files, fresh start) are left out unless force.',
     S(variants={'type': 'array', 'description': '1-6 arms, the first is the one the others are compared with',
                 'items': {'type': 'object', 'properties': {
                     'name': {'type': 'string', 'description': 'short label (default A, B, ...)'},
                     'build': {'type': 'string', 'description': 'the OS .syx the emulator boots'},
                     'image': {'type': 'string', 'description': 'flash batches: the .syx to flash'},
                     'plan': {'type': 'string', 'description': 'plan batches: the plan file'}}}},
       plan={'type': 'string', 'description': 'the plan every variant runs (unless it names its own)'},
       image={'type': 'string', 'description': 'the OS .syx every variant flashes (unless it names its own)'},
       unit={'type': 'string', 'enum': emulated_units(), 'description': 'flash batches: the unit'},
       build={'type': 'string', 'description': 'the OS .syx every variant boots (unless it names its own)'},
       runs={'type': 'integer', 'description': 'runs per variant, 1-200 (default 10)'},
       parallel={'type': 'integer', 'description': 'runs at once, 1-8 (default 4; all batches on the PC share a cap)'},
       minutes={'type': 'integer', 'description': 'a run that takes longer counts as a hang (default 10 flash, 20 plan)'},
       seed={'type': 'integer', 'description': 'the run order\'s shuffle (default random, reported)'},
       snapshot={'type': 'boolean', 'description': 'plan batches with snapshot-after: every run resumes the '
                                                   'variant\'s setup snapshot (faster; every run then starts at '
                                                   'the same emulated moment, so not for timing questions). '
                                                   'Gate batches use snapshots unless this is false'},
       gate={'type': 'boolean', 'description': 'run each plan once and record its passes as emulator passes '
                                               '(the gate a hardware run needs); runs is then 1'},
       plans={'type': 'array', 'items': {'type': 'string'},
              'description': 'gate: the hardware plan files (up to %d), each run once' % GATE_PLANS_MAX},
       force={'type': 'boolean', 'description': 'gate: run plans whose pass still stands too'},
       stop={'type': 'string', 'description': 'a batch run_id: start no more of its runs'})),
    ('steps', 'analogrun steps (tap, hold, keys, turn, cc, nrpn, note, wait, stats, shot NAME, send FILE [din], '
              'capture NAME S [any|none] [din], ping, save-project, save-kit...) on a unit or its emulator, any '
              'local write policy applied; up to 2 min. din: over the unit\'s DIN MIDI (Rytm, A4; Digitone/Digitakt with a digirun.py that has --din; '
              'hardware: units.X.din in config).',
     S(unit=UNIT(), target=TARGET(), steps={'type': 'string', 'req': True})),
    ('shot', 'Screenshot into this session\'s evidence folder.',
     S(unit=UNIT(), target=TARGET(), name={'type': 'string', 'description': 'bare .png or .bin name'})),
    ('expect_screen', 'Compare the screen with a reference .bin (default folder: the unit\'s plans\\refs), '
                      'retrying up to wait_s; record true saves it (emulator only).',
     S(unit=UNIT(), target=TARGET(), ref={'type': 'string', 'req': True}, wait_s={'type': 'number'},
       record={'type': 'boolean'})),
    ('data', 'The data store (analogrun / digirun data): ls PATH (\'/\' gives the roots), get PATH (one object '
             'as Transfer stores it, into the evidence folder); put FILE into PATH, clear PATH, roundtrip PATH '
             '(read, write back or to `to`, read back, compare). Writes ask the local write policy first and '
             'keep a copy of what they replace; hardware needs the claim.',
     S(unit=UNIT(), target=TARGET(), op={'type': 'string', 'enum': list(DATA_OPS), 'req': True},
       path={'type': 'string', 'req': True}, name={'type': 'string', 'description': 'ls/get: bare file name for the result'},
       file={'type': 'string', 'description': 'put: the object file (from get)'},
       to={'type': 'string', 'description': 'roundtrip: write to this slot instead of path'},
       slot_name={'type': 'string', 'description': 'put/roundtrip: the name the slot gets (a policy may require one)'})),
    ('audio_record', 'Record every ASIO input of the unit (optionally with a 997 Hz tone into it) into the '
                     'evidence folder; hardware needs the claim. target emulator (Digitone): the emulator\'s '
                     'twelve Overbridge channels, no tone.',
     S(unit=UNIT(), seconds={'type': 'number', 'req': True}, name={'type': 'string'}, tone={'type': 'boolean'},
       target={'type': 'string', 'enum': ['hardware', 'emulator'],
               'description': 'hardware (default) or emulator (this session\'s Digitone emulator)'})),
    ('audio_check', 'audiocheck.py dropouts or compare on recordings (offline, no claim): one verdict line.',
     S(op={'type': 'string', 'enum': ['dropouts', 'compare'], 'req': True}, wav={'type': 'string', 'req': True},
       ref={'type': 'string'}, channels={'type': 'string', 'description': 'e.g. 1,2'},
       folder={'type': 'string', 'description': 'where relative names are'})),
    ('recall_take', 'Overbridge Total Recall: take the unit\'s project image (obtr.py) into the evidence '
                    'folder. Needs claims on all three units; never restarts Overbridge.',
     S(unit=UNIT(), name={'type': 'string'})),
    ('recall_restore', 'Overbridge Total Recall restore of an image from recall_take (replaces the active '
                       'project). Needs all three claims and an approval token.',
     S(unit=UNIT(), image={'type': 'string', 'req': True}, approval={'type': 'string'})),
]


def tidy_call(name, args):
    """Accept a call whose tool or argument name, or enum value, differs only in letter case
    (`Rytm`, `Target`); anything else unknown is refused with the names expected."""
    tools = {n.lower(): (n, sch) for n, _, sch in TOOLS}
    if str(name).lower() not in tools:
        raise Refused('no tool %r (tools: %s)' % (name, ', '.join(n for n, _, _ in TOOLS)))
    name, sch = tools[str(name).lower()]
    props = sch['properties']
    keys = {k.lower(): k for k in props}
    out = {}
    for k, v in args.items():
        if k.startswith('_'):                     # the launcher's own (_elicit)
            out[k] = v
            continue
        if k.lower() not in keys:
            raise Refused('%s takes no argument %r (it takes: %s)' % (name, k, ', '.join(props)))
        k = keys[k.lower()]
        if k == 'unit':                           # an enum, or free text on claim/release
            v = v.lower() if isinstance(v, str) else [u.lower() for u in v] if isinstance(v, list) else v
        enum = props[k].get('enum')
        if enum and isinstance(v, str) and v not in enum:
            match = [e for e in enum if e.lower() == v.lower()]
            if not match:
                raise Refused('%s %r is not one of %s' % (k, v, ', '.join(enum)))
            v = match[0]
        out[k] = v
    return name, out


# ---- the socket server -------------------------------------------------------------------

D = None
SECRET = None
STARTED = time.time()                     # main() sets it again
GATE = threading.Lock()                   # one hello at a time decides on a restart
CODE = [os.path.abspath(f) for f in (__file__, batch.__file__, plancheck.__file__, elklock.__file__)]


class Handler(socketserver.StreamRequestHandler):
    def handle(self):
        with D.lock:
            D.connections += 1
        try:
            hello = json.loads(self.rfile.readline() or b'{}')
            if hello.get('hello') != SECRET or not hello.get('session'):
                return
            with GATE:
                t = None if D.restarting else D.newer_code()
                if D.restarting or t and not D.restart_waits_for() and not D.code_starts(t):
                    if not D.restarting:
                        D.restarting = True
                        D.save_jobs()
                        D.log_line({'session': 'daemon', 'daemon': 'restart', 'pid': os.getpid(),
                                    'reason': 'newer code from %s' % stamp(t)})
                        threading.Thread(target=SERVER.shutdown, daemon=True).start()
                    self.reply({'restart': True})     # newer code on disk: let the launcher start it
                    return
            self.reply({'ok': True, 'version': VERSION})
            s = D.session(hello)
            for raw in self.rfile:
                req = json.loads(raw)
                D.last_use = time.time()
                if req.get('method') == 'list':
                    self.reply({'id': req.get('id'), 'tools': [
                        {'name': n, 'description': d, 'inputSchema': sch} for n, d, sch in TOOLS]})
                    continue
                name, args = req.get('tool'), req.get('args') or {}
                t0, err, extra = time.time(), False, {}
                try:
                    if req.get('method') == 'answer':          # the person's answer, via the launcher
                        name, args = '(approval answer)', {k: req.get(k) for k in ('request', 'accept', 'fallback')}
                        lines = D.answer(s, req)
                    else:
                        name, args = tidy_call(name, args)
                        gave_back = D.leave_hardware(s, name, args)
                        try:
                            lines = getattr(D, 't_' + name)(s, args)
                        except Refused as e:
                            raise Refused('; '.join([str(e)] + gave_back)) if gave_back else e
                        if gave_back and isinstance(lines, list):
                            lines = lines + gave_back
                    if isinstance(lines, dict):                 # approve_plan asking in the client
                        extra, lines = {'elicit': lines['elicit']}, lines['lines']
                except Refused as e:
                    lines, err = ['refused: %s' % e], True
                except subprocess.TimeoutExpired as e:
                    lines, err = ['fail: timed out after %s s' % e.timeout], True
                except Exception as e:            # noqa: BLE001 - one line back, detail in the call log
                    lines, err = ['fail: %s: %s' % (type(e).__name__, e)], True
                D.log_line({'session': s.sid, 'tool': name, 'args': args, 'error': err,
                            'ms': int(1000 * (time.time() - t0)), 'lines': lines[:3]})
                self.reply(dict({'id': req.get('id'), 'text': '\n'.join(lines), 'error': err}, **extra))
        finally:
            with D.lock:
                D.connections -= 1
            D.last_use = time.time()

    def reply(self, obj):
        self.wfile.write((json.dumps(obj) + '\n').encode())
        self.wfile.flush()


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = False


def idle_watch():
    while True:
        time.sleep(30)
        if D.connections == 0 and not D.running() and not D.dialog.locked() \
                and time.time() - D.last_use > IDLE_EXIT:
            SERVER.shutdown()
            return


class Awake:
    """Holds off system sleep (not the display's) while set. Windows: the execution state of the
    thread that calls it; Linux: a systemd-inhibit child, if there is one. Elsewhere it does nothing."""
    def __init__(self):
        self.on, self.proc = False, None

    def set(self, on):
        if on == self.on:
            return
        self.on = on
        if WINDOWS:
            C.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x1 if on else 0))  # CONTINUOUS | SYSTEM_REQUIRED
        elif on and shutil.which('systemd-inhibit'):
            try:
                # the child waits for its stdin to close, so the inhibit ends with this daemon too
                self.proc = subprocess.Popen(['systemd-inhibit', '--what=sleep:idle', '--who=elkbench',
                                              '--why=a test run or claim is going', sys.executable, '-c',
                                              'import sys; sys.stdin.read()'], stdin=subprocess.PIPE,
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except OSError:
                self.proc = None
        elif not on and self.proc:
            try:
                self.proc.stdin.close()
                self.proc.wait(10)
            except (OSError, subprocess.TimeoutExpired):
                self.proc.kill()          # a stuck child must not end the watch: the inhibit goes with it
            self.proc = None


def awake_watch():
    """Re-checks every 15 s, from this one thread (the Windows state is per thread)."""
    a = Awake()
    while True:
        try:
            why = D.keeps_awake()
        except Exception:                 # elklock unreadable for a moment: keep the last state
            why = None
        if why is not None and bool(why) != a.on:
            a.set(bool(why))
            D.log_line({'session': 'daemon', 'daemon': 'keeps the PC awake' if why else 'lets the PC sleep',
                        'reason': '; '.join(why)})
        time.sleep(15)


def main():
    global D, SECRET, SERVER, STARTED
    if sys.argv[1:] == ['--check']:
        return 0                          # it imported and read the config: newer code can start
    os.makedirs(STATE, exist_ok=True)
    h = elklock.open_exclusive(os.path.join(STATE, 'daemon.lock'))
    if h is None:
        return 3                          # one daemon per PC: another one runs
    STARTED = time.time()
    D, SECRET = Daemon(), secrets.token_hex(16)
    SERVER = Server(('127.0.0.1', 0), Handler)
    write_json(os.path.join(STATE, 'daemon.json'), {'port': SERVER.server_address[1], 'secret': SECRET,
                                                    'pid': os.getpid(), 'version': VERSION,
                                                    'started': STARTED})
    D.log_line({'session': 'daemon', 'daemon': 'started', 'pid': os.getpid(), 'version': VERSION,
                'approvals_kept': len(D.open_goes())})
    threading.Thread(target=idle_watch, daemon=True).start()
    if CFG['keep_awake']:
        threading.Thread(target=awake_watch, daemon=True).start()
    try:
        SERVER.serve_forever(poll_interval=0.5)
    finally:
        try:
            os.remove(os.path.join(STATE, 'daemon.json'))
        except OSError:
            pass
        elklock.k32.CloseHandle(h)
    return 0


if __name__ == '__main__':
    sys.exit(main())
