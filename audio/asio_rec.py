"""Minimal ASIO host in ctypes: list channels or record every input to a multichannel WAV.

usage: asio_rec.py info <driver-name>
       asio_rec.py rec  <driver-name> <seconds> <out.wav>
Writes 32-bit float, all input channels interleaved; channel names go to <out>.channels.txt.
Without ASIO (Linux) the same commands go through PortAudio: sdaudio.py.
"""
import os, sys
if os.name != "nt" and __name__ == "__main__":
    import sdaudio
    sys.exit(sdaudio.main_rec(sys.argv[1:]))
import ctypes as C, struct, time, winreg

ole = C.OleDLL("ole32")

class GUID(C.Structure):
    _fields_ = [("d1", C.c_uint32), ("d2", C.c_uint16), ("d3", C.c_uint16), ("d4", C.c_ubyte * 8)]

def guid(s):
    g = GUID()
    ole.CLSIDFromString(C.c_wchar_p(s), C.byref(g))
    return g

class ChannelInfo(C.Structure):
    _fields_ = [("channel", C.c_long), ("isInput", C.c_long), ("isActive", C.c_long),
                ("channelGroup", C.c_long), ("type", C.c_long), ("name", C.c_char * 32)]

class BufferInfo(C.Structure):
    _fields_ = [("isInput", C.c_long), ("channelNum", C.c_long), ("buffers", C.c_void_p * 2)]

BUFSWITCH = C.CFUNCTYPE(None, C.c_long, C.c_long)
SRCHANGED = C.CFUNCTYPE(None, C.c_double)
MESSAGE = C.CFUNCTYPE(C.c_long, C.c_long, C.c_long, C.c_void_p, C.c_void_p)
BUFSWITCHTI = C.CFUNCTYPE(C.c_void_p, C.c_void_p, C.c_long, C.c_long)

class Callbacks(C.Structure):
    _fields_ = [("bufferSwitch", BUFSWITCH), ("sampleRateDidChange", SRCHANGED),
                ("asioMessage", MESSAGE), ("bufferSwitchTimeInfo", BUFSWITCHTI)]

TYPES = {16: ("Int16LSB", 2), 17: ("Int24LSB", 3), 18: ("Int32LSB", 4), 19: ("Float32LSB", 4)}

class Asio:
    # IASIO vtable slots after IUnknown
    names = ["init", "getDriverName", "getDriverVersion", "getErrorMessage", "start", "stop",
             "getChannels", "getLatencies", "getBufferSize", "canSampleRate", "getSampleRate",
             "setSampleRate", "getClockSources", "setClockSource", "getSamplePosition",
             "getChannelInfo", "createBuffers", "disposeBuffers", "controlPanel", "future",
             "outputReady"]

    def __init__(self, name):
        k = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\ASIO\{name}")
        clsid = guid(winreg.QueryValueEx(k, "CLSID")[0])
        ole.CoInitialize(None)
        self.p = C.c_void_p()
        ole.CoCreateInstance(C.byref(clsid), None, 1, C.byref(clsid), C.byref(self.p))
        self.vt = C.cast(C.cast(self.p, C.POINTER(C.c_void_p))[0], C.POINTER(C.c_void_p))

    def call(self, name, restype, *args):
        idx = 3 + self.names.index(name)
        argtypes = [C.c_void_p] + [C.c_long if isinstance(a, int) else
                                   C.c_double if isinstance(a, float) else C.c_void_p for a in args]
        f = C.WINFUNCTYPE(restype, *argtypes)(self.vt[idx])
        return f(self.p, *args)

def open_driver(name):
    a = Asio(name)
    if not a.call("init", C.c_long, C.c_void_p(0)):
        msg = C.create_string_buffer(128)
        a.call("getErrorMessage", None, msg)
        raise SystemExit(f"ASIO init failed: {msg.value.decode(errors='replace')}")
    return a

def info(a):
    nm = C.create_string_buffer(32); a.call("getDriverName", None, nm)
    ni, no = C.c_long(), C.c_long(); a.call("getChannels", C.c_long, C.byref(ni), C.byref(no))
    sr = C.c_double(); a.call("getSampleRate", C.c_long, C.byref(sr))
    bmin, bmax, bpref, bgran = (C.c_long() for _ in range(4))
    a.call("getBufferSize", C.c_long, *(C.byref(x) for x in (bmin, bmax, bpref, bgran)))
    li, lo = C.c_long(), C.c_long(); a.call("getLatencies", C.c_long, C.byref(li), C.byref(lo))
    chans = []
    for inp, n in ((1, ni.value), (0, no.value)):
        for c in range(n):
            ci = ChannelInfo(c, inp)
            a.call("getChannelInfo", C.c_long, C.byref(ci))
            chans.append((inp, c, ci.name.decode(errors="replace"), ci.type, ci.channelGroup))
    return dict(name=nm.value.decode(), inputs=ni.value, outputs=no.value, rate=sr.value,
                buf=(bmin.value, bmax.value, bpref.value, bgran.value),
                latency=(li.value, lo.value), chans=chans)

