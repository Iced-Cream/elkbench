"""Play a test signal to a Windows output device while recording a capture device; report
whether it came back, its level and the round-trip delay.

usage: loopback.py list
       loopback.py run <out-index> <in-index> <out.wav> [level-dBFS]
Signal (stereo, 48 kHz float): 0.5 s silence, 1 s white noise, 0.5 s silence, 1 s 1 kHz sine, 1 s silence.
The noise burst is found by cross-correlation, so it works with a pattern playing.
"""
import ctypes as C, ctypes.wintypes as W, sys, threading, time
import numpy as np
import wavein_rec as R

winmm = R.winmm
RATE = 48000

class WAVEOUTCAPSW(C.Structure):
    _fields_ = [("wMid", W.WORD), ("wPid", W.WORD), ("vDriverVersion", W.UINT),
                ("szPname", W.WCHAR * 32), ("dwFormats", W.DWORD), ("wChannels", W.WORD),
                ("wReserved1", W.WORD), ("dwSupport", W.DWORD)]

def outputs():
    out = []
    for i in range(winmm.waveOutGetNumDevs()):
        caps = WAVEOUTCAPSW()
        winmm.waveOutGetDevCapsW(i, C.byref(caps), C.sizeof(caps))
        out.append((i, caps.szPname, caps.wChannels))
    return out

def signal(level_db):
    a = 10 ** (level_db / 20)
    rng = np.random.default_rng(1)
    noise = rng.uniform(-1, 1, RATE) * a
    t = np.arange(RATE) / RATE
    sine = np.sin(2 * np.pi * 1000 * t) * a
    z = lambda s: np.zeros(int(s * RATE))
    mono = np.concatenate([z(0.5), noise, z(0.5), sine, z(1.0)]).astype("<f4")
    return mono, noise

def play(dev, mono):
    pcm = np.repeat(mono, 2).astype("<f4").tobytes()
    fmt = R.WAVEFORMATEX(3, 2, RATE, RATE * 8, 8, 32, 0)
    h = W.HANDLE()
    r = winmm.waveOutOpen(C.byref(h), dev, C.byref(fmt), None, None, 0)
    if r:
        raise SystemExit(f"waveOutOpen failed: MMSYSERR {r}")
    buf = C.create_string_buffer(pcm, len(pcm))
    hd = R.WAVEHDR(C.cast(buf, C.c_void_p), len(pcm), 0, 0, 0, 0, None, 0)
    winmm.waveOutPrepareHeader(h, C.byref(hd), C.sizeof(hd))
    winmm.waveOutWrite(h, C.byref(hd), C.sizeof(hd))
    while not (hd.dwFlags & R.WHDR_DONE):
        time.sleep(0.01)
    winmm.waveOutUnprepareHeader(h, C.byref(hd), C.sizeof(hd))
    winmm.waveOutClose(h)

if __name__ == "__main__":
    if sys.argv[1] == "list":
        print("outputs:"); [print(" ", *d) for d in outputs()]
        print("inputs:"); [print(" ", *d) for d in R.devices()]
        raise SystemExit
    odev, idev, path = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    level = float(sys.argv[5]) if len(sys.argv) > 5 else -24.0
    mono, noise = signal(level)
    got, stamps = {}, {}
    _start = winmm.waveInStart
    def stamped_start(h):            # note when capture really starts, for the delay estimate
        r = _start(h); stamps["rec"] = time.perf_counter(); return r
    winmm.waveInStart = stamped_start
    rec = threading.Thread(target=lambda: got.update(pcm=R.record(idev, len(mono) / RATE + 1.0, 2, RATE)))
    rec.start()
    time.sleep(0.3)
    stamps["play"] = time.perf_counter()
    play(odev, mono)
    rec.join()
    R.write_wav(path, got["pcm"], 2, RATE)
    x = np.frombuffer(got["pcm"], "<f4").reshape(-1, 2).mean(1)
    # cross-correlate with the noise burst (FFT)
    n = 1 << int(np.ceil(np.log2(len(x) + len(noise))))
    cc = np.fft.irfft(np.fft.rfft(x, n) * np.conj(np.fft.rfft(noise, n)), n)[: len(x)]
    k = int(np.argmax(np.abs(cc)))
    floor = np.median(np.abs(cc)) + 1e-12
    gain = cc[k] / (noise ** 2).sum()
    print(f"noise burst found at sample {k} ({k / RATE * 1000:.1f} ms into the recording), "
          f"peak/median {abs(cc[k]) / floor:.0f}x, gain {20 * np.log10(abs(gain) + 1e-12):+.1f} dB"
          f"{' (inverted)' if gain < 0 else ''}")
    # 1 kHz level: sine starts 1.5 s after the noise burst
    s0 = k + int(1.5 * RATE) + 2400
    seg = x[s0: s0 + RATE // 2]
    if len(seg) == RATE // 2:
        f = np.fft.rfftfreq(len(seg), 1 / RATE)
        sp = np.abs(np.fft.rfft(seg * np.hanning(len(seg)))) / (len(seg) / 4)
        i = np.argmin(abs(f - 1000))
        print(f"1 kHz tone back at {20 * np.log10(sp[i - 2:i + 3].max() + 1e-12):.1f} dBFS "
              f"(sent {level:.1f} dBFS)")
    expect = (stamps["play"] - stamps["rec"] + 0.5) * RATE
    print(f"approx. round trip (play call to capture, incl. Windows MME buffering): "
          f"{(k - expect) / RATE * 1000:.0f} ms")
