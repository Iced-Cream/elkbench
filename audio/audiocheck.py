"""Pass/fail checks on recordings from asio_rec.py / wavein_rec.py.

usage: audiocheck.py dropouts <in.wav> [--ch 1,2] [--json out.json] [--quiet [--log F]]
       audiocheck.py compare  <ref.wav> <test.wav> [--ch 1,2] [--tol 1.5] [--png out.png] [--quiet]

The last line is the verdict (PASS or FAIL with the worst number); --quiet
prints only that line and sends the rest to --log.

dropouts finds three kinds of fault on each channel and exits 1 if there is any:
  gap    a run of digital silence (>= --gap samples, default 32 = one render
         block) with sound on both sides
  click  a sample the signal's own short-term prediction cannot explain:
         the residual of a 32-tap linear predictor, fitted per 100 ms block,
         above --click (default 100; 0 = off) times both the block's median residual and the median
         of the next 5 ms, where the 5 ms before were already
         sounding (above -50 dBFS) at no more than 10 dB below the 5 ms after;
         so a new sound's onset is not a click, and neither is a click that
         lands on one (use gap and compare for those)
  dip    the 5 ms level falls more than --dip dB (default 30) below the
         surrounding 200 ms level and comes back within 50 ms
A sample skipped or repeated in a sustained sound scores several hundred.
Real material scores up to about 50 on drum attacks over a bed and on mains
buzz, and a raw saw or pulse oscillator on its own track output scores up to
about 200 once per cycle (a true discontinuity). So judge clicks on a steady
signal: a sine played into the unit's USB input and recorded back from its
main out (loopback.py / asio_loop.py) while the test runs, or a held pad; on
drum and synth tracks rely on gap, dip and compare.

compare is an A/B against a reference recording of the same material (stock
build vs test build): RMS level per channel, then the long-term spectrum in
1/3-octave bands from 31.5 Hz to 16 kHz, both normalised to the reference's
level unless --absolute. Bands more than 40 dB below the loudest are not
judged. Exits 1 if any judged band differs by more than --tol dB (or the
level by more than --level-tol dB with --absolute).
"""
import argparse, json, os, struct, sys
import numpy as np
from scipy import ndimage, signal
from scipy.linalg import solve_toeplitz


