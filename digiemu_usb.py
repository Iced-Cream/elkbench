# SPDX-License-Identifier: GPL-2.0-or-later
"""One session's Digitone emulator: digiemu booting an OS .syx headless, its USB MIDI port on TCP.

Runs under digiemu's own Python (in WSL on Windows), started by elkbench's emulator tool:

    python digiemu_usb.py REPO SYX PORT MINUTES TAG [--firmware-dir DIR] [--home DIR]
                          [--setup-key KEY [--require-setup] [--keep N] [--days D]]

The .syx is set up once in a digiemu home (digiemu's `--add`: extract, cold boot, settle;
about half a minute), shared by every session. Each start copies that folder to a work
folder of its own (TAG), so the session's card and any OS upgrade stay its own, then runs
digiemu's tools/usbrun.py on the settled snapshot: the link listens on 127.0.0.1:PORT, an
OS upgrade over it reboots into the new OS, and it stops after MINUTES.

Setup snapshots (--setup-key KEY, a digiemu whose usbrun.py has --stdin-commands):
the home's setup-snapshots/KEY folder holds a machine saved after a plan's setup
steps passed, and the +Drive it was saved with. When it is there, the session
boots that pair instead of the settled snapshot ("resuming setup snapshot KEY");
--require-setup makes its absence an error. Either way `save` on stdin saves the
running machine; once usbrun has written it, the pair is moved into
setup-snapshots/KEY ("setup snapshot KEY stored"), unless another session stored
one first. Folders unused for --days, and beyond the --keep most recently used,
are deleted at each start.
"""
import argparse
import os
import shutil
import sys
import time


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('repo')
    ap.add_argument('syx')
    ap.add_argument('port', type=int)
    ap.add_argument('minutes', type=float)
    ap.add_argument('tag')
    ap.add_argument('--firmware-dir', default=None,
                    help='a folder where the OS files flashed into it are put: a reboot needs the one written')
    ap.add_argument('--ob-audio-port', type=int, default=None,
                    help='also serve the twelve Overbridge audio channels on this TCP port '
                         '(a digiemu whose usbrun.py has --ob-audio-port)')
    ap.add_argument('--setup-key', default=None,
                    help='resume this setup snapshot if stored, and store it on `save`')
    ap.add_argument('--require-setup', action='store_true',
                    help='with --setup-key: stop if that snapshot is not stored')
    ap.add_argument('--keep', type=int, default=10, help='setup snapshots kept at most')
    ap.add_argument('--days', type=float, default=7, help='days an unused setup snapshot is kept')
    ap.add_argument('--home', default=os.path.join(
        os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state'), 'elkbench', 'digiemu'))
    args = ap.parse_args(argv)
    sys.path[:0] = [args.repo, os.path.join(args.repo, 'tools')]
    os.chdir(args.repo)
    from emu import bootstrap, portable

    print('[elkbench] setting up %s in %s' % (os.path.basename(args.syx), args.home), flush=True)
    rc = portable.add_firmware(args.syx, yes=True, home=args.home)
    if rc:
        print('[elkbench] digiemu could not set it up (exit %d)' % rc, flush=True)
        return rc
    fwdir = portable.plan_add(args.syx, args.home).fwdir
    work = os.path.join(args.home, 'sessions', args.tag)
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(fwdir, work, ignore=shutil.ignore_patterns('logs', 'reboots'))
    paths = portable.open_paths(work)
    os.environ.update(paths.env())
    import usbrun
    extra, snap = [], None
    try:
        if args.setup_key:
            store = os.path.join(args.home, 'setup-snapshots')
            prune(store, args.setup_key, args.keep, args.days)
            kept = os.path.join(store, args.setup_key)
            if os.path.isfile(os.path.join(kept, 'setup.snap')) and os.path.isfile(
                    os.path.join(kept, 'setup.snap.card')):
                os.utime(kept)                           # last use, for prune
                usbrun.copy_card(os.path.join(kept, 'setup.snap.card'), paths.card)
                shutil.copyfile(os.path.join(kept, 'setup.snap'), os.path.join(work, 'setup.snap'))
                snap = os.path.join(work, 'setup.snap')
                print('[elkbench] resuming setup snapshot %s' % args.setup_key, flush=True)
            elif args.require_setup:
                print('[elkbench] no setup snapshot %s is stored' % args.setup_key, flush=True)
                return 4
            else:
                print('[elkbench] no setup snapshot %s yet' % args.setup_key, flush=True)
            saved = os.path.join(work, 'save', 'setup.snap')
            os.makedirs(os.path.dirname(saved))
            extra = ['--stdin-commands', '--save-to', saved]
            sys.stdout = Watch(sys.stdout, '[usbrun] saved %s' % saved,
                               lambda: publish(saved, store, args.setup_key, args.tag))
        if not snap:
            snap, why = bootstrap.choose_snapshot(paths)
            if not snap:
                print('[elkbench] no settled snapshot in %s (%s)' % (work, why), flush=True)
                return 1
        print('[elkbench] booting %s from %s' % (os.path.basename(args.syx), snap), flush=True)
        return run(usbrun, snap, paths, work, args, extra)
    finally:
        sys.stdout = sys.__stdout__
        shutil.rmtree(work, ignore_errors=True)      # a card copy and maybe a reboot: ~300 MB


def run(usbrun, snap, paths, work, args, extra=()):
    return usbrun.main([snap, '--port', str(args.port), '--seconds', str(args.minutes * 60),
                        '--firmware', paths.syx, '--workdir', os.path.join(work, 'reboots'),
                        '--report', '60'] + (['--firmware', args.firmware_dir] if args.firmware_dir else [])
                       + (['--ob-audio-port', str(args.ob_audio_port)] if args.ob_audio_port else [])
                       + list(extra))


class Watch:
    """A stdout that calls `then()` once a line starting with `line` has been written."""

    def __init__(self, out, line, then):
        self.out, self.line, self.then, self.buf = out, line, then, ''

    def write(self, text):
        n = self.out.write(text)
        self.buf += text
        while '\n' in self.buf:
            first, self.buf = self.buf.split('\n', 1)
            if first.startswith(self.line):
                self.out.flush()
                self.then()
        return n

    def flush(self):
        self.out.flush()

    def __getattr__(self, name):
        return getattr(self.out, name)


def publish(saved, store, key, tag):
    """Move a saved pair (saved, saved.card) into store/key, unless one is there already."""
    tmp = os.path.join(store, '.%s.%s' % (key, tag))
    try:
        shutil.rmtree(tmp, ignore_errors=True)
        os.makedirs(tmp)
        os.replace(saved, os.path.join(tmp, 'setup.snap'))
        os.replace(saved + '.card', os.path.join(tmp, 'setup.snap.card'))
        try:
            os.rename(tmp, os.path.join(store, key))
            print('[elkbench] setup snapshot %s stored' % key, flush=True)
        except OSError:                              # another session stored it first
            shutil.rmtree(tmp, ignore_errors=True)
            print('[elkbench] setup snapshot %s stored (kept the one already there)' % key, flush=True)
    except OSError as e:
        shutil.rmtree(tmp, ignore_errors=True)
        print('[elkbench] setup snapshot %s NOT stored: %s' % (key, e), flush=True)


def prune(store, keep_key, keep, days):
    """Delete setup snapshots unused for `days`, and beyond the `keep` most recently used."""
    if not os.path.isdir(store):
        return
    now = time.time()
    snaps = []
    for name in os.listdir(store):
        p = os.path.join(store, name)
        if name.startswith('.'):                     # a publish that died part way
            if now - os.path.getmtime(p) > 3600:
                shutil.rmtree(p, ignore_errors=True)
        elif name != keep_key and os.path.isdir(p):
            snaps.append((os.path.getmtime(p), p))
    snaps.sort(reverse=True)
    for n, (used, p) in enumerate(snaps):
        if used < now - days * 86400 or n >= keep - 1:
            shutil.rmtree(p, ignore_errors=True)


if __name__ == '__main__':
    sys.exit(main())
