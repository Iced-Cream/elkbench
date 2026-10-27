# Testing on hardware: practices

How to run tests on an Elektron unit (Analog Rytm MKII, Analog Four MKII, Digitone, Digitakt) with
elkbench, whether a person or an AI agent session drives them. elkbench enforces some of these
rules itself; each section says which ones. The rest is up to the session, and a session that
uses elkbench is expected to follow all of them.

The aim is a run that leaves the unit as it found it, never touches the owner's own work, and
reaches the hardware only with something that has already worked in the emulator.

## 1. The emulator comes first

- **Every build is tested in the emulator before a unit sees it**: the emulator flash and
  boot, then the test steps themselves, with a pass. The hardware run then repeats the full
  test. It checks everything the emulator showed and adds what the emulator can't assess
  (analog voices, real USB audio, the panel, the real flash part). A step that fails in the
  emulator is not tried on the unit.
- **The gate is per flash, not per build.** Every flash in the run is run in the emulator as
  the same transition, in the same order: what is on the unit, then what goes on. That covers
  build-to-build steps and the restore to stock over the last build. A transition passes when
  the upgrade completes: the flash is erased and written, the unit reboots, and the new OS
  version is checked. A build that flashes onto stock says nothing about whether it can be
  flashed away from.
- **A gate step that can't run is a failure, not a skip.** If one emulator can't do a step,
  use another, or name the gap in the plan and get the owner's explicit yes for that step.
- Every test plan ends with the restore to stock, and that flash passes in the emulator too.
- **The emulator's card is not the unit's.** Each emulator run starts from a fresh copy of its
  card, so its project slots say nothing about the unit's. A plan handles the slot it writes as
  it finds it on the unit at run time (empty, or a test item to clear first), and its emulator
  run fills that slot first so the clearing path is tested too.

*elkbench enforces*: a hardware plan or flash needs a passing emulator run of the same steps,
reference screens and OS file (sha256). A plan that flashes gets that emulator run named for
it, and a pass whose OS file differs is refused before the owner is asked. An emulator run of a
Rytm or A4 plan reads every saved project before and after the steps (read only, through the
data store), and fails if one was added, removed, renamed or changed outside the slots the plan
saves into. Going back to the
emulator gives the unit back (section 4).

## 2. One go per run

- **The owner approves the whole run once**: the unit, the builds with their sha256 and
  emulator results, every flash in order including the stock restore, every write with its
  target (a slot, or "kit 1 of the active project (a test project)") and the settings it will
  change. It never promises that a slot is empty: it says what happens if the slot is occupied. Anything outside that (another build, another unit, a slot
  not named) needs a new go.
- **Ask first, claim after the yes.** Nobody is blocked while the owner decides, and the go
  lasts 2 hours. After the yes, claim the unit (queueing if need be) and check its state before
  writing: the handoff note, the OS, the slots the run will write. If the state no longer
  matches what the go covered, ask again.
- Record the unit's state at the start (OS version, a screenshot, the project list) and compare
  at the end.

*elkbench enforces*: hardware plans, flashes and Total Recall restores need an approval token.
A token is bound to the session, covers each item once, and expires after 2 h. `approve_plan`
refuses while the session holds a unit it isn't using. Every hardware call needs the session's
own claim (elklock).

## 3. During a run

- **No key stays held.** Every key session ends with a cancel, and a failed run still lets go
  of keys and the claim.
- **Audio at a safe level.** Never touch the main volume, keep test signals at about -24 dBFS,
  and assume the unit is connected to speakers.
- **No power cycling from software**, and no factory or service console commands. They bypass
  the OS's own safety.
- Change USB CONFIG only when the test needs it, and put it back. A switch drops Overbridge and
  the audio devices for anything else running.
- **Every wait on the unit has a timeout** (flash, reboot, a long transfer). Report a stuck
  unit; don't retry it blindly.
- **Stop at the first unexpected state**: a screen that doesn't match, a reply that isn't the
  one expected, a unit that doesn't answer. Take a screenshot, let go of keys, record the
  unit's state, then report. Don't improvise a recovery. A power cycle or the unit's start-up
  menu is the owner's call.
