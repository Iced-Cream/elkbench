"""Offline tests for elkbench: scratch lock, state and config, the sample plans in tests\\, no
unit is touched (anything that would reach a unit is either refused first or captured before it
starts).

    python test_elkbench.py

The plan tests need an analoghealth checkout (its testplan.py reads the plans): set
ELKBENCH_TEST_ANALOGHEALTH, or have tools.analoghealth in your own config.toml; without one they
are skipped.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
TMP = tempfile.mkdtemp(prefix='elkbench-test-')
PLANS = os.path.join(TMP, 'plans')


def write_fixtures():
    """The sample plans, with stand-in reference screens (128x64, 1 bit per pixel) and OS files
    beside them: the tests only hash those, and none is kept in the repo."""
    shutil.copytree(os.path.join(HERE, 'tests', 'plans'), PLANS)
    os.makedirs(os.path.join(PLANS, 'refs', 'menus'))
    for name, mark in (('global-menu', 300), ('project-menu', 600)):
        screen = bytearray(1024)
        screen[:16] = screen[-16:] = b'\xff' * 16          # top and bottom rows
        screen[mark] = 0x81
        with open(os.path.join(PLANS, 'refs', 'menus', name + '.bin'), 'wb') as fh:
            fh.write(screen)
    os.makedirs(os.path.join(TMP, 'os'))
    for name, tag in (('stock', 1), ('test-build', 2), ('remote-only', 3)):
        with open(os.path.join(TMP, 'os', name + '.syx'), 'wb') as fh:
            fh.write(bytes([0xF0, 0x00, 0x20, 0x3C, 0x7F, tag, 0xF7]))


write_fixtures()


def find_analoghealth():
    if os.environ.get('ELKBENCH_TEST_ANALOGHEALTH'):
        return os.environ['ELKBENCH_TEST_ANALOGHEALTH']
    own = os.environ.get('ELKBENCH_CONFIG') or os.path.join(
        os.environ.get('LOCALAPPDATA') or os.path.expanduser('~'), 'elkbench', 'config.toml')
    try:
        with open(own, 'rb') as fh:
            return tomllib.load(fh).get('tools', {}).get('analoghealth')
    except (OSError, ValueError):
        return None


ANALOGHEALTH = find_analoghealth()
if ANALOGHEALTH and not os.path.isfile(os.path.join(ANALOGHEALTH, 'tools', 'testplan.py')):
    ANALOGHEALTH = None
toml_str = lambda p: "'%s'" % p          # TOML literal string: backslashes stay as they are
with open(os.path.join(TMP, 'config.toml'), 'w', encoding='utf-8') as fh:
    # no built-in starts unless a test gives some (the repo's starts/ would apply to every plan)
    fh.write('person = "the tester"\nevidence_root = %s\nstarts = %s\n\n[tools]\n'
             % (toml_str(os.path.join(TMP, 'evidence')), toml_str(os.path.join(TMP, 'no-starts'))))
    if ANALOGHEALTH:
        fh.write('analoghealth = %s\n' % toml_str(ANALOGHEALTH))
    fh.write('\n[units.rytm]\nstock = [%s]\nno_stats = [%s]\n' % (
        toml_str(os.path.join(TMP, 'os', 'stock.syx')), toml_str(os.path.join(TMP, 'os', 'remote-only.syx'))))
os.environ['ELKBENCH_CONFIG'] = os.path.join(TMP, 'config.toml')
os.environ['ELKBENCH_DIR'] = os.path.join(TMP, 'state')
os.environ['ELKLOCK_DIR'] = os.path.join(TMP, 'locks')
os.makedirs(os.environ['ELKLOCK_DIR'])
sys.path.insert(0, HERE)
import daemon      # noqa: E402

needs_runner = unittest.skipUnless(ANALOGHEALTH, 'no analoghealth checkout (ELKBENCH_TEST_ANALOGHEALTH)')


class Launcher:
    """launcher.py over stdio, as Claude Code drives it."""

    def __init__(self, session, code=HERE, **env):
        env = dict(os.environ, CLAUDE_CODE_SESSION_ID=session, CLAUDE_PID=str(os.getpid()), **env)
        self.p = subprocess.Popen([sys.executable, os.path.join(code, 'launcher.py')], env=env,
                                  stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.n = 0

    def rpc(self, method, params=None):
        self.n += 1
        self.p.stdin.write((json.dumps({'jsonrpc': '2.0', 'id': self.n, 'method': method,
                                        'params': params or {}}) + '\n').encode())
        self.p.stdin.flush()
        return json.loads(self.p.stdout.readline())

    def write(self, msg):
        self.p.stdin.write((json.dumps(msg) + '\n').encode())
        self.p.stdin.flush()

    def read(self):
        return json.loads(self.p.stdout.readline())

    def call(self, tool, **args):
        r = self.rpc('tools/call', {'name': tool, 'arguments': args})['result']
        return r['content'][0]['text'], r['isError']

    def close(self):
        self.p.stdin.close()
        self.p.wait(10)
        self.p.stdout.close()


def forget_passes():
    try:
        os.remove(os.path.join(os.environ['ELKBENCH_DIR'], 'emulator-passes.json'))
    except OSError:
        pass


def read(path):
    with open(path, encoding='utf-8') as fh:
        return fh.read()


@needs_runner
class OverMcp(unittest.TestCase):
    def test_launcher_and_daemon(self):
        forget_passes()
        L = Launcher('test-session-a')
        try:
            init = L.rpc('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                        'clientInfo': {'name': 't', 'version': '0'}})
            self.assertEqual(init['result']['serverInfo']['name'], 'elkbench')
            names = [t['name'] for t in L.rpc('tools/list')['result']['tools']]
            self.assertIn('approve_plan', names)
            self.assertEqual(len(names), 23)
            text, err = L.call('status')
            self.assertTrue(text.startswith('ok: 0 claim'), text)
            text, err = L.call('run_plan', plan=os.path.join(PLANS, 'menus.txt'), target='hardware')
            self.assertTrue(err and 'claim rytm first' in text, text)
            text, err = L.call('claim', unit='rytm', purpose='elkbench selftest (scratch locks)')
            self.assertFalse(err, text)
            text, err = L.call('run_plan', plan=os.path.join(PLANS, 'menus.txt'), target='hardware')
            self.assertTrue(err and 'no passing emulator run' in text, text)
            text, err = L.call('steps', unit='rytm', target='hardware', steps='peek 0 4')
            self.assertTrue(err and 'not allowed' in text, text)
            text, err = L.call('claim', unit='syntakt', purpose='x')
            self.assertTrue(err and 'unknown device' in text, text)
            # a second session sees the unit busy and gets nothing
            M = Launcher('test-session-b')
            try:
                text, err = M.call('claim', unit='rytm', purpose='second session')
                self.assertTrue(err and 'BUSY' in text, text)
            finally:
                M.close()
            text, err = L.call('status')
            self.assertIn('held: rytm', text)
            log = os.path.join(os.environ['ELKBENCH_DIR'], 'calls.jsonl')
            rows = [json.loads(l) for l in read(log).splitlines()]
            self.assertTrue(all(r.get('session') for r in rows))
            self.assertIn('test-session-b', {r['session'] for r in rows})
        finally:
            subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
                           env=dict(os.environ, CLAUDE_CODE_SESSION_ID='test-session-a'), capture_output=True)
            L.close()


class RestartForNewerCode(unittest.TestCase):
    """A copy of the code with its own daemon: newer code on disk waits for elklock, not for
    sessions to check claims."""

    def setUp(self):
        self.code = os.path.join(TMP, 'code')
        shutil.rmtree(self.code, ignore_errors=True)
        os.makedirs(self.code)
        for f in ('daemon.py', 'launcher.py', 'batch.py', 'plancheck.py'):
            shutil.copy(os.path.join(HERE, f), self.code)
        shutil.copytree(os.path.join(HERE, 'elklock'), os.path.join(self.code, 'elklock'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        self.state = os.path.join(TMP, 'state-' + self._testMethodName)    # a daemon of its own
        self.L = Launcher('test-session-r', self.code, ELKBENCH_DIR=self.state)
        self.env = dict(os.environ, CLAUDE_CODE_SESSION_ID='test-session-r')

    def tearDown(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
                       env=self.env, capture_output=True)
        self.L.close()
        stop_daemon(self.state)                       # its daemon outlives the launcher

    def pid(self):
        return json.loads(read(os.path.join(self.state, 'daemon.json')))['pid']

    def newer(self, first_line=''):
        p = os.path.join(self.code, 'daemon.py')
        code = read(p)
        with open(p, 'w', encoding='utf-8') as fh:
            fh.write(first_line + code)
        later = time.time() + 5
        os.utime(p, (later, later))

    def test_waits_for_claims_then_starts(self):
        text, err = self.L.call('status')
        self.assertFalse(err, text)
        first = self.pid()
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.env, capture_output=True)
        self.newer()
        text, err = self.L.call('status')
        self.assertIn('when nothing holds it: rytm is claimed', text)
        self.assertEqual(self.pid(), first)
        subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
                       env=self.env, capture_output=True)
        text, err = self.L.call('status')
        self.assertFalse(err, text)
        self.assertNotIn('newer elkbench code', text)
        self.assertNotEqual(self.pid(), first)

    def test_broken_code_keeps_the_old_daemon(self):
        self.L.call('status')
        first = self.pid()
        self.newer('raise SystemExit("half-landed")\n')
        text, err = self.L.call('status')
        self.assertIn('does not start, so this one keeps serving: half-landed', text)
        self.assertEqual(self.pid(), first)


@needs_runner
class ApprovalInClaude(unittest.TestCase):
    """approve_plan through MCP elicitation: the client (here, this test) answers, never a dialog."""

    def setUp(self):
        forget_passes()
        info = daemon.Daemon().plan_info(os.path.join(PLANS, 'menus.txt'))
        run = os.path.join(TMP, 'emu-run-elicit')
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, 'summary.json'), 'w') as fh:
            json.dump({'emulator': True, 'pass': True}, fh)
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        with open(os.path.join(os.environ['ELKBENCH_DIR'], 'emulator-passes.json'), 'w') as fh:
            json.dump({info['key']: {'run_dir': run, 'time': time.time()}}, fh)
        self.L = Launcher('test-session-e')
        self.L.write({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-06-18', 'capabilities': {'elicitation': {}},
            'clientInfo': {'name': 't', 'version': '0'}}})
        self.L.read()

    def tearDown(self):
        self.L.close()

    def ask(self, answer, after=0):
        self.L.write({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'approve_plan', 'arguments': {'plans': [os.path.join(PLANS, 'menus.txt')],
                                                  'purpose': 'elicitation test'}}})
        req = self.L.read()
        self.assertEqual(req['method'], 'elicitation/create')
        self.assertIn('PLAN menus on rytm', req['params']['message'])
        time.sleep(after)
        self.L.write({'jsonrpc': '2.0', 'id': req['id'], 'result': answer})
        r = self.L.read()
        self.assertEqual(r['id'], 2)
        return r['result']['content'][0]['text']

    def test_yes_gives_a_token(self):
        text = self.ask({'action': 'accept', 'content': {'approve': True}})
        self.assertTrue(text.startswith('ok: token: ap-'), text)

    def test_no_and_cancel_give_none(self):
        # (a No or cancel within 2 s means the client could not show the prompt: that opens
        # the popup on the PC, so a decline is answered after 2 s and the instant one not tried)
        for answer, after in (({'action': 'accept', 'content': {'approve': False}}, 0),
                              ({'action': 'decline'}, 2.2)):
            text = self.ask(answer, after)
            self.assertTrue(text.startswith('refused:'), text)

    def test_a_tool_argument_cannot_claim_the_client_asked(self):
        # `_elicit` from the caller is overwritten by the launcher; this client did say it can ask
        self.L.write({'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {
            'name': 'approve_plan', 'arguments': {'plans': [os.path.join(PLANS, 'menus.txt')],
                                                  'purpose': 't', '_elicit': False}}})
        req = self.L.read()
        self.assertEqual(req['method'], 'elicitation/create')
        # answer it (a No, not a fast one, which would open the popup): a request left waiting
        # would refuse this session's next approve_plan in the other tests (one go at a time)
        self.L.write({'jsonrpc': '2.0', 'id': req['id'], 'result': {'action': 'accept', 'content': {'approve': False}}})
        self.assertTrue(self.L.read()['result']['content'][0]['text'].startswith('refused:'))


class MidiGuard(unittest.TestCase):
    """The stale-MIDI-port guards (2026-11-05): other MIDI programs warned about, midisrv's pid
    and start time logged, a hung job killed with the processes it started."""

    def test_parse_one_row_many_rows_none(self):
        one = '{"name":"midisrv.exe","pid":4321,"start":"2026-11-05T18:52:29"}'
        self.assertEqual(daemon.parse_midi_processes(one),
                         ([{'name': 'midisrv.exe', 'pid': 4321, 'start': '2026-11-05T18:52:29'}], []))
        many = json.dumps([{'name': 'MIDISRV.EXE', 'pid': 1, 'start': 'a'},
                           {'name': 'Transfer.exe', 'pid': 2, 'start': 'b'}])
        srv, others = daemon.parse_midi_processes(many)
        self.assertEqual(([r['pid'] for r in srv], [r['name'] for r in others]), ([1], ['Transfer.exe']))
        self.assertEqual(daemon.parse_midi_processes(''), ([], []))

    def test_lines(self):
        srv = [{'name': 'midisrv.exe', 'pid': 7, 'start': '2026-11-05T18:52:29'}]
        transfer = [{'name': 'Transfer.exe', 'pid': 9, 'start': '2026-11-05T15:06:00'}]
        warn, line = daemon.midi_lines(srv, transfer, None, flash=True)
        self.assertEqual(line, 'midisrv: pid 7, started 2026-11-05T18:52:29')
        self.assertEqual(len(warn), 1)
        self.assertIn('Transfer.exe (pid 9, running since 2026-11-05T15:06:00)', warn[0])
        self.assertIn('close it before flashing', warn[0])
        self.assertEqual(daemon.midi_lines(srv, [], None, last_srv=[7])[0], [])
        self.assertIn('restarted since the last hardware run: was pid 5',
                      daemon.midi_lines(srv, [], None, last_srv=[5])[1])
        self.assertEqual(daemon.midi_lines([], [], None)[1], 'midisrv: not running')
        self.assertEqual(daemon.midi_lines([], [], 'not Windows')[1], 'midisrv: unknown (not Windows)')

    def test_query_goes_encoded_and_lists_the_clients(self):
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout='[]', stderr='')
        self.assertEqual(daemon.midi_processes(['Transfer.exe', 'Overbridge%'], run=run), ([], [], None))
        cmd = seen[0]
        self.assertEqual(cmd[:4], ['powershell', '-NoProfile', '-NonInteractive', '-EncodedCommand'])
        import base64
        query = base64.b64decode(cmd[4]).decode('utf-16-le')
        self.assertIn("Name='midisrv.exe' OR Name LIKE 'Transfer.exe' OR Name LIKE 'Overbridge%'", query)
        with self.assertRaises(SystemExit):
            daemon.midi_query(["x' OR 1=1"])

    def test_a_failed_query_is_reported_not_raised(self):
        def run(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 20)
        srv, others, error = daemon.midi_processes([], run=run)
        self.assertEqual((srv, others), ([], []))
        self.assertIn('TimeoutExpired', error)

    @unittest.skipIf(os.name == 'nt', 'the process-group kill is the POSIX path; Windows uses taskkill /T')
    def test_run_limited_kills_the_tree(self):
        marker = os.path.join(TMP, 'grandchild.pid')
        code = ('import subprocess, sys, time; p = subprocess.Popen([sys.executable, "-c", '
                '"import time; time.sleep(60)"]); open(%r, "w").write(str(p.pid)); print("started", '
                'flush=True); time.sleep(60)' % marker)
        t0 = time.monotonic()
        rc, out, killed = daemon.run_limited([sys.executable, '-c', code], TMP, dict(os.environ), 1.5)
        self.assertTrue(killed)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertIn('started', out)
        with open(marker) as fh:
            gpid = int(fh.read())
        for _ in range(50):                      # the grandchild (elkflash under elklock run) too
            try:
                os.kill(gpid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            self.fail('the grandchild was not killed')
        self.assertEqual(daemon.run_limited([sys.executable, '-c', 'print(1)'], TMP, dict(os.environ), 30),
                         (0, '1\n', False))

    def test_a_job_over_its_limit_fails_and_says_why(self):
        d = daemon.Daemon()
        s = daemon.Session('test-session-midi', os.getpid(), TMP)
        job = daemon.Job(s, 'flash', 'rytm', 'emulator', 'flash X.syx')
        job.limit = 1
        got = []
        d.start(job, [sys.executable, '-c', 'import time; time.sleep(60)'], TMP,
                lambda job, rc, out: got.append((rc, out)))
        for _ in range(200):
            if job.ended:
                break
            time.sleep(0.05)
        self.assertTrue(got, 'done was not called')
        rc, out = got[0]
        self.assertNotEqual(rc, 0)
        self.assertIn('killed after 1 s', out)


class InProcess(unittest.TestCase):
    def setUp(self):
        forget_passes()
        self.d = daemon.Daemon()
        self.d.approvals.clear()          # one go at a time: an earlier test's open go would refuse a new one
        self.d.save_approvals()
        self.s = daemon.Session('test-session-c', os.getpid(), TMP)
        self.folder = os.path.join(TMP, 'ARMKII')
        self.saved = dict(daemon.UNITS['rytm'])
        self.saved_a4 = dict(daemon.UNITS['a4'])
        daemon.UNITS['rytm']['evidence'] = os.path.join(self.folder, 'runs')
        daemon.UNITS['a4']['evidence'] = os.path.join(TMP, 'A4MKII', 'runs')
        self.started = []
        self.d.start = lambda job, cmd, cwd, done, port=None: (self.started.append((job, cmd)),
                                                               setattr(self, 'done', done),
                                                               port and port.release(), ['running'])[3]
        daemon.ask_person = lambda title, text: True

    def tearDown(self):
        daemon.UNITS['rytm'].update(self.saved)
        daemon.UNITS['a4'].update(self.saved_a4)
        for unit in ('rytm', 'a4'):
            subprocess.run([sys.executable, daemon.ELKLOCK, 'release', unit, '--no-snapshot'],
                           env=self.s.env(), capture_output=True)

    def fake_pass(self, info):
        run = os.path.join(TMP, 'emu-run-' + info['key'][:6])
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, 'summary.json'), 'w') as fh:
            json.dump({'emulator': True, 'pass': True, 'flash_sha256': info['flash_sha']}, fh)
        self.d.record_pass(info['key'], {'run_dir': run})

    def approve(self, **items):
        out = self.d.t_approve_plan(self.s, dict(purpose='test', **items))
        rid = out[0].split()[1].rstrip(',')
        for _ in range(50):
            if self.d.jobs[rid].state != 'running':
                break
            time.sleep(0.05)
        line = self.d.t_status(self.s, {'run_id': rid})[1]
        return line.split()[1]

    @needs_runner
    def test_twins_share_the_key(self):
        for hw, emu in (('menus.txt', 'menus-emu.txt'), ('menus-flash.txt', 'menus-flash-emu.txt')):
            a = self.d.plan_info(os.path.join(PLANS, hw))
            b = self.d.plan_info(os.path.join(PLANS, emu))
            self.assertEqual(a['key'], b['key'], hw)
            self.assertTrue(b['emulator'] and not a['emulator'])
            twin = self.d.twin(a, False, self.s.sid)
            self.assertEqual(self.d.plan_info(twin)['key'], a['key'])
        self.assertNotEqual(self.d.plan_info(os.path.join(PLANS, 'menus.txt'))['key'],
                            self.d.plan_info(os.path.join(PLANS, 'menus-flash.txt'))['key'])

    @needs_runner
    def test_thread_answer_only_when_claude_could_not_ask(self):
        plan = os.path.join(PLANS, 'menus.txt')
        self.fake_pass(self.d.plan_info(plan))
        popup, popups = threading.Event(), []
        daemon.ask_person = lambda title, text: popups.append(1) or popup.wait(10) and False   # unanswered
        asked = lambda: self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 't', '_elicit': True})['elicit']['request']
        saved = daemon.FALLBACK
        try:
            daemon.FALLBACK = 'popup'                         # the default: no thread route
            rid = asked()
            self.assertNotIn('answer_in_thread', '\n'.join(self.d.answer(self.s, {'request': rid, 'fallback': True})))
            with self.assertRaisesRegex(daemon.Refused, 'waiting for an answer in the thread'):
                self.d.t_answer_in_thread(self.s, {'request': rid, 'approve': True, 'message_id': 'cmsg_x'})
            with self.assertRaisesRegex(daemon.Refused, 'still waiting'):
                asked()                                       # one request at a time
            self.d.t_end_go(self.s, {'request': rid})         # the popup is left unanswered
            daemon.FALLBACK = 'thread'                        # no popup at all
            n, rid = len(popups), asked()
            self.assertIn('answer_in_thread', '\n'.join(self.d.answer(self.s, {'request': rid, 'fallback': True})))
            self.assertTrue(self.d.t_answer_in_thread(self.s, {'request': rid, 'approve': False,
                                                               'message_id': 'cmsg_x'})[0].startswith('refused'))
            time.sleep(0.2)
            self.assertEqual(len(popups), n)
            daemon.FALLBACK = 'both'
            rid = asked()
            yes = {'request': rid, 'approve': True, 'message_id': 'cmsg_x'}
            with self.assertRaisesRegex(daemon.Refused, 'waiting for an answer in the thread'):
                self.d.t_answer_in_thread(self.s, yes)       # Claude is still asking: no thread route
            out = self.d.answer(self.s, {'request': rid, 'fallback': True})
            self.assertIn('answer_in_thread', '\n'.join(out))
            with self.assertRaisesRegex(daemon.Refused, 'id or link'):
                self.d.t_answer_in_thread(self.s, dict(yes, message_id='yes'))
            self.assertTrue(self.d.t_answer_in_thread(self.s, yes)[0].startswith('ok: token: ap-'))
            popup.set()                                       # the popup's later No changes nothing
            time.sleep(0.2)
            self.assertEqual(self.d.jobs[rid].state, 'approved')
            token = self.d.jobs[rid].lines[0].split()[1]
            self.assertEqual(len([t for t, g in self.d.open_goes().items() if g['session'] == self.s.sid]), 1)
            self.d.t_end_go(self.s, {'approval': token})
            # a session whose popup came first (no Claude prompt tried) has no thread route
            rid = self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 't'})[0].split()[1].rstrip(',')
            with self.assertRaisesRegex(daemon.Refused, 'waiting for an answer in the thread'):
                self.d.t_answer_in_thread(self.s, dict(yes, request=rid))
        finally:
            daemon.FALLBACK = saved
            popup.set()

    def test_a_restart_keeps_the_go(self):
        # the daemon restarts for newer code at any idle moment, also between a go's two flashes
        item = ('flash', 'rytm', 'ab' * 32)
        with self.d.lock:
            self.d.approvals['ap-kept'] = dict(session=self.s.sid, created=time.time(),
                                               expires=time.time() + 60, text='t', items=[
                                                   dict(item=item, used=False, label='flash x on rytm')])
            self.d.save_approvals()
        again = daemon.Daemon()
        again.use_approval(self.s, 'ap-kept', item, 'flash x')
        with self.assertRaisesRegex(daemon.Refused, 'approved once'):
            daemon.Daemon().use_approval(self.s, 'ap-kept', item, 'flash x')
        again.give_back('ap-kept', item)
        daemon.Daemon().use_approval(self.s, 'ap-kept', item, 'flash x')
        with self.assertRaisesRegex(daemon.Refused, 'needs a go from'):
            again.use_approval(self.s, None, item, 'flash x')

    def test_restart_waits_for_elklock_and_goes(self):
        self.d.approvals.clear()                  # gos earlier tests left in the shared state
        self.assertEqual(self.d.restart_waits_for(), [])
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        self.assertEqual(self.d.restart_waits_for(), ['rytm is claimed'])
        subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
                       env=self.s.env(), capture_output=True)
        with self.d.lock:
            self.d.approvals['ap-open'] = dict(session=self.s.sid, created=time.time(),
                                               expires=time.time() + 60, text='t', items=[
                                                   dict(item=('flash', 'rytm', 'cd' * 32), used=False,
                                                        label='flash y on rytm')])
        self.assertTrue(self.d.restart_waits_for()[0].startswith('a go is open'))
        self.d.approvals['ap-open']['items'][0]['used'] = True
        self.assertEqual(self.d.restart_waits_for(), [])
        job = daemon.Job(self.s, 'plan', 'rytm', 'emulator', 'x.txt')
        self.d.jobs[job.id] = job
        self.assertEqual(self.d.restart_waits_for(), ['a run is going'])
        job.state, job.lines = 'pass', ['PASS x']
        self.d.save_jobs()
        self.assertEqual(daemon.Daemon().t_status(self.s, {'run_id': job.id})[1:], ['PASS x'])

    def test_keeps_the_pc_awake_while_a_unit_is_claimed_or_a_run_goes(self):
        self.assertEqual(self.d.keeps_awake(), [])
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        self.assertEqual(self.d.keeps_awake(), ['rytm is claimed'])
        subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
                       env=self.s.env(), capture_output=True)
        with self.d.lock:                         # an open go alone lets the PC sleep
            self.d.approvals['ap-awake'] = dict(session=self.s.sid, created=time.time(),
                                                expires=time.time() + 60, text='t', items=[
                                                    dict(item=('flash', 'rytm', 'ab' * 32), used=False,
                                                         label='flash z on rytm')])
        self.assertEqual(self.d.keeps_awake(), [])
        del self.d.approvals['ap-awake']
        job = daemon.Job(self.s, 'plan', 'rytm', 'emulator', 'x.txt')
        self.d.jobs[job.id] = job
        self.assertEqual(self.d.keeps_awake(), ['a run is going'])
        job.state = 'pass'
        self.assertEqual(self.d.keeps_awake(), [])

    @needs_runner
    def test_one_go_per_run(self):
        plan = os.path.join(PLANS, 'menus.txt')
        info = self.d.plan_info(plan)
        with self.assertRaisesRegex(daemon.Refused, 'no passing emulator run'):
            self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 't'})
        self.fake_pass(info)
        token = self.approve(plans=[plan])
        self.assertTrue(token.startswith('ap-'), token)
        a = {'plan': plan, 'target': 'hardware', 'approval': token}
        saved = daemon.elklock.midi_inputs                  # this PC's ports are not the test's
        daemon.elklock.midi_inputs = lambda: ['Analog Rytm MKII']
        self.addCleanup(setattr, daemon.elklock, 'midi_inputs', saved)
        with self.assertRaisesRegex(daemon.Refused, 'claim rytm first'):
            self.d.t_run_plan(self.s, a)
        r = subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                           env=self.s.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        other = daemon.Session('other-session', os.getpid(), TMP)     # its own run folder
        with self.assertRaisesRegex(daemon.Refused, 'claim rytm first'):
            self.d.t_run_plan(other, a)
        self.d.t_run_plan(self.s, a)
        job, cmd = self.started[-1]
        self.assertEqual(cmd[2:5], ['run', 'rytm', '--'])
        self.assertEqual(cmd[-1], info['path'])
        with self.assertRaisesRegex(daemon.Refused, 'approved once'):
            self.d.t_run_plan(self.s, a)
        with self.assertRaisesRegex(daemon.Refused, 'is unknown'):
            self.d.t_run_plan(self.s, dict(a, approval='ap-forged'))
        # asking while holding the unit idle is refused: ask first, claim after the yes
        with self.assertRaisesRegex(daemon.Refused, 'release rytm first'):
            self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 't'})
        subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
                       env=self.s.env(), capture_output=True)
        token2 = self.approve(plans=[plan])
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        # the approval does not stretch to another plan
        other_plan = os.path.join(PLANS, 'stock-restore.txt')
        self.fake_pass(self.d.plan_info(other_plan))
        with self.assertRaisesRegex(daemon.Refused, 'does not cover'):
            self.d.t_run_plan(self.s, {'plan': other_plan, 'target': 'hardware', 'approval': token2})

    def test_end_go(self):
        other = daemon.Session('test-session-o', os.getpid(), TMP)
        item = ('flash', 'rytm', 'ef' * 32)
        go = lambda sess: dict(session=sess.sid, created=time.time(), expires=time.time() + 60, text='t',
                               items=[dict(item=item, used=False, label='flash z on rytm')])
        with self.d.lock:
            self.d.approvals['ap-mine'], self.d.approvals['ap-left'] = go(self.s), go(other)
        self.assertIn('ap-left', self.d.restart_waits_for()[0])
        listed = '\n'.join(self.d.t_end_go(self.s, {}))
        self.assertIn('2 open go(s)', listed)
        self.assertIn('ap-left', listed)
        # one go at a time: a second while this session has one unused is refused
        with self.assertRaisesRegex(daemon.Refused, 'open go \\(ap-mine\\)'):
            self.d.one_go_at_a_time(self.s)
        # a go whose session has a hardware run going stays
        job = daemon.Job(other, 'plan', 'rytm', 'hardware', 'x.txt')
        self.d.jobs[job.id] = job
        with self.assertRaisesRegex(daemon.Refused, 'has a run going'):
            self.d.t_end_go(self.s, {'approval': 'ap-left'})
        job.state = 'pass'
        # another session's unused go may be ended (it only takes a go away), and stays ended on disk
        self.assertIn('of session', self.d.t_end_go(self.s, {'approval': 'ap-left', 'note': 'left over'})[0])
        self.d.t_end_go(self.s, {'approval': 'ap-mine'})
        self.assertEqual(self.d.restart_waits_for(), [])
        self.assertEqual(daemon.Daemon().open_goes(), {})
        with self.assertRaisesRegex(daemon.Refused, 'was ended at .*left over'):
            daemon.Daemon().use_approval(other, 'ap-left', item, 'flash z')
        with self.assertRaisesRegex(daemon.Refused, 'already ended'):
            self.d.t_end_go(self.s, {'approval': 'ap-left'})
        with self.assertRaisesRegex(daemon.Refused, 'no go ap-none'):
            self.d.t_end_go(self.s, {'approval': 'ap-none'})
        self.d.one_go_at_a_time(self.s)                   # nothing open: a new go may be asked
        # a request still waiting is withdrawn by its own session only, and settles as a No
        ask = daemon.Job(self.s, 'approval', 'rytm', 'hardware', 'approval request')
        ask.settle = lambda ok, how: (setattr(ask, 'state', 'approved' if ok else 'refused'), ['refused: x'])[1]
        self.d.jobs[ask.id] = ask
        with self.assertRaisesRegex(daemon.Refused, 'still waiting'):
            self.d.one_go_at_a_time(self.s)
        with self.assertRaisesRegex(daemon.Refused, 'no go request'):
            self.d.t_end_go(other, {'request': ask.id})
        self.assertTrue(self.d.t_end_go(self.s, {'request': ask.id})[0].startswith('ok: request'))
        self.assertEqual(ask.state, 'refused')
        self.d.one_go_at_a_time(self.s)

    def test_a_go_may_list_an_item_twice(self):
        # The same build flashed twice in one run: each listing runs once.
        item = ('flash', 'rytm', 'ab' * 32)
        listed = lambda: dict(item=list(item), used=False, label='flash X.syx on rytm')
        self.d.approvals['ap-twice'] = dict(session=self.s.sid, expires=time.time() + 60,
                                            items=[listed(), listed()])
        self.d.use_approval(self.s, 'ap-twice', item, 'flash X.syx')
        self.d.use_approval(self.s, 'ap-twice', item, 'flash X.syx')
        with self.assertRaisesRegex(daemon.Refused, 'approved 2 times and has run'):
            self.d.use_approval(self.s, 'ap-twice', item, 'flash X.syx')
        # a flash refused after the check gives back the one listing it took, not both
        self.d.give_back('ap-twice', item)
        self.assertEqual([it['used'] for it in self.d.approvals['ap-twice']['items']], [True, False])
        self.d.use_approval(self.s, 'ap-twice', item, 'flash X.syx')
        with self.assertRaisesRegex(daemon.Refused, 'approved 2 times'):
            self.d.use_approval(self.s, 'ap-twice', item, 'flash X.syx')

    def test_listening_blocks_the_unit(self):
        with self.assertRaisesRegex(daemon.Refused, 'claim rytm first'):
            self.d.t_listen(self.s, {'unit': 'rytm', 'what': 'the kick'})
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        out = self.d.t_listen(self.s, {'unit': 'rytm', 'what': 'the kick'})
        self.assertTrue(out[0].startswith('listening:'), out)
        with self.assertRaisesRegex(daemon.Refused, 'listening'):
            self.d.check_hardware(self.s, ['rytm'])
        with self.assertRaisesRegex(daemon.Refused, 'still listening'):
            self.d.t_release(self.s, {'unit': 'rytm', 'note': 'x'})
        with self.assertRaisesRegex(daemon.Refused, 'id or link'):
            self.d.t_listen_done(self.s, {'unit': 'rytm', 'message_id': 'yes'})
        out = self.d.t_listen_done(self.s, {'unit': 'rytm', 'message_id': 'cmsg_123'})
        self.assertTrue(out[0].startswith('ok: %s has finished' % daemon.PERSON), out)
        self.assertFalse(daemon.elklock.holder('rytm').get('listening'))
        self.d.check_hardware(self.s, ['rytm'])
        with self.assertRaisesRegex(daemon.Refused, 'no listening test'):
            self.d.t_listen_done(self.s, {'unit': 'rytm', 'message_id': 'https://chat.example/m/124'})

    def test_keep_flash_names_no_build(self):
        saved = daemon.ANALOG_EMU
        daemon.ANALOG_EMU = TMP
        try:
            with self.assertRaisesRegex(daemon.Refused, 'no flash to keep'):
                self.d.t_emulator(self.s, {'action': 'start', 'unit': 'a4', 'keep_flash': True})
            with self.assertRaisesRegex(daemon.Refused, 'leave build out'):
                self.d.t_emulator(self.s, {'action': 'start', 'unit': 'a4', 'keep_flash': True, 'build': 'x.syx'})
        finally:
            daemon.ANALOG_EMU = saved

    def test_a_resumed_session_gets_its_new_pid(self):
        s = self.d.session({'session': 'resumed-1', 'pid': 111, 'cwd': TMP})
        self.assertIs(self.d.session({'session': 'resumed-1', 'pid': 222, 'cwd': TMP}), s)
        self.assertEqual((s.env()['ELKBENCH_CLIENT_PID'], s.env()['CLAUDE_PID']), ('222', '222'))

    def test_emulator_work_gives_the_unit_back(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        self.assertEqual(self.d.leave_hardware(self.s, 'shot', {'unit': 'rytm', 'target': 'hardware'}), [])
        self.assertTrue(daemon.elklock.mine(daemon.elklock.holder('rytm'), session=self.s.sid))
        self.d.t_listen(self.s, {'unit': 'rytm', 'what': 'the kick'})
        out = self.d.leave_hardware(self.s, 'run_plan', {'plan': 'p', 'target': 'emulator'})
        self.assertEqual(out, ['kept your rytm claim: %s is listening to it' % daemon.PERSON])
        self.d.t_listen_done(self.s, {'unit': 'rytm', 'message_id': '1729000100.000200'})
        out = self.d.leave_hardware(self.s, 'emulator', {'action': 'start', 'unit': 'a4'})
        self.assertTrue(out[0].startswith('released your rytm claim'), out)
        self.assertIsNone(daemon.elklock.holder('rytm'))
        self.assertIn('went back to emulator testing',
                      json.loads(read(os.path.join(os.environ['ELKLOCK_DIR'], 'rytm.handoff.json')))['note'])

    def test_plan_listen_step_waits_for_the_person(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        # a stand-in for analogrun at a listen step: write the file, wait until it is gone
        step = ('import json, os, time; f = os.environ["ANALOGRUN_LISTEN_FILE"]; '
                'json.dump({"what": "the kick"}, open(f, "w")); '
                '[time.sleep(0.1) for _ in iter(lambda: os.path.exists(f), False)]; print("went on")')
        job = daemon.Job(self.s, 'plan', 'rytm', 'hardware', 'plan listen')

        def done(job, rc, out):
            job.state, job.lines = ('pass' if rc == 0 else 'fail'), [out.strip()]
        daemon.Daemon.start(self.d, job, [sys.executable, '-c', step], TMP, done)
        out = self.d.t_status(self.s, {'run_id': job.id, 'wait_s': 20})
        self.assertTrue(out[-1].startswith('listening: the run waits at a listen step: the kick'), out)
        self.assertTrue(daemon.elklock.holder('rytm').get('listening'))
        with self.assertRaisesRegex(daemon.Refused, 'listening'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'say hi'})
        self.assertEqual(job.state, 'running')
        out = self.d.t_listen_done(self.s, {'unit': 'rytm', 'message_id': 'cmsg_9'})
        self.assertIn('the plan goes on', out[0])
        out = self.d.t_status(self.s, {'run_id': job.id, 'wait_s': 20})
        self.assertEqual(out[0].split(':')[0], 'pass', out)
        self.assertEqual(out[1], 'went on')
        self.assertFalse(daemon.elklock.holder('rytm').get('listening'))

    @needs_runner
    def test_hardware_flash_plan_gets_emulator_run(self):
        plan = os.path.join(PLANS, 'stock-restore.txt')
        info = self.d.plan_info(plan)
        self.assertTrue(info['flash'])
        self.fake_pass(info)
        token = self.approve(plans=[plan])
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        self.d.check_connected = lambda unit, target: None
        self.d.t_run_plan(self.s, {'plan': plan, 'target': 'hardware', 'approval': token})
        twin = self.started[-1][1][-1]
        self.assertNotEqual(twin, info['path'])
        text = read(twin)
        self.assertIn('include = %s' % info['path'], text)
        self.assertIn('emulator-run = %s' % self.d.find_pass(info['key'])['run_dir'], text)
        self.assertNotIn('emulator = yes', text)

    @needs_runner
    def test_check_plan_says_what_a_go_would_refuse(self):
        weak = os.path.join(PLANS, 'weak.txt')
        with open(weak, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nname = weak\nunit = rytm\nrefs = refs/menus\n\n[start]\ntap NO\nshot a.png\n'
                     '[still set]\nsay proves set-and-kept: by eye\nshot b.png\n')
        out = self.d.t_check_plan(self.s, {'plans': [weak, os.path.join(PLANS, 'menus.txt')]})
        self.assertTrue(out[0].startswith('refused: 2 plan(s) checked, 1 refused'), out)
        text = '\n'.join(out)
        self.assertIn('ERROR: [start] has no start check', text)
        self.assertIn('[still set] says it proves set-and-kept but has no machine assertion', text)
        self.assertIn('ok: menus', text)
        self.assertIn('gate: no emulator pass', text)
        self.fake_pass(self.d.plan_info(weak))
        with self.assertRaisesRegex(daemon.Refused, 'weak is refused by the plan checker'):
            self.d.t_approve_plan(self.s, {'plans': [weak], 'purpose': 't'})

    @needs_runner
    def test_approval_text_is_what_approve_plan_asks(self):
        plan = os.path.join(PLANS, 'menus.txt')
        with self.assertRaisesRegex(daemon.Refused, 'no passing emulator run'):
            self.d.t_approval_text(self.s, {'plans': [plan], 'purpose': 't'})
        self.fake_pass(self.d.plan_info(plan))
        out = self.d.t_approval_text(self.s, {'plans': [plan], 'purpose': 't'})
        tid = re.search(r'text_id (\w+)', out[0]).group(1)
        body = '\n'.join(out[2:])
        self.assertIn('proves: start: [start]; works: [project menu]', body)
        self.assertFalse(any(j.kind == 'approval' for j in self.d.jobs.values() if j.sess is self.s
                             and j.state == 'running'))
        with self.assertRaisesRegex(daemon.Refused, 'changed since approval_text'):
            self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 'other', 'text_id': tid})
        seen = []
        daemon.ask_person = lambda title, text: (seen.append(text), True)[1]
        self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 't', 'text_id': tid})
        for _ in range(50):
            if seen:
                break
            time.sleep(0.05)
        self.assertEqual(seen, [body])

    def test_unit_state_reads_nothing_from_the_unit(self):
        out = self.d.t_unit_state(self.s, {'unit': 'rytm'})
        self.assertTrue(out[0].startswith('ok: rytm as known now'), out)
        self.assertIn('projects: no hardware listing recorded yet', '\n'.join(out))
        os.makedirs(os.path.join(daemon.STATE, 'unit-state'), exist_ok=True)
        daemon.write_json(os.path.join(daemon.STATE, 'unit-state', 'rytm.json'),
                          {'time': time.time() - 120, 'projects': ['121 ~T SETUP']})
        out = '\n'.join(self.d.t_unit_state(self.s, {'unit': 'rytm'}))
        self.assertIn('  121 ~T SETUP', out)
        self.assertIn('2 min ago', out)

    @needs_runner
    def test_no_dialog_no_token(self):
        plan = os.path.join(PLANS, 'menus.txt')
        self.fake_pass(self.d.plan_info(plan))
        daemon.ask_person = lambda title, text: False
        out = self.d.t_approve_plan(self.s, {'plans': [plan], 'purpose': 't'})
        rid = out[0].split()[1].rstrip(',')
        time.sleep(0.2)
        self.assertEqual(self.d.jobs[rid].state, 'refused')

    def test_status_waits_for_the_end(self):
        job = daemon.Job(self.s, 'plan', 'rytm', 'emulator', 'plan t')
        self.d.jobs[job.id] = job

        def finish():
            time.sleep(1.0)
            job.state, job.lines = 'pass', ['t: PASS']
        threading.Thread(target=finish, daemon=True).start()
        t0 = time.time()
        out = self.d.t_status(self.s, {'run_id': job.id, 'wait_s': 30})
        self.assertTrue(out[0].startswith('pass: ') and out[1] == 't: PASS', out)
        self.assertLess(time.time() - t0, 5)
        job.state = 'running'
        out = self.d.t_status(self.s, {'run_id': job.id, 'wait_s': 0})
        self.assertTrue(out[0].startswith('running: '), out)

    @needs_runner
    def test_emulator_run_needs_no_claim_and_uses_a_twin(self):
        with self.assertRaisesRegex(daemon.Refused, 'no rytm emulator is running'):
            daemon.Daemon.check_connected(type('D', (), {'emulator_procs': lambda self, u: []})(),
                                          'rytm', 'emulator')
        self.d.emulator_procs = lambda unit: [1234]       # someone else's: never used
        with self.assertRaisesRegex(daemon.Refused, 'this session has no rytm emulator'):
            self.d.t_run_plan(self.s, {'plan': os.path.join(PLANS, 'menus.txt'), 'target': 'emulator'})
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        self.d.emulators[(self.s.sid, 'rytm')] = dict(proc=proc, session=self.s.sid, build='x',
                                                      address='tcp:127.0.0.1:7399')
        self.d.t_run_plan(self.s, {'plan': os.path.join(PLANS, 'menus.txt'), 'target': 'emulator'})
        job, stages = self.started[-1]
        self.assertTrue(callable(stages[0]) and callable(stages[-1]))   # the saved-project check
        cmd = stages[1]
        self.assertIn('tcp:127.0.0.1:7399', cmd)
        self.assertTrue(cmd[-1].startswith(os.environ['ELKBENCH_DIR']))

    @needs_runner
    def test_failed_plan_keeps_its_failure_lines(self):
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        self.d.emulators[(self.s.sid, 'rytm')] = dict(proc=proc, session=self.s.sid, build='x',
                                                      address='tcp:127.0.0.1:7399')
        self.d.t_run_plan(self.s, {'plan': os.path.join(PLANS, 'menus.txt'), 'target': 'emulator'})
        job, _ = self.started[-1]
        out = '\n'.join(['P on the emulator: FAIL (9 steps, 137 s) - %s' % TMP] +
                        ['  ok   step %d' % i for i in range(1, 7)] +
                        ['  FAIL 6 euclid', '  skip 7 humanise',
                         'first failure, 6 euclid: AssertionError: screen differs',
                         '    ----.---.', '    #--.-----', '    ----.---. (saved s6-got.png, -diff.png)'])
        self.done(job, 1, out)
        self.assertEqual(job.state, 'fail')
        self.assertIn('6 step(s) ok', job.lines)
        self.assertIn('FAIL 6 euclid', job.lines)
        self.assertTrue(any(l.startswith('first failure, 6 euclid') for l in job.lines), job.lines)
        self.assertIn('#--.-----', job.lines)                  # the diff picture, after the lines
        ev = job.lines.index('evidence: %s' % TMP)
        self.assertEqual(job.lines[ev - 1], '----.---. (saved s6-got.png, -diff.png)')
        self.assertEqual(job.lines[0], 'P on the emulator: FAIL (9 steps, 137 s)')   # path once, in evidence
        self.assertEqual(job.lines[ev + 1], 'log: %s' % os.path.join(TMP, 'log.txt'))

    def test_flash_result_keeps_what_the_log_says(self):
        self.d.emu_ready = lambda s, unit, build, snap=None: ('tcp:127.0.0.1:7399', [])
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        self.d.emulators[(self.s.sid, 'rytm')] = dict(proc=proc, session=self.s.sid, build='x',
                                                      address='tcp:127.0.0.1:7399')
        os.makedirs(daemon.STATE, exist_ok=True)              # where a pass is recorded
        flog = os.path.join(self.s.folder('rytm'), 'elkflash.log')
        with open(flog, 'w', encoding='utf-8') as fh:
            fh.write('Committed: an earlier flash\n')             # elkflash appends: not this one's
        self.d.t_flash(self.s, {'unit': 'rytm', 'image': self.saved['stock'][0], 'target': 'emulator'})
        job, _ = self.started[-1]
        with open(flog, 'a', encoding='utf-8') as fh:
            fh.write('sending 30720-byte chunks\nsent 1048576 bytes in 35 chunks, 40.0 s (25.6 KiB/s), 0 resends\n'
                     'Committed: the device is erasing and writing its flash now and restarts by itself.\n'
                     'waiting\rback after 31 s: Analog Rytm MKII, OS 1.74\n')
        self.done(job, 0, 'ok: back after 31 s: Analog Rytm MKII, OS 1.74\n')
        self.assertRegex(job.lines[0], r'^elkbench: %s flash \S+ on rytm starts ' % job.id)
        self.assertEqual(job.lines[1:4], ['sent 1048576 bytes in 35 chunks, 40.0 s (25.6 KiB/s), 0 resends',
                                          'Committed: the device is erasing and writing its flash now and '
                                          'restarts by itself.',
                                          'ok: back after 31 s: Analog Rytm MKII, OS 1.74'])
        self.assertEqual(job.lines[4], 'log: %s' % flog)

    def test_flash_logs_every_request_and_the_quiet_time_before_it(self):
        self.d.emu_ready = lambda s, unit, build, snap=None: ('tcp:127.0.0.1:7399', [])
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        self.d.emulators[(self.s.sid, 'rytm')] = dict(proc=proc, session=self.s.sid, build='x',
                                                      address='tcp:127.0.0.1:7399')
        os.makedirs(daemon.STATE, exist_ok=True)
        port = self.d.busy_port('rytm@tcp:127.0.0.1:7399', 'emulator', 'plan setup test project in 121')
        port.release()
        port.freed -= 4.2                                     # the plan ended 4.2 s ago
        self.d.t_flash(self.s, {'unit': 'rytm', 'image': self.saved['stock'][0], 'target': 'emulator'})
        job, cmd = self.started[-1]
        self.assertIn('--verbose', cmd)                       # with --quiet: into elkflash.log
        self.assertEqual(cmd[cmd.index('--verbose') - 1], '--quiet')
        flog = os.path.join(self.s.folder('rytm'), 'elkflash.log')
        with open(flog, encoding='utf-8') as fh:
            first = fh.read().splitlines()[-1]                # written before elkflash appends
        self.assertRegex(first, r'^elkbench: %s flash \S+ on rytm starts \d\d:\d\d:\d\d, 4\.\d s after the '
                                r'last use of this port ended \(plan setup test project in 121\)$' % job.id)
        with open(flog, 'a', encoding='utf-8') as fh:
            fh.write('no reply to PING, retrying (2/3)\n')
        self.done(job, 1, 'failed (exit 1): error: no reply to command 0x01 (seq 1) in 9 s\n')
        self.assertEqual(job.lines[0], first)
        self.assertIn('no reply to PING, retrying (2/3)', job.lines)   # a resend shows in status

    def test_port_lock_remembers_the_user_before(self):
        lk = self.d.busy_port('rytm', 'hardware', 'data ls')
        self.assertEqual(lk.before, (None, None))
        self.assertIn('no earlier use of this port', daemon.flash_start_line(
            daemon.Job(self.s, 'flash', 'rytm', 'hardware', 'flash X.syx'), lk))
        lk.release()
        freed = lk.freed
        again = self.d.busy_port('rytm', 'hardware', 'flash X.syx')
        self.assertIs(again, lk)
        self.assertEqual(again.before, (freed, 'data ls'))
        line = daemon.flash_start_line(daemon.Job(self.s, 'flash', 'rytm', 'hardware', 'flash X.syx'), again,
                                       now=freed + 12.34)
        self.assertTrue(line.endswith('12.3 s after the last use of this port ended (data ls)'), line)
        with self.assertRaises(daemon.Refused):
            self.d.busy_port('rytm', 'hardware', 'steps')             # still one user per port
        again.release()

    def test_saved_project_changes(self):
        steps = [('save', ['save-project 126 "~T X" GLOBAL,YES']), ('more', ['keys YES', 'save-project 7'])]
        self.assertEqual(daemon.saved_slots(steps), {126, 7})
        before = {1: ('MINE', 'a'), 2: ('OTHER', 'b'), 126: ('~T OLD', 'c')}
        self.assertEqual(daemon.project_changes(before, {**before, 126: ('~T X', 'd')}, {126}), [])
        self.assertEqual(daemon.project_changes(before, {1: ('MINE', 'z'), 3: ('NEW', 'e'), 126: ('~T X', 'd')},
                                                {126}),
                         ["project 1 changed ('MINE')", "project 2 removed (was 'OTHER')", "project 3 added ('NEW')"])
        self.assertEqual(daemon.project_changes(before, {**before, 2: ('~T OTHER', 'b')}, set()),
                         ["project 2 renamed 'OTHER' -> '~T OTHER'"])

    def test_emulator_plan_fails_on_a_write_outside_its_slots(self):
        reads = [{1: ('MINE', 'a'), 126: ('~T OLD', 'c')}, {1: ('MINE', 'z'), 126: ('~T X', 'd')}]
        self.d.saved_projects = lambda addr, folder: reads.pop(0)
        info = {'steps': [('s', ['save-project 126 "~T X" YES'])]}
        before, run, after = self.d.guard_projects(info, 'tcp:127.0.0.1:7399', ['run'], TMP)
        self.assertEqual(run, ['run'])
        self.assertEqual(before(0, ''), (0, ''))
        rc, out = after(0, '')
        self.assertEqual(rc, 1)
        self.assertIn("project 1 changed ('MINE')", out)
        reads[:] = [{1: ('MINE', 'a')}, {1: ('MINE', 'a'), 126: ('~T X', 'd')}]
        before(0, '')
        self.assertEqual(after(0, '')[0], 0)

    def test_every_unit_with_a_runner_guards_its_saved_projects(self):
        for unit in ('rytm', 'a4', 'digitone', 'digitakt'):
            if unit in daemon.UNITS:
                self.assertTrue(daemon.guards_projects(unit), unit)
        self.assertFalse(daemon.guards_projects('no-such-unit'))

    @needs_runner
    def test_digitone_emulator_plan_guards_its_saved_projects(self):
        plan = os.path.join(TMP, 'dn-guard.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('unit = digitone\n[home]\nexpect-screen home 2\n')
        proc = type('P', (), {'poll': lambda self: None, 'pid': 97})()
        saved = daemon.DIGIHEALTH
        daemon.DIGIHEALTH = TMP
        guarded = []
        self.d.guard_projects = lambda info, addr, cmd, folder: (guarded.append(addr), ['guarded'])[1]
        self.d.emulators[(self.s.sid, 'digitone')] = dict(proc=proc, session=self.s.sid, build='b',
                                                          address='tcp:127.0.0.1:7402')
        try:
            self.d.t_run_plan(self.s, {'plan': plan, 'target': 'emulator'})
        finally:
            daemon.DIGIHEALTH = saved
            self.d.emulators.pop((self.s.sid, 'digitone'), None)
        self.assertEqual(guarded, ['tcp:127.0.0.1:7402'])
        self.assertEqual(self.started[-1][1], ['guarded'])

    def test_hand_steps_are_named(self):
        steps = [('setup', ['keys PLAY', 'listen the kick plays']), ('turn (hardware)', ['turn A 1']),
                 ('check', ['shot s1', 'listen  "the snare, then silence"  # by hand'])]
        self.assertEqual(daemon.hand_steps(steps), ['[setup] listen the kick plays',
                                                    '[turn (hardware)] (hardware section)',
                                                    '[check] listen the snare, then silence'])

    def test_run_files_in_saved_order(self):
        d = tempfile.mkdtemp(dir=TMP)
        for i, n in enumerate(['b.png', 'a.png', 'log.txt']):
            open(os.path.join(d, n), 'w').close()
            os.utime(os.path.join(d, n), (1000 + i, 1000 + i))
        self.assertEqual(daemon.run_files(d), ['b.png', 'a.png'])
        self.assertEqual(daemon.run_files(d, limit=1), ['b.png', '(1 more)'])
        self.assertEqual(daemon.run_files(os.path.join(d, 'none')), [])

    def test_near_miss_names(self):
        self.assertEqual(daemon.tidy_call('Run_Plan', {'Plan': 'p', 'target': 'Emulator', '_elicit': True}),
                         ('run_plan', {'plan': 'p', 'target': 'emulator', '_elicit': True}))
        self.assertEqual(daemon.tidy_call('shot', {'unit': 'Rytm', 'target': 'hardware'})[1]['unit'], 'rytm')
        self.assertEqual(daemon.tidy_call('claim', {'unit': ['A4', 'Rytm'], 'purpose': 'x'})[1]['unit'],
                         ['a4', 'rytm'])
        with self.assertRaisesRegex(daemon.Refused, "takes no argument 'run'.*run_id"):
            daemon.tidy_call('status', {'run': 'r-1'})
        with self.assertRaisesRegex(daemon.Refused, "not one of emulator, hardware"):
            daemon.tidy_call('flash', {'unit': 'rytm', 'image': 'x', 'target': 'emu'})
        with self.assertRaisesRegex(daemon.Refused, 'no tool'):
            daemon.tidy_call('plan', {})

    def test_build_boots_a_fresh_emulator(self):
        calls = []
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99, 'terminate': lambda self: calls.append('stop'),
                              'wait': lambda self, t: 0})()

        def start(s, a, snapshot=None, power_off=False):
            calls.append(('start', a['build']))
            self.d.emulators[(s.sid, a['unit'])] = dict(proc=proc, session=s.sid, build='b',
                                                        address='tcp:127.0.0.1:7400')
            return ['ok: rytm emulator running', 'its first boot shows two prompts']
        self.d.t_emulator = start
        self.assertEqual(self.d.emu_ready(self.s, 'rytm', 'B.syx'),
                         ('tcp:127.0.0.1:7400', ['ok: rytm emulator running']))
        self.assertEqual(self.d.emu_ready(self.s, 'rytm', 'B.syx')[0], 'tcp:127.0.0.1:7400')
        self.assertEqual(calls, [('start', 'B.syx'), 'stop', ('start', 'B.syx')])
        self.assertEqual(self.d.emu_ready(self.s, 'rytm', None), ('tcp:127.0.0.1:7400', []))

    @needs_runner
    def test_setup_snapshot_split(self):
        plan = os.path.join(TMP, 'snap-plan.txt')
        with open(os.path.join(PLANS, 'menus.txt'), encoding='utf-8') as fh:
            text = fh.read()
        first = re.search(r'^\[(.+)\]$', text, re.M).group(1)
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('format = 1', 'format = 1\nsnapshot-after = %s\nrefs = %s'
                                  % (first, os.path.join(PLANS, 'refs')), 1))
        info = self.d.plan_info(plan)
        self.assertEqual(info['setup'], 1)
        setup = self.d.twin(info, False, self.s.sid, sections=info['steps'][:1], tag=' setup')
        rest = self.d.twin(info, False, self.s.sid, sections=info['steps'][1:], tag=' after setup')
        import testplan
        self.assertEqual([n for n, _ in testplan.parse(setup)[1]], [first])
        self.assertEqual(testplan.parse(rest)[1], info['steps'][1:])
        self.assertEqual(testplan.parse(rest)[0]['name'], info['name'] + ' after setup')
        build = os.path.join(TMP, 'b.syx')
        with open(build, 'wb') as fh:
            fh.write(b'x')
        k = self.d.snapshot_key(info, build)
        with open(build, 'wb') as fh:
            fh.write(b'y')
        self.assertNotEqual(k, self.d.snapshot_key(info, build))     # another build, another snapshot
        bad = plan + '.bad'
        with open(bad, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('format = 1', 'format = 1\nsnapshot-after = nope', 1))
        with self.assertRaisesRegex(daemon.Refused, 'snapshot-after names no section'):
            self.d.plan_info(bad)
        # nothing the emulator runs after the setup: every resumed run would have no step
        last = re.findall(r'^\[(.+)\]$', text, re.M)[-1]
        with open(bad, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('format = 1', 'format = 1\nsnapshot-after = %s' % last, 1))
        with self.assertRaisesRegex(daemon.Refused, 'leaves no section the emulator runs after it'):
            self.d.plan_info(bad)

    def fake_power_off_emulator(self, unit='rytm'):
        """A stand-in analog-emu in TMP: it says its USB MIDI port listens and, given
        --power-off, prints the power-off save on `quit` or the end of stdin (FAKE_EMU_NO_SAVE:
        the lines of a power-off whose save did not finish)."""
        folder = os.path.join(TMP, 'fake-emu')
        os.makedirs(folder, exist_ok=True)
        script = os.path.join(folder, 'fake-rytm.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write('#!%s\n' % sys.executable + (
                '# power off: no --card, nothing kept (the mark elkbench looks for in a build)\n'
                'import os, sys\n'
                'a = sys.argv\n'
                'print("usb midi tcp: listening on", a[a.index("--usb-midi-tcp") + 1], flush=True)\n'
                'if "--din-midi-tcp" in a: print("din midi tcp: listening", flush=True)\n'
                'print("args", " ".join(a[1:]), flush=True)\n'
                'if "--stdin-commands" not in a:\n'
                '    import time; time.sleep(120)\n'
                'for line in sys.stdin:\n'
                '    if line.strip() == "quit": break\n'
                'if "--power-off" in a:\n'
                '    if os.environ.get("FAKE_EMU_NO_SAVE"):\n'
                '        print("power off: the firmware did not finish its power-fail handler")\n'
                '    else:\n'
                '        print("power off: working state saved")\n'
                '        print("wrote card", a[a.index("--card") + 1] if "--card" in a else "-")\n'))
        if os.name == 'nt':                  # Windows starts a .cmd, not a shebang script
            exe = 'rytm.cmd'
            with open(os.path.join(folder, exe), 'w', encoding='utf-8') as fh:
                fh.write('@"%s" "%s" %%*\r\n' % (sys.executable, script))
                fh.write('@rem power off: no --card, nothing kept\r\n')
        else:
            exe = os.path.basename(script)
            os.chmod(script, 0o755)
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU, daemon.ELKFLASH = folder, TMP
        daemon.UNITS[unit].update(emu_exe=exe, emu_card=None)
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        self.d.wait_for_os = lambda addr, tries=30: True
        self.addCleanup(lambda: [self.d.stop_emulator(e) for e in self.d.emulators.values()])
        return folder

    def test_power_cycle_parts(self):
        steps = [('a', ['tap NO', 'power-cycle', 'tap YES']), ('b', ['wait 1']),
                 ('c', ['say proves works: x', 'power-cycle  # by hand on the unit']), ('d', ['expect-screen x 1'])]
        parts = daemon.power_cycle_parts(steps)
        self.assertEqual(parts, [[('a', ['tap NO'])], [('a, after the power cycle', ['tap YES']), ('b', ['wait 1']),
                                                       ('c', ['say proves works: x'])], [('d', ['expect-screen x 1'])]])
        self.assertEqual(daemon.power_cycles(steps), ['[a] power-cycle', '[c] power-cycle'])
        self.assertEqual(daemon.power_cycle_parts([('a', ['power-cycle', 'tap NO'])]),
                         [[], [('a, after the power cycle', ['tap NO'])]])
        self.assertEqual(daemon.power_cycle_parts([('a', ['tap NO'])]), [[('a', ['tap NO'])]])

    @needs_runner
    def test_power_cycle_plans_refused(self, unit='rytm'):
        plan = self.sample('power-cycle.txt', unit)
        text = read(plan)
        info = self.d.plan_info(plan)
        self.assertEqual(info['power_cycles'], ['[kept] power-cycle'])
        for change, why in (
                (lambda t: t + 'power-cycle\n', 'power cycle 2 has no machine assertion'),
                (lambda t: t.replace('power-cycle', 'power-cycle now'), 'takes nothing more'),
                (lambda t: t.replace('[kept]', '[kept (hardware)]'), r'in a \(hardware\) section'),
                # a section after the setup, so the snapshot-after check itself passes
                (lambda t: t.replace('format = 1', 'format = 1\nsnapshot-after = kept') + '\n[after]\nsay done\n',
                 'in the setup'),
                (lambda t: t.replace('unit = %s' % unit, 'unit = digitone'), '(?i)Rytm and A4 only|digi'),
                (lambda t: t.replace('format = 1', 'format = 1\nflash = %s' % os.path.join(TMP, 'os', 'test-build.syx')),
                 'cannot flash')):
            bad = os.path.join(TMP, 'pc-bad.txt')
            with open(bad, 'w', encoding='utf-8') as fh:
                fh.write(change(text).replace('refs = refs/menus', 'refs = %s' % os.path.join(PLANS, 'refs', 'menus')))
            with self.assertRaisesRegex(daemon.Refused, why):
                self.d.plan_info(bad)
        noread = os.path.join(TMP, 'pc-noread.txt')
        with open(noread, 'w', encoding='utf-8') as fh:
            fh.write(text.rsplit('expect-screen', 1)[0].replace('refs = refs/menus', 'refs = %s' % os.path.join(
                PLANS, 'refs', 'menus')) + 'shot readback.png\n')
        with self.assertRaisesRegex(daemon.Refused, 'power cycle 1 has no machine assertion'):
            self.d.plan_info(noread)
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        with self.assertRaisesRegex(daemon.Refused, 'batches do not power cycle yet'):
            self.d.t_batch(self.s, {'plan': plan, 'variants': [{}], 'build': build})
        self.d.emu_has_snapshots = lambda unit: False
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        # a gate runs only start runs that stop before the first power cycle: a run from a start
        # that must pass (no contrast for it) would reach the power cycle
        self.with_starts(unit)
        with self.assertRaisesRegex(daemon.Refused, r'start dirty \(must pass\).* would reach the power cycle'):
            self.d.t_batch(self.s, {'gate': True, 'plans': [plan], 'build': build})
        late = os.path.join(TMP, 'pc-late.txt')
        with open(late, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('refs = refs/menus', 'refs = %s' % os.path.join(PLANS, 'refs', 'menus')).replace(
                'power-cycle\n', 'power-cycle\nsay contrast fresh, dirty, occupied, testslot: x\n'))
        with self.assertRaisesRegex(daemon.Refused, r'stop in \[kept\]\).* would reach'):
            self.d.t_batch(self.s, {'gate': True, 'plans': [late], 'build': build})

    def test_emulator_usb_midi_real(self):
        """units.rytm.emu_usb_midi_real: the emulator starts with --usb-midi-real (the firmware's own
        USB MIDI sender, so mod SysEx replies come back); refused by an analog-emu without it."""
        self.fake_power_off_emulator('rytm')
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        self.d.t_emulator(self.s, {'action': 'start', 'unit': 'rytm', 'build': build})
        e = self.d.emulators[(self.s.sid, 'rytm')]
        self.assertNotIn('--usb-midi-real', e['cmd'])          # off unless config.toml sets it
        self.d.stop_emulator(e)
        daemon.UNITS['rytm']['emu_usb_midi_real'] = True
        self.addCleanup(lambda: daemon.UNITS['rytm'].pop('emu_usb_midi_real', None))
        saved = self.d.emu_help
        self.addCleanup(lambda: setattr(self.d, 'emu_help', saved))
        self.d.emu_help = lambda unit: 'usage: rytm [--usb-midi-tcp ADDR]'
        with self.assertRaisesRegex(daemon.Refused, 'has no --usb-midi-real'):
            self.d.t_emulator(self.s, {'action': 'start', 'unit': 'rytm', 'build': build})
        self.assertEqual(self.d.batch_emulator('rytm', build, TMP, 1)[2][:4], 'unit')
        self.d.emu_help = lambda unit: 'usage: rytm [--usb-midi-tcp ADDR] [--usb-midi-real]'
        self.d.t_emulator(self.s, {'action': 'start', 'unit': 'rytm', 'build': build})
        e = self.d.emulators[(self.s.sid, 'rytm')]
        self.assertIn('--usb-midi-real', e['cmd'])

    def test_power_cycle_emulator(self, unit='rytm'):
        self.fake_power_off_emulator(unit)
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        self.assertTrue(self.d.emu_has_power_off(unit))
        build = os.path.join(TMP, 'os', 'test-build.syx')
        self.d.t_emulator(self.s, {'action': 'start', 'unit': unit, 'build': build}, power_off=True)
        e = self.d.emulators[(self.s.sid, unit)]
        self.assertIn('--power-off', e['cmd'])
        self.assertIn('--stdin-commands', e['cmd'])
        first = e['address']
        rc, line = self.d.power_cycle(self.s, unit)
        self.assertEqual(rc, 0, line)
        self.assertIn('working state saved', line)
        self.assertNotEqual(e['address'], first)               # new ports, and the address follows
        self.assertEqual(e['boots'], 2)
        self.assertIsNone(e['proc'].poll())
        self.assertTrue(e['log'].endswith('emulator-boot2.log'))
        rc, line = self.d.power_cycle(self.s, unit, save=False)  # the no-save contrast: killed
        self.assertEqual(rc, 0, line)
        self.assertIn('killed, no save', line)
        os.environ['FAKE_EMU_NO_SAVE'] = '1'
        self.addCleanup(lambda: os.environ.pop('FAKE_EMU_NO_SAVE', None))
        self.d.stop_emulator(e)
        self.d.t_emulator(self.s, {'action': 'start', 'unit': unit, 'build': build}, power_off=True)
        rc, line = self.d.power_cycle(self.s, unit)
        self.assertEqual(rc, 1)
        self.assertIn('did not finish its power-off save', line)
        self.assertIn('did not finish its power-fail handler', line)
        self.d.t_emulator(self.s, {'action': 'start', 'unit': unit, 'build': build})   # without --power-off
        self.assertIn('did not start with --power-off', self.d.power_cycle(self.s, unit)[1])
        self.d.stop_emulator(self.d.emulators[(self.s.sid, unit)])
        # power_off on the emulator tool: a plan with power-cycle steps then runs on it without build
        self.d.t_emulator(self.s, {'action': 'start', 'unit': unit, 'build': build, 'power_off': True})
        self.assertIn('--power-off', self.d.emulators[(self.s.sid, unit)]['cmd'])
        self.assertEqual(self.d.emu_ready(self.s, unit, None, power_off=True)[1], [])
        self.d.stop_emulator(self.d.emulators[(self.s.sid, unit)])

    def test_digiemu_power_cycle(self):
        """digiemu's power cycle: `power-off` on its stdin, its save line, its boot on that card
        on the same link, then the OS's answer; a save that did not finish fails it."""
        tools = os.path.join(TMP, 'digiemu-po', 'tools')
        os.makedirs(tools, exist_ok=True)
        saved = daemon.DIGIEMU, daemon.DIGIHEALTH
        self.addCleanup(lambda: setattr(daemon, 'DIGIEMU', saved[0]) or setattr(daemon, 'DIGIHEALTH', saved[1]))
        daemon.DIGIEMU, daemon.DIGIHEALTH = os.path.dirname(tools), None
        with open(os.path.join(tools, 'usbrun.py'), 'w', encoding='utf-8') as fh:
            fh.write("ap.add_argument('--ob-audio-port')\n")
        self.assertFalse(self.d.digiemu_has_power_off())
        with open(os.path.join(tools, 'usbrun.py'), 'w', encoding='utf-8') as fh:
            fh.write("ap.add_argument('--power-off', action='store_true')\n")
        self.assertTrue(self.d.digiemu_has_power_off())
        daemon.DIGIEMU_UP_WAIT = 20
        script = os.path.join(TMP, 'fake-usbrun.py')
        with open(script, 'w', encoding='utf-8') as fh:
            fh.write('import os, sys, time\n'
                     'print("[usbrun] USB MIDI link on 127.0.0.1:7999", flush=True)\n'
                     'for line in sys.stdin:\n'
                     '    if line.strip() == "power-off":\n'
                     '        print("[usbrun] power off: supply cut", flush=True)\n'
                     '        if os.environ.get("FAKE_DIGIEMU_NO_SAVE"):\n'
                     '            print("[usbrun] power off: the OS did not finish its power-off save within 5 s"\n'
                     '                  " (0 blocks written)", flush=True)\n'
                     '            break\n'
                     '        print("[usbrun] power off: working state saved (5440 blocks written)", flush=True)\n'
                     '        print("[usbrun] power off: wrote card", flush=True)\n'
                     '        print("[usbrun] switching on again into X.syx", flush=True)\n'
                     '        time.sleep(1)\n'
                     '        print("[usbrun] up again after 61 s: gui.snap", flush=True)\n'
                     'time.sleep(30)\n')
        for no_save in (False, True):
            os.environ.pop('FAKE_DIGIEMU_NO_SAVE', None)
            if no_save:
                os.environ['FAKE_DIGIEMU_NO_SAVE'] = '1'
            self.addCleanup(lambda: os.environ.pop('FAKE_DIGIEMU_NO_SAVE', None))
            log_path = os.path.join(TMP, 'digiemu-po.log')
            log = open(log_path, 'w', encoding='utf-8')
            self.addCleanup(log.close)
            proc = subprocess.Popen([sys.executable, script], stdin=subprocess.PIPE, stdout=log,
                                    stderr=subprocess.STDOUT, env=dict(os.environ))
            self.addCleanup(proc.kill)
            e = dict(proc=proc, session=self.s.sid, build='X.syx', address='tcp:127.0.0.1:7999', log=log_path,
                     power_off=True, boots=1)
            self.d.emulators[(self.s.sid, 'digitone')] = e
            rc, line = self.d.power_cycle(self.s, 'digitone')
            if no_save:
                self.assertEqual(rc, 1, line)
                self.assertIn('did not finish its power-off save', line)
            else:
                self.assertEqual(rc, 0, line)
                self.assertIn('working state saved (5440 blocks written)', line)
                self.assertIn('up again at tcp:127.0.0.1:7999', line)
                self.assertEqual(e['boots'], 2)
            proc.kill()
            proc.wait()
        e['power_off'] = False
        proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        self.addCleanup(proc.kill)
        e['proc'] = proc
        self.assertIn('did not start with --power-off', self.d.power_cycle(self.s, 'digitone')[1])
        proc.kill()
        proc.wait()

    @needs_runner
    def test_power_cycle_run_plan(self, unit='rytm'):
        self.fake_power_off_emulator(unit)
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        plan = self.sample('power-cycle.txt', unit)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        self.d.guard_projects = lambda info, addr, cmd, folder: cmd
        self.d.t_run_plan(self.s, {'plan': plan, 'target': 'emulator', 'build': build})
        job, cmd = self.started[-1]
        self.assertEqual([c.__name__ for c in cmd], ['part 1 of the plan',
                                                     'power cycle 1 (emulator: power-off save, cold boot)',
                                                     'part 2 of the plan'])
        self.assertEqual(cmd[1](0, '')[0], 0)                 # the power cycle itself
        info = self.d.plan_info(plan)
        import testplan
        hw = self.d.plan_stages(self.s, info, job, 'hardware', info['steps'])
        self.assertTrue(isinstance(hw[0], list) and callable(hw[1]) and isinstance(hw[2], list))
        part2 = testplan.parse(hw[2][-1])
        self.assertEqual([n for n, _ in part2[1]], ['kept, after the power cycle'])
        self.assertNotEqual(part2[0].get('emulator'), 'yes')
        self.assertNotIn('power-cycle', ''.join(l for _, ls in part2[1] for l in ls))
        res = []
        t = threading.Thread(target=lambda: res.append(hw[1](0, '')))
        t.start()
        for _ in range(50):
            if os.path.exists(job.listen_file):
                break
            time.sleep(0.1)
        flag = daemon.read_json(job.listen_file, {})
        self.assertTrue(flag['power_cycle'])
        self.assertIn('power cycle the %s by hand' % unit, flag['what'])
        os.remove(job.listen_file)                             # listen_done, on the person's word
        t.join(5)
        self.assertEqual(res[0][0], 0)
        self.fake_pass(info)
        _, body = self.d.go_request(self.s, {'plans': [plan]})
        self.assertIn('POWER CYCLE BY HAND', body)
        self.assertIn('[kept] power-cycle', body)

    @needs_runner
    def test_batch_snapshot(self):
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP              # configured: the runs are stand-ins
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        plain = os.path.join(PLANS, 'menus.txt')
        with self.assertRaisesRegex(daemon.Refused, 'snapshot is for plan batches'):
            self.d.t_batch(self.s, {'unit': 'rytm', 'image': build, 'build': build, 'snapshot': True,
                                    'variants': [{}]})
        with self.assertRaisesRegex(daemon.Refused, 'variant A: snapshot needs a hardware plan with snapshot-after'):
            self.d.t_batch(self.s, {'plan': plain, 'build': build, 'snapshot': True, 'variants': [{}]})
        plan = os.path.join(TMP, 'snap-batch.txt')
        text = read(plain)
        first = re.search(r'^\[(.+)\]$', text, re.M).group(1)
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('format = 1', 'format = 1\nsnapshot-after = %s\nrefs = %s'
                                  % (first, os.path.join(PLANS, 'refs')), 1))
        self.d.emu_has_snapshots = lambda unit: True
        prepared, got = [], []

        def snap(s, unit, v, minutes, folder):
            prepared.append(v['name'])
            v['store'] = os.path.join(TMP, 'stored-%s.snap' % v['name'])
        self.d.batch_snapshot = snap
        self.d.batch_run = lambda s, unit, flash, minutes, folder, n, v: (
            got.append(v.get('store')), dict(outcome='pass', line='ok'))[1]
        out = self.d.t_batch(self.s, {'plan': plan, 'build': build, 'snapshot': True, 'runs': 2,
                                      'variants': [{'name': 'one'}, {'name': 'two'}]})
        self.assertIn("each variant's setup snapshot first", out[0])
        res = self.d.t_status(self.s, {'run_id': out[0].split('run_id ')[1].split()[0], 'wait_s': 30})
        self.assertIn('resumed from setup snapshots', res[1])
        self.assertEqual(sorted(prepared), ['one', 'two'])           # each variant's setup once
        self.assertEqual(sorted(got), sorted([os.path.join(TMP, 'stored-%s.snap' % n) for n in
                                              ('one', 'one', 'two', 'two')]))
        info = self.d.plan_info(plan)                                # a resumed run: only the rest
        work = tempfile.mkdtemp(dir=TMP)
        import testplan
        rest = testplan.parse(self.d.batch_plan(info, work, 'batch run 1', sections=info['steps'][1:]))
        self.assertEqual(rest[1], info['steps'][1:])
        self.assertEqual(rest[0]['out'], work)

    def test_snapshots_are_pruned(self):
        folder = os.path.join(daemon.STATE, 'snapshots')
        os.makedirs(folder, exist_ok=True)
        now = time.time()

        def snap(name, age_days):
            p = os.path.join(folder, name + '.snap')
            for q in (p, p + '.json'):
                with open(q, 'w') as fh:
                    fh.write('x')
                os.utime(q, (now - age_days * 86400,) * 2)
            return p
        old = snap('old', daemon.SNAPSHOT_DAYS + 1)
        recent = [snap('r%02d' % i, i / 100) for i in range(daemon.SNAPSHOT_MAX + 2)]
        self.d.prune_snapshots(keep=recent[-1])
        left = sorted(f for f in os.listdir(folder) if f.endswith('.snap'))
        self.assertNotIn('old.snap', left)                                  # unused too long
        self.assertIn(os.path.basename(recent[-1]), left)                   # the one in use stays
        self.assertEqual(len(left), daemon.SNAPSHOT_MAX)                    # the least recent go
        self.assertNotIn(os.path.basename(recent[-2]), left)
        self.assertFalse(os.path.exists(old + '.json'))

    def test_digitone_emulator_steps_need_no_claim(self):
        proc = type('P', (), {'poll': lambda self: None, 'pid': 98})()
        ran = []
        saved = daemon.DIGIHEALTH, daemon.subprocess.run
        daemon.DIGIHEALTH = TMP
        daemon.subprocess.run = lambda cmd, **kw: (ran.append(cmd), subprocess.CompletedProcess(cmd, 0, '  hi\n', ''))[1]
        try:
            with self.assertRaisesRegex(daemon.Refused, 'no digitone emulator'):
                self.d.t_steps(self.s, {'unit': 'digitone', 'target': 'emulator', 'steps': 'say hi'})
            self.d.emulators[(self.s.sid, 'digitone')] = dict(proc=proc, session=self.s.sid, build='b',
                                                              address='tcp:127.0.0.1:7401')
            out =self.d.t_steps(self.s, {'unit': 'digitone', 'target': 'emulator', 'steps': 'say hi'})
        finally:
            daemon.DIGIHEALTH, daemon.subprocess.run = saved
        self.assertEqual(out[0], 'ok: 1 step(s) done\n  hi')
        self.assertEqual(ran[0][2:5], ['--port', 'tcp:127.0.0.1:7401', 'do'])
        self.assertNotIn(daemon.ELKLOCK, ran[0])

    def test_digitone_emulator_audio(self):
        import socket, struct as st, threading
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(1)

        def serve():
            c, _ = srv.accept()
            frame = st.pack('>12i', *([1 << 30, -(1 << 29)] + [0] * 10))
            try:
                c.sendall(frame * 48000)
            finally:
                c.close()
        threading.Thread(target=serve, daemon=True).start()
        proc = type('P', (), {'poll': lambda self: None, 'pid': 98})()
        with self.assertRaisesRegex(daemon.Refused, 'no digitone emulator'):
            self.d.t_audio_record(self.s, {'unit': 'digitone', 'target': 'emulator', 'seconds': 1})
        self.d.emulators[(self.s.sid, 'digitone')] = dict(proc=proc, session=self.s.sid, build='b',
                                                          address='tcp:127.0.0.1:7401', ob_audio=None)
        with self.assertRaisesRegex(daemon.Refused, 'gives no audio'):
            self.d.t_audio_record(self.s, {'unit': 'digitone', 'target': 'emulator', 'seconds': 1})
        self.d.emulators[(self.s.sid, 'digitone')]['ob_audio'] = srv.getsockname()[1]
        out = self.d.t_audio_record(self.s, {'unit': 'digitone', 'target': 'emulator', 'seconds': 0.5,
                                             'name': 'emu'})
        srv.close()
        self.assertTrue(out[0].startswith('ok: ') and out[0].endswith('(0.50 s, 12 channels)'), out)
        self.assertEqual(out[1], 'peaks: Main L 0.500, Main R 0.250')
        wav = out[0][4:].split(' (')[0]
        with open(wav, 'rb') as fh:
            head = fh.read(44)
        self.assertEqual(st.unpack('<HHI', head[20:28]), (3, 12, 48000))
        self.assertEqual(st.unpack('<I', head[40:44])[0], 24000 * 48)

    @needs_runner
    def test_digitone_plans(self):
        plan = os.path.join(TMP, 'dn-plan.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('unit = digitone\n[home]\nexpect-screen home 2\n')
        saved = daemon.DIGIHEALTH
        try:
            daemon.DIGIHEALTH = None
            with self.assertRaisesRegex(daemon.Refused, 'tools.digihealth'):
                self.d.plan_info(plan)
            daemon.DIGIHEALTH = TMP
            self.assertEqual(self.d.plan_info(plan)['unit'], 'digitone')
            with open(plan, 'a', encoding='utf-8') as fh:
                fh.write('[save]\nsave-kit SAVE\n')
            with self.assertRaisesRegex(daemon.Refused, 'cannot save-kit'):
                self.d.plan_info(plan)
        finally:
            daemon.DIGIHEALTH = saved

    @needs_runner
    def test_data_writes_go_through_the_runner(self):
        proc = type('P', (), {'poll': lambda self: None, 'pid': 98})()
        ran = []
        saved = daemon.DIGIHEALTH, daemon.subprocess.run
        daemon.DIGIHEALTH = TMP
        self.d.emulators[(self.s.sid, 'digitone')] = dict(proc=proc, session=self.s.sid, build='b',
                                                          address='tcp:127.0.0.1:7401')
        results = iter([subprocess.CompletedProcess([], 2, '\nrefused (write policy): slot 5 is not a test slot\n', ''),
                        subprocess.CompletedProcess([], 0, '  100.0 %\nwrote 232 bytes to /soundbanks/H/128, named '
                                                           "'~T SND'\n", '')])
        daemon.subprocess.run = lambda cmd, **kw: (ran.append(cmd), next(results))[1]
        obj = os.path.join(self.s.folder('digitone'), 'snd.bin')
        with open(obj, 'wb') as fh:
            fh.write(b'x')
        try:
            with self.assertRaisesRegex(daemon.Refused, 'to is for roundtrip'):
                self.d.t_data(self.s, {'unit': 'digitone', 'target': 'emulator', 'op': 'put', 'path': '/x', 'to': '/y'})
            out = self.d.t_data(self.s, {'unit': 'digitone', 'target': 'emulator', 'op': 'put',
                                         'path': '/projects/5', 'file': 'snd.bin', 'slot_name': '~T X'})
            self.assertEqual(out[0], 'refused: refused (write policy): slot 5 is not a test slot')
            out = self.d.t_data(self.s, {'unit': 'digitone', 'target': 'emulator', 'op': 'put',
                                         'path': '/soundbanks/H/128', 'file': 'snd.bin', 'slot_name': '~T SND'})
        finally:
            daemon.DIGIHEALTH, daemon.subprocess.run = saved
        self.assertEqual(out[0], "ok: wrote 232 bytes to /soundbanks/H/128, named '~T SND'")
        self.assertEqual(ran[1][1], daemon.RUNNER)               # analogrun applies the write policy
        self.assertEqual(ran[1][2:], ['--port', 'tcp:127.0.0.1:7401', 'data', 'put', '/soundbanks/H/128',
                                      obj, '--name', '~T SND', '--yes'])

    def test_data_store_listing(self):
        proc = type('P', (), {'poll': lambda self: None, 'pid': 98})()
        ran = []
        saved = daemon.DIGIHEALTH, daemon.subprocess.run
        daemon.DIGIHEALTH = TMP
        daemon.subprocess.run = lambda cmd, **kw: (ran.append(cmd), subprocess.CompletedProcess(
            cmd, 0, '/projects\n/sounds\n', ''))[1]
        try:
            with self.assertRaisesRegex(daemon.Refused, 'op is one of'):
                self.d.t_data(self.s, {'unit': 'digitone', 'target': 'emulator', 'op': 'move', 'path': '/x'})
            with self.assertRaisesRegex(daemon.Refused, 'claim digitone first'):
                self.d.t_data(self.s, {'unit': 'digitone', 'target': 'hardware', 'op': 'ls', 'path': '/'})
            self.d.emulators[(self.s.sid, 'digitone')] = dict(proc=proc, session=self.s.sid, build='b',
                                                              address='tcp:127.0.0.1:7401')
            out = self.d.t_data(self.s, {'unit': 'digitone', 'target': 'emulator', 'op': 'ls', 'path': '/'})
        finally:
            daemon.DIGIHEALTH, daemon.subprocess.run = saved
        self.assertEqual(out[:3], ['ok: 2 line(s) for /:', '  /projects', '  /sounds'])
        self.assertEqual(ran[0][2:7], ['--port', 'tcp:127.0.0.1:7401', 'data', 'ls', '/'])
        self.assertTrue(os.path.isfile(out[-1].split(': ', 1)[1]))

    def test_digitakt_reuses_the_digitone_path(self):
        img = self.saved['stock'][0]
        with self.assertRaisesRegex(daemon.Refused, r'unknown unit .*digitakt'):
            self.d.unit('digitakt2')
        self.assertIn('digitakt', daemon.emulated_units())
        self.assertEqual(daemon.chunk_args('digitakt'), ['--chunk-size', '4096'])
        self.assertEqual(daemon.chunk_args('digitone'), [])
        saved = daemon.DIGIEMU
        daemon.DIGIEMU = TMP
        try:
            with self.assertRaisesRegex(daemon.Refused, 'build is the OS .syx to boot'):
                self.d.t_emulator(self.s, {'action': 'start', 'unit': 'digitakt'})
        finally:
            daemon.DIGIEMU = saved
        saved = daemon.DIGIHEALTH
        daemon.DIGIHEALTH = TMP
        try:
            with self.assertRaisesRegex(daemon.Refused, 'no digitakt emulator'):
                self.d.t_steps(self.s, {'unit': 'digitakt', 'target': 'emulator', 'steps': 'say hi'})
        finally:
            daemon.DIGIHEALTH = saved
        with self.assertRaisesRegex(daemon.Refused, "step 'turn' is not allowed"):
            self.d.t_steps(self.s, {'unit': 'digitakt', 'target': 'hardware', 'steps': 'turn A 3'})
        with self.assertRaisesRegex(daemon.Refused, "step 'save-kit' is not allowed"):
            self.d.t_steps(self.s, {'unit': 'digitakt', 'target': 'hardware', 'steps': 'save-kit'})
        # without a running emulator or a build the flash gate is digiemu's emu.fwcheck
        with self.assertRaisesRegex(daemon.Refused, 'emulator check \\(digiemu\\)'):
            self.d.t_flash(self.s, {'unit': 'digitakt', 'image': img, 'target': 'emulator'})
        with self.assertRaisesRegex(daemon.Refused, 'no ASIO driver known for digitakt'):
            self.d.t_audio_record(self.s, {'unit': 'digitakt', 'seconds': 1})
        with self.assertRaisesRegex(daemon.Refused, 'no emulator audio for digitakt'):
            self.d.t_audio_record(self.s, {'unit': 'digitakt', 'seconds': 1, 'target': 'emulator'})
        with self.assertRaisesRegex(daemon.Refused, 'no Total Recall for digitakt' if daemon.WINDOWS
                                    else 'Overbridge Engine'):
            self.d.t_recall_take(self.s, {'unit': 'digitakt'})

    def test_digitakt_flashes_in_small_chunks(self):
        img = self.saved['stock'][0]
        ran = []
        saved = daemon.DIGIEMU, daemon.ELKFLASH, self.d.emu_ready, self.d.start
        daemon.DIGIEMU, daemon.ELKFLASH = TMP, TMP
        self.d.emu_ready = lambda s, unit, build: ('tcp:127.0.0.1:7402', [])
        self.d.start = lambda job, cmd, cwd, *a, **k: ran.append(cmd) or ['ok: started']
        self.d.emulators[(self.s.sid, 'digitakt')] = dict(proc=None, session=self.s.sid, build='b',
                                                           address='tcp:127.0.0.1:7402', firmware=None)
        try:
            self.d.t_flash(self.s, {'unit': 'digitakt', 'image': img, 'target': 'emulator', 'build': img})
        finally:
            daemon.DIGIEMU, daemon.ELKFLASH, self.d.emu_ready, self.d.start = saved
            self.d.emulators.pop((self.s.sid, 'digitakt'), None)
        flat = [x for c in ran for x in (c if isinstance(c, list) else [])]
        self.assertIn('--chunk-size', flat)
        self.assertEqual(flat[flat.index('--chunk-size') + 1], '4096')

    @needs_runner
    def test_digitakt_plans_do_not_flash(self):
        img = self.saved['stock'][0]
        plan = os.path.join(TMP, 'dt-flash-plan.txt')
        with open(plan, 'w') as fh:
            fh.write('unit = digitakt\nflash = %s\n\n[one]\nsay hi\n' % img)
        saved = daemon.DIGIHEALTH
        daemon.DIGIHEALTH = TMP
        try:
            with self.assertRaisesRegex(daemon.Refused, 'a digitakt plan cannot flash'):
                self.d.plan_info(plan)
        finally:
            daemon.DIGIHEALTH = saved

    def test_digirun_capture_and_ping_need_a_digirun_with_them(self):
        old, new = os.path.join(TMP, 'dh-old'), os.path.join(TMP, 'dh-new')
        for root, text in ((old, '    send FILE  ...\n'), (new, '    capture FILE S [any|none]  ...\n    ping   ...\n')):
            os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
            with open(os.path.join(root, 'tools', 'digirun.py'), 'w') as fh:
                fh.write('"""digirun\n%s"""\n' % text)
        saved = daemon.DIGIHEALTH
        try:
            daemon.DIGIHEALTH = old
            self.assertTrue({'capture', 'ping', 'save-kit'} <= daemon.no_steps('digitone'))
            with self.assertRaisesRegex(daemon.Refused, "step 'ping' is not allowed"):
                self.d.t_steps(self.s, {'unit': 'digitone', 'target': 'hardware', 'steps': 'ping'})
            daemon.DIGIHEALTH = new
            self.assertEqual(daemon.no_steps('digitone') & {'capture', 'ping'}, set())
            self.assertIn('save-kit', daemon.no_steps('digitone'))
            with self.assertRaisesRegex(daemon.Refused, 'claim digitone first'):
                self.d.t_steps(self.s, {'unit': 'digitone', 'target': 'hardware', 'steps': 'ping; capture got.syx 1 none'})
        finally:
            daemon.DIGIHEALTH = saved

    def test_digiemu_setup_snapshots_follow_its_usbrun(self):
        root = os.path.join(TMP, 'digiemu-snaps')
        os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
        saved = daemon.DIGIEMU
        daemon.DIGIEMU = root
        try:
            with open(os.path.join(root, 'tools', 'usbrun.py'), 'w') as fh:
                fh.write("ap.add_argument('--save-on-exit')\n")
            self.assertFalse(self.d.emu_has_snapshots('digitone'))
            with open(os.path.join(root, 'tools', 'usbrun.py'), 'w') as fh:
                fh.write("ap.add_argument('--stdin-commands', action='store_true')\n")
            self.assertTrue(self.d.emu_has_snapshots('digitakt'))
            info = dict(unit='digitone', setup=1, emulator=False, flash=None, refs=TMP,
                        steps=[('setup', ['say set up']), ('check', ['say check'])])
            snap = self.d.setup_snapshot(self.s, info, {'build': self.saved['stock'][0]})
            self.assertTrue(snap['digi'])
            self.assertFalse(snap['have'])               # known once digiemu has started
            self.assertEqual(snap['live'], snap['key'])  # digiemu stores it by key, in its own home
            self.assertTrue(snap['store'].endswith(snap['key'] + '.digi'))
            # the key changes with the setup steps and with digiemu's checkout
            other = dict(info, steps=[('setup', ['say other']), ('check', ['say check'])])
            self.assertNotEqual(snap['key'], self.d.snapshot_key(other, self.saved['stock'][0]))
            with open(os.path.join(root, '.git'), 'w') as fh:
                fh.write('gitdir: .gitdir\n')
            os.makedirs(os.path.join(root, '.gitdir'), exist_ok=True)
            with open(os.path.join(root, '.gitdir', 'HEAD'), 'w') as fh:
                fh.write('a' * 40 + '\n')
            self.assertNotEqual(snap['key'], self.d.snapshot_key(info, self.saved['stock'][0]))
            # a plan that flashes, or records references, runs from the settled boot
            self.assertIsNone(self.d.setup_snapshot(self.s, dict(info, flash='x.syx'),
                                                    {'build': self.saved['stock'][0]}))
            self.assertIsNone(self.d.setup_snapshot(self.s, info, {'build': self.saved['stock'][0],
                                                                   'record_refs': True}))
        finally:
            daemon.DIGIEMU = saved

    def test_git_head_of_a_checkout_and_a_worktree(self):
        repo = os.path.join(TMP, 'gh-repo')
        os.makedirs(os.path.join(repo, '.git', 'refs', 'heads'), exist_ok=True)
        with open(os.path.join(repo, '.git', 'HEAD'), 'w') as fh:
            fh.write('ref: refs/heads/main\n')
        with open(os.path.join(repo, '.git', 'refs', 'heads', 'main'), 'w') as fh:
            fh.write('1' * 40 + '\n')
        self.assertEqual(daemon.git_head(repo), '1' * 40)
        wt = os.path.join(TMP, 'gh-wt')
        os.makedirs(os.path.join(repo, '.git', 'worktrees', 'wt'), exist_ok=True)
        os.makedirs(wt, exist_ok=True)
        with open(os.path.join(wt, '.git'), 'w') as fh:
            fh.write('gitdir: %s\n' % os.path.join(repo, '.git', 'worktrees', 'wt'))
        with open(os.path.join(repo, '.git', 'worktrees', 'wt', 'HEAD'), 'w') as fh:
            fh.write('ref: refs/heads/side\n')
        with open(os.path.join(repo, '.git', 'worktrees', 'wt', 'commondir'), 'w') as fh:
            fh.write('../..\n')
        with open(os.path.join(repo, '.git', 'packed-refs'), 'w') as fh:
            fh.write('# pack-refs\n%s refs/heads/side\n' % ('2' * 40))
        self.assertEqual(daemon.git_head(wt), '2' * 40)
        self.assertIsNone(daemon.git_head(os.path.join(TMP, 'no-such-repo')))

    def test_save_snapshot_now_waits_for_the_emulator_log(self):
        log = os.path.join(TMP, 'save-now.log')

        class Proc:
            def __init__(self, line):
                self.line, self.stdin = line, self

            def write(self, data):
                assert data == b'save\n'
                with open(log, 'a') as fh:
                    fh.write(self.line + '\n')

            def flush(self):
                pass

            def poll(self):
                return None
        open(log, 'w').close()
        e = dict(proc=Proc('[elkbench] setup snapshot K1 stored'), log=log)
        self.assertTrue(daemon.save_snapshot_now(e, dict(digi=True, key='K1', live='K1')))
        open(log, 'w').close()
        e = dict(proc=Proc('[usbrun] save failed: disk full'), log=log)
        self.assertFalse(daemon.save_snapshot_now(e, dict(digi=True, key='K1', live='K1')))
        open(log, 'w').close()
        self.assertTrue(daemon.save_snapshot_now(Proc('saved /w/setup.snap'),
                                                 dict(live='/w/setup.snap', log=log)))

    def test_digiemu_usb_stores_and_prunes_setup_snapshots(self):
        import digiemu_usb
        store = os.path.join(TMP, 'dusb-store')
        work = os.path.join(TMP, 'dusb-work')
        os.makedirs(work, exist_ok=True)
        saved = os.path.join(work, 'setup.snap')
        for data in (b'machine', b'card'):
            with open(saved + ('' if data == b'machine' else '.card'), 'wb') as fh:
                fh.write(data)
        seen = []
        out = digiemu_usb.Watch(types.SimpleNamespace(write=lambda t: len(t), flush=lambda: None),
                                '[usbrun] saved %s' % saved, lambda: seen.append(1))
        out.write('[usbrun] 1.0 s emulated\n[usbrun] sav')
        self.assertEqual(seen, [])
        out.write('ed %s\n' % saved)
        self.assertEqual(seen, [1])
        digiemu_usb.publish(saved, store, 'K1', 't1')
        with open(os.path.join(store, 'K1', 'setup.snap.card'), 'rb') as fh:
            self.assertEqual(fh.read(), b'card')
        # a second session saving the same setup keeps the one stored first
        for name, data in (('setup.snap', b'other'), ('setup.snap.card', b'other card')):
            with open(os.path.join(work, name), 'wb') as fh:
                fh.write(data)
        digiemu_usb.publish(saved, store, 'K1', 't2')
        with open(os.path.join(store, 'K1', 'setup.snap'), 'rb') as fh:
            self.assertEqual(fh.read(), b'machine')
        self.assertEqual(sorted(os.listdir(store)), ['K1'])
        for n, key in enumerate(('K2', 'K3', 'K4')):
            os.makedirs(os.path.join(store, key))
            os.utime(os.path.join(store, key), (time.time() - 100 + n, time.time() - 100 + n))
        os.utime(os.path.join(store, 'K1'), (time.time() - 9 * 86400,) * 2)
        digiemu_usb.prune(store, 'K4', keep=2, days=7)   # K4 in use; K1 too old; K3 the newest other
        self.assertEqual(sorted(os.listdir(store)), ['K3', 'K4'])

    def test_digiemu_paths_in_wsl(self):
        saved = daemon.DIGIEMU, daemon.DIGIEMU_PY
        daemon.DIGIEMU, daemon.DIGIEMU_PY = TMP, ['wsl', '~/py']
        try:
            cmd = self.d.digiemu_cmd([daemon.Path(r'D:\x\y.syx'), '7400'])
        finally:
            daemon.DIGIEMU, daemon.DIGIEMU_PY = saved
        self.assertEqual(cmd[:4], ['wsl', '-e', 'bash', '-lc'])
        self.assertTrue(cmd[4].endswith('~/py /mnt/d/x/y.syx 7400'), cmd[4])

    def test_release_empties_test_slots(self, unit='rytm'):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', unit, '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        saved, calls = daemon.UNITS[unit].get('clear_at_release'), []
        daemon.UNITS[unit]['clear_at_release'] = [121, 122]
        try:
            self.d.empty_test_slots = lambda s, u: (calls.append(u), (False, ['slot 122 still holds a project']))[1]
            with self.assertRaisesRegex(daemon.Refused, 'test slots are not empty, so the claim is kept'):
                self.d.t_release(self.s, {'unit': unit, 'note': 'menus closed'})
            self.assertTrue(daemon.elklock.mine(daemon.elklock.holder(unit), session=self.s.sid))
            out = self.d.t_release(self.s, {'unit': unit, 'note': 'cable out', 'accept_slots_left': True})
            self.assertTrue(out[0].startswith('ok: released'), out)
            handoff = json.loads(read(os.path.join(os.environ['ELKLOCK_DIR'], unit + '.handoff.json')))
            self.assertIn('test slots 121-122 NOT EMPTY: slot 122 still holds a project', handoff['note'])
            subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', unit, '--for', 'elkbench test'],
                           env=self.s.env(), capture_output=True)
            self.d.empty_test_slots = lambda s, u: (True, ['slot 121 backed up (x) and cleared',
                                                           'test slots 121-122 empty (data ls)'])
            out = self.d.t_release(self.s, {'unit': unit, 'note': 'menus closed'})
            self.assertIn('%s: slot 121 backed up (x) and cleared' % unit, out)
            handoff = json.loads(read(os.path.join(os.environ['ELKLOCK_DIR'], unit + '.handoff.json')))
            self.assertIn('test slots 121-122 empty (data ls)', handoff['note'])
        finally:
            del self.d.empty_test_slots
            daemon.UNITS[unit]['clear_at_release'] = saved or []
        self.assertEqual(calls, [unit, unit])

    def test_release_empties_digitone_test_slots(self):
        """On the Digitone, release lists with digirun and erases with analogrun's data clear
        (which asks the write policy), as the data tool does there."""
        ran = []
        saved = daemon.DIGIHEALTH, daemon.subprocess.run, daemon.UNITS['digitone'].get('clear_at_release')
        daemon.DIGIHEALTH = TMP
        daemon.UNITS['digitone']['clear_at_release'] = list(range(121, 129))
        self.d.check_connected = lambda unit, target: None
        # the Digitone lists only full slots: an empty one has no row at all
        full = ('PRESETS                  id    1   4194304 B  ops 0x0012  +metadata\n'
                '~T SETUP                 id  121   4194304 B  ops 0x007e  +metadata\n'
                '~T SETUP                 id  123   4194304 B  ops 0x007e  +metadata\n')
        empty = 'PRESETS                  id    1   4194304 B  ops 0x0012  +metadata\n'
        results = iter([subprocess.CompletedProcess([], 0, full, ''),
                        subprocess.CompletedProcess([], 0, 'cleared /projects/121\n', ''),
                        subprocess.CompletedProcess([], 0, 'cleared /projects/123\n', ''),
                        subprocess.CompletedProcess([], 0, empty, '')])
        daemon.subprocess.run = lambda cmd, **kw: (ran.append(cmd), next(results))[1]
        try:
            self.assertTrue(daemon.clears_at_release('digitone'))
            ok, lines = self.d.empty_test_slots(self.s, 'digitone')
        finally:
            daemon.DIGIHEALTH, daemon.subprocess.run = saved[:2]
            daemon.UNITS['digitone']['clear_at_release'] = saved[2] or []
            del self.d.check_connected
        self.assertTrue(ok, lines)
        self.assertEqual(lines[-1], 'test slots 121-128 empty (data ls)')
        digirun = os.path.join(TMP, 'tools', 'digirun.py')
        self.assertEqual([c[6] for c in ran], [digirun, daemon.RUNNER, daemon.RUNNER, digirun])
        self.assertEqual([c[10:12] for c in ran], [['ls', '/projects'], ['clear', '/projects/121'],
                                                 ['clear', '/projects/123'], ['ls', '/projects']])
        self.assertEqual(ran[1][-1], '--yes')
        self.assertFalse(daemon.clears_at_release('digitone'))   # unset again

    def test_clear_at_release_setting(self):
        self.assertEqual(daemon.slot_numbers('121-124, 127'), [121, 122, 123, 124, 127])
        self.assertEqual(daemon.slot_numbers([128, 121]), [121, 128])
        self.assertEqual(daemon.slot_numbers(None), [])
        with self.assertRaises(SystemExit):
            daemon.slot_numbers('121-x')
        self.assertEqual(daemon.slot_text([121, 122, 123, 124, 125, 126, 127, 128, 130]), '121-128, 130')
        ls = ('~T SETUP              id 121  54549 B\n'
              '                      id 122  0 B (no valid data)\n'
              'MY SONG               id 3  51200 B\n')
        self.assertEqual(daemon.occupied_slots(ls), {121, 3})

    def test_release_asks_for_stock(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        self.s.os_left['rytm'] = ('test-build.syx', False)
        self.s.done['rytm'] = ['plan menus PASS (x)']
        with self.assertRaisesRegex(daemon.Refused, 'not stock'):
            self.d.t_release(self.s, {'unit': 'rytm', 'note': 'menus closed'})
        out = self.d.t_release(self.s, {'unit': 'rytm', 'note': 'left on the test build on purpose',
                                        'accept_non_stock': True})
        self.assertTrue(out[0].startswith('ok: released'), out)
        log = read(os.path.join(self.folder, 'runs', 'test-log.md'))
        self.assertIn('test-build.syx (NOT STOCK)', log)
        handoff = json.loads(read(os.path.join(os.environ['ELKLOCK_DIR'], 'rytm.handoff.json')))
        self.assertIn('runs: plan menus PASS', handoff['note'])

    @needs_runner
    def test_steps_rules(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        with self.assertRaisesRegex(daemon.Refused, 'bare file name'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'shot ../x.png'})
        bad = os.path.join(TMP, 'os.syx')
        with open(bad, 'wb') as fh:
            fh.write(bytes([0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00, 1, 2, 0xF7]))
        with self.assertRaisesRegex(daemon.Refused, 'other than Elektron dumps'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'send ' + bad.replace('\\', '/')})
        with self.assertRaisesRegex(daemon.Refused, 'belong in a plan'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'wait 100; wait 30'})
        with self.assertRaisesRegex(daemon.Refused, 'bare file name'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'capture ../r.syx 5'})
        with self.assertRaisesRegex(daemon.Refused, 'belong in a plan'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'wait 100; capture r.syx 30'})

    @needs_runner
    def test_plan_send_files(self):
        with open(os.path.join(PLANS, 'menus.txt'), encoding='utf-8') as fh:
            text = fh.read()
        plan = os.path.join(PLANS, 'sends.txt')
        dump, rpc = os.path.join(TMP, 'kit.syx'), os.path.join(TMP, 'rpc.syx')
        with open(dump, 'wb') as fh:
            fh.write(bytes([0xF0, 0x00, 0x20, 0x3C, 0x07, 0x00, 0x62, 1, 1, 0, 0, 0, 0, 5, 0xF7]))
        with open(rpc, 'wb') as fh:
            fh.write(bytes([0xF0, 0x00, 0x20, 0x3C, 0x10, 0x00, 1, 2, 0xF7]))

        def info(steps):
            with open(plan, 'w', encoding='utf-8') as fh:
                fh.write(text + '\n[sysex]\n' + steps + '\n')
            return self.d.plan_info(plan)
        plain = info('say hi')['key']
        with self.assertRaisesRegex(daemon.Refused, 'absolute path, or a file an earlier capture'):
            info('send kit.syx')
        info('capture back.syx 1\nsend back.syx')                  # what this run captured
        with self.assertRaisesRegex(daemon.Refused, 'other than Elektron dumps'):
            info('send "%s"' % rpc.replace('\\', '/'))
        sc = os.path.join(TMP, 'sc.syx')              # sidecar-check's own command (0.8): accepted
        with open(sc, 'wb') as fh:
            fh.write(bytes([0xF0, 0x7D, 0x53, 0x43, 1, 7, 1, 0, 0, 0, 0, 0x4D, 0xF7]))
        info('send "%s"' % sc.replace('\\', '/'))
        sent = info('send "%s"' % dump.replace('\\', '/'))['key']
        with open(dump, 'ab') as fh:
            fh.write(bytes([0xF0, 0x00, 0x20, 0x3C, 0x07, 0x00, 0x62, 1, 1, 1, 0, 0, 0, 5, 0xF7]))
        self.assertNotEqual(info('send "%s"' % dump.replace('\\', '/'))['key'], sent)
        self.assertNotEqual(plain, sent)

    def kit_request(self):
        req = os.path.join(TMP, 'kitreq.syx')
        with open(req, 'wb') as fh:
            fh.write(bytes([0xF0, 0x00, 0x20, 0x3C, 0x07, 0x00, 0x62, 1, 1, 0, 0, 0, 0, 5, 0xF7]))
        return req.replace('\\', '/')

    def capture_runs(self, rc=1):
        """Runs in daemon record the commands (rc 1: device_do reads no log)."""
        import types
        ran = []
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=lambda cmd, **kw: (
            ran.append(cmd), subprocess.CompletedProcess(cmd, rc, 'x', ''))[1]))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)
        return ran

    def test_din_steps(self):
        self.assertEqual(daemon.din_steps(['send a.syx din', 'capture k.syx 6 any din', 'capture k.syx 6 din',
                                           'capture din 5', 'send din', 'send a.syx', 'cc 1 2 3']),
                         ['send a.syx din', 'capture k.syx 6 any din', 'capture k.syx 6 din'])
        self.assertEqual(daemon.din_steps(['cc 1 2 3 din', 'nrpn 1 0 112 9856 din', 'note 1 60 100 din',
                                           'bend 1 0 din', 'at 1 70 din', 'at 1 din', 'bend 1 0']),
                         ['cc 1 2 3 din', 'nrpn 1 0 112 9856 din', 'note 1 60 100 din', 'bend 1 0 din',
                          'at 1 70 din'])
        self.assertTrue({'bend', 'at'} <= daemon.STEP_OPS)

    @needs_runner
    def test_din_on_hardware_needs_the_configured_interface(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        saved = daemon.elklock.midi_inputs
        daemon.elklock.midi_inputs = lambda: ['Analog Rytm MKII', 'USB MIDI Interface']
        self.addCleanup(setattr, daemon.elklock, 'midi_inputs', saved)
        steps = 'send %s din; capture k.syx 1 any din' % self.kit_request()
        a = {'unit': 'rytm', 'target': 'hardware', 'steps': steps}
        with self.assertRaisesRegex(daemon.Refused, r'no interface is set for the rytm: din = .*\[units.rytm\]'):
            self.d.t_steps(self.s, a)
        daemon.UNITS['rytm']['din'] = 'MIDISPORT'
        with self.assertRaisesRegex(daemon.Refused, "DIN interface 'MIDISPORT' is not connected"):
            self.d.t_steps(self.s, a)
        daemon.UNITS['rytm']['din'] = 'USB MIDI Interface'
        ran = self.capture_runs()
        self.d.t_steps(self.s, a)
        cmd = ran[-1]
        self.assertEqual(cmd[cmd.index('--din') + 1], 'USB MIDI Interface')
        self.assertLess(cmd.index('--din'), cmd.index('do'))       # a global option
        self.assertIn(daemon.ELKLOCK, cmd)                           # still under the claim
        self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'send %s' % self.kit_request()})
        self.assertNotIn('--din', ran[-1])                           # only when a step uses it

    def test_unit_port_through_a_midi_host(self):
        # units.<unit>.port (or ELKBENCH_<UNIT>_PORT): a Rytm behind Sidecar has its host's port name
        saved = daemon.elklock.midi_inputs, daemon.UNITS['rytm']['port']
        self.addCleanup(lambda: setattr(daemon.elklock, 'midi_inputs', saved[0]) or
                        daemon.UNITS['rytm'].update(port=saved[1]))
        daemon.elklock.midi_inputs = lambda: ['Sidecar | Expander (Pi)', 'USB MIDI Interface']
        self.assertEqual(daemon.port_args('rytm'), [])                    # built-in name: runner defaults
        with self.assertRaisesRegex(daemon.Refused, "rytm is not on USB \\(no 'Analog Rytm' MIDI port\\)"):
            self.d.check_connected('rytm', 'hardware')
        daemon.UNITS['rytm']['port'] = 'Sidecar | Expander (Pi)'
        self.assertEqual(daemon.port_args('rytm'), ['--port', 'Sidecar | Expander (Pi)'])
        self.d.check_connected('rytm', 'hardware')                         # found under the host's name
        daemon.elklock.midi_inputs = lambda: ['Analog Rytm MKII']
        with self.assertRaisesRegex(daemon.Refused, "no 'Sidecar \\| Expander \\(Pi\\)' MIDI port"):
            self.d.check_connected('rytm', 'hardware')                     # the setting, not the old name
        old = os.environ.get('ELKBENCH_RYTM_PORT')
        os.environ['ELKBENCH_RYTM_PORT'] = 'From Env'
        self.addCleanup(lambda: os.environ.pop('ELKBENCH_RYTM_PORT') if old is None else
                        os.environ.update(ELKBENCH_RYTM_PORT=old))
        cfg = daemon.load_config(os.path.join(TMP, 'no-such-config.toml'))
        self.assertEqual(cfg['units']['rytm']['port'], 'From Env')
        self.assertEqual(cfg['units']['a4']['port'], 'Analog Four')

    @needs_runner
    def test_hardware_runs_use_the_units_port(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        saved = daemon.elklock.midi_inputs, daemon.UNITS['rytm']['port']
        self.addCleanup(lambda: setattr(daemon.elklock, 'midi_inputs', saved[0]) or
                        daemon.UNITS['rytm'].update(port=saved[1]))
        daemon.elklock.midi_inputs = lambda: ['Sidecar | Expander (Pi)']
        daemon.UNITS['rytm']['port'] = 'Sidecar | Expander (Pi)'
        ran = self.capture_runs()
        self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'tap PLAY'})
        cmd = ran[-1]
        self.assertEqual(cmd[cmd.index('--port') + 1], 'Sidecar | Expander (Pi)')
        self.assertIn(daemon.ELKLOCK, cmd)                                 # still under the claim

    @needs_runner
    def test_din_in_the_emulator(self):
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        e = dict(proc=proc, session=self.s.sid, build='x', address='tcp:127.0.0.1:7399')
        self.d.emulators[(self.s.sid, 'rytm')] = e
        a = {'unit': 'rytm', 'target': 'emulator', 'steps': 'send %s din' % self.kit_request()}
        self.d.emu_help = lambda unit: 'usage: rytm [--usb-midi-tcp ADDR]'
        with self.assertRaisesRegex(daemon.Refused, 'has no --din-midi-tcp'):
            self.d.t_steps(self.s, a)
        self.d.emu_help = lambda unit: 'usage: rytm [--usb-midi-tcp ADDR] [--din-midi-tcp ADDR]'
        with self.assertRaisesRegex(daemon.Refused, 'started without its DIN MIDI port'):
            self.d.t_steps(self.s, a)
        e['din'] = 'tcp:127.0.0.1:7398'
        ran = self.capture_runs()
        self.d.t_steps(self.s, a)
        self.assertEqual(ran[-1][ran[-1].index('--port') + 1:ran[-1].index('--port') + 4],
                         ['tcp:127.0.0.1:7399', '--din', 'tcp:127.0.0.1:7398'])
        # a plan: the twin runs with the emulator's DIN port
        plan = os.path.join(TMP, 'din-plan.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nunit = rytm\n[kit over din]\nsend %s din\ncapture k.syx 6 any din\n'
                     % self.kit_request())
        self.assertTrue(self.d.plan_info(plan)['din'])
        self.d.t_run_plan(self.s, {'plan': plan, 'target': 'emulator'})
        job, stages = self.started[-1]
        cmd = next(c for c in stages if isinstance(c, list))
        self.assertEqual(cmd[cmd.index('--din') + 1], 'tcp:127.0.0.1:7398')
        self.d.t_run_plan(self.s, {'plan': os.path.join(PLANS, 'menus.txt'), 'target': 'emulator'})
        self.assertNotIn('--din', self.started[-1][1])
        self.d.emu_help = lambda unit: 'usage: rytm [--usb-midi-tcp ADDR]'
        with self.assertRaisesRegex(daemon.Refused, 'has no --din-midi-tcp'):
            self.d.t_run_plan(self.s, {'plan': plan, 'target': 'emulator'})

    @needs_runner
    def test_din_batches(self):
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP              # configured: the runs are stand-ins
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        plan = os.path.join(TMP, 'din-batch.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nunit = rytm\n[kit over din]\nsend %s din\ncapture k.syx 6 any din\n'
                     % self.kit_request())
        build = os.path.join(TMP, 'os', 'test-build.syx')
        self.d.emu_help = lambda unit: 'usage: rytm [--usb-midi-tcp ADDR]'
        with self.assertRaisesRegex(daemon.Refused, 'has no --din-midi-tcp'):
            self.d.t_batch(self.s, {'plan': plan, 'build': build, 'variants': [{}]})
        asked = []
        self.d.batch_emulator = lambda unit, build, work, minutes, snapshot=None, din=False, **kw: (
            asked.append(din), (None, 'tcp:127.0.0.1:7001', None, 'tcp:127.0.0.1:7002' if din else None))[1]
        ran = self.capture_runs()
        folder = tempfile.mkdtemp(dir=TMP)
        for p, want in ((plan, True), (os.path.join(PLANS, 'menus.txt'), False)):
            self.d.batch_run(self.s, 'rytm', False, 1, folder, len(asked), {'build': build, 'info': self.d.plan_info(p)})
            self.assertEqual((asked[-1], '--din' in ran[-1]), (want, want))
        self.assertEqual(ran[0][ran[0].index('--din') + 1], 'tcp:127.0.0.1:7002')

    def test_din_interface_lock(self):
        unit_a, unit_b = threading.Lock(), threading.Lock()
        unit_a.acquire()
        unit_b.acquire()
        held = self.d.hold_din(['--din', 'USB MIDI Interface'], unit_a)
        with self.assertRaisesRegex(daemon.Refused, "DIN interface 'usb midi interface' is in use"):
            self.d.hold_din(['--din', 'usb midi interface'], unit_b)       # one interface, any unit
        self.assertFalse(unit_b.locked())                                   # the refused run let go
        held.release()
        self.assertFalse(unit_a.locked())
        unit_b.acquire()
        self.d.hold_din(['--din', 'USB MIDI Interface'], unit_b).release()
        emu = threading.Lock()
        self.assertIs(self.d.hold_din(['--din', 'tcp:127.0.0.1:7398'], emu), emu)   # an emulator's own
        self.assertIs(self.d.hold_din([], emu), emu)

    @needs_runner
    def test_twins_blank_the_din_setting(self):
        plan = os.path.join(TMP, 'din-setting.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nunit = rytm\ndin = USB MIDI Interface\n[kit over din]\n'
                     'send %s din\ncapture k.syx 6 any din\n' % self.kit_request())
        info = self.d.plan_info(plan)
        import testplan
        self.assertEqual(testplan.parse(self.d.twin(info, False, self.s.sid))[0]['din'], '')
        work = tempfile.mkdtemp(dir=TMP)
        self.assertEqual(testplan.parse(self.d.batch_plan(info, work, 'batch run 1'))[0]['din'], '')
        self.assertEqual(testplan.parse(plan)[0]['din'], 'USB MIDI Interface')    # the plan itself keeps it

    def test_digitone_din(self):
        old, new, emu = (os.path.join(TMP, n) for n in ('dn-din-old', 'dn-din-new', 'dn-din-emu'))
        for root, text in ((old, "    send FILE\n'''\nap.add_argument('--port')\n"),
                           (new, "    send FILE [din]\n'''\nap.add_argument('--din', default=None)\n")):
            os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
            with open(os.path.join(root, 'tools', 'digirun.py'), 'w') as fh:
                fh.write("'''digirun\n%s" % text)
        os.makedirs(os.path.join(emu, 'tools'), exist_ok=True)
        with open(os.path.join(emu, 'tools', 'usbrun.py'), 'w') as fh:
            fh.write("ap.add_argument('--port')\n")
        steps = 'send %s din; capture d.syx 2 any din' % self.kit_request()
        proc = type('P', (), {'poll': lambda self: None, 'pid': 98})()
        ran = []
        saved = daemon.DIGIHEALTH, daemon.DIGIEMU, daemon.subprocess.run
        daemon.subprocess.run = lambda cmd, **kw: (ran.append(cmd), subprocess.CompletedProcess(cmd, 0, '  ok\n', ''))[1]
        try:
            daemon.DIGIHEALTH, daemon.DIGIEMU = old, emu
            self.d.emulators[(self.s.sid, 'digitone')] = dict(proc=proc, session=self.s.sid, build='b',
                                                              address='tcp:127.0.0.1:7401')
            self.assertIn('need a digirun.py with --din', daemon.din_refusal('digitone'))
            self.assertIsNone(daemon.din_refusal('rytm'))
            self.assertNotIn('din', daemon.no_steps('digitone'))             # an option, not a step
            with self.assertRaisesRegex(daemon.Refused, 'need a digirun.py with --din'):
                self.d.device_do(self.s, 'digitone', 'emulator', steps)
            daemon.DIGIHEALTH = new
            self.assertIsNone(daemon.din_refusal('digitone'))
            self.assertIsNone(daemon.din_refusal('digitakt'))
            self.assertFalse(self.d.emu_has_din('digitone'))
            with self.assertRaisesRegex(daemon.Refused, "digiemu's tools/usbrun.py has no --din-port"):
                self.d.device_do(self.s, 'digitone', 'emulator', steps)
            with open(os.path.join(emu, 'tools', 'usbrun.py'), 'a') as fh:
                fh.write("ap.add_argument('--din-port', type=int)\n")
            self.assertTrue(self.d.emu_has_din('digitone'))
            with self.assertRaisesRegex(daemon.Refused, 'started without its DIN MIDI port'):
                self.d.device_do(self.s, 'digitone', 'emulator', steps)
            self.d.emulators[(self.s.sid, 'digitone')]['din'] = 'tcp:127.0.0.1:7402'
            ok, line, _ = self.d.device_do(self.s, 'digitone', 'emulator', steps)
            self.assertTrue(ok, line)
            self.assertEqual(ran[-1][2:7], ['--port', 'tcp:127.0.0.1:7401', '--din', 'tcp:127.0.0.1:7402', 'do'])
            ok, _, _ = self.d.device_do(self.s, 'digitone', 'emulator', 'send %s' % self.kit_request())
            self.assertNotIn('--din', ran[-1])                                # only when a step uses it
            with self.assertRaisesRegex(daemon.Refused, 'no interface is set for the digitone'):
                self.d.din_args(self.s, 'digitone', 'hardware', ['send x.syx din'])
        finally:
            daemon.DIGIHEALTH, daemon.DIGIEMU, daemon.subprocess.run = saved
            self.d.emulators.pop((self.s.sid, 'digitone'), None)

    @needs_runner
    def test_digitone_din_plans_and_batches(self):
        old, new = os.path.join(TMP, 'dn-din-old'), os.path.join(TMP, 'dn-din-new')
        for root, text in ((old, "ap.add_argument('--port')\n"), (new, "ap.add_argument('--din', default=None)\n")):
            os.makedirs(os.path.join(root, 'tools'), exist_ok=True)
            with open(os.path.join(root, 'tools', 'digirun.py'), 'w') as fh:
                fh.write("'''digirun\n    send FILE\n    capture FILE S\n'''\n" + text)
        plan = os.path.join(TMP, 'dn-din-plan.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nunit = digitone\n[dumps over din]\nsend %s din\ncapture d.syx 2 any din\n'
                     % self.kit_request())
        saved, launch = daemon.DIGIHEALTH, self.d.digiemu_launch
        try:
            daemon.DIGIHEALTH = old
            with self.assertRaisesRegex(daemon.Refused, 'need a digirun.py with --din'):
                self.d.plan_info(plan)
            daemon.DIGIHEALTH = new
            self.assertTrue(self.d.plan_info(plan)['din'])
            ended = []
            self.d.end_emulator = ended.append
            self.d.digiemu_launch = lambda *a, **k: (dict(address='tcp:127.0.0.1:7401'), '')
            proc, addr, why, din = self.d.batch_emulator('digitone', 'b.syx', TMP, 1, din=True)
            self.assertIsNone(proc)
            self.assertIn('no --din-port', why)                              # this digiemu serves no DIN
            self.assertEqual(len(ended), 1)
            self.d.digiemu_launch = lambda *a, **k: (dict(address='tcp:127.0.0.1:7401', din='tcp:127.0.0.1:7402'), '')
            self.assertEqual(self.d.batch_emulator('digitone', 'b.syx', TMP, 1, din=True)[1:],
                             ('tcp:127.0.0.1:7401', None, 'tcp:127.0.0.1:7402'))
            self.assertIsNone(self.d.batch_emulator('digitone', 'b.syx', TMP, 1)[3])
        finally:
            daemon.DIGIHEALTH = saved
            self.d.digiemu_launch = launch
            self.d.__dict__.pop('end_emulator', None)

    def test_digiemu_din_port_on_a_resumed_setup(self):
        emu = os.path.join(TMP, 'dn-din-emu-resume')
        os.makedirs(os.path.join(emu, 'tools'), exist_ok=True)
        with open(os.path.join(emu, 'tools', 'usbrun.py'), 'w') as fh:
            fh.write("ap.add_argument('--din-port', type=int)\n")
        build = os.path.join(TMP, 'dn-resume.syx')
        with open(build, 'wb') as fh:
            fh.write(b'\xf0\xf7')
        work = tempfile.mkdtemp(dir=TMP)
        cmds = []
        proc = type('P', (), {'poll': lambda self: 1, 'pid': 98})()
        saved = daemon.DIGIEMU, daemon.subprocess.Popen
        daemon.DIGIEMU = emu
        daemon.subprocess.Popen = lambda cmd, **kw: (cmds.append(cmd), proc)[1]
        self.d.stop_emulator = lambda e: None
        try:
            with self.assertRaisesRegex(daemon.Refused, 'did not start'):    # the stub never comes up
                self.d.digiemu_launch(build, 1, 'resume-test', work, os.path.join(work, 'firmware'),
                                      started=lambda e: cmds.append(e), setup_key='k1')
        finally:
            daemon.DIGIEMU, daemon.subprocess.Popen = saved
            self.d.__dict__.pop('stop_emulator', None)
        cmd, e = [str(x) for x in cmds[0]], cmds[1]
        self.assertIn('--din-port', cmd)
        self.assertIn('--setup-key', cmd)                                   # resuming a setup snapshot
        self.assertEqual(e['din'], 'tcp:127.0.0.1:%s' % cmd[cmd.index('--din-port') + 1])

    def test_no_stats_build(self):
        build = daemon.sha256(os.path.join(TMP, 'os', 'remote-only.syx'))
        stock = daemon.sha256(os.path.join(TMP, 'os', 'stock.syx'))
        self.s.os_left['rytm'] = ('remote-only.syx', False, build)
        with self.assertRaisesRegex(daemon.Refused, 'no stats command'):
            self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'hardware', 'steps': 'tap PLAY; stats'})
        self.d.emulators[(self.s.sid, 'rytm')] = {'sha': build}
        try:
            with self.assertRaisesRegex(daemon.Refused, 'no stats command'):
                self.d.t_steps(self.s, {'unit': 'rytm', 'target': 'emulator', 'steps': 'stats'})
        finally:
            del self.d.emulators[(self.s.sid, 'rytm')]
        self.d.check_stats('rytm', build, {'tap', 'shot'}, 'x')         # no stats step: fine
        self.d.check_stats('rytm', stock, {'stats'}, 'x')               # not listed: fine
        self.d.check_stats('rytm', None, {'stats'}, 'x')                # OS unknown: fine

    def test_hardware_flash_needs_emulator_flash(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        img = self.saved['stock'][0]
        build = os.path.join(TMP, 'os', 'test-build.syx')
        with self.assertRaisesRegex(daemon.Refused, 'give over'):
            self.d.t_flash(self.s, {'unit': 'rytm', 'image': img, 'target': 'hardware'})
        with self.assertRaisesRegex(daemon.Refused, 'emulator first'):
            self.d.t_flash(self.s, {'unit': 'rytm', 'image': img, 'target': 'hardware', 'over': build})
        with self.assertRaisesRegex(daemon.Refused, 'emulator first'):
            self.d.t_approve_plan(self.s, {'flashes': [{'unit': 'rytm', 'image': img, 'over': build}],
                                           'purpose': 't'})

    def test_a_flash_pass_is_of_one_transition(self):
        os.makedirs(daemon.STATE, exist_ok=True)
        unit, img = 'rytm', self.saved['stock'][0]
        a, b = os.path.join(TMP, 'os', 'test-build.syx'), os.path.join(TMP, 'os', 'remote-only.syx')
        d, sa, sb = daemon.sha256(img), daemon.sha256(a), daemon.sha256(b)
        # an emulator flash records the OS the emulator ran before it
        self.d.emu_ready = lambda s, unit, build, snap=None: ('tcp:127.0.0.1:7399', [])
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        self.d.emulators[(self.s.sid, unit)] = dict(proc=proc, build='test-build.syx', sha=sa,
                                                    address='tcp:127.0.0.1:7399')
        self.addCleanup(self.d.emulators.pop, (self.s.sid, unit), None)
        self.d.t_flash(self.s, {'unit': unit, 'image': img, 'target': 'emulator'})
        self.done(self.started[-1][0], 0, 'ok\n')
        self.assertTrue(self.d.flash_pass(unit, d, sa))
        self.assertFalse(self.d.flash_pass(unit, d, sb))           # stock over another build: not gated
        self.assertEqual(self.d.emulators[(self.s.sid, unit)]['sha'], d)
        # on hardware the OS flashed over is `over`, else what this session flashed last
        self.s.os_left[unit] = ('remote-only.syx', False, sb)
        self.assertEqual(self.d.os_on_unit(self.s, unit), sb)
        self.assertEqual(self.d.os_on_unit(self.s, unit, a), sa)
        self.s.os_left[unit] = ('unknown after a failed flash of x.syx', False)
        with self.assertRaisesRegex(daemon.Refused, 'stop and check'):
            self.d.os_on_unit(self.s, unit)
        self.s.os_left.pop(unit)
        # a Digitone/Digitakt firmware check flashes over no OS: it counts whatever was on the unit
        self.d.record_pass(daemon.flash_key('digitone', d), {'image': img, 'unit': 'digitone', 'fwcheck': 'x'})
        self.assertTrue(self.d.flash_pass('digitone', d, sb))
        # a plan pass from before flash keys named the OS: found when its run booted that OS
        run = os.path.join(TMP, 'emu-run-old-flash')
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, 'summary.json'), 'w') as fh:
            json.dump({'emulator': True, 'pass': True, 'flash_sha256': daemon.sha256(b)}, fh)
        self.d.record_pass('oldplankey', {'run_dir': run, 'unit': unit, 'os': d})
        self.assertTrue(self.d.flash_pass(unit, sb, d))
        self.assertFalse(self.d.flash_pass(unit, sb, sa))

    @needs_runner
    def test_the_plan_key_names_the_os_it_runs_on(self):
        plan = os.path.join(PLANS, 'stock-restore.txt')
        other = os.path.join(PLANS, 'stock-restore-2.txt')
        with open(other, 'w', encoding='utf-8') as fh:
            fh.write(read(plan).replace('os = ../os/test-build.syx', 'os = ../os/remote-only.syx'))
        self.addCleanup(os.remove, other)
        a, b = self.d.plan_info(plan), self.d.plan_info(other)
        self.assertEqual(a['steps'], b['steps'])
        self.assertNotEqual(a['key'], b['key'])                    # same steps, stock back over two builds
        self.assertEqual(a['old_key'], b['old_key'])
        self.assertEqual(a['os_sha'], daemon.sha256(os.path.join(TMP, 'os', 'test-build.syx')))
        # a pass recorded before the key named the OS still counts, for the OS its run booted
        run = os.path.join(TMP, 'emu-run-old-key')
        os.makedirs(run, exist_ok=True)
        with open(os.path.join(run, 'summary.json'), 'w') as fh:
            json.dump({'emulator': True, 'pass': True, 'flash_sha256': a['flash_sha']}, fh)
        self.d.record_pass(a['old_key'], {'run_dir': run, 'unit': 'rytm', 'os': a['os_sha']})
        self.assertTrue(self.d.plan_pass(a))
        self.assertFalse(self.d.plan_pass(b))
        # the twins carry the os setting with an absolute path, so they share the key
        twin = self.d.twin(a, False, self.s.sid)
        self.assertIn('os = %s' % a['os_file'], read(twin))
        self.assertEqual(self.d.plan_info(twin)['key'], a['key'])
        # on hardware, a session that left another OS on the unit is refused before the go is used
        self.fake_pass(b)
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        self.s.os_left['rytm'] = ('test-build.syx', False, a['os_sha'])
        self.addCleanup(self.s.os_left.pop, 'rytm', None)
        with self.assertRaisesRegex(daemon.Refused, 'this session left test-build.syx'):
            self.d.t_run_plan(self.s, {'plan': other, 'target': 'hardware', 'approval': 'x'})

    def test_no_config_means_tools_off(self):
        cfg = daemon.load_config(os.path.join(TMP, 'none.toml'))
        self.assertIsNone(cfg['analoghealth'])
        self.assertTrue(os.path.isfile(cfg['obtr']))      # Total Recall is built in
        self.assertTrue(cfg['units']['rytm']['evidence'].endswith(os.path.join('elkbench', 'rytm', 'runs')))
        self.assertEqual(cfg['person'], 'the person at this PC')
        with self.assertRaisesRegex(daemon.Refused, 'set tools.obtr'):
            daemon.need(None, 'obtr', 'Total Recall')

    def test_engine_needs_all_three(self):
        subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                       env=self.s.env(), capture_output=True)
        with self.assertRaisesRegex(daemon.Refused, 'claim a4 first' if daemon.WINDOWS else 'Overbridge Engine'):
            self.d.t_recall_take(self.s, {'unit': 'rytm'})

    def engine_stubs(self):
        """Total Recall without the Engine: obtr never runs (self.d.start is stubbed), claims are
        taken as held, and the platform check passes on Linux too."""
        saved = daemon.WINDOWS, daemon.OBTR
        daemon.WINDOWS, daemon.OBTR = True, os.path.abspath(__file__)
        self.d.check_hardware = lambda s, units: None
        self.addCleanup(lambda: setattr(daemon, 'WINDOWS', saved[0]) or setattr(daemon, 'OBTR', saved[1]))

    def take(self, s, unit, name, data=b'image'):
        """recall_take as obtr would end it: the image, its .json from save_meta, exit 0."""
        self.d.t_recall_take(s, {'unit': unit, 'name': name})
        job, cmd = self.started[-1]
        out = cmd[cmd.index('take') + 1]
        with open(out, 'wb') as fh:
            fh.write(data)
        with open(os.path.splitext(out)[0] + '.json', 'w') as fh:
            json.dump({'session': 12345, 'total': len(data), 'name': name}, fh)
        self.done(job, 0, 'ok: took %d bytes' % len(data))
        return out, job

    def restore(self, s, unit, img, token):
        return self.d.t_recall_restore(s, {'unit': unit, 'image': img, 'approval': token})

    def test_restore_of_this_runs_take(self):
        self.engine_stubs()
        args = dict(purpose='test', recall_restores=[{'unit': 'rytm', 'take': 'tr09'}])
        out = self.d.t_approval_text(self.s, args)
        body = '\n'.join(out)
        self.assertIn("of the image recall_take 'tr09' writes in this run: tr09.bin", body)
        self.assertIn("ungated: Total Recall can't run in the emulator", body)
        with self.assertRaisesRegex(daemon.Refused, 'bare file name'):
            self.d.t_approval_text(self.s, dict(args, recall_restores=[{'unit': 'rytm', 'take': 'a/b'}]))
        with self.assertRaisesRegex(daemon.Refused, 'image or a take, not both'):
            self.d.t_approval_text(self.s, dict(args, recall_restores=[
                {'unit': 'rytm', 'take': 'tr09', 'image': __file__}]))
        token = self.approve(recall_restores=[{'unit': 'rytm', 'take': 'tr09.bin'}])
        self.assertTrue(token.startswith('ap-'), token)
        img = os.path.join(self.s.folder('rytm'), 'tr09.bin')

        # before the take: no file, then a file nobody's take under the go wrote
        with self.assertRaisesRegex(daemon.Refused, 'no image'):
            self.restore(self.s, 'rytm', img, token)
        with open(img, 'wb') as fh:
            fh.write(b'planted')
        with open(img[:-4] + '.json', 'w') as fh:
            fh.write('{}')
        with self.assertRaisesRegex(daemon.Refused, 'not that one'):
            self.restore(self.s, 'rytm', img, token)
        # a take refuses to overwrite a file already there
        with self.assertRaisesRegex(daemon.Refused, 'already in this session'):
            self.d.t_recall_take(self.s, {'unit': 'rytm', 'name': 'tr09'})
        os.remove(img)
        os.remove(img[:-4] + '.json')

        out, job = self.take(self.s, 'rytm', 'tr09')
        self.assertEqual(os.path.normcase(out), os.path.normcase(img))
        self.assertTrue(any('now covers this file' in l for l in job.lines), job.lines)
        # a second take under the bound name is refused before obtr runs
        n = len(self.started)
        os.rename(img, img + '.away')       # even with the file gone, the binding refuses
        with self.assertRaisesRegex(daemon.Refused, 'already bound its restore'):
            self.d.t_recall_take(self.s, {'unit': 'rytm', 'name': 'tr09.bin'})
        self.assertEqual(len(self.started), n)
        os.rename(img + '.away', img)

        # altered after the take, then another unit
        with open(img, 'ab') as fh:
            fh.write(b'!')
        with self.assertRaisesRegex(daemon.Refused, 'not that one'):
            self.restore(self.s, 'rytm', img, token)
        with open(img, 'wb') as fh:
            fh.write(b'image')
        with self.assertRaisesRegex(daemon.Refused, 'does not cover'):
            self.restore(self.s, 'a4', img, token)
        # a copy elsewhere, same bytes: not the bound path
        other = os.path.join(TMP, 'tr09.bin')
        shutil.copy(img, other)
        shutil.copy(img[:-4] + '.json', other[:-4] + '.json')
        with self.assertRaisesRegex(daemon.Refused, 'does not cover'):
            self.restore(self.s, 'rytm', other, token)

        self.assertEqual(self.restore(self.s, 'rytm', img, token), ['running'])
        cmd = self.started[-1][1]
        self.assertEqual(cmd[cmd.index('restore') + 1], img)
        with self.assertRaisesRegex(daemon.Refused, 'has run; a new run needs a new go'):
            self.restore(self.s, 'rytm', img, token)

    def test_restore_take_is_this_sessions(self):
        self.engine_stubs()
        token = self.approve(recall_restores=[{'unit': 'rytm', 'take': 'mine'}])
        other = daemon.Session('other-session', os.getpid(), TMP)     # its own run folder
        theirs, job = self.take(other, 'rytm', 'mine')
        self.assertFalse(any('now covers' in l for l in job.lines), job.lines)   # their take binds no go of ours
        with self.assertRaisesRegex(daemon.Refused, 'does not cover'):
            self.restore(self.s, 'rytm', theirs, token)
        with self.assertRaisesRegex(daemon.Refused, 'another session'):
            self.restore(other, 'rytm', theirs, token)
        # a take made before the go does not bind it either
        self.d.approvals.clear()
        early, _ = self.take(self.s, 'rytm', 'early')
        token = self.approve(recall_restores=[{'unit': 'rytm', 'take': 'early'}])
        with self.assertRaisesRegex(daemon.Refused, 'not that one'):
            self.restore(self.s, 'rytm', early, token)

    def test_restore_by_sha_still_works(self):
        self.engine_stubs()
        img, _ = self.take(self.s, 'rytm', 'plain')
        self.assertIn('TOTAL RECALL RESTORE plain.bin on rytm', '\n'.join(self.d.t_approval_text(
            self.s, dict(purpose='test', recall_restores=[{'unit': 'rytm', 'image': img}]))))
        token = self.approve(recall_restores=[{'unit': 'rytm', 'image': img}])
        self.assertEqual(self.restore(self.s, 'rytm', img, token), ['running'])
        with self.assertRaisesRegex(daemon.Refused, 'has run'):
            self.restore(self.s, 'rytm', img, token)

    def test_batch_rules(self):
        img = self.saved['stock'][0]
        for a, why in (({'image': img, 'unit': 'rytm'}, 'variants is a list'),
                       ({'image': img, 'plan': 'p', 'variants': [{}]}, 'give plan .* or image'),
                       ({'image': img, 'unit': 'rytm', 'variants': [{}]}, 'variant 1: build'),
                       ({'image': img, 'unit': 'digitone', 'build': img, 'variants': [{}]}, 'tools.digiemu'),
                       ({'image': img, 'unit': 'rytm', 'build': img, 'variants': [{'name': 'x'}, {'name': 'x'}]},
                        'names must differ'),
                       ({'image': img, 'unit': 'rytm', 'build': img, 'variants': [{}], 'parallel': 9}, 'parallel 1-8')):
            with self.assertRaisesRegex(daemon.Refused, why):
                self.d.t_batch(self.s, a)

    def test_gate_batch_rules(self):
        img = self.saved['stock'][0]
        for a, why in (({'plans': ['p']}, 'plans .* is for a gate batch'),
                       ({'gate': True, 'plans': ['p'], 'plan': 'q'}, 'not both'),
                       ({'gate': True, 'plans': ['p'], 'runs': 3}, 'each plan once'),
                       ({'gate': True, 'image': img, 'unit': 'rytm'}, 'a gate batch runs plans'),
                       ({'gate': True, 'variants': [{}]}, 'a gate batch takes plans'),
                       ({'gate': True, 'plans': ['p'] * 17}, 'a gate batch takes plans: a list of 1-16')):
            with self.assertRaisesRegex(daemon.Refused, why):
                self.d.t_batch(self.s, a)

    def test_audio_waits(self):
        steps = [('play', ['tap PLAY', 'wait 30 audio  # hear the pattern', 'wait 2', 'tap STOP']),
                 ('quiet', ['wait 1'])]
        self.assertTrue(daemon.is_audio_wait('wait 8 AUDIO'))
        self.assertFalse(daemon.is_audio_wait('wait 8'))
        self.assertFalse(daemon.is_audio_wait('# wait 8 audio'))
        self.assertEqual(daemon.audio_waits(steps), ['[play] wait 30 audio'])
        short = daemon.emulator_steps(steps)
        self.assertEqual(short[0][1][1].split('#')[0].strip(), 'wait %g' % daemon.AUDIO_WAIT_EMULATOR)
        self.assertEqual(short[0][1][2:], ['wait 2', 'tap STOP'])
        self.assertEqual(short[1], steps[1])

    @needs_runner
    def test_audio_waits_short_in_emulator_twins_only(self):
        plan = os.path.join(PLANS, 'audio-wait.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(read(os.path.join(PLANS, 'menus.txt')).replace('[analoghealth answers]',
                                                                    '[listen]\ntap PLAY\nwait 30 audio\ntap STOP\n\n'
                                                                    '[analoghealth answers]'))
        info = self.d.plan_info(plan)
        self.assertEqual(info['audio_waits'], ['[listen] wait 30 audio'])
        import testplan
        waits = lambda p: [l.split('#')[0].strip() for n, ls in testplan.parse(p)[1] if n == 'listen'
                           for l in ls if l.startswith('wait')]
        self.assertEqual(waits(plan), ['wait 30 audio'])                       # the unit waits in full
        self.assertEqual(waits(self.d.twin(info, False, self.s.sid)), ['wait 0.5'])
        work = tempfile.mkdtemp(dir=TMP)
        self.assertEqual(waits(self.d.batch_plan(info, work, 'batch run 1')), ['wait 0.5'])
        self.assertEqual(self.d.plan_info(plan)['key'], info['key'])           # a pass vouches for the plan
        self.assertEqual(testplan.parse(self.d.twin(info, False, self.s.sid))[0]['name'], info['name'])

    @needs_runner
    def test_gate_batch(self):
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP              # configured: the runs are stand-ins
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        text = read(os.path.join(PLANS, 'menus.txt'))
        plans = []
        for name in ('gate-a', 'gate-b'):
            plans.append(os.path.join(PLANS, name + '.txt'))
            with open(plans[-1], 'w', encoding='utf-8') as fh:
                fh.write(text.replace('name = menus', 'name = ' + name).replace('out = runs', 'out = runs-' + name)
                         + '\n[%s]\nwait 0.1\n' % name)            # steps differ, so keys differ
        self.d.emu_has_snapshots = lambda unit: False
        self.d.batch_emulator = lambda unit, build, work, minutes, snapshot=None, din=False, **kw: (
            None, 'tcp:127.0.0.1:7001', None, None)
        projects = {'after': {121: ('~T A', 'a')}}
        self.d.saved_projects = lambda addr, folder: projects['after'] if folder.endswith('after') else {121: ('~T A', 'a')}
        ran = []

        def run(cmd, **kw):                 # analogrun run PLAN: a passing run in the plan's out folder
            twin = read(cmd[-1])
            out = re.search(r'^out = (.+)$', twin, re.M).group(1)
            d = os.path.join(out, 'run-%d' % len(ran))
            os.makedirs(d)
            with open(os.path.join(d, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': True}, fh)
            ran.append(twin)
            return subprocess.CompletedProcess(cmd, 0, 'x on the emulator: PASS (3 steps, 1 s) - %s\n' % d, '')
        import types
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=run))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)

        def gate(**a):
            out = self.d.t_batch(self.s, {'gate': True, 'plans': plans, 'build': build, **a})
            if not out[0].startswith('running'):
                return out
            return self.d.t_status(self.s, {'run_id': out[0].split('run_id ')[1].split()[0], 'wait_s': 30})
        res = gate()
        self.assertTrue(res[0].startswith('done: '), res)
        self.assertIn('2 of 2 plan(s) passed and recorded', res[1])
        self.assertEqual(len(ran), 2)
        for p in plans:
            info = self.d.plan_info(p)
            ep = self.d.find_pass(info['key'])
            self.assertEqual((ep['os'], ep['start']), (daemon.sha256(build), 'a fresh emulator'))
            self.assertIn(os.path.join(os.path.dirname(p), 'runs-' + info['name'], 'emu-'), ep['run_dir'])
        # nothing changed: nothing runs again
        out = gate()
        self.assertTrue(out[0].startswith('ok: nothing to gate'), out)
        self.assertEqual(len([l for l in out if l.startswith('kept: ')]), 2)
        self.assertEqual(len(ran), 2)
        # one plan changed: only that one runs
        with open(plans[1], 'a', encoding='utf-8') as fh:
            fh.write('\n[one more]\nwait 1\n')
        res = gate()
        self.assertIn('1 of 1 plan(s) passed', res[1])
        self.assertTrue(any(l.startswith('kept: gate-a') for l in res), res)
        self.assertEqual(len(ran), 3)
        # another build: both run again
        res = gate(build=os.path.join(TMP, 'os', 'stock.syx'))
        self.assertIn('2 of 2', res[1])
        # a write outside the plan's slots fails the run and records nothing
        projects['after'] = {121: ('~T A', 'a'), 5: ('MINE', 'b')}
        forget_passes()
        res = gate()
        self.assertTrue(res[0].startswith('fail: '), res)
        self.assertIn("write outside the plan's slots: project 5 added", '\n'.join(res))
        self.assertIsNone(self.d.find_pass(self.d.plan_info(plans[0])['key']))

    def sample(self, name, unit='rytm'):
        """A sample plan in tests/plans for `unit`: the Rytm's as it is; another unit's a copy
        beside it with its own unit and out folder (runs-<unit>), so the two never share runs."""
        path = os.path.join(PLANS, name)
        if unit == 'rytm':
            return path
        copy = os.path.join(PLANS, '%s-%s.txt' % (name[:-4], unit))
        with open(copy, 'w', encoding='utf-8') as fh:
            fh.write(read(path).replace('unit = rytm\n', 'unit = %s\n' % unit, 1).replace(
                'out = runs\n', 'out = runs-%s\n' % unit, 1))
        return copy

    def with_starts(self, unit='rytm'):
        """Stand-in built-in starts (dirty, occupied, testslot, testslot-cleared) of `unit` for one test."""
        folder = os.path.join(TMP, 'starts-%d' % len(os.listdir(TMP)))
        os.makedirs(os.path.join(folder, unit))
        for name in ('dirty', 'occupied', 'testslot', 'testslot-cleared'):
            with open(os.path.join(folder, unit, name + '.txt'), 'w', encoding='utf-8') as fh:
                fh.write('format = 1\nname = start %s\nunit = %s\nemulator = yes\n\n[%s]\nwait 0.1\n'
                         % (name, unit, name))
        saved = daemon.STARTS
        daemon.STARTS = folder
        self.addCleanup(setattr, daemon, 'STARTS', saved)
        return folder

    @needs_runner
    def test_contrast_and_start_runs(self, unit='rytm'):
        """Proposal A and B: a gate runs each plan from the built-in starts too; a run from a start
        passes, or stops in the section its contrast names; the go needs them all."""
        self.with_starts(unit)
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        text = read(self.sample('menus.txt', unit)).replace('name = menus', 'name = contrasted').replace(
            'out = runs', 'out = runs-contrasted')
        plan = os.path.join(PLANS, 'contrasted.txt')
        # no contrast on the start check: refused where the unit has built-in starts
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text)
        out = self.d.t_check_plan(self.s, {'plans': [plan]})
        self.assertIn('[start] has no contrast', '\n'.join(out))
        self.assertIn('no run from start dirty (must pass) yet', '\n'.join(out))
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('tap GLOBAL\n', 'say contrast occupied, testslot: the global menu differs\ntap GLOBAL\n'))
        info = self.d.plan_info(plan)
        self.assertEqual(info['check']['errors'], [])
        self.assertEqual(daemon.starts_needed(info), {'dirty': None, 'occupied': 'start', 'testslot': 'start'})
        self.d.emu_has_snapshots = lambda unit: False          # each start is built in its run
        self.d.batch_emulator = lambda unit, build, work, minutes, snapshot=None, din=False, **kw: (
            None, 'tcp:127.0.0.1:7001', None, None)
        self.d.saved_projects = lambda addr, folder: {121: ('~T A', 'a')}
        ran, behave = [], {}

        def run(cmd, **kw):
            twin = read(cmd[-1])
            out = re.search(r'^out = (.+)$', twin, re.M).group(1)
            if 'start ' in twin.splitlines()[0]:            # a start plan: builds its state
                ran.append(('start', out))
                os.makedirs(out, exist_ok=True)
                with open(os.path.join(out, 'summary.json'), 'w') as fh:
                    json.dump({'emulator': True, 'pass': True}, fh)
                return subprocess.CompletedProcess(cmd, 0, 'x on the emulator: PASS (1 steps, 1 s) - %s\n' % out, '')
            start = next((st for st in ('dirty', 'occupied', 'testslot') if out.endswith('-' + st)), 'main')
            stop = behave.get(start)
            d = os.path.join(out, 'run-%d' % len(ran))
            os.makedirs(d)
            steps = [{'step': 'start', 'result': 'FAIL' if stop == 'start' else 'ok', 'detail': 'differs'}]
            steps.append({'step': 'project menu', 'result': 'FAIL' if stop == 'project menu' else 'ok', 'detail': 'x'})
            ok = not stop
            with open(os.path.join(d, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': ok, 'steps': steps}, fh)
            ran.append((start, d))
            return subprocess.CompletedProcess(cmd, 0 if ok else 1, 'x on the emulator: %s (3 steps, 1 s) - %s\n'
                                               % ('PASS' if ok else 'FAIL', d), '')
        import types
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=run))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)

        def gate():
            out = self.d.t_batch(self.s, {'gate': True, 'plans': [plan], 'build': build})
            if not out[0].startswith('running'):
                return out
            return self.d.t_status(self.s, {'run_id': out[0].split('run_id ')[1].split()[0], 'wait_s': 30})
        # occupied passes too: its contrast is not seen to fail, so nothing about it is recorded
        behave.update(testslot='start', dirty=None, occupied=None)
        res = gate()
        text = '\n'.join(res)
        self.assertTrue(res[0].startswith('fail: '), res)
        self.assertIn('1 of 1 plan(s) passed and recorded as their emulator pass; 2 of 3 start run(s)', text)
        self.assertIn("contrasted @occupied: FAIL: passed, so [start] cannot tell this start apart", text)
        self.assertIn('contrasted @testslot: stopped in [start] as its contrast says: differs', text)
        with self.assertRaisesRegex(daemon.Refused, r'no emulator run from start occupied \(must stop in \[start\]\)'):
            self.d.go_request(self.s, {'plans': [plan]})
        # stopping earlier than its section does not count either
        behave.update(occupied='project menu')
        new = read(plan).replace('say contrast occupied, testslot:', 'say contrast testslot:').replace(
            'say proves works: YES', 'say contrast occupied: the menu differs\nsay proves works: YES')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(new)
        behave.update(occupied='start')
        res = gate()
        self.assertIn('stopped in [start] before [project menu]', '\n'.join(res))
        behave.update(occupied='project menu')
        res = gate()
        text = '\n'.join(res)
        self.assertTrue(res[0].startswith('done: '), res)
        items, body = self.d.go_request(self.s, {'plans': [plan]})
        self.assertIn('start dirty: passed', body)
        self.assertIn('start occupied: stopped in [project menu] as its contrast says', body)
        self.assertIn('contrast: from occupied it stops in [project menu] (the menu differs)', body)
        # nothing changed: nothing runs again
        n = len(ran)
        out = gate()
        self.assertTrue(out[0].startswith('ok: nothing to gate'), out)
        self.assertEqual(len(ran), n)

    @needs_runner
    def test_gate_runs_a_power_cycle_plans_start_runs(self, unit='rytm'):
        """A gate batch runs a power-cycle plan's start runs, each up to its first power cycle, and
        records them; its own pass comes from run_plan, and the go then takes the plan."""
        self.with_starts(unit)
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        # the plan names its OS (os =): the gate boots that file, with no build given
        build = os.path.join(TMP, 'os', 'test-build.syx')
        plan = os.path.join(PLANS, 'pc-contrasted.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(read(self.sample('power-cycle.txt', unit)).replace(
                'name = power cycle', 'name = pc contrasted\nos = %s' % build).replace('out = runs', 'out = runs-pc').replace(
                'tap GLOBAL\nexpect-screen global-menu 2\ntap NO\n',
                'tap GLOBAL\nsay contrast fresh, dirty, occupied: the menu differs\nexpect-screen global-menu 2\ntap NO\n', 1))
        self.addCleanup(os.remove, plan)
        info = self.d.plan_info(plan)
        self.assertEqual(info['check']['errors'], [])
        self.assertEqual(daemon.starts_needed(info), {'dirty': 'start', 'occupied': 'start', 'fresh': 'start'})
        self.d.emu_has_snapshots = lambda unit: False
        booted = []

        def emu(unit, build, work, minutes, snapshot=None, din=False, **kw):
            booted.append(build)
            return None, 'tcp:127.0.0.1:7001', None, None
        self.d.batch_emulator = emu
        self.d.saved_projects = lambda addr, folder: {121: ('~T A', 'a')}
        twins = []

        def run(cmd, **kw):
            twin = read(cmd[-1])
            out = re.search(r'^out = (.+)$', twin, re.M).group(1)
            os.makedirs(out, exist_ok=True)
            if 'start ' in twin.splitlines()[0]:
                with open(os.path.join(out, 'summary.json'), 'w') as fh:
                    json.dump({'emulator': True, 'pass': True}, fh)
                return subprocess.CompletedProcess(cmd, 0, 'x on the emulator: PASS (1 steps, 1 s) - %s\n' % out, '')
            twins.append(twin)
            d = os.path.join(out, 'run-%d' % len(twins))
            os.makedirs(d)
            with open(os.path.join(d, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': False,
                           'steps': [{'step': 'start', 'result': 'FAIL', 'detail': 'differs'}]}, fh)
            return subprocess.CompletedProcess(cmd, 1, 'x on the emulator: FAIL (3 steps, 1 s) - %s\n' % d, '')
        import types
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=run))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)
        out = self.d.t_batch(self.s, {'gate': True, 'plans': [plan]})
        self.assertTrue(out[0].startswith('running'), out)
        self.assertIn('its own pass comes from run_plan target emulator', '\n'.join(out))
        res = self.d.t_status(self.s, {'run_id': out[0].split('run_id ')[1].split()[0], 'wait_s': 30})
        text = '\n'.join(res)
        self.assertTrue(res[0].startswith('done: '), res)
        self.assertIn('0 of 0 plan(s) passed and recorded as their emulator pass; 3 of 3 start run(s)', text)
        self.assertEqual(set(booted), {build})
        self.assertEqual(len(twins), 3)                        # no main run: it would power cycle
        for t in twins:
            self.assertNotIn('power-cycle', t)
            self.assertNotIn('after the power cycle', t)
            self.assertNotIn('expect-screen global-menu 2\n', t.split('[kept]')[1])
            self.assertIn('[start]', t)
        # the go wants the plan's own pass, which run_plan records
        with self.assertRaisesRegex(daemon.Refused, 'no passing emulator run'):
            self.d.go_request(self.s, {'plans': [plan]})
        self.fake_pass(self.d.plan_info(plan))
        _, body = self.d.go_request(self.s, {'plans': [plan]})
        self.assertIn('start fresh: stopped in [start] as its contrast says', body)
        self.assertIn('POWER CYCLE BY HAND', body)
        # again: the start runs stand, nothing runs
        out = self.d.t_batch(self.s, {'gate': True, 'plans': [plan]})
        self.assertTrue(out[0].startswith('ok: nothing to gate'), out)
        self.assertEqual(len(twins), 3)

    @needs_runner
    def test_a_start_snapshot_is_built_once(self, unit='rytm'):
        """With snapshots, a built-in start is built once per build and start plan, then resumed."""
        folder = self.with_starts(unit)
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        build = os.path.join(TMP, 'os', 'test-build.syx')
        booted = []

        class Stdin:
            def __init__(self, log, live):
                self.log, self.live = log, live

            def write(self, b):
                with open(self.live, 'wb') as fh:
                    fh.write(b'snapshot')
                with open(self.log, 'a') as fh:
                    fh.write('saved %s\n' % self.live)

            def flush(self):
                pass

        def emu(unit, build, work, minutes, snapshot=None, din=False, **kw):
            booted.append(snapshot)
            log = os.path.join(work, 'emulator.log')
            open(log, 'w').close()
            proc = type('P', (), {'stdin': Stdin(log, snapshot), 'terminate': lambda self: None,
                                  'wait': lambda self, t: 0})()
            return proc, 'tcp:127.0.0.1:7002', None, None
        self.d.batch_emulator = emu
        self.d.start_steps = lambda s, unit, v, addr, work, minutes: None
        out = os.path.join(TMP, 'start-snap')
        os.makedirs(out, exist_ok=True)
        v = dict(start='dirty', build=build, start_plan=os.path.join(folder, unit, 'dirty.txt'))
        self.assertIsNone(self.d.batch_start(self.s, unit, v, 5, out))
        self.assertTrue(os.path.isfile(v['start_store']) and len(booted) == 1)
        w = dict(v)
        del w['start_store']
        self.assertIsNone(self.d.batch_start(self.s, unit, w, 5, out))
        self.assertEqual((w['start_store'], len(booted)), (v['start_store'], 1))     # resumed, not rebuilt
        with open(v['start_plan'], 'a') as fh:                                          # a changed start: rebuilt
            fh.write('wait 0.2\n')
        x = dict(start='dirty', build=build, start_plan=v['start_plan'])
        self.assertIsNone(self.d.batch_start(self.s, unit, x, 5, out))
        self.assertNotEqual(x['start_store'], v['start_store'])
        self.assertEqual(len(booted), 2)

    def test_contrast_fresh_leaves_the_pass_to_run_plan(self):
        """A plan whose start check stops on a fresh emulator (a readback after another plan) gets
        no main run in a gate: only its starts."""
        self.with_starts()
        plan = os.path.join(PLANS, 'readback.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nname = readback\nunit = rytm\nrefs = refs/menus\n\n[start]\ntap GLOBAL\n'
                     'say contrast fresh, dirty, occupied: a default project has no kept value\n'
                     'expect-screen global-menu 2\n')
        info = self.d.plan_info(plan)
        self.assertEqual(info['check']['errors'], [])
        self.assertEqual(daemon.starts_needed(info), {'fresh': 'start', 'dirty': 'start', 'occupied': 'start'})

    @needs_runner
    def test_a_start_builds_on_a_base_with_data_steps(self, unit='rytm'):
        """A start file may build on another start (`base`) and run data store steps (`data`)
        between its base and its own sections; loops and bad steps are refused."""
        folder = self.with_starts(unit)
        here = os.path.join(folder, unit)
        with open(os.path.join(here, 'backup.bin'), 'wb') as fh:
            fh.write(b'project')
        cleared = os.path.join(here, 'cleared.txt')
        with open(cleared, 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nname = start cleared\nunit = %s\nemulator = yes\nbase = testslot\n'
                     'data = clear /projects/121; put /projects/122 backup.bin\n\n[shot]\nshot x.png\n' % unit)
        chain = daemon.start_chain(unit, 'cleared')
        self.assertEqual([c[0] for c in chain], ['testslot', 'cleared'])
        self.assertEqual(chain[0][2], [])
        self.assertEqual(chain[1][2], [('clear', '/projects/121', None),
                                       ('put', '/projects/122', os.path.join(here, 'backup.bin'))])
        # the build: the base's plan, then the data steps, then the start's own sections
        calls = []

        def run(cmd, **kw):
            if 'data' in cmd:
                calls.append(('data', cmd[cmd.index('data') + 1], cmd[cmd.index('data') + 2]))
                return subprocess.CompletedProcess(cmd, 0, 'ok\n', '')
            twin = read(cmd[-1])
            out = re.search(r'^out = (.+)$', twin, re.M).group(1)
            calls.append(('plan', re.search(r'^include = (.+)$', twin, re.M).group(1)))
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': True}, fh)
            return subprocess.CompletedProcess(cmd, 0, 'x on the emulator: PASS (1 steps, 1 s) - %s\n' % out, '')
        import types
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=run))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)
        work = os.path.join(TMP, 'start-chain')
        self.assertIsNone(self.d.start_steps(self.s, unit, dict(start='cleared', start_chain=chain),
                                             'tcp:127.0.0.1:7003', work, 5))
        self.assertEqual(calls, [('plan', os.path.join(here, 'testslot.txt')), ('data', 'clear', '/projects/121'),
                                 ('data', 'put', '/projects/122'), ('plan', cleared)])
        # the snapshot key follows the base's text too
        build = os.path.join(TMP, 'os', 'test-build.syx')
        k1 = self.d.start_key(unit, build, chain)
        with open(os.path.join(here, 'testslot.txt'), 'a') as fh:
            fh.write('wait 0.2\n')
        self.assertNotEqual(k1, self.d.start_key(unit, build, daemon.start_chain(unit, 'cleared')))
        # refused: a loop, an unknown base, a bad data step, a missing put file
        for setting, why in (('base = cleared', 'builds on itself'), ('base = nowhere', 'no built-in start'),
                             ('data = wipe /projects/121', 'is `clear PATH` or `put PATH FILE`'),
                             ('data = clear projects/121', 'is `clear PATH` or `put PATH FILE`'),
                             ('data = put /projects/122 gone.bin', 'no file')):
            with open(cleared, 'w', encoding='utf-8') as fh:
                fh.write('format = 1\nname = start cleared\nunit = %s\nemulator = yes\n%s\n\n[shot]\n'
                         'shot x.png\n' % (unit, setting))
            with self.assertRaisesRegex(daemon.Refused, re.escape(why)):
                daemon.start_chain(unit, 'cleared')

    @needs_runner
    def test_a_digitone_start_is_built_through_digirun(self):
        """A Digitone start plan goes through analogrun with DIGIRUN_PATH set, as a batch run does,
        so analogrun can hand it to digirun; its data steps still go through analogrun's `data`."""
        here = os.path.join(self.with_starts('digitone'), 'digitone')
        with open(os.path.join(here, 'testslot-cleared.txt'), 'w', encoding='utf-8') as fh:
            fh.write('format = 1\nname = start testslot-cleared\nunit = digitone\nemulator = yes\n'
                     'base = testslot\ndata = clear /projects/121\n\n[shot]\nshot x.png\n')
        saved = daemon.DIGIHEALTH
        daemon.DIGIHEALTH = TMP
        self.addCleanup(setattr, daemon, 'DIGIHEALTH', saved)
        chain = daemon.start_chain('digitone', 'testslot-cleared')
        calls = []

        def run(cmd, **kw):
            path = kw.get('env', {}).get('DIGIRUN_PATH')
            if 'data' in cmd:
                calls.append(('data', cmd[cmd.index('data') + 1]))
                return subprocess.CompletedProcess(cmd, 0, 'ok\n', '')
            twin = read(cmd[-1])
            out = re.search(r'^out = (.+)$', twin, re.M).group(1)
            calls.append(('plan', path))
            os.makedirs(out, exist_ok=True)
            with open(os.path.join(out, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': True}, fh)
            return subprocess.CompletedProcess(cmd, 0, 'x on the emulator: PASS (1 steps, 1 s) - %s\n' % out, '')
        import types
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=run))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)
        work = os.path.join(TMP, 'start-digitone')
        self.assertIsNone(self.d.start_steps(self.s, 'digitone', dict(start='testslot-cleared', start_chain=chain),
                                             'tcp:127.0.0.1:7005', work, 5))
        tools = os.path.join(TMP, 'tools')
        self.assertEqual(calls, [('plan', tools), ('data', 'clear'), ('plan', tools)])

    def test_digitone_starts_ship(self):
        """The Digitone ships the four built-in starts, and its templates start from testslot-cleared."""
        self.assertEqual(sorted(f for f in os.listdir(os.path.join(HERE, 'starts', 'digitone'))),
                         ['dirty.txt', 'occupied.txt', 'testslot-cleared.txt', 'testslot.txt'])
        for name in ('setup-test-project', 'capture-with-level-check', 'listen'):
            text = read(os.path.join(HERE, 'templates', 'digitone', name + '.txt'))
            self.assertIn('\nstart = testslot-cleared\n', text, name)
            self.assertIn('say contrast occupied, testslot:', text, name)

    @needs_runner
    def test_required_starts_where_release_empties_the_test_slots(self, unit='rytm'):
        """A unit whose release empties its test slots (clear_at_release) is never found with them
        full: its plans run from testslot-cleared, the state every release leaves, not occupied."""
        self.with_starts(unit)
        text = read(self.sample('menus.txt', unit)).replace('name = menus', 'name = cleared').replace(
            'out = runs', 'out = runs-cleared').replace(
            'tap GLOBAL\n', 'say contrast testslot: the global menu differs\ntap GLOBAL\n', 1)
        plan = os.path.join(PLANS, 'cleared.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text)
        saved = daemon.UNITS[unit].get('clear_at_release')
        self.addCleanup(daemon.UNITS[unit].__setitem__, 'clear_at_release', saved or [])
        daemon.UNITS[unit]['clear_at_release'] = []
        self.assertEqual(daemon.required_starts(unit), ('dirty', 'occupied'))
        self.assertEqual(daemon.starts_needed(self.d.plan_info(plan)),
                         {'dirty': None, 'occupied': None, 'testslot': 'start'})
        daemon.UNITS[unit]['clear_at_release'] = list(range(121, 129))
        self.assertEqual(daemon.required_starts(unit), ('dirty', 'testslot-cleared'))
        self.assertEqual(daemon.starts_needed(self.d.plan_info(plan)),
                         {'dirty': None, 'testslot-cleared': None, 'testslot': 'start'})
        # a contrast may still name occupied: then its run is needed, and must stop
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('say contrast testslot:', 'say contrast occupied, testslot:'))
        self.assertEqual(daemon.starts_needed(self.d.plan_info(plan)),
                         {'dirty': None, 'testslot-cleared': None, 'occupied': 'start', 'testslot': 'start'})

    @needs_runner
    def test_a_plan_can_start_from_a_built_in_start(self, unit='rytm'):
        """`start = START` in a hardware plan: its own gate run (and pass) starts from that built-in
        start; run_plan does not record its pass, and the setting is part of what a pass vouches for."""
        self.with_starts(unit)
        text = read(self.sample('menus.txt', unit)).replace('name = menus', 'name = homed').replace(
            'out = runs', 'out = runs-homed').replace(
            'tap GLOBAL\n', 'say contrast occupied: the global menu differs\ntap GLOBAL\n', 1)
        plan = os.path.join(PLANS, 'homed.txt')
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text)
        plain = self.d.plan_info(plan)
        self.assertIsNone(plain['home'])
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('name = homed', 'name = homed\nstart = testslot'))
        info = self.d.plan_info(plan)
        self.assertEqual(info['home'], 'testslot')
        self.assertNotEqual(info['key'], plain['key'])
        self.assertEqual(daemon.starts_needed(info), {'dirty': None, 'occupied': 'start'})
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('name = homed', 'name = homed\nstart = dirty'))
        self.assertEqual(daemon.starts_needed(self.d.plan_info(plan)), {'occupied': 'start'})
        # a required start the plan's start check rejects is named in its contrast: it must stop
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('name = homed', 'name = homed\nstart = testslot').replace(
                'say contrast occupied:', 'say contrast dirty, occupied:'))
        self.assertEqual(daemon.starts_needed(self.d.plan_info(plan)), {'dirty': 'start', 'occupied': 'start'})
        for setting, why in (('start = nowhere', 'no built-in start'),
                             ('start = occupied', 'a contrast says the run from occupied stops')):
            with open(plan, 'w', encoding='utf-8') as fh:
                fh.write(text.replace('name = homed', 'name = homed\n' + setting))
            with self.assertRaisesRegex(daemon.Refused, re.escape(why)):
                self.d.plan_info(plan)
        # a gate: the plan's own run resumes the start and records the pass from it
        with open(plan, 'w', encoding='utf-8') as fh:
            fh.write(text.replace('name = homed', 'name = homed\nstart = testslot'))
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        self.d.emu_has_snapshots = lambda unit: False
        self.d.batch_emulator = lambda unit, build, work, minutes, snapshot=None, din=False, **kw: (
            None, 'tcp:127.0.0.1:7004', None, None)
        self.d.saved_projects = lambda addr, folder: {121: ('~T A', 'a')}
        ran = []

        def run(cmd, **kw):
            twin = read(cmd[-1])
            out = re.search(r'^out = (.+)$', twin, re.M).group(1)
            first = twin.splitlines()[0]
            if 'start ' in first:
                ran.append(('build', re.search(r'start (\w+)', first).group(1)))
                os.makedirs(out, exist_ok=True)
                with open(os.path.join(out, 'summary.json'), 'w') as fh:
                    json.dump({'emulator': True, 'pass': True}, fh)
                return subprocess.CompletedProcess(cmd, 0, 'x on the emulator: PASS (1 steps, 1 s) - %s\n' % out, '')
            start = next((st for st in ('dirty', 'occupied', 'testslot') if out.endswith('-' + st)), 'main')
            stop = start == 'occupied'
            d = os.path.join(out, 'run-%d' % len(ran))
            os.makedirs(d)
            steps = [{'step': 'start', 'result': 'FAIL' if stop else 'ok', 'detail': 'differs'}]
            with open(os.path.join(d, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': not stop, 'steps': steps}, fh)
            ran.append((start, d))
            return subprocess.CompletedProcess(cmd, 1 if stop else 0, 'x on the emulator: %s (1 steps, 1 s) - %s\n'
                                               % ('FAIL' if stop else 'PASS', d), '')
        import types
        daemon.subprocess = types.SimpleNamespace(**dict(vars(subprocess), run=run))
        self.addCleanup(setattr, daemon, 'subprocess', subprocess)
        out = self.d.t_batch(self.s, {'gate': True, 'plans': [plan], 'build': build})
        res = self.d.t_status(self.s, {'run_id': out[0].split('run_id ')[1].split()[0], 'wait_s': 30})
        self.assertTrue(res[0].startswith('done: '), res)
        self.assertNotIn('main', [r[0] for r in ran])                  # no run from a fresh emulator
        self.assertIn(('build', 'testslot'), ran)
        self.assertIn('testslot', [r[0] for r in ran])
        ep = self.d.find_pass(self.d.plan_info(plan)['key'])
        self.assertEqual(ep['start'], 'built-in start testslot')
        items, body = self.d.go_request(self.s, {'plans': [plan]})
        self.assertIn('(from built-in start testslot)', body)
        n = len(ran)
        out = self.d.t_batch(self.s, {'gate': True, 'plans': [plan], 'build': build})
        self.assertTrue(out[0].startswith('ok: nothing to gate'), out)
        self.assertEqual(len(ran), n)

    def test_a_flash_plan_needs_no_built_in_starts(self):
        """A plan that flashes boots the OS before its flash, often stock with no remote keys: the
        built-in starts cannot be built there, and the flash is gated as a transition."""
        self.with_starts()
        info = self.d.plan_info(os.path.join(PLANS, 'menus-flash.txt'))
        self.assertEqual(info['check']['errors'], [])
        self.assertEqual(daemon.starts_needed(info), {})

    def test_a_pass_after_ungated_writes_is_not_recorded(self):
        """Proposal B.3 and B.4: a pass records what ran before it on the session's emulator, and a
        pass that ran after an emulator-only write is not recorded."""
        proc = type('P', (), {'poll': lambda self: None, 'pid': 99})()
        self.d.emulators[(self.s.sid, 'rytm')] = dict(proc=proc, session=self.s.sid, build='x', sha='b',
                                                      address='tcp:127.0.0.1:7399', history=[])
        self.d.saved_projects = lambda addr, folder: {}
        plan = os.path.join(PLANS, 'menus.txt')

        def run_and_finish():
            self.d.t_run_plan(self.s, {'plan': plan, 'target': 'emulator'})
            job, _ = self.started[-1]
            d = os.path.join(TMP, 'b3-run-%d' % len(self.started))
            os.makedirs(d)
            with open(os.path.join(d, 'summary.json'), 'w') as fh:
                json.dump({'emulator': True, 'pass': True}, fh)
            self.done(job, 0, 'menus on the emulator: PASS (4 steps, 1 s) - %s\n' % d)
            return job
        self.d.note_emu(self.s, 'rytm', 'plan C', False)
        job = run_and_finish()
        self.assertIn('recorded as the emulator pass for this plan', job.lines)
        ep = self.d.find_pass(self.d.plan_info(plan)['key'])
        self.assertEqual(ep['after'], ['plan C'])
        forget_passes()
        self.d.note_emu(self.s, 'rytm', 'data put /projects/122', True)
        job = run_and_finish()
        self.assertTrue(any(l.startswith('not recorded as the emulator pass: it ran after data put /projects/122')
                            for l in job.lines), job.lines)
        self.assertIsNone(self.d.find_pass(self.d.plan_info(plan)['key']))

    @needs_runner
    def test_gate_batch_shares_one_setup_snapshot(self):
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)
        build = os.path.join(TMP, 'os', 'test-build.syx')
        text = read(os.path.join(PLANS, 'menus.txt'))
        first = re.search(r'^\[(.+)\]$', text, re.M).group(1)
        plans = []
        for name in ('share-a', 'share-b', 'share-c'):
            plans.append(os.path.join(PLANS, name + '.txt'))
            with open(plans[-1], 'w', encoding='utf-8') as fh:
                fh.write(text.replace('format = 1', 'format = 1\nsnapshot-after = %s' % first, 1)
                         + '\n[%s]\nwait 0.1\n' % name)
        self.d.emu_has_snapshots = lambda unit: True
        taken, done = [], set()

        def snap(s, unit, v, minutes, folder):
            store = os.path.join(TMP, 'shared-%s.snap' % self.d.snapshot_key(v['info'], v['build'])[:8])
            v['store'] = store
            taken.append((v['name'], store in done))     # was the shared one there already?
            done.add(store)
        self.d.batch_snapshot = snap
        got = []
        self.d.batch_run = lambda s, unit, flash, minutes, folder, n, v: (
            got.append((v['name'], v.get('store'))), dict(outcome='pass', line='ok', run_dir='d'))[1]
        out = self.d.t_batch(self.s, {'gate': True, 'plans': plans, 'build': build})
        self.assertIn('setups from snapshots', out[0])
        res = self.d.t_status(self.s, {'run_id': out[0].split('run_id ')[1].split()[0], 'wait_s': 30})
        self.assertIn('3 of 3 plan(s) passed', res[1])
        self.assertIn('3 resumed a setup snapshot', res[1])
        self.assertEqual(sum(not there for _, there in taken), 1)     # one setup run for all three
        self.assertEqual(len({st for _, st in got}), 1)

    def test_batch_runs_interleaved_and_sums_up(self):
        saved = daemon.ANALOG_EMU, daemon.ELKFLASH
        daemon.ANALOG_EMU = daemon.ELKFLASH = TMP              # configured: batch_run is a stand-in
        self.addCleanup(lambda: setattr(daemon, 'ANALOG_EMU', saved[0]) or setattr(daemon, 'ELKFLASH', saved[1]))
        seen = []

        def run(s, unit, flash, minutes, folder, n, v):
            seen.append(v['name'])
            time.sleep(0.01)
            hang = v['name'] == 'nofix' and seen.count('nofix') % 3 == 1      # 5 of its 15
            return dict(outcome='hang' if hang else 'pass', line='stuck' if hang else 'ok',
                        folder=os.path.join(folder, 'r%02d' % n) if hang else None)
        self.d.batch_run = run
        os.makedirs(os.environ['ELKBENCH_DIR'], exist_ok=True)   # calls.jsonl
        os_ = os.path.join(TMP, 'os')
        out = self.d.t_batch(self.s, {'unit': 'rytm', 'image': os.path.join(os_, 'stock.syx'), 'runs': 15,
                                      'parallel': 1, 'seed': 7, 'variants': [
                                          {'name': 'fix', 'build': os.path.join(os_, 'test-build.syx')},
                                          {'name': 'nofix', 'build': os.path.join(os_, 'stock.syx')}]})
        self.assertTrue(out[0].startswith('running: batch stock.syx x15, 30 runs'), out)
        rid = out[0].split('run_id ')[1].split()[0]
        res = self.d.t_status(self.s, {'run_id': rid, 'wait_s': 30})
        self.assertTrue(res[0].startswith('done: '), res)
        self.assertEqual(sorted(seen[:2]), ['fix', 'nofix'])  # every round holds each variant once
        self.assertTrue(all(sorted(seen[i:i + 2]) == ['fix', 'nofix'] for i in range(0, 30, 2)))
        self.assertEqual(seen, [daemon.batch.order(['fix', 'nofix'], 15, 7)[i] for i in range(30)])
        self.assertIn('fix (test-build.syx): 15/15 pass', res[2])
        self.assertIn('nofix (stock.syx): 10/15 pass', res[3])
        self.assertIn('5 hang', res[3])
        self.assertIn('nofix vs fix: p = 0.042', res[4])
        self.assertTrue(res[-1].startswith('results: '), res)
        self.assertLessEqual(len(res), 14)                    # a few lines, however many runs
        folder = res[-1][len('results: '):].split(' (')[0]
        with open(os.path.join(folder, 'results.jsonl'), encoding='utf-8') as fh:
            self.assertEqual(len(fh.readlines()), 30)
        self.assertEqual(json.loads(read(os.path.join(folder, 'batch.json')))['seed'], 7)

    def test_batch_stats(self):
        b = daemon.batch
        self.assertAlmostEqual(b.fisher(15, 15, 10, 15), 0.0421, places=3)
        self.assertEqual(b.fisher(5, 10, 5, 10), 1.0)
        lo, hi = b.wilson(0, 15)
        self.assertEqual(lo, 0.0)
        self.assertAlmostEqual(hi, 0.204, places=2)
        self.assertEqual(b.holm([0.01, 0.04, 0.03]), [0.03, 0.06, 0.06])
        self.assertNotEqual(b.order('ABC', 20, 1), b.order('ABC', 20, 2))

    # The Analog Four MKII runs the same release, power-cycle and start-run code as the Rytm: each
    # test above that takes `unit` runs again for it (stand-in starts, sample plans with unit = a4).
    def test_power_cycle_plans_refused_a4(self):
        self.test_power_cycle_plans_refused('a4')

    def test_power_cycle_emulator_a4(self):
        self.test_power_cycle_emulator('a4')

    def test_power_cycle_run_plan_a4(self):
        self.test_power_cycle_run_plan('a4')

    def test_release_empties_test_slots_a4(self):
        self.test_release_empties_test_slots('a4')

    def test_contrast_and_start_runs_a4(self):
        self.test_contrast_and_start_runs('a4')

    def test_gate_runs_a_power_cycle_plans_start_runs_a4(self):
        self.test_gate_runs_a_power_cycle_plans_start_runs('a4')

    def test_a_start_snapshot_is_built_once_a4(self):
        self.test_a_start_snapshot_is_built_once('a4')

    def test_a_start_builds_on_a_base_with_data_steps_a4(self):
        self.test_a_start_builds_on_a_base_with_data_steps('a4')

    def test_required_starts_where_release_empties_the_test_slots_a4(self):
        self.test_required_starts_where_release_empties_the_test_slots('a4')

    def test_a_plan_can_start_from_a_built_in_start_a4(self):
        self.test_a_plan_can_start_from_a_built_in_start('a4')


def stop_daemon(state):
    """Stop the daemon a test started with this state folder, if it is still running."""
    d = os.path.join(state, 'daemon.json')
    if not os.path.exists(d):
        return
    with open(d) as fh:
        pid = json.load(fh)['pid']
    if daemon.WINDOWS:
        subprocess.run(['taskkill', '/F', '/PID', str(pid)], capture_output=True)
    else:
        try:
            os.kill(pid, 9)
        except OSError:
            pass


def tearDownModule():
    stop_daemon(os.environ['ELKBENCH_DIR'])
    for _ in range(10):                               # Windows frees a killed daemon's files late
        shutil.rmtree(TMP, ignore_errors=True)
        if not os.path.exists(TMP):
            break
        time.sleep(0.5)

if __name__ == '__main__':
    unittest.main(verbosity=2)
