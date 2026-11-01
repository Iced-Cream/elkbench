"""An example write policy for analoghealth's runner (its tools/writepolicy.py): the rules one
setup uses so that automated test sessions never touch the owner's own work. Change PREFIX and
TEST_PROJECTS to suit your units, or write your own hooks.

Install: copy it to policy.py in analoghealth's config folder (%LOCALAPPDATA%\\analoghealth on
Windows, ~/.config/analoghealth elsewhere), or point ANALOGHEALTH_POLICY at it. Then set
`require_write_policy = true` in elkbench's config.toml so hardware tools stay off without it.

Everything a test session creates or changes is named with the prefix `~T `. A session may
write, overwrite, rename or clear only an item whose current name starts with `~T`, or an
empty slot, and it gives an empty slot a `~T` name in the same step. Project slots 121-128 are
reserved for tests; outside them only items already named `~T` may be touched. A kit or sound
dump sent to the unit lands in the active project, so the active project must be a test
project first: by convention a test project names its kit 1 `~T ...`, and the active
project's kit 1 is read back (a stored-kit dump request answers from the active project)
before any kit write. On the Digitone and Digitakt (digirun) the marker is pattern A01's name
instead, read back with a stored-pattern dump request. There is deliberately no option to
switch the rules off.

Names follow the unit's name editor: up to 16 characters (15 here, to leave a terminator),
upper case, from A-Z 0-9 space and + - = & / # @ ? % $ ~.
Tests: test_policy.py beside this file.
"""
import os
import re

import unitdata
from writepolicy import Refused

PREFIX = '~T '
NAME_CHARS = set('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 +-=&/#@?%$~')
NAME_MAX = 15
TEST_PROJECTS = range(121, 129)
KIT_REQUEST = 0x62                       # analogrun's stored-kit dump request


def is_test_name(name):
    return bool(name) and name.upper().startswith(PREFIX.rstrip())


def check_new_name(name):
    """-> the name as the unit will store it, or Refused."""
    name = name.upper()
    if not name.startswith(PREFIX):
        raise Refused('a test item\'s name must start with %r: %r' % (PREFIX, name))
    bad = sorted(set(name) - NAME_CHARS)
    if bad:
        raise Refused('%r has characters the unit drops: %s' % (name, ''.join(bad)))
    if len(name) > NAME_MAX or len(name) <= len(PREFIX):
        raise Refused('%r: use %d to %d characters' % (name, len(PREFIX) + 1, NAME_MAX))
    return name


def split_path(path):
    """Test sessions write only /projects/N and /soundbanks/X/N."""
    m = re.fullmatch(r'(/projects|/soundbanks/[A-P])/(\d+)', path)
    if not m:
        raise Refused('test sessions may write only /projects/N and /soundbanks/X/N, not %r' % path)
    return m.group(1), int(m.group(2))


def check_write(rpc, path, new_name=None):
    """Before a write, clear or rename of `path`: -> the ~T name the item must carry
    afterwards. An empty slot needs `new_name`; a ~T item keeps its name unless `new_name`
    is given."""
    parent, ident = split_path(path)
    name, _ = unitdata.target(rpc, path)
    if parent == '/projects' and ident not in TEST_PROJECTS and name is None:
        raise Refused('project slot %d is empty but outside the test slots 121-128' % ident)
    if name is not None and not is_test_name(name):
        raise Refused('%s holds %r, which is not a test item (%r prefix): refused'
                      % (path, name, PREFIX))
    if new_name is not None:
        return check_new_name(new_name)
    if name is None:
        raise Refused('%s is empty: give the new item a %r name (--name)' % (path, PREFIX))
    return name


def marker(session):
    """What marks a test project on this unit: (label, dump request id, name reader, renamer).
    The Rytm and A4 use kit 1; the Digitone and Digitakt (digirun) pattern A01, as their
    kits are not used as the project's marker there."""
    if getattr(session, 'unit', None) in ('digitone', 'digitakt'):
        return 'pattern A01', unitdata.PATTERN_REQUEST, unitdata.pattern_name, unitdata.renamed_pattern_dump
    return 'kit 1', KIT_REQUEST, unitdata.kit_name, unitdata.renamed_kit_dump


def check_active_project(session):
    """The active project must be a test project: its kit 1 (Digitone: pattern A01) is named ~T."""
    what, request, name_of, _ = marker(session)
    try:
        msgs = session.capture(request=session.dump_request(request, 0), wait=5.0, quiet=0.5)
    except TimeoutError:
        raise Refused('the active project\'s %s did not answer: cannot tell it is a test project' % what)
    name = name_of(msgs[0])
    if not is_test_name(name):
        raise Refused('the active project\'s %s is %r, not named %r: load a test project '
                      '(slots 121-128) first' % (what, name, PREFIX))
    return name


def before_dumps(session, msgs):
    if any(unitdata.is_kit_dump(m) for m in msgs):
        check_active_project(session)


def before_save_project(session, path):
    """A save into a ~T slot that holds a project: empty it first (a copy goes to the run
    folder), as the save needs an empty slot."""
    check_write(session.rpc, path)
    name, _ = unitdata.target(session.rpc, path)
    out = os.path.join(session.out, '%s-before.bin' % path.strip('/').replace('/', '-'))
    with open(out, 'wb') as fh:
        fh.write(session.rpc.data_read(path))
    session.rpc.data_clear(path)
    return 'emptied %s (held %r, copy in %s)' % (path, name, os.path.basename(out))


def after_save_project(session, path, name):
    """Reports the active project's marker and writes nothing. SAVE PROJECT AS makes the saved
    copy active, but a project saved some other way (a new one saved into a slot) leaves the
    previous project active, and the unit can't say which one is: marking kit 1 here once
    renamed a kit in the project that was active. (Digitone: its marker is pattern A01.) A plan that sends kits to its test project
    names that project's kit 1 itself (on the unit, after the save), and every dump still
    checks the marker first."""
    what = marker(session)[0]
    try:
        active = check_active_project(session)
        return 'its %s is %r (a test project)' % (what, active)
    except Refused:
        return 'not marked: the active project\'s %s is not a test name, so kit and sound dumps ' \
               'stay refused' % what


def before_save_kit(session):
    return 'active project %s: %r' % (marker(session)[0], check_active_project(session))


def describe_active(kit_name):
    return 'a test project' if is_test_name(kit_name) else 'not a test project'
