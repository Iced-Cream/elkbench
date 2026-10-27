"""Summarise a float32 WAV from wavein_rec.py and draw waveform + spectrogram PNG.

usage: analyse.py <in.wav> <out.png> [title]
"""
import struct, sys
import numpy as np
from PIL import Image, ImageDraw

def read_wav(path):
    raw = open(path, "rb").read()
    tag, ch, rate = struct.unpack_from("<HHI", raw, 20)
    pos = raw.index(b"data") + 8
    return rate, np.frombuffer(raw[pos:], "<f4").reshape(-1, ch)

def db(x):
    return 20 * np.log10(np.maximum(x, 1e-12))

def summary(rate, d):
    print(f"{d.shape[0] / rate:.2f} s, {d.shape[1]} ch, {rate} Hz")
    for c in range(d.shape[1]):
        x = d[:, c]
        peak, rms = np.abs(x).max(), np.sqrt((x ** 2).mean())
        print(f"  ch{c + 1}: peak {db(peak):6.1f} dBFS  rms {db(rms):6.1f} dBFS  dc {x.mean():+.5f}"
              f"  clipped {(np.abs(x) >= 0.999).sum()}")
    if d.shape[1] == 2:
        l, r = d[:, 0], d[:, 1]
        den = np.sqrt((l ** 2).sum() * (r ** 2).sum())
        print(f"  L/R correlation {((l * r).sum() / den) if den else 0:+.3f}")
    m = d.mean(1)
    spec = np.abs(np.fft.rfft(m * np.hanning(len(m))))
    f = np.fft.rfftfreq(len(m), 1 / rate)
    top = np.argsort(spec)[::-1]
    peaks = []
    for i in top:
        if f[i] > 20 and all(abs(f[i] - p) > 15 for p in peaks):
            peaks.append(f[i])
        if len(peaks) == 5:
            break
    print("  strongest frequencies (Hz):", ", ".join(f"{p:.0f}" for p in peaks))
    # energy above 20 kHz shows the stream is really 48 kHz wide, not upsampled
    hi = spec[f > 20000].sum() / max(spec.sum(), 1e-12)
    print(f"  share of spectrum above 20 kHz: {hi * 100:.2f}%")

def colour(v):
    # v in 0..1 -> dark blue .. yellow
    stops = np.array([[10, 10, 40], [60, 20, 120], [200, 50, 90], [250, 160, 40], [250, 250, 180]])
    p = v * (len(stops) - 1)
    i = np.clip(p.astype(int), 0, len(stops) - 2)
    t = (p - i)[..., None]
    return (stops[i] * (1 - t) + stops[i + 1] * t).astype(np.uint8)

def draw(rate, d, out, title):
    W, WH, SH = 1200, 120, 360
    nch = d.shape[1]
    img = Image.new("RGB", (W, 30 + nch * WH + SH + 20), (250, 250, 250))
    g = ImageDraw.Draw(img)
    g.text((8, 8), title, fill=(20, 20, 20))
    n = d.shape[0]
    cols = np.array_split(np.arange(n), W)
    for c in range(nch):
        y0 = 30 + c * WH
        mid = y0 + WH // 2
        g.line([(0, mid), (W, mid)], fill=(200, 200, 200))
        for x, idx in enumerate(cols):
            seg = d[idx, c]
            g.line([(x, mid - seg.max() * (WH / 2 - 4)), (x, mid - seg.min() * (WH / 2 - 4))],
                   fill=(40, 90, 170))
        g.text((4, y0 + 2), f"ch{c + 1}", fill=(90, 90, 90))
    # spectrogram of the mono sum, log frequency 30 Hz..24 kHz
    m = d.mean(1)
    nfft, hop = 2048, max(1, (n - 2048) // W)
    win = np.hanning(nfft)
    frames = np.stack([m[i:i + nfft] * win for i in range(0, max(1, n - nfft), hop)][:W])
    S = db(np.abs(np.fft.rfft(frames, axis=1)) / (nfft / 4))
    f = np.fft.rfftfreq(nfft, 1 / rate)
    ys = np.geomspace(30, rate / 2, SH)[::-1]
    rows = np.searchsorted(f, ys).clip(0, len(f) - 1)
    S = S[:, rows].T
    v = np.clip((S + 100) / 100, 0, 1)
    spec = Image.fromarray(colour(v)).resize((W, SH))
    y0 = 30 + nch * WH
    img.paste(spec, (0, y0))
    for hz in (100, 1000, 10000):
        y = y0 + int(np.interp(-np.log(hz), -np.log(ys), np.arange(SH)))
        g.text((4, y - 6), f"{hz // 1000}k" if hz >= 1000 else str(hz), fill=(255, 255, 255))
    g.text((4, y0 + SH + 4), f"{n / rate:.1f} s, {rate} Hz, spectrogram -100..0 dBFS", fill=(90, 90, 90))
    img.save(out)

if __name__ == "__main__":
    rate, d = read_wav(sys.argv[1])
    summary(rate, d)
    draw(rate, d, sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else sys.argv[1])
