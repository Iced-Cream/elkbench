"""asio_rec.py rec and asio_tone.py where there is no ASIO (Linux): the same arguments and the
same 32-bit float WAV of every input (names in <out>.channels.txt), through PortAudio
(`pip install sounddevice numpy`). The device is matched by name, ignoring case and a leading
"Elektron": "Elektron Analog Rytm MKII" finds the ALSA card "Analog Rytm MKII".
"""
import struct
import sys

RATE = 48000


def find_device(name):
    import sounddevice as sd
    want = name.lower().removeprefix('elektron').strip()
    hits = [(i, d) for i, d in enumerate(sd.query_devices())
            if want in d['name'].lower() and d['max_input_channels'] > 0]
    if not hits:
        names = ', '.join(d['name'] for d in sd.query_devices() if d['max_input_channels'] > 0)
        raise SystemExit('no audio input device matching %r (inputs: %s)' % (name, names))
    return hits[0]


def write_wav(path, x, rate):
    import numpy as np
    x = np.asarray(x, '<f4')
    nin = x.shape[1]
    fmt = struct.pack('<HHIIHH', 3, nin, rate, rate * nin * 4, nin * 4, 32)
    pcm = x.tobytes()
    with open(path, 'wb') as f:
        f.write(b'RIFF' + struct.pack('<I', 4 + 8 + len(fmt) + 8 + len(pcm)) + b'WAVE')
        f.write(b'fmt ' + struct.pack('<I', len(fmt)) + fmt)
        f.write(b'data' + struct.pack('<I', len(pcm)) + pcm)
    with open(path + '.channels.txt', 'w') as f:
        f.write('\n'.join('in %d' % (c + 1) for c in range(nin)))


def record(name, seconds, path):
    import sounddevice as sd
    i, d = find_device(name)
    nin = d['max_input_channels']
    x = sd.rec(int(seconds * RATE), samplerate=RATE, channels=nin, dtype='float32', device=i)
    sd.wait()
    write_wav(path, x, RATE)
    print('%s: %d in; wrote %s %s' % (d['name'], nin, path, x.shape))


def tone(name, path, seconds, oleft=0, level=-18.0, freq=997.0):
    import numpy as np
    import sounddevice as sd
    i, d = find_device(name)
    nin, nout = d['max_input_channels'], d['max_output_channels']
    if nout < oleft + 2:
        raise SystemExit('%s has %d outputs; need %d' % (d['name'], nout, oleft + 2))
    n = int(seconds * RATE)
    t = np.arange(n) / RATE
    ramp = np.minimum(1, np.minimum(t, t[::-1]) / 0.02)       # 20 ms fades
    play = np.zeros((n + RATE // 2, nout), 'float32')
    play[:n, oleft] = play[:n, oleft + 1] = 10 ** (level / 20) * np.sin(2 * np.pi * freq * t) * ramp
    print('%s: %d in, %d out; %g Hz at %g dBFS for %g s on out %d/%d'
          % (d['name'], nin, nout, freq, level, seconds, oleft, oleft + 1))
    x = sd.playrec(play, samplerate=RATE, channels=nin, dtype='float32', device=i)
    sd.wait()
    write_wav(path, x, RATE)
    print('recorded %.2f s' % (len(x) / RATE))


def main_rec(argv):
    """asio_rec.py rec DEVICE SECONDS OUT.wav (info: the device's channels)."""
    import sounddevice as sd
    if argv[:1] == ['info'] and len(argv) > 1:
        print(find_device(argv[1])[1])
        return 0
    if argv[:1] != ['rec'] or len(argv) < 4:
        print(sd.query_devices())
        return 2
    record(argv[1], float(argv[2]), argv[3])
    return 0


def main_tone(argv):
    """asio_tone.py DEVICE OUT.wav SECONDS [out-left-index] [level-dBFS] [freq-Hz]"""
    if len(argv) < 3:
        print(main_tone.__doc__)
        return 2
    extra = [int(argv[3])] if len(argv) > 3 else []
    extra += [float(v) for v in argv[4:6]]
    tone(argv[0], argv[1], float(argv[2]), *extra)
    return 0


if __name__ == '__main__':
    sys.exit(main_rec(sys.argv[1:]))