def record(a, d, seconds):
    nin, bsz = d["inputs"], d["buf"][2]
    infos = (BufferInfo * nin)(*[BufferInfo(1, c) for c in range(nin)])
    types = {t for inp, _, _, t, _ in d["chans"] if inp}
    assert len(types) == 1, types
    tname, width = TYPES[types.pop()]
    blocks, swaps = [], [0]

    def on_switch(index, direct):
        swaps[0] += 1
        blocks.append([C.string_at(infos[c].buffers[index], bsz * width) for c in range(nin)])

    def on_switch_ti(params, index, direct):
        on_switch(index, direct)
        return params

    def on_message(sel, val, msg, opt):
        if sel == 1:                       # kAsioSelectorSupported
            return 1 if val in (2, 3, 4, 5, 6) else 0
        if sel == 2:                       # kAsioEngineVersion
            return 2
        print("asioMessage", sel, val)
        return 0

    cbs = Callbacks(BUFSWITCH(on_switch), SRCHANGED(lambda r: print("rate ->", r)),
                    MESSAGE(on_message), BUFSWITCHTI(on_switch_ti))
    r = a.call("createBuffers", C.c_long, C.cast(infos, C.c_void_p), nin, bsz, C.cast(C.byref(cbs), C.c_void_p))
    if r != 0:
        raise SystemExit(f"createBuffers failed: {r}")
    a.call("start", C.c_long)
    need = int(seconds * d["rate"] / bsz) + 1
    t0 = time.time()
    while len(blocks) < need and time.time() - t0 < seconds + 5:
        time.sleep(0.05)
    a.call("stop", C.c_long)
    a.call("disposeBuffers", C.c_long)
    return blocks, tname, width, time.time() - t0

def to_float(raw, tname):
    import numpy as np
    if tname == "Float32LSB":
        return np.frombuffer(raw, "<f4")
    if tname == "Int32LSB":
        return np.frombuffer(raw, "<i4") / 2 ** 31
    if tname == "Int16LSB":
        return np.frombuffer(raw, "<i2") / 2 ** 15
    b = np.frombuffer(raw, np.uint8).reshape(-1, 3).astype(np.int32)
    v = b[:, 0] | b[:, 1] << 8 | b[:, 2] << 16
    return np.where(v >= 1 << 23, v - (1 << 24), v) / 2 ** 23

if __name__ == "__main__":
    import numpy as np
    a = open_driver(sys.argv[2])
    d = info(a)
    print({k: v for k, v in d.items() if k != "chans"})
    for inp, c, name, t, g in d["chans"]:
        print(f"  {'in ' if inp else 'out'} {c:2d} {name!r:28} type {TYPES.get(t, (t,))[0]} group {g}")
    if sys.argv[1] == "rec":
        secs, out = float(sys.argv[3]), sys.argv[4]
        blocks, tname, width, took = record(a, d, secs)
        print(f"{len(blocks)} buffers of {d['buf'][2]} frames in {took:.2f} s")
        nin = d["inputs"]
        x = np.stack([np.concatenate([to_float(b[c], tname) for b in blocks]) for c in range(nin)], 1)
        x = x[: int(secs * d["rate"])].astype("<f4")
        rate = int(d["rate"])
        fmt = struct.pack("<HHIIHH", 3, nin, rate, rate * nin * 4, nin * 4, 32)
        pcm = x.tobytes()
        with open(out, "wb") as f:
            f.write(b"RIFF" + struct.pack("<I", 4 + 8 + len(fmt) + 8 + len(pcm)) + b"WAVE")
            f.write(b"fmt " + struct.pack("<I", len(fmt)) + fmt)
            f.write(b"data" + struct.pack("<I", len(pcm)) + pcm)
        with open(out + ".channels.txt", "w") as f:
            f.write("\n".join(n for inp, _, n, _, _ in d["chans"] if inp))
        print("wrote", out, x.shape)
