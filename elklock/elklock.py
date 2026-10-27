"""Temporary ownership of the Elektron units on this PC, shared by every AI session
(any project) and by the person.

    elklock.py status                              who holds what, who waits, last handoff
    elklock.py claim DEVICE... --for "why" [--wait [--timeout 10m]] [--max 2h] [--idle 15m]
                                           [--manual NAME]
    elklock.py run DEVICE... -- COMMAND...         run COMMAND under the claim (never times out
                                                   while it runs)
    elklock.py renew DEVICE... [--max 2h]
    elklock.py listen DEVICE... --for "what"       the person listens now: the claim cannot
                                                   time out, nothing may drive the unit
    elklock.py heard DEVICE... --by "where"        the person said they have finished
    elklock.py release DEVICE... | --all  [--note "state left"] [--no-snapshot]
    elklock.py check DEVICE                        exit 0 if this session holds it
    elklock.py break DEVICE --yes                  the person frees a stuck claim
    elklock.py hook                                PreToolUse hook (reads JSON on stdin)

DEVICE: rytm, a4, digitone, digitakt (see DEVICES), or "all".

The lock is Windows' own: a small background holder process opens DEVICE.lock with no
sharing and keeps it open. While it does, nobody else can open that file, so a second claim
fails with a sharing violation. Windows closes the handle when the holder exits for any
reason, so a claim can never outlive its holder.

The holder lets go on `release`; when the claiming session's client process exits
(ELKBENCH_CLIENT_PID, else Claude Code's CLAUDE_PID);
after --max (default 2 h; `renew` extends it); or after --idle (default 15 min) with no device
command. None of those three automatic ends happens while a `elklock run` command is still
running: the claim waits for it, and none while the person is listening (`listen` until
`heard`; the hook then denies every device command and `release` waits too). Device commands
that the hook lets through count as use. A claim that ends by itself records the OS and the
screen in its handoff note, as `release` does.

`claim --wait` queues: tickets are served oldest first, and a plain claim does not jump the
queue. `release` writes a handoff record (OS version from `elkflash info`, a screenshot via
`elkflash info --screenshot`, your --note); the next `claim` prints it.

DEVICE.json beside the lock says who holds it; it is only a label, the lock is the handle.
Exit codes: 0 ok, 3 held by someone else (or wait timed out), 2 usage; `run` returns the
command's own code. Locks live in %LOCALAPPDATA%\\elklock on Windows, ~/.local/state/elklock
elsewhere (override with ELKLOCK_DIR).
Windows and Linux, stdlib only.
"""
import ctypes as C, glob, json, os, re, secrets, subprocess, sys, time
if os.name == 'nt':
    import ctypes.wintypes as W

