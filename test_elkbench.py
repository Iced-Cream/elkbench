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
    fh.write('person = "the tester"\nevidence_root = %s\n\n[tools]\n'
             % toml_str(os.path.join(TMP, 'evidence')))
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
            self.assertEqual(len(names), 19)
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
        for f in ('daemon.py', 'launcher.py', 'batch.py'):
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
        self.assertEqual(self.L.read()['method'], 'elicitation/create')


class InProcess(unittest.TestCase):
    def setUp(self):
        forget_passes()
        self.d = daemon.Daemon()
        self.s = daemon.Session('test-session-c', os.getpid(), TMP)
        self.folder = os.path.join(TMP, 'ARMKII')
        self.saved = dict(daemon.UNITS['rytm'])
        daemon.UNITS['rytm']['evidence'] = os.path.join(self.folder, 'runs')
        self.started = []
        self.d.start = lambda job, cmd, cwd, done, port=None: (self.started.append((job, cmd)),
                                                               setattr(self, 'done', done),
                                                               port and port.release(), ['running'])[3]
        daemon.ask_person = lambda title, text: True

    def tearDown(self):
        daemon.UNITS['rytm'].update(self.saved)
        subprocess.run([sys.executable, daemon.ELKLOCK, 'release', 'rytm', '--no-snapshot'],
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
        with self.assertRaisesRegex(daemon.Refused, 'claim rytm first'):
            self.d.t_run_plan(self.s, a)
        r = subprocess.run([sys.executable, daemon.ELKLOCK, 'claim', 'rytm', '--for', 'elkbench test'],
                           env=self.s.env(), capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        other = daemon.Session('test-session-d', os.getpid(), TMP)
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
        self.assertEqual(job.lines[:3], ['sent 1048576 bytes in 35 chunks, 40.0 s (25.6 KiB/s), 0 resends',
                                         'Committed: the device is erasing and writing its flash now and '
                                         'restarts by itself.',
                                         'ok: back after 31 s: Analog Rytm MKII, OS 1.74'])
        self.assertEqual(job.lines[3], 'log: %s' % flog)

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

        def start(s, a, snapshot=None):
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
        with self.assertRaisesRegex(daemon.Refused, 'no digitakt emulator with USB MIDI yet'):
            self.d.t_emulator(self.s, {'action': 'start', 'unit': 'digitakt'})
        saved = daemon.DIGIHEALTH
        daemon.DIGIHEALTH = TMP
        try:
            with self.assertRaisesRegex(daemon.Refused, 'no digitakt emulator with USB MIDI'):
                self.d.t_steps(self.s, {'unit': 'digitakt', 'target': 'emulator', 'steps': 'say hi'})
        finally:
            daemon.DIGIHEALTH = saved
        with self.assertRaisesRegex(daemon.Refused, "step 'turn' is not allowed"):
            self.d.t_steps(self.s, {'unit': 'digitakt', 'target': 'hardware', 'steps': 'turn A 3'})
        with self.assertRaisesRegex(daemon.Refused, "step 'save-kit' is not allowed"):
            self.d.t_steps(self.s, {'unit': 'digitakt', 'target': 'hardware', 'steps': 'save-kit'})
        # the flash gate without a build is digiemu's emu.fwcheck, as on the Digitone
        with self.assertRaisesRegex(daemon.Refused, 'emulator check \\(digiemu\\)'):
            self.d.t_flash(self.s, {'unit': 'digitakt', 'image': img, 'target': 'emulator'})
        with self.assertRaisesRegex(daemon.Refused, 'no digitakt emulator with USB MIDI yet'):
            self.d.t_flash(self.s, {'unit': 'digitakt', 'image': img, 'target': 'emulator', 'build': img})
        with self.assertRaisesRegex(daemon.Refused, 'no emulator for digitakt'):
            self.d.t_batch(self.s, {'image': img, 'unit': 'digitakt', 'build': img, 'variants': [{}]})
        with self.assertRaisesRegex(daemon.Refused, 'no ASIO driver known for digitakt'):
            self.d.t_audio_record(self.s, {'unit': 'digitakt', 'seconds': 1})
        with self.assertRaisesRegex(daemon.Refused, 'no Total Recall for digitakt' if daemon.WINDOWS
                                    else 'Overbridge Engine'):
            self.d.t_recall_take(self.s, {'unit': 'digitakt'})

    def test_digiemu_paths_in_wsl(self):
        saved = daemon.DIGIEMU, daemon.DIGIEMU_PY
        daemon.DIGIEMU, daemon.DIGIEMU_PY = TMP, ['wsl', '~/py']
        try:
            cmd = self.d.digiemu_cmd([daemon.Path(r'D:\x\y.syx'), '7400'])
        finally:
            daemon.DIGIEMU, daemon.DIGIEMU_PY = saved
        self.assertEqual(cmd[:4], ['wsl', '-e', 'bash', '-lc'])
        self.assertTrue(cmd[4].endswith('~/py /mnt/d/x/y.syx 7400'), cmd[4])

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
        self.d.batch_emulator = lambda unit, build, work, minutes, snapshot=None, din=False: (
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

    def test_din_not_on_the_digitone(self):
        with self.assertRaisesRegex(daemon.Refused, 'Rytm and A4 only'):
            self.d.device_do(self.s, 'digitone', 'emulator', 'send %s din' % self.kit_request())

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
        with self.assertRaisesRegex(daemon.Refused, 'emulator first'):
            self.d.t_flash(self.s, {'unit': 'rytm', 'image': img, 'target': 'hardware'})
        with self.assertRaisesRegex(daemon.Refused, 'emulator first'):
            self.d.t_approve_plan(self.s, {'flashes': [{'unit': 'rytm', 'image': img}], 'purpose': 't'})

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
