"""ASIO loopback: play loopback.py's test signal on two output channels while recording every
input; report where it came back, its level and the round-trip delay in samples (exact, since
in and out share the ASIO buffer clock).

usage: asio_loop.py <driver-name> <out.wav> [out-left-index] [level-dBFS]
"""
import ctypes as C, struct, sys, time
import numpy as np
import asio_rec as A
from loopback import signal, RATE

name, path = sys.argv[1], sys.argv[2]
oleft = int(sys.argv[3]) if len(sys.argv) > 3 else 0
level = float(sys.argv[4]) if len(sys.argv) > 4 else -24.0

a = A.open_driver(name)
d = A.info(a)
nin, nout, bsz = d["inputs"], d["outputs"], d["buf"][2]
assert {t for *_, t, _ in d["chans"]} == {18}, "expects Int32LSB on every channel"
onames = [n for inp, _, n, _, _ in d["chans"] if not inp]
print(f"{d['name']}: {nin} in, {nout} out, buffer {bsz}; sending on out {oleft}/{oleft + 1}"
      f" ({onames[oleft]}, {onames[oleft + 1]})")

mono, noise = signal(level)
pad = np.zeros(RATE // 2)
play = np.concatenate([pad, mono]).astype(np.float64)
total = len(play) + RATE // 2
playi = np.round(np.clip(play, -1, 1) * (2 ** 31 - 1)).astype("<i4")

infos = (A.BufferInfo * (nin + nout))(*([A.BufferInfo(1, c) for c in range(nin)] +
                                        [A.BufferInfo(0, c) for c in range(nout)]))
blocks, pos = [], [0]
silence = bytes(bsz * 4)

def on_switch(index, direct):
    blocks.append([C.string_at(infos[c].buffers[index], bsz * 4) for c in range(nin)])
    p = pos[0]
    chunk = playi[p:p + bsz]
    chunk = np.pad(chunk, (0, bsz - len(chunk))).tobytes() if len(chunk) else silence
    for c in range(nout):
        C.memmove(infos[nin + c].buffers[index], chunk if c in (oleft, oleft + 1) else silence, bsz * 4)
    pos[0] = p + bsz

def on_ti(params, index, direct):
    on_switch(index, direct)
    return params

def on_msg(sel, val, msg, opt):
    if sel == 1:
        return 1 if val in (2, 3, 4, 5, 6) else 0
    return 2 if sel == 2 else 0

cbs = A.Callbacks(A.BUFSWITCH(on_switch), A.SRCHANGED(lambda r: None), A.MESSAGE(on_msg), A.BUFSWITCHTI(on_ti))
r = a.call("createBuffers", C.c_long, C.cast(infos, C.c_void_p), nin + nout, bsz, C.cast(C.byref(cbs), C.c_void_p))
if r:
    raise SystemExit(f"createBuffers failed: {r}")
a.call("start", C.c_long)
t0 = time.time()
while len(blocks) * bsz < total and time.time() - t0 < 15:
    time.sleep(0.05)
a.call("stop", C.c_long)
a.call("disposeBuffers", C.c_long)

x = np.stack([np.concatenate([np.frombuffer(b[c], "<i4") for b in blocks]) for c in range(nin)], 1) / 2 ** 31
x = x.astype("<f4")
pcm = x.tobytes()
fmt = struct.pack("<HHIIHH", 3, nin, RATE, RATE * nin * 4, nin * 4, 32)
with open(path, "wb") as f:
    f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(pcm)) + b"WAVE")
    f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
    f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
with open(path + ".channels.txt", "w") as f:
    f.write("\n".join(n for inp, _, n, _, _ in d["chans"] if inp))

start = len(pad) + RATE // 2                  # where the noise burst was sent
inames = [n for inp, _, n, _, _ in d["chans"] if inp]
for c in range(nin):
    y = x[:, c].astype(np.float64)
    n = 1 << int(np.ceil(np.log2(len(y) + len(noise))))
    cc = np.fft.irfft(np.fft.rfft(y, n) * np.conj(np.fft.rfft(noise, n)), n)[: len(y)]
    k = int(np.argmax(np.abs(cc)))
    ratio = abs(cc[k]) / (np.median(np.abs(cc)) + 1e-12)
    gain = cc[k] / (noise ** 2).sum()
    found = ratio > 50
    print(f"  in {c:2d} {inames[c]:12} " + (f"back after {k - start} samples ({(k - start) / RATE * 1000:.2f} ms), "
          f"gain {20 * np.log10(abs(gain)):+.1f} dB{' inverted' if gain < 0 else ''}, {ratio:.0f}x" if found
          else f"not found ({ratio:.0f}x)"))
