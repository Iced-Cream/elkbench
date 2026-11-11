"""Headroom: may one more emulator start on this PC now?

Every emulator start (the emulator tool and each batch run) asks first, and a sweep outside
elkbench can ask too (`python headroom.py --wait 600`). A start goes ahead only while

- the whole PC's CPU, averaged over a few seconds, is under `max_cpu` percent,
- at least `min_free_gb` of RAM is free, and
- fewer than `max_emulators` emulator processes run on the PC (anyone's: elkbench's, a sweep's,
  one started by hand).

Off unless config.toml has an [emulator_headroom] table (see config.example.toml); then each
key left out takes the default below. A busy analog-emu takes about one core, so leave room for
what else the PC does. Starts are taken one at a time, and a start made in the last `settle`
seconds counts as one core of load it has not shown yet.

Stdlib only.
"""
import argparse
import ctypes as C
import json
import os
import subprocess
import sys
import threading
import time

WINDOWS = sys.platform == 'win32'
MAX_CPU = 60.0
MIN_FREE_GB = 4.0
MAX_EMULATORS = 10
SAMPLE_SECS = 3.0
SETTLE_SECS = 20.0
# what counts as an emulator process (image names; matched without case)
EMULATOR_IMAGES = ('rytm.exe', 'a4.exe', 'vskip.exe', 'dtboot.exe', 'dtpeek.exe')

_lock = threading.Lock()
_recent = []                  # time.time() of starts let through, for the settle allowance


class Limits:
    def __init__(self, max_cpu=MAX_CPU, min_free_gb=MIN_FREE_GB, max_emulators=MAX_EMULATORS,
                 images=EMULATOR_IMAGES, settle=SETTLE_SECS):
        self.max_cpu, self.min_free_gb, self.max_emulators = float(max_cpu), float(min_free_gb), int(max_emulators)
        self.images, self.settle = tuple(i.lower() for i in images), float(settle)

    @classmethod
    def from_config(cls, c):
        """From config.toml's [emulator_headroom] table (every key optional), or None: no table,
        no check."""
        h = c.get('emulator_headroom')
        if h is None:
            return None
        return cls(h.get('max_cpu', MAX_CPU), h.get('min_free_gb', MIN_FREE_GB),
                   h.get('max_emulators', MAX_EMULATORS), h.get('images', EMULATOR_IMAGES),
                   h.get('settle_secs', SETTLE_SECS))

    def text(self):
        return 'CPU under %g %%, %g GB free, at most %d emulators' % (self.max_cpu, self.min_free_gb,
                                                                    self.max_emulators)


# --- measurements -------------------------------------------------------------------------------

class _FT(C.Structure):
    _fields_ = [('lo', C.c_uint32), ('hi', C.c_uint32)]


def _cpu_times():
    """-> (idle, total) CPU time, any unit, summed over every core."""
    if WINDOWS:
        idle, kern, user = _FT(), _FT(), _FT()
        if not C.windll.kernel32.GetSystemTimes(C.byref(idle), C.byref(kern), C.byref(user)):
            raise OSError('GetSystemTimes failed')
        v = [(f.hi << 32) | f.lo for f in (idle, kern, user)]
        return v[0], v[1] + v[2]               # kernel time includes idle time
    with open('/proc/stat') as fh:
        f = [int(x) for x in fh.readline().split()[1:]]
    return f[3] + (f[4] if len(f) > 4 else 0), sum(f[:8])


def cpu_percent(secs=SAMPLE_SECS):
    """The whole PC's CPU use, percent, averaged over `secs`."""
    i0, t0 = _cpu_times()
    time.sleep(secs)
    i1, t1 = _cpu_times()
    return 0.0 if t1 == t0 else 100.0 * (1 - (i1 - i0) / (t1 - t0))