def read_wav(path):
    """-> rate, float array (samples, channels). PCM 16/24/32 or float32/64."""
    raw = open(path, "rb").read()
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"{path}: not a WAV file")
    pos, fmt, data = 12, None, None
    while pos + 8 <= len(raw):
        tag, size = raw[pos:pos + 4], struct.unpack_from("<I", raw, pos + 4)[0]
        body = raw[pos + 8:pos + 8 + size]
        if tag == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", body)
            if fmt[0] == 0xFFFE:               # WAVE_FORMAT_EXTENSIBLE: subformat's first u16
                fmt = (struct.unpack_from("<H", body, 24)[0],) + fmt[1:]
        elif tag == b"data":
            data = body
        pos += 8 + size + (size & 1)
    kind, ch, rate, _, _, bits = fmt
    if kind == 3:
        x = np.frombuffer(data, "<f4" if bits == 32 else "<f8").astype(np.float64)
    elif bits == 24:
        b = np.frombuffer(data[:len(data) // 3 * 3], np.uint8).reshape(-1, 3)
        x = ((b[:, 0].astype(np.int32) | b[:, 1].astype(np.int32) << 8 | b[:, 2].astype(np.int32) << 16)
             << 8 >> 8) / 2 ** 23
    else:
        x = np.frombuffer(data, {16: "<i2", 32: "<i4"}[bits]) / 2 ** (bits - 1)
    return rate, x[:len(x) // ch * ch].reshape(-1, ch)


def db(x):
    return 20 * np.log10(np.maximum(x, 1e-12))


def channels(arg, n):
    return list(range(n)) if not arg else [int(c) - 1 for c in arg.split(",")]


# --- dropouts -----------------------------------------------------------------

def find_gaps(x, rate, min_len=32, floor=1e-7, sound=-60):
    """Runs of |x| <= floor of at least min_len samples with sound around them."""
    quiet = np.abs(x) <= floor
    edges = np.diff(np.concatenate(([0], quiet.astype(np.int8), [0])))
    starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
    win = int(0.02 * rate)
    out = []
    for s, e in zip(starts, ends):
        if e - s < min_len or s == 0 or e == len(x):
            continue
        before, after = x[max(0, s - win):s], x[e:e + win]
        if db(np.sqrt(np.mean(before ** 2))) > sound and db(np.sqrt(np.mean(after ** 2))) > sound:
            out.append({"kind": "gap", "t": s / rate, "samples": int(e - s)})
    return out


def find_clicks(x, rate, order=32, factor=100.0, sound=-50, rise=10.0):
    """Residual of a per-block linear predictor, against the block's median."""
    block = int(0.1 * rate)
    out = []
    for b0 in range(0, len(x) - order, block):
        seg = x[b0:b0 + block + order]
        if len(seg) < 4 * order or db(np.sqrt(np.mean(seg ** 2))) < sound:
            continue
        r = np.correlate(seg, seg, "full")[len(seg) - 1:len(seg) + order]
        if r[0] <= 0:
            continue
        r[0] *= 1 + 1e-9
        try:
            a = solve_toeplitz(r[:order], r[1:order + 1])
        except np.linalg.LinAlgError:
            continue
        e = signal.lfilter(np.concatenate(([1], -a)), [1], seg)[order:]
        mag = np.abs(e)
        med = np.median(mag)
        if med <= 0:
            continue
        # against the block and the next 5 ms: an isolated sample stands out,
        # the start of a noisy sound (a hat) does not
        after = ndimage.median_filter(mag, size=int(0.005 * rate), mode="nearest")
        after = np.concatenate((after[int(0.0025 * rate) + 3:], np.full(int(0.0025 * rate) + 3, med)))
        hits = np.flatnonzero(mag > factor * np.maximum(med, after))
        last, w = -rate, int(0.005 * rate)
        for h in hits:
            # only inside sound that was already going at about the same
            # level: a hit after silence or over a quiet bed is a new sound
            k = order + h
            pre, post = seg[max(0, k - w):k - 1], seg[k + 2:k + w]
            if len(pre) < w // 2 or len(post) < w // 2:
                continue
            lpre, lpost = db(np.sqrt(np.mean(pre ** 2))), db(np.sqrt(np.mean(post ** 2)))
            if lpre < sound or lpost - lpre > rise:
                continue
            if h - last > w:                      # one report per 5 ms
                out.append({"kind": "click", "t": (b0 + order + h) / rate,
                            "ratio": round(float(mag[h] / med), 1)})
            last = h
    return out


def find_dips(x, rate, depth=30.0, sound=-50):
    """5 ms level against the median of the surrounding 200 ms."""
    w = int(0.005 * rate)
    n = len(x) // w
    if n < 50:
        return []
    lvl = db(np.sqrt(np.mean(x[:n * w].reshape(n, w) ** 2, axis=1)))
    ctx = np.array([np.median(lvl[max(0, i - 20):i + 21]) for i in range(n)])
    low = (lvl < ctx - depth) & (ctx > sound)
    out, i = [], 0
    while i < n:
        if low[i]:
            j = i
            while j < n and low[j]:
                j += 1
            if j - i <= 10 and i > 0 and j < n:   # back within 50 ms
                out.append({"kind": "dip", "t": i * w / rate, "ms": (j - i) * 5,
                            "depth_db": round(float(ctx[i] - lvl[i:j].min()), 1)})
            i = j
        i += 1
    return out


def dropouts(x, rate, gap=32, click=100.0, dip=30.0):
    found = find_gaps(x, rate, gap) + (find_clicks(x, rate, factor=click) if click else []) + find_dips(x, rate, dip)
    return sorted(found, key=lambda f: f["t"])


# --- A/B ----------------------------------------------------------------------

BANDS = 1000 * 2.0 ** (np.arange(-15, 13) / 3)     # 31.5 Hz .. 16 kHz


def band_levels(x, rate):
    f, p = signal.welch(x, rate, nperseg=8192)
    out = []
    for fc in BANDS:
        m = (f >= fc / 2 ** (1 / 6)) & (f < fc * 2 ** (1 / 6))
        out.append(10 * np.log10(max(p[m].sum(), 1e-24)))
    return np.array(out)


def compare(ref, test, rate, tol=1.5, absolute=False, level_tol=1.0, span=40.0):
    lr, lt = db(np.sqrt(np.mean(ref ** 2))), db(np.sqrt(np.mean(test ** 2)))
    br, bt = band_levels(ref, rate), band_levels(test, rate)
    if not absolute:
        bt = bt - (lt - lr)
    judged = br > br.max() - span
    diff = bt - br
    worst = int(np.argmax(np.where(judged, np.abs(diff), -1)))
    ok = bool(np.all(np.abs(diff[judged]) <= tol)) and (not absolute or abs(lt - lr) <= level_tol)
    return {"ref_rms_db": round(float(lr), 2), "test_rms_db": round(float(lt), 2),
            "level_diff_db": round(float(lt - lr), 2), "bands_hz": [round(float(b), 1) for b in BANDS],
            "band_diff_db": [round(float(d), 2) if j else None for d, j in zip(diff, judged)],
            "worst_band_hz": round(float(BANDS[worst]), 1), "worst_diff_db": round(float(diff[worst]), 2),
            "pass": ok, "ref_bands": br, "test_bands": bt, "judged": judged}


def plot(results, out, title):
    from PIL import Image, ImageDraw
    W, H, top = 900, 300, 30
    img = Image.new("RGB", (W, top + H * len(results) + 10), (250, 250, 250))
    g = ImageDraw.Draw(img)
    g.text((8, 8), title, fill=(20, 20, 20))
    for k, (name, r) in enumerate(results):
        y0 = top + k * H
        hi = max(r["ref_bands"].max(), r["test_bands"].max())
        xs = np.linspace(40, W - 20, len(BANDS))
        ys = lambda v: y0 + 20 + (hi - v) / 80 * (H - 50)
        for v in range(0, 81, 20):
            g.line([(40, ys(hi - v)), (W - 20, ys(hi - v))], fill=(220, 220, 220))
            g.text((4, ys(hi - v) - 6), f"-{v}", fill=(120, 120, 120))
        for bands, col in ((r["ref_bands"], (40, 90, 170)), (r["test_bands"], (200, 60, 60))):
            g.line([(x, ys(min(max(v, hi - 80), hi))) for x, v in zip(xs, bands)], fill=col, width=2)
        for x, fc, j in zip(xs, BANDS, r["judged"]):
            if fc in (BANDS[0], BANDS[9], BANDS[18], BANDS[27]):
                g.text((x - 10, y0 + H - 26), f"{fc:.0f}", fill=(90, 90, 90))
        g.text((44, y0 + 4), f"{name}: ref blue, test red; worst {r['worst_diff_db']:+.1f} dB at "
               f"{r['worst_band_hz']:.0f} Hz; {'PASS' if r['pass'] else 'FAIL'}", fill=(20, 20, 20))
    img.save(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0], epilog=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("dropouts")
    p.add_argument("wav")
    p.add_argument("--ch")
    p.add_argument("--gap", type=int, default=32)
    p.add_argument("--click", type=float, default=100.0, help="0 turns click detection off")
    p.add_argument("--dip", type=float, default=30.0)
    p.add_argument("--json")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--log")
    p = sub.add_parser("compare")
    p.add_argument("ref")
    p.add_argument("test")
    p.add_argument("--ch")
    p.add_argument("--tol", type=float, default=1.5)
    p.add_argument("--absolute", action="store_true")
    p.add_argument("--level-tol", type=float, default=1.0)
    p.add_argument("--png")
    p.add_argument("--json")
    p.add_argument("--quiet", action="store_true")
    p.add_argument("--log")
    a = ap.parse_args(argv)
    if not a.quiet:
        rc, verdict = run(a)
        print(verdict)
        return rc
    out = sys.stdout
    sys.stdout = open(a.log, "a", encoding="utf-8") if a.log else open(os.devnull, "w")
    try:
        rc, verdict = run(a)
        print(verdict)
    finally:
        sys.stdout.close()
        sys.stdout = out
    print(verdict)
    return rc


def run(a):
    """-> (exit code, the verdict line)."""

    if a.cmd == "dropouts":
        rate, d = read_wav(a.wav)
        report, bad = {}, 0
        for c in channels(a.ch, d.shape[1]):
            found = dropouts(d[:, c], rate, a.gap, a.click, a.dip)
            report[f"ch{c + 1}"] = found
            bad += len(found)
            print(f"ch{c + 1}: {len(found)} faults" + "".join(
                f"\n  {f['t']:9.4f} s  {f['kind']:5s}  " + ", ".join(f"{k} {v}" for k, v in f.items()
                                                                  if k not in ("t", "kind"))
                for f in found[:50]) + ("\n  ..." if len(found) > 50 else ""))
        if a.json:
            json.dump(report, open(a.json, "w"), indent=1)
        if not bad:
            return 0, "PASS: no dropouts (%s)" % ", ".join(report)
        ch, f = next((c, fs[0]) for c, fs in report.items() if fs)
        return 1, f"FAIL: {bad} faults, first a {f['kind']} at {f['t']:.3f} s on {ch}"

    rr, dr = read_wav(a.ref)
    rt, dt = read_wav(a.test)
    if rr != rt or dr.shape[1] != dt.shape[1]:
        return 1, f"FAIL: formats differ ({rr} Hz x {dr.shape[1]} vs {rt} Hz x {dt.shape[1]})"
    results = []
    for c in channels(a.ch, dr.shape[1]):
        r = compare(dr[:, c], dt[:, c], rr, a.tol, a.absolute, a.level_tol)
        results.append((f"ch{c + 1}", r))
        print(f"ch{c + 1}: rms ref {r['ref_rms_db']:.1f} test {r['test_rms_db']:.1f} dBFS "
              f"({r['level_diff_db']:+.2f}); worst band {r['worst_band_hz']:.0f} Hz "
              f"{r['worst_diff_db']:+.2f} dB; {'pass' if r['pass'] else 'FAIL'}")
    if a.png:
        plot(results, a.png, f"{os.path.basename(a.ref)} (ref) vs {os.path.basename(a.test)} (test)")
    if a.json:
        json.dump({n: {k: v for k, v in r.items() if k not in ("ref_bands", "test_bands", "judged")}
                   for n, r in results}, open(a.json, "w"), indent=1)
    ok = all(r["pass"] for _, r in results)
    n, w = max(results, key=lambda nr: abs(nr[1]["worst_diff_db"]))
    return (0 if ok else 1), ("%s: worst band %.0f Hz %+.2f dB (%s), level %+.2f dB" % (
        "PASS" if ok else "FAIL", w["worst_band_hz"], w["worst_diff_db"], n, w["level_diff_db"]))


if __name__ == "__main__":
    sys.exit(main())
