"""Offline tests for elklock's lock: a scratch lock folder and fake sessions, no unit touched
(release runs with --no-snapshot). Windows and Linux:  python test_elklock.py"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ELKLOCK = os.path.join(HERE, 'elklock.py')
TMP = tempfile.mkdtemp(prefix='elklock-test-')
ENV = dict(os.environ, ELKLOCK_DIR=TMP, ELKBENCH_CONFIG=os.path.join(TMP, 'none.toml'))
sys.path.insert(0, HERE)
os.environ.update(ELKLOCK_DIR=TMP, ELKBENCH_CONFIG=ENV['ELKBENCH_CONFIG'])
import elklock      # noqa: E402


def run(session, *args, pid=None):
    env = dict(ENV, CLAUDE_CODE_SESSION_ID=session)
    if pid:
        env['CLAUDE_PID'] = str(pid)
    return subprocess.run([sys.executable, ELKLOCK, *args], env=env, capture_output=True, text=True,
                          timeout=60)


def free(dev, within=5.0):
    end = time.time() + within
    while time.time() < end:
        if elklock.holder(dev) is None:
            return True
        time.sleep(0.1)
    return False


class Port(unittest.TestCase):
    def test_port_of_and_connected(self):
        # built-in name; ELKBENCH_<DEV>_PORT (or units.<dev>.port) for a unit behind a MIDI host
        self.assertEqual(elklock.port_of('rytm'), 'Analog Rytm')
        sidecar = ['Sidecar | Expander (Pi)']
        self.assertFalse(elklock.connected('rytm', sidecar))
        self.assertTrue(elklock.connected('rytm', sidecar, 'Sidecar | Expander (Pi)'))
        os.environ['ELKBENCH_RYTM_PORT'] = 'Sidecar | Expander (Pi)'
        try:
            self.assertEqual(elklock.port_of('rytm'), 'Sidecar | Expander (Pi)')
            self.assertTrue(elklock.connected('rytm', sidecar))
            self.assertFalse(elklock.connected('rytm', ['Analog Rytm MKII']))
            self.assertEqual(elklock.port_of('a4'), 'Analog Four')
        finally:
            del os.environ['ELKBENCH_RYTM_PORT']

    def test_release_port_is_for_one_device(self):
        r = run('test-a', 'release', 'rytm', 'a4', '--port', 'Sidecar', '--no-snapshot')
        self.assertEqual(r.returncode, 2)
        self.assertIn('one device', r.stdout)


class Din(unittest.TestCase):
    """A DIN MIDI interface from elkbench's config is a device of its own (din-NAME)."""

    def setUp(self):
        self.cfg = os.path.join(TMP, 'din.toml')
        with open(self.cfg, 'w', encoding='utf-8') as fh:
            fh.write("[din.u2midi]\nport = 'U2MIDI Pro'\n")
        self.env = dict(ENV, ELKBENCH_CONFIG=self.cfg)

    def tearDown(self):
        subprocess.run([sys.executable, ELKLOCK, 'release', 'din-u2midi', 'a4', '--no-snapshot'],
                       env=dict(self.env, CLAUDE_CODE_SESSION_ID='test-d'), capture_output=True, timeout=60)

    def run_d(self, session, *args, stdin=None):
        return subprocess.run([sys.executable, ELKLOCK, *args], env=dict(self.env, CLAUDE_CODE_SESSION_ID=session),
                              capture_output=True, text=True, timeout=60, input=stdin)

    def hook(self, session, cmd):
        r = self.run_d(session, 'hook', stdin=json.dumps({'tool_name': 'Bash', 'session_id': session,
                                                          'tool_input': {'command': cmd}}))
        return json.loads(r.stdout)['hookSpecificOutput']['permissionDecisionReason'] if r.stdout.strip() else ''

    def test_claim_release_and_hook(self):
        r = self.run_d('test-d', 'claim', 'din-u2midi', '--for', 'din test')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.run_d('test-e', 'claim', 'din-u2midi', '--for', 'x').returncode, 3)   # one holder
        # a command naming the interface needs its claim; the interface's claim alone is not a unit's
        cmd = 'python analogrun.py --port "Analog Four" --din "U2MIDI Pro" do "send k.syx din"'
        self.assertIn('a4', self.hook('test-d', cmd))
        self.assertIn('din-u2midi', self.hook('test-e', 'python dinprobe.py "U2MIDI Pro" --json'))
        self.assertEqual(self.hook('test-d', 'python dinprobe.py "U2MIDI Pro" --json'), '')
        self.assertIn('unit not named', self.hook('test-d', 'python analogrun.py info'))
        r = self.run_d('test-d', 'release', 'din-u2midi', '--note', 'x')
        self.assertIn('released din-u2midi', r.stdout)
        self.assertTrue(free('din-u2midi'))


