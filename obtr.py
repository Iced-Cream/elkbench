"""Overbridge Total Recall without a DAW: take a project image from an Elektron unit through
the running Overbridge Engine, and restore it. Part of elkbench.

    python obtr.py devices                          the Engine's device list
    python obtr.py take OUT.bin [--device NAME]     new session (no prompt on the unit), read the
                                                    image; OUT.bin = image as sent, OUT.json = session
    python obtr.py sync SESSION.json OUT.bin        read the image again in an existing session
    python obtr.py restore IN.bin [--device NAME] [--answer-timeout 120]
                                                    recall with the session saved beside IN.bin; the
                                                    unit asks USE PLUGIN STATE / USE DEVICE STATE and
                                                    the answer comes from its panel
    python obtr.py decode IN.bin OUT.raw            unpack the LZ4-in-LZ4 image to the raw project

It talks to the Engine the way an Overbridge plugin does, over the Engine's local TCP ports,
for interoperability; the message layouts come from studying the Engine and plugin and from
traffic captures of a plugin session. Needs the
Overbridge Engine running, the unit in OVERBRIDGE USB mode, and no plugin or DAW connected to
that unit. Exit codes: 0 done, 1 failure, 2 the unit answered "device state" (restore not
applied), 3 timeout waiting for the unit. Stdlib only.
"""
import json, os, random, socket, struct, sys, time

MASTER_PORT = 46000                       # the Engine's master port on 127.0.0.1
MASTER_MAGIC, ASYNC_MAGIC = 0x3ADE68B1, 0x3671A379
STREAM_REQ = 0xE1                         # the plugin's requests and their replies
# session states as this tool sees them (our own labels, for the log)
STATE_NAMES = {0: 'unknown', 1: 'no session', 2: 'session running', 3: 'session aborted',
               4: 'new session asked', 5: 'new session: unit asking',
               6: 'new session: accepted, waiting to sync', 7: 'recall asked',
               8: 'recall: unit asking', 9: 'recall: host state chosen',
               10: 'recall: device state chosen', 11: 'host to unit', 12: 'unit to host'}
T_PROJECT, T_SESSION, T_STATE = 0x1B, 0x28, 0x27   # Analog Rytm / Four MKII
# The Digitone (product 0x18) numbers the same three one higher: its state is type 0x28, the
# session ID 0x29 and the project image 0x1C (seen in a capture of its plugin, 2026-10-25).
PRODUCT_TYPES = {0x18: (0x1C, 0x29, 0x28)}
FRAG = 995                                # payload per async packet (1023 - 28-byte header)


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


# ---- framing: every TCP message is u32 magic, u32 length, payload --------------------------

def frame(magic, payload):
    return struct.pack('<II', magic, len(payload)) + payload


class Conn:
    def __init__(self, port, magic, timeout=5.0):
        self.s = socket.create_connection(('127.0.0.1', port), timeout=timeout)
        self.magic, self.buf = magic, b''

    def send(self, payload):
        self.s.sendall(frame(self.magic, payload))

    def recv(self, timeout):
        """One framed message, or None on timeout."""
        self.s.settimeout(timeout)
        end = time.time() + timeout
        while True:
            if len(self.buf) >= 8:
                magic, ln = struct.unpack_from('<II', self.buf)
                if magic != self.magic:
                    raise IOError('bad magic 0x%08x' % magic)
                if len(self.buf) >= 8 + ln:
                    msg, self.buf = self.buf[8:8 + ln], self.buf[8 + ln:]
                    return msg
            left = end - time.time()
            if left <= 0:
                return None
            self.s.settimeout(left)
            try:
                chunk = self.s.recv(65536)
            except socket.timeout:
                return None
            if not chunk:
                raise IOError('connection closed by the Engine')
            self.buf += chunk

    def close(self):
        audio = getattr(self, 'audio', None)
        if audio is not None:
            audio.close()                # after the async port, which callers close first
        self.s.close()


# ---- master port: hello, device list, connection to one device ----------------------------