- **A stalled flash ends the run**: it shows PREPARING but never erases the flash, there's no
  reply, or it times out. Don't flash again as a recovery attempt, not even stock. The owner
  decides what happens next.

*elkbench enforces*: OS upgrades run under the claim, so it can't end mid-flash, and elkflash
waits for the restart with a timeout. The rest of this section is up to the session.

## 4. The claim

- **One writer per unit.** Claim before anything that talks to a unit, read the previous
  holder's handoff note, and never break someone else's claim.
- **Going back to the emulator gives the unit back.** If a test has to change mid-run, release
  the unit (with a note naming the OS left on it, even a test build), fix and re-run in the
  emulator, then claim again. Never hold hardware while doing emulator work.
- **Release the moment you're done**, also after a failure, with a note on the state left: the
  OS, the active project, open menus, changed settings.

*elkbench enforces*: an emulator call from a session holding a unit releases it first, unless
the session's own hardware run is still going or someone is listening. A claim ends after
15 min with no device use (never during a run or a listening test), and a claim that ends by
itself records the OS and screen in its handoff note. `release` writes the handoff note and a
row in the unit's test log, and asks for the stock OS first.

## 5. Listening tests

- When a test needs a person to listen, **keep the claim for the whole listening test** and
  **send nothing to the unit until they say they have finished**: no keys, MIDI, screenshots,
  flashes or release, and no moving on to the next step. A fixed wait is never a substitute
  for their answer.
- Before asking, put the unit in the state to be heard, with a sound that is actually audible
  (check the level and the MIDI channel), and say exactly what to listen for.

*elkbench enforces*: `listen` (or a `listen TEXT` step in a plan, which pauses the run) holds
the unit. The claim can't time out, every call to that unit is refused, the elklock hook denies
device commands, and release waits. Only the person ends it: `listen_done` with their reply, or
their answer to the client's prompt. A session can't end it itself.

## 6. The owner's own work

- **Never write over what isn't the test's.** Decide on a way to recognise test items (a name
  prefix, reserved project slots) and only write, rename, clear or delete items that carry it,
  or slots that are empty. Everything else is read-only.
- Kits and sounds cross projects. A kit or sound dump received lands in the *active* project,
  and the +Drive sound library and samples are shared. So make a test project active before
  sending any dump, and mark anything saved to the +Drive.
- **Write to the active project only once it is known to be a test project**, in the same run.
  Make the test project with SAVE PROJECT AS, which makes the saved copy the active one; a new
  project saved into another slot (INIT NEW and a save) leaves the previous project active, and
  a write meant for the test project lands in that one instead. If the run can't confirm which
  project is active, it writes nothing to it.
- **Loading another project drops unsaved changes** in the working one. Before the first load,
  know that the working project is saved, or ask the owner. Never save their project yourself.
- **Global settings**: don't save them. If a test needs a changed setting, change the live
  setting and put it back before release; list it in the handoff note.
- **Leave the unit on its stock OS** at the end, and say so in the handoff note.
- Test items stay unless the plan says to clean up; the handoff note lists them.

**An example: the rules elkbench was developed with.** They come with it as a ready write
policy, `examples/write-policy/policy.py` (tests beside it).
- **A name prefix:** everything a test creates or changes is named `~T ` followed by a name
  (`~T PROJ 1`, `~T KIT A`). A session may write, overwrite, rename or clear only an item whose
  name starts with `~T`, or an empty slot, and it names an empty slot `~T ...` in the same step.
  `~` is accepted everywhere a name goes (the unit's name editor, Transfer's rename, SysEx and
  the data store), it should sort after letters and digits (not checked), and nobody names their own work with a
  leading tilde.
- **Reserved project slots:** 121-128 are for tests; the owner keeps 1-120. Outside them, only
  items already named `~T` may be touched.
- **The active project:** the unit doesn't say which project is active, so by convention a test
  project names its kit 1 `~T ...`, and the policy checks the active kit 1 before any kit or
  sound dump is sent. On the Digitone and Digitakt the marker is pattern A01's name instead
  (`~T ...`), read back with a stored-pattern dump request, and there is no key-driven kit
  save to check. The policy never names the marker itself: after a save it can't tell which
  project is active, so a plan names its test project's marker on the unit.
