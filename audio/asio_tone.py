"""ASIO: play a steady sine on two output channels for N seconds while recording every input,
for audiocheck.py dropouts (a sustained tone makes any gap or skipped block stand out).

usage: asio_tone.py <driver-name> <out.wav> <seconds> [out-left-index] [level-dBFS] [freq-Hz]
Writes 32-bit float WAV of all inputs (names in <out>.channels.txt), like asio_loop.py.
"""
import os, sys
if os.name != "nt":                # no ASIO here: PortAudio (sdaudio.py), same arguments
    import sdaudio
    sys.exit(sdaudio.main_tone(sys.argv[1:]))
import ctypes as C, struct, time
import numpy as np
import asio_rec as A

RATE = 48000
name, path, seconds = sys.argv[1], sys.argv[2], float(sys.argv[3])
oleft = int(sys.argv[4]) if len(sys.argv) > 4 else 0
level = float(sys.argv[5]) if len(sys.argv) > 5 else -18.0
freq = float(sys.argv[6]) if len(sys.argv) > 6 else 997.0

a = A.open_driver(name)
d = A.info(a)
nin, nout, bsz = d["inputs"], d["outputs"], d["buf"][2]
assert {t for *_, t, _ in d["chans"]} == {18}, "expects Int32LSB on every channel"
onames = [n for inp, _, n, _, _ in d["chans"] if not inp]
print(f"{d['name']}: {nin} in, {nout} out, buffer {bsz}; {freq:g} Hz at {level:g} dBFS for {seconds:g} s "
      f"on out {oleft}/{oleft + 1} ({onames[oleft]}, {onames[oleft + 1]})")

n = int(seconds * RATE)
t = np.arange(n) / RATE
ramp = np.minimum(1, np.minimum(t, t[::-1]) / 0.02)       # 20 ms fades
play = 10 ** (level / 20) * np.sin(2 * np.pi * freq * t) * ramp
playi = np.round(np.clip(play, -1, 1) * (2 ** 31 - 1)).astype("<i4")
total = n + RATE // 2

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
while len(blocks) * bsz < total and time.time() - t0 < seconds + 10:
    time.sleep(0.05)
a.call("stop", C.c_long)
a.call("disposeBuffers", C.c_long)

x = (np.stack([np.concatenate([np.frombuffer(b[c], "<i4") for b in blocks]) for c in range(nin)], 1)
     / 2 ** 31).astype("<f4")
pcm = x.tobytes()
fmt = struct.pack("<HHIIHH", 3, nin, RATE, RATE * nin * 4, nin * 4, 32)
with open(path, "wb") as f:
    f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(pcm)) + b"WAVE")
    f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
    f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
with open(path + ".channels.txt", "w") as f:
    f.write("\n".join(n for inp, _, n, _, _ in d["chans"] if inp))
expected = total // bsz
print(f"recorded {len(blocks)} buffers of {bsz} ({len(blocks) * bsz / RATE:.2f} s); expected about {expected}")