def master_devices(m):
    m.send(struct.pack('<I', 4))
    while True:
        msg = m.recv(5)
        if msg is None:
            raise IOError('no device list from the Engine')
        if struct.unpack_from('<I', msg)[0] == 5:
            break
    count, = struct.unpack_from('<Q', msg, 4)
    i, out = 12, []
    for _ in range(count):
        uid, ptype, nlen = struct.unpack_from('<QII', msg, i); i += 16
        name = msg[i:i + nlen].decode('utf-8', 'replace'); i += nlen + 6
        out.append({'uid': uid, 'product': ptype, 'name': name})
    return out


def connect(device):
    m = Conn(MASTER_PORT, MASTER_MAGIC)
    hello = m.recv(5)
    if not hello or struct.unpack_from('<II', hello) != (1, 5):
        raise IOError('unexpected Engine hello: %r' % hello)
    m.send(struct.pack('<II', 1, 5))
    devs = master_devices(m)
    match = [d for d in devs if device.lower() in d['name'].lower()]
    if len(match) != 1:
        raise IOError('device %r: %d matches in %s' % (device, len(match), [d['name'] for d in devs]))
    dev = match[0]
    m.send(struct.pack('<IIQ', 7, 0, dev['uid']))
    while True:
        msg = m.recv(5)
        if msg is None:
            raise IOError('no connection response from the Engine')
        if struct.unpack_from('<I', msg)[0] == 8:
            break
    base, rpc, audio = struct.unpack_from('<iii', msg, 4)
    if base < 0:
        # Connecting to the port anyway after the Engine said -1 has crashed its per-device
        # process mid-restore. Never fall back; stop.
        raise IOError('the Engine reports the plugin port busy (-1): another client holds it, '
                      'or an earlier connection was not released; restart Overbridge first')
    log('engine: %s uid %016x, async port %d' % (dev['name'], dev['uid'], base))
    # Also open the audio port (base + 1) and keep it open, sending nothing on it: the Engine
    # hands a unit's plugin port out once and frees it only when its audio client count goes
    # back to 0, and only a connection here counts as an audio client. (A plugin also asks for
    # audio streaming here; this tool never does.) Closing it at the end frees the plugin port,
    # so Overbridge needs no restart after a session.
    m.audio = socket.create_connection(('127.0.0.1', audio), timeout=5.0)
    log('engine: audio port %d held (no audio requested), freed on close' % audio)
    return m, Conn(base, ASYNC_MAGIC), dev


# ---- async packets: 28-byte header, then up to 995 bytes ----------------------------------

class Async:
    def __init__(self, conn, product=None):
        self.c, self.seq, self.pending = conn, random.randint(0x1000, 0x7000), []
        self.t_project, self.t_session, self.t_state = PRODUCT_TYPES.get(
            product, (T_PROJECT, T_SESSION, T_STATE))

    def packet(self, cmd, typ, data, total=None, offset=0, arg=0, check=None, new=True):
        """One packet. A transaction's fragments share one sequence number (new=False after
        the first) and one checksum: the 16-bit sum of the whole transaction's data."""
        if new:
            self.seq += 1
        total = len(data) if total is None else total
        check = sum(data) & 0xFFFF if check is None else check
        h = struct.pack('>BBIIIBBHIHHH', STREAM_REQ, 2, self.seq, 0xFFFFFFFF, offset, cmd, typ,
                        arg, total, len(data), check, 0)
        return h + data

    def send(self, *packets):
        # One async packet per TCP message, as the plugin always sends. Several packets in one
        # message (a large image write) has crashed the Engine.
        for p in packets:
            self.c.send(p)

    def events(self, timeout):
        """Packets from the device, one TCP message at a time."""
        msg = self.c.recv(timeout)
        out, j = [], 0
        while msg and j + 28 <= len(msg):
            h = msg[j:j + 28]
            seq, tid, off = struct.unpack_from('>III', h, 2)
            arg, = struct.unpack_from('>H', h, 16)
            tot, flen = struct.unpack_from('>IH', h, 18)
            out.append({'stream': h[0], 'seq': seq, 'tid': tid, 'off': off, 'cmd': h[14],
                        'typ': h[15], 'arg': arg, 'tot': tot, 'data': msg[j + 28:j + 28 + flen]})
            j += 28 + flen
        return out

    def state(self, st, sid):
        self.send(self.packet(0x50, 0, struct.pack('>BI', st, sid)))
        log('-> state %d %s, session %08x' % (st, STATE_NAMES.get(st, '?'), sid))

    def read(self, typ):
        p = self.packet(0x20, typ, bytes(8))
        self.send(p)
        return self.seq                  # the reply's transaction ID