DIR = os.environ.get('ELKLOCK_DIR') or os.path.join(
    os.environ.get('LOCALAPPDATA') if os.name == 'nt' else
    os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'), 'elklock')
# device -> (regex that names it in a command line, MIDI port name fragment)
DEVICES = {
    'rytm': (r'rytm|armkii', 'Analog Rytm'),
    'a4': (r'analog.?four|a4mkii|\ba4\b', 'Analog Four'),
    'digitone': (r'digitone|digiusb', 'Digitone'),   # digiusb (digihealth) talks to the Digitone
    'digitakt': (r'digitakt', 'Digitakt'),           # digiusb with a Digitakt: name it, or claim both
}


def load_config():
    """elkbench's config.toml (ELKBENCH_CONFIG, else %LOCALAPPDATA%\\elkbench\\config.toml on
    Windows, ~/.config/elkbench/config.toml elsewhere); every key is optional. Uses
    tools.elkflash and tools.analoghealth (checkouts; left out, the handoff snapshot uses an
    installed elkflash and skips the active project) and elklock.guard (more command names the
    hook treats as device tools)."""
    import tomllib
    path = os.environ.get('ELKBENCH_CONFIG') or os.path.join(
        os.environ.get('LOCALAPPDATA') if os.name == 'nt' else
        os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'elkbench', 'config.toml')
    try:
        with open(path, 'rb') as fh:
            return tomllib.load(fh)
    except (OSError, ValueError):
        return {}


CFG = load_config()
ELKFLASH_REPO = CFG.get('tools', {}).get('elkflash')          # None: an installed elkflash
ANALOGHEALTH = CFG.get('tools', {}).get('analoghealth')
RUNNER_DIR = os.path.join(ANALOGHEALTH, 'tools') if ANALOGHEALTH else None
# commands that talk to a unit: the hook requires a claim before they run
GUARD = ['analogusb', 'analogrun', 'digiusb', 'digirun', 'elkflash', 'obtr',
         'asio_rec', 'asio_tone', 'asio_loop', 'wavein_rec', 'loopback.py'] + list(CFG.get('elklock', {}).get('guard', []))
# a tool's name as a word of its own (analogrun, analogrun.py, tools\\analogrun.py), not inside a longer
# name (test_analogusb.py) or a folder (analoghealth-gap10)
DEVICE_TOOLS = re.compile('|'.join(r'(?<![\w-])%s(?![\w-])' % re.escape(g) for g in GUARD), re.I)
# Tools that drive the Overbridge Engine, which every unit in Overbridge mode shares: a fault or
# restart there reaches all of them, so these need a claim on every unit, connected or not.
ENGINE_TOOLS = re.compile(r'obtr|overbridge engine|taskkill.*overbridge', re.I)
# offline forms that never touch a unit
OFFLINE = re.compile(r'selftest|--self-test|--help|\bdecode\b|\bencode\b|\bbootstrap\b|'
                     r'--from-file|unittest|pytest|\blist\b|--list-midi|--dry-run', re.I)
# emulator ports: the loopMIDI names, or an emulator's USB MIDI over TCP (a unit is never on TCP)
EMULATOR = re.compile(r'rytm-emu|a4-emu|dn-emu|--port[ =]"?tcp:', re.I)
# long, must-not-be-interrupted operations: the hook requires them under `elklock run`
MUST_RUN = re.compile(r'elkflash\S*\s+upgrade', re.I)
# file, text and git commands: a tool's name in their arguments is a path (a run's elkflash.log)
# or text (a commit message), not a command
FILE_COMMANDS = re.compile(r'\s*(?:cat|cp|mv|ls|dir|head|tail|less|more|grep|rg|find|type|copy|wc|diff|'
                           r'sed|awk|sort|uniq|cut|tr|echo|printf|tee|cd|pushd|popd|mkdir|touch|git|'
                           r'get-content|gc|copy-item|cpi|get-childitem|gci|select-string|sls|test-path|'
                           r'set-location|sl|write-output|write-host|set-content|add-content|out-file|'
                           r'new-item|ni|move-item|mi|export|set|\$env:\w+\s*=)(?:\s|$)', re.I)


def split_command(cmd):
    """`cmd` cut at ; && || | & and newlines outside quotes. A heredoc's lines stay with the
    command that reads them."""
    segs, cur, quote, i = [], '', None, 0
    while i < len(cmd):
        c = cmd[i]
        if quote:
            cur += c
            if c == quote:
                quote = None
            elif c == '\\' and quote == '"' and i + 1 < len(cmd):
                cur += cmd[i + 1]
                i += 1
        elif c in '\'"':
            quote = c
            cur += c
        elif c == '\n' and re.search(r'<<-?\s*([\'"]?)(\w+)\1', cur):
            end = re.search(r'<<-?\s*([\'"]?)(\w+)\1', cur).group(2)
            m = re.compile(r'^\s*%s\s*$' % re.escape(end), re.M).search(cmd, i + 1)
            stop = m.end() if m else len(cmd)
            cur += cmd[i:stop]
            i = stop
            continue
        elif cmd.startswith(('&&', '||'), i):
            segs.append(cur)
            cur = ''
            i += 2
            continue
        elif c in ';|\n' or (c == '&' and not cmd.startswith('&1', i) and not cur.rstrip().endswith('>')):
            segs.append(cur)
            cur = ''
        else:
            cur += c
        i += 1
    return [seg for seg in segs + [cur] if seg.strip()]


def substitutions(seg):
    """The commands inside $(...) and backticks in `seg`."""
    inner, i = [], 0
    while i < len(seg):
        if seg.startswith('$(', i):
            depth, j = 1, i + 2
            while j < len(seg) and depth:
                depth += {'(': 1, ')': -1}.get(seg[j], 0)
                j += 1
            inner.append(seg[i + 2:j - 1])
            i = j
        elif seg[i] == '`':
            j = seg.find('`', i + 1)
            j = len(seg) if j < 0 else j
            inner.append(seg[i + 1:j])
            i = j + 1
        else:
            i += 1
    return inner


def device_part(cmd):
    """`cmd` without its file, text and git commands (`cat X`, `sed -n 1p X`, `git commit -m
    "..."`, `Copy-Item X Y`, ...), whose arguments are paths and text; a command they run inside
    $(...) or backticks is still checked. What is left is what may talk to a unit."""
    keep = []
    for seg in split_command(cmd):
        if FILE_COMMANDS.match(seg) and not re.search(r'\s-exec(?:dir)?\s|\s-ok\s', seg):
            keep += [p for p in (device_part(s) for s in substitutions(seg)) if p]
        else:
            keep.append(seg)
    return ' ; '.join(keep)

WINDOWS = os.name == 'nt'
if WINDOWS:
    k32 = C.WinDLL('kernel32', use_last_error=True)
    k32.CreateFileW.restype = W.HANDLE
    k32.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, C.c_void_p, W.DWORD, W.DWORD, W.HANDLE]
    k32.OpenProcess.restype = W.HANDLE
    k32.OpenProcess.argtypes = [W.DWORD, W.BOOL, W.DWORD]
    k32.WaitForSingleObject.argtypes = [W.HANDLE, W.DWORD]
    k32.GetExitCodeProcess.argtypes = [W.HANDLE, C.POINTER(W.DWORD)]
    k32.CloseHandle.argtypes = [W.HANDLE]
    INVALID = W.HANDLE(-1).value
    GENERIC_RW, OPEN_ALWAYS, SHARING_VIOLATION = 0xC0000000, 4, 32
    SYNCHRONIZE, QUERY_LIMITED, WAIT_TIMEOUT, STILL_ACTIVE = 0x00100000, 0x1000, 0x102, 259
