"""Record from a Windows capture device through winmm waveIn (stdlib only).

usage: wavein_rec.py list
       wavein_rec.py rec <name-substring> <seconds> <out.wav> [channels] [rate]
Records 32-bit float; writes a WAVE_FORMAT_IEEE_FLOAT .wav.
"""
import ctypes as C, ctypes.wintypes as W, struct, sys, time

winmm = C.WinDLL("winmm")

class WAVEINCAPSW(C.Structure):
    _fields_ = [("wMid", W.WORD), ("wPid", W.WORD), ("vDriverVersion", W.UINT),
                ("szPname", W.WCHAR * 32), ("dwFormats", W.DWORD),
                ("wChannels", W.WORD), ("wReserved1", W.WORD)]

class WAVEFORMATEX(C.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", W.WORD), ("nChannels", W.WORD), ("nSamplesPerSec", W.DWORD),
                ("nAvgBytesPerSec", W.DWORD), ("nBlockAlign", W.WORD),
                ("wBitsPerSample", W.WORD), ("cbSize", W.WORD)]

class WAVEHDR(C.Structure):
    pass
WAVEHDR._fields_ = [("lpData", C.c_void_p), ("dwBufferLength", W.DWORD),
                    ("dwBytesRecorded", W.DWORD), ("dwUser", C.c_size_t),
                    ("dwFlags", W.DWORD), ("dwLoops", W.DWORD),
                    ("lpNext", C.c_void_p), ("reserved", C.c_size_t)]
WHDR_DONE = 1

def devices():
    out = []
    for i in range(winmm.waveInGetNumDevs()):
        caps = WAVEINCAPSW()
        winmm.waveInGetDevCapsW(i, C.byref(caps), C.sizeof(caps))
        out.append((i, caps.szPname, caps.wChannels))
    return out

def record(dev, seconds, ch, rate):
    fmt = WAVEFORMATEX(3, ch, rate, rate * ch * 4, ch * 4, 32, 0)
    h = W.HANDLE()
    r = winmm.waveInOpen(C.byref(h), dev, C.byref(fmt), None, None, 0)
    if r:
        raise SystemExit(f"waveInOpen failed: MMSYSERR {r}")
    nbuf, blen = 8, rate // 10 * ch * 4          # 100 ms buffers
    bufs = [C.create_string_buffer(blen) for _ in range(nbuf)]
    hdrs = [WAVEHDR(C.cast(b, C.c_void_p), blen, 0, 0, 0, 0, None, 0) for b in bufs]
    for hd in hdrs:
        winmm.waveInPrepareHeader(h, C.byref(hd), C.sizeof(hd))
        winmm.waveInAddBuffer(h, C.byref(hd), C.sizeof(hd))
    data, want, i = bytearray(), int(seconds * rate) * ch * 4, 0
    winmm.waveInStart(h)
    while len(data) < want:
        hd = hdrs[i]
        while not (hd.dwFlags & WHDR_DONE):
            time.sleep(0.005)
        data += C.string_at(bufs[i], hd.dwBytesRecorded)
        hd.dwFlags &= ~WHDR_DONE
        winmm.waveInAddBuffer(h, C.byref(hd), C.sizeof(hd))
        i = (i + 1) % nbuf
    winmm.waveInStop(h); winmm.waveInReset(h)
    for hd in hdrs:
        winmm.waveInUnprepareHeader(h, C.byref(hd), C.sizeof(hd))
    winmm.waveInClose(h)
    return bytes(data[:want])

def write_wav(path, pcm, ch, rate):
    fmt = struct.pack("<HHIIHH", 3, ch, rate, rate * ch * 4, ch * 4, 32)
    with open(path, "wb") as f:
        f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(pcm)) + b"WAVE")
        f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
        f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)

if __name__ == "__main__":
    if sys.argv[1] == "list":
        for d in devices():
            print(*d)
    else:
        name, secs, out = sys.argv[2], float(sys.argv[3]), sys.argv[4]
        ch = int(sys.argv[5]) if len(sys.argv) > 5 else 2
        rate = int(sys.argv[6]) if len(sys.argv) > 6 else 48000
        # "#<n>" picks by index: MME names are cut to 31 characters, so two units can look alike
        dev = int(name[1:]) if name.startswith("#") else \
            next(i for i, n, _ in devices() if name.lower() in n.lower())
        write_wav(out, record(dev, secs, ch, rate), ch, rate)
        print("wrote", out)