def free_gb():
    """RAM free for new programs, GB (Windows: available physical memory)."""
    if WINDOWS:
        class MS(C.Structure):
            _fields_ = [('len', C.c_uint32), ('load', C.c_uint32)] + [
                (n, C.c_uint64) for n in ('total', 'avail', 'tpage', 'apage', 'tvirt', 'avirt', 'aext')]
        m = MS()
        m.len = C.sizeof(MS)
        if not C.windll.kernel32.GlobalMemoryStatusEx(C.byref(m)):
            raise OSError('GlobalMemoryStatusEx failed')
        return m.avail / 2 ** 30
    with open('/proc/meminfo') as fh:
        for line in fh:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) / 2 ** 20
    raise OSError('no MemAvailable in /proc/meminfo')


def emulator_count(images=EMULATOR_IMAGES):
    """Emulator processes running on the PC now, anyone's."""
    images = {i.lower() for i in images}
    if WINDOWS:
        r = subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True,
                           creationflags=0x08000000)              # CREATE_NO_WINDOW
        return sum(1 for line in r.stdout.splitlines()
                   if line.startswith('"') and line[1:].split('"', 1)[0].lower() in images)
    stems = {i[:-4] if i.endswith('.exe') else i for i in images}
    n = 0
    for pid in filter(str.isdigit, os.listdir('/proc')):
        try:
            with open('/proc/%s/comm' % pid) as fh:
                n += fh.read().strip() in {s[:15] for s in stems}
        except OSError:
            pass
    return n


# --- the check ----------------------------------------------------------------------------------

def measure(limits, sample=SAMPLE_SECS):
    """-> dict(cpu, free_gb, emulators, pending, ok, why): may one more emulator start now?
    `pending` starts let through within `settle` seconds each count one core of load."""
    now = time.time()
    _recent[:] = [t for t in _recent if now - t < limits.settle]
    cores = os.cpu_count() or 1
    cpu, mem, n = cpu_percent(sample), free_gb(), emulator_count(limits.images)
    pending = len(_recent)
    seen_cpu = cpu + pending * 100.0 / cores
    why = []
    if seen_cpu >= limits.max_cpu:
        why.append('CPU %.0f %%%s (limit %g %%)' % (seen_cpu, ' with %d start(s) still settling' % pending
                                                    if pending else '', limits.max_cpu))
    if mem < limits.min_free_gb:
        why.append('%.1f GB free (needs %g GB)' % (mem, limits.min_free_gb))
    if n >= limits.max_emulators:
        why.append('%d emulators running (at most %d)' % (n, limits.max_emulators))
    return dict(cpu=round(cpu, 1), free_gb=round(mem, 1), emulators=n, pending=pending,
                ok=not why, why='; '.join(why))


def acquire(limits, wait=0.0, stopped=None, poll=5.0, sample=SAMPLE_SECS):
    """Wait up to `wait` seconds (None: no end) for headroom, one start at a time, and record the
    start. -> (True, reading) when it may start, (False, reading) when the wait ran out or
    `stopped` (a threading.Event) was set."""
    end = None if wait is None else time.time() + wait
    while True:
        with _lock:
            r = measure(limits, sample)
            if r['ok']:
                _recent.append(time.time())
                return True, r
        if (stopped is not None and stopped.is_set()) or (end is not None and time.time() + poll > end):
            return False, r
        if stopped is not None:
            stopped.wait(poll)
        else:
            time.sleep(poll)


def main(argv=None):
    p = argparse.ArgumentParser(description='Exit 0 when one more emulator may start on this PC, '
                                            'else 1; prints the reading as JSON.')
    p.add_argument('--wait', type=float, default=0, help='seconds to wait for headroom (default 0)')
    p.add_argument('--max-cpu', type=float, default=MAX_CPU)
    p.add_argument('--min-free-gb', type=float, default=MIN_FREE_GB)
    p.add_argument('--max-emulators', type=int, default=MAX_EMULATORS)
    a = p.parse_args(argv)
    ok, r = acquire(Limits(a.max_cpu, a.min_free_gb, a.max_emulators), a.wait)
    print(json.dumps(r))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
