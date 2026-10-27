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

    def test_pid_alive(self):
        self.assertTrue(elklock.pid_alive(os.getpid()))
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        self.assertFalse(elklock.pid_alive(p.pid))


if __name__ == '__main__':
    unittest.main(verbosity=2)
