"""elkbench daemon: the one owner of the Elektron units' test toolchain on this PC.

Every MCP client session reaches it through its own small launcher (launcher.py, an MCP stdio
server); the daemon runs once per PC, so one process owns each MIDI port, the Overbridge
connection and the audio interface at a time. It wraps the existing tools without changing
them: analogrun.py / testplan.py (analoghealth), elkflash, the bundled audio scripts (audio/:
asio_*, audiocheck.py), obtr.py and elklock.

Rules it enforces:
- every unit-touching tool refuses unless the calling session holds the unit's elklock claim
  (Overbridge Total Recall: every unit, since the Engine is shared); emulator and offline
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
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import batch

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
}
DATA_OPS = ('ls', 'get', 'put', 'clear', 'roundtrip')
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
        units[name] = u
    # analoghealth's local write policy (its writepolicy.py): ANALOGHEALTH_POLICY, else policy.py
    # in its config folder
    policy = os.environ.get('ANALOGHEALTH_POLICY') if 'ANALOGHEALTH_POLICY' in os.environ else \
        os.path.join(os.environ.get('LOCALAPPDATA', '') if WINDOWS else
                     os.path.join(os.path.expanduser('~'), '.config'), 'analoghealth', 'policy.py')
    return dict(
        person=c.get('person') or 'the person at this PC',
        elklock=t.get('elklock') or os.path.join(HERE, 'elklock', 'elklock.py'),
        analoghealth=t.get('analoghealth'),
        analog_emu=t.get('analog_emu'), elkflash=t.get('elkflash'), usb_audio=t.get('usb_audio') or os.path.join(HERE, 'audio'),
        obtr=os.path.join(HERE, 'obtr.py'), audio_python=t.get('audio_python') or [sys.executable],
        approval_fallback=c.get('approval_fallback') or 'popup',
        batch_slots=int(c.get('batch_max_emulators') or batch.SLOTS_DEFAULT),
        write_policy=(policy or '(ANALOGHEALTH_POLICY is empty)') if c.get('require_write_policy') else None,
        keep_awake=c.get('keep_awake', True) is not False,
        units=units)


CFG = load_config()
UNITS = CFG['units']
PERSON = CFG['person']
FALLBACK = CFG['approval_fallback']       # when the client cannot show the prompt: popup, thread or both
if FALLBACK not in ('popup', 'thread', 'both'):
    raise SystemExit('config.toml: approval_fallback must be popup, thread or both, not %r' % FALLBACK)
ELKLOCK = CFG['elklock']
ANALOGHEALTH = CFG['analoghealth']
RUNNER = os.path.join(ANALOGHEALTH, 'tools', 'analogrun.py') if ANALOGHEALTH else None
ANALOG_EMU = CFG['analog_emu']            # folder with analog-emu's rytm.exe and a4.exe
ELKFLASH = CFG['elkflash']                # a checkout to run `-m elkflash` in, or None: installed
USB_AUDIO = CFG['usb_audio']
OBTR = CFG['obtr']
AUDIO_PY = CFG['audio_python']            # a Python with numpy + scipy
WRITE_POLICY = CFG['write_policy']      # must exist before hardware calls, or None
batch.set_slots(CFG['batch_slots'])      # emulators all batches on this PC may run at once

sys.path[:0] = [os.path.dirname(ELKLOCK)] + ([os.path.join(ANALOGHEALTH, 'tools')] if ANALOGHEALTH else [])
import elklock    # noqa: E402
testplan = None


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
        self.ports = {}                   # (unit, target) -> threading.Lock: one user per port
        self.emulators = {}               # (session, unit) -> dict(proc, session, build, address)
        self.dialog = threading.Lock()
        self.passes_path = os.path.join(STATE, 'emulator-passes.json')
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
                                         threading.Lock())

    def busy_port(self, unit, target):
        lk = self.port_lock(unit, target)
        if not lk.acquire(blocking=False):
            running = [j.id + ' ' + j.desc for j in self.jobs.values()
                       if j.state == 'running' and j.unit == unit and
                       (j.target == 'emulator') == (target == 'emulator')]
            raise Refused('the %s %s port is in use (%s); try again when it ends'
                          % (unit, target, ', '.join(running) or 'a short call'))
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
        have = UNITS[name]['runner']     # True: analogrun (plans too)
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

    def emu_ready(self, s, unit, build, snapshot=None):
        """-> (address, lines): with `build`, a fresh emulator of this session boots it first
        (any it runs is stopped), saving the separate emulator calls; else the running one."""
        if not build:
            return self.emu_port(s, unit), []
        mine = self.emulators.get((s.sid, unit))
        if mine and mine['proc'].poll() is None:
            self.stop_emulator(mine)
        lines = self.t_emulator(s, {'action': 'start', 'unit': unit, 'build': build}, snapshot)
        addr = self.emu_port(s, unit)
        # analog-emu's port listens before the OS boots: a flash sent at once got no reply
        # to its first command. Wait until the OS answers Ping.
        for _ in range(30):
            try:
                r = subprocess.run([sys.executable, '-m', 'elkflash', 'info', '--port', addr,
                                    '--timeout', '1'], cwd=ELKFLASH, capture_output=True,
                                   timeout=10, **NOWIN)
                if r.returncode == 0:
                    break
            except subprocess.TimeoutExpired:
                pass
            time.sleep(1)
        return addr, lines[:1]

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

    # -- plans --

    def plan_info(self, path):
        """-> dict(settings, steps, unit, key, flash, flash_sha, refs) for a plan file. The key
        names what an emulator pass vouches for: unit, steps, reference screens, OS file."""
        runner()
        path = os.path.abspath(path)
        if not os.path.isfile(path):
            raise Refused('no plan file %s' % path)
        try:
            settings, steps = testplan.parse(path)
        except (ValueError, OSError) as e:
            raise Refused('plan does not parse: %s' % e)
        fmt = settings.get('format', '1')
        if fmt not in PLAN_FORMATS:
            raise Refused('plan format %s; this server reads format %s' % (fmt, ', '.join(PLAN_FORMATS)))
        here = os.path.dirname(path)
        rel = lambda p: os.path.normpath(p if os.path.isabs(p) else os.path.join(here, p))
        unit = settings.get('unit', 'rytm')
        self.unit(unit, need_runner='plan')
        din = din_steps([l for _, lines in steps for l in lines])
        if din and UNITS[unit]['runner'] is not True:
            raise Refused('%s: DIN steps run on the Rytm and A4 only so far' % din[0])
        flash = rel(settings['flash']) if settings.get('flash') else None
        if flash and not os.path.isfile(flash):
            raise Refused('the plan flashes %s, which does not exist' % flash)
        flash_sha = sha256(flash) if flash else None
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
        vouched = {'unit': unit, 'steps': steps, 'flash': flash_sha, 'refs': ref_sums}
        if send_sums:                 # left out otherwise, so earlier passes still count
            vouched['sends'] = send_sums
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
        return dict(path=path, here=here, settings=settings, steps=steps, unit=unit, key=key,
                    flash=flash, flash_sha=flash_sha, refs=refs, writes=writes, setup=setup, din=bool(din),
                    name=settings.get('name') or os.path.splitext(os.path.basename(path))[0],
                    emulator=settings.get('emulator', 'no').lower() == 'yes', rel=rel)

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

    def twin(self, info, record_refs, sid, emulator_run=None, sections=None, tag=''):
        """An emulator twin of a hardware plan, with every path setting made absolute. Its runs
        go in a folder of the session's own (emulator runs of several sessions may start in the
        same second, and testplan names a run folder by the second). With emulator_run, a
        hardware twin instead: the plan as written, plus the `emulator-run` its flash needs (the
        recorded emulator pass), so nobody has to add that line by hand."""
        g, rel = info['settings'].get, info['rel']
        if emulator_run:
            lines = ['# written by elkbench for a hardware run of %s' % info['path'],
                     'include = %s' % info['path'], 'emulator-run = %s' % emulator_run,
                     'refs = %s' % info['refs'], 'out = %s' % rel(g('out', 'runs'))]
        else:
            lines = ['# written by elkbench for an emulator run of %s' % info['path'],
                     'include = %s' % info['path'], 'emulator = yes',
                     'port = %s' % UNITS[info['unit']]['emu'],
                     'din =',                  # the unit's interface is not the emulator's
                     'record-refs = %s' % ('yes' if record_refs else 'no'),
                     'refs = %s' % info['refs'],
                     'out = %s' % os.path.join(rel(g('out', 'runs')), 'emu-%s' % sid[:8])]
        for k in ('flash', 'audio-tools', 'elkflash'):
            if g(k):
                lines.append('%s = %s' % (k, rel(g(k))))
        if sections is not None:                       # only these sections (setup / the rest)
            lines.append('name = %s%s' % (info['name'], tag))
            for name, body in sections:
                lines += ['', '[%s]' % name] + body
        os.makedirs(os.path.join(STATE, 'twins'), exist_ok=True)
        p = os.path.join(STATE, 'twins', '%s-%s-%s%s%s.txt' % (
            re.sub(r'\W+', '-', info['name']), info['key'][:8], sid[:8], '-hw' if emulator_run else '',
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
                what = read_json(job.listen_file, {}).get('what') or '(see the plan)'
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
            goes = [a for a in self.approvals.values()
                    if time.time() < a['expires'] and not all(it['used'] for it in a['items'])]
            if goes:
                why.append('a go is open (until %s at the latest)' % stamp(max(a['expires'] for a in goes)))
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
        out = ['ok: released %s' % ', '.join(mine)]
        for u in mine:
            done = s.done.get(u, [])
            os_text = ('%s (%s)' % (s.os_left[u][0], 'stock' if s.os_left[u][1] else 'NOT STOCK')
                       if u in s.os_left else 'not flashed by this session')
            full = '%s; runs: %s; OS: %s' % (note, '; '.join(done) or 'none', os_text)
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

    def t_approve_plan(self, s, a):
        items, text = [], []
        for p in a.get('plans') or []:
            info = self.plan_info(p)
            if info['emulator']:
                raise Refused('%s is an emulator plan; approve the hardware plan' % p)
            ep = self.find_pass(info['key'], info['flash_sha'])
            if not ep:
                raise Refused('%s has no passing emulator run of the same steps, refs and OS file: '
                              'run_plan target emulator first' % info['name'])
            items.append(dict(item=('plan', info['key']), label='plan ' + info['name'], used=False))
            hand = hand_steps(info['steps'])
            text.append('PLAN %s on %s\n  %s\n  steps: %s%s%s\n  emulator pass: %s%s' % (
                info['name'], info['unit'], info['path'], ', '.join(n for n, _ in info['steps']),
                '\n  FLASH %s\n    sha256 %s%s' % (os.path.basename(info['flash']), info['flash_sha'],
                                                   ' (stock)' if self.is_stock(info['unit'], info['flash_sha'])
                                                   else '') if info['flash'] else '',
                '\n  writes: ' + '; '.join(info['writes']) if info['writes'] else '',
                ep.get('run_dir') or stamp(ep['time']),
                ''.join('\n  NOT TESTED IN THE EMULATOR: ' + h for h in hand)))
        for kind in ('flashes', 'recall_restores'):
            for f in a.get(kind) or []:
                self.unit(f.get('unit'))
                img = os.path.abspath(f.get('image', ''))
                if not os.path.isfile(img):
                    raise Refused('no file %s' % img)
                d = sha256(img)
                if kind == 'flashes':
                    if not self.find_pass('flash:%s:%s' % (f['unit'], d)):
                        raise Refused('%s has not flashed and booted in the %s emulator: flash target '
                                      'emulator first' % (os.path.basename(img), f['unit']))
                    items.append(dict(item=('flash', f['unit'], d), used=False,
                                      label='flash %s on %s' % (os.path.basename(img), f['unit'])))
                    text.append('FLASH %s on %s\n  sha256 %s%s' % (
                        os.path.basename(img), f['unit'], d, ' (stock)' if self.is_stock(f['unit'], d) else ''))
                else:
                    items.append(dict(item=('restore', f['unit'], d), used=False,
                                      label='recall restore %s on %s' % (os.path.basename(img), f['unit'])))
                    text.append('TOTAL RECALL RESTORE %s on %s (replaces the active project)\n  sha256 %s'
                                % (os.path.basename(img), f['unit'], d))
        if not items:
            raise Refused('nothing to approve: give plans, flashes or recall_restores')
        # Ask first, claim after the yes (owner, 2026-10-24): a unit held while the person
        # decides is idle and blocks everyone queued for it. Held only for a run that is going
        # or a listening test, it may stay.
        idle = [u for u in UNITS if elklock.mine(elklock.holder(u), session=s.sid)
                and not (elklock.holder(u) or {}).get('listening')
                and not any(j.sess is s and j.unit == u and j.target == 'hardware' for j in self.running())]
        if idle:
            raise Refused('release %s first: ask for the go before claiming, then claim after the yes '
                          '(your go lasts %d h) and check the unit\'s state before writing'
                          % (', '.join(idle), APPROVAL_TTL // 3600))
        purpose = (a.get('purpose') or '').strip() or '(no purpose given)'
        body = ('An AI session asks for one go for this hardware run.\n\nSession %s (%s)\n'
                'Purpose: %s\n\n%s\n\nEach item runs once; the go expires in %d h.\n'
                'Approve?' % (s.sid[:8], s.cwd, purpose, '\n\n'.join(text), APPROVAL_TTL // 3600))
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

    def t_emulator(self, s, a, snapshot=None):
        """Start, stop or list the emulators: always headless (no window) and each session its
        own, its USB MIDI port on a TCP port of its own (analog-emu --usb-midi-tcp), so emulators need no loopMIDI and never queue for one another."""
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
        if not u.get('emu_exe'):
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
        proc = subprocess.Popen(cmd, cwd=work, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.PIPE if snapshot else None, **NOWIN)
        self.emulators[(s.sid, a['unit'])] = dict(proc=proc, session=s.sid, build=label, sha=sha,
                                                 address='tcp:127.0.0.1:%d' % tcp, flashed=flashed,
                                                 snapshot=snapshot, log=log_path,
                                                 din='tcp:127.0.0.1:%d' % din if din else None)
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
        e['proc'].terminate()
        try:
            e['proc'].wait(10)
        except subprocess.TimeoutExpired:
            e['proc'].kill()

    def setup_snapshot(self, s, info, a):
        """-> dict(key, store, live, have) when this emulator run can use a setup snapshot: a
        hardware plan with snapshot-after, booted from `build`, no flash in the plan, not
        recording references. `live` is the emulator's own copy (a stored one copied in to
        resume, or where `save` writes a new one)."""
        if not (info['setup'] and a.get('build') and not info['emulator'] and not info['flash']
                and not a.get('record_refs') and UNITS[info['unit']].get('emu_exe')):
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

    def emu_has_din(self, unit):
        """Whether this analog-emu build serves its DIN MIDI over TCP (--din-midi-tcp)."""
        return '--din-midi-tcp' in self.emu_help(unit)

    def din_args(self, s, unit, tgt, lines):
        """['--din', PORT] for analogrun when `lines` use the unit's DIN MIDI (send/capture ...
        din), else []: on hardware the interface named in config (units.<unit>.din), which must be
        connected; in the emulator, this session's emulator's DIN MIDI over TCP."""
        if not din_steps(lines):
            return []
        if UNITS[unit]['runner'] is not True:
            raise Refused('DIN steps (send/capture ... din) run on the Rytm and A4 only so far')
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
        the project that was active)."""
        stages = list(cmd) if cmd and (isinstance(cmd[0], (list, tuple)) or callable(cmd[0])) else [cmd]
        seen = {}
        allowed = saved_slots(info['steps'])

        def before(rc, out):
            try:
                seen['before'] = self.saved_projects(addr, os.path.join(folder, 'before'))
            except (RuntimeError, OSError, subprocess.SubprocessError) as e:
                return 1, 'fail: could not read the saved projects before the run: %s\n' % e
            return 0, ''

        def after(rc, out):
            try:
                now = self.saved_projects(addr, os.path.join(folder, 'after'))
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

    def snapshot_stages(self, s, info, snap, addr, started, din=()):
        """The runner stages of a snapshot run. Resuming: the steps after setup only. Fresh: the
        setup steps; when they pass, the emulator saves the snapshot (kept for later runs with
        the run that proved it); then the rest on the same emulator. A setup failure ends the
        run with the setup's own report, and no snapshot is kept."""
        cut = info['setup']
        run = lambda plan: [sys.executable, RUNNER, '--port', addr] + list(din) + ['--quiet', 'run', plan]
        rest = run(self.twin(info, False, s.sid, sections=info['steps'][cut:], tag=' after setup'))
        if snap['have']:
            meta = read_json(snap['store'] + '.json', {})
            started.append('resumed a setup snapshot: %s passed in %s' % (
                info['steps'][cut - 1][0], meta.get('run_dir')))
            return [rest]
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
        return [setup_then_save, rest]

    def t_run_plan(self, s, a):
        tgt = self.target(a.get('target'))
        info = self.plan_info(a.get('plan', ''))
        unit = info['unit']
        if tgt == 'hardware':
            if info['emulator']:
                raise Refused('this is an emulator plan; give the hardware plan')
            if a.get('record_refs'):
                raise Refused('references are recorded in the emulator only')
            self.check_hardware(s, [unit])
            ep = self.find_pass(info['key'], info['flash_sha'])
            if not ep:
                raise Refused('no passing emulator run of these steps, refs and OS file: run_plan '
                              'target emulator first')
            self.check_connected(unit, tgt)
            din = self.din_args(s, unit, tgt, [l for _, ls in info['steps'] for l in ls])
            item = ('plan', info['key'])
            self.use_approval(s, a.get('approval'), item, 'plan ' + info['name'])
            try:
                port = self.hold_din(din, self.busy_port(unit, tgt))
            except Refused:
                self.give_back(a.get('approval'), item)
                raise
            # A plan that flashes needs `emulator-run` for testplan's gate: the recorded pass.
            plan = self.twin(info, False, s.sid, emulator_run=ep['run_dir']) \
                if info['flash'] and ep.get('run_dir') else info['path']
            cmd = [sys.executable, ELKLOCK, 'run', unit, '--', sys.executable, RUNNER] + din + [
                '--quiet', 'run', plan]
            started = []
        else:
            if info['din'] and UNITS[unit].get('emu_exe') and not self.emu_has_din(unit):
                raise Refused(self.no_emu_din(unit))
            snap = self.setup_snapshot(s, info, a)
            addr, started = self.emu_ready(s, unit, a.get('build'), snap and snap['live'])
            din = self.din_args(s, unit, tgt, [l for _, ls in info['steps'] for l in ls])
            port = self.busy_port('%s@%s' % (unit, addr), tgt)
            if snap:
                cmd = self.snapshot_stages(s, info, snap, addr, started, din)
            else:
                plan = info['path'] if info['emulator'] else self.twin(info, bool(a.get('record_refs')), s.sid)
                cmd = [sys.executable, RUNNER, '--port', addr] + din + ['--quiet', 'run', plan]
            if UNITS[unit]['runner'] is True:          # analogrun's data store: Rytm and A4
                cmd = self.guard_projects(info, addr, cmd, os.path.join(
                    s.folder(unit), 'projects-%s' % time.strftime('%H%M%S')))
        job = Job(s, 'plan', unit, tgt, 'plan ' + info['name'])
        record = not info['emulator'] or info['settings'].get('record-refs', 'no') != 'yes'
        record = record and not a.get('record_refs')

        def done(job, rc, out):
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            m = next((re.search(r' - (.+)$', l) for l in lines if re.search(r': (PASS|FAIL) \(', l)), None)
            run_dir = m.group(1).strip() if m else None
            summ = read_json(os.path.join(run_dir, 'summary.json'), {}) if run_dir else {}
            ok = rc == 0 and summ.get('pass')
            job.state = 'pass' if ok else 'fail'
            kept = [l for l in lines if not l.startswith(('claimed', 'already yours', 'last handoff',
                                                          'released', 'not yours', '  os:', 'os:'))]
            # Verdict line, then only what did not pass: a cap on all lines once cut off the
            # FAIL line of a long plan. Passing steps become a count; the first failure's
            # screen-diff picture (+ lit only now, - lit only in the reference) stays, up to
            # 12 rows, as reading it costs less than opening the diff PNG.
            if run_dir and kept and kept[0].endswith(' - ' + run_dir):
                kept[0] = kept[0][:-len(' - ' + run_dir)]        # the evidence line names it
            oks = sum(l.startswith('ok ') for l in kept[1:])
            art = lambda l: re.fullmatch(r'[-+#.]+( \(saved .*)?', l)
            rest = [l for l in kept[1:] if not l.startswith('ok ') and not art(l)]
            pic = [l for l in kept[1:] if art(l)]
            pic = pic[:12] + (['(%d more rows)' % (len(pic) - 12)] if len(pic) > 12 else [])
            job.lines = kept[:1] + (['%d step(s) ok' % oks] if oks else []) + rest[:12] + pic
            job.lines.append('evidence: %s' % (run_dir or job.log))
            if run_dir:
                job.lines.append('log: %s' % os.path.join(run_dir, 'log.txt'))
                shots = run_files(run_dir)
                if shots:
                    job.lines.append('shots: ' + ', '.join(shots))
            if tgt == 'emulator':
                job.lines += ['not tested here (skipped in the emulator): ' + h for h in hand_steps(info['steps'])]
            verdict = '%s %s (%s)' % (info['name'], 'PASS' if ok else 'FAIL', run_dir or job.log)
            if tgt == 'hardware':
                s.done.setdefault(unit, []).append('plan ' + verdict)
                if summ.get('flash_sha256'):
                    s.os_left[unit] = (os.path.basename(info['flash']),
                                       self.is_stock(unit, summ['flash_sha256']), summ['flash_sha256'])
            elif ok and record:
                self.record_pass(info['key'], {'plan': info['path'], 'run_dir': run_dir, 'unit': unit})
                if info['flash_sha']:
                    self.record_pass('flash:%s:%s' % (unit, info['flash_sha']),
                                     {'image': info['flash'], 'unit': unit, 'run_dir': run_dir})
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
        folder = s.folder(a['unit'])
        stamp = time.strftime('%H%M%S')
        tail = ['--yes', '--no-confirm', '--wait', '--quiet', '--log', os.path.join(folder, 'elkflash.log'),
                '--screenshot', os.path.join(folder, 'after-flash-%s.png' % stamp)]
        base = [sys.executable, '-m', 'elkflash', 'upgrade', img]
        if tgt == 'hardware':
            self.check_hardware(s, [a['unit']])
            if not self.find_pass('flash:%s:%s' % (a['unit'], d)):
                raise Refused('this file has not flashed and booted in the %s emulator: flash target '
                              'emulator first' % a['unit'])
            self.check_connected(a['unit'], tgt)
            item = ('flash', a['unit'], d)
            self.use_approval(s, a.get('approval'), item, 'flash %s' % os.path.basename(img))
            try:
                port = self.busy_port(a['unit'], tgt)
            except Refused:
                self.give_back(a.get('approval'), item)
                raise
            cmd = [sys.executable, ELKLOCK, 'run', a['unit'], '--'] + base + ['--port', u['port']] + tail
        else:
            addr, started = self.emu_ready(s, a['unit'], a.get('build'))
            port = self.busy_port('%s@%s' % (a['unit'], addr), tgt)
            if addr.startswith('tcp:'):
                cmd = base + ['--port', addr] + tail
            else:
                i, o = addr.split(',')
                cmd = base + ['--in-port', i, '--out-port', o] + tail
        job = Job(s, 'flash', a['unit'], tgt, 'flash ' + os.path.basename(img))
        flog = os.path.join(folder, 'elkflash.log')
        at = os.path.getsize(flog) if os.path.exists(flog) else 0     # elkflash appends to it

        def done(job, rc, out):
            lines = [l.strip() for l in out.splitlines() if l.strip()]
            job.state = 'pass' if rc == 0 else 'fail'
            # --quiet prints only the last line; the lines that say what happened are in the log
            try:
                with open(flog, encoding='utf-8', errors='replace') as fh:
                    fh.seek(at)
                    said = [l.strip() for l in fh.read().replace('\r', '\n').splitlines()
                            if re.match(r'\s*(sent \d|Committed|back after|note:|Press YES|SUCCESS)', l)]
            except OSError:
                said = []
            job.lines = [l for l in said if not any(l in x for x in lines[-3:])] + lines[-3:] + ['log: %s' % flog]
            if tgt == 'hardware':
                if rc == 0:
                    s.os_left[a['unit']] = (os.path.basename(img), self.is_stock(a['unit'], d), d)
                else:
                    s.os_left[a['unit']] = ('unknown after a failed flash of %s' % os.path.basename(img), False)
                s.done.setdefault(a['unit'], []).append('flash %s %s' % (os.path.basename(img), job.state.upper()))
            elif rc == 0:
                self.record_pass('flash:%s:%s' % (a['unit'], d), {'image': img, 'unit': a['unit']})
                job.lines.append('recorded as the emulator pass for this file')
                e = self.emulators.get((s.sid, a['unit'])) or {}
                if e.get('flashed'):                  # a restart with keep_flash boots this
                    write_json(e['flashed'], {'image': os.path.basename(img), 'sha': d})
                    e['build'] = os.path.basename(img)
                e['sha'] = d
            else:
                # the next emulator start overwrites its log: keep this one with the evidence
                e = self.emulators.get((s.sid, a['unit'])) or {}
                src = os.path.join(os.path.dirname(e['flashed']), 'emulator.log') if e.get('flashed') else None
                if src and os.path.isfile(src):
                    kept = os.path.join(folder, 'emulator-flash-%s.log' % stamp)
                    shutil.copyfile(src, kept)
                    job.lines.append('emulator log: %s' % kept)

        return (started if tgt == 'emulator' else []) + self.start(job, cmd, ELKFLASH, done, port)

    def device_do(self, s, unit, tgt, steps, timeout=180, need='steps'):
        """analogrun do STEPS on the unit or its emulator -> (ok, one line, folder)."""
        self.unit(unit, need_runner=need)
        runner()
        if tgt == 'hardware':
            self.check_hardware(s, [unit])
            self.check_connected(unit, tgt)
            where = UNITS[unit]['port']
            din = self.din_args(s, unit, tgt, steps.split(';'))
            port = self.hold_din(din, self.busy_port(unit, tgt))
        else:
            where = self.emu_port(s, unit)
            din = self.din_args(s, unit, tgt, steps.split(';'))
            port = self.busy_port('%s@%s' % (unit, where), tgt)
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
            allowed = STEP_OPS
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
        get, and put, clear and roundtrip through analogrun's `data`, which asks the local write
        policy first and keeps a copy of what it replaces."""
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
            port = self.busy_port(a['unit'], tgt)
        else:
            where = self.emu_port(s, a['unit'])
            port = self.busy_port('%s@%s' % (a['unit'], where), tgt)
        try:
            cmd = [sys.executable, script, '--port', where, 'data'] + args
            if tgt == 'hardware':
                cmd = [sys.executable, ELKLOCK, 'run', a['unit'], '--'] + cmd
            r = subprocess.run(cmd, cwd=os.path.dirname(os.path.dirname(script)), env=s.env(),
                               capture_output=True, text=True, timeout=300 if write else 120, **NOWIN)
        finally:
            port.release()
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
        cut = len(lines) > 40
        return ['ok: %d line(s) for %s%s' % (len(lines), path, ', the first 40:' if cut else ':')] + \
            ['  ' + l for l in lines[:40]] + ['listing: %s' % out]

    def t_audio_record(self, s, a):
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
        up. A batch never records an emulator pass: the hardware gate stays with run_plan/flash."""
        if a.get('stop'):
            j = self.jobs.get(a['stop'])
            if not j or not j.batch or j.sess is not s:
                raise Refused('no batch %s of this session' % a['stop'])
            j.batch.stop()
            return ['ok: %s starts no more runs; the %s' % (j.id, j.batch.progress())]
        if bool(a.get('plan')) == bool(a.get('image')):
            raise Refused('give plan (a plan file) or image (an OS .syx to flash), and variants')
        given = a.get('variants') or []
        if not 1 <= len(given) <= 6 or not all(isinstance(v, dict) for v in given):
            raise Refused('variants is a list of 1-6 objects {name, build, image or plan}: each boots '
                          'build (else the batch\'s build), then flashes its image or runs its plan')
        runs = int(a.get('runs') or 10)
        parallel = int(a.get('parallel') or batch.PARALLEL_DEFAULT)
        if not 1 <= runs <= 200 or not 1 <= parallel <= 8:
            raise Refused('runs (per variant) is 1-200, parallel 1-8')
        seed = int(a['seed']) if a.get('seed') is not None else secrets.randbelow(10 ** 6)
        flash = bool(a.get('image'))
        minutes = min(max(int(a.get('minutes') or (10 if flash else 20)), 1), 60)
        variants, units = [], set()
        for i, v in enumerate(given):
            build = os.path.abspath(v.get('build') or a.get('build') or '')
            if not os.path.isfile(build) or not build.lower().endswith('.syx'):
                raise Refused('variant %d: build is the OS .syx its emulator boots' % (i + 1))
            x = dict(name=str(v.get('name') or chr(65 + i)), build=build)
            if flash:
                x['image'] = os.path.abspath(v.get('image') or a['image'])
                if not os.path.isfile(x['image']) or not x['image'].lower().endswith('.syx'):
                    raise Refused('variant %s: no OS .syx file %s' % (x['name'], x['image']))
                units.add(a.get('unit'))
            else:
                x['info'] = self.plan_info(v.get('plan') or a['plan'])
                units.add(x['info']['unit'])
            variants.append(x)
        if len({v['name'] for v in variants}) < len(variants):
            raise Refused('variant names must differ')
        if len(units) != 1:
            raise Refused('every variant runs on one unit' if flash or len(units) > 1 else 'give unit')
        unit = units.pop()
        u = self.unit(unit)
        if not u.get('emu_exe'):
            raise Refused('no emulator for %s' % unit)
        else:
            need(ANALOG_EMU, 'analog_emu', 'the analog-emu emulators')
        need(ELKFLASH, 'elkflash', 'elkflash')
        flash or runner()
        if not flash and any(v['info']['din'] for v in variants) and not self.emu_has_din(unit):
            raise Refused(self.no_emu_din(unit))
        snapshot = bool(a.get('snapshot'))
        if snapshot:
            # Every resumed run starts at the same emulated moment: for steps whose timing does not
            # matter, never to measure something timing-dependent (that is why it is off by default)
            if flash:
                raise Refused('snapshot is for plan batches (a flash batch boots each run fresh)')
            for v in variants:
                i = v['info']
                if not i['setup'] or i['emulator'] or i['flash']:
                    raise Refused('variant %s: snapshot needs a hardware plan with snapshot-after and no '
                                  'flash (%s)' % (v['name'], i['name']))
            if not self.emu_has_snapshots(unit):
                raise Refused('this analog-emu has no --stdin-commands, so no snapshots')
            self.prune_snapshots()
        # each variant's label names only what differs between them
        parts = lambda v: [('build', os.path.basename(v['build'])),
                           ('what', os.path.basename(v['image']) if flash else v['info']['name'])]
        vary = {k for k, _ in parts(variants[0]) if len({dict(parts(v))[k] for v in variants}) > 1} or {'build'}
        for v in variants:
            v['label'] = ' + '.join(x for k, x in parts(v) if k in vary)
        job = Job(s, 'batch', unit, 'emulator', 'batch %s x%d' % (
            os.path.basename(a['image']) if flash else variants[0]['info']['name'], runs))
        folder = os.path.join(s.folder(unit), 'batch-' + job.id)
        os.makedirs(folder)
        write_json(os.path.join(folder, 'batch.json'), dict(
            unit=unit, kind='flash' if flash else 'plan', runs=runs, parallel=parallel, seed=seed,
            minutes=minutes, snapshot=snapshot, variants=[{k: (v[k] if k != 'info' else v[k]['path']) for k in v} for v in variants]))
        job.batch = b = batch.Batch(variants, runs, parallel, seed, folder,
                                    lambda n, v: self.batch_run(s, unit, flash, minutes, folder, n, v))
        with self.lock:
            self.jobs[job.id] = job

        def work():
            try:
                if snapshot:                          # each variant's setup first, a few at a time
                    with ThreadPoolExecutor(parallel) as ex:
                        why = [w for w in ex.map(lambda v: self.batch_snapshot(s, unit, v, minutes, folder),
                                                 variants) if w]
                    if why:
                        job.state, job.lines = 'fail', why[:3] + ['no runs made: results %s' % folder]
                        return
                b.run()
                job.state = 'stopped' if b.stopped.is_set() else 'done'
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
        return ['running: %s, %d runs (%d variant(s) x %d), %d at a time, seed %d%s; status with run_id %s '
                'waits for its end' % (job.desc, len(b.plan), len(variants), runs, parallel, seed,
                                       ', each variant\'s setup snapshot first' if snapshot else '', job.id)]

    def batch_emulator(self, unit, build, work, minutes, snapshot=None, din=False):
        """A fresh headless emulator of `build` for one batch run, in `work`, on a port of its own;
        with `snapshot`, resuming that file (or, if it does not exist, saving to it on `save`);
        with `din`, its DIN MIDI on a port of its own too. -> (proc, address, None, DIN address or
        None), or (proc or None, None, why, None) when it did not come up."""
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
        if proc:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()

    def batch_plan(self, info, work, title, sections=None):
        """A twin of the plan for one batch run (all of it, or only `sections`), its run folder in
        `work`: runs going at once must not share testplan's per-second run folder names."""
        g = info['settings'].get
        lines = ['# written by elkbench for %s of %s' % (title, info['path']),
                 'include = %s' % info['path'], 'emulator = yes', 'din =', 'record-refs = no',
                 'refs = %s' % info['refs'], 'out = %s' % work]
        lines += ['%s = %s' % (k, info['rel'](g(k))) for k in ('flash', 'audio-tools', 'elkflash') if g(k)]
        if sections is not None:
            lines.append('name = %s (%s)' % (info['name'], title))
            for name, body in sections:
                lines += ['', '[%s]' % name] + body
        plan = os.path.join(work, 'plan.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
        return plan

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
            p = subprocess.run([sys.executable, RUNNER, '--port', addr] + (['--din', din] if din else []) +
                               ['--quiet', 'run', plan], cwd=ANALOGHEALTH,
                               env=s.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                               encoding='utf-8', errors='replace', timeout=minutes * 60, **NOWIN)
            ok, line = self.plan_outcome(p.returncode, p.stdout)
            if not ok:
                return 'variant %s: its setup did not pass: %s (%s)' % (v['name'], line, work)
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
        finally:
            self.end_emulator(proc)

    def batch_run(self, s, unit, flash, minutes, folder, n, v):
        """One run of a batch in its own folder: a fresh emulator of v['build'] on a port of its
        own (resuming v's setup snapshot in a snapshot batch), then the flash or plan (only its
        steps after setup when resumed), then the emulator ends. A passing run's folder goes."""
        work = os.path.join(folder, 'r%02d' % n)
        os.makedirs(work)
        snap = None
        if v.get('store'):
            snap = os.path.join(work, 'setup.snap')
            shutil.copyfile(v['store'], snap)
        proc, addr, why, din = self.batch_emulator(unit, v['build'], work, minutes, snapshot=snap,
                                                   din=not flash and v['info']['din'])
        try:
            if why:
                return dict(outcome='error', line=why, folder=work)
            if flash:
                cmd, cwd = [sys.executable, '-m', 'elkflash', 'upgrade', v['image'], '--port', addr, '--yes',
                            '--no-confirm', '--wait', '--quiet', '--log', os.path.join(work, 'elkflash.log'),
                            '--screenshot', os.path.join(work, 'after.png')], ELKFLASH
            else:
                info = v['info']
                plan = self.batch_plan(info, work, 'batch run %d' % n,
                                       sections=info['steps'][info['setup']:] if snap else None)
                cmd, cwd = [sys.executable, RUNNER, '--port', addr] + (['--din', din] if din else []) + [
                    '--quiet', 'run', plan], ANALOGHEALTH
            try:
                p = subprocess.run(cmd, cwd=cwd, env=s.env(), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                   text=True, encoding='utf-8', errors='replace', timeout=minutes * 60, **NOWIN)
            except subprocess.TimeoutExpired:
                return dict(outcome='hang', line='no end after %d min' % minutes, folder=work)
            with open(os.path.join(work, 'run.log'), 'w', encoding='utf-8') as fh:
                fh.write(p.stdout)
            if flash:
                out = [l.strip() for l in p.stdout.splitlines() if l.strip()]
                ok, line = p.returncode == 0, (out or ['exit %d' % p.returncode])[-1]
            else:
                ok, line = self.plan_outcome(p.returncode, p.stdout)
        finally:
            self.end_emulator(proc)
        if ok:
            shutil.rmtree(work, ignore_errors=True)
            return dict(outcome='pass', line=line)
        return dict(outcome='fail', line=line, folder=work)

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
EMU_BUILD = lambda: {'type': 'string', 'description': 'emulator only: boot a fresh emulator of this OS '
                                                     '.syx first (replaces yours), instead of an emulator call'}
TOOLS = [
    ('claim', 'Claim units through elklock before any hardware call (prints the last handoff). '
              'Overbridge Total Recall needs all of them.',
     S(unit={'type': ['string', 'array'], 'items': {'type': 'string'}, 'req': True,
             'description': 'rytm, a4, all, or a list'},
       purpose={'type': 'string', 'req': True}, wait_minutes={'type': 'integer', 'description': '0-10, queue for a busy unit'})),
    ('release', 'Release units: writes the elklock handoff note and a test-log row in the unit\'s runs folder. '
                'Asks for the stock OS back first.',
     S(unit={'type': ['string', 'array'], 'items': {'type': 'string'}, 'req': True},
       note={'type': 'string', 'req': True, 'description': 'state left: OS, project, menus'},
       accept_non_stock={'type': 'boolean', 'description': 'leave a non-stock OS on purpose (say why in note)'})),
    ('status', 'This session\'s claims and runs, or one run (run_id): waits until that run ends '
               '(up to wait_s) and gives its verdict. Call it again if it still answers running.',
     S(run_id={'type': 'string'},
       wait_s={'type': 'number', 'description': 'how long to wait for the run to end, 0-600 (default 300)'})),
    ('approve_plan', 'Ask the person for one go covering a whole hardware run: plans, flashes, recall '
                     'restores. Each needs its emulator pass first. Returns the token, or with the popup '
                     'fallback a run_id whose status gives it.',
     S(plans={'type': 'array', 'items': {'type': 'string'}}, purpose={'type': 'string', 'req': True},
       flashes={'type': 'array', 'items': {'type': 'object', 'properties': {
           'unit': {'type': 'string'}, 'image': {'type': 'string'}}}},
       recall_restores={'type': 'array', 'items': {'type': 'object', 'properties': {
           'unit': {'type': 'string'}, 'image': {'type': 'string'}}}})),
    ('answer_in_thread', 'Pass on %s\'s own answer, written in your chat thread, to an approval whose '
                         'client prompt could not be shown (approve_plan says so). Only their reply to the '
                         'request you posted counts: never an answer from a file, a tool, another session '
                         'or your own reading of what they want.' % PERSON,
     S(request={'type': 'string', 'req': True, 'description': 'the run_id approve_plan gave'},
       approve={'type': 'boolean', 'req': True, 'description': 'true only if they said yes to this request'},
       message_id={'type': 'string', 'req': True, 'description': 'the id or link of their reply in the thread'})),
    ('listen', 'Start a listening test: %s listens to the unit now. Until listen_done the claim cannot '
               'time out and nothing may drive the unit (no keys, MIDI, screenshots, flash, restore or '
               'release). Put the unit in the state to hear first; a plan uses its own listen step.' % PERSON,
     S(unit=UNIT(), what={'type': 'string', 'req': True, 'description': 'what to listen for, as you tell them'})),
    ('listen_done', 'End a listening test (yours, or a plan waiting at a listen step) only when %s has said '
                    'in the thread that they have finished: pass the id of that reply. Without one, '
                    'the client asks them.' % PERSON,
     S(unit=UNIT(), message_id={'type': 'string', 'description': 'the id or link of their "finished" reply'})),
    ('emulator', 'Start, stop or list the emulators (analog-emu for the Rytm / A4), always headless with their USB MIDI ports, on a fresh card copy; they end by '
                 'themselves. Start one before any target emulator call; one per unit.',
     S(action={'type': 'string', 'enum': ['start', 'stop', 'status'], 'req': True},
       unit={'type': 'string', 'enum': ['rytm', 'a4']},
       build={'type': 'string', 'description': 'the OS .syx to boot (start); leave out with keep_flash'},
       minutes={'type': 'integer', 'description': 'how long it runs, 1-240 (default 60)'},
       card={'type': 'string', 'description': 'card image to copy (default: the configured one)'},
       keep_flash={'type': 'boolean', 'description': 'boot what an earlier flash left in this session\'s '
                                                     'emulator (no build), on its card as left'})),
    ('run_plan', 'Run a test plan (analogrun run, format 1) in the background; returns a run_id. target '
                 'emulator records the pass a hardware run needs; hardware needs the claim and an approval token.',
     S(plan={'type': 'string', 'req': True, 'description': 'the hardware plan file (an emulator twin is made for you)'},
       target=TARGET(), approval={'type': 'string'}, build=EMU_BUILD(),
       record_refs={'type': 'boolean', 'description': 'emulator only: save the reference screens'})),
    ('flash', 'Flash an OS .syx with elkflash (--no-confirm --wait) in the background; returns a run_id. '
              'Hardware: claim, a passing emulator flash of the same file, and an approval token.',
     S(unit=UNIT(), image={'type': 'string', 'req': True}, target=TARGET(), approval={'type': 'string'},
       build=EMU_BUILD())),
    ('batch', 'Emulator only (Rytm, A4): run a plan or a flash many times across variants, each run on a '
              'fresh emulator of its own, a few at a time, round by round in shuffled order; returns a '
              'run_id. status gives progress, then a line per variant (pass count, 95% range), Fisher p '
              'against the first variant and the first failures; per-run detail stays in results.jsonl. '
              'Never counts as the emulator pass a hardware run needs.',
     S(variants={'type': 'array', 'description': '1-6 arms, the first is the one the others are compared with',
                 'items': {'type': 'object', 'properties': {
                     'name': {'type': 'string', 'description': 'short label (default A, B, ...)'},
                     'build': {'type': 'string', 'description': 'the OS .syx the emulator boots'},
                     'image': {'type': 'string', 'description': 'flash batches: the .syx to flash'},
                     'plan': {'type': 'string', 'description': 'plan batches: the plan file'}}}},
       plan={'type': 'string', 'description': 'the plan every variant runs (unless it names its own)'},
       image={'type': 'string', 'description': 'the OS .syx every variant flashes (unless it names its own)'},
       unit={'type': 'string', 'enum': ['rytm', 'a4'], 'description': 'flash batches: the unit'},
       build={'type': 'string', 'description': 'the OS .syx every variant boots (unless it names its own)'},
       runs={'type': 'integer', 'description': 'runs per variant, 1-200 (default 10)'},
       parallel={'type': 'integer', 'description': 'runs at once, 1-8 (default 4; all batches on the PC share a cap)'},
       minutes={'type': 'integer', 'description': 'a run that takes longer counts as a hang (default 10 flash, 20 plan)'},
       seed={'type': 'integer', 'description': 'the run order\'s shuffle (default random, reported)'},
       snapshot={'type': 'boolean', 'description': 'plan batches with snapshot-after: every run resumes the '
                                                   'variant\'s setup snapshot (faster; every run then starts at '
                                                   'the same emulated moment, so not for timing questions)'},
       stop={'type': 'string', 'description': 'a batch run_id: start no more of its runs'})),
    ('steps', 'analogrun steps (tap, hold, keys, turn, cc, nrpn, note, wait, stats, shot NAME, send FILE [din], '
              'capture NAME S [any|none] [din], ping, save-project, save-kit...) on a unit or its emulator, any '
              'local write policy applied; up to 2 min. din: over the unit\'s DIN MIDI (Rytm/A4; hardware: units.X.din '
              'in config).',
     S(unit=UNIT(), target=TARGET(), steps={'type': 'string', 'req': True})),
    ('shot', 'Screenshot into this session\'s evidence folder.',
     S(unit=UNIT(), target=TARGET(), name={'type': 'string', 'description': 'bare .png or .bin name'})),
    ('expect_screen', 'Compare the screen with a reference .bin (default folder: the unit\'s plans\\refs), '
                      'retrying up to wait_s; record true saves it (emulator only).',
     S(unit=UNIT(), target=TARGET(), ref={'type': 'string', 'req': True}, wait_s={'type': 'number'},
       record={'type': 'boolean'})),
    ('data', 'The data store (analogrun data): ls PATH (\'/\' gives the roots), get PATH (one object '
             'as Transfer stores it, into the evidence folder); put FILE into PATH, clear PATH, roundtrip PATH '
             '(read, write back or to `to`, read back, compare). Writes ask the local write policy first and '
             'keep a copy of what they replace; hardware needs the claim.',
     S(unit=UNIT(), target=TARGET(), op={'type': 'string', 'enum': list(DATA_OPS), 'req': True},
       path={'type': 'string', 'req': True}, name={'type': 'string', 'description': 'ls/get: bare file name for the result'},
       file={'type': 'string', 'description': 'put: the object file (from get)'},
       to={'type': 'string', 'description': 'roundtrip: write to this slot instead of path'},
       slot_name={'type': 'string', 'description': 'put/roundtrip: the name the slot gets (a policy may require one)'})),
    ('audio_record', 'Record every ASIO input of the unit (optionally with a 997 Hz tone into it) into the '
                     'evidence folder; hardware needs the claim.',
     S(unit=UNIT(), seconds={'type': 'number', 'req': True}, name={'type': 'string'}, tone={'type': 'boolean'})),
    ('audio_check', 'audiocheck.py dropouts or compare on recordings (offline, no claim): one verdict line.',
     S(op={'type': 'string', 'enum': ['dropouts', 'compare'], 'req': True}, wav={'type': 'string', 'req': True},
       ref={'type': 'string'}, channels={'type': 'string', 'description': 'e.g. 1,2'},
       folder={'type': 'string', 'description': 'where relative names are'})),
    ('recall_take', 'Overbridge Total Recall: take the unit\'s project image (obtr.py) into the evidence '
                    'folder. Needs claims on every unit; never restarts Overbridge.',
     S(unit=UNIT(), name={'type': 'string'})),
    ('recall_restore', 'Overbridge Total Recall restore of an image from recall_take (replaces the active '
                       'project). Needs claims on every unit and an approval token.',
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
CODE = [os.path.abspath(f) for f in (__file__, batch.__file__, elklock.__file__)]


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
            self.proc.stdin.close()
            self.proc.wait(10)
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
                'approvals_kept': sum(time.time() < a['expires'] for a in D.approvals.values())})
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