- Names: at most 15 characters (the unit stores 16), upper case, from A-Z, 0-9, space and
  `+ - = & / # @ ? % $ ~`. The unit drops any other character silently.
- A save into a test slot that already holds a project empties it first, keeping a copy in the
  run folder: SAVE PROJECT AS asks an extra question when the slot holds another project.

*elkbench enforces*: writes on a unit go through analoghealth's runner, which asks a **local
write policy** before every write, rename, clear, dump and key-driven save; a refusal stops the
step before anything is sent. The policy is a Python file on your computer (the hooks are in
analoghealth's `tools/writepolicy.py`): use the example as it is, change its prefix and slots,
or write your own. With `require_write_policy = true` in `config.toml`, hardware tools stay off
while no policy is installed.

## 7. Shared things

- **The Overbridge Engine is shared** by every unit in Overbridge mode. Anything that drives it
  (Total Recall, a plugin or a DAW run by a session), or quits or restarts Overbridge, holds a
  claim on every unit, connected or not, since a unit switched on midway joins the same Engine.
  Ask the person before quitting or restarting Overbridge.
- Never drive Transfer's or Overbridge's own windows unattended; those steps are the person's.
  Close Transfer's use of a port before MIDI work.

*elkbench enforces*: Total Recall needs claims on all three units; the elklock hook does the
same for commands that drive or stop the Engine.

## 8. Remote control on stock firmware

A stock OS has no remote keys, so a test that drives stock behaviour from a computer flashes a
remote-only build instead: the stock OS with only the remote keys (and encoder turns) patched
in. `standalone/patch.py` in [analoghealth](https://github.com/Iced-Cream/analoghealth) builds
one for the Analog Rytm MKII and Analog Four MKII, and the same in
[digihealth](https://github.com/irpina/digihealth) for the Digitone, from your own stock OS
file. There is none for the Digitakt yet.

- It is still a build: the emulator gate, one go per run and the restore to stock all apply.
- **Name it in results** ("stock 1.74 + remote-only build"). A result counts as stock behaviour
  when it is about what the OS does with the input (menus, pages, values, the sequencer, saved
  data, sound) and the same input on the stock image in the emulator agrees.
- **Not for** tests of the input path itself (encoder acceleration, dead zones, key repeat,
  timing finer than a UI tick), SysEx handling, or the firmware file and upgrade path: there the
  patch differs from stock. Such results need a hand check on true stock.
- A remote turn back by the same counts can land off its start, so plans must not assume a turn
  undoes itself.
- **Size a turn for what it drives.** Remote turns are raw encoder counts, and the OS decides
  what they do.
  - A parameter page moves with a few counts.
  - A list menu needs many. Measured on a Rytm MKII with OS 1.74, LFO DESTINATION list, with
    counts sent 8 per UI tick:
    - 16 counts from rest move nothing, 32 reach the 2nd entry;
    - after that it takes about 16-32 counts per entry.
    - The emulator agrees, except that it moves at 16.
    - Sent 1 per tick, the emulator needs 42-56 for the first entry and 24-32 per entry after it.
  - Move a list with one large turn (e.g. `turn D 32 8` for the first entry, `turn D 32 8` for
    each one or two after), not many small ones.
  - Check where it landed with a shot or `expect-screen` rather than counting turns.
- **A turn that seems to do nothing**: before reporting a tool fault, send the same counts as
  panel turns in the emulator (dtboot `--turn ENC:DELTA@ATxCOUNT/SPACING`) and compare. If those
  don't move it either, the counts were too few.

*elkbench enforces*: release asks for stock over a remote-only build like any other. List
remote-only builds under `no_stats` in `config.toml` and plans or steps with a `stats` step are
refused on them, since their channel has no stats command.

## 9. Evidence and reporting

- Every run keeps its evidence (screenshots, recordings, logs, flash logs) in a run folder of
  its own, outside any public repo. Captures that show the owner's own project names or samples
  are never published.
- Every hardware run gets a row in the unit's test log the same day: the date, each build and
  its sha256, what was checked, and the result.
- The handoff note names the active project the unit is left with (its name, and whether it is a
  test project); the next session checks that on the unit before writing to the active project.
- What a session learns about a unit or a tool (a stuck reboot, a timing, an odd behaviour)
  gets written down where the next session will find it.
