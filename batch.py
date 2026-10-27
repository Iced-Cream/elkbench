"""Batches for elkbench: the same emulator plan or flash run many times across variants, for
counts that mean something. The daemon supplies one run (a fresh emulator, the plan or flash,
an outcome); this file orders the runs, runs them a few at a time and sums them up.

- order: round by round (every variant once per round), shuffled within each round by a
  recorded seed, so whatever else loads the PC lands on every variant alike;
- a cap per batch (parallel) and one for the whole PC (SLOTS), so two sessions' batches share
  the emulators rather than pile up;
- outcomes: pass, fail, hang (the run's time ran out) or error (the run never got going: the
  emulator did not start). Errors are counted but left out of the comparison;
- summary: each variant's pass count with a 95% Wilson interval, and every variant against the
  first by Fisher's exact test (two-sided), Holm-adjusted when there are several. Every run
  planned is run: stopping once the numbers look good would make them look better than they are.

Stdlib only.
"""
import json
import math
import os
import random
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor

SLOTS_DEFAULT = 8                       # emulators batches may run at once on this PC
PARALLEL_DEFAULT = 4                    # at once within one batch
OUTCOMES = ('pass', 'fail', 'hang', 'error')
SLOTS = threading.BoundedSemaphore(SLOTS_DEFAULT)


def set_slots(n):
    global SLOTS
    SLOTS = threading.BoundedSemaphore(max(1, int(n)))


def order(names, runs, seed):
    """-> the variant names in run order: `runs` rounds, each holding every variant once in an
    order shuffled by `seed`."""
    rng = random.Random(seed)
    out = []
    for _ in range(runs):
        r = list(names)
        rng.shuffle(r)
        out += r
    return out


def wilson(k, n, z=1.96):
    """95% Wilson score interval of k successes in n -> (low, high)."""
    if not n:
        return 0.0, 1.0
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def fisher(k1, n1, k2, n2):
    """Two-sided Fisher exact p for k1 of n1 against k2 of n2: the sum of every table with the
    same margins that is no likelier than the one seen."""
    total, col = n1 + n2, k1 + k2
    if not n1 or not n2:
        return 1.0
    prob = lambda a: math.comb(n1, a) * math.comb(n2, col - a) / math.comb(total, col)
    seen = prob(k1)
    lo, hi = max(0, col - n2), min(col, n1)
    return min(1.0, sum(q for q in (prob(a) for a in range(lo, hi + 1)) if q <= seen * (1 + 1e-9)))


def holm(ps):
    """Holm-adjusted p-values, in the order given."""
    m = len(ps)
    idx = sorted(range(m), key=lambda i: ps[i])
    adj, run = [0.0] * m, 0.0
    for rank, i in enumerate(idx):
        run = max(run, min(1.0, (m - rank) * ps[i]))
        adj[i] = run
    return adj


def pct(x):
    return '%d%%' % round(100 * x)


def summarize(variants, results, failures_shown=6):
    """-> the summary lines: one per variant, one per comparison, a few failures. The count of
    lines stays the same however many runs there are."""
    lines, counts = [], []
    for v in variants:
        rs = [r for r in results if r['variant'] == v['name']]
        n = sum(r['outcome'] != 'error' for r in rs)
        k = sum(r['outcome'] == 'pass' for r in rs)
        counts.append((k, n))
        lo, hi = wilson(k, n)
        secs = [r['secs'] for r in rs if r['outcome'] == 'pass']
        extra = ', '.join('%d %s' % (sum(r['outcome'] == o for r in rs), o)
                          for o in ('fail', 'hang', 'error') if any(r['outcome'] == o for r in rs))
        lines.append('%s (%s): %d/%d pass (95%%: %s-%s)%s%s' % (
            v['name'], v['label'], k, n, pct(lo), pct(hi), '; ' + extra if extra else '',
            ', median %.0f s' % statistics.median(secs) if secs else ''))
    if len(variants) > 1:
        k0, n0 = counts[0]
        ps = [fisher(k, n, k0, n0) for k, n in counts[1:]]
        adj = holm(ps)
        for v, p, a in zip(variants[1:], ps, adj):
            lines.append('%s vs %s: p = %s (Fisher exact, two-sided%s)' % (
                v['name'], variants[0]['name'], fmt_p(p),
                ', Holm %s' % fmt_p(a) if len(ps) > 1 else ''))
    bad = [r for r in results if r['outcome'] != 'pass']
    for r in bad[:failures_shown]:
        lines.append('r%02d %s %s: %s' % (r['n'], r['variant'], r['outcome'], r.get('line') or ''))
    if len(bad) > failures_shown:
        lines.append('(%d more not passing: results.jsonl)' % (len(bad) - failures_shown))
    return lines


def fmt_p(p):
    return '%.2g' % p if p >= 0.001 else '<0.001'


class Batch:
    """Runs `run_one(n, variant) -> dict(outcome, secs, line, folder)` for each run in order,
    `parallel` at a time and within the PC's SLOTS, writing results.jsonl in `folder` as each
    run ends. stop() lets the runs going finish their emulator's end and starts no more."""

    def __init__(self, variants, runs, parallel, seed, folder, run_one):
        self.variants, self.runs, self.parallel, self.seed = variants, runs, parallel, seed
        self.folder, self.run_one = folder, run_one
        self.plan = order([v['name'] for v in variants], runs, seed)
        self.results = []
        self.stopped = threading.Event()
        self.lock = threading.Lock()
        self.started = time.time()

    def progress(self):
        with self.lock:
            done = list(self.results)
        per = ', '.join('%s %d/%d' % (v['name'], sum(r['variant'] == v['name'] and r['outcome'] == 'pass'
                                                      for r in done),
                                      sum(r['variant'] == v['name'] for r in done)) for v in self.variants)
        return '%d/%d done (pass/run: %s)' % (len(done), len(self.plan), per)

    def one(self, n, name):
        if self.stopped.is_set():
            return
        with SLOTS:
            if self.stopped.is_set():
                return
            v = next(x for x in self.variants if x['name'] == name)
            t0 = time.time()
            try:
                r = self.run_one(n, v)
            except Exception as e:            # noqa: BLE001 - one run's trouble is its outcome
                r = dict(outcome='error', line='%s: %s' % (type(e).__name__, e))
            r = dict(dict(secs=round(time.time() - t0, 1)), **r, n=n, variant=name)
        with self.lock:
            self.results.append(r)
            with open(os.path.join(self.folder, 'results.jsonl'), 'a', encoding='utf-8') as fh:
                fh.write(json.dumps(r) + '\n')

    def run(self):
        with ThreadPoolExecutor(self.parallel) as ex:
            list(ex.map(lambda a: self.one(*a), enumerate(self.plan)))
        self.results.sort(key=lambda r: r['n'])
        return self.results

    def stop(self):
        self.stopped.set()