else:
    import fcntl


def now():
    return time.time()


def path(dev, ext):
    return os.path.join(DIR, dev + ext)


def open_exclusive(p):
    """The handle, or None if someone else holds the file. Windows: an open with no sharing;
    elsewhere: an flock on an open file descriptor. Either ends when the process exits."""
    if not WINDOWS:
        fd = os.open(p, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            return None
        return fd
    h = k32.CreateFileW(p, GENERIC_RW, 0, None, OPEN_ALWAYS, 0x80, None)
    if h == INVALID or h is None:
        if C.get_last_error() == SHARING_VIOLATION:
            return None
        raise OSError(C.get_last_error(), 'CreateFileW %s' % p)
    return h


def close_handle(h):
    if WINDOWS:
        k32.CloseHandle(h)
    else:
        os.close(h)


def pid_alive(pid):
    if not pid:
        return False
    if not WINDOWS:
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True                     # someone else's process, but it exists
        try:                                # a zombie has exited
            with open('/proc/%d/stat' % int(pid)) as fh:
                return fh.read().rsplit(')', 1)[1].split()[0] != 'Z'
        except (OSError, IndexError):
            return True
    h = k32.OpenProcess(QUERY_LIMITED, False, int(pid))
    if not h:
        return False
    code = W.DWORD()
    ok = k32.GetExitCodeProcess(h, C.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == STILL_ACTIVE


class Mutex:
    """Serialises read-modify-write of the label files (exclusive create; stale after 10 s)."""

    def __enter__(self):
        self.p = os.path.join(DIR, '.mutex')
        for _ in range(400):
            try:
                os.close(os.open(self.p, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
                return self
            except FileExistsError:
                try:
                    if now() - os.path.getmtime(self.p) > 10:
                        os.remove(self.p)
                except OSError:
                    pass
                time.sleep(0.025)
        raise SystemExit('elklock: could not take %s' % self.p)

    def __exit__(self, *exc):
        try:
            os.remove(self.p)
        except FileNotFoundError:
            pass


def read_json(p):
    try:
        with open(p, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, ValueError, PermissionError):
        return {}


def write_json(p, data):
    tmp = '%s.%d.tmp' % (p, os.getpid())
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, indent=1)
    for _ in range(40):
        try:
            os.replace(tmp, p)
            return
        except PermissionError:          # a reader has it open for a moment
            time.sleep(0.025)
    os.replace(tmp, p)


def update_info(dev, fn):
    """Apply fn to the label under the mutex; returns the new label (or {} if gone)."""
    with Mutex():
        info = read_json(path(dev, '.json'))
        if not info:
            return {}
        fn(info)
        write_json(path(dev, '.json'), info)
        return info


def held(dev):
    h = open_exclusive(path(dev, '.lock'))
    if h is None:
        return True
    close_handle(h)
    return False


def holder(dev):
    """The current holder's label, or None if the device is free."""
    if not held(dev):
        return None
    for _ in range(20):                  # the label may be being rewritten this instant
        info = read_json(path(dev, '.json'))
        if info:
            return info
        time.sleep(0.025)
    return {'purpose': '(holder starting)'}


def live_runs(info):
    return [r for r in info.get('runs', []) if pid_alive(r.get('pid'))]


def describe(dev, info):
    if info is None:
        return '%-9s free' % dev
    who = 'person: %s' % info['manual'] if info.get('manual') else 'session %s' % info.get('session')
    since = time.strftime('%H:%M', time.localtime(info['since'])) if info.get('since') else '?'
    runs = live_runs(info)
    busy = ', running: %s' % '; '.join('%s (for %d s)' % (r['cmd'][:60], now() - r.get('since', now()))
                                       for r in runs) if runs else ''
    idle = int((now() - info.get('used', now())) // 60)
    left = int((info.get('expires', now()) - now()) // 60)
    listening = ', LISTENING (the person is listening: %s)' % info['listening'].get('what', '') \
        if info.get('listening') else ''
    return '%-9s held by %s (%s), for "%s", since %s, idle %d min, %d min max left%s%s' % (
        dev, who, info.get('where', '?'), info.get('purpose', ''), since, idle, left, busy, listening)


def duration(text):
    m = re.fullmatch(r'(\d+)([smh]?)', text or '')
    if not m:
        raise SystemExit('elklock: bad duration %r (e.g. 45m, 2h, 900)' % text)
    return int(m.group(1)) * {'': 1, 's': 1, 'm': 60, 'h': 3600}[m.group(2)]


def human(sec):
    return '%d min' % (sec // 60) if sec >= 120 else '%d s' % sec


def devices(args):
    out = []
    for a in args:
        a = a.lower()
        if a == 'all':
            out += list(DEVICES)
        elif a in DEVICES:
            out.append(a)
        else:
            raise SystemExit('elklock: unknown device %r (known: %s, all)' % (a, ', '.join(DEVICES)))
    if not out:
        raise SystemExit('elklock: name a device (%s, all)' % ', '.join(DEVICES))
    return list(dict.fromkeys(out))


def opt(args, name, default=None):
    if name in args:
        i = args.index(name)
        val = args[i + 1] if i + 1 < len(args) else None
        del args[i:i + 2]
        return val
    return default


def flag(args, name):
    if name in args:
        args.remove(name)
        return True
    return False


def session_id():
    """The calling session's id: ELKBENCH_SESSION_ID, else Claude Code's own."""
    return os.environ.get('ELKBENCH_SESSION_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID')


def client_pid():
    """The session's client process, whose exit ends its claims (None: not known)."""
    return os.environ.get('ELKBENCH_CLIENT_PID') or os.environ.get('CLAUDE_PID')


def mine(info, manual=None, session=None):
    if not info:
        return False
    if manual:
        return info.get('manual') == manual
    session = session or session_id()
    return bool(session) and info.get('session') == session and not info.get('manual')


# ---- the holder process ---------------------------------------------------------------

def run_holder(dev, nonce, info_json):
    """Hold DEVICE.lock open until released, or until the session ends, --max or --idle
    pass with no `elklock run` command still running."""
    h = open_exclusive(path(dev, '.lock'))
    if h is None:
        return 3
    info = json.loads(info_json)
    info.update(holder_pid=os.getpid(), nonce=nonce)
    with Mutex():
        write_json(path(dev, '.json'), info)
    if WINDOWS:
        watch = k32.OpenProcess(SYNCHRONIZE, False, int(info['pid'])) if info.get('pid') else None
    else:
        watch = info.get('pid') if pid_alive(info.get('pid')) else None
    session_gone = bool(info.get('pid')) and not watch
    reason = 'released'
    try:
        while True:
            if watch and WINDOWS and k32.WaitForSingleObject(watch, 1000) != WAIT_TIMEOUT:
                session_gone = True
                k32.CloseHandle(watch)
                watch = None
            elif watch and not WINDOWS:
                time.sleep(1)
                if not pid_alive(watch):
                    session_gone, watch = True, None
            if not watch:
                time.sleep(1)
            cur = read_json(path(dev, '.json'))
            if not cur:
                continue                    # being rewritten this instant
            if cur.get('nonce') != nonce or cur.get('release'):
                reason = cur.get('release_reason', 'released')
                break
            if live_runs(cur):
                continue                    # never end in the middle of a run
            t = now()
            if session_gone:
                reason = 'claiming session ended'
                break
            if cur.get('listening'):
                continue                    # the person is listening: no idle or max end
            if t > cur.get('expires', 0):
                reason = 'max time reached'
                break
            if t - cur.get('used', t) > cur.get('idle', 900):
                reason = 'idle'
                break
    finally:
        rec = None
        if reason != 'released' and read_json(path(dev, '.handoff.json')).get('nonce') != nonce:
            # An automatic end: record the unit's state (OS, screen) while the lock is still
            # ours, so the next holder knows what was left on it. A scratch ELKLOCK_DIR is a
            # test: no device reads then.
            rec = snapshot(dev, '(no release: the claim ended by itself (%s))' % reason,
                           bool(os.environ.get('ELKLOCK_DIR')))
        with Mutex():
            cur = read_json(path(dev, '.json'))
            if cur.get('nonce') == nonce:
                if rec is not None:           # automatic end: leave a handoff note
                    rec.update(nonce=nonce, holder=who_of(cur), purpose=cur.get('purpose'), ended=reason)
                    write_json(path(dev, '.handoff.json'), rec)
                try:
                    os.remove(path(dev, '.json'))
                except FileNotFoundError:
                    pass
        close_handle(h)                     # the lock ends here
    return 0


def who_of(info):
    return 'person: %s' % info['manual'] if info.get('manual') else 'session %s' % info.get('session')


def spawn_holder(dev, info):
    nonce = secrets.token_hex(8)
    cmd = [sys.executable, os.path.abspath(__file__), '_hold', dev, nonce, json.dumps(info)]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
              close_fds=True)
    if not WINDOWS:                         # its own session: the command's end does not reach it
        p = subprocess.Popen(cmd, start_new_session=True, **kw)
    else:
        flags = 0x00000008 | 0x00000200    # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        try:   # leave the tool call's job object, so the holder outlives the command
            p = subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)
        except OSError:
            p = subprocess.Popen(cmd, creationflags=flags, **kw)
    deadline = now() + 10
    while now() < deadline:
        if read_json(path(dev, '.json')).get('nonce') == nonce and held(dev):
            return True
        if p.poll() is not None:
            return False
        time.sleep(0.05)
    p.kill()
    return False


# ---- the queue ------------------------------------------------------------------------

def queue_dir(dev):
    d = os.path.join(DIR, 'queue', dev)
    os.makedirs(d, exist_ok=True)
    return d


def tickets(dev):
    """Live tickets, oldest first; dead or expired ones are removed."""
    out = []
    for p in sorted(glob.glob(os.path.join(queue_dir(dev), '*.json'))):
        t = read_json(p)
        if not t:
            continue
        dead = (t.get('pid') and not pid_alive(t['pid'])) or now() > t.get('until', 0) \
            or (t.get('waiter') and not pid_alive(t['waiter']))
        if dead:
            try:
                os.remove(p)
            except OSError:
                pass
            continue
        t['_path'] = p
        out.append(t)
    return out


def ahead_of_me(dev, my_ticket):
    for t in tickets(dev):
        if t['_path'] == my_ticket:
            return []
        yield t


def take_ticket(dev, info, until):
    p = os.path.join(queue_dir(dev), '%019d-%s.json' % (time.time_ns(), secrets.token_hex(4)))
    write_json(p, {'who': who_of(info), 'purpose': info['purpose'], 'pid': info.get('pid'),
                   'waiter': os.getpid(), 'since': now(), 'until': until})
    return p


# ---- commands -------------------------------------------------------------------------

def show_handoff(dev):
    h = read_json(path(dev, '.handoff.json'))
    if not h:
        return
    print('  last handoff (%s, %s, ended: %s): %s' % (
        time.strftime('%H:%M', time.localtime(h.get('time', 0))), h.get('holder'),
        h.get('ended'), h.get('note') or ''))
    for k in ('os', 'active_project', 'screen_file'):
        if h.get(k):
            print('    %s: %s' % (k, h[k]))


def cmd_claim(args):
    purpose = opt(args, '--for', '')
    limit = duration(opt(args, '--max', '2h'))
    idle = duration(opt(args, '--idle', '15m'))
    manual = opt(args, '--manual')
    wait = flag(args, '--wait')
    timeout = duration(opt(args, '--timeout', '10m'))
    session = session_id()
    if not manual and not session:
        raise SystemExit('elklock: no ELKBENCH_SESSION_ID (or CLAUDE_CODE_SESSION_ID); a person claims '
                         'with --manual NAME')
    if not purpose:
        raise SystemExit('elklock: say what the claim is for: --for "T16 store-check run"')
    devs = devices(args)
    info0 = {'session': None if manual else session, 'manual': manual,
             'pid': None if manual else client_pid(), 'where': os.getcwd(),
             'purpose': purpose, 'idle': idle, 'runs': []}
    todo = [d for d in devs if not mine(holder(d), manual)]
    for d in devs:
        if d not in todo:
            print('already yours: ' + describe(d, holder(d)))
    if not todo:
        return 0
    my = {d: take_ticket(d, info0, now() + (timeout if wait else 30)) for d in todo}
    deadline = now() + (timeout if wait else 0)
    shown = False
    try:
        while True:
            blockers = []
            for d in todo:
                cur = holder(d)
                if cur:
                    blockers.append('BUSY ' + describe(d, cur))
                for t in ahead_of_me(d, my[d]):
                    blockers.append('QUEUED before you on %s: %s, for "%s"' % (d, t['who'], t['purpose']))
            if not blockers:
                taken = []
                for d in todo:
                    t = now()
                    if spawn_holder(d, dict(info0, since=t, used=t, expires=t + limit)):
                        taken.append(d)
                    else:
                        break
                if len(taken) == len(todo):
                    for d in todo:
                        print('claimed ' + describe(d, holder(d)))
                        show_handoff(d)
                    return 0
                for d in taken:                   # lost a race: give back, try again
                    release(d, 'released')
            if now() >= deadline:
                for b in blockers:
                    print(b)
                print('Nothing claimed. %s' % ('Waited %s.' % human(timeout) if wait else
                      'Wait (claim --wait), or tell the person who holds it.'))
                return 3
            if not shown:
                for b in blockers:
                    print(b)
                print('waiting (up to %s)...' % human(timeout), flush=True)
                shown = True
            time.sleep(0.5)
    finally:
        for p in my.values():
            try:
                os.remove(p)
            except OSError:
                pass


def release(dev, reason):
    update_info(dev, lambda i: i.update(release=True, release_reason=reason))
    for _ in range(80):
        if not held(dev):
            return True
        time.sleep(0.05)
    return False


def snapshot(dev, note, skip):
    """The handoff record: OS version, a screenshot and the note. Best-effort, read-only."""
    rec = {'time': now(), 'note': note, 'ended': 'released'}
    if skip:
        return rec
    port = DEVICES[dev][1]
    hdir = os.path.join(DIR, 'handoff', dev)
    os.makedirs(hdir, exist_ok=True)
    # One elkflash call reads the OS version and saves the screen (RPC Screenshot).
    png = os.path.join(hdir, 'screen.png')
    try:
        if os.path.exists(png):
            os.remove(png)
        r = subprocess.run([sys.executable, '-m', 'elkflash', 'info', '--port', port,
                            '--screenshot', png],
                           cwd=ELKFLASH_REPO, capture_output=True, text=True, timeout=30)
        line = [l for l in r.stdout.splitlines() if re.search(r'\bOS\b|version', l, re.I)]
        rec['os'] = (line[0].strip() if line else (r.stdout.strip() or r.stderr.strip())[:200])
        rec['screen_file'] = png if os.path.exists(png) else 'no screen came back'
    except Exception as e:                       # noqa: BLE001 - record and go on
        rec['os'] = 'elkflash info failed: %s' % e
        rec['screen_file'] = 'no screen: %s' % e
    if dev == 'rytm' and RUNNER_DIR:
        # The unit won't say which project is active; kit 1's name is the next best
        # thing (and a local write policy may add what it makes of it).
        try:
            r = subprocess.run([sys.executable, 'analogrun.py', '--port', port, 'active'],
                               cwd=RUNNER_DIR, capture_output=True, text=True, timeout=20)
            rec['active_project'] = (r.stdout.strip() or r.stderr.strip().splitlines()[-1])[:200]
        except Exception as e:                   # noqa: BLE001
            rec['active_project'] = 'analogrun active failed: %s' % e
    return rec


def cmd_release(args):
    manual = opt(args, '--manual')
    note = opt(args, '--note', '')
    skip = flag(args, '--no-snapshot')
    if flag(args, '--all'):
        args = [d for d in DEVICES if mine(holder(d), manual)] or list(DEVICES)
    rc = 0
    for d in devices(args):
        cur = holder(d)
        if cur is None:
            continue
        if not mine(cur, manual):
            print('not yours, left alone: ' + describe(d, cur))
            continue
        if live_runs(cur):
            print('%s: a `elklock run` command is still running (%s); release after it ends'
                  % (d, live_runs(cur)[0]['cmd'][:60]))
            rc = 3
            continue
        if cur.get('listening'):
            print('%s: the person is still listening (%s); release only after they say they have '
                  'finished (`heard`)' % (d, cur['listening'].get('what', '')))
            rc = 3
            continue
        rec = snapshot(d, note, skip)
        rec.update(holder=who_of(cur), purpose=cur.get('purpose'), nonce=cur.get('nonce'))
        write_json(path(d, '.handoff.json'), rec)
        if release(d, 'released'):
            print('released %s' % d)
            show_handoff(d)
        else:
            print('%s: holder did not let go' % d)
            rc = 1
    return rc


def cmd_run(args):
    if '--' not in args:
        raise SystemExit('elklock: run DEVICE... -- COMMAND...')
    i = args.index('--')
    devs, command = devices(args[:i]), args[i + 1:]
    if not command:
        raise SystemExit('elklock: run needs a command after --')
    for d in devs:
        if not mine(holder(d)):
            print('elklock: claim %s first: ' % d + describe(d, holder(d)))
            return 3
    p = subprocess.Popen(command)
    entry = {'pid': p.pid, 'cmd': ' '.join(command), 'since': now()}
    for d in devs:
        update_info(d, lambda inf: (inf.setdefault('runs', []).append(entry),
                                    inf.update(used=now())))
    try:
        rc = p.wait()
    except KeyboardInterrupt:
        p.terminate()
        rc = p.wait()
    finally:
        for d in devs:
            update_info(d, lambda inf: inf.update(
                runs=[r for r in inf.get('runs', []) if r.get('pid') != p.pid], used=now()))
    return rc


def cmd_break(args):
    """The person frees someone else's claim: the holder is asked to exit, then killed."""
    if not flag(args, '--yes'):
        raise SystemExit("elklock: break frees someone else's claim; add --yes")
    for d in devices(args):
        cur = holder(d)
        if cur is None:
            print('%s was free' % d)
            continue
        write_json(path(d, '.handoff.json'), {'time': now(), 'holder': who_of(cur),
                   'purpose': cur.get('purpose'), 'ended': 'broken by the person',
                   'note': '(unit state not recorded)'})
        if not release(d, 'broken by the person') and cur.get('holder_pid'):
            subprocess.run(['taskkill', '/F', '/PID', str(cur['holder_pid'])], capture_output=True)
        print(('broke ' if not held(d) else 'could not break ') + describe(d, cur))
    return 0


def cmd_renew(args):
    limit = duration(opt(args, '--max', '2h'))
    manual = opt(args, '--manual')
    rc = 0
    for d in devices(args):
        if not mine(holder(d), manual):
            print('not yours: ' + describe(d, holder(d)))
            rc = 3
            continue
        print('renewed ' + describe(d, update_info(
            d, lambda i: i.update(expires=now() + limit, used=now()))))
    return rc


def cmd_listen(args):
    """The person listens to the unit now: the claim cannot end by idle or max time, and
    nothing may drive the unit (the hook denies device commands) until `heard`."""
    what = (opt(args, '--for') or '').strip()
    if not what:
        raise SystemExit('elklock: listen DEVICE --for "what the person listens for"')
    rc = 0
    for d in devices(args):
        if not mine(holder(d)):
            print('not yours: ' + describe(d, holder(d)))
            rc = 3
            continue
        print('listening ' + describe(d, update_info(
            d, lambda i: i.update(listening={'what': what, 'since': now()}, used=now()))))
    return rc


def cmd_heard(args):
    """The person said they have finished listening (--by: where they said it)."""
    by = (opt(args, '--by') or '').strip()
    if not by:
        raise SystemExit('elklock: heard DEVICE --by "where the person said they have finished"')
    rc = 0
    for d in devices(args):
        cur = holder(d)
        if not mine(cur):
            print('not yours: ' + describe(d, cur))
            rc = 3
            continue
        if not cur.get('listening'):
            print('%s: no listening test was going on' % d)
            continue

        def done(i):
            i.setdefault('heard', []).append(dict(i.pop('listening'), ended=now(), by=by))
            i.update(used=now())
        print('heard ' + describe(d, update_info(d, done)))
    return rc


def cmd_check(args):
    return 0 if all(mine(holder(d)) for d in devices(args)) else 3


if WINDOWS:
    class MIDIINCAPSW(C.Structure):
        _fields_ = [('wMid', W.WORD), ('wPid', W.WORD), ('vDriverVersion', W.UINT),
                    ('szPname', W.WCHAR * 32), ('dwSupport', W.DWORD)]


def midi_inputs():
    """Names of the MIDI ports the system lists now, or None where it cannot tell. Only lists
    them: no port is opened, so it never competes with a tool holding one. Linux: the ALSA
    sequencer's client and port names."""
    if not WINDOWS:
        try:
            with open('/proc/asound/seq/clients', encoding='utf-8', errors='replace') as fh:
                return re.findall(r'^\s*(?:Client|Port)\s+\d+\s*:\s*"([^"]*)"', fh.read(), re.M)
        except OSError:
            return None
    winmm = C.WinDLL('winmm')
    names = []
    for i in range(winmm.midiInGetNumDevs()):
        caps = MIDIINCAPSW()
        if winmm.midiInGetDevCapsW(i, C.byref(caps), C.sizeof(caps)) == 0:
            names.append(caps.szPname)
    return names


def connected(dev, ports):
    """True if a MIDI port of the unit is listed (it is on USB and running its OS); also
    True when the system cannot list its ports (ports None)."""
    if ports is None:
        return True
    want = DEVICES[dev][1].lower()
    return any(want in p.lower() and 'emu' not in p.lower() for p in ports)


def cmd_status(args):
    if flag(args, '--json'):            # for the desktop pane
        out = []
        try:
            ports = midi_inputs()
        except OSError:
            ports = None
        for d in DEVICES:
            cur = holder(d)
            out.append({'device': d, 'connected': connected(d, ports) if ports is not None else None,
                        'holder': who_of(cur) if cur else None,
                        'purpose': cur.get('purpose') if cur else None,
                        'where': cur.get('where') if cur else None,
                        'since': cur.get('since') if cur else None,
                        'used': cur.get('used') if cur else None,
                        'expires': cur.get('expires') if cur else None,
                        'listening': cur.get('listening') if cur else None,
                        'running': [r['cmd'] for r in live_runs(cur)] if cur else [],
                        'running_since': [r.get('since') for r in live_runs(cur)] if cur else [],
                        'waiting': [{'who': t['who'], 'purpose': t['purpose'], 'since': t['since']}
                                    for t in tickets(d)],
                        'handoff': {k: v for k, v in read_json(path(d, '.handoff.json')).items()
                                    if k != 'nonce'} or None})
        print(json.dumps({'now': now(), 'devices': out}))
        return 0
    for d in DEVICES:
        print(describe(d, holder(d)))
        for t in tickets(d):
            print('  waiting: %s, for "%s", since %s' % (
                t['who'], t['purpose'], time.strftime('%H:%M', time.localtime(t['since']))))
        show_handoff(d)
    return 0


def hook():
    """PreToolUse hook (Claude Code's format; another client with pre-command hooks can feed it the
    same JSON): deny a Bash/PowerShell command that talks to a unit
    unless this session holds that unit's claim; an OS upgrade must also run under
    `elklock run`. An allowed device command counts as use of the claim."""
    try:
        ev = json.load(sys.stdin)
    except ValueError:
        return 0
    if ev.get('tool_name') not in ('Bash', 'PowerShell'):
        return 0
    cmd = (ev.get('tool_input') or {}).get('command', '')
    if re.search(r'(?:elk|dev)lock(\.py)?"?\s+heard\b', cmd, re.I):
        # Only the person ends a listening test: elkbench's listen_done asks them, or they run
        # `heard` themselves outside the AI session.
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': 'Only the person ends a listening test. Use elkbench\'s '
                                        'listen_done, which asks them, after they say in the thread '
                                        'that they have finished.'}}))
        return 0
    run_part = device_part(cmd)
    if not DEVICE_TOOLS.search(run_part) or OFFLINE.search(run_part):
        return 0
    # elklock's own claim/release/renew/status/check, alone on the line: tool names in its
    # --for/--note text are words, not commands.
    if re.fullmatch(r'\s*\S*python\S*\s+"?\S*(?:elk|dev)lock\.py"?\s+(claim|release|renew|status|check|listen)\b[^;&|`$<>]*',
                    cmd, re.I):
        return 0
    if EMULATOR.search(cmd) and not re.search(r'Elektron', cmd):
        return 0          # emulator ports only
    session = ev.get('session_id')
    # the units a command names: its --port when it gives one, else the part that may talk to a
    # unit (a folder such as rytmhealth names no unit)
    ports = re.findall(r'--port[ =]("[^"]*"|\'[^\']*\'|\S+)', run_part, re.I)
    where = ' '.join(ports) if ports else re.sub(r'rytmhealth', '', run_part, flags=re.I)
    named = [d for d, (rx, _) in DEVICES.items() if re.search(rx, where, re.I)]
    holders = {d: holder(d) for d in DEVICES}
    ours = [d for d in DEVICES if mine(holders[d], session=session)]
    missing = [d for d in named if d not in ours] if named else \
        ([] if ours else ['(unit not named in the command)'])
    if ENGINE_TOOLS.search(run_part) and not OFFLINE.search(run_part):
        # Every unit, connected or not: one switched on midway joins the same Engine.
        needed = list(DEVICES)
        missing = sorted((set(missing) | {d for d in needed if d not in ours})
                         - {'(unit not named in the command)'}) \
            or ([] if ours else ['(every connected unit: the Overbridge Engine is shared)'])
    me = os.path.abspath(__file__)
    lines = []
    if missing:
        lines.append('This command talks to an Elektron unit, and this session holds no claim '
                     'on: %s.' % ', '.join(missing))
        for d in named:
            if holders[d] and d not in ours:
                lines.append('BUSY ' + describe(d, holders[d]))
        lines.append('Claim first: python "%s" claim <device> --for "<what you are testing>" '
                     '[--wait] (rytm, a4, digitone, digitakt). If it is busy, wait or tell the '
                     'person; never stop another holder yourself. Release when done.' % me)
    elif any(holders[d] and holders[d].get('listening') for d in (named or ours)):
        for d in (named or ours):
            if holders[d] and holders[d].get('listening'):
                lines.append('%s: the person is listening (%s). Nothing may drive the unit until they '
                             'say in the thread that they have finished; then: python "%s" heard %s '
                             '--by "<where they said it>".' % (d, holders[d]['listening'].get('what', ''),
                                                               me, d))
    elif MUST_RUN.search(run_part) and not re.search(r'(?:elk|dev)lock(\.py)?"?\s+run\b', cmd):
        lines.append('Run OS upgrades under the claim so it cannot end mid-flash: '
                     'python "%s" run <device> -- python -m elkflash upgrade ...' % me)
    if lines:
        print(json.dumps({'hookSpecificOutput': {
            'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
            'permissionDecisionReason': '\n'.join(lines)}}))
        return 0
    for d in (named or ours):
        try:
            update_info(d, lambda i: i.update(used=now()))
        except SystemExit:
            pass
    return 0


def main(argv):
    os.makedirs(DIR, exist_ok=True)
    if argv[:1] == ['_hold']:
        return run_holder(*argv[1:4])
    if argv[:1] == ['hook']:
        return hook()
    fn = {'claim': cmd_claim, 'run': cmd_run, 'renew': cmd_renew, 'release': cmd_release,
          'break': cmd_break, 'check': cmd_check, 'status': cmd_status, 'listen': cmd_listen,
          'heard': cmd_heard}.get(argv[0] if argv else None)
    if not fn:
        print(__doc__)
        return 2
    return fn(argv[1:])


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
