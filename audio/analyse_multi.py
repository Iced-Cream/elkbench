"""Per-channel summary and stacked waveform/mini-spectrogram PNG for a multichannel float WAV.

usage: analyse_multi.py <in.wav> <out.png> [title]   (names from <in.wav>.channels.txt)
"""
import sys
import numpy as np
from PIL import Image, ImageDraw
from analyse import read_wav, db, colour

rate, d = read_wav(sys.argv[1])
names = open(sys.argv[1] + ".channels.txt").read().split("\n")
title = sys.argv[3] if len(sys.argv) > 3 else sys.argv[1]
n, nch = d.shape
print(f"{n / rate:.2f} s, {nch} ch, {rate} Hz")
h = 480
for c in range(nch):
    x = d[:, c]
    peak, rms = np.abs(x).max(), np.sqrt((x ** 2).mean())
    spec = np.abs(np.fft.rfft(x * np.hanning(n)))
    f = np.fft.rfftfreq(n, 1 / rate)
    cen = (spec * f).sum() / max(spec.sum(), 1e-12)
    pk = f[np.argmax(spec * (f > 20))]
    e = np.sqrt((x[: n // h * h].reshape(-1, h) ** 2).mean(1))
    hits = int(((e[1:] > 4 * e[:-1] + 1e-6) & (e[1:] > rms * 0.5)).sum())
    print(f"  {c + 1:2d} {names[c]:12} peak {db(peak):6.1f}  rms {db(rms):6.1f} dBFS"
          f"  peak freq {pk:6.0f} Hz  centroid {cen:6.0f} Hz  onsets {hits}")

W, RH, LW = 1300, 70, 100
img = Image.new("RGB", (W, 30 + nch * RH + 20), (250, 250, 250))
g = ImageDraw.Draw(img)
g.text((8, 8), title, fill=(20, 20, 20))
half = (W - LW) // 2
cols = np.array_split(np.arange(n), half)
nfft = 1024
hop = max(1, (n - nfft) // half)
win = np.hanning(nfft)
f = np.fft.rfftfreq(nfft, 1 / rate)
ys = np.geomspace(30, rate / 2, RH - 6)[::-1]
rows = np.searchsorted(f, ys).clip(0, len(f) - 1)
for c in range(nch):
    y0 = 30 + c * RH
    x = d[:, c]
    peak = np.abs(x).max()
    g.text((4, y0 + 4), names[c], fill=(40, 40, 40))
    g.text((4, y0 + 20), f"{db(peak):.0f} dBFS pk", fill=(110, 110, 110))
    mid, amp = y0 + RH // 2, RH / 2 - 4
    g.line([(LW, mid), (LW + half, mid)], fill=(210, 210, 210))
    for i, idx in enumerate(cols):     # absolute scale, so quiet tracks look quiet
        seg = x[idx]
        g.line([(LW + i, mid - seg.max() * amp), (LW + i, mid - seg.min() * amp)], fill=(40, 90, 170))
    frames = np.stack([x[i:i + nfft] * win for i in range(0, n - nfft, hop)][:half])
    S = db(np.abs(np.fft.rfft(frames, axis=1)) / (nfft / 4))[:, rows].T
    img.paste(Image.fromarray(colour(np.clip((S + 100) / 100, 0, 1))), (LW + half + 4, y0 + 3))
g.text((LW, 30 + nch * RH + 4), "waveform (full scale = 0 dBFS)", fill=(90, 90, 90))
g.text((LW + half + 4, 30 + nch * RH + 4), "spectrogram 30 Hz-24 kHz log, -100..0 dBFS", fill=(90, 90, 90))
img.save(sys.argv[2])

# do the track outputs add up to the main mix? (main also carries FX and the inputs)
tracks = d[:, 2:10].sum(1)
main = d[:, :2].mean(1)
den = np.sqrt((tracks ** 2).sum() * (main ** 2).sum())
print(f"  corr(sum of tracks, main mono) {((tracks * main).sum() / den) if den else 0:+.3f}")
