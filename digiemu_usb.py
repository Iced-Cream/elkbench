# SPDX-License-Identifier: GPL-2.0-or-later
"""One session's Digitone emulator: digiemu booting an OS .syx headless, its USB MIDI port on TCP.

Runs under digiemu's own Python (in WSL on Windows), started by elkbench's emulator tool:

    python digiemu_usb.py REPO SYX PORT MINUTES TAG [--firmware-dir DIR] [--home DIR]

The .syx is set up once in a digiemu home (digiemu's `--add`: extract, cold boot, settle;
about half a minute), shared by every session. Each start copies that folder to a work
folder of its own (TAG), so the session's card and any OS upgrade stay its own, then runs
digiemu's tools/usbrun.py on the settled snapshot: the link listens on 127.0.0.1:PORT, an
OS upgrade over it reboots into the new OS, and it stops after MINUTES.
"""
import argparse
import os
import shutil
import sys


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
    snap, why = bootstrap.choose_snapshot(paths)
    if not snap:
        print('[elkbench] no settled snapshot in %s (%s)' % (work, why), flush=True)
        return 1
    print('[elkbench] booting %s from %s' % (os.path.basename(args.syx), snap), flush=True)
    import usbrun
    try:
        return run(usbrun, snap, paths, work, args)
    finally:
        shutil.rmtree(work, ignore_errors=True)      # a card copy and maybe a reboot: ~300 MB


def run(usbrun, snap, paths, work, args):
    return usbrun.main([snap, '--port', str(args.port), '--seconds', str(args.minutes * 60),
                        '--firmware', paths.syx, '--workdir', os.path.join(work, 'reboots'),
                        '--report', '60'] + (['--firmware', args.firmware_dir] if args.firmware_dir else [])
                       + (['--ob-audio-port', str(args.ob_audio_port)] if args.ob_audio_port else []))


if __name__ == '__main__':
    sys.exit(main())