def wait_for(a, want, timeout, image_tid=None, collect=None):
    """Read events until the device reports a state in `want`; collect image fragments."""
    end, last = time.time() + timeout, None
    while time.time() < end:
        for e in a.events(min(1.0, max(0.05, end - time.time()))):
            if e['cmd'] == 0x30 and e['typ'] == a.t_state and len(e['data']) == 6:
                last = e['data'][5]
                log('<- state %d %s' % (last, STATE_NAMES.get(last, '?')))
                if last in want:
                    return last
                if last == 3:
                    raise IOError('the unit aborted the session')
            elif e['cmd'] == 0x21 and collect is not None and e['tid'] == image_tid:
                collect['tot'] = e['tot']
                collect.setdefault('parts', {})[e['off']] = e['data']
            elif e['cmd'] == 0x21 and e['typ'] == a.t_session:
                if collect is not None:
                    collect['sid'] = struct.unpack('>I', e['data'][:4])[0]
            elif e['cmd'] == 0x23:
                raise IOError('the unit answered error for transaction %x' % e['tid'])
    raise TimeoutError('no state %s within %d s (last %s)' % (sorted(want), timeout, last))


def image_complete(col):
    parts = col.get('parts', {})
    if not parts:
        return None
    data = b''.join(parts[k] for k in sorted(parts))
    return data if len(data) == (col['tot'] & 0x7FFFFFFF) else None


def read_image(a, timeout=30):
    col = {}
    tid = a.read(a.t_project)
    end = time.time() + timeout
    while time.time() < end:
        for e in a.events(0.5):
            if e['cmd'] == 0x21 and e['tid'] == tid:
                col['tot'] = e['tot']
                col.setdefault('parts', {})[e['off']] = e['data']
            elif e['cmd'] == 0x30 and e['typ'] == a.t_state and len(e['data']) == 6:
                log('<- state %d %s' % (e['data'][5], STATE_NAMES.get(e['data'][5], '?')))
        img = image_complete(col)
        if img is not None:
            return img, col['tot']
    raise TimeoutError('image incomplete after %d s' % timeout)


# ---- LZ4 frames (the image is an LZ4 frame holding another) ------------------------------

def lz4_block(src, out):
    i = 0
    while i < len(src):
        tok = src[i]; i += 1; n = tok >> 4
        if n == 15:
            while True:
                x = src[i]; i += 1; n += x
                if x != 255:
                    break
        out += src[i:i + n]; i += n
        if i >= len(src):
            break
        off = src[i] | src[i + 1] << 8; i += 2; m = (tok & 15) + 4
        if (tok & 15) == 15:
            while True:
                x = src[i]; i += 1; m += x
                if x != 255:
                    break
        for _ in range(m):
            out.append(out[-off])


def lz4_frame(f):
    if f[:4] != b'\x04\x22\x4d\x18':
        raise ValueError('not an LZ4 frame')
    flg, i = f[4], 7 + (8 if f[4] & 8 else 0) + (4 if f[4] & 1 else 0)
    out = bytearray()
    while True:
        sz, = struct.unpack_from('<I', f, i); i += 4
        if sz == 0:
            return bytes(out)
        raw, sz = sz & 0x80000000, sz & 0x7FFFFFFF
        blk = f[i:i + sz]; i += sz
        out += blk if raw else b''
        if not raw:
            lz4_block(blk, out)
        if flg & 0x10:
            i += 4


def unpack_image(img):
    inner = lz4_frame(img)
    return lz4_frame(inner) if inner[:4] == b'\x04\x22\x4d\x18' else inner


# ---- commands --------------------------------------------------------------------------

def opt(args, name, default=None):
    if name in args:
        i = args.index(name); v = args[i + 1]; del args[i:i + 2]; return v
    return default


def cmd_devices(args):
    m = Conn(MASTER_PORT, MASTER_MAGIC)
    m.recv(5); m.send(struct.pack('<II', 1, 5))
    for d in master_devices(m):
        print('%016x  product 0x%02x  %s' % (d['uid'], d['product'], d['name']))
    m.close()
    return 0


def save_meta(path, meta):
    with open(os.path.splitext(path)[0] + '.json', 'w') as fh:
        json.dump(meta, fh, indent=1)


