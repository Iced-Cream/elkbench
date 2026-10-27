"""elkbench launcher: the MCP stdio server each client session starts (any MCP client: Claude
Code, or another agent). It only forwards: every tool call goes, with this session's id and its
client's pid, to the one elkbench daemon on this PC
(daemon.py), which it starts when none runs. See README.md.

    python launcher.py            (started by the MCP client from its server settings)

Session identity: ELKBENCH_SESSION_ID and ELKBENCH_CLIENT_PID when the client sets them, else
Claude Code's CLAUDE_CODE_SESSION_ID / CLAUDE_PID, else mcp-<pid> and no client process.
"""
import ctypes as C
import json
import os
import socket
import subprocess
import sys
import threading
import time

WINDOWS = os.name == 'nt'
if WINDOWS:
    import ctypes.wintypes as W
HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.environ.get('ELKBENCH_DIR') or os.path.join(          # as in daemon.py
    os.environ.get('LOCALAPPDATA') if WINDOWS else
    os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'), 'elkbench')
PROTOCOL = '2025-06-18'
OUT = threading.Lock()


if WINDOWS:
    class PROCESSENTRY32W(C.Structure):
        _fields_ = [('dwSize', W.DWORD), ('cntUsage', W.DWORD), ('th32ProcessID', W.DWORD),
                    ('th32DefaultHeapID', C.c_size_t), ('th32ModuleID', W.DWORD), ('cntThreads', W.DWORD),
                    ('th32ParentProcessID', W.DWORD), ('pcPriClassBase', C.c_long), ('dwFlags', W.DWORD),
                    ('szExeFile', W.WCHAR * 260)]


def proc_parent(pid):
    """Linux: (parent pid, process name) from /proc, or (0, '')."""
    try:
        with open('/proc/%d/stat' % pid) as fh:
            stat = fh.read()
        name, rest = stat[stat.index('(') + 1:stat.rindex(')')], stat[stat.rindex(')') + 2:]
        return int(rest.split()[1]), name.lower()
    except (OSError, ValueError, IndexError):
        return 0, ''


def client_pid():
    """The session's MCP client process: ELKBENCH_CLIENT_PID, else Claude Code's CLAUDE_PID, else
    the nearest ancestor named claude*. elklock ends a claim when this process exits."""
    for name in ('ELKBENCH_CLIENT_PID', 'CLAUDE_PID'):
        if os.environ.get(name):
            return int(os.environ[name])
    if not WINDOWS:
        pid = os.getpid()
        for _ in range(12):
            parent = proc_parent(pid)[0]
            if parent <= 1:
                return None
            if proc_parent(parent)[1].startswith('claude'):
                return parent
            pid = parent
        return None
    k32 = C.WinDLL('kernel32')
    k32.CreateToolhelp32Snapshot.restype = W.HANDLE
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    procs, e = {}, PROCESSENTRY32W()
    e.dwSize = C.sizeof(e)
    ok = k32.Process32FirstW(snap, C.byref(e))
    while ok:
        procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
        ok = k32.Process32NextW(snap, C.byref(e))
    k32.CloseHandle(snap)
    pid = os.getpid()
    for _ in range(12):
        parent = procs.get(pid, (0, ''))[0]
        if not parent or parent not in procs:
            return None
        if procs[parent][1].startswith('claude'):
            return parent
        pid = parent
    return None


PID = client_pid()
SESSION = (os.environ.get('ELKBENCH_SESSION_ID') or os.environ.get('CLAUDE_CODE_SESSION_ID')
           or 'mcp-%s' % (PID or os.getpid()))


def start_daemon():
    flags = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    cmd = [sys.executable, os.path.join(HERE, 'daemon.py')]
    if not WINDOWS:                            # its own session, so it outlives this one
        subprocess.Popen(cmd, start_new_session=True, **kw)
        return
    try:   # leave Claude Code's job object, so the daemon outlives this session
        subprocess.Popen(cmd, creationflags=flags | 0x01000000, **kw)
    except OSError:
        subprocess.Popen(cmd, creationflags=flags, **kw)


def connect():
    """A connection to the daemon after the hello, starting the daemon if needed."""
    deadline = time.time() + 15
    started = 0
    while time.time() < deadline:
        try:
            with open(os.path.join(STATE, 'daemon.json'), encoding='utf-8') as fh:
                d = json.load(fh)
            sock = socket.create_connection(('127.0.0.1', d['port']), timeout=5)
            f = sock.makefile('rwb')
            f.write((json.dumps({'hello': d['secret'], 'session': SESSION, 'pid': PID,
                                 'cwd': os.getcwd()}) + '\n').encode())
            f.flush()
            sock.settimeout(None)
            r = json.loads(f.readline() or b'{}')
            if r.get('ok'):
                return sock, f
            sock.close()
            if r.get('restart'):
                time.sleep(1.0)             # it is stopping for newer code; start it again
                started = 0
        except (OSError, ValueError, KeyError):
            pass
        if time.time() - started > 3:      # again if the one started met the old one still exiting
            start_daemon()
            started = time.time()
        time.sleep(0.3)
    raise RuntimeError('the elkbench daemon did not start (see %s)' % STATE)


def ask(req):
    sock, f = connect()
    try:
        f.write((json.dumps(req) + '\n').encode())
        f.flush()
        return json.loads(f.readline())
    finally:
        sock.close()


def send(msg):
    with OUT:
        sys.stdout.buffer.write((json.dumps(msg) + '\n').encode())
        sys.stdout.buffer.flush()


CLIENT = {'elicit': False}               # set from the client's initialize
WAITING = {}                              # our request id -> [threading.Event, response]
NEXT_ID = [0]


def request(method, params, timeout):
    """A request to the client (server -> client), e.g. elicitation; -> its response message."""
    with OUT:
        NEXT_ID[0] += 1
        rid = 'elkbench-%d' % NEXT_ID[0]
    slot = WAITING[rid] = [threading.Event(), None]
    send({'jsonrpc': '2.0', 'id': rid, 'method': method, 'params': params})
    try:
        if not slot[0].wait(timeout):
            raise TimeoutError('no answer from the client')
        return slot[1]
    finally:
        WAITING.pop(rid, None)


def approve_in_client(elicit):
    """Ask the person through the client (MCP elicitation); their answer, not the model's,
    goes back to the daemon. A client that cannot ask falls back to the popup on the PC."""
    t0 = time.time()
    try:
        r = request('elicitation/create', {
            'message': elicit['message'],
            'requestedSchema': {'type': 'object', 'properties': {'approve': {
                'type': 'boolean', 'title': 'Approve this hardware run',
                'description': 'Yes gives this session one go for exactly the items above.'}},
                'required': ['approve']}}, timeout=30 * 60)
    except TimeoutError:
        return ask({'id': 1, 'method': 'answer', 'request': elicit['request'], 'accept': False})
    if 'error' in r:
        return ask({'id': 1, 'method': 'answer', 'request': elicit['request'], 'fallback': True})
    res = r.get('result') or {}
    took = time.time() - t0
    try:      # what the client answered and how fast, to tell clients that show the prompt apart
        with open(os.path.join(STATE, 'clients.jsonl'), 'a', encoding='utf-8') as fh:
            fh.write(json.dumps({'time': time.time(), 'session': SESSION, 'elicit_answer': res.get('action'),
                                 'seconds': round(took, 3)}) + '\n')
    except OSError:
        pass
    if res.get('action') in ('cancel', 'decline') and took < 2:
        # a No or cancel before a person could have read it: a client with nowhere to show
        # the prompt (headless, or Remote Control, which declines in ~40 ms); ask on the PC
        return ask({'id': 1, 'method': 'answer', 'request': elicit['request'], 'fallback': True})
    yes = res.get('action') == 'accept' and (res.get('content') or {}).get('approve') is True
    return ask({'id': 1, 'method': 'answer', 'request': elicit['request'], 'accept': yes})


def handle(msg):
    mid, method = msg.get('id'), msg.get('method')
    if method is None and mid in WAITING:  # the client's answer to our request
        WAITING[mid][1] = msg
        WAITING[mid][0].set()
        return
    try:
        if method == 'initialize':
            CLIENT['elicit'] = 'elicitation' in ((msg.get('params') or {}).get('capabilities') or {})
            try:      # which clients can ask the person themselves (the approval prompt)
                os.makedirs(STATE, exist_ok=True)
                with open(os.path.join(STATE, 'clients.jsonl'), 'a', encoding='utf-8') as fh:
                    fh.write(json.dumps({'time': time.time(), 'session': SESSION, 'elicit': CLIENT['elicit'],
                                         'client': (msg.get('params') or {}).get('clientInfo'),
                                         'capabilities': (msg.get('params') or {}).get('capabilities')}) + '\n')
            except OSError:
                pass
            v = (msg.get('params') or {}).get('protocolVersion') or PROTOCOL
            result = {'protocolVersion': v, 'capabilities': {'tools': {}},
                      'serverInfo': {'name': 'elkbench', 'version': '1'},
                      'instructions': 'Elektron hardware test tools. Run everything in the emulator before '
                                      'the unit, every flash too; a hardware run needs a go from the '
                                      'person at the PC (approve_plan); release with a note when done. '
                                      'The practices sessions follow are in docs/testing-practices.md '
                                      'in the elkbench repo; read it before a hardware run. '
                                      'When the person listens to a unit (listen, or a plan\'s listen '
                                      'step), keep the claim and send nothing to it until they say '
                                      'they have finished (listen_done). Order: emulator runs, then '
                                      'approve_plan, then claim after the yes (approve_plan refuses '
                                      'while you hold a unit idle; an emulator call while holding one '
                                      'releases it), then check the unit\'s state before writing. '
                                      'If you can start a cheaper test-runner subagent (for Claude Code, '
                                      'agents/elkbench-runner.md in the elkbench repo), hand it the plans, '
                                      'flashes and steps to run, and interpret its report yourself.'}
        elif method == 'tools/list':
            result = {'tools': ask({'id': 1, 'method': 'list'})['tools']}
        elif method == 'tools/call':
            p = msg.get('params') or {}
            args = dict(p.get('arguments') or {}, _elicit=CLIENT['elicit'])   # never the caller's say
            r = ask({'id': 1, 'method': 'call', 'tool': p.get('name'), 'args': args})
            if r.get('elicit'):
                r = approve_in_client(r['elicit'])
            result = {'content': [{'type': 'text', 'text': r.get('text', '')}], 'isError': bool(r.get('error'))}
        elif method == 'ping':
            result = {}
        elif mid is None:
            return                          # a notification
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': 'no method %s' % method}})
            return
    except Exception as e:                  # noqa: BLE001 - the session sees one line
        if mid is None:
            return
        if method == 'tools/call':
            result = {'content': [{'type': 'text', 'text': 'fail: %s' % e}], 'isError': True}
        else:
            send({'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32603, 'message': str(e)}})
            return
    if mid is not None:
        send({'jsonrpc': '2.0', 'id': mid, 'result': result})


def main():
    for raw in sys.stdin.buffer:
        if not raw.strip():
            continue
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        # calls run side by side; a long claim --wait must not hold up a status poll
        threading.Thread(target=handle, args=(msg,), daemon=True).start()


if __name__ == '__main__':
    main()