class Lock(unittest.TestCase):
    def tearDown(self):
        for s in ('test-a', 'test-b'):
            run(s, 'release', '--all', '--no-snapshot')
        free('rytm')

    def test_claim_busy_release(self):
        r = run('test-a', 'claim', 'rytm', '--for', 'lock test')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(elklock.mine(elklock.holder('rytm'), session='test-a'))
        self.assertIsNone(elklock.open_exclusive(elklock.path('rytm', '.lock')))
        r = run('test-b', 'claim', 'rytm', '--for', 'second')
        self.assertEqual(r.returncode, 3, r.stdout)
        self.assertIn('BUSY', r.stdout)
        r = run('test-a', 'release', 'rytm', '--no-snapshot', '--note', 'done')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(free('rytm'))
        with open(elklock.path('rytm', '.handoff.json'), encoding='utf-8') as fh:
            self.assertEqual(json.load(fh)['note'], 'done')

    def test_all_or_nothing(self):
        self.assertEqual(run('test-a', 'claim', 'a4', '--for', 'one').returncode, 0)
        r = run('test-b', 'claim', 'rytm', 'a4', '--for', 'both')
        self.assertEqual(r.returncode, 3)
        self.assertIsNone(elklock.holder('rytm'))

    def test_session_exit_frees_the_unit(self):
        session = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            r = run('test-a', 'claim', 'rytm', '--for', 'until exit', pid=session.pid)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIsNotNone(elklock.holder('rytm'))
        finally:
            session.kill()
            session.wait()
        self.assertTrue(free('rytm', 5.0))

    def hook(self, session, cmd):
        r = subprocess.run([sys.executable, ELKLOCK, 'hook'], env=ENV, capture_output=True, text=True,
                           input=json.dumps({'tool_name': 'Bash', 'session_id': session,
                                             'tool_input': {'command': cmd}}), timeout=30)
        return json.loads(r.stdout)['hookSpecificOutput']['permissionDecisionReason'] if r.stdout.strip() else ''

    def test_listening_holds_the_unit(self):
        r = run('test-a', 'claim', 'rytm', '--for', 'listen test', '--idle', '2s')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(run('test-a', 'listen', 'rytm', '--for', 'the kick').returncode, 0)
        time.sleep(4)                                     # twice the idle time: still held
        self.assertIn('LISTENING', elklock.describe('rytm', elklock.holder('rytm')))
        r = run('test-a', 'release', 'rytm', '--no-snapshot')
        self.assertEqual(r.returncode, 3)
        self.assertIn('still listening', r.stdout)
        self.assertIn('the person is listening', self.hook('test-a', 'python analogrun.py --port "Analog Rytm" info'))
        self.assertIn('Only the person', self.hook('test-a', 'python elklock.py heard rytm --by me'))
        self.assertEqual(self.hook('test-a', 'python elklock.py status'), '')
        r = run('test-a', 'heard', 'rytm', '--by', 'thread message cmsg_x')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(elklock.holder('rytm')['heard'][0]['by'], 'thread message cmsg_x')
        self.assertTrue(free('rytm', 6.0))                # idle again: it ends by itself
        with open(elklock.path('rytm', '.handoff.json'), encoding='utf-8') as fh:
            h = json.load(fh)
        self.assertEqual(h['ended'], 'idle')
        self.assertIn('ended by itself', h['note'])

    def test_hook_reads_tool_names_in_file_paths_as_paths(self):
        # no claim held: only a command that talks to a unit is denied
        self.assertEqual(self.hook('test-a', 'cat "D:/runs/r1/elkflash.log"'), '')
        self.assertEqual(self.hook('test-a', 'cp runs/r1/elkflash.log evidence/ && tail -5 evidence/elkflash.log'), '')
        self.assertEqual(self.hook('test-a', 'Get-Content runs\\elkflash.log | Select-String fail'), '')
        self.assertIn('holds no claim', self.hook('test-a', 'cat elkflash.log; python -m elkflash upgrade x.syx --port "Elektron Analog Rytm MKII"'))
        self.assertIn('holds no claim', self.hook('test-a', 'cat $(python -m elkflash info --port "Elektron Analog Rytm MKII")'))

    def test_hook_reads_tool_names_in_text_and_names_as_text(self):
        # no claim held: sed, git messages, heredocs and longer names are not unit commands
        for cmd in ["sed -n 1,40p tools/analogusb.py",
                    "cd Repos/analoghealth-gap10 && git add tools/analogrun.py && git commit -m 'analogrun: pad shots; analogusb too'",
                    "git commit -F - <<'EOF'\nanalogusb: pad shots\n\npython analogrun.py --port x\nEOF",
                    "git commit -m \"$(cat <<'EOF'\nanalogrun pad shots\nEOF\n)\"",
                    "python tools/test_analogusb.py -v",
                    "cd D:/Development/Repos/analoghealth-gap10 && ls tools",
                    "echo analogrun | tr a-z A-Z",
                    "grep -n 'def pads' tools/analogusb.py | head -3",
                    "cd /d/Repos/rytmhealth/tools && grep -n 'def open_port' analogrun.py",
                    "export STOCK=/d/fw/Analog-Rytm_MKII_OS1.74.syx A4=/d/fw/Analog-Four_MKII_OS1.56.syx; python tests/test_keys.py"]:
            self.assertEqual(self.hook('test-a', cmd), '', cmd)
        for cmd in ["cd tools && python analogrun.py --port \"Elektron Analog Rytm MKII\" info",
                    "git log -1; python tools\\analogusb.py --port \"Elektron Analog Rytm MKII\" keys TAP",
                    "echo $(python -m elkflash info --port \"Elektron Analog Rytm MKII\")",
                    "find . -name '*.syx' -exec python -m elkflash upgrade {} \\;",
                    "git commit -m 'list' && python analogrun.py --port \"Elektron Analog Rytm MKII\" info"]:
            self.assertIn('holds no claim', self.hook('test-a', cmd), cmd)
        # the unit comes from --port, not from a folder name
        r = run('test-a', 'claim', 'a4', '--for', 'path test')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.hook('test-a', 'cd D:/Repos/rytmhealth/tools; python analogrun.py --port "Analog Four" data ls /projects'), '')
        self.assertIn('rytm', self.hook('test-a', 'cd D:/Repos/rytmhealth/tools; python analogrun.py --port "Elektron Analog Rytm MKII" info'))
        run('test-a', 'release', 'a4', '--no-snapshot')

    def test_hook_names_the_rytm_behind_sidecar(self):
        side = 'python analogrun.py --port "Sidecar | Expander (Pi)" info'
        envs = ["$env:ELKBENCH_RYTM_PORT = 'Sidecar | Expander (Pi)'; python analogrun.py info",
                "export ELKBENCH_RYTM_PORT='Sidecar | Expander (Pi)'; python -m elkflash info --port x",
                "ELKBENCH_RYTM_PORT='Sidecar | Expander (Pi)' python analogrun.py --port \"Sidecar | Expander (Pi)\" info"]
        self.assertIn('rytm', self.hook('test-a', side))
        # another unit's claim does not cover the Rytm behind the host
        self.assertEqual(run('test-a', 'claim', 'a4', '--for', 'sidecar test').returncode, 0)
        for cmd in [side] + envs:
            self.assertIn('rytm', self.hook('test-a', cmd), cmd)
        # the host's name in a path names no unit
        self.assertEqual(self.hook('test-a', 'cd D:/Projects/Sidecar/port-174; python analogrun.py --port "Analog Four" info'), '')
        run('test-a', 'release', 'a4', '--no-snapshot')
        self.assertEqual(run('test-a', 'claim', 'rytm', '--for', 'sidecar test').returncode, 0)
        for cmd in [side] + envs:
            self.assertEqual(self.hook('test-a', cmd), '', cmd)
        run('test-a', 'release', 'rytm', '--no-snapshot')

    def test_stall_marks_the_unit_until_a_watched_power_cycle(self):
        py = sys.executable
        self.assertEqual(run('test-a', 'claim', 'a4', '--for', 'stall test').returncode, 0)
        try:
            # elkflash upgrade exits 5 under run: the unit is marked stalled
            r = run('test-a', 'run', 'a4', '--', py, '-c', 'import sys; sys.exit(5)', 'elkflash', 'upgrade', 'x.syx')
            self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
            self.assertIn('STALLED', r.stdout)
            self.assertIsNotNone(elklock.stalled('a4'))
            self.assertEqual(run('test-a', 'stalled', 'a4').returncode, 3)
            # nothing else runs on it, not even a reflash; the hook says the same
            r = run('test-a', 'run', 'a4', '--', py, '-c', 'pass', 'analogrun', 'shot')
            self.assertEqual(r.returncode, 3)
            self.assertIn('do not reflash', r.stdout)
            self.assertIn('STALLED', self.hook('test-a', 'python analogrun.py --port "Analog Four" info'))
            self.assertIn('STALLED', self.hook('test-a', 'python -m elkflash info --port "Analog Four"'))
            self.assertEqual(self.hook('test-a', 'python elklock.py run a4 -- python -m elkflash usb-cycle '
                                                 '--port "Analog Four"'), '')
            self.assertIn('Only the person', self.hook('test-a', 'python elklock.py unstall a4 --by x --yes'))
            self.assertIn('STALLED', elklock.describe('a4', elklock.holder('a4')))
            # no handoff read of a stalled unit
            self.assertIn('STALLED', elklock.snapshot('a4', 'n', False)['os'])
            # usb-cycle that never saw it leave keeps the mark; its pass clears it
            r = run('test-a', 'run', 'a4', '--', py, '-c', 'import sys; sys.exit(5)', 'elkflash', 'usb-cycle')
            self.assertEqual(r.returncode, 5)
            self.assertIsNotNone(elklock.stalled('a4'))
            r = run('test-a', 'run', 'a4', '--', py, '-c', 'pass', 'elkflash', 'usb-cycle')
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertIn('no longer stalled', r.stdout)
            self.assertIsNone(elklock.stalled('a4'))
            with open(elklock.path('a4', '.stalls.jsonl'), encoding='utf-8') as fh:
                self.assertIn('usb-cycle', fh.read())
            # an ordinary failure of an upgrade marks nothing
            run('test-a', 'run', 'a4', '--', py, '-c', 'import sys; sys.exit(1)', 'elkflash', 'upgrade', 'x.syx')
            self.assertIsNone(elklock.stalled('a4'))
        finally:
            elklock.clear_stalled('a4', 'test')
            run('test-a', 'release', 'a4', '--no-snapshot')

    def test_unstall_is_the_persons(self):
        elklock.mark_stalled('a4', 'test')
        r = run('test-a', 'unstall', 'a4', '--by', 'x')
        self.assertNotEqual(r.returncode, 0)
        self.assertIsNotNone(elklock.stalled('a4'))
        r = run('test-a', 'unstall', 'a4', '--by', 'the person, by hand', '--yes')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIsNone(elklock.stalled('a4'))

    def test_pid_alive(self):
        self.assertTrue(elklock.pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        self.assertFalse(elklock.pid_alive(p.pid))


if __name__ == '__main__':
    unittest.main(verbosity=2)