def cmd_take(args):
    out = args[0]
    device = opt(args, '--device', 'Analog Rytm')
    m, c, dev = connect(device)
    a = Async(c, dev['product'])
    try:
        sid = random.getrandbits(32) | 1
        a.state(4, sid)
        wait_for(a, {6}, 30)             # a new session needs no answer on the unit
        a.state(12, sid)
        img, tot = read_image(a)
        wait_for(a, {2}, 30)
    finally:
        c.close(); m.close()
    raw = unpack_image(img)
    with open(out, 'wb') as fh:
        fh.write(img)
    meta = {'session': sid, 'device': dev['name'], 'uid': '%016x' % dev['uid'], 'total': tot,
            'bytes': len(img), 'raw_bytes': len(raw), 'magic': raw[:4].hex(),
            'name': raw[8:24].split(b'\0')[0].decode('latin-1'), 'time': time.time()}
    save_meta(out, meta)
    log('image %d B (raw %d B, project %r), session %08x -> %s' % (len(img), len(raw), meta['name'], sid, out))
    return 0


def cmd_sync(args):
    with open(args[0]) as fh:
        meta = json.load(fh)
    out, device = args[1], opt(args, '--device', 'Analog Rytm')
    m, c, dev = connect(device)
    a = Async(c, dev['product'])
    try:
        a.state(12, meta['session'])
        img, tot = read_image(a)
        wait_for(a, {2}, 30)
    finally:
        c.close(); m.close()
    raw = unpack_image(img)
    with open(out, 'wb') as fh:
        fh.write(img)
    save_meta(out, dict(meta, total=tot, bytes=len(img), raw_bytes=len(raw), time=time.time()))
    log('image %d B (raw %d B), session %08x -> %s' % (len(img), len(raw), meta['session'], out))
    return 0


def write_image(a, img, total):
    a.state(11, 0)                       # as the plugin does before writing
    check = sum(img) & 0xFFFF
    packets = [a.packet(0x10, a.t_project, img[o:o + FRAG], total=total, offset=o,
                        check=check, new=(o == 0))
               for o in range(0, len(img), FRAG)]
    a.send(*packets)
    log('-> image %d B in %d fragments, one message each' % (len(img), len(packets)))


def cmd_restore(args):
    src = args[0]
    device = opt(args, '--device', 'Analog Rytm')
    wait_answer = int(opt(args, '--answer-timeout', '120'))
    with open(src, 'rb') as fh:
        img = fh.read()
    with open(os.path.splitext(src)[0] + '.json') as fh:
        meta = json.load(fh)
    unpack_image(img)                    # refuse a file that is not an image
    m, c, dev = connect(device)
    a = Async(c, dev['product'])
    try:
        sid = meta['session']
        a.state(7, sid)
        wait_for(a, {8, 9, 10}, 15)
        log('waiting up to %d s for the answer on the unit (USE PLUGIN STATE, or on the Digitone '
            'ACCEPT RECALL)' % wait_answer)
        st = wait_for(a, {9, 10}, wait_answer)
        if st == 10:
            log('the unit chose its own state: nothing written')
            a.state(12, sid)
            read_image(a)
            wait_for(a, {2}, 30)
            return 2
        write_image(a, img, meta['total'])
        wait_for(a, {2}, 60)             # completion is the state change; no acknowledgement
    finally:
        c.close(); m.close()
    log('restored %s (%r)' % (src, meta.get('name')))
    return 0


def cmd_decode(args):
    with open(args[0], 'rb') as fh:
        raw = unpack_image(fh.read())
    with open(args[1], 'wb') as fh:
        fh.write(raw)
    print('%d B, magic %s, name %r' % (len(raw), raw[:4].hex(), raw[8:24].split(b'\0')[0]))
    return 0


def main(argv):
    cmds = {'devices': cmd_devices, 'take': cmd_take, 'sync': cmd_sync, 'restore': cmd_restore,
            'decode': cmd_decode}
    if not argv or argv[0] not in cmds:
        print(__doc__)
        return 2
    try:
        return cmds[argv[0]](argv[1:])
    except TimeoutError as e:
        log('timeout: %s' % e)
        return 3
    except (IOError, ValueError) as e:
        log('failed: %s' % e)
        return 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
